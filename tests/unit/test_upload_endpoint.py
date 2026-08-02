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

# R263 — referência capturada no import (antes do monkeypatch do fixture `env`)
# para os testes que precisam do bloco de lado REAL do run_pipeline.
_REAL_RUN_PIPELINE = ocr_runner.run_pipeline


def _make_pdf_bytes(n_pages: int = 3, size: tuple[int, int] = (842, 595)) -> bytes:
    """rev01 — PDF sintético (via Pillow) para testar a ingestão sem rede."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    imgs = [Image.new("RGB", size, (255, 255, 255)) for _ in range(n_pages)]
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


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
    assert sheet["side_locked"] == 1  # R263 — decisão humana tranca o lado
    assert not sheet["needs_review"]
    assert sheet["status"] == "pending"


# --- R263 — o lock humano sobrevive ao reprocess (fix do loop 409) ---


def _mock_real_pipeline_internals(monkeypatch, *, rows):
    """Restaura o run_pipeline REAL (o fixture `env` mocka-o) e isola só os
    internos de OCR — o bloco de lado corre a sério (padrão de
    test_rev00_new_format._mock_pipeline)."""
    monkeypatch.setattr(ocr_runner, "run_pipeline", _REAL_RUN_PIPELINE)
    pass1 = {"header": {"setor_maquina": "GUILHOTINA"}, "rows": rows, "footer": {}}
    monkeypatch.setattr(ocr_runner, "_run_ocr", lambda *a, **k: (pass1, None))
    monkeypatch.setattr(ocr_runner, "_merge_pass2_into_pass1", lambda p1, p2: p1)
    monkeypatch.setattr(ocr_runner, "_build_current_and_dq", lambda raw, tpl: (dict(raw), {}))


_FRENTE_ROWS = [{"of": "262107", "modelo": "OMEGA"}, {"of": "262559", "modelo": "CGC2"}]


def test_worker_passes_side_lock_to_pipeline(env, client, monkeypatch):
    """Depois do resolve-side, o worker chama run_pipeline com side_locked=True."""
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)
    db.set_needs_review(sheet_id, "side_hint_conflict")
    client.post(
        f"/sheet/{sheet_id}/resolve-side",
        data={"side": "V"},
        headers=_DESKTOP,
        follow_redirects=False,
    )
    captured: dict = {}
    monkeypatch.setattr(
        ocr_runner,
        "run_pipeline",
        lambda _p, **kw: (captured.update(kw), _fake_extraction())[1],
    )
    main._process_sheet_ocr(sheet_id)
    assert captured["side_locked"] is True
    assert captured["page_hint"] == "V"


def test_resolve_against_heuristic_stays_clean_and_deposits(env, client, monkeypatch):
    """Regressão do loop 409: Pass-1 com cara de frente + humano diz "É Verso"
    → o reprocess NÃO re-marca side_hint_conflict, deposita e o validate deixa
    de responder "resolve o lado antes de validar"."""
    _mock_real_pipeline_internals(monkeypatch, rows=_FRENTE_ROWS)
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
    assert sheet["needs_review"]  # heurística marcou (pista=V mas parece frente)
    assert sheet["review_reason"] == "side_hint_conflict"
    assert deposited == []

    resp = client.post(
        f"/sheet/{sheet_id}/resolve-side",
        data={"side": "V"},  # humano contradiz a heurística
        headers=_DESKTOP,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    main._process_sheet_ocr(sheet_id)  # reprocess enfileirado pelo resolve-side

    sheet = db.get_sheet(sheet_id)
    assert not sheet["needs_review"]  # antes do R263 voltava a 1 → loop
    assert deposited == [sheet_id]
    v = client.post(f"/sheet/{sheet_id}/validate", headers=_DESKTOP)
    assert "revisão de lado" not in v.text


def test_reprocess_without_resolution_still_reflags(env, client, monkeypatch):
    """Guarda anti-regressão do default: sem resolução humana, o reprocess
    continua a re-marcar o conflito (proteção do CSV intacta)."""
    _mock_real_pipeline_internals(monkeypatch, rows=_FRENTE_ROWS)
    sheet_id = client.post(
        "/upload?return=json",
        files={"image": ("kanban.png", _PNG_1x1, "image/png")},
        data={"page": "V"},
        headers=_DESKTOP,
    ).json()["sheet_id"]
    main._process_sheet_ocr(sheet_id)
    db.update_status(sheet_id, "pending")  # reprocess manual, sem resolve-side
    main._process_sheet_ocr(sheet_id)
    sheet = db.get_sheet(sheet_id)
    assert sheet["needs_review"]
    assert sheet["review_reason"] == "side_hint_conflict"


def test_set_page_hint_lock_semantics(env):
    """R263 — migração + setter: default 0; locked=True grava 1; chamada sem
    locked não faz downgrade do lock. init_db é idempotente com a coluna."""
    db.init_db()  # 2ª chamada (o fixture já correu uma) — idempotente
    sid = db.insert_sheet("x.png")
    assert db.get_sheet(sid)["side_locked"] == 0
    db.set_page_hint(sid, "V", locked=True)
    sheet = db.get_sheet(sid)
    assert sheet["page_hint"] == "V"
    assert sheet["side_locked"] == 1
    db.set_page_hint(sid, "F")  # sem locked → não despromove
    sheet = db.get_sheet(sid)
    assert sheet["page_hint"] == "F"
    assert sheet["side_locked"] == 1


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


# ---------------------------------------------------------------------------
# rev01 — ingestão de PDF / multi-PDF (scans de folhas manuscritas)
# ---------------------------------------------------------------------------

def test_upload_pdf_creates_n_sheets(env, client):
    """Um PDF de 3 páginas → 3 folhas 'pending'; resposta uniforme com `sheets`."""
    resp = client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(3), "application/pdf")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert len(body["sheets"]) == 3
    assert body["sheet_id"] == body["sheets"][0]["sheet_id"]  # retrocompat
    for s in body["sheets"]:
        assert db.get_sheet(s["sheet_id"])["status"] == "pending"
    # 3 páginas rasterizadas + o PDF-fonte preservado no disco.
    assert len(list((env / "images").glob("*.jpg"))) == 3
    assert len(list((env / "images").glob("*.pdf"))) == 1


def test_upload_pdf_detected_by_magic_not_extension(env, client):
    """Filename `.bin` mas conteúdo `%PDF-` → tratado como PDF (magic > extensão)."""
    resp = client.post(
        "/upload?return=json",
        files={"image": ("scan.bin", _make_pdf_bytes(2), "application/octet-stream")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 2


def test_upload_rejects_fake_pdf(env, client):
    """Filename `.pdf` sem assinatura %PDF- → 400."""
    resp = client.post(
        "/upload?return=json",
        files={"image": ("x.pdf", b"hello, not a pdf", "application/pdf")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 400


def test_upload_pdf_skips_autocrop_but_image_runs_it(env, client, monkeypatch):
    """auto_crop corre para FOTOS mas é SALTADO para páginas de PDF (regressão)."""
    from app.web import image_crop

    calls: list[str] = []
    monkeypatch.setattr(image_crop, "auto_crop", lambda p, *a, **k: calls.append(str(p)))

    client.post(
        "/upload?return=json",
        files={"image": ("k.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    )
    assert len(calls) == 1  # imagem → 1 chamada
    calls.clear()

    client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(2), "application/pdf")},
        headers=_DESKTOP,
    )
    assert calls == []  # PDF → nenhuma chamada


def test_upload_pdf_page_hint_null_and_group_shared(env, client):
    """Mesmo com page='F', as páginas de PDF ficam page_hint NULL (auto-deteção)
    e partilham o mesmo capture_group (agrupadas pelo PDF)."""
    body = client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(3), "application/pdf")},
        data={"page": "F"},
        headers=_DESKTOP,
    ).json()
    groups = set()
    for s in body["sheets"]:
        sh = db.get_sheet(s["sheet_id"])
        assert sh["page_hint"] is None
        groups.add(sh["capture_group"])
    assert len(groups) == 1


def test_upload_image_response_shape_unchanged(env, client):
    """Retrocompat: upload de imagem mantém sheet_id/status/error e ganha `sheets`."""
    body = client.post(
        "/upload?return=json",
        files={"image": ("k.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    ).json()
    assert isinstance(body["sheet_id"], int)
    assert body["status"] == "pending"
    assert body["error"] is None
    assert body["count"] == 1
    assert len(body["sheets"]) == 1
    assert body["sheets"][0]["sheet_id"] == body["sheet_id"]


def test_upload_pdf_too_many_pages_422(env, client, monkeypatch):
    """PDF acima do cap → 422, nenhuma folha criada, PDF-fonte apagado."""
    monkeypatch.setenv("PDF_MAX_PAGES", "1")
    resp = client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(2), "application/pdf")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 422
    assert db.list_sheets() == []  # nenhuma folha
    assert list((env / "images").glob("*.pdf")) == []  # fonte apagada


def test_upload_pdf_multipage_redirects_to_queue(env, client):
    """Sem ?return=json, um PDF multipágina redireciona para /queue."""
    resp = client.post(
        "/upload",
        files={"image": ("scan.pdf", _make_pdf_bytes(2), "application/pdf")},
        headers=_DESKTOP,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/queue"


def test_upload_pdf_without_pypdfium2_returns_503(env, client, monkeypatch):
    """Se o servidor não tiver pypdfium2 (dep ausente), um PDF dá 503 com
    mensagem clara — e o resto da app continua a funcionar (imagens ok)."""
    from app.web import pdf_ingest

    monkeypatch.setattr(pdf_ingest, "pdfium", None)
    resp = client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(2), "application/pdf")},
        headers=_DESKTOP,
    )
    assert resp.status_code == 503
    assert db.list_sheets() == []
    assert list((env / "images").glob("*.pdf")) == []  # fonte apagada
    # imagem continua a funcionar mesmo sem a dep de PDF
    ok = client.post(
        "/upload?return=json",
        files={"image": ("k.png", _PNG_1x1, "image/png")},
        headers=_DESKTOP,
    )
    assert ok.status_code == 200


def test_upload_pdf_pages_process_to_extracted(env, client):
    """As páginas de PDF passam pelo worker (OCR mockado) até 'extracted'."""
    body = client.post(
        "/upload?return=json",
        files={"image": ("scan.pdf", _make_pdf_bytes(2), "application/pdf")},
        headers=_DESKTOP,
    ).json()
    for s in body["sheets"]:
        main._process_sheet_ocr(s["sheet_id"])
        assert db.get_sheet(s["sheet_id"])["status"] == "extracted"
