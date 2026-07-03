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


class TestRebuildFromRaw:
    def test_rebuild_drops_system_snaps_and_keeps_human_fields(self, tmp_db):
        sid = db.insert_sheet("test.jpg")
        raw = {
            "template_name": "expedicao",
            "header": {"operador": "OCR"},
            "footer": {},
            "rows": [{
                "cliente": "MTG",
                "ov": "250410",
                "of": "257509",
                "modelo": "CA08E10B",
            }],
        }
        db.update_extraction(sid, raw, {}, raw)
        db.apply_edit(sid, "rows[0].of", "WRONG_SYSTEM", source="system")
        db.apply_edit(sid, "rows[0].modelo", "MODELO_HUMANO", source="human")

        changed = main._rebuild_sheet_data_from_raw(sid, db.get_sheet(sid))

        assert changed is True
        sheet = db.get_sheet(sid)
        row = sheet["sheet_data"]["rows"][0]
        assert row["of"] == "257509"
        assert row["modelo"] == "MODELO_HUMANO"
        assert row["ov"] == "250410"

    def test_rebuild_skips_when_human_changed_row_structure(self, tmp_db):
        sid = db.insert_sheet("test.jpg")
        raw = {
            "template_name": "expedicao",
            "header": {},
            "footer": {},
            "rows": [{"of": "257509"}],
        }
        db.update_extraction(sid, raw, {}, raw)
        db.add_row(sid)
        db.apply_edit(sid, "rows[0].of", "WRONG_SYSTEM", source="system")

        changed = main._rebuild_sheet_data_from_raw(sid, db.get_sheet(sid))

        assert changed is False
        sheet = db.get_sheet(sid)
        assert len(sheet["sheet_data"]["rows"]) == 2
        assert sheet["sheet_data"]["rows"][0]["of"] == "WRONG_SYSTEM"


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

    def test_very_different_with_concrete_ref_auto_applies(self, tmp_db):
        """R215: ref concreta volta a substituir, mesmo em very_different."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"modelo": "OCR_ORIG"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {"engine_status": "very_different", "value": "CANONICO", "source": "plan"}

        applied = main._maybe_apply_snap(sid, "rows[0].modelo", cell, frozenset())

        assert applied is True
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "CANONICO"

    def test_marginal_write_gate_flag(self, tmp_db):
        """R236 — CROSS_WRITE_GATE_MARGINAL: OFF (default) mantém o R219
        (substitui sempre); ON bloqueia a gravação de very_different vindo de
        winner marginal (weak_guess) — a proposta fica visível, o OCR intacto.
        Caso provado: folha 2367 (encomenda fora do plano do dia)."""
        from app.config import get_settings

        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"esp": "3"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different", "value": "4",
            "source": "plan", "winner_mode": "weak_guess",
        }
        # Default OFF → aplica (política R219 intacta).
        assert main._maybe_apply_snap(sid, "rows[0].esp", cell, frozenset()) is True
        # ON → não grava.
        settings = get_settings()
        original = settings.cross_write_gate_marginal
        try:
            object.__setattr__(settings, "cross_write_gate_marginal", True)
            assert main._maybe_apply_snap(
                sid, "rows[0].esp", cell, frozenset()
            ) is False
            # Winner FORTE não é afetado pelo gate.
            strong = dict(cell, winner_mode="strong")
            assert main._maybe_apply_snap(
                sid, "rows[0].esp", strong, frozenset()
            ) is True
        finally:
            object.__setattr__(settings, "cross_write_gate_marginal", original)

    @pytest.mark.parametrize("source", [None, "ocr_raw", "syntax", "obra_concluida"])
    def test_very_different_without_concrete_ref_does_not_auto_apply(
        self, tmp_db, source
    ):
        """Sem ref concreta continua a ser revisão humana."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"modelo": "OCR_ORIG"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different",
            "value": "CANONICO",
            "source": source,
        }

        applied = main._maybe_apply_snap(sid, "rows[0].modelo", cell, frozenset())

        assert applied is False
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "OCR_ORIG"

    @pytest.mark.parametrize("source", ["plan", "sap", "maquinas", "colaboradores", "ferramenta"])
    def test_very_different_with_concrete_source_auto_applies(
        self, tmp_db, source
    ):
        """Fontes concretas seguem 30/05: escrevem o valor canónico."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"modelo": "OCR_ORIG"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different",
            "value": "CANONICO",
            "source": source,
        }

        applied = main._maybe_apply_snap(sid, "rows[0].modelo", cell, frozenset())

        assert applied is True
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "CANONICO"

    @pytest.mark.parametrize("ref_source", ["plan", "sap", "maquinas", "colaboradores"])
    def test_very_different_with_concrete_ref_source_uses_ref(
        self, tmp_db, ref_source
    ):
        """Quando o legado preserva OCR mas traz ref concreta, aplica a ref."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"modelo": "OCR_ORIG"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different",
            "value": "OCR_ORIG",
            "source": "ocr_raw",
            "ref_source": ref_source,
            "ref": "CANONICO",
        }

        applied = main._maybe_apply_snap(sid, "rows[0].modelo", cell, frozenset())

        assert applied is True
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "CANONICO"

    def test_ferramenta_review_label_is_not_auto_applied(self, tmp_db):
        """CONI inválido expõe uma regra, não um canónico determinístico."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"coni": "XYZ"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different",
            "value": "XYZ",
            "source": "ocr_raw",
            "ref_source": "ferramenta",
            "ref": "CONI, TORRES, OCT, CIL, CIO, CIB",
        }

        applied = main._maybe_apply_snap(sid, "rows[0].coni", cell, frozenset())

        assert applied is False
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["coni"] == "XYZ"

    def test_concrete_ref_auto_applies_even_for_invalid_numeric_ocr(self, tmp_db):
        """R217 (30/05): texto/lixo num campo numérico volta a ser substituído
        pela ref concreta do plan/SAP — sem o travão `auto_apply` do R216."""
        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"comp_mm": "ABC"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        cell = {
            "engine_status": "very_different",
            "value": "ABC",
            "source": "ocr_raw",
            "ref_source": "plan",
            "ref": "1200",
        }

        applied = main._maybe_apply_snap(sid, "rows[0].comp_mm", cell, frozenset())

        assert applied is True
        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["comp_mm"] == "1200"


class TestShadowRunCounters:
    def test_very_different_is_persisted_separately_from_snapped(self, tmp_db):
        sid = _mk_sheet()
        run_id = db.start_shadow_run(sid)
        scoring = {
            "summary": {
                "snapped": 2,
                "very_different": 3,
                "confirmed": 4,
                "na": 5,
                "total": 14,
            }
        }

        db.finish_shadow_run(
            run_id, sid, scoring,
            cells_total=14,
            cells_snapped=2,
            cells_confirmed=4,
            cells_na=5,
            duration_ms=9,
        )

        with db.conn() as c:
            row = c.execute(
                "SELECT cells_snapped, cells_very_different, cells_confirmed, "
                "cells_na FROM shadow_runs WHERE id = ?",
                (run_id,),
            ).fetchone()

        assert dict(row) == {
            "cells_snapped": 2,
            "cells_very_different": 3,
            "cells_confirmed": 4,
            "cells_na": 5,
        }


class TestBuildCcMapsLegacyRefs:
    def test_ref_map_uses_legacy_plan_value_when_ref_empty(self, tmp_db, monkeypatch):
        from app.cross_check import storage as cc_storage
        from app.pipeline.scoring_engine import ENGINE_VERSION

        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"modelo": "OCR"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        monkeypatch.setattr(cc_storage, "load_sheet_cross_check", lambda _sid: {
            "engine_version": ENGINE_VERSION,
            "rows": [{
                "row_index": 0,
                "fields": {
                    "modelo": {
                        "status": "NO_MATCH",
                        "value": "OCR",
                        "ref": "",
                        "plan_value": "PLAN",
                    },
                },
            }],
            "header": {},
            "footer": {},
        })

        _status, ref_map, *_ = main._build_cc_maps(sid)

        assert ref_map["rows[0].modelo"] == "PLAN"

    def test_ref_title_uses_ferramenta_source(self, tmp_db, monkeypatch):
        from app.cross_check import storage as cc_storage
        from app.pipeline.scoring_engine import ENGINE_VERSION

        sid = db.insert_sheet("test.jpg")
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"coni": "ABC"}],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)
        monkeypatch.setattr(cc_storage, "load_sheet_cross_check", lambda _sid: {
            "engine_version": ENGINE_VERSION,
            "rows": [{
                "row_index": 0,
                "fields": {
                    "coni": {
                        "status": "NO_MATCH",
                        "value": "ABC",
                        "ref": "CIB/CIL/CIO/CONI/OCT/TORRES ou número",
                        "ref_source": "ferramenta",
                    },
                },
            }],
            "header": {},
            "footer": {},
        })

        _status, ref_map, ref_title_map, *_ = main._build_cc_maps(sid)

        assert ref_map["rows[0].coni"] == "CIB/CIL/CIO/CONI/OCT/TORRES ou número"
        assert (
            ref_title_map["rows[0].coni"]
            == "Ferramenta esperada: CIB/CIL/CIO/CONI/OCT/TORRES ou número"
        )


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


class _EmptyRefsWatcher:
    def get_refs(self):
        return {
            "available": True,
            "loaded_at": "test",
            "of_to_entries": {},
            "clientes_plan": frozenset(),
            "lotes_sap_full": {},
        }


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

    def test_rebuild_from_raw_drops_old_system_snap_but_keeps_human_edit(
        self, tmp_db, monkeypatch
    ):
        monkeypatch.setattr(main, "get_watcher", lambda: _EmptyRefsWatcher())
        monkeypatch.setattr(main, "store_cross_check", lambda *a, **k: {})
        monkeypatch.setattr(main, "_spawn_shadow_scoring", lambda *a, **k: None)

        sid = db.insert_sheet("t.jpg")
        raw = {
            "template_name": "bobine_formato",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{"cliente": "RAW", "modelo": "RAW"}],
        }
        polluted = {
            "template_name": "bobine_formato",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{"cliente": "SNAP_ANTIGO", "modelo": "SNAP_ANTIGO"}],
        }
        db.update_extraction(sid, raw, {}, polluted)
        db.apply_edit(sid, "rows[0].modelo", "MODELO_HUMANO")

        main._run_and_store_cross_check(sid, rebuild_from_raw=True)

        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["cliente"] == "RAW"
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "MODELO_HUMANO"

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

    def test_invalid_numeric_ocr_is_overwritten_by_ref(self, tmp_db, monkeypatch):
        """R217 (30/05): texto/lixo num campo numérico volta a ser substituído
        pelo valor canónico do plan (substitute-everything), não fica para
        revisão como no R216."""
        monkeypatch.setattr(main, "get_watcher", lambda: _FakeWatcher())
        monkeypatch.setattr(main, "store_cross_check", lambda *a, **k: {})

        sid = db.insert_sheet("t.jpg")
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 1200 H", "comp_mm": "ABC",
            }],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)

        main._run_and_store_cross_check(sid)

        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["comp_mm"] == "1200"

    def test_acabamento_reference_is_overwritten_by_cross_check(self, tmp_db, monkeypatch):
        """Acabamento volta à política de 30/05: winner concreto substitui."""
        monkeypatch.setattr(main, "get_watcher", lambda: _FakeWatcher())
        monkeypatch.setattr(main, "store_cross_check", lambda *a, **k: {})

        sid = db.insert_sheet("t.jpg")
        sheet_data = {
            "template_name": "acabamento",
            "header": {"operador": "", "data": "10-05-2026"},
            "footer": {},
            "rows": [{
                "of": "262107",
                "modelo": "PEÇA-X",
                "qtd": "1",
            }],
        }
        db.update_extraction(sid, sheet_data, {}, sheet_data)

        main._run_and_store_cross_check(sid)

        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["rows"][0]["modelo"] == "OMEGA 1200 H"
