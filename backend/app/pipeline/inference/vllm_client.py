"""Client for the vLLM OpenAI-compatible chat-completions endpoint.

The function :func:`extract` is the single entry point used by the
benchmark (and, in later phases, by the FastAPI ``/extract`` route). It
turns a Kanban image into a validated :class:`KanbanExtraction`, plus
metadata (``prompt_version``, ``model``, latency, tokens) for the audit
trail.

Phase 0 deliberately keeps the client minimal:

- Synchronous :class:`httpx.Client` (concurrency lands in Phase 3).
- No ``response_format=json_object`` enforcement — the prompt itself
  already asks for JSON, and the briefing warns against grammar
  enforcement during reasoning ("Let Me Speak Freely?", EMNLP 2024).
- Retries with exponential backoff on 5xx / network errors only; 4xx
  errors are surfaced immediately because they indicate a contract bug,
  not a transient problem.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.config import Settings, get_settings
from app.observability.logger import get_logger
from app.pipeline.inference.schemas import KanbanExtraction
from app.pipeline.inference.schemas_strict import KANBAN_JSON_SCHEMA, grammar_version
from app.pipeline.inference.response_parser import parse_ocr_response
from app.pipeline.preprocessing import preprocess as preprocess_image
from app.pipeline.region_detection import split_kanban

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_PATH = PROMPTS_DIR / "extraction_v2.txt"
HEADER_PROMPT_PATH = PROMPTS_DIR / "header_v1.txt"
TABLE_PROMPT_PATH = PROMPTS_DIR / "table_v1.txt"
FOOTER_PROMPT_PATH = PROMPTS_DIR / "footer_v1.txt"

_log = get_logger(__name__)


class ExtractionError(Exception):
    """Base class for extraction failures the caller should surface as errors."""


class ExtractionParseError(ExtractionError):
    """Raised when the VLM response is not valid JSON we can recover."""

    def __init__(self, file: str, preview: str) -> None:
        super().__init__(
            f"failed to parse JSON from VLM response for {file!r}: {preview!r}"
        )
        self.file = file
        self.preview = preview


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one image extraction call.

    The metadata fields go straight into the audit log so the whole
    extraction can be reproduced (or compared across prompt / model
    versions).
    """

    extraction: KanbanExtraction
    prompt_version: str
    grammar_version: str
    model: str
    latency_ms: float
    completion_tokens: int


