"""Build a side-by-side CSV: raw OCR vs DQ-snap vs cross-check status.

Per cell of a sheet, output:
    ROW | CAMPO | OCR_RAW | APOS_DQ_SNAP | STATUS | PLAN_VALOR | RAZAO

Usage:
    .venv\\Scripts\\python.exe scripts\\compare_ocr_vs_crosscheck.py <sheet_id> [out.csv]

If out.csv omitted, writes to ~/Downloads/sheet<id>_ocr_vs_crosscheck.csv
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8')

from app.web import db  # noqa: E402
from app.cross_check import storage  # noqa: E402

HEADER_FIELDS = ("operador", "n_operador", "setor_maquina", "data")
ROW_FIELDS = (
    "pri", "cliente", "ov", "of", "modelo", "qtd",
    "comp_mm", "larg_mm", "lote", "coni", "esp", "lbase", "ltopo",
)
FOOTER_FIELDS = ("colunas_produzidas", "horas_trabalhadas")


def _get_field(d: dict, path_parts: list[str]):
    """Walk path like ['rows', 0, 'modelo'] safely."""
    cur = d
    for p in path_parts:
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                return None
            cur = cur[p]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
    return cur


def main():
    if len(sys.argv) < 2:
        print("Usage: compare_ocr_vs_crosscheck.py <sheet_id> [out.csv]")
        sys.exit(1)

    sheet_id = int(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        Path.home() / "Downloads" / f"sheet{sheet_id}_ocr_vs_crosscheck.csv"
    )

    sheet = db.get_sheet(sheet_id)
    if sheet is None:
        print(f"Sheet {sheet_id} not found")
        sys.exit(1)

    raw = sheet.get("raw_extraction") or {}
    final = sheet.get("sheet_data") or {}
    audit = (sheet.get("dq_audit") or {}).get("cells", {})
    cross_check = storage.load_sheet_cross_check(sheet_id) or {}
    cross_rows = {r["row_index"]: r for r in cross_check.get("rows", [])}
    # Round 33: header/footer come from engine top-level keys
    cc_header = cross_check.get("header", {}) or {}
    cc_footer = cross_check.get("footer", {}) or {}

    rows_out = []

    def _record(scope: str, field: str, ocr_val, snap_val, status: str,
                plan_val: str = "", reason: str = "", fix_rule: str = ""):
        rows_out.append({
            "ROW": scope,
            "CAMPO": field.upper(),
            "OCR_RAW": ocr_val if ocr_val is not None else "",
            "APOS_DQ_SNAP": snap_val if snap_val is not None else "",
            "STATUS": status,
            "PLAN_VALOR": plan_val,
            "RAZAO": reason,
            "REGRA_DQ": fix_rule,
        })

    # --- Header (Round 33: from engine cc_header → status NA, no plan check) ---
    for f in HEADER_FIELDS:
        ocr_v = _get_field(raw, ["header", f])
        snap_v = _get_field(final, ["header", f])
        path = f"header.{f}"
        a = audit.get(path, {})
        fix_chain = a.get("fix_chain", [])
        fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
        cc_info = cc_header.get(f, {})
        status = cc_info.get("status", "NA")
        _record(
            scope="HEADER",
            field=f,
            ocr_val=ocr_v,
            snap_val=snap_v,
            status=status,
            fix_rule=fix_rule,
        )

    # --- Rows ---
    raw_rows = raw.get("rows", []) or []
    final_rows = final.get("rows", []) or []
    n_rows = max(len(raw_rows), len(final_rows))
    for i in range(n_rows):
        ocr_row = raw_rows[i] if i < len(raw_rows) else {}
        snap_row = final_rows[i] if i < len(final_rows) else {}
        cross_row = cross_rows.get(i, {})
        cross_fields = cross_row.get("fields", {})

        for f in ROW_FIELDS:
            ocr_v = ocr_row.get(f, "")
            snap_v = snap_row.get(f, "")
            path = f"rows[{i}].{f}"
            a = audit.get(path, {})
            fix_chain = a.get("fix_chain", [])
            fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
            cinfo = cross_fields.get(f, {})
            status = cinfo.get("status", "?")
            plan_v = cinfo.get("plan_value", "") or ""
            reason = cinfo.get("reason", "") or ""
            _record(
                scope=f"L{i+1}",
                field=f,
                ocr_val=ocr_v,
                snap_val=snap_v,
                status=status,
                plan_val=plan_v,
                reason=reason,
                fix_rule=fix_rule,
            )

    # --- Footer ---
    for f in FOOTER_FIELDS:
        ocr_v = _get_field(raw, ["footer", f])
        snap_v = _get_field(final, ["footer", f])
        path = f"footer.{f}"
        a = audit.get(path, {})
        fix_chain = a.get("fix_chain", [])
        fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
        cc_info = cc_footer.get(f, {})
        status = cc_info.get("status", "NA")
        _record(
            scope="FOOTER",
            field=f,
            ocr_val=ocr_v,
            snap_val=snap_v,
            status=status,
            fix_rule=fix_rule,
        )

    # --- Write CSV ---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["ROW", "CAMPO", "OCR_RAW", "APOS_DQ_SNAP", "STATUS",
                        "PLAN_VALOR", "RAZAO", "REGRA_DQ"],
            delimiter=";",
        )
        w.writeheader()
        for r in rows_out:
            w.writerow(r)

    # Summary stats
    by_status = {}
    by_changed = {"unchanged": 0, "snap_changed": 0}
    for r in rows_out:
        by_status[r["STATUS"]] = by_status.get(r["STATUS"], 0) + 1
        if str(r["OCR_RAW"]) != str(r["APOS_DQ_SNAP"]):
            by_changed["snap_changed"] += 1
        else:
            by_changed["unchanged"] += 1

    print(f"Wrote {len(rows_out)} rows → {out_path}")
    print(f"Status counts: {by_status}")
    print(f"OCR vs DQ-snap: {by_changed}")


if __name__ == "__main__":
    main()
