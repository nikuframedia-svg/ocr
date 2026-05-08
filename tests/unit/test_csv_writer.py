"""Byte-by-byte regression tests for the CSV writer.

The output of :func:`app.pipeline.inference.csv_writer.write_csv` must
match what the legacy ``ocr6.py`` produces for the same extraction.
Metalogalva already consumes the legacy format and breaking it would
break their downstream tooling.
"""
from __future__ import annotations

from pathlib import Path

from app.pipeline.inference.csv_writer import write_csv
from app.pipeline.inference.schemas import Footer, Header, KanbanExtraction, Row


def _read_bytes_with_bom(path: Path) -> bytes:
    """Return raw bytes, including the BOM."""
    return path.read_bytes()


def test_single_sheet_byte_regression(tmp_path: Path) -> None:
    extraction = KanbanExtraction(
        header=Header(
            operador="Júlio Lima",
            n_operador="0537",
            setor_maquina="BOBINE",
            data="01-04-2026",
        ),
        rows=[
            Row(
                pri="1",
                cliente="MTG",
                ov="",
                of="261860",
                modelo="CGC2E10D",
                qtd="5",
                comp_mm="5000",
                larg_mm="200",
                lote="M25B0746",
                coni="10",
                esp="",
                lbase="",
                ltopo="",
            )
        ],
        footer=Footer(colunas_produzidas="5", horas_trabalhadas="8"),
    )
    output = tmp_path / "out.csv"
    write_csv(output, [("test.jpg", extraction)])

    expected = (
        "﻿### CABEÇALHO\r\n"
        "FICHEIRO;DATA;OPERADOR;N_OPERADOR;SETOR_MAQUINA\r\n"
        "test.jpg;01-04-2026;Júlio Lima;0537;BOBINE\r\n"
        "\r\n"
        "### TABELA DE PRODUÇÃO\r\n"
        "FICHEIRO;DATA;OPERADOR;PRI;CLIENTE;OV;OF;MODELO;QTD;COMP_MM;"
        "LARG_MM;LOTE;CONI;ESP;LBASE;LTOPO\r\n"
        "test.jpg;01-04-2026;Júlio Lima;1;MTG;;261860;CGC2E10D;5;5000;200;"
        "M25B0746;10;;;\r\n"
        "\r\n"
        "### RODAPÉ\r\n"
        "FICHEIRO;DATA;OPERADOR;COLUNAS_PRODUZIDAS;HORAS_TRABALHADAS\r\n"
        "test.jpg;01-04-2026;Júlio Lima;5;8\r\n"
    )
    assert _read_bytes_with_bom(output) == expected.encode("utf-8")


def test_no_rows_still_emits_table_block(tmp_path: Path) -> None:
    extraction = KanbanExtraction(
        header=Header(operador="X", n_operador="1", setor_maquina="S", data="01-01-2026"),
        rows=[],
        footer=Footer(colunas_produzidas="0", horas_trabalhadas="0"),
    )
    output = tmp_path / "out.csv"
    write_csv(output, [("a.jpg", extraction)])

    text = output.read_text(encoding="utf-8-sig")
    assert "### CABEÇALHO" in text
    assert "### TABELA DE PRODUÇÃO" in text
    assert "### RODAPÉ" in text
    # Table block must contain the header row even when there are no data rows.
    assert "FICHEIRO;DATA;OPERADOR;PRI;CLIENTE;OV;OF;MODELO;QTD" in text


def test_multi_sheet_groups_by_block(tmp_path: Path) -> None:
    """Multiple sheets must produce one entry per sheet in each block,
    not interleave header/table/footer per sheet."""
    sheet_a = KanbanExtraction(
        header=Header(operador="A", n_operador="1", setor_maquina="S", data="01-01-2026"),
        rows=[Row(of="111111")],
        footer=Footer(colunas_produzidas="1", horas_trabalhadas="1"),
    )
    sheet_b = KanbanExtraction(
        header=Header(operador="B", n_operador="2", setor_maquina="S", data="02-01-2026"),
        rows=[Row(of="222222")],
        footer=Footer(colunas_produzidas="2", horas_trabalhadas="2"),
    )
    output = tmp_path / "out.csv"
    write_csv(output, [("a.jpg", sheet_a), ("b.jpg", sheet_b)])

    text = output.read_text(encoding="utf-8-sig")
    # Header block has both sheets together
    header_block = text.split("### TABELA DE PRODUÇÃO")[0]
    assert "a.jpg;01-01-2026;A;1;S" in header_block
    assert "b.jpg;02-01-2026;B;2;S" in header_block
    # Table block has both rows together, after header block
    table_block = text.split("### TABELA DE PRODUÇÃO")[1].split("### RODAPÉ")[0]
    assert "a.jpg;01-01-2026;A;;;;111111" in table_block
    assert "b.jpg;02-01-2026;B;;;;222222" in table_block


def test_uses_utf8_bom(tmp_path: Path) -> None:
    extraction = KanbanExtraction.empty()
    extraction.header.operador = "X"
    output = tmp_path / "out.csv"
    write_csv(output, [("a.jpg", extraction)])

    raw = output.read_bytes()
    # UTF-8 BOM is EF BB BF
    assert raw[:3] == b"\xef\xbb\xbf"


def test_uses_crlf_line_endings(tmp_path: Path) -> None:
    extraction = KanbanExtraction.empty()
    output = tmp_path / "out.csv"
    write_csv(output, [("a.jpg", extraction)])

    raw = output.read_bytes()
    # Every line ends with \r\n; \n alone should not appear except as part of \r\n.
    lf_count = raw.count(b"\n")
    crlf_count = raw.count(b"\r\n")
    assert lf_count == crlf_count
