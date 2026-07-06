"""Verificação do endpoint de upload `POST /upload` (R139+).

Não havia nenhum teste a exercer o `/upload` — esta suíte fecha essa lacuna e
prova, sem precisar do Ollama, que o caminho completo funciona:

  HTTP (validação + multipart) → grava imagem em disco → db.insert_sheet
  → resposta (JSON ou redirect 303) → worker _process_sheet_ocr → status
  'extracted' → render de /sheet/{id}.

O OCR (`ocr_runner.run_pipeline`) é simulado; os efeitos de disco do
cross-check / factory CSV / kernel são neutralizados (mesma estratégia de
``test_sheet_edit_oob.py``) para o teste não tocar em ``data/`` nem na rede.
"""
from __future__ import annotations

import base64

import pytest
from app.web import db, main, ocr_runner
from fastapi.testclient import TestClient

# PNG 1×1 mínimo e válido (sem dependências externas).
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4"
    "2mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _fake_extraction() -> dict:
    """Resultado canónico de OCR (folha de Acabamento MTG2)."""
    data = {
        "template_name": "acabamento",
        "header": {
            "operador": "JÚLIO LIMA",
            "n_operador": "0537",
            "setor_maquina": "ACABAMENTO MTG2",
            "cod_maquina": "M061",
            "data": "25-05-2026",
            "turno": "M",
        },
        "rows": [{"of": "262892", "modelo": "CGC2E10D", "qtd": "4"}],
        "footer": {"colunas_produzidas": "4"},
    }
    return {
        "raw": data,
        "dq": {"cells": {}},
        "current": data,
        "template_name": "acabamento",
    }


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """DB + diretórios isolados em tmp; OCR simulado; efeitos de disco off."""
    # DB isolada.
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()

    # Upload grava em tmp (e _process_sheet_ocr lê de _DATA_DIR).
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_IMAGES_DIR", images)

    # OCR simulado — sem Ollama. rev00: run_pipeline aceita page_hint.
    monkeypatch.setattr(
        ocr_runner, "run_pipeline", lambda _p, **_kw: _fake_extraction()
    )

    # Neutralizar efeitos de disco/rede do pós-processamento (testados noutro lado).
    monkeypatch.setattr(main, "_run_and_store_cross_check", lambda sid: None)
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda sid: None)
    monkeypatch.setattr(main, "_build_cc_maps", lambda sid: ({}, {}, {}, {}, {}, {}))
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)
    return tmp_path


@pytest.fixture()
def client():
    return TestClient(main.app)


def test_upload_accepts_file_and_creates_pending_sheet(env, client):
    """Camada HTTP: aceita o multipart, grava a imagem, cria folha 'pending'."""
    resp = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sheet_id = body["sheet_id"]
    assert isinstance(sheet_id, int)
    assert body["status"] == "pending"
    assert body["error"] is None

    # Imagem gravada em disco e folha registada na DB.
    images = list((env / "images").glob("*.png"))
    assert len(images) == 1
    sheet = db.get_sheet(sheet_id)
    assert sheet is not None
    assert sheet["status"] == "pending"


