"""Lightweight dataclasses for the learning engine."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as _field


@dataclass(frozen=True)
class EditDiff:
    """A single human correction mined from the ``edits`` table."""

    edit_id: int
    sheet_id: int
    field_path: str       # e.g. "rows[3].modelo"
    field: str            # leaf field name, e.g. "modelo"
    old: str              # OCR value (wrong)
    new: str              # human-corrected value (right)


@dataclass
class Proposal:
    """A candidate learning, before it is persisted to the ``learnings``
    table. ``dedup_key`` makes mining idempotent — re-mining the same
    corrections upserts the same row instead of duplicating it."""

    kind: str                          # cliente_alias|operador_alias|modelo_alias
                                       # |confusion_pair|fewshot_example
                                       # |observed_value|snap_rule
    payload: dict
    dedup_key: str
    field: str | None = None
    template_name: str | None = None
    origin: str = "edits_mining"       # edits_mining|validated_sheets|llm|manual
    evidence_count: int = 1
    evidence_refs: list = _field(default_factory=list)
