"""Task C E3 — endpoints /admin/kpis (tab + validate/save/revert)."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.web import db, kpi_params, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_MOBILE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}


@pytest.fixture(autouse=True)
def tmp_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    monkeypatch.setattr(kpi_params, "_PARAMS_PATH", tmp_path / "kpi_params.json")
    kpi_params.invalidate_cache()

    class _W:
        def get_refs(self):
            return {}

    import app.cross_check.ref_watcher as rw
    monkeypatch.setattr(rw, "get_watcher", lambda: _W())
    yield
    kpi_params.invalidate_cache()


@pytest.fixture()
def client():
    return TestClient(main.app)


def _seed_production(day="2026-07-01"):
    with db.conn() as c:
        c.execute("INSERT INTO sheets (image_path, status, sheet_data) VALUES (?,?,?)",
                  ("x.jpg", "validated",
                   json.dumps({"header": {"setor_maquina": "Corte"}})))
        sid = c.execute("SELECT MAX(id) AS i FROM sheets").fetchone()["i"]
        c.execute(
            "INSERT INTO production_rows (sheet_id, row_index, operador, "
            "sheet_iso_date, sheet_hours, qtd) VALUES (?,?,?,?,?,?)",
            (sid, 0, "ANA", day, 4.0, 12))


class TestKpisTab:
    def test_renders_defaults(self, client):
        r = client.get("/admin/kpis", headers=_DESKTOP)
        assert r.status_code == 200
        assert "Fórmulas dos KPIs" in r.text
        assert "qtd / horas" in r.text
        assert "Variáveis disponíveis" in r.text


class TestValidate:
    def test_valid_with_preview(self, client):
        _seed_production()
        r = client.post("/admin/kpis/validate", headers=_DESKTOP,
                        json={"kpis": kpi_params.DEFAULT_KPIS})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["errors"] == {}
        assert data["preview_date"] == "2026-07-01"
        cards = {c["id"]: c for c in data["preview"]}
        assert cards["col_per_h"]["value"] == 3.0
        assert data["variables"]["qtd"] == 12

    def test_invalid_formula_flagged(self, client):
        bad = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        bad[0]["expr"] = "qtd / naoexiste"
        r = client.post("/admin/kpis/validate", headers=_DESKTOP,
                        json={"kpis": bad})
        data = r.json()
        assert data["ok"] is False
        assert bad[0]["id"] in data["errors"]
        assert data["preview"] is None

    def test_no_production_no_preview(self, client):
        r = client.post("/admin/kpis/validate", headers=_DESKTOP,
                        json={"kpis": kpi_params.DEFAULT_KPIS})
        data = r.json()
        assert data["ok"] is True
        assert data["preview"] is None
        assert data["preview_date"] is None

    def test_mobile_403(self, client):
        r = client.post("/admin/kpis/validate", headers=_MOBILE,
                        json={"kpis": []})
        assert r.status_code == 403


class TestSave:
    def test_save_ok(self, client):
        kpis_new = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        kpis_new[0]["expr"] = "qtd / horas * 2"
        r = client.post("/admin/kpis/save", headers=_DESKTOP,
                        json={"version": 0, "kpis": kpis_new})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "version": 1}

    def test_save_conflict_409(self, client):
        kpi_params.save_kpis(kpi_params.DEFAULT_KPIS, expected_version=0)
        r = client.post("/admin/kpis/save", headers=_DESKTOP,
                        json={"version": 0, "kpis": kpi_params.DEFAULT_KPIS})
        assert r.status_code == 409

    def test_save_invalid_422(self, client):
        bad = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        bad[0]["expr"] = "__import__('os')"
        r = client.post("/admin/kpis/save", headers=_DESKTOP,
                        json={"version": 0, "kpis": bad})
        assert r.status_code == 422
        assert bad[0]["id"] in r.json()["errors"]

    def test_save_missing_version_422(self, client):
        r = client.post("/admin/kpis/save", headers=_DESKTOP,
                        json={"kpis": kpi_params.DEFAULT_KPIS})
        assert r.status_code == 422

    def test_mobile_403(self, client):
        r = client.post("/admin/kpis/save", headers=_MOBILE,
                        json={"version": 0, "kpis": []})
        assert r.status_code == 403


class TestRevert:
    def test_revert_defaults(self, client):
        kpis_new = copy.deepcopy(kpi_params.DEFAULT_KPIS)
        kpis_new[0]["expr"] = "qtd / horas * 2"
        kpi_params.save_kpis(kpis_new, expected_version=0)
        r = client.post("/admin/kpis/revert", headers=_DESKTOP,
                        json={"to": "defaults"})
        assert r.status_code == 200
        got = {k["id"]: k for k in kpi_params.get_kpis()}
        assert got["col_per_h"]["expr"] == "qtd / horas"

    def test_revert_bad_index_422(self, client):
        r = client.post("/admin/kpis/revert", headers=_DESKTOP,
                        json={"to": 99})
        assert r.status_code == 422

    def test_mobile_403(self, client):
        r = client.post("/admin/kpis/revert", headers=_MOBILE, json={})
        assert r.status_code == 403
