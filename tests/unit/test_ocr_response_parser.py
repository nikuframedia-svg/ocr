from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ocr6
from app.pipeline.inference.response_parser import (
    OCRResponseParseError,
    detect_fustes_side,
    parse_ocr_response,
)
from app.web import ocr_runner


def test_json_payload_still_wins() -> None:
    raw = '{"header":{"operador":"X"},"rows":[{"of":"123"}],"footer":{}}'

    parsed = parse_ocr_response(raw)

    assert parsed["header"]["operador"] == "X"
    assert parsed["rows"] == [{"of": "123"}]


def test_html_production_table_maps_rows_by_header() -> None:
    raw = """
    <table>
      <tr><th>PRI</th><th>CLIENTE</th><th>OV</th><th>OF</th><th>MODELO</th>
          <th>QTD</th><th>COMP</th><th>LARG</th><th>LOTE</th><th>CONI</th>
          <th>ESP</th><th>LBASE</th><th>LTOPO</th></tr>
      <tr><td>C1</td><td>ELECNOR</td><td>2506273</td><td>256690</td>
          <td>U522</td><td>3</td><td>5940</td><td>250</td><td>M26B001</td>
          <td>12</td><td>2,6</td><td>479</td><td>193</td></tr>
    </table>
    """

    parsed = parse_ocr_response(raw)
    row = parsed["rows"][0]

    assert row["pri"] == "C1"
    assert row["cliente"] == "ELECNOR"
    assert row["of"] == "256690"
    assert row["modelo"] == "U522"
    assert row["comp_mm"] == "5940"
    assert row["larg_mm"] == "250"


def test_html_header_footer_and_aliases() -> None:
    raw = """
    <table>
      <tr><td>Operador</td><td>José Martins</td><td>Nº</td><td>1503</td></tr>
      <tr><td>Setor / Máquina</td><td>LASER</td><td>Data</td><td>16-06-2026</td></tr>
    </table>
    <table>
      <tr><th>OF</th><th>Referência / Peça</th><th>QTD</th></tr>
      <tr><td>263472</td><td>CLC6F08R</td><td>121</td></tr>
    </table>
    <table>
      <tr><td>Colunas Produzidas</td><td>121</td><td>Horas Trabalhadas</td><td>7H30</td></tr>
    </table>
    """

    parsed = parse_ocr_response(raw, row_fields=("of", "modelo", "qtd"))

    assert parsed["header"]["operador"] == "José Martins"
    assert parsed["header"]["n_operador"] == "1503"
    assert parsed["header"]["setor_maquina"] == "LASER"
    assert parsed["rows"] == [{"of": "263472", "modelo": "CLC6F08R", "qtd": "121"}]
    assert parsed["footer"]["colunas_produzidas"] == "121"
    assert parsed["footer"]["horas_trabalhadas"] == "7H30"


def test_horizontal_header_table_is_not_a_production_row() -> None:
    raw = """
    <table>
      <tr><th>Operador</th><th>Nº</th><th>Setor/Máquina</th><th>Data</th></tr>
      <tr><td>José</td><td>1503</td><td>LASER MTG2</td><td>16-06-2026</td></tr>
    </table>
    """

    parsed = parse_ocr_response(raw)

    assert parsed["header"]["operador"] == "José"
    assert parsed["header"]["n_operador"] == "1503"
    assert parsed["header"]["setor_maquina"] == "LASER MTG2"
    assert parsed["header"]["data"] == "16-06-2026"
    assert parsed["rows"] == []


def test_json_with_html_rows_uses_html_fallback() -> None:
    raw = """
    {"rows": "<table><tr><th>OF</th><th>MODELO</th><th>QTD</th></tr>
              <tr><td>262892</td><td>CGC2E10D</td><td>4</td></tr></table>"}
    """

    parsed = parse_ocr_response(raw, row_fields=("of", "modelo", "qtd"))

    assert parsed["rows"] == [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4"}]


def test_html_without_row_header_uses_template_order() -> None:
    raw = """
    <table>
      <tr><td>262892</td><td>CGC2E10D</td><td>4</td></tr>
    </table>
    """

    parsed = parse_ocr_response(raw, row_fields=("of", "modelo", "qtd"))

    assert parsed["rows"] == [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4"}]


def test_non_json_non_html_fails() -> None:
    with pytest.raises(OCRResponseParseError):
        parse_ocr_response("just text, no structure")


def test_ocr6_process_image_accepts_nanonets_html(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = """
    <table>
      <tr><th>OF</th><th>Referência / Peça</th><th>QTD</th></tr>
      <tr><td>262892</td><td>CGC2E10D</td><td>4</td></tr>
    </table>
    """
    monkeypatch.setattr(ocr6, "image_to_base64", lambda *_a, **_k: "image")
    monkeypatch.setattr(ocr6, "ollama_request", lambda *_a, **_k: (raw, {}))

    result = ocr6.process_image(
        Path("dummy.jpg"),
        idx=1,
        total=1,
        row_fields=("of", "modelo", "qtd"),
        header_fields=("operador",),
        footer_fields=("colunas_produzidas",),
    )

    assert result.metrics is not None
    assert result.metrics.status == "ok"
    assert result.rows == [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4"}]


def test_ocr6_html_without_mappable_rows_is_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "<table><tr><td>Operador</td><td>Jose</td></tr></table>"
    monkeypatch.setattr(ocr6, "image_to_base64", lambda *_a, **_k: "image")
    monkeypatch.setattr(ocr6, "ollama_request", lambda *_a, **_k: (raw, {}))

    result = ocr6.process_image(Path("dummy.jpg"), idx=1, total=1)

    assert result.metrics is not None
    assert result.metrics.status == "erro_json"
    assert result.metrics.error == "HTML table detected but no mappable production rows"


def test_ocr6_horizontal_header_html_without_rows_is_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = """
    <table>
      <tr><th>Operador</th><th>Nº</th><th>Setor/Máquina</th><th>Data</th></tr>
      <tr><td>José</td><td>1503</td><td>LASER MTG2</td><td>16-06-2026</td></tr>
    </table>
    """
    monkeypatch.setattr(ocr6, "image_to_base64", lambda *_a, **_k: "image")
    monkeypatch.setattr(ocr6, "ollama_request", lambda *_a, **_k: (raw, {}))

    result = ocr6.process_image(Path("dummy.jpg"), idx=1, total=1)

    assert result.metrics is not None
    assert result.metrics.status == "erro_json"
    assert result.metrics.error == "HTML table detected but no mappable production rows"


def test_fustes_side_detect_from_html_keywords() -> None:
    assert detect_fustes_side("<table><tr><th>MOTIVO DA PARAGEM</th></tr></table>") == "V"
    assert detect_fustes_side("<table><tr><th>PRI</th><th>CLIENTE</th></tr></table>") == "F"


def test_ocr_runner_side_detect_uses_html_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "<table><tr><th>MOTIVO DA PARAGEM</th><th>INÍCIO</th></tr></table>"
    monkeypatch.setattr(
        ocr_runner.ocr6,
        "process_image",
        lambda *_a, **_k: SimpleNamespace(raw_response=raw),
    )

    assert ocr_runner._detect_side(Path("dummy.jpg")) == "V"
