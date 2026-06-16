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
from fastapi.testclient import TestClient

from app.web import db, main, ocr_runner

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

    # OCR simulado — sem Ollama.
    monkeypatch.setattr(ocr_runner, "run_pipeline", lambda _p: _fake_extraction())

    # Neutralizar efeitos de disco/rede do pós-processamento (testados noutro lado).
    monkeypatch.setattr(main, "_run_and_store_cross_check", lambda sid: None)
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda sid: None)
    monkeypatch.setattr(main, "_build_cc_maps", lambda sid: ({}, {}, {}, {}, {}, {}, {}))
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
