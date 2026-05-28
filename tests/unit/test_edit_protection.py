"""R133 — protecção de edições humanas contra o auto-overwrite.

Bug reportado: operador edita uma célula e o valor "não guarda" — o
re-cross-check disparado por `sheet_edit` reescrevia logo o campo com o
valor canónico do plan (`_apply_auto_overwrites` / `_apply_operador_snap`
/ `_apply_codmaq_fill`, source='system'). O fix protege os campos cuja
última edição é `source='human'`.

Estes testes isolam a DB num ficheiro temporário (monkeypatch `_DB_PATH`)
para não tocar no `data/app.db` tracked.
"""
from __future__ import annotations

import pytest

from app.web import db
from app.web import main


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """DB SQLite isolada por teste."""
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


def _mk_sheet() -> int:
    """Cria uma sheet (satisfaz a FK edits→sheets) e devolve o id."""
    return db.insert_sheet("t.jpg")


def _insert_edit(sheet_id: int, field_path: str, value: str, source: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (sheet_id, field_path, "", value, source),
        )


class TestHumanEditedPaths:
    def test_empty_when_no_edits(self, tmp_db):
        sid = _mk_sheet()
        assert main._human_edited_paths(sid) == frozenset()

    def test_returns_human_edited_path(self, tmp_db):
        sid = _mk_sheet()
        _insert_edit(sid, "rows[0].modelo", "VALOR_HUMANO", "human")
        assert "rows[0].modelo" in main._human_edited_paths(sid)

    def test_system_edit_not_protected(self, tmp_db):
        sid = _mk_sheet()
        _insert_edit(sid, "rows[0].comp_mm", "5000", "system")
        assert "rows[0].comp_mm" not in main._human_edited_paths(sid)

    def test_last_edit_wins_human_over_system(self, tmp_db):
        # sistema escreveu, depois o humano corrigiu → protegido
        sid = _mk_sheet()
        _insert_edit(sid, "rows[0].of", "246250", "system")
        _insert_edit(sid, "rows[0].of", "263361", "human")
        assert "rows[0].of" in main._human_edited_paths(sid)

    def test_last_edit_wins_system_over_human(self, tmp_db):
        # humano escreveu, depois o sistema reescreveu → NÃO protegido
        # (cenário improvável com o fix, mas a regra "última edição ganha"
        # tem de ser coerente).
        sid = _mk_sheet()
        _insert_edit(sid, "rows[0].esp", "3", "human")
        _insert_edit(sid, "rows[0].esp", "2,6", "system")
        assert "rows[0].esp" not in main._human_edited_paths(sid)

    def test_scoped_to_sheet(self, tmp_db):
        sid1 = _mk_sheet()
        sid2 = _mk_sheet()
        _insert_edit(sid1, "rows[0].modelo", "A", "human")
        _insert_edit(sid2, "rows[1].cliente", "B", "human")
        assert main._human_edited_paths(sid1) == frozenset({"rows[0].modelo"})
        assert main._human_edited_paths(sid2) == frozenset({"rows[1].cliente"})


class TestMaybeApplySnapProtected:
    """A guarda `protected` em `_maybe_apply_snap` devolve False sem tocar
    na DB (a célula protegida nunca é auto-substituída)."""

    def test_protected_field_not_applied(self, tmp_db):
        cell = {"engine_status": "snapped", "value": "CANONICO", "source": "plan"}
        applied = main._maybe_apply_snap(
            1, "rows[0].modelo", cell, frozenset({"rows[0].modelo"})
        )
        assert applied is False

    def test_unprotected_snapped_would_apply(self, tmp_db):
        # Sem protecção, um snapped com valor concreto aplica (escreve edit
        # source='system'). Precisa de uma sheet real com sheet_data porque
        # db.apply_edit lê o sheet_data antes de gravar.
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"modelo": "OCR_ORIG"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {"engine_status": "snapped", "value": "CANONICO", "source": "plan"}
        applied = main._maybe_apply_snap(sid, "rows[0].modelo", cell, frozenset())
        assert applied is True
        # O edit ficou registado como 'system' → NÃO protege futuros snaps.
        assert "rows[0].modelo" not in main._human_edited_paths(sid)
        # E o sheet_data foi de facto reescrito para o canónico.
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "CANONICO"


# Refs sintéticas (espelham test_scoring_engine) — plan com OF 262107.
_REFS = {
    "available": True,
    "loaded_at": "test",
    "of_to_entries": {
        "262107": [{
            "ov": "2410001", "cliente": "ELECNOR",
            "designacao": "OMEGA 1200 H", "comp": 1200, "larg": 250,
            "lbase": 50, "ltopo": 30, "esp": 2.6,
        }],
    },
    "of_to_ovs": {"262107": frozenset({"2410001"})},
    "lotes_sap_full": {},
    "clientes_plan": frozenset({"ELECNOR"}),
}


class _FakeWatcher:
    def get_refs(self):
        return _REFS


class TestEditPersistsEndToEnd:
    """Prova end-to-end do bug 'escrevo mas não guarda': um edit humano
    sobrevive ao `_run_and_store_cross_check` (que antes revertia)."""

    def test_human_modelo_edit_survives_re_cross_check(self, tmp_db, monkeypatch):
        monkeypatch.setattr(main, "get_watcher", lambda: _FakeWatcher())
        monkeypatch.setattr(main, "store_cross_check", lambda *a, **k: {})

        sid = db.insert_sheet("t.jpg")
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 1200 H", "comp_mm": "1200",
            }],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)

        # Operador corrige o modelo à mão (source='human').
        db.apply_edit(sid, "rows[0].modelo", "MODELO_DO_OPERADOR")

        # Re-corre o cross-check (como faz o endpoint sheet_edit).
        main._run_and_store_cross_check(sid)

        # FIX R133: o valor humano TEM de persistir (antes era revertido
        # para a designação do plan "OMEGA 1200 H").
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "MODELO_DO_OPERADOR"

    def test_auto_overwrite_still_works_without_human_edit(self, tmp_db, monkeypatch):
        """Não regredir R130-R132: sem edit humano, o auto-overwrite continua
        a alinhar o modelo OCR pela designação do plan."""
        monkeypatch.setattr(main, "get_watcher", lambda: _FakeWatcher())
        monkeypatch.setattr(main, "store_cross_check", lambda *a, **k: {})

        sid = db.insert_sheet("t.jpg")
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 12OO H",  # OCR com O→0 — será alinhado  # noqa: RUF001
                "comp_mm": "1200",
            }],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)

        main._run_and_store_cross_check(sid)

        # Sem edit humano, o motor alinha o modelo pela designação canónica.
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "OMEGA 1200 H"
