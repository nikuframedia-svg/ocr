"""
Pipeline Kanban OCR — Metalogalva
  Qwen2.5-VL via Ollama faz OCR + extração estruturada numa só chamada.
  Output: CSV com 3 blocos (CABEÇALHO / TABELA / RODAPÉ) + JSON + métricas.

Uso:
  python ocr6.py imagem.jpg
  python ocr6.py img1.jpg img2.jpg img3.jpg
  python ocr6.py D:\\kanban\\
  python ocr6.py D:\\kanban\\ --json-only
  python ocr6.py --list-models
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

# R106 — OF normalisation (always 6 digits). Add backend/ to sys.path so the
# import works both when ocr6 is imported by the web app and run standalone.
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
from app.pipeline.scoring_engine import normalize_of  # noqa: E402

# ── Configuração ──────────────────────────────────────────────────────────────
# R64 — OLLAMA_URL is overridable via env var so the laptop runtime can
# point at a remote Ollama (e.g. the desktop PC on the same LAN).
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# MODEL is overridable via OCR_MODEL env var so we can benchmark different
# Ollama models without forking the script. Default = qwen3.5:9b (R224 — lê
# melhor que o qwen3-vl:8b nestes kanbans manuscritos; ver decisão do Luís).
# Production server adds OCR_NO_THINK=1 to disable Qwen3 reasoning blocks
# (older Qwen3 tests showed reasoning blocks hurt JSON stability/latency).
MODEL = os.environ.get("OCR_MODEL", "qwen3.5:9b")
TIMEOUT_SEC = 600
MAX_RETRIES = 2
NUM_PREDICT = 8192
TEMPERATURE = 0.0

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kanban-ocr")

# ── Prompt ────────────────────────────────────────────────────────────────────
# Default inline prompt — used when no --prompt FILE is given. Same as
# the original ocr6.py baseline. The v3 prompt (with examples + watch-out
# for 0/O/1/I/L confusions) lives in prompts/ocr6_v3.txt.
DEFAULT_PROMPT = """You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PRI | CLIENTE | OV | OF | MODELO | QTD | COMP_MM | LARG_MM | LOTE | CONI | ESP | LBASE | LTOPO
3. FOOTER fields: Colunas Produzidas, Horas Trabalhadas

IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- CONI can be a number (10, 12, 14) or text (T, OCT, TORRES).
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "BOBINE-FORMATO",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"pri":"","cliente":"","ov":"","of":"","modelo":"","qtd":"","comp_mm":"","larg_mm":"","lote":"","coni":"","esp":"","lbase":"","ltopo":""}
  ],
  "footer": {
    "colunas_produzidas": "",
    "horas_trabalhadas": ""
  }
}"""

# Mutable globals — set by main() once we've resolved --prompt FILE, so
# ollama_request() and ExtractionMetrics defaults pick up the right prompt.
PROMPT: str = DEFAULT_PROMPT
PROMPT_HASH: str = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


def load_prompt(path: Path | None) -> tuple[str, str]:
    """Return (prompt_text, sha256_first_12). Falls back to DEFAULT_PROMPT."""
    if path is None:
        return DEFAULT_PROMPT, hashlib.sha256(DEFAULT_PROMPT.encode()).hexdigest()[:12]
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


# ── Tipos ─────────────────────────────────────────────────────────────────────
@dataclass
class ExtractionMetrics:
    """Métricas por folha — alimentam comparação entre runs."""
    file: str
    status: str = "pending"           # ok | erro_modelo | erro_json | sem_dados
    duration_sec: float = 0.0
    rows_count: int = 0
    eval_count: int = 0               # tokens gerados (do Ollama)
    eval_duration_ms: int = 0
    prompt_hash: str = field(default_factory=lambda: PROMPT_HASH)
    model: str = MODEL
    error: str | None = None
    retries: int = 0


@dataclass
class ExtractionResult:
    """Output completo de uma folha."""
    file: str
    header: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)
    footer: dict[str, str] = field(default_factory=dict)
    metrics: ExtractionMetrics | None = None
    raw_response: str = ""


# ── Pydantic-style schema (validação leve, sem regex agressivo) ───────────────
ROW_FIELDS = ["pri", "cliente", "ov", "of", "modelo", "qtd", "comp_mm",
              "larg_mm", "lote", "coni", "esp", "lbase", "ltopo"]
HEADER_FIELDS = ("operador", "n_operador", "setor_maquina", "data")
FOOTER_FIELDS = ("colunas_produzidas", "horas_trabalhadas")


def normalize_extraction(
    data: dict[str, Any],
    *,
    row_fields: list[str] | tuple[str, ...] | None = None,
    header_fields: list[str] | tuple[str, ...] | None = None,
    footer_fields: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """
    Normaliza estrutura sem rejeitar dados. Validação estrita acontece
    a jusante na camada de validação contra ERP/Dossier.

    R84: templated schemas. Callers (template-aware OCR pipeline) pass
    the schema appropriate to the detected template. Defaults are the
    bobine_formato schema for backwards compat with legacy callers.
    """
    rf = list(row_fields) if row_fields else ROW_FIELDS
    hf = list(header_fields) if header_fields else list(HEADER_FIELDS)
    ff = list(footer_fields) if footer_fields else list(FOOTER_FIELDS)

    header = data.get("header", {}) or {}
    footer = data.get("footer", {}) or {}
    rows = data.get("rows", []) or []

    # Garantir campos do header
    for k in hf:
        header[k] = str(header.get(k, "")).strip()

    # Garantir campos do footer
    for k in ff:
        footer[k] = str(footer.get(k, "")).strip()

    # Normalizar rows: garantir que cada row tem todos os campos do schema
    clean_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean_row = {f: str(row.get(f, "")).strip() for f in rf}
        # R106 — OF is always 6 digits (zero-pad short numeric reads).
        if "of" in clean_row:
            clean_row["of"] = normalize_of(clean_row["of"])
        # Filtrar rows totalmente vazios
        if any(clean_row.values()):
            clean_rows.append(clean_row)

    return {"header": header, "rows": clean_rows, "footer": footer}


# ── Utilitários ───────────────────────────────────────────────────────────────
def fmt_dur(s: float) -> str:
    return f"{s:.1f}s" if s < 60 else f"{int(s)//60}m {int(s)%60:02d}s"


# Qwen2.5-VL uses 14x14 patches grouped 2x2 by the merger, so dims must
# be multiples of 28. Ollama's runner asserts this hard
# (ggml.c:4081: GGML_ASSERT(a->ne[2] * 4 == b->ne[0])); without resize
# any image whose dims aren't a multiple of 28 crashes the runner.
_STRIDE = 28
_MAX_IMAGE_LONG_EDGE = 1288  # 46 * _STRIDE; close to Qwen's 1280 hint


def _round_to_stride(value: int) -> int:
    rounded = ((value + _STRIDE // 2) // _STRIDE) * _STRIDE
    return max(_STRIDE, rounded)


def image_to_base64(path: Path, max_edge: int = _MAX_IMAGE_LONG_EDGE) -> str:
    """Resize to multiples of 28 (Qwen2.5-VL merger constraint) and
    encode as JPEG base64.

    ``max_edge`` lets process_image fall back to smaller dims when
    Ollama's GGML runner crashes on certain dim+prompt combos
    (assertion ne[2]*4==ne[0] fires inconsistently around 1288×924)."""
    with Image.open(path) as raw:
        rgb = raw.convert("RGB")
    long_edge = max(rgb.size)
    scale = min(1.0, max_edge / long_edge)
    target = (
        _round_to_stride(round(rgb.size[0] * scale)),
        _round_to_stride(round(rgb.size[1] * scale)),
    )
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _salvage_truncated_json(snippet: str) -> dict[str, Any] | None:
    """R224 — recupera um JSON cortado a meio (modelo bateu no teto de tokens a
    meio das linhas). Fecha os containers ainda abertos e tenta parsar; se
    falhar, recua até ao `}` anterior (descarta a linha incompleta) e repete.
    Devolve o JSON com o máximo de linhas completas, ou None."""
    best = snippet
    while best:
        stack: list[str] = []
        in_str = esc = False
        balanced = True
        for ch in best:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch in "{[":
                    stack.append("}" if ch == "{" else "]")
                elif ch in "}]":
                    if not stack:
                        balanced = False
                        break
                    stack.pop()
        if balanced and not in_str:
            candidate = best.rstrip().rstrip(",") + "".join(reversed(stack))
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        cut = best.rfind("}")
        if cut == -1:
            return None
        best = best[:cut]
    return None


def clean_json(raw: str) -> dict[str, Any]:
    """Remove <think>...</think>, markdown fences, e extrai o JSON."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # R224 — bloco <think> SEM fecho (o modelo divagou até ao teto sem fechar):
    # o regex acima não o apanha; salta o prefixo até ao primeiro '{'.
    if "<think>" in text and "{" in text:
        text = text[text.index("{"):]
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e <= s:
        raise ValueError("JSON delimiters not found in response")
    try:
        return json.loads(text[s:e])
    except json.JSONDecodeError:
        # R224 — JSON truncado: salvar as linhas completas em vez de perder a
        # folha toda (recupera "header"+"rows" lidas até ao corte).
        salvaged = _salvage_truncated_json(text[s:])
        if salvaged is not None:
            return salvaged
        raise


