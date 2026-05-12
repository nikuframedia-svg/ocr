"""Wrapper that runs the v9 OCR pipeline + DQ Module on one image.

Imports ``ocr6`` (workspace-root script) and ``app.dq.pipeline`` and
glues them. Sets the prompt to ``prompts/ocr6_v3.txt`` (canonical) on
import.

Round 54 — multi-template aware. The flow is:

1. Pass-1: run OCR with the BOBINE-FORMATO v3 prompt (canonical, proven
   87.9% field accuracy).
2. Detect template from ``header.setor_maquina`` via templates_registry.
3. If detected template != bobine_formato, run Pass-2 with a template-
   specific prompt built from prompt_builder.build_prompt(template).
   The pass-2 result replaces pass-1's rows/footer; header stays from
   whichever pass had a higher-quality header (pass-2 if non-empty).
4. Persist ``template_name`` in the returned dict so downstream
   (DQ, cross-check, UI, CSV writer) can be template-aware.

Public API:
    run_pipeline(image_path: Path) -> dict with keys:
        raw            — direct ocr6 output (header/rows/footer) used downstream
        dq             — DQ audit (cells/violations/score/stp_eligible/summary)
        current        — same as raw initially; lives for editing
        template_name  — canonical template id (e.g. "bobine_formato", "laser")
    rerun_pipeline_for_template(image_path, template_name) -> dict
        Same return shape; forces a specific template (used when the
        operator manually edits the setor field post-OCR).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]

# ocr6.py is at workspace root
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import ocr6  # type: ignore  # noqa: E402

from app.dq.cross_sheet_index import CrossSheetIndex  # noqa: E402
from app.dq.pipeline import run_dq  # noqa: E402
from app.pipeline.prompt_builder import build_prompt  # noqa: E402
from app.templates_registry import (  # noqa: E402
    DEFAULT_TEMPLATE,
    detect_template,
    get_template,
)

# Pin canonical prompt v3 (same as production runs of v9) — used as the
# BOBINE-FORMATO baseline. R54 introduces per-template prompts but the
# default at import remains v3 so legacy behavior is preserved.
_PROMPT_PATH = _REPO / "prompts" / "ocr6_v3.txt"
_V3_PROMPT, _V3_PROMPT_HASH = ocr6.load_prompt(_PROMPT_PATH)
ocr6.PROMPT, ocr6.PROMPT_HASH = _V3_PROMPT, _V3_PROMPT_HASH

# Load learned artefacts once at module import
_LEARNED_PATH = _REPO / "lexicons" / "learned.json"
_LEARNED: dict[str, Any] = (
    json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
    if _LEARNED_PATH.exists()
    else {}
)

# Persistent cross-sheet index
_INDEX_PATH = _REPO / "data" / "cross_sheet.json"


def _swap_prompt(prompt_text: str) -> tuple[str, str]:
    """Swap ocr6.PROMPT atomically. Returns the previous (prompt, hash)."""
    import hashlib
    prev = (ocr6.PROMPT, ocr6.PROMPT_HASH)
    ocr6.PROMPT = prompt_text
    ocr6.PROMPT_HASH = hashlib.sha256(prompt_text.encode()).hexdigest()[:12]
    return prev


def _run_ocr(image_path: Path) -> tuple[dict, dict | None]:
    """Run ocr6.process_image with the currently-installed prompt.

    Returns (raw_extraction_dict, metrics_dict). Raises on hard failure.
    """
    result = ocr6.process_image(image_path, idx=1, total=1)
    if not result.metrics or result.metrics.status != "ok":
        err = result.metrics.error if result.metrics else "unknown"
        raise RuntimeError(f"OCR failed: {err}")
    raw = {
        "header": result.header,
        "rows": result.rows,
        "footer": result.footer,
    }
    metrics = (
        {
            "status": result.metrics.status,
            "total_duration_ms": getattr(result.metrics, "total_duration_ms", None),
            "eval_count": getattr(result.metrics, "eval_count", None),
        }
        if result.metrics
        else None
    )
    return raw, metrics


def _merge_pass2_into_pass1(pass1: dict, pass2: dict) -> dict:
    """Prefer pass-2 rows/footer; for header use pass-2 unless empty.

    Pass-2 has the right schema for non-bobine templates, so its rows
    must replace pass-1. The header is identical in both passes (same
    image, same header area) but we trust pass-2 because it was
    informed about the template's canonical setor name.
    """
    h1 = pass1.get("header", {}) or {}
    h2 = pass2.get("header", {}) or {}

    # For each header field: prefer pass-2 if non-empty, else pass-1
    merged_header = {}
    for k in ("operador", "n_operador", "setor_maquina", "data"):
        v2 = (h2.get(k) or "").strip()
        v1 = (h1.get(k) or "").strip()
        merged_header[k] = v2 or v1

    return {
        "header": merged_header,
        "rows": pass2.get("rows", []) or [],
        "footer": pass2.get("footer", {}) or {},
    }


def run_pipeline(image_path: Path) -> dict:
    """Run OCR + DQ end-to-end on a single image. Multi-template aware.

    Strategy:
      1. Pass-1 with v3 (bobine) prompt — always runs. Gives baseline
         header (notably ``setor_maquina``) and a default extraction.
      2. detect_template(setor_maquina) — pick the right TemplateSpec.
      3. If template == bobine_formato → skip pass-2, use pass-1 as-is.
      4. Else → swap prompt, re-run OCR, merge results, restore prompt.

    Returns dict with: raw, dq, current, template_name.
    Raises on hard OCR failure.
    """
    # Pass 1 — v3 / bobine baseline
    pass1_raw, _ = _run_ocr(image_path)

    # Detect template from pass-1 header
    setor = (pass1_raw.get("header", {}) or {}).get("setor_maquina", "")
    template = detect_template(setor)

    if template.name == DEFAULT_TEMPLATE.name:
        # Bobine path — pass-1 result is canonical, no re-run needed
        raw_extraction = pass1_raw
    else:
        # Non-bobine — run pass 2 with template-specific prompt
        prev = _swap_prompt(build_prompt(template))
        try:
            pass2_raw, _ = _run_ocr(image_path)
        finally:
            # Restore v3 baseline so subsequent uploads start fresh
            ocr6.PROMPT, ocr6.PROMPT_HASH = prev

        raw_extraction = _merge_pass2_into_pass1(pass1_raw, pass2_raw)

    # Persist template_name inside raw so DQ + UI know which schema to use
    raw_extraction["template_name"] = template.name

    # Lazy-load + persist cross-sheet index (unchanged from R53)
    index = CrossSheetIndex.load(_INDEX_PATH)
    if not index.ov_to_cliente and _LEARNED_PATH.exists():
        index.seed_from_learned(_LEARNED_PATH)

    # DQ pass — for now still uses the same engine. Phase 3 will make
    # it template-aware (skip fields not in template.row_fields).
    dq_result, fixed = run_dq(
        extraction=raw_extraction,
        sheet_name=image_path.stem,
        learned=_LEARNED,
        index=index,
    )
    index.save(_INDEX_PATH)

    # Carry template_name into fixed too (run_dq returns just the rows
    # dict, doesn't know about template_name)
    if isinstance(fixed, dict):
        fixed["template_name"] = template.name

    return {
        "raw": raw_extraction,
        "dq": dq_result.to_dict(),
        "current": fixed,
        "template_name": template.name,
    }


def rerun_pipeline_for_template(
    image_path: Path,
    template_name: str,
) -> dict:
    """Re-run OCR with a forced template (used when operator edits setor).

    Bypasses the auto-detection step. Useful when:
      - Pass-1 detection guessed wrong (e.g. setor field is illegible)
      - Operator manually corrected the setor in the UI and we want to
        re-extract with the right schema instead of just re-rendering.

    Returns the same dict shape as run_pipeline.
    """
    template = get_template(template_name)

    if template.name == DEFAULT_TEMPLATE.name:
        # Bobine forced — use v3 directly
        raw_extraction, _ = _run_ocr(image_path)
    else:
        prev = _swap_prompt(build_prompt(template))
        try:
            raw_extraction, _ = _run_ocr(image_path)
        finally:
            ocr6.PROMPT, ocr6.PROMPT_HASH = prev

    raw_extraction["template_name"] = template.name

    index = CrossSheetIndex.load(_INDEX_PATH)
    if not index.ov_to_cliente and _LEARNED_PATH.exists():
        index.seed_from_learned(_LEARNED_PATH)

    dq_result, fixed = run_dq(
        extraction=raw_extraction,
        sheet_name=image_path.stem,
        learned=_LEARNED,
        index=index,
    )
    index.save(_INDEX_PATH)

    if isinstance(fixed, dict):
        fixed["template_name"] = template.name

    return {
        "raw": raw_extraction,
        "dq": dq_result.to_dict(),
        "current": fixed,
        "template_name": template.name,
    }


__all__ = ["run_pipeline", "rerun_pipeline_for_template"]
