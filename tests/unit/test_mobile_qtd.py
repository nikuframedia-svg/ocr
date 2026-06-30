from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db
from app.web import main

_MOBILE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()


@pytest.fixture()
def isolate(monkeypatch):
    monkeypatch.setattr(main, "_run_and_store_cross_check", lambda *a, **k: None)


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed_sheet() -> int:
    sid = db.insert_sheet("mobile.jpg")
    sheet_data = {
        "template_name": "acabamento",
        "header": {
            "operador": "TESTE",
            "n_operador": "123",
            "setor_maquina": "ACABAMENTO MTG4",
            "cod_maquina": "M061",
            "data": "25-05-2026",
            "turno": "M",
        },
        "rows": [
            {"of": "262892", "modelo": "CGC2E10D", "qtd": "4"},
        ],
        "footer": {"colunas_produzidas": "4"},
    }
    db.update_extraction(sid, sheet_data, {}, sheet_data)
    return sid


def test_mobile_qtd_batch_saves_qtd_without_validating(tmp_db, isolate, client):
    sid = _seed_sheet()

    response = client.post(
        "/mobile/qtds-batch",
        json={
            "edits": [
                {"sheet_id": sid, "field_path": "rows[0].qtd", "value": "7"},
                {
                    "sheet_id": sid,
                    "field_path": "footer.colunas_produzidas",
                    "value": "7",
                },
            ],
        },
        headers=_MOBILE,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    sheet = db.get_sheet(sid)
    assert sheet["status"] == "extracted"
    assert sheet["sheet_data"]["rows"][0]["qtd"] == "7"
    assert sheet["sheet_data"]["footer"]["colunas_produzidas"] == "7"


def test_mobile_qtd_batch_rejects_invalid_path(tmp_db, isolate, client):
    sid = _seed_sheet()

    response = client.post(
        "/mobile/qtds-batch",
        json={
            "edits": [
                {
                    "sheet_id": sid,
                    "field_path": "rows[undefined].qtd",
                    "value": "9",
                },
            ],
        },
        headers=_MOBILE,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["applied"] == 0
    assert "not allowed" in body["errors"][0]["error"]
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["qtd"] == "4"


def test_mobile_validate_still_forbidden(tmp_db, isolate, client):
    sid = _seed_sheet()

    response = client.post(f"/sheet/{sid}/validate", headers=_MOBILE)

    assert response.status_code == 403
    assert "Validação só pode ser feita em desktop" in response.text


def test_mobile_qtd_batch_blocks_validated_sheet(tmp_db, isolate, client):
    sid = _seed_sheet()
    db.validate_sheet(sid, "TESTE")

    response = client.post(
        "/mobile/qtds-batch",
        json={
            "edits": [
                {"sheet_id": sid, "field_path": "rows[0].qtd", "value": "7"},
            ],
        },
        headers=_MOBILE,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "already validated" in body["errors"][0]["error"]
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["qtd"] == "4"
