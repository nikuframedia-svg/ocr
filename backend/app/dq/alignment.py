"""R135 — post-OCR column-alignment guard.

VLMs lose column tracking when a kanban cell is blank and emit a value under
the WRONG key (e.g. a priority code lands in OF, or a 6-digit OF lands in PRI).
Parsing is key-based, so this is not a positional shift — it's a wrong-key
emission that the prompt alone can't fully prevent.

This guard is deterministic and ref-free: for the documented PRI<->OF confusion
it moves a value to the right column ONLY when the move is unambiguous (the
value matches exactly one EMPTY sibling column's format and violates the format
of the column it currently sits in). Every move — and every unfixable format
violation — is recorded as a flag for the audit trail. The raw OCR is preserved
upstream; only the editable `current` copy is corrected.
"""
from __future__ import annotations

import re
from copy import deepcopy

_OF_RE = re.compile(r"^\d{6}$")
# priority: small int (1, 2, 100), letter+digits (C7, P4), optional REP. prefix
_PRI_RE = re.compile(r"^(?:REP\.?\s*)?[A-Za-z]{0,2}\d{1,3}$")


def _is_of(v: str) -> bool:
    return bool(_OF_RE.match(v))


def _is_pri(v: str) -> bool:
    return bool(v) and not _is_of(v) and bool(_PRI_RE.match(v))


def check_and_fix_alignment(
    rows: list, row_fields: tuple[str, ...]
) -> tuple[list, list[dict]]:
    """Return ``(fixed_rows, flags)``.

    ``fixed_rows`` is a deep copy of ``rows`` with unambiguous PRI<->OF swaps
    applied. ``flags`` lists every applied move plus every detected-but-unfixed
    format violation, for ``dq_audit['violations']``.
    """
    has_of = "of" in row_fields
    has_pri = "pri" in row_fields
    fixed = deepcopy(rows)
    flags: list[dict] = []
    if not has_of:
        return fixed, flags
    for i, row in enumerate(fixed):
        if not isinstance(row, dict):
            continue
        of_v = str(row.get("of") or "").strip()
        pri_v = str(row.get("pri") or "").strip()

        # Priority-looking code sitting in OF while PRI is empty → move to PRI.
        if has_pri and of_v and not pri_v and _is_pri(of_v):
            row["pri"], row["of"] = of_v, ""
            flags.append({
                "row_index": i, "from": "of", "to": "pri", "value": of_v,
                "reason": "valor curto tipo prioridade no campo OF (OF tem 6 dígitos)",
            })
            continue

        # 6-digit OF sitting in PRI while OF is empty → move to OF.
        if has_pri and pri_v and not of_v and _is_of(pri_v):
            row["of"], row["pri"] = pri_v, ""
            flags.append({
                "row_index": i, "from": "pri", "to": "of", "value": pri_v,
                "reason": "valor de 6 dígitos tipo OF no campo PRI",
            })
            continue

        # Detection-only: OF filled but not 6 digits and not an obvious swap.
        if of_v and not _is_of(of_v):
            flags.append({
                "row_index": i, "field": "of", "value": of_v,
                "reason": "OF não tem 6 dígitos — verificar alinhamento de colunas",
            })
    return fixed, flags


__all__ = ["check_and_fix_alignment"]
