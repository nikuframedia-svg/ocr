"""Error attractors — where human corrections concentrate.

An *attractor* is a field, operador or template that pulls a
disproportionate share of human corrections. The /learnings page uses
these both as a fixed dashboard and as context for the LLM assistant.

Two distinct signals per attractor:

- ``cc_error_rate`` — the **real** OCR error rate, in [0, 1]: NO_MATCH
  cells over (MATCH + NO_MATCH) cells, taken from the cross-check (the
  per-cell comparison of the OCR output against the SAP refs). This is
  what "taxa de erro" should mean — typically well under 5%.
- ``review_rate`` — the share of sheets in that group that needed at
  least one human correction. A *review-load* signal, NOT an error rate:
  a field that almost always gets a manual touch reads as 1.0 here even
  when the OCR was right. Kept because it is still useful, but it must
  never be presented as "% wrong".

``correction_count`` is the raw human-edit volume (can far exceed the
sheet count — many edits per sheet).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.cross_check import storage as cc_storage
from app.web import db

_ROW_FIELD_RE = re.compile(r"^rows\[\d+\]\.(.+)$")


def _norm_field_path(field_path: str) -> str:
    """``rows[3].modelo`` → ``rows[].modelo``; header/footer kept as-is."""
    m = _ROW_FIELD_RE.match(field_path or "")
    if m:
        return f"rows[].{m.group(1)}"
    return field_path or "?"


def _severity(cc_error_rate: float | None, review_rate: float) -> str:
    """Severity from the real cross-check error rate when available;
    otherwise falls back to the review-load rate.

    The cross-check scale is real OCR error (NO_MATCH share), so the
    thresholds are tight: ≥15% alta · ≥5% média · resto baixa.
    """
    if cc_error_rate is not None:
        if cc_error_rate >= 0.15:
            return "alta"
        if cc_error_rate >= 0.05:
            return "media"
        return "baixa"
    if review_rate >= 0.5:
        return "alta"
    if review_rate >= 0.2:
        return "media"
    return "baixa"


def _human_edits() -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.sheet_id, e.field_path, e.old_value, e.new_value, "
            "       s.operador AS sheet_operador, "
            "       json_extract(s.sheet_data, '$.template_name') AS template_name "
            "FROM edits e LEFT JOIN sheets s ON s.id = e.sheet_id "
            "WHERE e.source = 'human'"
        ).fetchall()
    return [dict(r) for r in rows]


def _populations() -> tuple[int, dict[str, int], dict[str, int]]:
    """(total sheets, sheets-per-template, sheets-per-operador) — the
    denominators for the review-rate ratio."""
    with db.conn() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM sheets "
            "WHERE status IN ('extracted', 'validated')"
        ).fetchone()["n"]
        tpl_rows = c.execute(
            "SELECT json_extract(sheet_data, '$.template_name') tpl, COUNT(*) n "
            "FROM sheets WHERE status IN ('extracted', 'validated') GROUP BY tpl"
        ).fetchall()
        op_rows = c.execute(
            "SELECT operador, COUNT(*) n FROM sheets "
            "WHERE operador IS NOT NULL GROUP BY operador"
        ).fetchall()
    by_tpl = {(r["tpl"] or "").strip(): r["n"] for r in tpl_rows if r["tpl"]}
    by_op = {(r["operador"] or "").strip().upper(): r["n"] for r in op_rows}
    return total, by_tpl, by_op


def _sheet_templates() -> dict[int, str]:
    """sheet_id → template_name — used to bucket cross-check rows (the
    per-sheet cross-check JSON does not carry the template name)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, json_extract(sheet_data, '$.template_name') tpl FROM sheets"
        ).fetchall()
    return {r["id"]: (r["tpl"] or "").strip() for r in rows}