# ── Cliente Ollama ────────────────────────────────────────────────────────────
def ollama_request(image_b64: str) -> tuple[str | None, dict[str, Any]]:
    """
    Faz request ao Ollama. Devolve (response_text, telemetria).
    telemetria inclui eval_count, eval_duration, total_duration.
    """
    # Disable thinking for hybrid models (Qwen3 family). Without this, some
    # builds emit extra reasoning tokens that degrade JSON output and latency.
    # R224 — o `think:False` do Ollama nem sempre é honrado por builds custom
    # (ex.: qwen3.5:9b): o "thinking" enche tokens até ao teto (8192) → folhas
    # a demorar minutos e por vezes sem JSON. Reforçamos com a convenção Qwen
    # `/no_think` no próprio prompt (cinto-e-suspensórios).
    no_think = os.environ.get("OCR_NO_THINK", "").lower() in ("1", "true", "yes")
    prompt_text = f"{PROMPT}\n/no_think" if no_think else PROMPT
    payload = {
        "model": MODEL,
        "prompt": prompt_text,
        "images": [image_b64],
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT},
    }
    if no_think:
        payload["think"] = False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            body = json.loads(r.read().decode("utf-8"))
            telemetria = {
                "eval_count": body.get("eval_count", 0),
                "eval_duration_ms": body.get("eval_duration", 0) // 1_000_000,
                "total_duration_ms": body.get("total_duration", 0) // 1_000_000,
            }
            return body.get("response", "").strip(), telemetria
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code} from Ollama (model='{MODEL}')"
        if e.code == 404:
            msg += " — verifica com --list-models"
        log.error(msg)
        return None, {"error": msg}
    except urllib.error.URLError as e:
        log.error(f"Ollama unreachable at {OLLAMA_URL}: {e}")
        return None, {"error": str(e)}
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return None, {"error": str(e)}


# ── CSV ───────────────────────────────────────────────────────────────────────
def write_csv(output_csv: Path, results: list[ExtractionResult]) -> None:
    with output_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")

        w.writerow(["### CABEÇALHO"])
        w.writerow(["FICHEIRO", "DATA", "OPERADOR", "N_OPERADOR", "SETOR_MAQUINA"])
        for r in results:
            h = r.header
            w.writerow([r.file, h.get("data", ""), h.get("operador", ""),
                        h.get("n_operador", ""), h.get("setor_maquina", "")])
        w.writerow([])

        w.writerow(["### TABELA DE PRODUÇÃO"])
        w.writerow(["FICHEIRO", "DATA", "OPERADOR",
                    "PRI", "CLIENTE", "OV", "OF", "MODELO",
                    "QTD", "COMP_MM", "LARG_MM", "LOTE", "CONI", "ESP",
                    "LBASE", "LTOPO"])
        for r in results:
            h = r.header
            for row in r.rows:
                w.writerow([
                    r.file, h.get("data", ""), h.get("operador", ""),
                    row.get("pri", ""), row.get("cliente", ""),
                    row.get("ov", ""), row.get("of", ""),
                    row.get("modelo", ""), row.get("qtd", ""),
                    row.get("comp_mm", ""), row.get("larg_mm", ""),
                    row.get("lote", ""), row.get("coni", ""),
                    row.get("esp", ""), row.get("lbase", ""),
                    row.get("ltopo", ""),
                ])
        w.writerow([])

        w.writerow(["### RODAPÉ"])
        w.writerow(["FICHEIRO", "DATA", "OPERADOR",
                    "COLUNAS_PRODUZIDAS", "HORAS_TRABALHADAS"])
        for r in results:
            h, ft = r.header, r.footer
            w.writerow([r.file, h.get("data", ""), h.get("operador", ""),
                        ft.get("colunas_produzidas", ""),
                        ft.get("horas_trabalhadas", "")])


