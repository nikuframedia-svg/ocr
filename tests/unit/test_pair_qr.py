"""R257 — QR do /pair gerado localmente (fim do api.qrserver.com).

O pair.html enviava o URL completo da app (incluindo o hostname do tunnel
público) na query string para api.qrserver.com — fuga a um terceiro; e numa
LAN offline o QR nem renderizava. Agora /pair/qr.png é gerado no servidor.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_app.db")
    db.init_db()
    return TestClient(main.app)


def test_qr_png_served_locally(client):
    r = client.get("/pair/qr.png", headers=_DESKTOP)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_qr_decodes_to_capture_url_of_request_host(client):
    # Sem depender de um leitor de QR: gera localmente o QR esperado para o
    # host do request e compara byte-a-byte com a resposta do servidor.
    import io

    import qrcode

    r = client.get("/pair/qr.png", headers={**_DESKTOP, "host": "factory:8080"})
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data("http://factory:8080/capture")
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf)
    assert r.content == buf.getvalue()


def test_no_external_qr_service_in_templates():
    from pathlib import Path
    tpl_dir = Path(main.__file__).parent / "templates"
    hits = [p.name for p in tpl_dir.rglob("*.html")
            if "qrserver" in p.read_text(encoding="utf-8")]
    assert hits == []
