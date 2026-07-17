"""Task C E4 — endpoints do wizard de registo de kanbans (/admin/kanban-templates*)."""
from __future__ import annotations

import io
import json
import queue

import pytest
from fastapi.testclient import TestClient

from app import templates_registry as reg
from app.web import db, main, ocr_queue, template_store

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_MOBILE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}

_DISC = {
    "parse_ok": True, "title": "TPL999",
    "header": ["Operador", "Data"],
    "columns": ["OF", "MODELO", "QTD"],
    "footer": ["Colunas Produzidas"],
    "raw": '{"columns": ["OF", "MODELO", "QTD"]}',
}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    monkeypatch.setattr(main, "_DATA_DIR", tmp_path)
    # fila: capturar em vez de processar (worker não corre nos testes)
    enqueued: list[int] = []
    monkeypatch.setattr(main.ocr_queue, "enqueue_discovery",
                        lambda tid: enqueued.append(tid) or 1)
    yield {"tmp": tmp_path, "enqueued": enqueued}
    reg.set_runtime_templates([])


@pytest.fixture()
def client():
    return TestClient(main.app)


def _create(client, nome="corte esposende", unidade_id=None):
    uid = unidade_id or db.create_unidade("Esposende")
    r = client.post(
        "/admin/kanban-templates",
        data={"nome": nome, "unidade_id": str(uid)},
        files={"file": ("tpl.png", io.BytesIO(b"png-bytes"), "image/png")},
        headers=_DESKTOP,
    )
    return uid, r


