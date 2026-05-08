"""Round 29 Phase D — Excel multi-sheet export.

Output structure:
- `Resumo`: period totals, per-day summary, per-operator summary
- One sheet per production day (`DD-MM-YYYY`):
  - Day header row (date, total qty, total sheets, total OFs)
  - Sub-tables per operator: their rows for that day in the standard
    13-column kanban format

Reads from `production_rows` (denormalized) for fast iteration.
"""
from __future__ import annotations

import io
from collections import defaultdict

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .db import conn

# Order of columns in the per-day per-operator sub-tables (matches kanban)
ROW_COLUMNS = [
    ("pri", "PRI"),
    ("cliente", "CLIENTE"),
    ("ov", "OV"),
    ("of", "OF"),
    ("modelo", "MODELO"),
    ("qtd", "QTD"),
    ("comp_mm", "COMP_MM"),
    ("larg_mm", "LARG_MM"),
    ("lote", "LOTE"),
    ("coni", "CONI"),
    ("esp", "ESP"),
    ("lbase", "LBASE"),
    ("ltopo", "LTOPO"),
]


# ---- styles ------------------------------------------------------------

_BORDER = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)

_FONT_BASE = Font(name="Inter", size=10)
_FONT_BOLD = Font(name="Inter", size=10, bold=True)
_FONT_HEADER = Font(name="Inter", size=11, bold=True, color="FFFFFF")
_FONT_TITLE = Font(name="Inter", size=14, bold=True, color="16140F")
_FONT_HERO = Font(name="Inter", size=20, bold=True, color="16140F")

_FILL_HEADER = PatternFill("solid", fgColor="2F5597")
_FILL_OPERATOR = PatternFill("solid", fgColor="DCE6F1")
_FILL_DAYHEAD = PatternFill("solid", fgColor="F0E2C9")


def _query_rows(date_from: str, date_to: str, operador: str | None) -> list[dict]:
    """Return all production_rows in [date_from, date_to] (inclusive),
    optionally filtered by operador. Joined with sheets to get
    setor_maquina + canonical sheets.operador (post-validate)."""
    sql = """
        SELECT pr.*,
               s.operador AS validated_operador,
               json_extract(s.sheet_data, '$.header.setor_maquina') AS setor_maquina,
               json_extract(s.sheet_data, '$.footer.colunas_produzidas') AS colunas_produzidas,
               json_extract(s.sheet_data, '$.footer.horas_trabalhadas') AS horas_trabalhadas
          FROM production_rows pr
          JOIN sheets s ON s.id = pr.sheet_id
         WHERE pr.sheet_iso_date BETWEEN ? AND ?
    """
    params: list = [date_from, date_to]
    if operador:
        # Match either denormalized operador or validated.operador
        sql += " AND (UPPER(pr.operador) = UPPER(?) OR UPPER(s.operador) = UPPER(?))"
        params.extend([operador, operador])
    sql += " ORDER BY pr.sheet_iso_date ASC, pr.operador ASC, pr.sheet_id ASC, pr.row_index ASC"

    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _operador_canon(row: dict) -> str:
    """Use the validated operador if available, else the row's stored operador."""
    return (row.get("validated_operador") or row.get("operador") or "—").strip() or "—"


def _date_iso_to_pt(iso: str) -> str:
    """YYYY-MM-DD → DD-MM-YYYY for sheet titles + day headers."""
    if not iso or len(iso) != 10:
        return iso or ""
    return f"{iso[8:10]}-{iso[5:7]}-{iso[0:4]}"


def _style_cell(cell, *, bold=False, fill=None, align="left"):
    cell.font = _FONT_BOLD if bold else _FONT_BASE
    cell.border = _BORDER
    if fill is not None:
        cell.fill = fill
    cell.alignment = Alignment(horizontal=align, vertical="center")


