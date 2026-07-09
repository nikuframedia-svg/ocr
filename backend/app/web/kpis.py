"""KPI computation — runs over ``production_rows`` (denormalised) for
sub-100ms aggregations even at 500K+ rows.

All functions return plain dicts/lists ready for Jinja2 + Chart.js.

Date semantics:
- ``sheet_iso_date`` is YYYY-MM-DD from header.data (the sheet's own
  date as the operator wrote it). Use this for production-day grouping.
- ``captured_at`` is when the photo was uploaded. Diverges from sheet
  date if a photo is uploaded later. Use for lead-time and ingest-rate.
"""
from __future__ import annotations

from app.dq.machines import resolve_machine_from_setor
from app.production.weights import calculate_row_weights

from . import kpi_params
from .db import conn


# --------------------------------------------------------------------------
# Production sector flow constants (Round 29 Phase C)
# --------------------------------------------------------------------------

# Order matches the screenshot from the user (Bobine → ... → Expedição).
# R68: phase names aligned with templates_registry.py — Title Case, no
# acentos in "Bobine Formato"/"Corte"/"Quinadora", merge of Manual+Robot
# into "Abertura Portinholas", Gemini under "Corte".
PRODUCTION_SECTORS = (
    "Bobine Formato",
    "Corte",
    "Quinadora",
    "Soldadura",
    "Abertura Portinholas",
    "Acabamento",
    "Expedição",
)

# Maps sector name → list of cod_maquina that belong to it (mirrors the
# factory validator at C:\kanban\nifruka\06_Kanban_OCR\kanban_csv2excel...).
# The lists mirror maquinas.xlsx for the kanbans we now OCR directly.
SECTOR_TO_COD_MAQUINAS = {
    "Bobine Formato": ["M032"],
    "Corte": ["M020", "M091"],          # Guilhotina 9m, 10m
    "Quinadora": [],                     # not captured yet
    "Soldadura": [],
    "Abertura Portinholas": [],
    "Acabamento": ["M001", "M054", "M055", "M061", "M106", "M107"],
    "Expedição": [],
}

# Reverse lookup: cod_maquina → sector name (for routing rows from DB into
# the right sector card). Sectors without machines map to nothing.
COD_MAQUINA_TO_SECTOR = {
    cm: sector
    for sector, cms in SECTOR_TO_COD_MAQUINAS.items()
    for cm in cms
}

_PHASE_TO_SECTOR = {
    "bf": "Bobine Formato",
    "c": "Corte",
    "q": "Quinadora",
    "s": "Soldadura",
    "r": "Abertura Portinholas",
    "a": "Acabamento",
    "exp": "Expedição",
}


def _sector_machine_codes(refs: dict | None = None) -> dict[str, list[str]]:
    """Machine buckets, preferring ``maquinas.xlsx`` over static fallbacks."""
    out = {sector: list(codes) for sector, codes in SECTOR_TO_COD_MAQUINAS.items()}
    for rec in ((refs or {}).get("maquinas_by_codmaq") or {}).values():
        code = str(rec.get("codmaq") or "").strip().upper()
        phase = str(rec.get("colunaexcel") or "").strip().lower()
        sector = _PHASE_TO_SECTOR.get(phase)
        if code and sector and code not in out.setdefault(sector, []):
            out[sector].append(code)
    return out


def _cod_machine_to_sector(refs: dict | None = None) -> dict[str, str]:
    return {
        cm: sector
        for sector, codes in _sector_machine_codes(refs).items()
        for cm in codes
    }


# Setor name → cod_maquina derivation (matches factory validator's
# _CODIGOS_MAQUINAS mapping at C:\kanban\nifruka\06_Kanban_OCR\...).
def _derive_cod_maquina(setor_maquina: str | None, refs: dict | None = None) -> str:
    """Convert setor_maquina text → cod_maquina code. Empty if unknown."""
    if not setor_maquina:
        return ""
    rec = resolve_machine_from_setor(setor_maquina, refs)
    if rec and rec.get("codmaq"):
        return str(rec["codmaq"]).strip().upper()
    s = setor_maquina.strip().upper()
    table = {
        "BOBINE-FORMATO": "M032",
        "BOB.FORMATO": "M032",
        "BOBINE FORMATO": "M032",
        "GUILHOTINA 9M": "M020",
        "GUILH. 9M": "M020",
        "GUILHOTINA 10M": "M091",
        "GUILH. 10M": "M091",
        "ACABAMENTO MTG2": "M001",
        "ACAB. MTG2": "M001",
        "ACABAMENTO MTG3": "M055",
        "ACAB. MTG3": "M055",
        "ACABAMENTO MTG4": "M061",
        "ACAB. MTG4": "M061",
        "ACABAMENTO MTG5": "M106",
        "ACAB. MTG5": "M106",
        "ACABAMENTO SERRALHARIA MTG2": "M107",
        "ACAB. SERRALHARIA MTG2": "M107",
        "MTG2": "M001",
        # R132 — TPL103 MÁQUINA DE FUSTES (codsec S004 em maquinas.xlsx).
        # Fallback defensivo — o caminho normal é via `maquinas_by_kanban`
        # (`_apply_codmaq_fill`); este só aplica em CPIS export para folhas
        # legacy sem header.cod_maquina preenchido.
        "MÁQUINA DE FUSTES": "M031",
        "MAQUINA DE FUSTES": "M031",
        "MAQ DE FUSTES": "M031",
        "MAQ FUSTES": "M031",
    }
    if s in table:
        return table[s]
    for key, code in table.items():
        if key in s:
            return code
    return ""


