"""Task C — página /admin com separadores + tab Unidades + partilha do corpo
de refs entre /refs e /admin/referencias (redirect `back` whitelisted)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def fake_refs(monkeypatch):
    """Watcher/importer/uploads falsos — o tab Referências renderiza sem
    ficheiros reais no disco."""

    class _Watcher:
        def get_refs(self):
            return {"available": True, "stats": {}}

        def status(self):
            return {}

    monkeypatch.setattr(main, "get_watcher", lambda: _Watcher())

    class _Importer:
        @staticmethod
        def status():
            return {"enabled": False, "thread_alive": False, "source_dir": None,
                    "interval_seconds": None, "last_run_at": None,
                    "last_error": None, "last_result": None}

    monkeypatch.setattr(main, "ref_importer", _Importer())
    from app.cross_check import refs_uploads
    monkeypatch.setattr(refs_uploads, "recent", lambda *a, **k: [])


@pytest.fixture()
def client():
    return TestClient(main.app)


class TestAdminSkeleton:
    def test_admin_redirects_to_referencias(self, tmp_db, client):
        r = client.get("/admin", headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/referencias"

    def test_unknown_tab_404(self, tmp_db, client):
        r = client.get("/admin/nao-existe", headers=_DESKTOP)
        assert r.status_code == 404

    def test_fixed_admin_routes_not_shadowed(self, tmp_db, client, monkeypatch):
        # /admin/{tab} não pode engolir os endpoints JSON fixos.
        monkeypatch.setattr(main, "load_to_analisar", lambda limit=None: [])
        r = client.get("/admin/to-analisar", headers=_DESKTOP)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_tab_referencias_renders_refs_body(self, tmp_db, fake_refs, client):
        r = client.get("/admin/referencias", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Estado das refs" in r.text
        # POSTs do corpo devem voltar ao /admin/referencias, não ao /refs.
        assert 'name="back" value="/admin/referencias"' in r.text

    def test_tab_kanbans_renders(self, tmp_db, client):
        r = client.get("/admin/kanbans", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Kanbans registados" in r.text

    def test_tab_kanbans_lists_builtin_inventory(self, tmp_db, client):
        # R258 — inventário por setor: os modelos de fábrica aparecem na
        # tab (antes eram só um número), com setor + fase + badges.
        r = client.get("/admin/kanbans", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Modelos de fábrica" in r.text
        assert "BOBINE-FORMATO" in r.text      # alias canónico do default
        assert "GUILHOTINA 6M" in r.text       # setor de corte
        assert "bobine_formato" in r.text      # nome técnico
        assert "bobine_formato_legacy" not in r.text  # template interno
        assert ">verso<" in r.text             # badge dos templates verso
        assert ">TPL102<" in r.text            # badge dos Gemini

    def test_tab_kpis_renders(self, tmp_db, client):
        r = client.get("/admin/kpis", headers=_DESKTOP)
        assert r.status_code == 200

    def test_refs_page_still_alive(self, tmp_db, fake_refs, client):
        # URL histórico — zero quebra.
        r = client.get("/refs", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Estado das refs" in r.text
        assert 'name="back" value="/refs"' in r.text


class TestRefsBackWhitelist:
    def test_default(self):
        resp = main._refs_redirect("ok", "x")
        assert resp.headers["location"].startswith("/refs?")

    def test_admin_back(self):
        resp = main._refs_redirect("ok", "x", back="/admin/referencias")
        assert resp.headers["location"].startswith("/admin/referencias?")

    def test_open_redirect_blocked(self):
        resp = main._refs_redirect("ok", "x", back="https://evil.example")
        assert resp.headers["location"].startswith("/refs?")

    def test_revalidate_back_roundtrip(self, tmp_db, client, monkeypatch):
        monkeypatch.setattr(main, "_start_revalidation", lambda: True)
        r = client.post("/refs/revalidate",
                        data={"back": "/admin/referencias"},
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/referencias?ok=")


class TestAdminUnidades:
    def test_tab_lists_seed(self, tmp_db, client):
        r = client.get("/admin/unidades", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Trofa" in r.text
        assert "(sede)" in r.text

    def test_create_unidade(self, tmp_db, client):
        r = client.post("/admin/unidades", data={"nome": "Esposende"},
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?ok=")
        assert {u["nome"] for u in db.list_unidades()} == {"Esposende", "Trofa"}

    def test_create_duplicate_flashes_err(self, tmp_db, client):
        db.create_unidade("Esposende")
        r = client.post("/admin/unidades", data={"nome": "esposende"},
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?err=")

    def test_create_empty_flashes_err(self, tmp_db, client):
        r = client.post("/admin/unidades", data={"nome": "   "},
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?err=")

    def test_toggle(self, tmp_db, client):
        uid = db.create_unidade("Esposende")
        r = client.post(f"/admin/unidades/{uid}/toggle",
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?ok=")
        assert [u["nome"] for u in db.list_unidades()] == ["Trofa"]

    def test_toggle_trofa_blocked(self, tmp_db, client):
        r = client.post(f"/admin/unidades/{db.trofa_unidade_id()}/toggle",
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?err=")
        assert db.list_unidades()[0]["ativo"] == 1

    def test_toggle_missing(self, tmp_db, client):
        r = client.post("/admin/unidades/999/toggle",
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/unidades?err=")
