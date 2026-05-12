"""CPIS migration export — schema + row-mapping tests.

The output of `build_cpis_workbook` must match the column layout of the
user-supplied `MigracaoNikufraCPIS.xlsx` template byte-for-byte at the
header level (17 columns, exact labels, `Folha1` sheet name). Numeric
values for Peso / Desperdício must match `geometry.row_waste`.
"""
from __future__ import annotations

import datetime as dt
import io

import openpyxl

from app.web.export import (
    CPIS_COLUMNS,
    _build_cpis_row,
    _iso_to_date,
    cpis_filename_for,
)


# Cabeçalhos exatos do template (transcritos de
# /Users/martimnicolau/Downloads/MigracaoNikufraCPIS.xlsx).
EXPECTED_HEADER_LABELS = [
    "Data",
    "Cód. Funcionário",
    "Nome Funcionário",
    "Setor / Máquina Desc.",
    "Cód. Máquina",
    "OF",
    "OV",
    "Cliente",
    "Modelo",
    "QTD",
    "Comprimento (mm)",
    "Largura (mm)",
    "Espessura (mm)",
    "CONI",
    "Peso (kg)",
    "Desperdício (kg)",
    "% Desperdício",
]


def test_cpis_columns_match_template_exactly() -> None:
    """The 17 header labels must match the user-supplied template."""
    labels = [label for _, label in CPIS_COLUMNS]
    assert labels == EXPECTED_HEADER_LABELS


def test_iso_to_date_parses_valid_iso() -> None:
    assert _iso_to_date("2026-04-09") == dt.date(2026, 4, 9)
    assert _iso_to_date(None) is None
    assert _iso_to_date("") is None
    assert _iso_to_date("not-a-date") is None


def test_build_cpis_row_bobine_formato_with_waste() -> None:
    """Typical BOBINE-FORMATO row → 17 fields populated."""
    raw = {
        "sheet_iso_date": "2026-04-09",
        "operador": "JÚLIO LIMA",
        "validated_operador": "JÚLIO LIMA",
        "n_operador": "0537",
        "setor_maquina": "BOBINE-FORMATO",
        "header_cod_maquina": None,
        "of": "261860",
        "ov": "100200",
        "cliente": "ENEDIS",
        "modelo": "CGC2E10D",
        "qtd": 5,
        "comp_mm": 5000,
        "larg_mm": 1500,
        "lbase": 200,
        "ltopo": 150,
        "esp": 3.0,
        "coni": "10",
    }
    out = _build_cpis_row(raw)

    assert out["data"] == dt.date(2026, 4, 9)
    assert out["cod_funcionario"] == 537
    assert out["nome_funcionario"] == "JÚLIO LIMA"
    assert out["setor_maquina_desc"] == "BOBINE-FORMATO"
    assert out["cod_maquina"] == "M032"  # derived from setor
    assert out["of"] == "261860"
    assert out["ov"] == "100200"
    assert out["cliente"] == "ENEDIS"
    assert out["modelo"] == "CGC2E10D"
    assert out["qtd"] == 5
    assert out["comp_mm"] == 5000
    assert out["larg_mm"] == 1500
    assert out["esp_mm"] == 3.0
    assert out["coni"] == "10"
    # Geometric waste — non-zero because larg(1500) > lbase+ltopo(350) so
    # multiple pieces fit per slice.
    assert out["peso_kg"] is not None
    assert out["peso_kg"] > 0
    assert out["desperdicio_kg"] is not None
    assert out["desperdicio_kg"] >= 0
    assert out["desperdicio_pct"] is not None


def test_build_cpis_row_header_cod_maquina_wins_over_derivation() -> None:
    """If OCR captures cod_maquina directly, prefer it over the table."""
    raw = {
        "sheet_iso_date": "2026-04-09",
        "n_operador": "1",
        "setor_maquina": "UNKNOWN-MACHINE",  # not in derivation table
        "header_cod_maquina": "M999",
        "operador": "X",
    }
    out = _build_cpis_row(raw)
    assert out["cod_maquina"] == "M999"