def write_json(output_json: Path, result: ExtractionResult) -> None:
    payload = {
        "file": result.file,
        "header": result.header,
        "rows": result.rows,
        "footer": result.footer,
        "metrics": asdict(result.metrics) if result.metrics else None,
    }
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")


# ── Pipeline por imagem ───────────────────────────────────────────────────────
def process_image(
    image_path: Path,
    idx: int,
    total: int,
    *,
    row_fields: list[str] | tuple[str, ...] | None = None,
    header_fields: list[str] | tuple[str, ...] | None = None,
    footer_fields: list[str] | tuple[str, ...] | None = None,
) -> ExtractionResult:
    name = image_path.name
    metrics = ExtractionMetrics(file=name)
    result = ExtractionResult(file=name, metrics=metrics)

    log.info(f"[{idx}/{total}] {name} — start")
    t0 = time.time()

    # Try descending image sizes — Ollama's Qwen2.5-VL runner crashes
    # (GGML assertion) on rare dim+prompt combos around 1288×924; a
    # smaller resize sidesteps it without hurting accuracy much.
    edges_to_try = [_MAX_IMAGE_LONG_EDGE, 1120, 1008]
    raw = None
    telemetria = {}
    for edge_idx, edge in enumerate(edges_to_try):
        image_b64 = image_to_base64(image_path, max_edge=edge)
        for attempt in range(MAX_RETRIES + 1):
            raw, telemetria = ollama_request(image_b64)
            if raw is not None:
                metrics.retries = attempt + edge_idx * (MAX_RETRIES + 1)
                break
            if attempt < MAX_RETRIES:
                log.warning(f"  retry {attempt + 1}/{MAX_RETRIES} (edge={edge})")
                time.sleep(2)
        if raw is not None:
            if edge_idx > 0:
                log.info(f"  ↻ recovered with smaller edge={edge}")
            break
        if edge_idx < len(edges_to_try) - 1:
            log.warning(f"  fallback to smaller edge ({edges_to_try[edge_idx + 1]})")
            time.sleep(2)

    metrics.duration_sec = time.time() - t0
    metrics.eval_count = telemetria.get("eval_count", 0)
    metrics.eval_duration_ms = telemetria.get("eval_duration_ms", 0)

    if raw is None:
        metrics.status = "erro_modelo"
        metrics.error = telemetria.get("error", "unknown")
        log.error(f"  ✗ erro_modelo ({fmt_dur(metrics.duration_sec)})")
        return result

    result.raw_response = raw

    # Round 59 — auto-retry on JSON parse failure. Vision LLMs
    # occasionally produce malformed JSON (missing comma, unescaped
    # quote) even at temperature=0 due to sampling state. The same
    # input retried often succeeds on attempt 2/3.
    extracted = None
    last_err: Exception | None = None
    for json_attempt in range(MAX_RETRIES + 1):
        try:
            extracted = clean_json(raw)
            break
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            if json_attempt < MAX_RETRIES:
                log.warning(
                    f"  JSON parse failed (attempt {json_attempt + 1}/"
                    f"{MAX_RETRIES + 1}): {e}; retrying OCR..."
                )
                time.sleep(1)
                raw, telemetria = ollama_request(image_b64)
                if raw is None:
                    log.error(f"  retry Ollama call returned None: {telemetria.get('error', '')}")
                    break
                result.raw_response = raw
                metrics.retries = json_attempt + 1
                # Refresh telemetry counters from latest call
                metrics.eval_count = int(telemetria.get("eval_count") or metrics.eval_count)
                metrics.eval_duration_ms = int(
                    telemetria.get("eval_duration_ms") or metrics.eval_duration_ms
                )

    if extracted is None:
        metrics.status = "erro_json"
        metrics.error = (
            f"{type(last_err).__name__}: {last_err}" if last_err else "unknown"
        )
        log.error(f"  ✗ erro_json após {MAX_RETRIES + 1} tentativas: {last_err}")
        log.debug(f"  raw[:300]: {raw[:300] if raw else '(no raw)'}")
        return result

    normalized = normalize_extraction(
        extracted,
        row_fields=row_fields,
        header_fields=header_fields,
        footer_fields=footer_fields,
    )
    result.header = normalized["header"]
    result.rows = normalized["rows"]
    result.footer = normalized["footer"]
    metrics.rows_count = len(result.rows)
    metrics.status = "ok" if result.rows else "sem_dados"

    tps = (metrics.eval_count / (metrics.eval_duration_ms / 1000)
           if metrics.eval_duration_ms > 0 else 0)
    log.info(f"  ✓ {metrics.rows_count} linhas | "
             f"{fmt_dur(metrics.duration_sec)} | "
             f"{metrics.eval_count} tok ({tps:.0f} tok/s)")
    return result


