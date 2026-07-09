"""Task C E4 — filtro por unidade fabril em list_sheets_filtered + /queue + /kanbans."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed_sheet(unidade_id=None, template_name="bobine_formato", operador="ANA"):
    data = {"template_name": template_name,
            "header": {"operador": operador, "setor_maquina": "Corte",
                       "data": "01-07-2026"},
            "rows": [], "footer": {}}
    with db.conn() as c:
        c.execute(
            "INSERT INTO sheets (image_path, status, sheet_data, unidade_id) "
            "VALUES (?,?,?,?)",
            ("x.jpg", "extracted", json.dumps(data), unidade_id))
        return c.execute("SELECT MAX(id) AS i FROM sheets").fetchone()["i"]


class TestListSheetsFiltered:
    def test_no_filter_returns_all(self):
        _seed_sheet()
        _seed_sheet(unidade_id=db.create_unidade("Esposende"))
        assert len(db.list_sheets_filtered()) == 2

    def test_stamped_unidade(self):
        uid = db.create_unidade("Esposende")
        s_esp = _seed_sheet(unidade_id=uid)
        _seed_sheet()  # NULL → Trofa
        got = db.list_sheets_filtered(unidade=uid)
        assert [s["id"] for s in got] == [s_esp]

    def test_null_falls_to_trofa(self):
        uid = db.create_unidade("Esposende")
        s_trofa = _seed_sheet()  # NULL
        _seed_sheet(unidade_id=uid)
        got = db.list_sheets_filtered(unidade=db.trofa_unidade_id())
        assert [s["id"] for s in got] == [s_trofa]

    def test_null_resolves_via_registered_template(self):
        # folha processada ANTES do carimbo, com template de unidade
        uid = db.create_unidade("Esposende")
        db.insert_kanban_template(f"u{uid}_corte", uid, "{}", status="ativo")
        s = _seed_sheet(template_name=f"u{uid}_corte")  # unidade_id NULL
        got = db.list_sheets_filtered(unidade=uid)
        assert [x["id"] for x in got] == [s]
        # e NÃO aparece na Trofa
        assert db.list_sheets_filtered(unidade=db.trofa_unidade_id()) == []

    def test_combines_with_other_filters(self):
        uid = db.create_unidade("Esposende")
        _seed_sheet(unidade_id=uid, operador="ANA")
        _seed_sheet(unidade_id=uid, operador="RUI")
        got = db.list_sheets_filtered(operador="ANA", unidade=uid)
        assert len(got) == 1


class TestQueuePage:
    def test_dropdown_hidden_with_single_unidade(self, client):
        _seed_sheet()
        r = client.get("/queue", headers=_DESKTOP)
        assert r.status_code == 200
        assert 'name="unidade"' not in r.text

    def test_dropdown_and_column_with_two_unidades(self, client):
        uid = db.create_unidade("Esposende")
        _seed_sheet(unidade_id=uid)
        _seed_sheet()
        r = client.get("/queue", headers=_DESKTOP)
        assert 'name="unidade"' in r.text
        assert "Esposende" in r.text
        assert ">Unidade<" in r.text  # coluna da tabela

    def test_filter_applies(self, client):
        uid = db.create_unidade("Esposende")
        s_esp = _seed_sheet(unidade_id=uid, operador="ESPOP")
        s_trofa = _seed_sheet(operador="TROFAOP")
        r = client.get(f"/queue?unidade={uid}", headers=_DESKTOP)
        # marcadores por id na tabela (os nomes aparecem sempre no dropdown)
        assert f"<b>{s_esp}</b>" in r.text
        assert f"<b>{s_trofa}</b>" not in r.text

    def test_sticky_key_registered(self, client):
        _seed_sheet()
        r = client.get("/queue", headers=_DESKTOP)
        assert "'unidade'" in r.text  # FILTER_KEYS do script sticky

    def test_garbage_param_ignored(self, client):
        _seed_sheet()
        r = client.get("/queue?unidade=abc", headers=_DESKTOP)
        assert r.status_code == 200


class TestKanbanViewerPage:
    def test_filter_applies(self, client, monkeypatch):
        monkeypatch.setattr(main, "_build_cc_maps",
                            lambda sid: ({}, {}, {}, {}, {}, {}))
        uid = db.create_unidade("Esposende")
        s_esp = _seed_sheet(unidade_id=uid, operador="ESPOP")
        s_trofa = _seed_sheet(operador="TROFAOP")
        r = client.get(f"/kanbans?unidade={uid}", headers=_DESKTOP)
        assert r.status_code == 200
        # a folha selecionada é a da unidade; a outra não aparece na navegação
        assert f"/sheet/{s_esp}/validate" in r.text
        assert f"sheet_id={s_trofa}" not in r.text
        r2 = client.get(f"/kanbans?unidade={db.trofa_unidade_id()}",
                        headers=_DESKTOP)
        assert f"/sheet/{s_trofa}/validate" in r2.text

    def test_sticky_key_registered(self, client, monkeypatch):
        monkeypatch.setattr(main, "_build_cc_maps",
                            lambda sid: ({}, {}, {}, {}, {}, {}))
        _seed_sheet()
        r = client.get("/kanbans", headers=_DESKTOP)
        assert "'unidade'" in r.text