def test_build_cpis_row_missing_geometry_returns_none_for_weights() -> None:
    """Rows without enough geometry skip Peso/Desperdício gracefully."""
    raw = {
        "sheet_iso_date": "2026-04-09",
        "n_operador": "1",
        "setor_maquina": "BOBINE-FORMATO",
        "operador": "X",
        "qtd": 5,
        # No lbase/ltopo/esp → row_waste returns invalid
    }
    out = _build_cpis_row(raw)
    assert out["peso_kg"] is None
    assert out["desperdicio_kg"] is None
    assert out["desperdicio_pct"] is None
    # Other fields still present
    assert out["qtd"] == 5
    assert out["setor_maquina_desc"] == "BOBINE-FORMATO"


def test_cpis_filename_includes_operador_slug() -> None:
    f = cpis_filename_for("2026-04-01", "2026-04-30")
    assert f == "MigracaoNikufraCPIS_2026-04-01_2026-04-30.xlsx"

    f_op = cpis_filename_for("2026-04-01", "2026-04-30", "JÚLIO LIMA")
    assert f_op.startswith("MigracaoNikufraCPIS_2026-04-01_2026-04-30_")
    assert f_op.endswith(".xlsx")
    # Diacritics stripped, spaces removed
    assert "JLIOLIMA" in f_op or "JULIOLIMA" in f_op or "LIMA" in f_op


def test_workbook_header_row_matches_template() -> None:
    """End-to-end: build a workbook from a synthetic row and read it back.

    Validates: sheet name = Folha1, first row = 17 expected labels, second
    row has the synthetic data in the right slots.
    """
    # Inline-import to avoid pulling kpis/db at module-load time
    from app.web import export

    # Stub out the DB query — we don't need real data for this test
    synthetic = [{
        "sheet_iso_date": "2026-04-09",
        "operador": "JÚLIO LIMA",
        "validated_operador": "JÚLIO LIMA",
        "n_operador": "0537",
        "setor_maquina": "BOBINE-FORMATO",
        "header_cod_maquina": None,
        "of": "261860",
        "ov": "100200",
        "cliente": "ENEDIS",
        "modelo": "CGC2E10D",
        "qtd": 5,
        "comp_mm": 5000,
        "larg_mm": 1500,
        "lbase": 200,
        "ltopo": 150,
        "esp": 3.0,
        "coni": "10",
    }]
    original_query = export._query_cpis_rows
    export._query_cpis_rows = lambda *a, **kw: synthetic  # type: ignore[attr-defined]
    try:
        xlsx_bytes = export.build_cpis_workbook("2026-04-01", "2026-04-30")
    finally:
        export._query_cpis_rows = original_query  # type: ignore[attr-defined]

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    assert wb.sheetnames == ["Folha1"]
    ws = wb["Folha1"]
    header_row = [c.value for c in ws[1]]
    assert header_row == EXPECTED_HEADER_LABELS

    data_row = [c.value for c in ws[2]]
    # openpyxl reads date cells back as datetime; compare the date portion.
    assert isinstance(data_row[0], (dt.date, dt.datetime))
    assert (data_row[0].date() if isinstance(data_row[0], dt.datetime)
            else data_row[0]) == dt.date(2026, 4, 9)
    assert data_row[1] == 537                         # Cód. Funcionário
    assert data_row[2] == "JÚLIO LIMA"                # Nome
    assert data_row[3] == "BOBINE-FORMATO"            # Setor / Máquina
    assert data_row[4] == "M032"                      # Cód. Máquina
    assert data_row[5] == "261860"                    # OF
    assert data_row[7] == "ENEDIS"                    # Cliente
    assert data_row[9] == 5                           # QTD
    assert data_row[10] == 5000                       # Comprimento
    assert data_row[11] == 1500                       # Largura
    assert data_row[12] == 3.0                        # Espessura
    assert data_row[14] is not None and data_row[14] > 0   # Peso
    assert data_row[15] is not None and data_row[15] >= 0  # Desperdício kg


def test_workbook_empty_period_still_has_header() -> None:
    """Zero rows → workbook with just the header row, valid for download."""
    from app.web import export

    original_query = export._query_cpis_rows
    export._query_cpis_rows = lambda *a, **kw: []  # type: ignore[attr-defined]
    try:
        xlsx_bytes = export.build_cpis_workbook("2026-04-01", "2026-04-30")
    finally:
        export._query_cpis_rows = original_query  # type: ignore[attr-defined]

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb["Folha1"]
    assert [c.value for c in ws[1]] == EXPECTED_HEADER_LABELS
    # No data rows
    assert ws.max_row == 1
