"""R137 — filtros "sticky" no Histórico (/queue) e Kanban Viewer (/kanbans).

Os filtros desapareciam ao abrir um kanban e voltar (browser back, "← Voltar",
menu lateral ou redirect pós-validação caem em URLs sem querystring). A solução
guarda os filtros em sessionStorage e re-aplica-os ao aterrar sem filtros.

O comportamento JS verifica-se manualmente (não há JS runner no repo); aqui
asseguramos por render que o script sticky e o clear-on-Limpar estão presentes,
e que o filtro DATA CAPTURA deixou de cair do `_qs` (round-trip completo).

DB isolada por teste; efeitos de disco do cross-check neutralizados.
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
    """Refs disponíveis + neutraliza efeitos de disco do cross-check
    (o render de /kanbans com folha chama _build_cc_maps)."""
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


def _seed(operador="AUGUSTO MONTEIRO"):
    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {"operador": operador, "n_operador": "95", "data": "20-05-2026",
                   "setor_maquina": "", "cod_maquina": ""},
        "rows": [{"of": "111", "cliente": "X", "modelo": "A", "qtd": "3"}],
        "footer": {},
    }
    db.update_extraction(sid, raw_extraction=sheet_data,
                         dq_audit={"cells": {}}, sheet_data=sheet_data)
    return sid


class TestQueueSticky:
    def test_sticky_script_present(self, tmp_db, isolate, client):
        r = client.get("/queue", headers=_DESKTOP)
        assert r.status_code == 200
        assert "sessionStorage" in r.text
        assert "queue_filters" in r.text
        assert "location.replace" in r.text

    def test_limpar_clears_storage(self, tmp_db, isolate, client):
        # operador_filter activa o link "Limpar filtros".
        r = client.get("/queue?operador=ZZZ", headers=_DESKTOP)
        assert r.status_code == 200
        assert "sessionStorage.removeItem('queue_filters')" in r.text

    def test_captured_filter_in_qs(self, tmp_db, isolate, client):
        # Regressão: a DATA CAPTURA caía do _qs (e do back-url).
        r = client.get("/queue?captured=2026-05-20", headers=_DESKTOP)
        assert r.status_code == 200
        assert "captured=2026-05-20" in r.text


class TestKanbansSticky:
    def test_sticky_script_present(self, tmp_db, isolate, client):
        r = client.get("/kanbans", headers=_DESKTOP)
        assert r.status_code == 200
        assert "sessionStorage" in r.text
        assert "kanbans_filters" in r.text
        assert "location.replace" in r.text

    def test_limpar_clears_storage(self, tmp_db, isolate, client):
        _seed(operador="AUGUSTO MONTEIRO")
        r = client.get("/kanbans?operador=AUGUSTO%20MONTEIRO", headers=_DESKTOP)
        assert r.status_code == 200
        assert "sessionStorage.removeItem('kanbans_filters')" in r.text
