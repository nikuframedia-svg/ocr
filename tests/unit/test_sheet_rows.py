"""R136 — adicionar / remover linhas na tabela de produção antes de validar.

Cobre os pedidos do utilizador (issue-tracker rows 22/23): poder acrescentar
uma linha que o OCR não apanhou e remover linhas erradas/inventadas.

  - db.add_row / db.delete_row: mutação estrutural de sheet_data["rows"] com
    audit trail (edits) e re-sync de production_rows.
  - endpoints /add-row e /remove-row: gating (desktop, não validada) + JSON.

DB isolada por teste (monkeypatch `_DB_PATH`); efeitos de disco do cross-check
neutralizados por monkeypatch (não toca em data/ nem kanban_refs/).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db
from app.web import main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def isolate(monkeypatch):
    """Refs disponíveis + neutraliza efeitos de disco do cross-check."""
    refs = {"available": True, "loaded_at": "test", "colaboradores": {}, "operador_aliases": {}}

    class _Watcher:
        def get_refs(self):
            return refs

    monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())
    monkeypatch.setattr(main, "cross_check_sheet", lambda *a, **k: {})
    monkeypatch.setattr(main, "store_cross_check", lambda **k: None)
    monkeypatch.setattr(main, "_spawn_shadow_scoring", lambda *a, **k: None)
    monkeypatch.setattr(main, "_apply_auto_overwrites", lambda *a, **k: 0)
    monkeypatch.setattr(main, "_apply_operador_snap", lambda *a, **k: 0)
    monkeypatch.setattr(main, "_apply_codmaq_fill", lambda *a, **k: 0)
    return refs


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed(rows):
    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {"operador": "AUGUSTO MONTEIRO", "n_operador": "95",
                   "data": "20-05-2026", "setor_maquina": "", "cod_maquina": ""},
        "rows": rows,
        "footer": {},
    }
    db.update_extraction(sid, raw_extraction=sheet_data,
                         dq_audit={"cells": {}}, sheet_data=sheet_data)
    return sid


def _edits(sheet_id):
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT field_path, old_value, new_value, source FROM edits "
            "WHERE sheet_id = ? ORDER BY id", (sheet_id,)).fetchall()]


def _prod_row_count(sheet_id):
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM production_rows WHERE sheet_id = ?",
            (sheet_id,)).fetchone()["n"]


class TestAddRow:
    def test_add_row_appends_empty_and_returns_index(self, tmp_db):
        sid = _seed([{"of": "111", "modelo": "A"}, {"of": "222", "modelo": "B"}])
        new_idx = db.add_row(sid)
        assert new_idx == 2
        rows = db.get_sheet(sid)["sheet_data"]["rows"]
        assert len(rows) == 3
        assert rows[2] == {}

    def test_add_row_logs_audit(self, tmp_db):
        sid = _seed([{"of": "111"}])
        db.add_row(sid)
        edits = _edits(sid)
        assert any(e["field_path"] == "rows[1]" and e["new_value"] == "(linha adicionada)"
                   and e["source"] == "human" for e in edits)

    def test_empty_added_row_absent_from_production_rows(self, tmp_db):
        # 1 linha preenchida + 1 vazia → só a preenchida conta nos aggregates.
        sid = _seed([{"of": "111", "cliente": "X", "modelo": "A", "qtd": "3"}])
        before = _prod_row_count(sid)
        db.add_row(sid)
        assert _prod_row_count(sid) == before  # a linha vazia é filtrada


class TestDeleteRow:
    def test_delete_row_removes_correct_one_and_shifts(self, tmp_db):
        sid = _seed([{"of": "111"}, {"of": "222"}, {"of": "333"}])
        removed = db.delete_row(sid, 1)
        assert removed == {"of": "222"}
        rows = db.get_sheet(sid)["sheet_data"]["rows"]
        assert [r["of"] for r in rows] == ["111", "333"]  # 333 shifted to index 1

    def test_delete_row_logs_removed_content(self, tmp_db):
        sid = _seed([{"of": "111"}, {"of": "222"}])
        db.delete_row(sid, 0)
        edits = _edits(sid)
        e = next(x for x in edits if x["field_path"] == "rows[0]"
                 and x["new_value"] == "(linha removida)")
        assert '"111"' in e["old_value"]  # conteúdo removido fica no audit

    def test_delete_row_out_of_range_raises(self, tmp_db):
        sid = _seed([{"of": "111"}])
        with pytest.raises(ValueError):
            db.delete_row(sid, 5)


class TestEndpoints:
    def test_add_row_endpoint(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}])
        r = client.post(f"/sheet/{sid}/add-row", headers=_DESKTOP)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "row_index": 1}
        assert len(db.get_sheet(sid)["sheet_data"]["rows"]) == 2

    def test_remove_row_endpoint(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}, {"of": "222"}])
        r = client.post(f"/sheet/{sid}/remove-row", json={"row_index": 0}, headers=_DESKTOP)
        assert r.status_code == 200
        assert r.json()["ok"] is True
        rows = db.get_sheet(sid)["sheet_data"]["rows"]
        assert [x["of"] for x in rows] == ["222"]

    def test_remove_row_out_of_range_400(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}])
        r = client.post(f"/sheet/{sid}/remove-row", json={"row_index": 9}, headers=_DESKTOP)
        assert r.status_code == 400

    def test_add_row_blocked_when_validated(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}])
        db.validate_sheet(sid, "AUGUSTO MONTEIRO")
        r = client.post(f"/sheet/{sid}/add-row", headers=_DESKTOP)
        assert r.status_code == 409
        assert len(db.get_sheet(sid)["sheet_data"]["rows"]) == 1

    def test_remove_row_blocked_when_validated(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}, {"of": "222"}])
        db.validate_sheet(sid, "AUGUSTO MONTEIRO")
        r = client.post(f"/sheet/{sid}/remove-row", json={"row_index": 0}, headers=_DESKTOP)
        assert r.status_code == 409
        assert len(db.get_sheet(sid)["sheet_data"]["rows"]) == 2


class TestRender:
    """A página renderiza os botões antes de validar e esconde-os depois."""

    def test_buttons_present_before_validation(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}])
        r = client.get(f"/sheet/{sid}", headers=_DESKTOP)
        assert r.status_code == 200
        assert "+ Adicionar linha" in r.text
        assert "/add-row" in r.text
        assert "/remove-row" in r.text

    def test_buttons_hidden_when_validated(self, tmp_db, isolate, client):
        sid = _seed([{"of": "111"}])
        db.validate_sheet(sid, "AUGUSTO MONTEIRO")
        r = client.get(f"/sheet/{sid}", headers=_DESKTOP)
        assert r.status_code == 200
        assert "/add-row" not in r.text
        assert "/remove-row" not in r.text