@lru_cache(maxsize=1)
def _load_prompt() -> tuple[str, str]:
    """Return ``(prompt text, sha256 hex digest)`` of the prompt file."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, digest


@lru_cache(maxsize=8)
def _load_prompt_at(path: Path) -> tuple[str, str]:
    """Return ``(prompt text, 12-char sha256)`` for the given prompt file.

    Used by the multi-crop pipeline (header/table/footer) where each
    region has its own focused prompt.
    """
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return text, digest


def _clean_json(raw: str) -> dict[str, Any]:
    """Recover a dict from JSON first, then Nanonets-style HTML tables."""
    return parse_ocr_response(raw)


def _section_payload(data: dict[str, Any], section: str) -> dict[str, Any]:
    nested = data.get(section)
    if isinstance(nested, dict):
        return nested
    return data


# Qwen2.5-VL uses 14x14 vision patches and the merger module groups them
# in 2x2 blocks, so both image dims must be multiples of 28. Ollama's
# llama.cpp-based runner asserts this hard (ggml.c:4081); vLLM's
# smart_resize handles it server-side via mm_processor_kwargs but we
# still resize on the client because (a) it works for both backends,
# (b) it caps the upload size on slow connections, (c) re-resize
# server-side is a no-op for already-aligned images.
_STRIDE = 28
_MAX_IMAGE_LONG_EDGE = 1288  # = 46 * _STRIDE; close to Qwen's 1280 hint


def _round_to_stride(value: int) -> int:
    """Round ``value`` to the nearest multiple of ``_STRIDE`` (min one stride)."""
    rounded = ((value + _STRIDE // 2) // _STRIDE) * _STRIDE
    return max(_STRIDE, rounded)


def _encode_image(image_path: Path, *, preprocess: bool = True) -> tuple[str, str]:
    """Return ``(base64 ascii data, mime type)`` for the image.

    1. Open + convert to RGB.
    2. Phase 0.5 preprocessing (deskew + CLAHE) — only when
       ``preprocess=True`` (default off; A/B regressed on Qwen2.5-VL).
    3. Down-sample to ``_MAX_IMAGE_LONG_EDGE`` on the long side, both
       dims rounded to multiples of 28 (Qwen2.5-VL merger constraint).
    4. Re-encode as JPEG quality 90.
    """
    with Image.open(image_path) as raw:
        rgb = raw.convert("RGB")
    if preprocess:
        rgb = preprocess_image(rgb)
    long_edge = max(rgb.size)
    scale = min(1.0, _MAX_IMAGE_LONG_EDGE / long_edge)
    target = (
        _round_to_stride(round(rgb.size[0] * scale)),
        _round_to_stride(round(rgb.size[1] * scale)),
    )
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return data, "image/jpeg"


def _encode_pil_image(rgb: Image.Image) -> tuple[str, str]:
    """Encode an already-decoded RGB image with the same stride-28
    resize logic as :func:`_encode_image`.

    Used by :func:`extract_with_crops` where the source image has
    already been split into 3 sub-images and we don't want to re-open
    from disk three times.
    """
    long_edge = max(rgb.size)
    scale = min(1.0, _MAX_IMAGE_LONG_EDGE / long_edge)
    target = (
        _round_to_stride(round(rgb.size[0] * scale)),
        _round_to_stride(round(rgb.size[1] * scale)),
    )
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def _build_payload(
    *,
    prompt: str,
    image_b64: str,
    mime: str,
    model: str,
    max_tokens: int = 8192,
    guided: bool = True,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Build the OpenAI-compatible chat-completions payload.

    Optional layers in `extra_body`:
    - ``guided=True``: vLLM-only. Pins llguidance + KANBAN_JSON_SCHEMA
      regex constraints. Strongest enforcement.
    - ``json_mode=True``: Ollama-native ``format="json"`` passthrough.
      Light syntax-only enforcement (valid JSON, no schema). Cheap.
      ``guided`` already implies syntactic validity, so set only when
      guided is off.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    extra: dict[str, Any] = {}
    if guided:
        # vLLM 0.20.x reads guided_decoding_backend / guided_json from
        # extra_body. The schema is *syntactic* enforcement only — the
        # Phase-4 lever the briefing references.
        extra["guided_json"] = KANBAN_JSON_SCHEMA
        extra["guided_decoding_backend"] = "llguidance"
    elif json_mode:
        # Ollama ignores `response_format` (an OpenAI concept) but
        # honours `format` from the native API; passing it through
        # `extra_body` makes the OpenAI-compat layer relay it.
        extra["format"] = "json"
    if extra:
        payload["extra_body"] = extra
    return payload


def _post_with_retries(
    url: str,
    payload: dict[str, Any],
    settings: Settings,
    log: Any,
) -> dict[str, Any]:
    """POST to ``url`` with exponential backoff on 5xx and network errors."""
    max_attempts = settings.vllm_max_retries + 1
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=settings.vllm_timeout_s) as client:
                response = client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt >= max_attempts:
                log.error("vllm.network_failed", attempt=attempt, error=type(exc).__name__)
                raise
            wait = 2 ** (attempt - 1)
            log.warning(
                "vllm.retry",
                attempt=attempt,
                wait_s=wait,
                error=type(exc).__name__,
            )
            time.sleep(wait)
            continue

        if 400 <= response.status_code < 500:
            log.error(
                "vllm.client_error",
                status=response.status_code,
                body_preview=response.text[:300],
            )
            response.raise_for_status()

        if response.status_code >= 500:
            if attempt >= max_attempts:
                log.error("vllm.server_error_giveup", status=response.status_code)
                response.raise_for_status()
            wait = 2 ** (attempt - 1)
            log.warning(
                "vllm.retry",
                attempt=attempt,
                wait_s=wait,
                status=response.status_code,
            )
            time.sleep(wait)
            continue

        return response.json()  # type: ignore[no-any-return]

    raise RuntimeError("unreachable: retry loop exited without returning")


def extract(image_path: Path, *, settings: Settings | None = None) -> ExtractionResult:
    """Extract a Kanban sheet from one image.

    Raises:
        ExtractionParseError: if the VLM response can't be coerced to JSON.
        httpx.HTTPError: if the vLLM endpoint is unreachable or rejects
            the request after retries.
        pydantic.ValidationError: if the parsed JSON doesn't match the
            extraction schema.
    """
    settings = settings or get_settings()
    prompt, prompt_version = _load_prompt()
    grammar_v = grammar_version() if settings.guided_decoding_enabled else "off"
    image_b64, mime = _encode_image(image_path, preprocess=settings.preprocess_enabled)
    payload = _build_payload(
        prompt=prompt,
        image_b64=image_b64,
        mime=mime,
        model=settings.vllm_model,
        guided=settings.guided_decoding_enabled,
        json_mode=settings.json_mode_enabled,
    )

    url = f"{str(settings.vllm_url).rstrip('/')}/chat/completions"

    log = _log.bind(
        file=image_path.name,
        model=settings.vllm_model,
        prompt_version=prompt_version[:8],
        grammar_version=grammar_v,
    )
    log.info("vllm.request")

    start = time.perf_counter()
    response = _post_with_retries(url, payload, settings, log)
    latency_ms = (time.perf_counter() - start) * 1000

    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("vllm.malformed_response", preview=str(response)[:300])
        raise ExtractionParseError(image_path.name, str(response)[:300]) from exc

    completion_tokens = int(response.get("usage", {}).get("completion_tokens", 0))

    try:
        parsed = _clean_json(content)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("vllm.parse_failed", error=str(exc), raw_preview=content[:300])
        raise ExtractionParseError(image_path.name, content[:300]) from exc

    extraction = KanbanExtraction.model_validate(parsed)
    log.info(
        "vllm.response",
        latency_ms=round(latency_ms, 1),
        completion_tokens=completion_tokens,
        rows=len(extraction.rows),
    )
    return ExtractionResult(
        extraction=extraction,
        prompt_version=prompt_version,
        grammar_version=grammar_v,
        model=settings.vllm_model,
        latency_ms=latency_ms,
        completion_tokens=completion_tokens,
    )


def _send_chat(
    *,
    image_b64: str,
    mime: str,
    prompt: str,
    settings: Settings,
    log: Any,
    file_label: str,
) -> tuple[dict[str, Any], float, int]:
    """Issue one chat-completions call, return ``(parsed_dict, latency_ms, tokens)``.

    Shared by :func:`extract` and :func:`extract_with_crops`. Raises
    :class:`ExtractionParseError` if the response can't be turned into
    a JSON dict.
    """
    payload = _build_payload(
        prompt=prompt,
        image_b64=image_b64,
        mime=mime,
        model=settings.vllm_model,
        guided=settings.guided_decoding_enabled,
        json_mode=settings.json_mode_enabled,
    )
    url = f"{str(settings.vllm_url).rstrip('/')}/chat/completions"
    start = time.perf_counter()
    response = _post_with_retries(url, payload, settings, log)
    latency_ms = (time.perf_counter() - start) * 1000
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("vllm.malformed_response", preview=str(response)[:300])
        raise ExtractionParseError(file_label, str(response)[:300]) from exc
    completion_tokens = int(response.get("usage", {}).get("completion_tokens", 0))
    try:
        parsed = _clean_json(content)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("vllm.parse_failed", error=str(exc), raw_preview=content[:300])
        raise ExtractionParseError(file_label, content[:300]) from exc
    return parsed, latency_ms, completion_tokens


def extract_with_crops(
    image_path: Path, *, settings: Settings | None = None
) -> ExtractionResult:
    """3-pass extraction: header / table / footer prompted independently.

    Each crop gets a focused prompt with only the columns it carries,
    so the model has less to hold in context per call. Trade-off: 3x
    the API calls (and roughly 3x the latency) for typically cleaner
    structure and fewer JSON glitches on long sheets.
    """
    settings = settings or get_settings()
    grammar_v = grammar_version() if settings.guided_decoding_enabled else "off"

    with Image.open(image_path) as raw:
        rgb = raw.convert("RGB")
    if settings.preprocess_enabled:
        rgb = preprocess_image(rgb)
    crops = split_kanban(rgb)

    header_prompt, h_v = _load_prompt_at(HEADER_PROMPT_PATH)
    table_prompt, t_v = _load_prompt_at(TABLE_PROMPT_PATH)
    footer_prompt, f_v = _load_prompt_at(FOOTER_PROMPT_PATH)
    combined_version = f"{h_v[:4]}-{t_v[:4]}-{f_v[:4]}"

    log = _log.bind(
        file=image_path.name,
        model=settings.vllm_model,
        prompt_version=combined_version,
        grammar_version=grammar_v,
        mode="crops",
        source_sha=crops.source_sha,
    )
    log.info("vllm.request")

    total_latency = 0.0
    total_tokens = 0

    log.info("vllm.crop", region="header")
    h_b64, mime = _encode_pil_image(crops.header)
    header_data, lat_h, tok_h = _send_chat(
        image_b64=h_b64,
        mime=mime,
        prompt=header_prompt,
        settings=settings,
        log=log,
        file_label=f"{image_path.name}:header",
    )
    total_latency += lat_h
    total_tokens += tok_h

    log.info("vllm.crop", region="table")
    t_b64, _ = _encode_pil_image(crops.table)
    table_data, lat_t, tok_t = _send_chat(
        image_b64=t_b64,
        mime=mime,
        prompt=table_prompt,
        settings=settings,
        log=log,
        file_label=f"{image_path.name}:table",
    )
    total_latency += lat_t
    total_tokens += tok_t

    log.info("vllm.crop", region="footer")
    f_b64, _ = _encode_pil_image(crops.footer)
    footer_data, lat_f, tok_f = _send_chat(
        image_b64=f_b64,
        mime=mime,
        prompt=footer_prompt,
        settings=settings,
        log=log,
        file_label=f"{image_path.name}:footer",
    )
    total_latency += lat_f
    total_tokens += tok_f

    header_payload = _section_payload(header_data, "header") if isinstance(header_data, dict) else {}
    table_rows = table_data.get("rows", []) if isinstance(table_data, dict) else []
    footer_payload = _section_payload(footer_data, "footer") if isinstance(footer_data, dict) else {}
    combined_payload = {
        "header": header_payload,
        "rows": table_rows if isinstance(table_rows, list) else [],
        "footer": footer_payload,
    }
    extraction = KanbanExtraction.model_validate(combined_payload)

    log.info(
        "vllm.response",
        latency_ms=round(total_latency, 1),
        completion_tokens=total_tokens,
        rows=len(extraction.rows),
    )
    return ExtractionResult(
        extraction=extraction,
        prompt_version=combined_version,
        grammar_version=grammar_v,
        model=settings.vllm_model,
        latency_ms=total_latency,
        completion_tokens=total_tokens,
    )
