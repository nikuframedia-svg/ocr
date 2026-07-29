"""Regression tests for CPIS export by individually selected dates."""
from __future__ import annotations

import io

import openpyxl
import pytest
from app.web import db, export, main
from fastapi.testclient import TestClient

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


def _insert_production_sheet(date_iso: str, lote: str) -> None:
    year, month, day = date_iso.split("-")
    sheet_data = {
        "header": {
            "operador": "OPERADOR TESTE",
            "n_operador": "1",
            "data": f"{day}-{month}-{year}",
            "setor_maquina": "BOBINE-FORMATO",
        },
        "rows": [{
            "of": f"OF-{day}",
            "qtd": "1",
            "lote": lote,
        }],
        "footer": {},
    }
    sheet_id = db.insert_sheet(f"{date_iso}.jpg")
    db.update_extraction(
        sheet_id,
        raw_extraction=sheet_data,
        dq_audit={"cells": {}},
        sheet_data=sheet_data,
    )


def test_query_cpis_rows_filters_exact_non_consecutive_dates(tmp_db) -> None:
    _insert_production_sheet("2026-07-23", "M26B0023")
    _insert_production_sheet("2026-07-24", "M26B0024")
    _insert_production_sheet("2026-07-29", "M26B0029")

    rows = export._query_cpis_rows(
        None,
        None,
        None,
        selected_dates=("2026-07-23", "2026-07-29"),
    )

    assert [row["sheet_iso_date"] for row in rows] == [
        "2026-07-23",
        "2026-07-29",
    ]
    assert [row["lote"] for row in rows] == ["M26B0023", "M26B0029"]


def test_query_cpis_rows_explicit_empty_selection_returns_no_rows(tmp_db) -> None:
    _insert_production_sheet("2026-07-23", "M26B0023")

    rows = export._query_cpis_rows(
        None,
        None,
        None,
        selected_dates=(),
    )

    assert rows == []


def test_valid_selected_date_without_production_returns_header_only_xlsx(tmp_db) -> None:
    xlsx = export.build_cpis_workbook(
        None,
        None,
        selected_dates=("2026-08-01",),
    )

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx))
    worksheet = workbook["Folha1"]
    assert worksheet.max_row == 1
    assert [cell.value for cell in worksheet[1]] == [
        label for _, label in export.CPIS_COLUMNS
    ]


def _capture_endpoint_export(monkeypatch) -> dict:
    captured: dict = {}

    def fake_build(*args, **kwargs):
        captured["build_args"] = args
        captured["build_kwargs"] = kwargs
        return b"xlsx"

    def fake_filename(*args, **kwargs):
        captured["filename_args"] = args
        captured["filename_kwargs"] = kwargs
        return "selected.xlsx"

    monkeypatch.setattr(export, "build_cpis_workbook", fake_build)
    monkeypatch.setattr(export, "cpis_filename_for", fake_filename)
    return captured


def test_export_cpis_normalizes_sorts_and_deduplicates_dates(monkeypatch) -> None:
    captured = _capture_endpoint_export(monkeypatch)
    client = TestClient(main.app)

    response = client.get(
        "/export/cpis",
        params=[
            ("date_mode", "selected"),
            ("selected_dates", "2026-07-29"),
            ("selected_dates", "2026-07-23"),
            ("selected_dates", "2026-07-29"),
        ],
    )

    assert response.status_code == 200
    assert captured["build_kwargs"]["selected_dates"] == (
        "2026-07-23",
        "2026-07-29",
    )
    assert captured["filename_kwargs"]["selected_dates"] == (
        "2026-07-23",
        "2026-07-29",
    )
    assert response.headers["content-disposition"] == 'attachment; filename="selected.xlsx"'


def test_export_cpis_period_mode_remains_unchanged(monkeypatch) -> None:
    captured = _capture_endpoint_export(monkeypatch)
    client = TestClient(main.app)

    response = client.get(
        "/export/cpis",
        params={
            "date_from": "2026-07-01",
            "date_to": "2026-07-29",
            "operador": "OPERADOR TESTE",
        },
    )

    assert response.status_code == 200
    assert captured["build_args"][:3] == (
        "2026-07-01",
        "2026-07-29",
        "OPERADOR TESTE",
    )
    assert captured["build_kwargs"]["selected_dates"] is None


def test_export_modal_exposes_selected_days_without_changing_period_presets(
    tmp_db,
    monkeypatch,
) -> None:
    class _Watcher:
        def get_refs(self):
            return {}

    def _get_watcher():
        return _Watcher()

    monkeypatch.setattr(main, "get_watcher", _get_watcher)

    response = TestClient(main.app).get("/excel", headers=_DESKTOP)

    assert response.status_code == 200
    html = response.text
    assert "Selecionar dias" in html
    assert 'name="date_mode"' in html
    assert 'name="selected_dates"' in html
    assert "Seleciona pelo menos um dia para exportar o CPIS." in html
    assert "apenas no CPIS" in html
    assert 'name="date_from"' in html
    assert 'name="date_to"' in html
    for label in ("1 dia", "1 semana", "1 mês", "3 meses", "6 meses", "1 ano", "Sempre"):
        assert f"label: '{label}'" in html


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"date_mode": "selected"}, "select at least one date"),
        (
            {
                "date_mode": "selected",
                "selected_dates": "2026-07-23",
                "date_from": "2026-07-01",
                "date_to": "2026-07-29",
            },
            "selected dates cannot be combined",
        ),
        (
            {"date_mode": "selected", "selected_dates": "23-07-2026"},
            "selected_dates must be YYYY-MM-DD",
        ),
        (
            {"date_mode": "selected", "selected_dates": "2026-02-30"},
            "invalid selected date",
        ),
        (
            {"selected_dates": "2026-07-23"},
            "selected_dates requires date_mode=selected",
        ),
        (
            {"date_mode": "unknown"},
            "date_mode must be",
        ),
    ],
)
def test_export_cpis_rejects_invalid_selected_date_requests(
    params,
    detail,
) -> None:
    response = TestClient(main.app).get("/export/cpis", params=params)

    assert response.status_code == 400
    assert detail in response.json()["detail"]
