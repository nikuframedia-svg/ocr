"""Pydantic schemas for the OCR extraction output.

The schema is deliberately **permissive**: it enforces only structural and
type contracts (the dict has the right keys, every field is a string).
Format validation (date format, OF regex, LOTE regex, etc.) is the job
of:

- :mod:`app.pipeline.validation` (Phase 3) — for production validation
- :mod:`scripts.annotate_cli` — for ground-truth annotation

This keeps the VLM output capturable even when individual fields are
malformed; per-field correctness is measured at comparison time by the
benchmark, not by parse-time exceptions.

The single exception is :attr:`Header.data`, which is *normalised* (not
validated) to ``DD-MM-YYYY`` with zero-padded day/month. Otherwise the
benchmark would flag every ``10-4-2026`` vs ``10-04-2026`` as a
mismatch even though both encode the same date.
"""
from __future__ import annotations

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_DATE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")

HEADER_FIELDS: Final[tuple[str, ...]] = (
    "operador",
    "n_operador",
    "setor_maquina",
    "cod_maquina",
    "data",
)

ROW_FIELDS: Final[tuple[str, ...]] = (
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
)

FOOTER_FIELDS: Final[tuple[str, ...]] = (
    "colunas_produzidas",
    "horas_trabalhadas",
)

CRITICAL_ROW_FIELDS: Final[tuple[str, ...]] = ("of", "modelo", "qtd")
"""Fields whose accuracy is reported separately in the baseline."""


def _coerce_to_str(value: Any) -> str:
    """Coerce ``None`` / numerics / floats to a stripped string.

    The VLM occasionally emits ``null``, integers, or floats for fields
    that should be strings (``"qtd": 5`` instead of ``"qtd": "5"``).
    Normalising at the boundary keeps downstream code simple.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, int | float):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


class _StringFields(BaseModel):
    """Mixin that coerces every incoming dict value to a string before validation."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _stringify(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {k: _coerce_to_str(v) for k, v in data.items()}


class Header(_StringFields):
    operador: str = ""
    n_operador: str = ""
    setor_maquina: str = ""
    cod_maquina: str = ""
    data: str = ""
    # R129 — turno (M/R/XM/T) nos kanbans com checkbox de turno.
    turno: str = ""

    @field_validator("data", mode="after")
    @classmethod
    def _normalise_date(cls, value: str) -> str:
        if not value:
            return value
        unified = value.replace("/", "-").replace(".", "-")
        match = _DATE_RE.match(unified)
        if not match:
            return unified
        day, month, year = match.groups()
        return f"{day.zfill(2)}-{month.zfill(2)}-{year}"


class Row(_StringFields):
    pri: str = ""
    cliente: str = ""
    ov: str = ""
    of: str = ""
    modelo: str = ""
    qtd: str = ""
    comp_mm: str = ""
    larg_mm: str = ""
    lote: str = ""
    coni: str = ""
    esp: str = ""
    lbase: str = ""
    ltopo: str = ""
    # R128 — kanban LASER (dimensões diâmetro base/topo)
    dbase: str = ""
    dtopo: str = ""


class Footer(_StringFields):
    colunas_produzidas: str = ""
    horas_trabalhadas: str = ""


class KanbanExtraction(BaseModel):
    """Top-level extraction object: one Kanban sheet → one instance."""

    model_config = ConfigDict(extra="ignore")

    header: Header
    rows: list[Row]
    footer: Footer

    @classmethod
    def empty(cls) -> KanbanExtraction:
        """Convenience factory for tests and fallback paths."""
        return cls(header=Header(), rows=[], footer=Footer())
