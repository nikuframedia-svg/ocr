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
    queued: list[tuple[int, ...]] = []
    monkeypatch.setattr(
        main,
        "_start_sheet_cross_check",
        lambda sheet_ids, **kwargs: queued.append(tuple(sorted(sheet_ids))),
    )
    return queued


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed_sheet(
    *,
    rows: list[dict] | None = None,
    footer: dict | None = None,
) -> int:
    sid = db.insert_sheet("mobile.jpg")
    if rows is None:
        rows = [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4"}]
    if footer is None:
        footer = {"colunas_produzidas": "4"}
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
        "rows": rows,
        "footer": footer,
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
    assert isolate == [(sid,)]


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
    assert isolate == []


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
    assert isolate == []


def test_mobile_qtd_batch_queues_cross_check_without_running_inline(
    tmp_db, monkeypatch, client
):
    sid = _seed_sheet()
    queued: list[tuple[int, ...]] = []

    def fail_inline(*args, **kwargs):
        raise AssertionError("cross-check ran inline")

    monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
    monkeypatch.setattr(
        main,
        "_start_sheet_cross_check",
        lambda sheet_ids, **kwargs: queued.append(tuple(sorted(sheet_ids))),
    )

    response = client.post(
        "/mobile/qtds-batch",
        json={
            "edits": [
                {"sheet_id": sid, "field_path": "rows[0].qtd", "value": "7"},
            ],
        },
        headers=_MOBILE,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert queued == [(sid,)]


def test_mobile_qtd_cross_check_background_uses_mobile_profile(monkeypatch):
    calls: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        main,
        "_run_and_store_cross_check",
        lambda sid, **kwargs: calls.append((sid, kwargs)),
    )

    main._run_sheet_cross_checks((3, 5), profile_trigger="mobile_qtd")

    assert calls == [
        (3, {"profile_trigger": "mobile_qtd"}),
        (5, {"profile_trigger": "mobile_qtd"}),
    ]


def test_mobile_qtd_batch_persists_multiple_rows_and_production_rows(
    tmp_db, isolate, client
):
    sid = _seed_sheet(
        rows=[
            {"of": "262892", "modelo": "CGC2E10D", "qtd": "4"},
            {"of": "262893", "modelo": "CGC2E11D", "qtd": "5"},
        ],
        footer={"colunas_produzidas": "9"},
    )

    response = client.post(
        "/mobile/qtds-batch",
        json={
            "edits": [
                {"sheet_id": sid, "field_path": "rows[0].qtd", "value": "10"},
                {"sheet_id": sid, "field_path": "rows[1].qtd", "value": "23"},
                {
                    "sheet_id": sid,
                    "field_path": "footer.colunas_produzidas",
                    "value": "33",
                },
            ],
        },
        headers=_MOBILE,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == 3
    assert body["sheets_updated"] == [sid]
    sheet = db.get_sheet(sid)
    assert sheet["sheet_data"]["rows"][0]["qtd"] == "10"
    assert sheet["sheet_data"]["rows"][1]["qtd"] == "23"
    assert sheet["sheet_data"]["footer"]["colunas_produzidas"] == "33"

    with db.conn() as c:
        rows = c.execute(
            "SELECT row_index, qtd, sheet_total_qty FROM production_rows "
            "WHERE sheet_id = ? ORDER BY row_index",
            (sid,),
        ).fetchall()
    assert [(r["row_index"], r["qtd"], r["sheet_total_qty"]) for r in rows] == [
        (0, 10, 33),
        (1, 23, 33),
    ]
    assert isolate == [(sid,)]


# ---------------------------------------------------------------------------
# rev00 — SUCATA por linha no mobile (espelha cesta_n)
# ---------------------------------------------------------------------------

def _seed_bobine(rows: list[dict] | None = None) -> int:
    """Folha de produção com template que declara `sucata` (bobine_formato)."""
    sid = db.insert_sheet("mobile.jpg")
    sheet_data = {
        "template_name": "bobine_formato",
        "header": {
            "operador": "TESTE", "n_operador": "123",
            "setor_maquina": "BOBINE-FORMATO", "cod_maquina": "M032",
            "data": "25-05-2026", "turno": "M",
        },
        "rows": rows or [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4", "sucata": ""}],
        "footer": {"colunas_produzidas": "4", "horas_trabalhadas": "8"},
    }
    db.update_extraction(sid, sheet_data, {}, sheet_data)
    return sid


def _seed_paragens() -> int:
    sid = db.insert_sheet("verso.jpg")
    sheet_data = {
        "template_name": "paragens",
        "header": {"operador": "TESTE", "n_operador": "1", "setor_maquina": "GUILHOTINA",
                   "data": "25-05-2026", "turno": "M"},
        "rows": [{"motivo": "avaria", "inicio": "09:00", "fim": "09:30"}],
        "footer": {},
    }
    db.update_extraction(sid, sheet_data, {}, sheet_data)
    return sid


def test_mobile_qtds_lists_sucata_for_production(tmp_db, isolate, client):
    sid = _seed_bobine()
    body = client.get(f"/mobile/qtds?ids={sid}", headers=_MOBILE).json()
    sheet = body["sheets"][0]
    assert "sucata" in sheet["row_fields_extra"]
    assert "sucata" in sheet["rows"][0]


def test_mobile_qtds_skips_paragens_sheet(tmp_db, isolate, client):
    prod = _seed_bobine()
    verso = _seed_paragens()
    body = client.get(f"/mobile/qtds?ids={prod},{verso}", headers=_MOBILE).json()
    ids = [s["sheet_id"] for s in body["sheets"]]
    assert prod in ids
    assert verso not in ids  # verso de paragens não vai ao ecrã de QTDs


def test_mobile_qtd_batch_saves_sucata(tmp_db, isolate, client):
    sid = _seed_bobine()
    r = client.post(
        "/mobile/qtds-batch",
        json={"edits": [{"sheet_id": sid, "field_path": "rows[0].sucata", "value": "2"}]},
        headers=_MOBILE,
    )
    assert r.status_code == 200
    assert r.json()["applied"] == 1
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["sucata"] == "2"
    # rev00 — e chega à tabela production_rows como INTEIRO (não só no JSON).
    with db.conn() as c:
        row = c.execute(
            "SELECT sucata FROM production_rows WHERE sheet_id = ? AND row_index = 0",
            (sid,),
        ).fetchone()
    assert row["sucata"] == 2


def _seed_paragens_sheet(template_name: str, setor: str) -> int:
    sid = db.insert_sheet("verso.jpg")
    sd = {
        "template_name": template_name,
        "header": {"operador": "X", "data": "25-05-2026", "setor_maquina": setor},
        "rows": [{"motivo": "avaria", "inicio": "09:00", "fim": "09:30"}],
        "footer": {},
    }
    db.update_extraction(sid, sd, {}, sd)
    return sid


def test_downtime_lists_generic_and_maq_fustes_paragens(tmp_db):
    """rev00 — o filtro dirigido pelo registry mostra o paragens genérico E o
    maq_fustes_paragens (o bug histórico escondia este último)."""
    from app.downtime import list_downtime_sheets

    p1 = _seed_paragens_sheet("paragens", "GUILHOTINA")
    p2 = _seed_paragens_sheet("maq_fustes_paragens", "MÁQUINA DE FUSTES")
    prod = _seed_bobine()  # produção → NÃO deve aparecer

    ids = {s["sheet_id"] for s in list_downtime_sheets(db._DB_PATH)}
    assert p1 in ids
    assert p2 in ids
    assert prod not in ids


def test_mobile_qtd_batch_rejects_sucata_on_template_without_it(tmp_db, isolate, client):
    # acabamento não declara sucata → edição rejeitada.
    sid = _seed_sheet()
    r = client.post(
        "/mobile/qtds-batch",
        json={"edits": [{"sheet_id": sid, "field_path": "rows[0].sucata", "value": "2"}]},
        headers=_MOBILE,
    )
    assert r.status_code == 400
    assert "sucata" in str(r.json()["errors"])
