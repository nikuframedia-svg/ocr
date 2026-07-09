"""Task C F7 — gate opcional ADMIN_TOKEN em /admin* e /refs*."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()


@pytest.fixture()
def client():
    return TestClient(main.app)


class TestGateOff:
    def test_default_open(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        r = client.get("/admin/unidades", headers=_DESKTOP)
        assert r.status_code == 200


class TestGateOn:
    def test_blocks_without_token(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "seg redo123")
        r = client.get("/admin/unidades", headers=_DESKTOP)
        assert r.status_code == 401

    def test_header_token(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "segredo123")
        r = client.get("/admin/unidades",
                       headers={**_DESKTOP, "X-Admin-Token": "segredo123"})
        assert r.status_code == 200

    def test_query_token_sets_cookie(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "segredo123")
        r = client.get("/admin/unidades?token=segredo123", headers=_DESKTOP)
        assert r.status_code == 200
        assert r.cookies.get("admin_token") == "segredo123"
        # navegação seguinte só com o cookie
        r2 = client.get("/admin/kpis", headers=_DESKTOP)
        assert r2.status_code == 200

    def test_wrong_token_401(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "segredo123")
        r = client.get("/admin/unidades",
                       headers={**_DESKTOP, "X-Admin-Token": "errado"})
        assert r.status_code == 401

    def test_non_admin_paths_untouched(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_TOKEN", "segredo123")
        r = client.get("/queue", headers=_DESKTOP)
        assert r.status_code == 200
