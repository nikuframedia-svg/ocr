"""Classify a learning proposal's risk.

``low``      — purely additive / maps to a known-good value. Auto-applies.
``behavior`` — changes a decision rule (confusion weights, snap rules) or
               maps to an unknown target. Held in quarantine for review.
"""
from __future__ import annotations

from app.learning.models import Proposal

_ALWAYS_BEHAVIOR = {"confusion_pair", "snap_rule"}
_ALWAYS_LOW = {"fewshot_example", "observed_value"}


def classify_risk(proposal: Proposal) -> str:
    if proposal.kind in _ALWAYS_BEHAVIOR:
        return "behavior"
    if proposal.kind in _ALWAYS_LOW:
        return "low"
    if proposal.kind.endswith("_alias"):
        return _alias_risk(proposal)
    return "behavior"


def _alias_risk(proposal: Proposal) -> str:
    """An alias is low-risk only when its target is an already-known
    canonical value — i.e. we are mapping a misread onto a real entry,
    not inventing one."""
    to = (proposal.payload.get("to") or "").upper().strip()
    if not to:
        return "behavior"
    try:
        known = _known_values(proposal.kind)
    except Exception:
        return "behavior"
    return "low" if to in known else "behavior"


def _known_values(kind: str) -> set[str]:
    from app.dq import snap

    if kind == "cliente_alias":
        vals: set[str] = set(snap._CLIENTES) | set(snap._CLIENTES_PLAN)
    elif kind == "modelo_alias":
        vals = set(snap._MODELOS_FULL)
    else:  # operador_alias — canonical list lives in refs, not snap
        vals = set()
    return {v.upper().strip() for v in vals}
