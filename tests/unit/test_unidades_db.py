"""Unidades fabris + kanban_templates — camada de dados (Task C, E2)."""

import sqlite3

import pytest

from app.web import db


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    return tmp_path / "app.db"


class TestInitDb:
    def test_init_db_idempotent(self, tmp_db):
        # Correr 2x não pode falhar nem duplicar o seed.
        db.init_db()
        unidades = db.list_unidades()
        assert [u["nome"] for u in unidades] == ["Trofa"]

    def test_seed_trofa(self, tmp_db):
        unidades = db.list_unidades()
        assert len(unidades) == 1
        assert unidades[0]["nome"] == "Trofa"
        assert unidades[0]["ativo"] == 1

    def test_trofa_unidade_id(self, tmp_db):
        assert db.trofa_unidade_id() == db.list_unidades()[0]["id"]

    def test_sheets_has_unidade_id(self, tmp_db):
        with db.conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sheets)")}
        assert "unidade_id" in cols


class TestUnidadesCrud:
    def test_create_and_list(self, tmp_db):
        uid = db.create_unidade("Esposende")
        assert uid > 0
        nomes = [u["nome"] for u in db.list_unidades()]
        assert nomes == ["Esposende", "Trofa"]  # ordem alfabética NOCASE

    def test_unique_nocase(self, tmp_db):
        db.create_unidade("Esposende")
        with pytest.raises(sqlite3.IntegrityError):
            db.create_unidade("ESPOSENDE")

    def test_nome_vazio(self, tmp_db):
        with pytest.raises(ValueError):
            db.create_unidade("   ")

    def test_nome_com_espacos_e_normalizado(self, tmp_db):
        uid = db.create_unidade("  Vila do Conde  ")
        u = [x for x in db.list_unidades() if x["id"] == uid][0]
        assert u["nome"] == "Vila do Conde"

    def test_toggle_ativo(self, tmp_db):
        uid = db.create_unidade("Esposende")
        db.set_unidade_ativo(uid, False)
        assert [u["nome"] for u in db.list_unidades()] == ["Trofa"]
        todas = db.list_unidades(only_ativo=False)
        assert {u["nome"] for u in todas} == {"Esposende", "Trofa"}
        db.set_unidade_ativo(uid, True)
        assert len(db.list_unidades()) == 2


class TestSheetUnidade:
    def test_set_sheet_unidade(self, tmp_db):
        with db.conn() as c:
            c.execute(
                "INSERT INTO sheets (image_path, status) VALUES (?, ?)",
                ("x.jpg", "uploaded"),
            )
            sheet_id = c.execute("SELECT id FROM sheets").fetchone()["id"]
        uid = db.create_unidade("Esposende")
        db.set_sheet_unidade(sheet_id, uid)
        with db.conn() as c:
            r = c.execute("SELECT unidade_id FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
        assert r["unidade_id"] == uid


class TestKanbanTemplates:
    def _mk(self, status="draft"):
        uid = db.create_unidade("Esposende")
        tid = db.insert_kanban_template(
            f"u{uid}_corte", uid, '{"name": "corte"}', image_path="tpl.jpg",
            status=status,
        )
        return uid, tid

    def test_insert_and_get(self, tmp_db):
        uid, tid = self._mk()
        t = db.get_kanban_template(tid)
        assert t is not None
        assert t["name"] == f"u{uid}_corte"
        assert t["unidade_id"] == uid
        assert t["status"] == "draft"
        assert t["activated_at"] is None

    def test_get_missing(self, tmp_db):
        assert db.get_kanban_template(999) is None

    def test_name_unique(self, tmp_db):
        uid, _ = self._mk()
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_kanban_template(f"u{uid}_corte", uid, "{}")

    def test_list_with_unidade_nome(self, tmp_db):
        self._mk()
        rows = db.list_kanban_templates()
        assert len(rows) == 1
        assert rows[0]["unidade_nome"] == "Esposende"

    def test_list_filter_status(self, tmp_db):
        self._mk(status="ativo")
        assert db.list_kanban_templates(status="draft") == []
        assert len(db.list_kanban_templates(status="ativo")) == 1

    def test_update_spec(self, tmp_db):
        _, tid = self._mk()
        db.update_kanban_template_spec(tid, '{"name": "corte", "v": 2}')
        t = db.get_kanban_template(tid)
        assert '"v": 2' in t["spec_json"]

    def test_status_transition_sets_activated_at(self, tmp_db):
        _, tid = self._mk()
        db.set_kanban_template_status(tid, "a_analisar")
        assert db.get_kanban_template(tid)["activated_at"] is None
        db.set_kanban_template_status(tid, "ativo")
        assert db.get_kanban_template(tid)["activated_at"] is not None

    def test_status_with_discovery_json(self, tmp_db):
        _, tid = self._mk()
        db.set_kanban_template_status(tid, "analisado", discovery_json='{"raw": 1}')
        t = db.get_kanban_template(tid)
        assert t["status"] == "analisado"
        assert t["discovery_json"] == '{"raw": 1}'

    def test_fk_unidade_enforced(self, tmp_db):
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_kanban_template("u99_x", 99, "{}")