class TestCreate:
    def test_create_enqueues_discovery(self, env, client):
        uid, r = _create(client)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["name"] == f"u{uid}_corte_esposende"
        tpl = db.get_kanban_template(data["id"])
        assert tpl["status"] == "a_analisar"
        assert env["enqueued"] == [data["id"]]
        assert (env["tmp"] / tpl["image_path"]).exists()

    def test_duplicate_409(self, env, client):
        uid, r1 = _create(client)
        assert r1.status_code == 200
        _, r2 = _create(client, unidade_id=uid)
        assert r2.status_code == 409

    def test_duplicate_upload_preserves_winner_image(self, env, client):
        # R258 — antes, os dois uploads escreviam para o MESMO ficheiro e o
        # perdedor do IntegrityError apagava a imagem do VENCEDOR.
        uid = db.create_unidade("Esposende")
        r1 = client.post(
            "/admin/kanban-templates",
            data={"nome": "corte esposende", "unidade_id": str(uid)},
            files={"file": ("tpl.png", io.BytesIO(b"winner"), "image/png")},
            headers=_DESKTOP)
        assert r1.status_code == 200
        r2 = client.post(
            "/admin/kanban-templates",
            data={"nome": "corte esposende", "unidade_id": str(uid)},
            files={"file": ("tpl.png", io.BytesIO(b"loser"), "image/png")},
            headers=_DESKTOP)
        assert r2.status_code == 409
        img = env["tmp"] / db.get_kanban_template(r1.json()["id"])["image_path"]
        assert img.read_bytes() == b"winner"
        leftovers = [p for p in img.parent.iterdir() if p.name.startswith(".up_")]
        assert leftovers == []  # sem temps órfãos

    def test_upload_too_large_413_no_leftover(self, env, client, monkeypatch):
        monkeypatch.setattr(main, "_MAX_UPLOAD_BYTES", 4)
        uid = db.create_unidade("Esposende")
        r = client.post(
            "/admin/kanban-templates",
            data={"nome": "x", "unidade_id": str(uid)},
            files={"file": ("t.png", io.BytesIO(b"x" * 1024), "image/png")},
            headers=_DESKTOP)
        assert r.status_code == 413
        img_dir = env["tmp"] / "template_images"
        assert list(img_dir.iterdir()) == []  # nem canónico nem temp .up_*
        assert db.list_kanban_templates() == []
        assert env["enqueued"] == []

    def test_bad_extension_422(self, env, client):
        uid = db.create_unidade("Esposende")
        r = client.post(
            "/admin/kanban-templates",
            data={"nome": "x", "unidade_id": str(uid)},
            files={"file": ("tpl.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
            headers=_DESKTOP)
        assert r.status_code == 422

    def test_unknown_unidade_422(self, env, client):
        r = client.post(
            "/admin/kanban-templates",
            data={"nome": "x", "unidade_id": "999"},
            files={"file": ("t.png", io.BytesIO(b"p"), "image/png")},
            headers=_DESKTOP)
        assert r.status_code == 422

    def test_mobile_403(self, env, client):
        uid = db.create_unidade("Esposende")
        r = client.post(
            "/admin/kanban-templates",
            data={"nome": "x", "unidade_id": str(uid)},
            files={"file": ("t.png", io.BytesIO(b"p"), "image/png")},
            headers=_MOBILE)
        assert r.status_code == 403


class TestDiscoveryProcessing:
    def test_process_discovery_suggests_spec(self, env, client, monkeypatch):
        uid, r = _create(client)
        tid = r.json()["id"]
        monkeypatch.setattr(main.ocr_runner, "run_discovery", lambda p: dict(_DISC))
        main._process_discovery(tid)
        tpl = db.get_kanban_template(tid)
        assert tpl["status"] == "analisado"
        spec = json.loads(tpl["spec_json"])
        assert spec["row_fields"] == ["of", "modelo", "qtd"]
        assert spec["cross_check_fields"] == ["of", "modelo"]
        disc = json.loads(tpl["discovery_json"])
        assert disc["parse_ok"] is True
        assert disc["raw"]  # audit EN1090

    def test_process_discovery_failure_still_analisado(self, env, client, monkeypatch):
        uid, r = _create(client)
        tid = r.json()["id"]
        monkeypatch.setattr(main.ocr_runner, "run_discovery",
                            lambda p: {"parse_ok": False, "title": "",
                                       "header": [], "columns": [],
                                       "footer": [], "raw": ""})
        main._process_discovery(tid)
        tpl = db.get_kanban_template(tid)
        assert tpl["status"] == "analisado"
        assert json.loads(tpl["discovery_json"])["parse_ok"] is False

    def test_status_endpoint(self, env, client, monkeypatch):
        uid, r = _create(client)
        tid = r.json()["id"]
        monkeypatch.setattr(main.ocr_runner, "run_discovery", lambda p: dict(_DISC))
        main._process_discovery(tid)
        s = client.get(f"/admin/kanban-templates/{tid}/status", headers=_DESKTOP)
        assert s.status_code == 200
        body = s.json()
        assert body["status"] == "analisado"
        assert body["spec"]["row_fields"] == ["of", "modelo", "qtd"]

    def test_process_discovery_exception_sets_error(self, env, client, monkeypatch):
        # R258 — caminho except (run_discovery a LEVANTAR, não parse_ok=False):
        # o template nunca pode ficar preso em 'a_analisar'.
        uid, r = _create(client)
        tid = r.json()["id"]

        def _boom(p):
            raise RuntimeError("boom")

        monkeypatch.setattr(main.ocr_runner, "run_discovery", _boom)
        main._process_discovery(tid)
        tpl = db.get_kanban_template(tid)
        assert tpl["status"] == "analisado"
        disc = json.loads(tpl["discovery_json"])
        assert disc["parse_ok"] is False
        assert "RuntimeError" in disc["error"]

    def test_discovery_failure_preserves_stub_row_fields(self, env, client, monkeypatch):
        # R258 — com parse_ok=False, o worker preserva o que o humano já
        # tiver posto no stub (main._process_discovery :257-260).
        uid, r = _create(client)
        tid = r.json()["id"]
        db.update_kanban_template_spec(tid, json.dumps({
            "name": r.json()["name"], "row_fields": ["of"],
            "setor_aliases": ["CORTE ESPOSENDE"]}))
        monkeypatch.setattr(main.ocr_runner, "run_discovery",
                            lambda p: {"parse_ok": False, "title": "",
                                       "header": [], "columns": [],
                                       "footer": [], "raw": ""})
        main._process_discovery(tid)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        assert spec["row_fields"] == ["of"]
        assert spec["setor_aliases"] == ["CORTE ESPOSENDE"]

    def test_status_returns_real_queue_position(self, env, client, monkeypatch):
        # R258 — o wizard mostrava queue_size (total) sob "posição na fila".
        monkeypatch.setattr(ocr_queue, "_ocr_queue", queue.Queue())
        uid, r = _create(client)
        tid = r.json()["id"]
        ocr_queue._ocr_queue.put(("sheet", 1))
        ocr_queue._ocr_queue.put(("discovery", tid))
        s = client.get(f"/admin/kanban-templates/{tid}/status", headers=_DESKTOP)
        assert s.json()["queue_position"] == 2
        monkeypatch.setattr(ocr_queue, "_ocr_queue", queue.Queue())
        s = client.get(f"/admin/kanban-templates/{tid}/status", headers=_DESKTOP)
        assert s.json()["queue_position"] is None  # em processamento/concluído


class TestSpecAndActivate:
    def _analisado(self, client, monkeypatch):
        uid, r = _create(client)
        tid = r.json()["id"]
        monkeypatch.setattr(main.ocr_runner, "run_discovery", lambda p: dict(_DISC))
        main._process_discovery(tid)
        return uid, tid

    def test_save_spec_and_activate(self, env, client, monkeypatch):
        uid, tid = self._analisado(client, monkeypatch)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        spec["setor_aliases"] = ["CORTE ESPOSENDE"]
        r = client.post(f"/admin/kanban-templates/{tid}/spec",
                        json={"spec": spec}, headers=_DESKTOP)
        assert r.status_code == 200, r.text
        r = client.post(f"/admin/kanban-templates/{tid}/activate", headers=_DESKTOP)
        assert r.status_code == 200
        name = f"u{uid}_corte_esposende"
        assert name in reg.TEMPLATES
        t, reason = reg.detect_template_with_reason("CORTE ESPOSENDE")
        assert t.name == name and reason == "exact_alias"
        assert template_store.unidade_for_template(name) == uid

    def test_spec_409_while_a_analisar(self, env, client, monkeypatch):
        # R258 — gravar spec com a análise ainda na fila seria sobreposto
        # pelo worker (só row_fields do stub sobrevivem, e só com
        # parse_ok=False).
        uid, r = _create(client)
        tid = r.json()["id"]
        spec = {"row_fields": ["of"], "setor_aliases": ["CORTE ESPOSENDE"]}
        r1 = client.post(f"/admin/kanban-templates/{tid}/spec",
                         json={"spec": spec}, headers=_DESKTOP)
        assert r1.status_code == 409
        monkeypatch.setattr(main.ocr_runner, "run_discovery", lambda p: dict(_DISC))
        main._process_discovery(tid)
        r2 = client.post(f"/admin/kanban-templates/{tid}/spec",
                         json={"spec": spec}, headers=_DESKTOP)
        assert r2.status_code == 200, r2.text

    def test_spec_save_on_active_template_reloads_registry(self, env, client, monkeypatch):
        # R258 — edição a quente de um template ativo (main :3097-3098):
        # o alias novo tem de ficar imediatamente detetável.
        uid, tid = self._analisado(client, monkeypatch)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        spec["setor_aliases"] = ["CORTE ESPOSENDE"]
        client.post(f"/admin/kanban-templates/{tid}/spec",
                    json={"spec": spec}, headers=_DESKTOP)
        client.post(f"/admin/kanban-templates/{tid}/activate", headers=_DESKTOP)
        spec["setor_aliases"] = ["CORTE ESPOSENDE", "CORTE NOVO"]
        r = client.post(f"/admin/kanban-templates/{tid}/spec",
                        json={"spec": spec}, headers=_DESKTOP)
        assert r.status_code == 200, r.text
        t, reason = reg.detect_template_with_reason("CORTE NOVO")
        assert t is not None and t.name == f"u{uid}_corte_esposende"

    def test_admin_tab_shows_setor_of_registered(self, env, client, monkeypatch):
        # R258 — inventário por setor: a tabela dos registados mostra o
        # 1º alias do spec; um stub a_analisar (sem aliases) mostra "—".
        uid, r = _create(client)
        page = client.get("/admin/kanbans", headers=_DESKTOP)
        assert "—" in page.text
        tid = r.json()["id"]
        monkeypatch.setattr(main.ocr_runner, "run_discovery", lambda p: dict(_DISC))
        main._process_discovery(tid)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        spec["setor_aliases"] = ["CORTE ESPOSENDE"]
        rs = client.post(f"/admin/kanban-templates/{tid}/spec",
                         json={"spec": spec}, headers=_DESKTOP)
        assert rs.status_code == 200, rs.text
        page = client.get("/admin/kanbans", headers=_DESKTOP)
        assert page.status_code == 200
        assert "CORTE ESPOSENDE" in page.text

    def test_spec_exact_alias_conflict_422(self, env, client, monkeypatch):
        _, tid = self._analisado(client, monkeypatch)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        spec["setor_aliases"] = ["GUILHOTINA"]  # builtin — roubaria folhas
        r = client.post(f"/admin/kanban-templates/{tid}/spec",
                        json={"spec": spec}, headers=_DESKTOP)
        assert r.status_code == 422
        assert any(c["kind"] == "exact" for c in r.json()["conflicts"])

    def test_activate_without_alias_422(self, env, client, monkeypatch):
        _, tid = self._analisado(client, monkeypatch)
        r = client.post(f"/admin/kanban-templates/{tid}/activate", headers=_DESKTOP)
        assert r.status_code == 422  # spec sugerido não tem aliases

    def test_activate_from_draft_409(self, env, client):
        uid = db.create_unidade("Esposende")
        tid = db.insert_kanban_template("u9_x", uid, "{}", status="draft")
        r = client.post(f"/admin/kanban-templates/{tid}/activate", headers=_DESKTOP)
        assert r.status_code == 409

    def test_deactivate_removes_from_registry(self, env, client, monkeypatch):
        uid, tid = self._analisado(client, monkeypatch)
        spec = json.loads(db.get_kanban_template(tid)["spec_json"])
        spec["setor_aliases"] = ["CORTE ESPOSENDE"]
        client.post(f"/admin/kanban-templates/{tid}/spec",
                    json={"spec": spec}, headers=_DESKTOP)
        client.post(f"/admin/kanban-templates/{tid}/activate", headers=_DESKTOP)
        name = f"u{uid}_corte_esposende"
        assert name in reg.TEMPLATES
        r = client.post(f"/admin/kanban-templates/{tid}/deactivate", headers=_DESKTOP)
        assert r.status_code == 200
        assert name not in reg.TEMPLATES  # kill-switch imediato


class TestDelete:
    def test_delete_without_sheets(self, env, client):
        uid, r = _create(client)
        tid = r.json()["id"]
        d = client.post(f"/admin/kanban-templates/{tid}/delete", headers=_DESKTOP)
        assert d.status_code == 200
        assert db.get_kanban_template(tid) is None

    def test_delete_removes_image_file(self, env, client):
        # R258 — o delete tem de apagar também o ficheiro em template_images/
        uid, r = _create(client)
        tid = r.json()["id"]
        img = env["tmp"] / db.get_kanban_template(tid)["image_path"]
        assert img.exists()
        d = client.post(f"/admin/kanban-templates/{tid}/delete", headers=_DESKTOP)
        assert d.status_code == 200
        assert not img.exists()

    def test_delete_with_sheets_409(self, env, client):
        uid, r = _create(client)
        tid = r.json()["id"]
        name = r.json()["name"]
        with db.conn() as c:
            c.execute(
                "INSERT INTO sheets (image_path, status, sheet_data) VALUES (?,?,?)",
                ("s.jpg", "validated",
                 json.dumps({"template_name": name, "header": {}})))
        d = client.post(f"/admin/kanban-templates/{tid}/delete", headers=_DESKTOP)
        assert d.status_code == 409
        assert db.get_kanban_template(tid) is not None


class TestStartupRecovery:
    def test_startup_reenqueues_orphan_discoveries(self, env, monkeypatch):
        # R258 — processo morto a meio da análise: o template fica
        # 'a_analisar' e o startup tem de o repor na fila (main :306-313).
        uid = db.create_unidade("Esposende")
        tid = db.insert_kanban_template(
            f"u{uid}_orfao", uid, "{}", status="a_analisar")
        db.insert_kanban_template(
            f"u{uid}_feito", uid, "{}", status="analisado")
        monkeypatch.setattr(main.ocr_queue, "start_worker", lambda *a, **k: None)
        monkeypatch.setattr(main.ocr_queue, "recover_pending", lambda *a, **k: 0)
        monkeypatch.setattr(main.ref_importer, "start_background_importer",
                            lambda: False)
        main._startup()
        assert env["enqueued"] == [tid]  # só o órfão, não o 'analisado'


class TestRefsLookup:
    def test_lote_tier(self, env, client, monkeypatch):
        class _W:
            def get_refs(self):
                return {"lotes_sap_full": {"L123": {"esp": 4.0, "larg": 1500}}}

        monkeypatch.setattr(main, "get_watcher", lambda: _W())
        r = client.get("/admin/refs-lookup?q=l123", headers=_DESKTOP)
        data = r.json()
        assert data["found"] is True and data["mode"] == "lote"
        assert data["lote"]["esp"] == 4.0

    def test_not_found(self, env, client, monkeypatch):
        class _W:
            def get_refs(self):
                return {}

        monkeypatch.setattr(main, "get_watcher", lambda: _W())
        r = client.get("/admin/refs-lookup?q=999999", headers=_DESKTOP)
        assert r.json()["found"] is False

    def test_empty_query(self, env, client):
        r = client.get("/admin/refs-lookup", headers=_DESKTOP)
        assert r.json()["mode"] == "none"
