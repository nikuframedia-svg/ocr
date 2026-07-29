"""Regression tests for the read-only /excel CPIS preview."""
from __future__ import annotations

import re

import pytest
from app.web import db, main
from fastapi.testclient import TestClient

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


def _refs():
    entry = {
        "of": "999999",
        "ov": "100200",
        "cliente": "ENEDIS",
        "designacao": "CGC2E10D",
        "comp": 5000,
        "lbase": 200,
        "ltopo": 150,
        "esp": 2.6,
        "npecas": 6,
        "pesounit": 17.85875,
    }
    return {
        "of_to_entries": {"999999": [entry]},
        "plan_by_ov": {"100200": [{**entry, "_of": "999999"}]},
        "lotes_sap_full": {
            "L1": {"esp": 2.6, "larg": 1500},
        },
    }


def test_excel_preview_formats_shared_weight_columns(tmp_db, monkeypatch):
    class _Watcher:
        def get_refs(self):
            return _refs()

    def _get_watcher():
        return _Watcher()

    monkeypatch.setattr(main, "get_watcher", _get_watcher)

    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {
            "operador": "JÚLIO LIMA",
            "n_operador": "0537",
            "data": "09-04-2026",
            "setor_maquina": "BOBINE-FORMATO",
            "cod_maquina": "",
        },
        "rows": [{
            "of": "999999",
            "ov": "100200",
            "cliente": "ENEDIS",
            "modelo": "CGC2E10D",
            "qtd": "5",
            "comp_mm": "9999",
            "larg_mm": "999",
            "lote": "L1",
            "lbase": "200",
            "ltopo": "150",
            "esp": "26",
            "coni": "10",
        }],
        "footer": {},
    }
    db.update_extraction(
        sid,
        raw_extraction=sheet_data,
        dq_audit={"cells": {}},
        sheet_data=sheet_data,
    )

    client = TestClient(main.app)
    response = client.get("/excel?of=999999", headers=_DESKTOP)

    assert response.status_code == 200
    html = response.text
    assert "Peso Consumido (t)" in html
    assert "Peso Produzido (t)" in html
    assert "Desperdício (t)" in html
    assert "% Desperdício" in html
    assert "<th>Lote</th>" in html
    assert "0.153" in html
    assert "0.089" in html
    assert "0.064" in html
    assert re.search(r">\s*L1\s*<", html)
    assert re.search(r">\s*2\.6\s*<", html)


def test_excel_preview_shows_expedicao_produced_weight_by_ov_model(tmp_db, monkeypatch):
    class _Watcher:
        def get_refs(self):
            return _refs()

    def _get_watcher():
        return _Watcher()

    monkeypatch.setattr(main, "get_watcher", _get_watcher)

    sid = db.insert_sheet("exp.jpg")
    sheet_data = {
        "header": {
            "operador": "JÚLIO LIMA",
            "n_operador": "0537",
            "data": "09-04-2026",
            "setor_maquina": "EXPEDIÇÃO",
            "cod_maquina": "",
        },
        "rows": [{
            "of": "",
            "ov": "100200",
            "cliente": "",
            "modelo": "CGC2E10D",
            "qtd": "5",
        }],
        "footer": {},
    }
    db.update_extraction(
        sid,
        raw_extraction=sheet_data,
        dq_audit={"cells": {}},
        sheet_data=sheet_data,
    )

    client = TestClient(main.app)
    response = client.get("/excel?ov=100200", headers=_DESKTOP)

    assert response.status_code == 200
    html = response.text
    assert "EXPEDIÇÃO" in html
    assert "0.089" in html
