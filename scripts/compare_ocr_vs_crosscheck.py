"""Build a side-by-side CSV: raw OCR vs DQ-snap vs cross-check status.

Per cell of a sheet, output:
    ROW | CAMPO | OCR_RAW | APOS_DQ_SNAP | STATUS | REF_ORIGEM | REF_VALOR | RAZAO

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
from app.templates_registry import get_template  # noqa: E402

HEADER_FIELDS = ("operador", "n_operador", "setor_maquina", "data")
ROW_FIELDS = (
    "pri", "cliente", "ov", "of", "modelo", "qtd",
    "comp_mm", "larg_mm", "lote", "coni", "esp", "lbase", "ltopo",
)
FOOTER_FIELDS = ("colunas_produzidas", "horas_trabalhadas")


def _ordered_union(*groups):
    out = []
    seen = set()
    for group in groups:
        for item in group or ():
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


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


def _ref_value(cell: dict) -> str:
    ref = cell.get("ref")
    if ref not in (None, ""):
        return str(ref)
    plan_value = cell.get("plan_value")
    return "" if plan_value is None else str(plan_value)


def _ref_source(cell: dict) -> str:
    return str(cell.get("ref_source") or cell.get("source") or "")


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
    try:
        template = get_template(final.get("template_name", "bobine_formato"))
        header_fields = _ordered_union(
            template.header_fields,
            HEADER_FIELDS,
            (raw.get("header") or {}).keys(),
            (final.get("header") or {}).keys(),
            cc_header.keys(),
        )
        row_fields_base = _ordered_union(template.row_fields, ROW_FIELDS)
        footer_fields = _ordered_union(
            template.footer_fields,
            FOOTER_FIELDS,
            (raw.get("footer") or {}).keys(),
            (final.get("footer") or {}).keys(),
            cc_footer.keys(),
        )
    except Exception:  # noqa: BLE001
        header_fields = _ordered_union(
            HEADER_FIELDS,
            (raw.get("header") or {}).keys(),
            (final.get("header") or {}).keys(),
            cc_header.keys(),
        )
        row_fields_base = list(ROW_FIELDS)
        footer_fields = _ordered_union(
            FOOTER_FIELDS,
            (raw.get("footer") or {}).keys(),
            (final.get("footer") or {}).keys(),
            cc_footer.keys(),
        )

    rows_out = []

    def _record(scope: str, field: str, ocr_val, snap_val, status: str,
                ref_val: str = "", ref_source: str = "",
                reason: str = "", fix_rule: str = ""):
        rows_out.append({
            "ROW": scope,
            "CAMPO": field.upper(),
            "OCR_RAW": ocr_val if ocr_val is not None else "",
            "APOS_DQ_SNAP": snap_val if snap_val is not None else "",
            "STATUS": status,
            "REF_ORIGEM": ref_source,
            "REF_VALOR": ref_val,
            "RAZAO": reason,
            "REGRA_DQ": fix_rule,
        })

    # --- Header ---
    for f in header_fields:
        ocr_v = _get_field(raw, ["header", f])
        snap_v = _get_field(final, ["header", f])
        path = f"header.{f}"
        a = audit.get(path, {})
        fix_chain = a.get("fix_chain", [])
        fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
        cc_info = cc_header.get(f, {})
        status = cc_info.get("status", "NA")
        ref_v = _ref_value(cc_info)
        _record(
            scope="HEADER",
            field=f,
            ocr_val=ocr_v,
            snap_val=snap_v,
            status=status,
            ref_val=ref_v,
            ref_source=_ref_source(cc_info),
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

        for f in _ordered_union(row_fields_base, ocr_row.keys(), snap_row.keys(), cross_fields.keys()):
            ocr_v = ocr_row.get(f, "")
            snap_v = snap_row.get(f, "")
            path = f"rows[{i}].{f}"
            a = audit.get(path, {})
            fix_chain = a.get("fix_chain", [])
            fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
            cinfo = cross_fields.get(f, {})
            status = cinfo.get("status", "?")
            ref_v = _ref_value(cinfo)
            reason = cinfo.get("reason", "") or ""
            _record(
                scope=f"L{i+1}",
                field=f,
                ocr_val=ocr_v,
                snap_val=snap_v,
                status=status,
                ref_val=ref_v,
                ref_source=_ref_source(cinfo),
                reason=reason,
                fix_rule=fix_rule,
            )

    # --- Footer ---
    for f in footer_fields:
        ocr_v = _get_field(raw, ["footer", f])
        snap_v = _get_field(final, ["footer", f])
        path = f"footer.{f}"
        a = audit.get(path, {})
        fix_chain = a.get("fix_chain", [])
        fix_rule = " | ".join(fc for fc in fix_chain if "L2:" in fc)
        cc_info = cc_footer.get(f, {})
        status = cc_info.get("status", "NA")
        ref_v = _ref_value(cc_info)
        _record(
            scope="FOOTER",
            field=f,
            ocr_val=ocr_v,
            snap_val=snap_v,
            status=status,
            ref_val=ref_v,
            ref_source=_ref_source(cc_info),
            fix_rule=fix_rule,
        )

    # --- Write CSV ---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=["ROW", "CAMPO", "OCR_RAW", "APOS_DQ_SNAP", "STATUS",
                        "REF_ORIGEM", "REF_VALOR", "RAZAO", "REGRA_DQ"],
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