def _write_resumo(ws, all_rows: list[dict], date_from: str, date_to: str) -> None:
    """First sheet: period summary + per-day summary + per-operator summary."""
    ws.title = "Resumo"

    # Title
    ws["A1"] = "Metalogalva — Resumo de Produção"
    ws["A1"].font = _FONT_TITLE
    ws.merge_cells("A1:F1")

    ws["A2"] = "Período"
    ws["B2"] = f"{_date_iso_to_pt(date_from)} a {_date_iso_to_pt(date_to)}"
    ws["A2"].font = _FONT_BOLD

    # Aggregate
    total_qty = sum(r["qtd"] or 0 for r in all_rows)
    sheet_ids = {r["sheet_id"] for r in all_rows}
    of_set = {r["of"] for r in all_rows if r["of"]}
    operadores = {_operador_canon(r) for r in all_rows}

    ws["A4"] = "TOTAL COLUNAS"
    ws["B4"] = total_qty
    ws["A4"].font = _FONT_BOLD
    ws["B4"].font = _FONT_HERO

    ws["A5"] = "TOTAL KANBANS"
    ws["B5"] = len(sheet_ids)
    ws["A5"].font = _FONT_BOLD

    ws["A6"] = "OFs ÚNICAS"
    ws["B6"] = len(of_set)
    ws["A6"].font = _FONT_BOLD

    ws["A7"] = "OPERADORES"
    ws["B7"] = len(operadores)
    ws["A7"].font = _FONT_BOLD

    # Per-day summary
    by_day: dict[str, dict] = defaultdict(lambda: {"qtd": 0, "sheets": set(), "ofs": set(), "ops": set()})
    for r in all_rows:
        d = by_day[r["sheet_iso_date"]]
        d["qtd"] += r["qtd"] or 0
        d["sheets"].add(r["sheet_id"])
        if r["of"]:
            d["ofs"].add(r["of"])
        d["ops"].add(_operador_canon(r))

    row_idx = 9
    ws.cell(row=row_idx, column=1, value="Por dia").font = _FONT_BOLD
    row_idx += 1
    headers = ["DATA", "COLUNAS", "KANBANS", "OFs", "OPERADORES"]
    for ci, h in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=ci, value=h)
        _style_cell(cell, bold=True, fill=_FILL_HEADER)
        cell.font = _FONT_HEADER
    row_idx += 1
    for d_iso in sorted(by_day):
        d = by_day[d_iso]
        ws.cell(row=row_idx, column=1, value=_date_iso_to_pt(d_iso))
        ws.cell(row=row_idx, column=2, value=d["qtd"])
        ws.cell(row=row_idx, column=3, value=len(d["sheets"]))
        ws.cell(row=row_idx, column=4, value=len(d["ofs"]))
        ws.cell(row=row_idx, column=5, value=len(d["ops"]))
        for ci in range(1, 6):
            _style_cell(ws.cell(row=row_idx, column=ci))
        row_idx += 1

    # Per-operator summary
    row_idx += 2
    by_op: dict[str, dict] = defaultdict(lambda: {"qtd": 0, "sheets": set(), "ofs": set(), "days": set()})
    for r in all_rows:
        op = _operador_canon(r)
        by_op[op]["qtd"] += r["qtd"] or 0
        by_op[op]["sheets"].add(r["sheet_id"])
        if r["of"]:
            by_op[op]["ofs"].add(r["of"])
        by_op[op]["days"].add(r["sheet_iso_date"])

    ws.cell(row=row_idx, column=1, value="Por operador").font = _FONT_BOLD
    row_idx += 1
    headers = ["OPERADOR", "COLUNAS", "KANBANS", "OFs", "DIAS"]
    for ci, h in enumerate(headers, start=1):
        cell = ws.cell(row=row_idx, column=ci, value=h)
        _style_cell(cell, bold=True, fill=_FILL_HEADER)
        cell.font = _FONT_HEADER
    row_idx += 1
    for op in sorted(by_op, key=lambda x: -by_op[x]["qtd"]):
        d = by_op[op]
        ws.cell(row=row_idx, column=1, value=op)
        ws.cell(row=row_idx, column=2, value=d["qtd"])
        ws.cell(row=row_idx, column=3, value=len(d["sheets"]))
        ws.cell(row=row_idx, column=4, value=len(d["ofs"]))
        ws.cell(row=row_idx, column=5, value=len(d["days"]))
        for ci in range(1, 6):
            _style_cell(ws.cell(row=row_idx, column=ci))
        row_idx += 1

    # Column widths
    for ci, width in enumerate([22, 14, 12, 10, 12, 14], start=1):
        ws.column_dimensions[get_column_letter(ci)].width = width