# --------------------------------------------------------------------------
# Watermark — used by client-side banner to detect new uploads since open
# --------------------------------------------------------------------------

def latest_sheet_id() -> int:
    """Highest sheet id, or 0 if no sheets. Used as a monotonically-
    increasing watermark for live-update banners ("X new sheets")."""
    with conn() as c:
        row = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM sheets").fetchone()
    return row["m"] or 0


# --------------------------------------------------------------------------
# Today / window summary
# --------------------------------------------------------------------------

def today_summary(reference_date: str | None = None) -> dict:
    """Output for ``reference_date`` (default: today, ISO YYYY-MM-DD).

    Returns total qty + total sheets + total hours + 4 breakdowns.

    Hours are computed via a per-sheet subquery (DISTINCT on sheet_id,
    not on sheet_hours value) so two sheets with identical hours are
    correctly summed.
    """
    ref = reference_date  # NULL means we use SQL date('now', 'localtime')
    where_date = "DATE('now', 'localtime')" if ref is None else "?"
    params: tuple = () if ref is None else (ref,)

    with conn() as c:
        totals = c.execute(
            f"""SELECT
                    COALESCE(SUM(qtd), 0) AS total_qty,
                    COUNT(DISTINCT sheet_id) AS total_sheets,
                    COALESCE((SELECT SUM(h) FROM (
                        SELECT DISTINCT sheet_id, sheet_hours AS h FROM production_rows
                        WHERE sheet_iso_date = {where_date}
                    )), 0) AS total_hours,
                    COUNT(*) AS total_rows,
                    COUNT(DISTINCT of) AS total_ofs,
                    COUNT(DISTINCT cliente) AS total_clientes,
                    COUNT(DISTINCT CASE WHEN sheet_status = 'extracted' THEN sheet_id END) AS sheets_pending,
                    COUNT(DISTINCT CASE WHEN sheet_status = 'validated' THEN sheet_id END) AS sheets_validated
                FROM production_rows
                WHERE sheet_iso_date = {where_date}""",
            (*params, *params),
        ).fetchone()

        by_operador = c.execute(
            f"""SELECT pr.operador,
                       SUM(pr.qtd) AS qty,
                       COUNT(DISTINCT pr.sheet_id) AS sheets,
                       (SELECT SUM(h) FROM (
                           SELECT DISTINCT sheet_id, sheet_hours AS h
                           FROM production_rows
                           WHERE sheet_iso_date = {where_date}
                             AND operador = pr.operador
                       )) AS hours
                FROM production_rows pr
                WHERE pr.sheet_iso_date = {where_date} AND pr.operador IS NOT NULL
                GROUP BY pr.operador
                ORDER BY qty DESC""",
            (*params, *params),
        ).fetchall()

        by_of = c.execute(
            f"""SELECT of, SUM(qtd) AS qty, GROUP_CONCAT(DISTINCT modelo) AS modelos,
                       MAX(cliente) AS cliente
                FROM production_rows
                WHERE sheet_iso_date = {where_date} AND of IS NOT NULL
                GROUP BY of
                ORDER BY qty DESC
                LIMIT 10""",
            params,
        ).fetchall()

        by_cliente = c.execute(
            f"""SELECT cliente, SUM(qtd) AS qty, COUNT(DISTINCT of) AS ofs
                FROM production_rows
                WHERE sheet_iso_date = {where_date} AND cliente IS NOT NULL
                GROUP BY cliente
                ORDER BY qty DESC
                LIMIT 10""",
            params,
        ).fetchall()

        by_modelo = c.execute(
            f"""SELECT modelo, SUM(qtd) AS qty, COUNT(DISTINCT of) AS ofs
                FROM production_rows
                WHERE sheet_iso_date = {where_date} AND modelo IS NOT NULL
                GROUP BY modelo
                ORDER BY qty DESC
                LIMIT 10""",
            params,
        ).fetchall()

    return {
        "date": ref or "today",
        "totals": dict(totals),
        "by_operador": [dict(r) for r in by_operador],
        "by_of": [dict(r) for r in by_of],
        "by_cliente": [dict(r) for r in by_cliente],
        "by_modelo": [dict(r) for r in by_modelo],
    }


