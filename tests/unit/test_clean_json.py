"""Regression tests for ``_clean_json``.

These exist to *freeze* the behaviour ported from the legacy ``ocr6.py``.
If you change ``_clean_json`` and these break, re-baselining the OCR
metrics is required — the function is on the hot path of every
extraction.
"""
from __future__ import annotations

import pytest
from app.pipeline.inference.vllm_client import _clean_json, _section_payload
from app.pipeline.inference.response_parser import OCRResponseParseError


def test_plain_json() -> None:
    raw = '{"header": {"operador": "X"}, "rows": [], "footer": {}}'
    assert _clean_json(raw) == {"header": {"operador": "X"}, "rows": [], "footer": {}}


def test_strips_think_block() -> None:
    raw = '<think>let me reason about this</think>\n{"a": 1}'
    assert _clean_json(raw) == {"a": 1}


def test_strips_multiline_think_block() -> None:
    raw = (
        "<think>\n"
        "step 1: detect header\n"
        "step 2: parse table\n"
        "</think>\n"
        '{"a": 1, "b": [1, 2]}'
    )
    assert _clean_json(raw) == {"a": 1, "b": [1, 2]}


def test_extracts_from_json_fenced_block() -> None:
    raw = "Here is the result:\n```json\n{\"a\": 1}\n```\n"
    assert _clean_json(raw) == {"a": 1}


def test_extracts_from_unmarked_fenced_block() -> None:
    raw = "Result:\n```\n{\"a\": 1}\n```"
    assert _clean_json(raw) == {"a": 1}


def test_extracts_from_braces_when_surrounded_by_prose() -> None:
    raw = 'The extracted data is: {"a": 1, "b": "hi"} and that is all.'
    assert _clean_json(raw) == {"a": 1, "b": "hi"}


def test_handles_nested_objects() -> None:
    raw = '{"header": {"data": "01-04-2026"}, "rows": [{"of": "261860"}]}'
    assert _clean_json(raw) == {
        "header": {"data": "01-04-2026"},
        "rows": [{"of": "261860"}],
    }


def test_invalid_json_raises() -> None:
    with pytest.raises(OCRResponseParseError):
        _clean_json('{"a": 1,')


def test_no_json_at_all_raises() -> None:
    with pytest.raises(OCRResponseParseError):
        _clean_json("just some text without any json")


def test_think_then_fenced() -> None:
    raw = "<think>some reasoning</think>\n```json\n{\"a\": 1}\n```"
    assert _clean_json(raw) == {"a": 1}


def test_repairs_missing_comma_between_array_objects() -> None:
    """Qwen2.5-VL through Ollama drops the comma between sibling row
    objects. _clean_json must paper over it instead of erroring."""
    raw = (
        '```json\n'
        '{"rows": [\n'
        '  {"of": "111111"}\n'
        '  {"of": "222222"}\n'
        ']}\n'
        '```'
    )
    parsed = _clean_json(raw)
    assert parsed == {"rows": [{"of": "111111"}, {"of": "222222"}]}


def test_repair_does_not_corrupt_already_valid_json() -> None:
    raw = '{"rows": [{"of": "111111"}, {"of": "222222"}]}'
    assert _clean_json(raw) == {"rows": [{"of": "111111"}, {"of": "222222"}]}


def test_html_table_fallback() -> None:
    raw = """
    <table>
      <tr><th>PRI</th><th>CLIENTE</th><th>OF</th><th>MODELO</th><th>QTD</th></tr>
      <tr><td>1</td><td>ELECNOR</td><td>256690</td><td>U522</td><td>3</td></tr>
    </table>
    """
    parsed = _clean_json(raw)
    assert parsed["rows"][0]["cliente"] == "ELECNOR"
    assert parsed["rows"][0]["of"] == "256690"


def test_section_payload_unwraps_canonical_header_payload() -> None:
    payload = {
        "header": {"operador": "José", "data": "16-06-2026"},
        "rows": [{"of": "263472"}],
        "footer": {},
    }

    assert _section_payload(payload, "header") == {
        "operador": "José",
        "data": "16-06-2026",
    }


def test_section_payload_keeps_flat_crop_payload() -> None:
    payload = {"operador": "José", "data": "16-06-2026"}

    assert _section_payload(payload, "header") == payload
