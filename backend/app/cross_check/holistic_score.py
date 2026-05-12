"""Round 57 — Shared holistic field-match scoring.

Used by both DQ snap (`snap_of` Stage holistic) and cross-check engine
(`_best_plan_entry`). Single source of truth so both layers agree on
"which plan entry best fits this row?".

Principle (user explicit, R51 + R57):
  "Todas as células têm o mesmo peso, a linha que ganha é a com maior
   percentagem de match."

9 fields scored, +1 each (max score = 9):

| # | Field | Match rule |
|---|---|---|
| 1 | cliente | exact upper (alias-resolved) |
| 2 | ov | exact string |
| 3 | modelo | substring (operator upper ⊂ plan.designacao upper) |
| 4 | comp_mm | ±100mm |
| 5 | lbase | ±30mm |
| 6 | ltopo | ±30mm |
| 7 | esp | ±0.05 |
| 8 | larg | ±20mm vs SAP[lote].larg |
| 9 | material | plan.material == SAP[lote].desc.split()[0] |

Tie-breaking helper `_score_high_signal` returns count of just the
high-discriminant subset (fields 1-3) — used by snap_of to pick the
strongest candidate when total scores are equal.

Public API:
    score_entry(entry, row, refs) -> int (0-9)
    score_high_signal(entry, row, refs) -> int (0-3)
"""
from __future__ import annotations

from typing import Any


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def score_entry(
    entry: dict,
    row: dict,
    refs: dict,
    cliente_aliases: dict[str, str] | None = None,
) -> int:
    """Holistic 0-9 match score for one plan entry against one row.

    `entry` is a plan_colunas entry dict (keys: cliente, ov, designacao,
    comp, lbase, ltopo, esp, material, fechado, ...).
    `row` is a kanban row dict (operator's data after snap).
    `refs` is the full refs dict — needed to look up SAP[lote] for
    fields 8/9.
    """
    op_cli_raw = (row.get("cliente") or "").strip().upper()
    if cliente_aliases:
        op_cli = cliente_aliases.get(op_cli_raw, op_cli_raw)
    else:
        op_cli = op_cli_raw
    op_ov = str(row.get("ov") or "").strip()
    op_mod = (row.get("modelo") or "").strip().upper()
    op_comp = _num(row.get("comp_mm"))
    op_lb = _num(row.get("lbase"))
    op_lt = _num(row.get("ltopo"))
    op_esp = _num(row.get("esp"))
    op_larg = _num(row.get("larg_mm"))
    op_lote = (row.get("lote") or "").strip().upper()

    sap_full = refs.get("lotes_sap_full", {}) if refs else {}
    sap_e = sap_full.get(op_lote) if op_lote else None
    sap_larg = _num(sap_e.get("larg")) if sap_e else None
    sap_desc_first = ""
    if sap_e:
        d = (sap_e.get("desc") or "").strip().upper()
        sap_desc_first = d.split()[0] if d.split() else ""

    s = 0
    # Field 1 — cliente
    if op_cli and op_cli == (entry.get("cliente") or "").strip().upper():
        s += 1
    # Field 2 — ov
    if op_ov and op_ov == str(entry.get("ov") or "").strip():
        s += 1
    # Field 3 — modelo substring
    des = (entry.get("designacao") or "").upper()
    if op_mod and len(op_mod) >= 4 and op_mod in des:
        s += 1
    # Field 4 — comp ±100mm
    plan_comp = _num(entry.get("comp"))
    if op_comp is not None and plan_comp is not None and abs(op_comp - plan_comp) <= 100:
        s += 1
    # Field 5 — lbase ±30mm
    plan_lb = _num(entry.get("lbase"))
    if op_lb is not None and plan_lb is not None and abs(op_lb - plan_lb) <= 30:
        s += 1
    # Field 6 — ltopo ±30mm
    plan_lt = _num(entry.get("ltopo"))
    if op_lt is not None and plan_lt is not None and abs(op_lt - plan_lt) <= 30:
        s += 1
    # Field 7 — esp ±0.05
    plan_esp = _num(entry.get("esp"))
    if op_esp is not None and plan_esp is not None and abs(op_esp - plan_esp) <= 0.05:
        s += 1
    # Field 8 — larg via SAP[lote].larg ±20mm
    if sap_larg is not None and op_larg is not None and abs(op_larg - sap_larg) <= 20:
        s += 1
    # Field 9 — material consistency
    plan_mat = (entry.get("material") or "").strip().upper()
    if plan_mat and sap_desc_first and plan_mat == sap_desc_first:
        s += 1
    return s


def score_high_signal(
    entry: dict,
    row: dict,
    cliente_aliases: dict[str, str] | None = None,
) -> int:
    """Tie-breaker score (0-3) — counts only the 3 high-discriminant
    fields: cliente exact + ov exact + modelo substring.

    Used by snap_of to pick the strongest candidate in a tied top set.
    These 3 fields uniquely identify a plan row better than dim
    tolerances which match many entries.
    """
    op_cli_raw = (row.get("cliente") or "").strip().upper()
    if cliente_aliases:
        op_cli = cliente_aliases.get(op_cli_raw, op_cli_raw)
    else:
        op_cli = op_cli_raw
    op_ov = str(row.get("ov") or "").strip()
    op_mod = (row.get("modelo") or "").strip().upper()

    s = 0
    if op_cli and op_cli == (entry.get("cliente") or "").strip().upper():
        s += 1
    if op_ov and op_ov == str(entry.get("ov") or "").strip():
        s += 1
    des = (entry.get("designacao") or "").upper()
    if op_mod and len(op_mod) >= 4 and op_mod in des:
        s += 1
    return s


__all__ = ["score_entry", "score_high_signal"]
