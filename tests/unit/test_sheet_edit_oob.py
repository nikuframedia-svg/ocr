"""R136 — uma só fonte de verdade para data/operador no cabeçalho + nome
do operador a actualizar quando se muda o nº.

Cobre os dois pontos reportados pelo utilizador:

  Point #2 — editar `header.n_operador` devolve um swap *out-of-band* da
  célula `header.operador` com o nome canónico resolvido por
  `_apply_operador_snap` (antes só mudava na BD, no ecrã ficava o nome
  antigo até refrescar a página).

  Point #1 — (a) a célula `header.data` edita-se com `<input type="date">`
  (ISO) mas grava-se em DD-MM-YYYY; (b) `/validate` lê data/nº/operador do
  CABEÇALHO (não de inputs próprios duplicados) e rejeita um cabeçalho
  inválido — deixou de reverter edições ao validar.

DB isolada por teste (monkeypatch `_DB_PATH`), tal como em
``test_edit_protection.py``. Os efeitos de disco do cross-check (store +
shadow + factory CSV + alias lexicon) são neutralizados por monkeypatch
para o teste não tocar em ``data/`` nem ``kanban_refs/``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db
from app.web import main

# Colaboradores sintéticos (shape de _mine_colaboradores: sname UPPER ASCII).
_COLABS = {
    1162: {"sname": "HELDER FERNANDES", "pernr": "10001162"},
    95: {"sname": "AUGUSTO MONTEIRO", "pernr": "10000095"},
}

# UA desktop — /edit e /validate rejeitam mobile.
_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def isolate(monkeypatch):
    """Refs sintéticos + neutraliza efeitos de disco do cross-check.

    Mantém `_apply_operador_snap` REAL (é o que se está a testar); só
    desliga auto-overwrites/codmaq e a persistência em disco.
    """
    refs = {
        "available": True,
        "loaded_at": "test",
        "colaboradores": _COLABS,
        "operador_aliases": {},
    }

    class _Watcher:
        def get_refs(self):
            return refs

    monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())
    monkeypatch.setattr(main, "cross_check_sheet", lambda *a, **k: {})
    monkeypatch.setattr(main, "store_cross_check", lambda **k: None)
    monkeypatch.setattr(main, "_spawn_shadow_scoring", lambda *a, **k: None)
    monkeypatch.setattr(main, "_build_cc_maps", lambda *a, **k: ({}, {}, {}, {}, {}, {}))
    monkeypatch.setattr(main, "_apply_auto_overwrites", lambda *a, **k: 0)
    monkeypatch.setattr(main, "_apply_codmaq_fill", lambda *a, **k: 0)
    monkeypatch.setattr(main, "_maybe_record_operador_alias", lambda sid: None)
    monkeypatch.setattr(main, "_start_sheet_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda sid: None)
    # Não tocar em ficheiros tracked (data/events.jsonl, kernel_state.json).
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr("app.learning.scheduler.maybe_trigger_learning",
                        lambda *a, **k: None)
    return refs


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed(operador="HELENA FERNANDEZ", n_operador="1162", data="20-05-2026"):
    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {
            "operador": operador,
            "n_operador": n_operador,
            "data": data,
            "setor_maquina": "",
            "cod_maquina": "",
        },
        "rows": [],
        "footer": {},
    }
    db.update_extraction(sid, raw_extraction=sheet_data,
                         dq_audit={"cells": {}}, sheet_data=sheet_data)
    return sid


def _seed_bobine_v3():
    sid = db.insert_sheet("bobine-v3.jpg")
    sheet_data = {
        "template_name": "bobine_formato",
        "header": {
            "operador": "AUGUSTO MONTEIRO",
            "n_operador": "95",
            "data": "27-07-2026",
            "setor_maquina": "BOBINE-FORMATO",
            "cod_maquina": "M032",
            "turno": "M",
        },
        "rows": [{"of": "262107", "modelo": "CFC5F45RIV", "fecho": ""}],
        "footer": {},
    }
    db.update_extraction(
        sid,
        raw_extraction=sheet_data,
        dq_audit={"cells": {}},
        sheet_data=sheet_data,
    )
    return sid


@pytest.mark.parametrize(("value", "expected"), [("", ""), ("x", "X"), ("X", "X")])
def test_desktop_edit_accepts_and_normalizes_fecho(
    value, expected, tmp_db, isolate, client,
):
    sid = _seed_bobine_v3()
    response = client.post(
        f"/sheet/{sid}/edit",
        data={"field_path": "rows[0].fecho", "new_value": value},
        headers=_DESKTOP,
    )
    assert response.status_code == 200
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["fecho"] == expected


def test_desktop_edit_rejects_invalid_fecho(tmp_db, isolate, client):
    sid = _seed_bobine_v3()
    response = client.post(
        f"/sheet/{sid}/edit",
        data={"field_path": "rows[0].fecho", "new_value": "SIM"},
        headers=_DESKTOP,
    )
    assert response.status_code == 400
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["fecho"] == ""


class TestEditNumberRefreshesName:
    """Point #2 — mudar o nº do operador actualiza o nome no ecrã (OOB)."""

    def test_edit_n_operador_returns_oob_name_cell(self, tmp_db, isolate, client):
        # OCR leu o nome de outra pessoa; cod 95 → AUGUSTO MONTEIRO.
        sid = _seed(operador="HELENA FERNANDEZ", n_operador="1162")
        r = client.post(
            f"/sheet/{sid}/edit",
            data={"field_path": "header.n_operador", "new_value": "95"},
            headers=_DESKTOP,
        )
        assert r.status_code == 200
        body = r.text
        # BD: o snap escreveu o nome canónico do nº 95.
        hdr = db.get_sheet(sid)["sheet_data"]["header"]
        assert hdr["operador"] == "AUGUSTO MONTEIRO"
        assert hdr["n_operador"] == "95"
        # Resposta: swap out-of-band da célula do nome para o ecrã actualizar.
        assert 'hx-swap-oob="true"' in body
        assert 'id="cell-header-operador"' in body
        assert "AUGUSTO MONTEIRO" in body

    def test_edit_unrelated_field_has_no_oob(self, tmp_db, isolate, client):
        # Editar setor_maquina (sem efeito no operador) → sem OOB.
        sid = _seed(operador="AUGUSTO MONTEIRO", n_operador="95")
        r = client.post(
            f"/sheet/{sid}/edit",
            data={"field_path": "header.setor_maquina", "new_value": "LASER"},
            headers=_DESKTOP,
        )
        assert r.status_code == 200
        assert "hx-swap-oob" not in r.text


