"""ocr6.py crops variant — split each Kanban into header/table/footer crops
and run 3 focused inferences per sheet. Output schema same as ocr6.py.

Usage:
    python ocr6_crops.py inputs/originais/ --out reports/extractions_v10/
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import logging
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

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5vl:7b"
TIMEOUT_SEC = 600
MAX_RETRIES = 2
NUM_PREDICT = 8192
TEMPERATURE = 0.0
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}

# Crop boundaries (fractions of total height)
HEADER_END = 0.22
TABLE_START = 0.18
TABLE_END = 0.87
FOOTER_START = 0.83

# Stride-28 image resize (Qwen2.5-VL merger)
STRIDE = 28
EDGES_TO_TRY = [1288, 1120, 1008]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ocr6-crops")

# Load region prompts
PROMPT_DIR = Path(__file__).parent / "backend" / "app" / "pipeline" / "inference" / "prompts"
PROMPT_HEADER = (PROMPT_DIR / "header_v1.txt").read_text(encoding="utf-8")
PROMPT_TABLE = (PROMPT_DIR / "table_v1.txt").read_text(encoding="utf-8")
PROMPT_FOOTER = (PROMPT_DIR / "footer_v1.txt").read_text(encoding="utf-8")
PROMPTS = {"header": PROMPT_HEADER, "table": PROMPT_TABLE, "footer": PROMPT_FOOTER}
PROMPT_HASH = hashlib.sha256(
    (PROMPT_HEADER + PROMPT_TABLE + PROMPT_FOOTER).encode()
).hexdigest()[:12]

ROW_FIELDS = ["pri", "cliente", "ov", "of", "modelo", "qtd", "comp_mm",
              "larg_mm", "lote", "coni", "esp", "lbase", "ltopo"]


@dataclass
class ExtractionMetrics:
    file: str
    status: str = "pending"
    duration_sec: float = 0.0
    rows_count: int = 0
    eval_count: int = 0
    eval_duration_ms: int = 0
    prompt_hash: str = field(default_factory=lambda: PROMPT_HASH)
    model: str = MODEL
    error: str | None = None
    retries: int = 0


@dataclass
class ExtractionResult:
    file: str
    header: dict[str, str] = field(default_factory=dict)
    rows: list[dict[str, str]] = field(default_factory=list)
    footer: dict[str, str] = field(default_factory=dict)
    metrics: ExtractionMetrics | None = None


def _round_to_stride(value: int) -> int:
    return max(STRIDE, ((value + STRIDE // 2) // STRIDE) * STRIDE)


def encode_image(rgb: Image.Image, max_edge: int) -> str:
    long_edge = max(rgb.size)
    scale = min(1.0, max_edge / long_edge)
    target = (
        _round_to_stride(round(rgb.size[0] * scale)),
        _round_to_stride(round(rgb.size[1] * scale)),
    )
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    buf = BytesIO()
    rgb.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def split_kanban(path: Path) -> tuple[Image.Image, Image.Image, Image.Image]:
    with Image.open(path) as raw:
        img = raw.convert("RGB")
    w, h = img.size
    header = img.crop((0, 0, w, int(h * HEADER_END)))
    table = img.crop((0, int(h * TABLE_START), w, int(h * TABLE_END)))
    footer = img.crop((0, int(h * FOOTER_START), w, h))
    return header, table, footer


def ollama_request(prompt: str, image_b64: str) -> tuple[str | None, dict]:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT},
    }
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
            return body.get("response", "").strip(), {
                "eval_count": body.get("eval_count", 0),
                "eval_duration_ms": body.get("eval_duration", 0) // 1_000_000,
            }
    except urllib.error.HTTPError as e:
        return None, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return None, {"error": str(e)}


def clean_json(raw: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"): part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    s, e = text.find("{"), text.rfind("}") + 1
    if s == -1 or e <= s:
        raise ValueError("JSON delimiters not found")
    return json.loads(text[s:e])


def extract_region(rgb: Image.Image, prompt: str, region: str) -> tuple[dict | None, dict]:
    """Try descending image edges; return parsed dict or None."""
    last_telem = {}
    for edge in EDGES_TO_TRY:
        b64 = encode_image(rgb, edge)
        for attempt in range(MAX_RETRIES + 1):
            raw, telem = ollama_request(prompt, b64)
            last_telem = telem
            if raw is not None:
                try:
                    return clean_json(raw), telem
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"  {region}: parse error ({type(e).__name__}); raw[:150]={raw[:150]!r}")
                    continue  # retry
            if attempt < MAX_RETRIES:
                time.sleep(2)
        log.warning(f"  {region}: edge={edge} failed; falling back smaller")
    return None, last_telem


def normalize_extraction(header: dict, rows_obj: dict, footer: dict) -> tuple[dict, list[dict], dict]:
    h = header or {}
    f = footer or {}
    for k in ("operador", "n_operador", "setor_maquina", "data"):
        h[k] = str(h.get(k, "")).strip()
    for k in ("colunas_produzidas", "horas_trabalhadas"):
        f[k] = str(f.get(k, "")).strip()
    rows_list = (rows_obj or {}).get("rows", []) or []
    clean_rows = []
    for row in rows_list:
        if not isinstance(row, dict): continue
        cr = {fld: str(row.get(fld, "")).strip() for fld in ROW_FIELDS}
        if any(cr.values()): clean_rows.append(cr)
    return h, clean_rows, f


def write_csv(out_csv: Path, r: ExtractionResult) -> None:
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["### CABEÇALHO"])
        w.writerow(["FICHEIRO","DATA","OPERADOR","N_OPERADOR","SETOR_MAQUINA"])
        h = r.header
        w.writerow([r.file, h.get("data",""), h.get("operador",""),
                    h.get("n_operador",""), h.get("setor_maquina","")])
        w.writerow([])
        w.writerow(["### TABELA DE PRODUÇÃO"])
        w.writerow(["FICHEIRO","DATA","OPERADOR","PRI","CLIENTE","OV","OF","MODELO",
                    "QTD","COMP_MM","LARG_MM","LOTE","CONI","ESP","LBASE","LTOPO"])
        for row in r.rows:
            w.writerow([r.file, h.get("data",""), h.get("operador",""),
                        row.get("pri",""), row.get("cliente",""),
                        row.get("ov",""), row.get("of",""),
                        row.get("modelo",""), row.get("qtd",""),
                        row.get("comp_mm",""), row.get("larg_mm",""),
                        row.get("lote",""), row.get("coni",""),
                        row.get("esp",""), row.get("lbase",""),
                        row.get("ltopo","")])
        w.writerow([])
        w.writerow(["### RODAPÉ"])
        w.writerow(["FICHEIRO","DATA","OPERADOR","COLUNAS_PRODUZIDAS","HORAS_TRABALHADAS"])
        ft = r.footer
        w.writerow([r.file, h.get("data",""), h.get("operador",""),
                    ft.get("colunas_produzidas",""), ft.get("horas_trabalhadas","")])


def write_json(out_json: Path, r: ExtractionResult) -> None:
    out_json.write_text(json.dumps({
        "file": r.file, "header": r.header, "rows": r.rows, "footer": r.footer,
        "metrics": asdict(r.metrics) if r.metrics else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def process_image(image_path: Path, idx: int, total: int) -> ExtractionResult:
    name = image_path.name
    metrics = ExtractionMetrics(file=name)
    result = ExtractionResult(file=name, metrics=metrics)

    log.info(f"[{idx}/{total}] {name} — split + 3 inferences")
    t0 = time.time()

    header_img, table_img, footer_img = split_kanban(image_path)

    h_data, h_telem = extract_region(header_img, PROMPTS["header"], "header")
    t_data, t_telem = extract_region(table_img, PROMPTS["table"], "table")
    f_data, f_telem = extract_region(footer_img, PROMPTS["footer"], "footer")

    metrics.duration_sec = time.time() - t0
    metrics.eval_count = (h_telem.get("eval_count", 0) +
                          t_telem.get("eval_count", 0) +
                          f_telem.get("eval_count", 0))

    if t_data is None:  # table is critical
        metrics.status = "erro_modelo"
        metrics.error = t_telem.get("error", "table extraction failed")
        log.error(f"  ✗ erro_modelo (table) ({metrics.duration_sec:.1f}s)")
        return result

    h, rows, f = normalize_extraction(h_data or {}, t_data, f_data or {})
    result.header = h
    result.rows = rows
    result.footer = f
    metrics.rows_count = len(rows)
    metrics.status = "ok" if rows else "sem_dados"

    log.info(f"  ✓ {len(rows)} rows | {metrics.duration_sec:.1f}s | "
             f"{metrics.eval_count} tok")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Kanban OCR (crops mode)")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--out", type=Path, default=Path("reports/extractions_v10"))
    parser.add_argument("--no-move", action="store_true", default=True)
    args = parser.parse_args()

    if not args.paths:
        parser.print_help()
        return 1

    paths = []
    for a in args.paths:
        p = Path(a)
        if p.is_dir():
            paths.extend(sorted(q for q in p.iterdir() if q.suffix.lower() in IMG_EXTS))
        elif p.exists() and p.suffix.lower() in IMG_EXTS:
            paths.append(p)

    if not paths:
        log.error("No images found")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print(f"  KANBAN OCR — Metalogalva (CROPS)")
    print(f"  Model:       {MODEL}")
    print(f"  Prompt hash: {PROMPT_HASH}")
    print(f"  Images:      {len(paths)}")
    print(f"  Output dir:  {args.out}")
    print("=" * 64)

    results = []
    t_global = time.time()
    for i, p in enumerate(paths, 1):
        r = process_image(p, i, len(paths))
        results.append(r)
        if r.metrics and r.metrics.status in ("ok", "sem_dados"):
            write_csv(args.out / f"{p.stem}.csv", r)
            write_json(args.out / f"{p.stem}.json", r)

    print()
    ok = sum(1 for r in results if r.metrics and r.metrics.status == "ok")
    total_rows = sum(r.metrics.rows_count for r in results if r.metrics)
    dur = time.time() - t_global
    print(f"{ok}/{len(results)} OK, {total_rows} rows total, {dur:.1f}s "
          f"(avg {dur/len(results):.1f}s/sheet)")

    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
