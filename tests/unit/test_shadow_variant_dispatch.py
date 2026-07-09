"""R257 — dispatch da variante na thread de sombra (_spawn_shadow_scoring).

Bug corrigido: o dispatch comparava com o literal "next" (era R250), pelo que
CROSS_SHADOW_VARIANT=v30cal caía silenciosamente no default v30. Como o
ranking v30cal é byte-idêntico ao v30, o soak do R255 compararia v30-vs-v30
e daria luz verde FALSA ao flip. Estes testes cravam o contrato: a thread de
sombra corre EXATAMENTE a variante configurada; "current" (default) mantém o
default do processo.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.pipeline import scoring_engine
from app.web import db, main


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


def _run_shadow_and_capture_variant(monkeypatch, configured: str) -> str:
    """Corre _spawn_shadow_scoring com CROSS_SHADOW_VARIANT=``configured`` e
    devolve a variante que a thread de sombra REALMENTE usou."""
    seen: dict[str, str] = {}
    done = threading.Event()

    def _fake_shadow_score(_sd, _da, _refs):
        seen["variant"] = scoring_engine.scoring_variant()
        done.set()
        scoring = {"summary": {"snapped": 0, "very_different": 0,
                               "confirmed": 0, "na": 0, "total": 0}}
        return scoring, 0, 0, 0, 0, 1

    monkeypatch.setattr(scoring_engine, "shadow_score", _fake_shadow_score)
    monkeypatch.setattr(main, "get_settings",
                        lambda: SimpleNamespace(cross_shadow_variant=configured))
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)

    sheet_id = db.insert_sheet("shadow_dispatch_test.jpg")
    main._spawn_shadow_scoring(sheet_id, {"header": {}, "rows": []}, None, {})
    assert done.wait(timeout=10), "thread de sombra não correu"
    return seen["variant"]


class TestShadowVariantDispatch:
    def test_v30cal_runs_v30cal(self, tmp_db, monkeypatch):
        # O caso do soak R255 — antes do R257 isto devolvia "v30".
        assert _run_shadow_and_capture_variant(monkeypatch, "v30cal") == "v30cal"

    def test_next_still_dispatches(self, tmp_db, monkeypatch):
        # Compat com o A/B original do R250.
        assert _run_shadow_and_capture_variant(monkeypatch, "next") == "next"

    def test_current_keeps_process_default(self, tmp_db, monkeypatch):
        # "current" = auditoria clássica: a sombra corre a MESMA variante da
        # produção (default do ContextVar, "v30" salvo env override).
        default = scoring_engine.scoring_variant()
        assert _run_shadow_and_capture_variant(monkeypatch, "current") == default

    def test_blank_keeps_process_default(self, tmp_db, monkeypatch):
        default = scoring_engine.scoring_variant()
        assert _run_shadow_and_capture_variant(monkeypatch, "") == default