# ── Listar modelos ────────────────────────────────────────────────────────────
def list_models() -> None:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            models = json.loads(r.read().decode("utf-8")).get("models", [])
            print("=" * 64)
            for m in models:
                print(f"  {m.get('name')}")
            print("=" * 64)
    except Exception as e:
        log.error(f"Erro a listar modelos: {e}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def collect_image_paths(args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            paths.extend(sorted(q for q in p.iterdir()
                                if q.suffix.lower() in IMG_EXTS))
        elif p.exists() and p.suffix.lower() in IMG_EXTS:
            paths.append(p)
        else:
            log.warning(f"Ignorado (não existe ou não é imagem): {a}")
    return paths


def _compute_metrics_md(
    results: list[ExtractionResult],
    ground_truth_dir: Path,
    out_dir: Path,
) -> None:
    """Compare each result to ground_truth_dir/<stem>.json and write
    out_dir/_metrics.md. Reuses the project's metrics module so numbers
    are directly comparable to extract.py's output."""
    sys.path.insert(0, str(Path(__file__).parent / "backend"))
    try:
        from app.evaluation.metrics import compare_extractions, compute_metrics
        from app.pipeline.inference.schemas import KanbanExtraction
    except ImportError as e:
        log.warning(f"backend metrics not available, skipping ({e})")
        return

    comps: dict[str, list] = {}
    for r in results:
        if not r.metrics or r.metrics.status != "ok":
            continue
        stem = Path(r.file).stem
        gt_path = ground_truth_dir / f"{stem}.json"
        if not gt_path.exists():
            log.warning(f"no ground truth for {stem}, skipping")
            continue
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        actual = KanbanExtraction.model_validate(
            {"header": r.header, "rows": r.rows, "footer": r.footer}
        )
        expected = KanbanExtraction.model_validate(
            {
                "header": gt.get("header", {}),
                "rows": gt.get("rows", []),
                "footer": gt.get("footer", {}),
            }
        )
        comps[stem] = compare_extractions(stem, expected, actual)

    if not comps:
        return

    m = compute_metrics(comps)
    lines = [
        f"# Extraction metrics — {out_dir.name}",
        "",
        f"Generated: {datetime.now().isoformat()}",
        f"Prompt hash: {PROMPT_HASH}",
        f"Model: {MODEL}",
        "",
        "## Summary",
        "",
        "```",
        f"sheets evaluated:    {m.total_sheets}",
        f"field accuracy:      {m.field_accuracy * 100:.1f}%",
        f"perfect sheets:      {m.perfect_sheet_rate * 100:.1f}%",
        f"critical accuracy:   {m.critical_field_accuracy * 100:.1f}%",
        f"hallucination rate:  {m.hallucination_rate * 100:.1f}%",
        "```",
        "",
        "## Accuracy by field",
        "",
        "| field | accuracy | CER (lex) |",
        "|---|---|---|",
    ]
    for k in sorted(m.accuracy_by_field):
        cer = m.cer_by_field.get(k)
        cer_s = f"{cer:.3f}" if cer is not None else "—"
        lines.append(f"| `{k}` | {m.accuracy_by_field[k] * 100:.1f}% | {cer_s} |")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_metrics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"  → _metrics.md (field acc {m.field_accuracy * 100:.1f}%, "
             f"critical {m.critical_field_accuracy * 100:.1f}%)")


