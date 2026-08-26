"""R261 — cross-check em tempo real lote↔StockSAP no kanban Bobine formato.

Cobre o pedido do utilizador: ao alterar o lote na revisão, verificar de
imediato contra o StockSAP (larg_mm↔Largura, esp↔Espessura) e sugerir a
atualização das medidas com confirmação de 1 clique — nunca escrever sem
confirmação (lição R260).

  - POST /sheet/{id}/edit em rows[i].(lote|larg_mm|esp) → banner OOB
    `#lote-cc-banner` na resposta (_lote_suggest.html), SEM escritas.
  - POST /sheet/{id}/apply-lote-medidas → escreve lote canónico + medidas
    do StockSAP via apply_edits_batch(source='human'), devolve banner
    "applied" + células OOB. R269 — escreve TUDO o que o banner mostrou e
    difere do valor atual, mesmo dentro da tolerância de sinalização
    (folha 5226: "confirmamos mas não aplica").

DB isolada por teste (monkeypatch `_DB_PATH`); efeitos de disco do
cross-check neutralizados, como em test_sheet_edit_oob.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db
from app.web import main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Entry sintética do StockSAP (shape do ref_watcher: larg é string).
_SAP = {
    "M26B0358": {"qtd": 5, "esp": 2.6, "larg": "1280", "desc": "S355J2"},
}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def isolate(monkeypatch):
    """Refs sintéticos com lotes_sap_full + neutraliza efeitos de disco."""
    refs = {
        "available": True,
        "loaded_at": "test",
        "colaboradores": {},
        "operador_aliases": {},
        "lotes_sap_full": dict(_SAP),
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
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr("app.learning.scheduler.maybe_trigger_learning",
                        lambda *a, **k: None)
    return refs


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed(rows, template_name=None):
    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {"operador": "AUGUSTO MONTEIRO", "n_operador": "95",
                   "data": "20-05-2026", "setor_maquina": "", "cod_maquina": ""},
        "rows": rows,
        "footer": {},
    }
    if template_name:
        sheet_data["template_name"] = template_name
    db.update_extraction(sid, raw_extraction=sheet_data,
                         dq_audit={"cells": {}}, sheet_data=sheet_data)
    return sid


def _row(sid, idx=0):
    return db.get_sheet(sid)["sheet_data"]["rows"][idx]


def _edit(client, sid, field_path, value):
    return client.post(f"/sheet/{sid}/edit",
                       data={"field_path": field_path, "new_value": value},
                       headers=_DESKTOP)


def _apply(client, sid, row_index, lote):
    return client.post(f"/sheet/{sid}/apply-lote-medidas",
                       data={"row_index": row_index, "lote": lote},
                       headers=_DESKTOP)


class TestSuggestOnEdit:
    def test_lote_diverge_shows_banner_without_writing(self, tmp_db, isolate, client):
        # Guard anti-R260: sugerir NUNCA escreve as medidas.
        sid = _seed([{"lote": "", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].lote", "M26B0358")
        assert r.status_code == 200
        body = r.text
        assert 'id="lote-cc-banner"' in body
        assert 'hx-swap-oob="true"' in body
        assert "Aplicar medidas do StockSAP" in body
        assert "1280" in body  # larg do SAP proposta
        row = _row(sid)
        assert row["larg_mm"] == "855"  # inalterado
        assert row["esp"] == "3"        # inalterado (3 vs 2.6 ≤ tol 0.5)

    def test_lote_match_green_without_apply_button(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "", "larg_mm": "1280", "esp": "2,6"}])
        r = _edit(client, sid, "rows[0].lote", "M26B0358")
        assert r.status_code == 200
        assert "confirmado no StockSAP" in r.text
        assert "Aplicar medidas" not in r.text

    def test_lote_not_found_warns_without_button(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].lote", "M99B9999")
        assert r.status_code == 200
        assert "não existe no StockSAP" in r.text
        assert "apply-lote-medidas" not in r.text

    def test_lote_h_form_proposes_m_form(self, tmp_db, isolate, client):
        # R259 — H nunca confirma silenciosamente; o banner propõe a forma M.
        sid = _seed([{"lote": "", "larg_mm": "1280", "esp": "2,6"}])
        r = _edit(client, sid, "rows[0].lote", "H26B0358")
        assert r.status_code == 200
        assert "Usar M26B0358" in r.text

    def test_lote_cleared_returns_empty_banner(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].lote", "")
        assert r.status_code == 200
        assert 'id="lote-cc-banner"' in r.text  # limpa sugestão anterior
        assert "StockSAP" not in r.text

    def test_other_field_edit_has_no_banner(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].cliente", "ENEDIS")
        assert r.status_code == 200
        assert "lote-cc-banner" not in r.text

    def test_refs_unavailable_edit_still_works(self, tmp_db, isolate, client):
        isolate["available"] = False
        sid = _seed([{"lote": "", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].lote", "M26B0358")
        assert r.status_code == 200
        assert "StockSAP" not in r.text
        assert _row(sid)["lote"] == "M26B0358"  # a edição em si persiste

    def test_non_lote_template_has_empty_banner(self, tmp_db, isolate, client):
        # guilhotina não tem lote em row_fields → gate de escopo desliga.
        sid = _seed([{"of": "111"}], template_name="guilhotina")
        r = _edit(client, sid, "rows[0].lote", "M26B0358")
        assert r.status_code == 200
        assert "StockSAP" not in r.text

    def test_reverse_direction_esp_edit_shows_diverge(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "1280", "esp": "2,6"}])
        r = _edit(client, sid, "rows[0].esp", "4")
        assert r.status_code == 200
        assert "Aplicar medidas do StockSAP" in r.text

    def test_reverse_direction_unknown_lote_is_silent(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "ZZZ99", "larg_mm": "855", "esp": "3"}])
        r = _edit(client, sid, "rows[0].esp", "4")
        assert r.status_code == 200
        assert "StockSAP" not in r.text  # not_found suprimido nesta direção


class TestSheetPagePlaceholder:
    def test_sheet_page_renders_banner_placeholder(self, tmp_db, isolate, client):
        # O alvo OOB tem de existir na página para o hx-swap-oob pegar.
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "3"}])
        r = client.get(f"/sheet/{sid}", headers=_DESKTOP)
        assert r.status_code == 200
        assert 'id="lote-cc-banner"' in r.text


class TestApplyLoteMedidas:
    def test_apply_writes_divergent_and_missing_as_human(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "4"}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        body = r.text
        assert "Aplicado do StockSAP" in body
        # Células OOB para atualizar a tabela sem reload.
        assert 'id="cell-rows-0-larg_mm"' in body
        assert 'id="cell-rows-0-esp"' in body
        row = _row(sid)
        assert row["larg_mm"] == "1280"
        assert row["esp"] == "2,6"  # _format_value: 2.6 → '2,6'
        assert row["lote"] == "M26B0358"  # já era canónico — não reescrito
        with db.conn() as c:
            edits = {e["field_path"]: e["source"] for e in c.execute(
                "SELECT field_path, source FROM edits WHERE sheet_id = ?",
                (sid,)).fetchall()}
        assert edits["rows[0].larg_mm"] == "human"
        assert edits["rows[0].esp"] == "human"
        # Protegidos contra o cross em background (classe de falha R260).
        protected = main._human_edited_paths(sid)
        assert "rows[0].larg_mm" in protected
        assert "rows[0].esp" in protected

    def test_apply_h_form_writes_canonical_lote(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "H26B0358", "larg_mm": "1280", "esp": "2,6"}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        assert _row(sid)["lote"] == "M26B0358"

    def test_apply_fills_missing_measures(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "", "esp": ""}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        row = _row(sid)
        assert row["larg_mm"] == "1280"
        assert row["esp"] == "2,6"

    def test_apply_within_tolerance_still_writes(self, tmp_db, isolate, client):
        # R269 — folha 5226: a Fátima confirma e as medidas dentro da
        # tolerância (larg ±50, esp ±0,5) ficavam por escrever. Confirmação
        # explícita escreve sempre o valor SAP mostrado no banner.
        sid = _seed([{"lote": "M26B0358", "larg_mm": "1250", "esp": ""}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        assert "Aplicado do StockSAP" in r.text
        row = _row(sid)
        assert row["larg_mm"] == "1280"  # 1250 vs 1280 ≤ tol 50 — escrito na mesma
        assert row["esp"] == "2,6"

    def test_apply_normalizes_format_variants(self, tmp_db, isolate, client):
        # R269 — "2.6" é o mesmo número do SAP noutra grafia: o apply
        # normaliza a célula para o formato canónico do banner.
        sid = _seed([{"lote": "M26B0358", "larg_mm": "1280", "esp": "2.6"}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        assert _row(sid)["esp"] == "2,6"

    def test_apply_coherent_row_writes_nothing(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "1280", "esp": "2,6"}])
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        assert "nada a alterar" in r.text
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM edits WHERE sheet_id = ?",
                          (sid,)).fetchone()["n"]
        assert n == 0

    def test_apply_stale_lote_writes_nothing(self, tmp_db, isolate, client):
        # TOCTOU — o lote confirmado no banner já não é o alvo recomputado.
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "4"}])
        r = _apply(client, sid, 0, "M11B1111")
        assert r.status_code == 200
        assert "A linha mudou" in r.text
        row = _row(sid)
        assert row["larg_mm"] == "855"
        assert row["esp"] == "4"

    def test_apply_validated_sheet_409(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "4"}])
        with db.conn() as c:
            c.execute("UPDATE sheets SET status = 'validated' WHERE id = ?", (sid,))
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 409
        assert _row(sid)["larg_mm"] == "855"

    def test_apply_bad_row_index_400(self, tmp_db, isolate, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "4"}])
        r = _apply(client, sid, 7, "M26B0358")
        assert r.status_code == 400

    def test_apply_queues_background_cross_check(self, tmp_db, isolate,
                                                 monkeypatch, client):
        sid = _seed([{"lote": "M26B0358", "larg_mm": "855", "esp": "4"}])
        queued = []
        monkeypatch.setattr(
            main, "_start_sheet_cross_check",
            lambda sheet_ids, **kw: queued.append((tuple(sorted(sheet_ids)), kw)))
        r = _apply(client, sid, 0, "M26B0358")
        assert r.status_code == 200
        assert queued == [((sid,), {"profile_trigger": "apply_lote_medidas"})]
