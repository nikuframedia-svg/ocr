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
    monkeypatch.setattr(main, "_start_sheet_cross_check", lambda *a, **k: None)
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


class TestSucataOnlyRow:
    def test_sucata_only_row_kept_in_production_rows(self, tmp_db):
        # R261 — sucata conta como conteúdo: uma linha só-com-sucata tem de
        # chegar a production_rows (e aos exports), mesmo sem OF/qtd.
        sid = _seed([
            {"of": "111", "cliente": "X", "modelo": "A", "qtd": "3"},
            {"sucata": "4"},
        ])
        assert _prod_row_count(sid) == 2
        with db.conn() as c:
            r = c.execute(
                "SELECT sucata FROM production_rows "
                "WHERE sheet_id = ? AND row_index = 1", (sid,)).fetchone()
        assert r["sucata"] == 4


class TestFechoOnlyRow:
    def test_fecho_is_normalised_and_persisted(self, tmp_db):
        sid = _seed([{"fecho": "x"}])
        with db.conn() as c:
            row = c.execute(
                "SELECT fecho FROM production_rows WHERE sheet_id = ?",
                (sid,),
            ).fetchone()
        assert row["fecho"] == "X"


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

    def test_add_row_queues_cross_check_without_running_inline(
        self, tmp_db, isolate, monkeypatch, client
    ):
        sid = _seed([{"of": "111"}])
        queued: list[tuple[tuple[int, ...], dict]] = []

        def fail_inline(*args, **kwargs):
            raise AssertionError("cross-check ran inline")

        monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
        monkeypatch.setattr(
            main,
            "_start_sheet_cross_check",
            lambda sheet_ids, **kwargs: queued.append((tuple(sorted(sheet_ids)), kwargs)),
        )

        r = client.post(f"/sheet/{sid}/add-row", headers=_DESKTOP)

        assert r.status_code == 200
        assert queued == [((sid,), {"profile_trigger": "add_row"})]

    def test_remove_row_queues_cross_check_without_running_inline(
        self, tmp_db, isolate, monkeypatch, client
    ):
        sid = _seed([{"of": "111"}, {"of": "222"}])
        queued: list[tuple[tuple[int, ...], dict]] = []

        def fail_inline(*args, **kwargs):
            raise AssertionError("cross-check ran inline")

        monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
        monkeypatch.setattr(
            main,
            "_start_sheet_cross_check",
            lambda sheet_ids, **kwargs: queued.append((tuple(sorted(sheet_ids)), kwargs)),
        )

        r = client.post(f"/sheet/{sid}/remove-row", json={"row_index": 0}, headers=_DESKTOP)

        assert r.status_code == 200
        assert queued == [((sid,), {"profile_trigger": "remove_row"})]

    def test_apply_of_entry_batches_and_queues_cross_check_without_running_inline(
        self, tmp_db, monkeypatch, client
    ):
        sid = _seed([{"of": "", "cliente": "", "modelo": ""}])
        queued: list[tuple[tuple[int, ...], dict]] = []
        refs = {
            "of_to_entries": {
                "262892": [
                    {
                        "cliente": "MTG",
                        "ov": "450001",
                        "designacao": "CGC2E10D",
                        "comp": 5000,
                        "lbase": 120,
                        "ltopo": 100,
                        "esp": 3,
                    }
                ]
            }
        }

        class _Watcher:
            def get_refs(self):
                return refs

        def fail_inline(*args, **kwargs):
            raise AssertionError("cross-check ran inline")

        monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())
        monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
        monkeypatch.setattr(
            main,
            "_start_sheet_cross_check",
            lambda sheet_ids, **kwargs: queued.append((tuple(sorted(sheet_ids)), kwargs)),
        )

        r = client.post(
            f"/sheet/{sid}/apply-of-entry",
            json={"row_index": 0, "of": "262892", "entry_idx": 0},
            headers=_DESKTOP,
        )

        assert r.status_code == 200
        assert r.json()["n_applied"] == 8
        row = db.get_sheet(sid)["sheet_data"]["rows"][0]
        assert row["of"] == "262892"
        assert row["cliente"] == "MTG"
        assert row["modelo"] == "CGC2E10D"
        assert row["comp_mm"] == "5000"
        assert queued == [((sid,), {"profile_trigger": "apply_of_entry"})]

    def test_apply_of_entry_preserves_human_edited_field(
        self, tmp_db, monkeypatch, client
    ):
        """R260 — a varinha nunca clobra uma correção manual da mesma linha.
        O operador corrige o modelo à mão; aplicar uma entrada do plano com
        modelo diferente preserva o valor humano e aplica os restantes."""
        sid = _seed([{"of": "", "cliente": "", "modelo": ""}])
        # Correção manual do operador (source='human' por defeito).
        db.apply_edit(sid, "rows[0].modelo", "CGC2E10D_ESPECIAL")
        refs = {
            "of_to_entries": {
                "262892": [{
                    "cliente": "MTG", "ov": "450001", "designacao": "CGC2E10D",
                    "comp": 5000, "lbase": 120, "ltopo": 100, "esp": 3,
                }]
            }
        }

        class _Watcher:
            def get_refs(self):
                return refs

        monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())
        monkeypatch.setattr(main, "_start_sheet_cross_check", lambda *a, **k: None)

        r = client.post(
            f"/sheet/{sid}/apply-of-entry",
            json={"row_index": 0, "of": "262892", "entry_idx": 0},
            headers=_DESKTOP,
        )

        assert r.status_code == 200
        body = r.json()
        assert "modelo" in body["kept"]           # correção manual preservada
        assert body["n_applied"] == 7             # os outros 7 aplicados
        row = db.get_sheet(sid)["sheet_data"]["rows"][0]
        assert row["modelo"] == "CGC2E10D_ESPECIAL"   # NÃO revertido ao plano
        assert row["of"] == "262892"
        assert row["cliente"] == "MTG"
        # R260 — a correção manual continua protegida; os campos aplicados pela
        # varinha são source='system' (valores do plano, não digitados) para
        # não poluir as métricas/learning que filtram source='human'.
        prot = main._human_edited_paths(sid)
        assert "rows[0].modelo" in prot        # correção manual preservada
        assert "rows[0].of" not in prot        # aplicado pela varinha = system

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


class TestConiCanonicalisation:
    """R134 — o `coni` (FERRAMENTA/CONI) é canonicalizado no write path real.

    db.py `_sync_production_rows` é o ÚNICO ponto vivo de normalização do valor
    persistido (o ocr_runner não valida e o export CPIS lê verbatim). Este teste
    cobre o caminho db.update_extraction → production_rows e falha se essa
    canonicalização for revertida.
    """

    def test_coni_canonicalised_on_production_rows_write(self, tmp_db):
        sid = _seed([
            {"of": "111", "modelo": "A", "qtd": "1", "coni": "Coni"},
            {"of": "222", "modelo": "B", "qtd": "1", "coni": "OCT."},
            {"of": "333", "modelo": "C", "qtd": "1", "coni": "T"},
        ])
        with db.conn() as c:
            got = [r["coni"] for r in c.execute(
                "SELECT coni FROM production_rows WHERE sheet_id = ? ORDER BY of",
                (sid,)).fetchall()]
        # válidos → canónico; inválido 'T' fica raw (não inventa snap, vai a revisão)
        assert got == ["CONI", "OCT", "T"]