def _write_day_sheet(wb: openpyxl.Workbook, day_iso: str, day_rows: list[dict]) -> None:
    """Per-day worksheet: day-header row + sub-tables per operator."""
    title = _date_iso_to_pt(day_iso)  # 'DD-MM-YYYY' (Excel allows '-' in titles)
    ws = wb.create_sheet(title=title[:31])  # Excel max 31 chars

    # Day header
    qtd = sum(r["qtd"] or 0 for r in day_rows)
    sheets_n = len({r["sheet_id"] for r in day_rows})
    ofs_n = len({r["of"] for r in day_rows if r["of"]})

    ws["A1"] = f"Produção — {title}"
    ws["A1"].font = _FONT_TITLE
    ws.merge_cells("A1:F1")

    ws["A2"] = "TOTAL COLUNAS"
    ws["B2"] = qtd
    ws["A2"].font = _FONT_BOLD
    ws["B2"].font = _FONT_HERO

    ws["C2"] = "KANBANS"
    ws["D2"] = sheets_n
    ws["C2"].font = _FONT_BOLD
    ws["D2"].font = _FONT_BOLD

    ws["E2"] = "OFs"
    ws["F2"] = ofs_n
    ws["E2"].font = _FONT_BOLD
    ws["F2"].font = _FONT_BOLD

    # Group by operator
    by_op: dict[str, list[dict]] = defaultdict(list)
    for r in day_rows:
        by_op[_operador_canon(r)].append(r)

    row_idx = 4
    for op in sorted(by_op):
        # Operator header bar
        ws.cell(row=row_idx, column=1, value=op).font = _FONT_BOLD
        ws.merge_cells(start_row=row_idx, start_column=1,
                       end_row=row_idx, end_column=len(ROW_COLUMNS))
        for ci in range(1, len(ROW_COLUMNS) + 1):
            ws.cell(row=row_idx, column=ci).fill = _FILL_OPERATOR
        row_idx += 1

        # Column headers
        for ci, (_, label) in enumerate(ROW_COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=ci, value=label)
            _style_cell(cell, bold=True, fill=_FILL_HEADER)
            cell.font = _FONT_HEADER
        row_idx += 1

        # Rows
        for r in by_op[op]:
            for ci, (key, _) in enumerate(ROW_COLUMNS, start=1):
                v = r.get(key)
                cell = ws.cell(row=row_idx, column=ci, value=v if v is not None else "")
                _style_cell(cell)
            row_idx += 1

        row_idx += 1  # blank line between operators

    # Column widths — generous defaults for Inter font
    widths = [6, 14, 11, 10, 14, 7, 10, 10, 12, 10, 8, 9, 9]
    for ci, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = w


def export_excel(date_from: str, date_to: str, operador: str | None = None) -> bytes:
    """Build and return the .xlsx file bytes for the given period."""
    rows = _query_rows(date_from, date_to, operador)

    wb = openpyxl.Workbook()
    ws = wb.active
    _write_resumo(ws, rows, date_from, date_to)

    # Group by day, write one sheet per day with data
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["sheet_iso_date"]:
            by_day[r["sheet_iso_date"]].append(r)
    for day_iso in sorted(by_day):
        _write_day_sheet(wb, day_iso, by_day[day_iso])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def filename_for(date_from: str, date_to: str, operador: str | None = None) -> str:
    """Slugged filename for the download."""
    op_suffix = ""
    if operador:
        slug = operador.upper().replace(" ", "")
        # Strip non-ascii (rough)
        slug = "".join(ch for ch in slug if ch.isalnum())
        op_suffix = f"_{slug}"
    return f"metalogalva_{date_from}_{date_to}{op_suffix}.xlsx"