def test_upload_redirects_when_not_json_mode(env, client):
    """Sem ?return=json devolve redirect 303 para /sheet/{id}."""
    resp = client.post(
        "/upload",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/sheet/")


def test_worker_processes_to_extracted(env, client):
    """O worker (com OCR simulado) leva a folha a 'extracted', não a 'error'."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]

    main._process_sheet_ocr(sheet_id)

    sheet = db.get_sheet(sheet_id)
    assert sheet is not None
    assert sheet["status"] == "extracted"
    # A leitura real do setor (MTG2) sobrevive ao pipeline (regressão R139).
    assert (sheet.get("sheet_data") or {}).get("header", {}).get(
        "setor_maquina"
    ) == "ACABAMENTO MTG2"


def test_sheet_page_renders_after_upload(env, client):
    """O destino do redirect (/sheet/{id}) renderiza 200 — apanha regressões
    de template (ex.: field_labels do commit do Codex)."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)

    resp = client.get(f"/sheet/{sheet_id}", headers=_DESKTOP)
    assert resp.status_code == 200


def test_upload_rejects_unsupported_extension(env, client):
    """Extensão não suportada → 400 (validação de entrada)."""
    resp = client.post(
        "/upload?return=json",
        files={"image": ("notes.txt", b"hello", "text/plain")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# rev00 — captura guiada: pista de página + capture_group + resolução do lado
# ---------------------------------------------------------------------------

def test_upload_persists_page_and_capture_group(env, client):
    """A captura guiada envia `page` + `capture_group`; ficam gravados."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "V", "capture_group": "sess-1"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    sheet = db.get_sheet(sheet_id)
    assert sheet["page_hint"] == "V"
    assert sheet["capture_group"] == "sess-1"


def test_upload_without_hint_leaves_page_hint_null(env, client):
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    sheet = db.get_sheet(sheet_id)
    assert sheet["page_hint"] is None


def test_hinted_upload_clean_not_flagged(env, client):
    """Upload com pista + extração limpa não fica marcado para revisão."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "F", "capture_group": "g"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)
    sheet = db.get_sheet(sheet_id)
    assert not sheet["needs_review"]


def test_needs_review_result_sets_flag_and_suspends_deposit(env, client, monkeypatch):
    """rev00 — garantia anti-corrupção: se o run_pipeline devolve needs_review,
    o worker marca a folha E NÃO deposita CSV na fábrica."""
    fake = _fake_extraction()
    fake["needs_review"] = True
    fake["review_reason"] = "side_hint_conflict"
    monkeypatch.setattr(ocr_runner, "run_pipeline", lambda _p, **_k: fake)
    deposited: list[int] = []
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda sid: deposited.append(sid))
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "V"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)
    sheet = db.get_sheet(sheet_id)
    assert sheet["needs_review"]
    assert sheet["review_reason"] == "side_hint_conflict"
    assert deposited == []  # depósito suspenso


def test_clean_reprocess_clears_stale_review_and_deposits(env, client, monkeypatch):
    """Um reprocess que agora resolve limpo desmarca a folha E deposita."""
    deposited: list[int] = []
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda sid: deposited.append(sid))
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    db.set_needs_review(sheet_id, "side_indeterminate")  # marcada antes
    main._process_sheet_ocr(sheet_id)  # run_pipeline (mock) devolve limpo
    sheet = db.get_sheet(sheet_id)
    assert not sheet["needs_review"]
    assert deposited == [sheet_id]


def test_resolve_side_sets_hint_clears_review_requeues(env, client):
    """Resolver 'é verso' → page_hint='V', needs_review limpo, folha re-enfileirada."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)
    db.set_needs_review(sheet_id, "side_indeterminate")
    assert db.get_sheet(sheet_id)["needs_review"]

    resp = client.post(
        f"/sheet/{sheet_id}/resolve-side",
        data={"side": "V"},
        headers=_DESKTOP,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sheet = db.get_sheet(sheet_id)
    assert sheet["page_hint"] == "V"
    assert not sheet["needs_review"]
    assert sheet["status"] == "pending"


def test_resolve_side_rejects_bad_side(env, client):
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    resp = client.post(
        f"/sheet/{sheet_id}/resolve-side",
        data={"side": "X"},
        headers=_DESKTOP,
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_list_sheets_exposes_new_columns(env, client):
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "F", "capture_group": "g9"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    row = next(s for s in db.list_sheets() if s["id"] == sheet_id)
    assert row["page_hint"] == "F"
    assert row["capture_group"] == "g9"
    assert "needs_review" in row and "review_reason" in row


def test_capture_and_queue_pages_render(env, client):
    """Smoke: os templates editados renderizam sem erro de Jinja."""
    # semear uma folha com pista + revisão para exercitar os badges da queue
    sid = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "V", "capture_group": "g"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sid)
    db.set_needs_review(sid, "side_indeterminate")
    assert client.get("/capture").status_code == 200
    q = client.get("/queue", headers=_DESKTOP)
    assert q.status_code == 200
    assert "rever lado" in q.text  # badge de revisão renderiza


def test_sheet_renders_review_banner(env, client):
    sid = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sid)
    db.set_needs_review(sid, "side_indeterminate")
    resp = client.get(f"/sheet/{sid}", headers=_DESKTOP)
    assert resp.status_code == 200
    assert "Lado duvidoso" in resp.text
    assert "/resolve-side" in resp.text