# --------------------------------------------------------------------------
# Trends
# --------------------------------------------------------------------------

def daily_trend(days: int = 30, end_date: str | None = None) -> list[dict]:
    """Per-day (last N days ending at ``end_date``): qty, sheets, hours, ofs.

    ``end_date`` defaults to today. When the user filters /dashboard with
    ``?date=YYYY-MM-DD``, the Timeline must center on that date too —
    otherwise the user sees the filtered KPIs but a 7-day window from
    today, which is confusing.

    Hours sum is computed per-day via a DISTINCT (sheet_id, sheet_hours)
    subquery aliased to the outer date — see B1 fix in the audit plan.
    """
    if end_date:
        # window = [end_date - days, end_date]
        date_lo = "DATE(?, ?)"
        date_hi = "DATE(?)"
        params = (end_date, f"-{days} days", end_date)
    else:
        date_lo = "DATE('now', 'localtime', ?)"
        date_hi = "DATE('now', 'localtime')"
        params = (f"-{days} days",)
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"""SELECT pr.sheet_iso_date AS day,
                       SUM(pr.qtd) AS qty,
                       COUNT(DISTINCT pr.sheet_id) AS sheets,
                       (SELECT SUM(h) FROM (
                           SELECT DISTINCT sheet_id, sheet_hours AS h
                           FROM production_rows
                           WHERE sheet_iso_date = pr.sheet_iso_date
                       )) AS hours,
                       COUNT(DISTINCT pr.of) AS ofs
                FROM production_rows pr
                WHERE pr.sheet_iso_date IS NOT NULL
                  AND pr.sheet_iso_date >= {date_lo}
                  AND pr.sheet_iso_date <= {date_hi}
                GROUP BY pr.sheet_iso_date
                ORDER BY pr.sheet_iso_date ASC""",
            params,
        ).fetchall()]


def daily_per_operador(days: int = 7, end_date: str | None = None) -> list[dict]:
    """Per (day, operador) qty over the N days ending at ``end_date``
    (defaults to today). Used by the Today dashboard Timeline view."""
    if end_date:
        date_lo = "DATE(?, ?)"
        date_hi = "DATE(?)"
        params = (end_date, f"-{days} days", end_date)
    else:
        date_lo = "DATE('now', 'localtime', ?)"
        date_hi = "DATE('now', 'localtime')"
        params = (f"-{days} days",)
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"""SELECT sheet_iso_date AS day, operador, SUM(qtd) AS qty,
                       COUNT(DISTINCT sheet_id) AS sheets
                FROM production_rows
                WHERE operador IS NOT NULL
                  AND sheet_iso_date IS NOT NULL
                  AND sheet_iso_date >= {date_lo}
                  AND sheet_iso_date <= {date_hi}
                GROUP BY sheet_iso_date, operador
                ORDER BY day, operador""",
            params,
        ).fetchall()]


def weekly_trend(weeks: int = 12) -> list[dict]:
    """Per-week (last N weeks): qty, sheets."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT strftime('%Y-W%W', sheet_iso_date) AS week,
                      SUM(qtd) AS qty,
                      COUNT(DISTINCT sheet_id) AS sheets,
                      COUNT(DISTINCT of) AS ofs
               FROM production_rows
               WHERE sheet_iso_date IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY week
               ORDER BY week ASC""",
            (f"-{weeks * 7} days",),
        ).fetchall()]


def monthly_trend(months: int = 12) -> list[dict]:
    """Per-month last N months: qty, sheets, distinct clientes."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT substr(sheet_iso_date, 1, 7) AS month,
                      SUM(qtd) AS qty,
                      COUNT(DISTINCT sheet_id) AS sheets,
                      COUNT(DISTINCT cliente) AS clientes,
                      COUNT(DISTINCT of) AS ofs
               FROM production_rows
               WHERE sheet_iso_date IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY month
               ORDER BY month ASC""",
            (f"-{months} months",),
        ).fetchall()]


# --------------------------------------------------------------------------
# Per-worker views
# --------------------------------------------------------------------------

