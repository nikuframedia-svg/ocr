"""R253/F2 — diff célula-a-célula produção vs sombra (lógica PARTILHADA).

Extraída do scripts/diag/shadow_agreement.py para servir também a rota de
triagem /sheet/<id>/shadow-view (passo 2 do procedimento de flip): a régua
que decide "diverge" tem de ser UMA — a mesma no CSV offline e no ecrã.
"""
from __future__ import annotations

from typing import Any

CROSSABLE = ("of", "ov", "cliente", "modelo", "lote",
             "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo")
_IDENTITY = ("of", "ov", "cliente")
_DIMS = ("comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo")


def _severity(field: str) -> str:
    """identidade > dims > resto — a identidade tem consequência de
    auditoria EN1090/ISO9001 se for para produção."""
    if field in _IDENTITY:
        return "identidade"
    if field in _DIMS:
        return "dimensao"
    return "outro"


def diff_prod_vs_shadow(prod: dict, shadow: dict) -> dict[str, Any]:
    """Compara os campos cruzáveis linha a linha. Devolve
    {"n_cells", "diffs": [...], "status_flips": [...]} — cada diff com
    row/field/valores/status/confiança/razão da sombra + severidade."""
    p_rows = (prod or {}).get("rows") or []
    s_rows = (shadow or {}).get("rows") or []
    n_cells = 0
    diffs: list[dict] = []
    status_flips: list[dict] = []
    for i in range(min(len(p_rows), len(s_rows))):
        pf = (p_rows[i] or {}).get("fields") or {}
        sf = (s_rows[i] or {}).get("fields") or {}
        for field in CROSSABLE:
            pc, sc = pf.get(field) or {}, sf.get(field) or {}
            pv = str(pc.get("value") or pc.get("ref") or "").strip()
            sv = str(sc.get("value") or "").strip()
            if not pv and not sv:
                continue
            n_cells += 1
            ps = str(pc.get("status") or "")
            ss = str(sc.get("status") or "")
            if pv.upper() != sv.upper():
                diffs.append({
                    "row": i, "field": field,
                    "severity": _severity(field),
                    "producao": pv, "sombra": sv,
                    "status_prod": ps, "status_sombra": ss,
                    "sombra_conf": sc.get("decision_confidence"),
                    "sombra_reason": sc.get("decision_reason") or "",
                })
            elif ps != ss:
                status_flips.append({
                    "row": i, "field": field,
                    "de": ps, "para": ss,
                })
    order = {"identidade": 0, "dimensao": 1, "outro": 2}
    diffs.sort(key=lambda d: (order[d["severity"]], d["row"]))
    return {"n_cells": n_cells, "diffs": diffs,
            "status_flips": status_flips}
