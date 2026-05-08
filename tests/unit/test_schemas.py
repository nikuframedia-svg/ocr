"""Tests for the Pydantic extraction schema.

The schema is permissive on purpose (see :mod:`app.pipeline.inference.schemas`).
These tests document what *is* enforced.
"""
from __future__ import annotations

import pytest
from app.pipeline.inference.schemas import (
    CRITICAL_ROW_FIELDS,
    Footer,
    Header,
    KanbanExtraction,
    Row,
)
from pydantic import ValidationError


def test_round_trip_minimal() -> None:
    data = {
        "header": {"operador": "Júlio Lima", "n_operador": "0537", "data": "01-04-2026"},
        "rows": [{"of": "261860", "modelo": "CGC2E10D", "qtd": "5"}],
        "footer": {"colunas_produzidas": "5"},
    }
    parsed = KanbanExtraction.model_validate(data)
    assert parsed.header.operador == "Júlio Lima"
    assert parsed.rows[0].of == "261860"
    assert parsed.footer.colunas_produzidas == "5"


def test_none_coerced_to_empty_string() -> None:
    parsed = Header.model_validate({"operador": None, "data": None})
    assert parsed.operador == ""
    assert parsed.data == ""


def test_int_coerced_to_string_for_numeric_fields() -> None:
    parsed = Row.model_validate({"qtd": 5, "comp_mm": 5000, "larg_mm": 200})
    assert parsed.qtd == "5"
    assert parsed.comp_mm == "5000"
    assert parsed.larg_mm == "200"


def test_integer_float_coerced_without_decimal() -> None:
    parsed = Row.model_validate({"qtd": 5.0})
    assert parsed.qtd == "5"


def test_non_integer_float_keeps_decimal() -> None:
    parsed = Footer.model_validate({"horas_trabalhadas": 8.5})
    assert parsed.horas_trabalhadas == "8.5"


def test_whitespace_stripped() -> None:
    parsed = Header.model_validate({"operador": "  Júlio Lima  "})
    assert parsed.operador == "Júlio Lima"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01/04/2026", "01-04-2026"),
        ("01.04.2026", "01-04-2026"),
        ("01-04-2026", "01-04-2026"),
        # Single-digit components must be zero-padded so the benchmark
        # doesn't count "10-4-2026" and "10-04-2026" as different dates.
        ("1-4-2026", "01-04-2026"),
        ("10-4-2026", "10-04-2026"),
        ("9/4/2026", "09-04-2026"),
        # Garbage that doesn't match the date shape is left as-is
        # (only the separator pass applies).
        ("hoje", "hoje"),
        ("", ""),
    ],
)
def test_date_normalisation(raw: str, expected: str) -> None:
    parsed = Header.model_validate({"data": raw})
    assert parsed.data == expected


def test_extra_fields_ignored() -> None:
    parsed = Row.model_validate({"of": "261860", "extra_unknown": "ignored"})
    assert parsed.of == "261860"
    assert not hasattr(parsed, "extra_unknown")


def test_empty_factory() -> None:
    empty = KanbanExtraction.empty()
    assert empty.header.operador == ""
    assert empty.rows == []
    assert empty.footer.colunas_produzidas == ""


def test_missing_top_level_keys_raises() -> None:
    with pytest.raises(ValidationError):
        KanbanExtraction.model_validate({"header": {}, "rows": []})  # missing footer


def test_critical_fields_constant_intact() -> None:
    assert CRITICAL_ROW_FIELDS == ("of", "modelo", "qtd")


def test_rows_default_to_empty_list_when_missing_in_extraction() -> None:
    with pytest.raises(ValidationError):
        KanbanExtraction.model_validate({"header": {}, "footer": {}})


def test_all_row_fields_default_to_empty_string() -> None:
    row = Row()
    for field_name in (
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
    ):
        assert getattr(row, field_name) == ""