def main() -> int:
    global PROMPT, PROMPT_HASH

    parser = argparse.ArgumentParser(description="Kanban OCR — Metalogalva")
    parser.add_argument("paths", nargs="*",
                        help="Imagem(s) ou pasta com imagens")
    parser.add_argument("--list-models", action="store_true",
                        help="Listar modelos disponíveis no Ollama")
    parser.add_argument("--json-only", action="store_true",
                        help="Não mover ficheiros nem escrever CSV consolidado")
    parser.add_argument("--no-move", action="store_true",
                        help="Não mover imagens para processados/")
    parser.add_argument("--prompt", type=Path, default=None, metavar="FILE",
                        help="Carregar prompt de FILE (default: prompt inline)")
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="Pasta de output (default: junto às imagens)")
    parser.add_argument("--ground-truth", type=Path, default=None, metavar="DIR",
                        help="Após o batch, comparar contra DIR/<stem>.json e "
                             "escrever <out>/_metrics.md")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Logging DEBUG")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.list_models:
        list_models()
        return 0

    if not args.paths:
        parser.print_help()
        return 1

    if args.prompt is not None:
        PROMPT, PROMPT_HASH = load_prompt(args.prompt)

    image_paths = collect_image_paths(args.paths)
    if not image_paths:
        log.error("Nenhuma imagem encontrada.")
        return 1

    out_dir = args.out if args.out is not None else image_paths[0].parent
    out_dir.mkdir(parents=True, exist_ok=True)

    base_dir = image_paths[0].parent
    processed_dir = base_dir / "processados"
    if not args.no_move and not args.json_only:
        processed_dir.mkdir(exist_ok=True)

    print("=" * 64)
    print(f"  KANBAN OCR — Metalogalva")
    print(f"  Modelo       : {MODEL}")
    print(f"  Prompt       : {args.prompt or '(inline default)'} ({PROMPT_HASH})")
    print(f"  Imagens      : {len(image_paths)}")
    print(f"  Output dir   : {out_dir}")
    print("=" * 64)

    results: list[ExtractionResult] = []
    t_global = time.time()

    for i, img in enumerate(image_paths, 1):
        result = process_image(img, i, len(image_paths))
        results.append(result)

        if result.metrics and result.metrics.status in ("ok", "sem_dados"):
            csv_path = out_dir / f"{img.stem}.csv"
            json_path = out_dir / f"{img.stem}.json"
            write_csv(csv_path, [result])
            write_json(json_path, result)

            if not args.no_move and not args.json_only:
                dest = processed_dir / img.name
                img.rename(dest)
                log.info(f"  → {csv_path.name} + {json_path.name} | "
                         f"movido p/ processados/")
            else:
                log.info(f"  → {csv_path.name} + {json_path.name}")

    total_dur = time.time() - t_global

    # ── Relatório ─────────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("  RELATÓRIO")
    print("=" * 64)
    print(f"  {'Ficheiro':<40} {'Linhas':>6} {'Duração':>8}  Status")
    print("-" * 64)
    for r in results:
        m = r.metrics
        if not m:
            continue
        icon = "✓" if m.status == "ok" else "✗"
        print(f"  {m.file:<40} {m.rows_count:>6} "
              f"{fmt_dur(m.duration_sec):>8}  {icon} {m.status}")

    ok = sum(1 for r in results if r.metrics and r.metrics.status == "ok")
    total_rows = sum(r.metrics.rows_count for r in results if r.metrics)
    avg_dur = (total_dur / len(results)) if results else 0

    print("-" * 64)
    print(f"  Imagens : {len(results)}  ({ok} OK, {len(results) - ok} erro(s))")
    print(f"  Linhas  : {total_rows}")
    print(f"  Duração : {fmt_dur(total_dur)}  (média {fmt_dur(avg_dur)}/img)")
    print("=" * 64)

    # Métricas agregadas para análise de regressão
    metrics_path = out_dir / f"_metrics_{datetime.now():%Y%m%d_%H%M%S}.json"
    metrics_path.write_text(
        json.dumps({
            "model": MODEL,
            "prompt_hash": PROMPT_HASH,
            "total_duration_sec": total_dur,
            "total_images": len(results),
            "ok": ok,
            "total_rows": total_rows,
            "per_image": [asdict(r.metrics) for r in results if r.metrics],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"Métricas: {metrics_path.name}")

    if args.ground_truth is not None:
        _compute_metrics_md(results, args.ground_truth, out_dir)

    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
