"""Error attractors — where human corrections concentrate.

An *attractor* is a field, operador or template that pulls a
disproportionate share of human corrections. The /learnings LLM tab uses
these both as a fixed dashboard and as context for the assistant.

``error_rate`` is a true ratio in [0, 1]: the share of sheets that needed
at least one human correction in that group. ``correction_count`` is the
raw edit volume (can far exceed the sheet count — many edits per sheet).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from app.web import db

_ROW_FIELD_RE = re.compile(r"^rows\[\d+\]\.(.+)$")


def _norm_field_path(field_path: str) -> str:
    """``rows[3].modelo`` → ``rows[].modelo``; header/footer kept as-is."""
    m = _ROW_FIELD_RE.match(field_path or "")
    if m:
        return f"rows[].{m.group(1)}"
    return field_path or "?"


def _severity(error_rate: float) -> str:
    """Severity from a real [0,1] error rate (share of sheets touched)."""
    if error_rate >= 0.5:
        return "alta"
    if error_rate >= 0.2:
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
    denominators for the error-rate ratio."""
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
    """Share of distinct sheets touched, in [0, 1]."""
    if population <= 0:
        return 0.0
    distinct_sheets = len({e["sheet_id"] for e in edits})
    return round(min(1.0, distinct_sheets / population), 4)


def compute_attractors(top_n: int = 10) -> list[dict]:
    """Rank attractors by correction volume. Each entry carries an
    ``error_rate`` (share of sheets touched, [0,1]) and a ``severity``."""
    edits = _human_edits()
    total_sheets, sheets_by_tpl, sheets_by_op = _populations()

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

    def _add(scope: str, label: str, es: list[dict], population: int) -> None:
        rate = _rate(es, population)
        attractors.append({
            "scope": scope,
            "label": label,
            "correction_count": len(es),
            "error_rate": rate,
            "severity": _severity(rate),
            "top_confusions": _top_confusions(es),
        })

    for fld, es in by_field.items():
        _add("campo", fld, es, total_sheets)
    for op, es in by_operador.items():
        _add("operador", op, es, sheets_by_op.get(op, 0))
    for tpl, es in by_template.items():
        _add("template", tpl, es, sheets_by_tpl.get(tpl, 0))

    attractors.sort(key=lambda a: a["correction_count"], reverse=True)
    return attractors[:top_n]
