"""JSON schema for guided decoding via llguidance / xgrammar.

Distinct from the permissive :class:`KanbanExtraction` (which captures
whatever the model emits so the benchmark can flag errors at compare
time), this module emits the **grammar** that the inference engine's
sampler is constrained by — values that violate the regex are simply
not in the token-level decision space.

It is *syntactic* enforcement only (per-field regex / type / enum), not
schema-as-system-prompt. The briefing's warning against "forçar JSON
schema durante raciocínio" applies to forcing the model to internally
reason about a schema; we are doing constrained sampling, which has
been measured to give large quality wins on industrial OCR with closed
vocabularies (the Phase 4 lever).

The schema is built from regex patterns tuned against the 19 sample
sheets from Metalogalva (Apr 2026). Patterns are intentionally a bit
looser than what we have observed, to keep room for unseen variants
(new lotes, new clientes). Tighten in Phase F as the lexicon grows.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# ---------------------------------------------------------------------------
# Per-field regex patterns. Every value-string field accepts ``""`` as well as
# its proper pattern — empty cells in the Kanban are common.
# ---------------------------------------------------------------------------

_DATE = r"^\d{1,2}-\d{1,2}-\d{4}$|^$"
_OF = r"^\d{6}$|^$"
_OV = r"^\d{6,8}$|^$"
_LOTE = r"^[HM]\d{2}B\d{4,5}$|^$"
_NUMERIC_SHORT = r"^\d{1,4}$|^$"
_NUMERIC_DIM = r"^\d{2,5}$|^$"
_NUMERIC_LARGE = r"^\d{1,5}$|^$"
_ESP = r"^\d{1,2}(?:[.,]\d)?$|^$"
_HORAS = r"^\d{1,2}(?:[: ]?\d{0,2})?\s?[hH]?$|^\d{1,2}[.,]\d{1,2}$|^$"
_CONI = r"^(?:OCT\.?|TORRES|T|\d{1,3})$|^$"
_PRI = r"^(?:[A-Z]?\d{1,3}|P\.?\d|REP\.?\s?C?\d+)$|^$"

# Free-form text fields: alphanumeric, common punctuation/accents. Not
# constrained to a closed list because the model has to handle unseen
# clients/operators on production sheets. The pattern shape is loose
# enough not to fight the model on legitimate strings, tight enough to
# block hallucinations like full sentences.
_OPERADOR = r"^[A-ZÀ-Ý][A-ZÀ-Ý .]{1,40}$|^$"
_SETOR = r"^[A-Z][A-Z0-9.\- ]{1,40}$|^$"  # R129 — aceita dígitos (MTG2, HPE32) e ponto (PAV.4)
_CLIENTE = r"^[A-Z0-9][A-Z0-9 .,'ÉÊÇª-]{0,40}$|^$"
# Allow lowercase i/v/iv suffixes ("CLC8F07Ri-V", "CLC8F06Div"), Ç, °, ª.
_MODELO = r"^[A-Z0-9][A-Z0-9 .\-/°ªivÇ+]{0,60}$|^$"


def _string(pattern: str) -> dict[str, Any]:
    return {"type": "string", "pattern": pattern}


_TURNO = r"^(M|R|XM|T)?$"  # R129 — só no kanban Acabamento MTG2

_HEADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operador", "n_operador", "setor_maquina", "data"],
    "properties": {
        "operador": _string(_OPERADOR),
        "n_operador": _string(_NUMERIC_SHORT),
        "setor_maquina": _string(_SETOR),
        # cod_maquina (M0xx) — optional for backwards-compatibility with
        # extractions captured before the field was added. Empty string
        # allowed for kanbans where the code isn't printed.
        "cod_maquina": {"type": "string", "pattern": r"^M\d{3}$|^$"},
        "data": _string(_DATE),
        # R129 — turno: M/R/XM/T (Acabamento MTG2). Não em `required` —
        # vazio para todos os outros templates.
        "turno": _string(_TURNO),
    },
}

_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "pri",
        "cliente",
        "ov",
        "of",
        "modelo",
        "qtd",
        "comp_mm",
        "larg_mm",
        "lote",
        "coni",
        "esp",
        "lbase",
        "ltopo",
    ],
    "properties": {
        "pri": _string(_PRI),
        "cliente": _string(_CLIENTE),
        "ov": _string(_OV),
        "of": _string(_OF),
        "modelo": _string(_MODELO),
        "qtd": _string(_NUMERIC_SHORT),
        "comp_mm": _string(_NUMERIC_DIM),
        "larg_mm": _string(_NUMERIC_DIM),
        "lote": _string(_LOTE),
        "coni": _string(_CONI),
        "esp": _string(_ESP),
        "lbase": _string(_NUMERIC_DIM),
        "ltopo": _string(_NUMERIC_DIM),
        # R128 — kanban LASER (dbase/dtopo). Não vão a `required` —
        # só são preenchidos para o template laser; mantemos
        # `additionalProperties: False` válido para os outros.
        "dbase": _string(_NUMERIC_DIM),
        "dtopo": _string(_NUMERIC_DIM),
    },
}

_FOOTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["colunas_produzidas", "horas_trabalhadas"],
    "properties": {
        "colunas_produzidas": _string(_NUMERIC_LARGE),
        "horas_trabalhadas": _string(_HORAS),
    },
}

KANBAN_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "KanbanExtraction",
    "type": "object",
    "additionalProperties": False,
    "required": ["header", "rows", "footer"],
    "properties": {
        "header": _HEADER_SCHEMA,
        "rows": {
            "type": "array",
            "minItems": 0,
            "maxItems": 25,
            "items": _ROW_SCHEMA,
        },
        "footer": _FOOTER_SCHEMA,
    },
}


def grammar_version() -> str:
    """Stable hash of the schema content. Goes into the audit log so a
    change in the grammar is visible across runs."""
    canonical = json.dumps(KANBAN_JSON_SCHEMA, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:12]