def _cross_check_field_stats() -> tuple[dict, dict, dict]:
    """Real MATCH/NO_MATCH counts from the per-sheet cross-check JSONs.

    Returns ``(by_field, by_template, by_operador)`` — each a dict of
    label → ``{"match": int, "no_match": int}``. Cells with status ``NA``
    are ignored (no ref to compare against). Degrades to empty dicts if
    the cross-check directory is unavailable.
    """
    by_field: dict[str, dict[str, int]] = defaultdict(
        lambda: {"match": 0, "no_match": 0})
    by_template: dict[str, dict[str, int]] = defaultdict(
        lambda: {"match": 0, "no_match": 0})
    by_operador: dict[str, dict[str, int]] = defaultdict(
        lambda: {"match": 0, "no_match": 0})
    try:
        sheets = cc_storage.iter_sheet_cross_checks()
    except OSError:
        return {}, {}, {}
    tpl_by_id = _sheet_templates()
    for sh in sheets:
        tpl = tpl_by_id.get(sh.get("sheet_id"), "")
        op = (sh.get("operador") or "").strip().upper()
        for row in sh.get("rows", []) or []:
            for fld, info in (row.get("fields") or {}).items():
                status = (info or {}).get("status")
                if status not in ("MATCH", "NO_MATCH"):
                    continue
                key = "match" if status == "MATCH" else "no_match"
                by_field[f"rows[].{fld}"][key] += 1
                if tpl:
                    by_template[tpl][key] += 1
                if op:
                    by_operador[op][key] += 1
    return dict(by_field), dict(by_template), dict(by_operador)


def _top_confusions(edits: list[dict]) -> list[dict]:
    counter: Counter[tuple[str, str]] = Counter(
        ((e["old_value"] or "").strip(), (e["new_value"] or "").strip())
        for e in edits
        if (e["old_value"] or "").strip() != (e["new_value"] or "").strip()
    )
    return [
        {"old": old, "new": new, "n": n}
        for (old, new), n in counter.most_common(5)
    ]


def _rate(edits: list[dict], population: int) -> float:
    """Share of distinct sheets touched by a human edit, in [0, 1]."""
    if population <= 0:
        return 0.0
    distinct_sheets = len({e["sheet_id"] for e in edits})
    return round(min(1.0, distinct_sheets / population), 4)


def compute_attractors(top_n: int = 10) -> list[dict]:
    """Rank attractors by human-correction volume. Each entry carries the
    real cross-check error rate (``cc_error_rate``, when there is
    cross-check data for that group), a ``review_rate`` (share of sheets
    touched) and a ``severity``."""
    edits = _human_edits()
    total_sheets, sheets_by_tpl, sheets_by_op = _populations()
    cc_by_field, cc_by_tpl, cc_by_op = _cross_check_field_stats()

    by_field: dict[str, list[dict]] = defaultdict(list)
    by_operador: dict[str, list[dict]] = defaultdict(list)
    by_template: dict[str, list[dict]] = defaultdict(list)
    for e in edits:
        by_field[_norm_field_path(e["field_path"])].append(e)
        op = (e["sheet_operador"] or "").strip().upper()
        if op:
            by_operador[op].append(e)
        tpl = (e["template_name"] or "").strip()
        if tpl:
            by_template[tpl].append(e)

    attractors: list[dict] = []

    def _add(scope: str, label: str, es: list[dict],
             population: int, cc: dict) -> None:
        review_rate = _rate(es, population)
        stat = cc.get(label)
        if stat:
            cc_match, cc_no_match = stat["match"], stat["no_match"]
            cc_total = cc_match + cc_no_match
            cc_error_rate = round(cc_no_match / cc_total, 4) if cc_total else None
        else:
            cc_match = cc_no_match = cc_total = 0
            cc_error_rate = None
        attractors.append({
            "scope": scope,
            "label": label,
            "correction_count": len(es),
            "review_rate": review_rate,
            "cc_match": cc_match,
            "cc_no_match": cc_no_match,
            "cc_total": cc_total,
            "cc_error_rate": cc_error_rate,
            "severity": _severity(cc_error_rate, review_rate),
            "top_confusions": _top_confusions(es),
        })

    for fld, es in by_field.items():
        _add("campo", fld, es, total_sheets, cc_by_field)
    for op, es in by_operador.items():
        _add("operador", op, es, sheets_by_op.get(op, 0), cc_by_op)
    for tpl, es in by_template.items():
        _add("template", tpl, es, sheets_by_tpl.get(tpl, 0), cc_by_tpl)

    attractors.sort(key=lambda a: a["correction_count"], reverse=True)
    return attractors[:top_n]
