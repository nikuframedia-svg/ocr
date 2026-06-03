"""Tests for the strict JSON schema used in guided decoding.

Two contracts:

1. **Draft coverage**: every value observed in ``ground_truth_draft/``
   (the 19 hand-checked Kanban sheets) must validate against the
   patterns. Otherwise we'd be vetoing legitimate values at sampling
   time.
2. **Anti-tests**: deliberately malformed values must fail validation.
   These confirm the patterns are not vacuous.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.pipeline.inference.schemas_strict import (
    KANBAN_JSON_SCHEMA,
    grammar_version,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DRAFTS_DIR = _PROJECT_ROOT / "ground_truth_draft"


def _pattern_for(*keys: str) -> str:
    """Drill into the schema to extract a specific field's regex."""
    node = KANBAN_JSON_SCHEMA
    for key in keys:
        if "items" in node and isinstance(node["items"], dict):
            node = node["items"]
        node = node["properties"][key]
    return node["pattern"]


def _matches(value: str, *path: str) -> bool:
    return re.match(_pattern_for(*path), value) is not None


def test_grammar_version_is_short_hex() -> None:
    v = grammar_version()
    assert len(v) == 12
    assert all(c in "0123456789abcdef" for c in v)


def test_drafts_validate_against_schema() -> None:
    """Every value in every draft JSON must pass its corresponding regex."""
    if not _DRAFTS_DIR.is_dir():
        pytest.skip("no ground_truth_draft/ — nothing to cross-check")

    failures: list[str] = []
    for path in sorted(_DRAFTS_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        h = data["header"]
        for field in ("operador", "n_operador", "setor_maquina", "data"):
            if not _matches(h.get(field, ""), "header", field):
                failures.append(f"{path.name} header.{field}={h.get(field)!r}")
        for idx, row in enumerate(data["rows"]):
            for field, value in row.items():
                if field == "coni":
                    # Legacy drafts predate the current FERRAMENTA / CONI
                    # contract. The new contract is asserted explicitly below.
                    continue
                if not _matches(value, "rows", field):
                    failures.append(f"{path.name} rows[{idx}].{field}={value!r}")
        f = data["footer"]
        for field in ("colunas_produzidas", "horas_trabalhadas"):
            if not _matches(f.get(field, ""), "footer", field):
                failures.append(f"{path.name} footer.{field}={f.get(field)!r}")

    assert not failures, "Schema rejects observed values:\n  " + "\n  ".join(failures)


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("01-04-2026", ("header", "data")),
        ("1-4-2026", ("header", "data")),
        ("18-04-2026", ("header", "data")),
        ("261860", ("rows", "of")),
        ("M26B0292", ("rows", "lote")),
        ("M24B11531", ("rows", "lote")),
        ("CONI", ("rows", "coni")),
        ("OCT", ("rows", "coni")),
        ("TORRES", ("rows", "coni")),
        ("CIL", ("rows", "coni")),
        ("CIO", ("rows", "coni")),
        ("CIB", ("rows", "coni")),
        ("12", ("rows", "coni")),
        ("2,6", ("rows", "esp")),
        ("3.5", ("rows", "esp")),
        ("8:00", ("footer", "horas_trabalhadas")),
        ("6 H", ("footer", "horas_trabalhadas")),
        ("0,30", ("footer", "horas_trabalhadas")),
    ],
)
def test_known_good_values(value: str, path: tuple[str, ...]) -> None:
    assert _matches(value, *path), f"{value!r} should match pattern at {path}"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ("hoje", ("header", "data")),  # not a date
        ("2026/04/18", ("header", "data")),  # wrong separator
        ("abc", ("rows", "of")),  # non-numeric
        ("12345", ("rows", "of")),  # 5 digits, OF must be 6
        ("1234567", ("rows", "of")),  # 7 digits, too long
        ("XYZ", ("rows", "lote")),  # not lote shape
        ("M2BB0292", ("rows", "lote")),  # extra letter
        ("T", ("rows", "coni")),  # old shorthand is no longer valid
        ("OCT.", ("rows", "coni")),  # final value must be normalised to OCT
        ("ABC", ("rows", "coni")),
        ("just a sentence with words", ("rows", "modelo")),  # too long & spaces+lowercase
    ],
)
def test_known_bad_values_rejected(value: str, path: tuple[str, ...]) -> None:
    assert not _matches(value, *path), f"{value!r} should NOT match pattern at {path}"
