"""R138 — o wizard "Corrigir via OF" marcava ~92% das linhas do plan como
fechadas porque `remaining` usava MAX(fases): as fases iniciais (bf/corte)
sobre-produzem (margem de sucata), logo max ≥ quanttrp quase sempre.

A correcção torna o "produzido" consciente do setor: usa a fase do kanban
(setor→colunaexcel) e, sem mapeamento, a fase mais a jusante (expedição).

Caso real reproduzido: OF 254877, quanttrp=20, fases bf=c=q=20 mas
s=r=a=exp=10 (acabamento/expedição a meio) — antes "fechada", agora activa.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db
from app.web import main
from app.pipeline import of_consumption as ofc

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Fases do caso-prova: corte completo (20), acabamento/expedição a meio (10).
_ENTRY = {
    "_of": "262892", "of": "262892", "designacao": "CGC2E10D",
    "quanttrp": 20,
    "fases": {"bf": 20, "c": 20, "q": 20, "s": 10, "r": 10, "a": 10, "exp": 10},
    "fechado": "0",
}


class TestRemainingPhaseAware:
    def test_phase_acabamento_is_active(self):
        # phase 'a' (acabamento) = 10 → faltam 10. ANTES (max=20) dava 0.
        assert ofc.remaining(_ENTRY, {}, phase="a") == 10.0

    def test_phase_corte_is_done(self):
        # phase 'c' (corte) = 20 = quanttrp → concluída para o corte.
        assert ofc.remaining(_ENTRY, {}, phase="c") == 0.0

    def test_no_phase_falls_back_to_expedicao(self):
        # Sem setor mapeado → fase mais a jusante (exp=10), não o MAX.
        assert ofc.remaining(_ENTRY, {}, phase=None) == 10.0

    def test_max_would_have_marked_done(self):
        # Regressão explícita: o antigo MAX(fases) dava remaining 0 (fechada).
        assert ofc._max_phase(_ENTRY["fases"]) == 20.0
        assert 20 - ofc._max_phase(_ENTRY["fases"]) == 0  # o bug

    def test_fechado_flag_still_zero(self):
        e = {**_ENTRY, "fechado": "1"}
        assert ofc.remaining(e, {}, phase="a") == 0.0

    def test_missing_quanttrp_is_inf(self):
        e = {**_ENTRY, "quanttrp": None}
        assert ofc.remaining(e, {}, phase="a") == float("inf")


class TestSortPhaseAware:
    def test_active_line_kept_for_its_sector(self, monkeypatch):
        monkeypatch.setattr(ofc, "get_consumption", lambda: {})
        out = ofc.sort_entries_by_remaining([dict(_ENTRY)], include_done=False, phase="a")
        assert len(out) == 1
        assert out[0]["_done"] is False
        assert out[0]["_remaining"] == 10.0

    def test_done_for_corte_filtered_by_default(self, monkeypatch):
        monkeypatch.setattr(ofc, "get_consumption", lambda: {})
        out = ofc.sort_entries_by_remaining([dict(_ENTRY)], include_done=False, phase="c")
        assert out == []  # remaining 0 → escondida por defeito
        out_all = ofc.sort_entries_by_remaining([dict(_ENTRY)], include_done=True, phase="c")
        assert len(out_all) == 1 and out_all[0]["_done"] is True


class TestOfLookupEndpoint:
    @pytest.fixture()
    def tmp_db(self, tmp_path, monkeypatch):
        test_db = tmp_path / "test_app.db"
        monkeypatch.setattr(db, "_DB_PATH", test_db)
        db.init_db()
        return test_db

    def test_acabamento_sheet_sees_active_line(self, tmp_db, monkeypatch):
        # Folha de ACABAMENTO → _current_phase resolve a fase 'a'.
        sid = db.insert_sheet("t.jpg")
        sheet_data = {
            "header": {"operador": "X", "n_operador": "1", "data": "20-05-2026",
                       "setor_maquina": "ACABAMENTO MTG4", "cod_maquina": "M061"},
            "rows": [{"of": "262892", "modelo": "CGC2E10D", "qtd": "2"}],
            "footer": {},
        }
        db.update_extraction(sid, raw_extraction=sheet_data,
                             dq_audit={"cells": {}}, sheet_data=sheet_data)

        refs = {
            "of_to_entries": {"262892": [dict(_ENTRY)]},
            "plan_by_ov": {}, "plan_by_modelo_ft": {},
            "maquinas_by_kanban": {"ACABAMENTO MTG4": {"colunaexcel": "a"}},
        }

        class _W:
            def get_refs(self):
                return refs

        monkeypatch.setattr(main, "get_watcher", lambda: _W())
        monkeypatch.setattr(ofc, "get_consumption", lambda: {})

        client = TestClient(main.app)
        r = client.get(f"/sheet/{sid}/of-lookup?q=262892&include_done=1", headers=_DESKTOP)
        assert r.status_code == 200
        j = r.json()
        assert j["found"] is True and j["mode"] == "of"
        e = j["entries"][0]
        assert e["done"] is False        # ANTES: True (max=20)
        assert e["remaining"] == 10.0    # acabamento: faltam 10 de 20
        assert e["quanttrp"] == 20.0
