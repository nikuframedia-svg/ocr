"""Error attractors — where human corrections concentrate.

An *attractor* is a field, operador or template that pulls a
disproportionate share of human corrections. The /learnings LLM tab uses
these both as a fixed dashboard and as context for the assistant.
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
    if error_rate >= 0.15:
        return "alta"
    if error_rate >= 0.05:
        return "media"
    return "baixa"


def _human_edits() -> list[dict]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.field_path, e.old_value, e.new_value, "
            "       s.operador AS sheet_operador, "
            "       json_extract(s.sheet_data, '$.template_name') AS template_name "
            "FROM edits e LEFT JOIN sheets s ON s.id = e.sheet_id "
            "WHERE e.source = 'human'"
        ).fetchall()
    return [dict(r) for r in rows]


def _denominators() -> tuple[int, int, dict[str, int]]:
    """(total production rows, total sheets, rows-per-operador)."""
    with db.conn() as c:
        total_rows = c.execute(
            "SELECT COUNT(*) n FROM production_rows"
        ).fetchone()["n"]
        total_sheets = c.execute(
            "SELECT COUNT(*) n FROM sheets "
            "WHERE status IN ('extracted', 'validated')"
        ).fetchone()["n"]
        op_rows = c.execute(
            "SELECT operador, COUNT(*) n FROM production_rows "
            "WHERE operador IS NOT NULL GROUP BY operador"
        ).fetchall()
    rows_by_op = {
        (r["operador"] or "").strip().upper(): r["n"] for r in op_rows
    }
    return total_rows, total_sheets, rows_by_op


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


def compute_attractors(top_n: int = 10) -> list[dict]:
    """Rank attractors by correction volume. Each entry carries an
    ``error_rate`` (corrections / observations) and a ``severity``."""
    edits = _human_edits()
    total_rows, total_sheets, rows_by_op = _denominators()

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

    for fld, es in by_field.items():
        denom = total_rows if fld.startswith("rows[].") else total_sheets
        denom = denom or len(es)
        rate = round(len(es) / denom, 4) if denom else 0.0
        attractors.append({
            "scope": "campo",
            "label": fld,
            "correction_count": len(es),
            "denominator": denom,
            "error_rate": rate,
            "severity": _severity(rate),
            "top_confusions": _top_confusions(es),
        })

    for op, es in by_operador.items():
        denom = rows_by_op.get(op) or len(es)
        rate = round(len(es) / denom, 4) if denom else 0.0
        attractors.append({
            "scope": "operador",
            "label": op,
            "correction_count": len(es),
            "denominator": denom,
            "error_rate": rate,
            "severity": _severity(rate),
            "top_confusions": _top_confusions(es),
        })

    for tpl, es in by_template.items():
        denom = total_rows or len(es)
        rate = round(len(es) / denom, 4) if denom else 0.0
        attractors.append({
            "scope": "template",
            "label": tpl,
            "correction_count": len(es),
            "denominator": denom,
            "error_rate": rate,
            "severity": _severity(rate),
            "top_confusions": _top_confusions(es),
        })

    attractors.sort(key=lambda a: a["correction_count"], reverse=True)
    return attractors[:top_n]