def workers_summary() -> list[dict]:
    """All operadores with cumulative metrics + a 7-day qty trend
    list (oldest → newest, left-padded with zeros) ready for MiniBars."""
    import datetime as _dt
    today = _dt.date.today()
    days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT operador,
                      COUNT(DISTINCT sheet_id) AS sheets,
                      SUM(qtd) AS total_qty,
                      COUNT(DISTINCT of) AS distinct_ofs,
                      COUNT(DISTINCT cliente) AS distinct_clientes,
                      MIN(sheet_iso_date) AS first_day,
                      MAX(sheet_iso_date) AS last_day,
                      AVG(qtd) AS avg_qty_per_row
               FROM production_rows
               WHERE operador IS NOT NULL
               GROUP BY operador
               ORDER BY total_qty DESC"""
        ).fetchall()]
        trend_rows = c.execute(
            """SELECT operador, sheet_iso_date AS day, SUM(qtd) AS qty
               FROM production_rows
               WHERE operador IS NOT NULL
                 AND sheet_iso_date IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', '-7 days')
               GROUP BY operador, sheet_iso_date""",
        ).fetchall()
    trend_by_op: dict[str, dict[str, int]] = {}
    for tr in trend_rows:
        trend_by_op.setdefault(tr["operador"], {})[tr["day"]] = tr["qty"] or 0
    for row in rows:
        m = trend_by_op.get(row["operador"], {})
        row["trend"] = [m.get(d, 0) for d in days]
    return rows


def worker_detail(operador: str) -> dict:
    """One operador: per-day trend, mix per cliente, mix per modelo."""
    with conn() as c:
        per_day = [dict(r) for r in c.execute(
            """SELECT sheet_iso_date AS day, SUM(qtd) AS qty, COUNT(DISTINCT sheet_id) AS sheets
               FROM production_rows
               WHERE operador = ? AND sheet_iso_date IS NOT NULL
               GROUP BY sheet_iso_date ORDER BY sheet_iso_date""",
            (operador,),
        ).fetchall()]
        mix_cliente = [dict(r) for r in c.execute(
            """SELECT cliente, SUM(qtd) AS qty, COUNT(DISTINCT of) AS ofs
               FROM production_rows
               WHERE operador = ? AND cliente IS NOT NULL
               GROUP BY cliente ORDER BY qty DESC""",
            (operador,),
        ).fetchall()]
        mix_modelo = [dict(r) for r in c.execute(
            """SELECT modelo, SUM(qtd) AS qty, COUNT(*) AS rows,
                      AVG(comp_mm) AS avg_comp
               FROM production_rows
               WHERE operador = ? AND modelo IS NOT NULL
               GROUP BY modelo ORDER BY qty DESC LIMIT 20""",
            (operador,),
        ).fetchall()]
        recent_sheets = [dict(r) for r in c.execute(
            """SELECT DISTINCT sheet_id, sheet_iso_date, sheet_status, sheet_hours
               FROM production_rows
               WHERE operador = ?
               ORDER BY sheet_iso_date DESC LIMIT 20""",
            (operador,),
        ).fetchall()]
    return {
        "operador": operador,
        "per_day": per_day,
        "mix_cliente": mix_cliente,
        "mix_modelo": mix_modelo,
        "recent_sheets": recent_sheets,
    }


# --------------------------------------------------------------------------
# Per-OF (job) views
# --------------------------------------------------------------------------

def jobs_summary(limit: int = 100) -> list[dict]:
    """All distinct OFs with cumulative metrics."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT of,
                      MAX(cliente) AS cliente,
                      GROUP_CONCAT(DISTINCT modelo) AS modelos,
                      SUM(qtd) AS total_qty,
                      COUNT(DISTINCT operador) AS operadores,
                      COUNT(DISTINCT sheet_id) AS sheets,
                      MIN(sheet_iso_date) AS first_day,
                      MAX(sheet_iso_date) AS last_day
               FROM production_rows
               WHERE of IS NOT NULL
               GROUP BY of
               ORDER BY last_day DESC, total_qty DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()]


def job_detail(of_number: str) -> dict:
    """One OF: timeline (per day), operadores envolvidos, sheets that touched it."""
    with conn() as c:
        timeline = [dict(r) for r in c.execute(
            """SELECT sheet_iso_date AS day, operador, SUM(qtd) AS qty,
                      GROUP_CONCAT(DISTINCT modelo) AS modelos
               FROM production_rows
               WHERE of = ?
               GROUP BY sheet_iso_date, operador
               ORDER BY sheet_iso_date, operador""",
            (of_number,),
        ).fetchall()]
        sheets_in = [dict(r) for r in c.execute(
            """SELECT DISTINCT sheet_id, sheet_iso_date, operador, sheet_status
               FROM production_rows
               WHERE of = ?
               ORDER BY sheet_iso_date""",
            (of_number,),
        ).fetchall()]
        rows_in = [dict(r) for r in c.execute(
            """SELECT sheet_id, row_index, operador, sheet_iso_date,
                      cliente, modelo, qtd, comp_mm, larg_mm, lote, coni, esp, lbase, ltopo
               FROM production_rows
               WHERE of = ?
               ORDER BY sheet_iso_date, sheet_id, row_index""",
            (of_number,),
        ).fetchall()]
    return {
        "of": of_number,
        "timeline": timeline,
        "sheets": sheets_in,
        "rows": rows_in,
    }