class TestDateCellStoresPt:
    """Point #1 — a célula da data recebe ISO mas grava DD-MM-YYYY."""

    def test_iso_input_stored_as_dd_mm_yyyy(self, tmp_db, isolate, client):
        sid = _seed(operador="AUGUSTO MONTEIRO", n_operador="95", data="20-05-2026")
        r = client.post(
            f"/sheet/{sid}/edit",
            data={"field_path": "header.data", "new_value": "2026-05-29"},
            headers=_DESKTOP,
        )
        assert r.status_code == 200
        assert db.get_sheet(sid)["sheet_data"]["header"]["data"] == "29-05-2026"

    def test_edit_queues_cross_check_without_running_inline(
        self, tmp_db, isolate, monkeypatch, client
    ):
        sid = _seed(operador="AUGUSTO MONTEIRO", n_operador="95", data="20-05-2026")
        queued: list[tuple[tuple[int, ...], dict]] = []

        def fail_inline(*args, **kwargs):
            raise AssertionError("cross-check ran inline")

        monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
        monkeypatch.setattr(
            main,
            "_start_sheet_cross_check",
            lambda sheet_ids, **kwargs: queued.append((tuple(sorted(sheet_ids)), kwargs)),
        )

        r = client.post(
            f"/sheet/{sid}/edit",
            data={"field_path": "header.data", "new_value": "2026-05-29"},
            headers=_DESKTOP,
        )

        assert r.status_code == 200
        assert queued == [((sid,), {"profile_trigger": "sheet_edit"})]


class TestValidateReadsHeader:
    """Point #1 — validate lê do cabeçalho; rejeita cabeçalho inválido."""

    def test_validate_uses_header_values(self, tmp_db, isolate, client):
        sid = _seed(operador="AUGUSTO MONTEIRO", n_operador="95", data="20-05-2026")
        r = client.post(f"/sheet/{sid}/validate", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 303
        sheet = db.get_sheet(sid)
        assert sheet["status"] == "validated"
        assert sheet["operador"] == "AUGUSTO MONTEIRO"

    def test_validate_rejects_bad_date_in_header(self, tmp_db, isolate, client):
        sid = _seed(operador="AUGUSTO MONTEIRO", n_operador="95", data="lixo")
        r = client.post(f"/sheet/{sid}/validate", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 400
        assert db.get_sheet(sid)["status"] != "validated"

    def test_validate_rejects_empty_operador(self, tmp_db, isolate, client):
        sid = _seed(operador="", n_operador="95", data="20-05-2026")
        r = client.post(f"/sheet/{sid}/validate", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 400
        assert db.get_sheet(sid)["status"] != "validated"

    def test_validate_from_form_still_applies_values(self, tmp_db, isolate, client):
        # Backward-compat: kanban_viewer.html confirma data/nº/operador no
        # próprio form. Esses valores devem ser aplicados (caminho clássico).
        sid = _seed(operador="HELENA FERNANDEZ", n_operador="1162", data="20-05-2026")
        r = client.post(
            f"/sheet/{sid}/validate",
            data={"operador": "AUGUSTO MONTEIRO", "data": "2026-05-29", "n_operador": "95"},
            headers=_DESKTOP, follow_redirects=False,
        )
        assert r.status_code == 303
        sheet = db.get_sheet(sid)
        assert sheet["status"] == "validated"
        assert sheet["operador"] == "AUGUSTO MONTEIRO"  # do form (KPI)
        assert sheet["sheet_data"]["header"]["data"] == "29-05-2026"
        assert sheet["sheet_data"]["header"]["n_operador"] == "95"


def test_admin_reload_refs_uses_background_revalidation(tmp_db, monkeypatch, client):
    class _Watcher:
        def force_reload(self):
            return {
                "loaded_at": "test",
                "stats": {"n_lotes": 2, "n_ofs": 3},
            }

    def fail_inline(*args, **kwargs):
        raise AssertionError("cross-check ran inline")

    monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())
    monkeypatch.setattr(main, "_run_and_store_cross_check", fail_inline)
    monkeypatch.setattr(main, "_start_revalidation", lambda: True)

    r = client.post("/admin/reload-refs", headers=_DESKTOP)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["sheets_revalidated"] == 0
    assert body["revalidation_started"] is True
