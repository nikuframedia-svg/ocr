"""R236 — Golden set de verdade humana a partir do app.db da fábrica.

Minera a tabela ``edits`` (``source='human'`` e ``old_value != new_value``):
cada linha é um DESACORDO real de um humano com o que estava no ecrã (OCR ou
auto-substituição do cross) — verdade não-circular, ao contrário do
``resultado_atual`` (que é em grande parte o output do próprio motor).

Uso:
    uv run python scripts/diag/build_golden_set.py \
        --db ~/Downloads/auditoria_humana/app.db \
        --out reports/golden_set/cells.csv [--all-fields]

Output (CSV): sheet_id, row_index, field, field_path, ocr_raw (raw_extraction),
old_value (o que estava no ecrã), human_truth (new_value), edited_at,
sheet_status, template_name. A última edição humana por (sheet, field_path)
ganha (espelha ``_human_edited_paths`` em web/main.py).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from pathlib import Path

CROSSABLE = {
    "of", "ov", "cliente", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo",
}
_ROW_RE = re.compile(r"^rows\[(\d+)\]\.(\w+)$")


def build(db_path: Path, out_path: Path, crossable_only: bool = True) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # Última edição humana por (sheet, field_path) — ordem por id crescente,
    # o dict fica com a mais recente.
    latest: dict[tuple[int, str], sqlite3.Row] = {}
    for r in con.execute(
        "SELECT id, sheet_id, field_path, old_value, new_value, edited_at "
        "FROM edits WHERE source='human' AND old_value <> new_value ORDER BY id"
    ):
        latest[(r["sheet_id"], r["field_path"])] = r

    sheets: dict[int, dict] = {}

    def sheet_ctx(sheet_id: int) -> dict:
        if sheet_id not in sheets:
            row = con.execute(
                "SELECT status, raw_extraction, sheet_data FROM sheets WHERE id=?",
                (sheet_id,),
            ).fetchone()
            raw = {}
            tpl = ""
            status = ""
            if row:
                status = row["status"] or ""
                try:
                    raw = json.loads(row["raw_extraction"] or "{}") or {}
                except (TypeError, ValueError):
                    raw = {}
                try:
                    fin = json.loads(row["sheet_data"] or "{}") or {}
                except (TypeError, ValueError):
                    fin = {}
                tpl = fin.get("template_name") or raw.get("template_name") or ""
            sheets[sheet_id] = {"raw": raw, "tpl": tpl, "status": status}
        return sheets[sheet_id]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_out = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "sheet_id", "row_index", "field", "field_path", "ocr_raw",
            "old_value", "human_truth", "edited_at", "sheet_status",
            "template_name",
        ])
        for (sheet_id, field_path), r in sorted(latest.items()):
            m = _ROW_RE.match(field_path)
            row_index: int | None = None
            field = field_path
            if m:
                row_index, field = int(m.group(1)), m.group(2)
            if crossable_only and field not in CROSSABLE:
                continue
            ctx = sheet_ctx(sheet_id)
            ocr_raw = ""
            if row_index is not None:
                rows = ctx["raw"].get("rows") or []
                if row_index < len(rows):
                    ocr_raw = str((rows[row_index] or {}).get(field) or "")
            else:
                section, _, fname = field_path.partition(".")
                ocr_raw = str((ctx["raw"].get(section) or {}).get(fname) or "")
            w.writerow([
                sheet_id, "" if row_index is None else row_index, field,
                field_path, ocr_raw, r["old_value"], r["new_value"],
                r["edited_at"], ctx["status"], ctx["tpl"],
            ])
            n_out += 1
    return {"cells": n_out, "out": str(out_path)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", type=Path,
                    default=Path("reports/golden_set/cells.csv"))
    ap.add_argument("--all-fields", action="store_true",
                    help="incluir campos não-cruzáveis (qtd, cesta_n, ...)")
    args = ap.parse_args()
    info = build(args.db, args.out, crossable_only=not args.all_fields)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