# --------------------------------------------------------------------------
# Cross-cutting matrices
# --------------------------------------------------------------------------

def operador_cliente_matrix(months: int = 12) -> dict:
    """Heatmap data: operador × cliente, with avg qty/hour as the metric.

    Returns ``{operadores, clientes, cells}`` where cells is a list of
    ``{operador, cliente, qty, sheets, rate_per_hour}``.
    """
    with conn() as c:
        cells = [dict(r) for r in c.execute(
            """SELECT pr.operador, pr.cliente,
                      SUM(pr.qtd) AS qty,
                      COUNT(DISTINCT pr.sheet_id) AS sheets,
                      CASE
                          WHEN (SELECT SUM(h) FROM (
                                  SELECT DISTINCT sheet_id, sheet_hours AS h
                                  FROM production_rows
                                  WHERE operador = pr.operador
                                    AND cliente = pr.cliente
                                    AND sheet_iso_date >= DATE('now', 'localtime', ?))
                                ) > 0
                          THEN SUM(pr.qtd) /
                               (SELECT SUM(h) FROM (
                                  SELECT DISTINCT sheet_id, sheet_hours AS h
                                  FROM production_rows
                                  WHERE operador = pr.operador
                                    AND cliente = pr.cliente
                                    AND sheet_iso_date >= DATE('now', 'localtime', ?)))
                          ELSE 0
                      END AS rate_per_hour
               FROM production_rows pr
               WHERE pr.operador IS NOT NULL AND pr.cliente IS NOT NULL
                 AND pr.sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY pr.operador, pr.cliente
               ORDER BY pr.operador, pr.cliente""",
            (f"-{months * 31} days", f"-{months * 31} days", f"-{months * 31} days"),
        ).fetchall()]
        # R117 — SUM() devolve NULL em grupos vazios; descarta cells sem qty
        # e placeholders óbvios de cliente; coerce rate_per_hour NULL → 0.
        cells = [
            {**row, "rate_per_hour": row["rate_per_hour"] or 0}
            for row in cells
            if row["qty"] is not None
            and (row["cliente"] or "").strip() not in ("", "/", "-")
        ]
        operadores = sorted({row["operador"] for row in cells})
        clientes = sorted({row["cliente"] for row in cells})
    return {"operadores": operadores, "clientes": clientes, "cells": cells}


def operador_modelo_matrix(months: int = 12, top_n: int = 25) -> dict:
    """Matriz competências: operador × modelo, with qty + count.

    Limits to ``top_n`` modelos by total volume to keep the matrix readable.
    """
    with conn() as c:
        top_modelos_rows = c.execute(
            """SELECT modelo, SUM(qtd) AS total
               FROM production_rows
               WHERE modelo IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY modelo
               ORDER BY total DESC LIMIT ?""",
            (f"-{months * 31} days", top_n),
        ).fetchall()
        top_modelos = [r["modelo"] for r in top_modelos_rows]
        if not top_modelos:
            return {"operadores": [], "modelos": [], "cells": []}

        placeholders = ",".join("?" for _ in top_modelos)
        cells = [dict(r) for r in c.execute(
            f"""SELECT operador, modelo,
                       SUM(qtd) AS qty, COUNT(*) AS rows
                FROM production_rows
                WHERE operador IS NOT NULL AND modelo IN ({placeholders})
                  AND sheet_iso_date >= DATE('now', 'localtime', ?)
                GROUP BY operador, modelo""",
            (*top_modelos, f"-{months * 31} days"),
        ).fetchall()]
        operadores = sorted({c["operador"] for c in cells})
    return {"operadores": operadores, "modelos": top_modelos, "cells": cells}


# --------------------------------------------------------------------------
# Mix top-N
# --------------------------------------------------------------------------

def top_clientes(months: int = 12, limit: int = 15) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT cliente, SUM(qtd) AS qty,
                      COUNT(DISTINCT of) AS ofs,
                      COUNT(DISTINCT sheet_id) AS sheets
               FROM production_rows
               WHERE cliente IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY cliente ORDER BY qty DESC LIMIT ?""",
            (f"-{months * 31} days", limit),
        ).fetchall()]


def top_modelos(months: int = 12, limit: int = 15) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT modelo, SUM(qtd) AS qty,
                      COUNT(DISTINCT of) AS ofs,
                      COUNT(DISTINCT operador) AS operadores
               FROM production_rows
               WHERE modelo IS NOT NULL
                 AND sheet_iso_date >= DATE('now', 'localtime', ?)
               GROUP BY modelo ORDER BY qty DESC LIMIT ?""",
            (f"-{months * 31} days", limit),
        ).fetchall()]


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def alerts() -> dict:
    """Sheets needing attention.

    - Hard L3 violations (from dq_audit)
    - Sheets with high edit count
    - Sheets stuck in 'extracted' (unvalidated for >1 day)
    """
    import json as _json
    with conn() as c:
        # Pull last 100 sheets with dq_audit; filter Python side
        recent = c.execute(
            """SELECT id, captured_at, status, dq_audit, operador
               FROM sheets
               WHERE dq_audit IS NOT NULL
               ORDER BY captured_at DESC LIMIT 100"""
        ).fetchall()

        hard_violations: list[dict] = []
        for row in recent:
            try:
                audit = _json.loads(row["dq_audit"])
            except (TypeError, ValueError):
                continue
            n_err = audit.get("summary", {}).get("n_l3_errors", 0)
            if n_err > 0:
                hard_violations.append({
                    "id": row["id"],
                    "captured_at": row["captured_at"],
                    "status": row["status"],
                    "operador": row["operador"],
                    "n_errors": n_err,
                })

        high_edits = [dict(r) for r in c.execute(
            """SELECT s.id, s.captured_at, s.operador, s.status,
                      COUNT(e.id) AS n_edits
               FROM sheets s
               LEFT JOIN edits e ON e.sheet_id = s.id
               GROUP BY s.id
               HAVING n_edits >= 5
               ORDER BY n_edits DESC LIMIT 20"""
        ).fetchall()]

        stuck = [dict(r) for r in c.execute(
            """SELECT id, captured_at, operador, status
               FROM sheets
               WHERE status = 'extracted'
                 AND extracted_at < DATETIME('now', '-1 day')
               ORDER BY extracted_at ASC LIMIT 20"""
        ).fetchall()]

        top_edited_fields = [dict(r) for r in c.execute(
            """SELECT
                   substr(field_path, instr(field_path, '.') + 1) AS field,
                   COUNT(*) AS n
               FROM edits
               GROUP BY field ORDER BY n DESC LIMIT 10"""
        ).fetchall()]

    return {
        "hard_violations": hard_violations[:20],
        "high_edits": high_edits,
        "stuck": stuck,
        "top_edited_fields": top_edited_fields,
    }


# --------------------------------------------------------------------------
# Lead time (capture span per OF)
# --------------------------------------------------------------------------

def lead_time_per_of(limit: int = 20) -> list[dict]:
    """OFs with span between first and last sheet date that touched them."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT of,
                      MAX(cliente) AS cliente,
                      SUM(qtd) AS total_qty,
                      MIN(sheet_iso_date) AS first_day,
                      MAX(sheet_iso_date) AS last_day,
                      CAST(julianday(MAX(sheet_iso_date)) - julianday(MIN(sheet_iso_date)) AS INTEGER) AS days_span
               FROM production_rows
               WHERE of IS NOT NULL AND sheet_iso_date IS NOT NULL
               GROUP BY of
               HAVING days_span > 0
               ORDER BY days_span DESC LIMIT ?""",
            (limit,),
        ).fetchall()]


# --------------------------------------------------------------------------
# Round 29 Phase C — production overview (sector flow + cod_maquina breakdown)
# --------------------------------------------------------------------------

def _range_for_period(date_iso: str, period: str) -> tuple[str, str, str]:
    """Round 34: compute (date_from, date_to, label) for a period.

    period: 'day' | 'week' | 'month' | 'year'
    Returns inclusive ISO date range + Portuguese label for the title.
    """
    from datetime import date as _Date, timedelta
    try:
        d = _Date.fromisoformat(date_iso)
    except ValueError:
        return date_iso, date_iso, date_iso

    PT_MONTHS = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                 "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

    if period == "week":
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        label = f"Semana {monday.strftime('%d-%m')} a {sunday.strftime('%d-%m-%Y')}"
        return monday.isoformat(), sunday.isoformat(), label
    if period == "month":
        first = d.replace(day=1)
        if first.month == 12:
            last = first.replace(year=first.year + 1, month=1) - timedelta(days=1)
        else:
            last = first.replace(month=first.month + 1) - timedelta(days=1)
        label = f"{PT_MONTHS[d.month - 1]} {d.year}"
        return first.isoformat(), last.isoformat(), label
    if period == "year":
        return f"{d.year}-01-01", f"{d.year}-12-31", f"Ano {d.year}"
    # default: day
    label = f"Dia {d.strftime('%d-%m-%Y')}"
    return date_iso, date_iso, label


def production_overview(
    date: str | None = None,
    period: str = "day",
    kpi_defs: list[dict] | None = None,
) -> dict:
    """Production overview — sector flow + totals over a period.

    Round 34 added period support: 'day' | 'week' | 'month' | 'year'.

    `date` (YYYY-MM-DD) is the reference date inside the period.
    Default: today. The query runs ``BETWEEN date_from AND date_to``
    where the range is derived from period.

    Task C E3 — os KPIs derivados (col/h, min/col, toneladas, …) vêm de
    fórmulas editáveis (kpi_params); sem ficheiro de overrides o resultado
    é idêntico às fórmulas históricas. `kpi_defs` permite calcular com um
    conjunto candidato (preview do /admin/kpis) sem o gravar.
    """
    if not date:
        from datetime import date as _Date
        date = _Date.today().isoformat()
    date_from, date_to, period_label = _range_for_period(date, period)

    with conn() as c:
        rows = c.execute(
            """SELECT pr.sheet_id, pr.qtd, pr.comp_mm, pr.larg_mm, pr.esp,
                      pr.operador, pr.sheet_hours, pr.sheet_iso_date,
                      pr.of, pr.ov, pr.cliente, pr.modelo,
                      pr.lote, pr.lbase, pr.ltopo,
                      json_extract(s.sheet_data, '$.header.setor_maquina') AS setor_maquina
               FROM production_rows pr
               JOIN sheets s ON s.id = pr.sheet_id
               WHERE pr.sheet_iso_date BETWEEN ? AND ?""",
            (date_from, date_to),
        ).fetchall()

    sector_data: dict[str, dict] = {
        sector: {
            "name": sector,
            "qtd": 0,
            "n_sheets": set(),
            "n_rows": 0,
            "_sheet_hours": {},
            "machines": {},
        }
        for sector in PRODUCTION_SECTORS
    }

    all_sheet_ids: set[int] = set()
    all_operadores: set[str] = set()
    from app.cross_check.ref_watcher import get_watcher
    refs = get_watcher().get_refs()
    sector_to_codes = _sector_machine_codes(refs)
    cod_to_sector = _cod_machine_to_sector(refs)

    sheet_hours_global: dict[int, float] = {}
    total_qty = 0
    # R72 — split tonnage into 4 metrics:
    #   consumido  = aço saído do stock (rectangular sheet, user's formula)
    #   produzido  = aço final em colunas (trapezoidal, R38)
    #   waste      = consumido - produzido
    #   chapas     = ceil(qtd/npecas) por linha, somado
    total_consumido_kg = 0.0
    total_produzido_kg = 0.0
    total_waste_kg = 0.0
    total_chapas = 0

    for row in rows:
        sid = row["sheet_id"]
        qtd = row["qtd"] or 0
        cm = _derive_cod_maquina(row["setor_maquina"], refs)
        sector = cod_to_sector.get(cm, "")
        if not sector:
            sm = (row["setor_maquina"] or "").strip().upper()
            # R68: PRODUCTION_SECTORS now in Title Case (aligned with
            # registry); normalize to upper for case-insensitive contains.
            for s in PRODUCTION_SECTORS:
                s_up = s.upper()
                if s_up in sm or sm in s_up:
                    sector = s
                    break
        if not sector:
            continue

        bucket = sector_data[sector]
        bucket["qtd"] += qtd
        bucket["n_rows"] += 1
        bucket["n_sheets"].add(sid)
        bucket["_sheet_hours"][sid] = row["sheet_hours"] or 0

        if cm and cm in sector_to_codes.get(sector, []):
            mb = bucket["machines"].setdefault(cm, {
                "code": cm, "qtd": 0, "n_rows": 0,
                "n_sheets": set(), "_sheet_hours": {},
            })
            mb["qtd"] += qtd
            mb["n_rows"] += 1
            mb["n_sheets"].add(sid)
            mb["_sheet_hours"][sid] = row["sheet_hours"] or 0

        all_sheet_ids.add(sid)
        if row["operador"]:
            all_operadores.add(row["operador"])
        sheet_hours_global[sid] = row["sheet_hours"] or 0
        total_qty += qtd

        if not qtd:
            continue

        weights = calculate_row_weights(dict(row), refs=refs)
        if not weights.direct_consumption:
            continue
        if weights.peso_consumido_kg is not None:
            total_consumido_kg += weights.peso_consumido_kg
        if weights.peso_produzido_kg is not None:
            total_produzido_kg += weights.peso_produzido_kg
        if weights.desperdicio_kg is not None:
            total_waste_kg += weights.desperdicio_kg
        if weights.n_chapas is not None:
            total_chapas += weights.n_chapas

    # Task C E3 — derivados via fórmulas editáveis. As expressões default
    # reproduzem as fórmulas históricas (R29/R34/R72) byte-a-byte; ver
    # kpi_params.DEFAULT_KPIS. Fórmula partida → fallback à default.
    if kpi_defs is None:
        kpi_defs = kpi_params.get_kpis()

    sectors_out = []
    for sector in PRODUCTION_SECTORS:
        b = sector_data[sector]
        hours = sum(b["_sheet_hours"].values())
        sector_kpis = kpi_params.compute_scope_kpis("sector", {
            "qtd": b["qtd"], "horas": hours,
            "n_folhas": len(b["n_sheets"]), "n_linhas": b["n_rows"],
        }, kpi_defs)
        machines_out = []
        for cm in sector_to_codes.get(sector, []):
            mb = b["machines"].get(cm)
            if mb:
                m_hours = sum(mb["_sheet_hours"].values())
                m_out = {
                    "code": cm,
                    "qtd": mb["qtd"],
                    "n_sheets": len(mb["n_sheets"]),
                    "n_rows": mb["n_rows"],
                    "hours": round(m_hours, 1),
                    "col_per_h": None,
                    "min_per_col": None,
                }
                m_kpis = kpi_params.compute_scope_kpis("machine", {
                    "qtd": mb["qtd"], "horas": m_hours,
                    "n_folhas": len(mb["n_sheets"]), "n_linhas": mb["n_rows"],
                }, kpi_defs)
                for kid, val in m_kpis.items():
                    if kid not in ("code", "qtd", "n_sheets", "n_rows", "hours"):
                        m_out[kid] = val
                machines_out.append(m_out)
            else:
                machines_out.append({
                    "code": cm, "qtd": None, "n_sheets": 0, "n_rows": 0,
                    "hours": None, "col_per_h": None, "min_per_col": None,
                })
        has_data = b["qtd"] > 0
        s_out = {
            "name": sector,
            "has_data": has_data,
            "qtd": b["qtd"] if has_data else None,
            "n_sheets": len(b["n_sheets"]),
            "n_rows": b["n_rows"],
            "hours": round(hours, 1) if has_data else None,
            "col_per_h": None,
            "min_per_col": None,
            "machines": machines_out,
        }
        if has_data:
            # O gate has_data → None mantém o comportamento histórico
            # (setor sem produção mostra '—', não 0).
            for kid, val in sector_kpis.items():
                if kid not in ("name", "has_data", "qtd", "n_sheets",
                               "n_rows", "hours", "machines"):
                    s_out[kid] = val
        sectors_out.append(s_out)

    total_hours = sum(sheet_hours_global.values())
    n_ops = len(all_operadores)
    totals_vars = {
        "qtd": total_qty,
        "horas": total_hours,
        "kg_consumido": total_consumido_kg,
        "kg_produzido": total_produzido_kg,
        "kg_desperdicio": total_waste_kg,
        "chapas": total_chapas,
        "n_folhas": len(all_sheet_ids),
        "n_operadores": n_ops,
    }
    totals_kpis = kpi_params.compute_scope_kpis("totals", totals_vars, kpi_defs)
    totals = {
        "colunas": total_qty,
        "hours": round(total_hours, 1),
        "n_sheets": len(all_sheet_ids),
        "n_operadores": n_ops,
    }
    for kid, val in totals_kpis.items():
        if kid not in totals:
            totals[kid] = val

    # Cartões para render genérico (metas são capacidade nova do E3).
    kpi_cards = []
    for k in kpi_defs:
        if "totals" not in (k.get("scopes") or []):
            continue
        val = totals_kpis.get(k["id"])
        target = k.get("target")
        meets = None
        if target is not None and val is not None:
            meets = (val >= target) if k.get("direction") == "higher" else (val <= target)
        kpi_cards.append({
            "id": k["id"], "label": k.get("label") or k["id"],
            "unit": k.get("unit") or "", "value": val,
            "target": target, "direction": k.get("direction"),
            "meets_target": meets,
        })

    return {
        "date": date,
        "period": period,
        "period_label": period_label,
        "date_from": date_from,
        "date_to": date_to,
        "totals": totals,
        "sectors": sectors_out,
        "kpi_cards": kpi_cards,
        "kpi_variables": {"totals": totals_vars},
    }
