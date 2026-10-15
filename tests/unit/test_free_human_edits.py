"""Human text is authoritative; plan lookup is optional review assistance."""
from copy import deepcopy
import io
import json

import openpyxl

from fastapi.testclient import TestClient
import pytest

from app.web import db, main
from app.web import export

DESKTOP = {"User-Agent": "Mozilla/5.0 desktop"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    for name in ("_start_sheet_cross_check", "_spawn_shadow_scoring",
                 "_deposit_csv_to_factory", "_invalidate_production_caches"):
        monkeypatch.setattr(main, name, lambda *a, **k: None)
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)
    monkeypatch.setattr("app.learning.scheduler.maybe_trigger_learning", lambda: None)
    data = {"template_name": "gemini", "header": {"operador": "", "n_operador": "",
            "data": "data por escrever", "setor_maquina": "HD36"},
            "footer": {}, "rows": [
                {"of": "265958", "modelo": "ESCADAS TL465-101", "qtd": "4",
                 "reference_review": "pending"} for _ in range(5)]}
    sid = db.insert_sheet("synthetic.jpg")
    db.update_extraction(sid, deepcopy(data), {}, deepcopy(data))
    return sid, TestClient(main.app, headers=DESKTOP)


def post_row(env, values, **extra):
    sid, client = env
    return client.post(f"/sheet/{sid}/rows/4/edit", json={
        "expected_revision": db.get_sheet(sid)["revision"], "values": values, **extra})


def test_image_7955_manual_of_never_becomes_suggested_of(env, monkeypatch):
    sid, _ = env
    before = db.get_sheet(sid)["raw_extraction"]
    monkeypatch.setattr(main, "get_watcher", lambda: pytest.fail("Manual edits must not need a reference"))
    response = post_row(env, {"of": "266303"}, suggestion_for="266303", entry={"of": "265958"})
    assert response.status_code == 200
    row = db.get_sheet(sid)["sheet_data"]["rows"][4]
    assert row["of"] == "266303"
    assert row["modelo"] == "ESCADAS TL465-101"
    assert row["reference_review"] == ""
    assert db.get_sheet(sid)["raw_extraction"] == before


@pytest.mark.parametrize("value", ["", "  livre / sem catálogo  ", "quantidade por contar", "0,00"])
def test_manual_text_and_blanks_are_exact_and_survive_rebuild_and_plan_proposal(env, value):
    sid, _ = env
    response = post_row(env, {"of": "266303", "modelo": value, "qtd": value})
    assert response.status_code == 200
    main._rebuild_sheet_data_from_raw(sid, db.get_sheet(sid))
    protected = main._human_edited_paths(sid)
    for field in ("of", "modelo", "qtd"):
        cell = {"engine_status": "very_different", "value": "OLD_PLAN_VALUE",
                "source": "plan", "ref_source": "plan"}
        assert not main._maybe_apply_snap(sid, f"rows[4].{field}", cell, protected)
    row = db.get_sheet(sid)["sheet_data"]["rows"][4]
    assert row["of"] == "266303"
    assert row["modelo"] == value
    assert row["qtd"] == value


def test_validation_accepts_pending_references_and_free_header(env):
    sid, client = env
    before = deepcopy(db.get_sheet(sid)["sheet_data"])
    response = client.post(f"/sheet/{sid}/validate", follow_redirects=False,
                           data={"expected_revision": db.get_sheet(sid)["revision"]})
    assert response.status_code == 303
    sheet = db.get_sheet(sid)
    assert sheet["status"] == "validated"
    assert sheet["sheet_data"] == before


def test_stale_row_or_validation_cannot_erase_concurrent_edit(env):
    sid, client = env
    stale = db.get_sheet(sid)["revision"]
    db.apply_edit(sid, "rows[4].of", "CONCURRENT")
    response = post_row(env, {"of": "266303"}, expected_revision=stale)
    assert response.status_code == 409
    response = client.post(f"/sheet/{sid}/validate", data={"expected_revision": stale})
    assert response.status_code == 409
    sheet = db.get_sheet(sid)
    assert sheet["status"] == "extracted"
    assert sheet["sheet_data"]["rows"][4]["of"] == "CONCURRENT"


def test_legacy_header_submission_cannot_adopt_a_concurrent_revision(env, monkeypatch):
    sid, client = env
    original = db.apply_edits_batch
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        db.apply_edit(sid, "rows[4].of", "SECOND_OPERATOR")
        return result
    monkeypatch.setattr(db, "apply_edits_batch", race)
    response = client.post(f"/sheet/{sid}/validate", data={"operador": "NOVO", "data": "15/10/2026"})
    assert response.status_code == 409
    assert db.get_sheet(sid)["status"] == "extracted"
    assert db.get_sheet(sid)["sheet_data"]["rows"][4]["of"] == "SECOND_OPERATOR"


def test_invalid_paths_do_not_write_internal_fields(env):
    response = post_row(env, {"_deleted": "true"})
    assert response.status_code == 400


def test_exports_preserve_literal_human_cells_without_changing_numeric_projection(env, monkeypatch):
    sid, _ = env
    assert post_row(env, {"qtd": "por contar", "comp_mm": " 0,00 ", "modelo": "", "fecho": "SIM"}).status_code == 200
    db.apply_edit(sid, "header.data", "data escrita livremente")
    db.apply_edit(sid, "header.n_operador", " 0001 ")
    class Watcher:
        def get_refs(self):
            return {}
    monkeypatch.setattr("app.cross_check.get_watcher", lambda: Watcher())
    cpis = openpyxl.load_workbook(io.BytesIO(export.build_cpis_workbook(None, None)), data_only=True).active
    columns = {label: i for i, (_key, label) in enumerate(export.CPIS_COLUMNS)}
    values = list(cpis.values)[-1]
    assert values[columns["OF"]] == "265958"
    assert values[columns["QTD"]] == "por contar"
    assert values[columns["Comprimento (mm)"]] == 0
    assert values[columns["Data"]] == "data escrita livremente"
    assert values[columns["Cód. Funcionário"]] == 10000001
    assert values[columns["Fechar bobine"]] == "SIM"
    assert values[columns["Modelo"]] is None  # Empty cells round-trip as None in XLSX.
    book = openpyxl.load_workbook(io.BytesIO(export.export_excel(None, None)), data_only=True)
    assert any("por contar" in row for sheet in book.worksheets[1:] for row in sheet.values)
    with db.conn() as conn:
        numeric = conn.execute("SELECT qtd FROM production_rows WHERE sheet_id=? AND row_index=4", (sid,)).fetchone()[0]
    assert numeric is None  # KPI arithmetic does not add invented quantities.


def timing_events(capsys):
    lines = capsys.readouterr().err.splitlines()
    return [json.loads(line) for line in lines if line.startswith('{"event": "http_')]


def test_timing_records_rejected_mutations_without_private_values(env, capsys):
    sid, client = env
    response = client.post(f"/sheet/{sid}/rows/4/edit?private=HIDDEN_QUERY", json={
        "expected_revision": -1, "values": {"of": "HIDDEN_BODY"}})
    assert response.status_code == 409
    events = timing_events(capsys)
    assert [event["event"] for event in events] == ["http_request_start", "http_timing"]
    first, last = events
    assert first["request_id"] == last["request_id"]
    assert last["action"] == "edit" and last["status"] == 409
    assert last["duration_ms"] >= 0
    assert "HIDDEN" not in json.dumps(events)


def test_probe_events_are_marked_separately_from_real_users(env, capsys):
    sid, client = env
    response = client.post(f"/sheet/{sid}/rows/4/edit", headers={"X-OCR-Probe": "1"}, json={
        "expected_revision": -1, "values": {"of": "266303"}})
    assert response.status_code == 409
    events = timing_events(capsys)
    assert len(events) == 2
    assert all(event["synthetic"] is True for event in events)


def test_timing_records_unhandled_failure(env, monkeypatch, capsys):
    sid, _ = env
    def fail(*a, **k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(db, "apply_edits_batch", fail)
    client = TestClient(main.app, headers=DESKTOP, raise_server_exceptions=False)
    response = client.post(f"/sheet/{sid}/rows/4/edit", json={
        "expected_revision": db.get_sheet(sid)["revision"], "values": {"of": "266303"}})
    assert response.status_code == 500
    events = timing_events(capsys)
    assert events[-1]["status"] == 500
    assert events[-1]["action"] == "edit"
    assert events[0]["request_id"] == events[-1]["request_id"]


def test_browser_timing_has_separate_values_and_never_logs_payload_text(env, capsys):
    _, client = env
    response = client.post("/telemetry/interaction", json={
        "action": "edit", "duration_ms": 156.25, "result": "ok", "private": "HIDDEN_TEXT"})
    assert response.status_code == 204
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event == {"event": "browser_timing", "action": "edit", "duration_ms": 156.2, "result": "ok"}
    assert client.post("/telemetry/interaction", json={
        "action": "edit", "duration_ms": -1, "result": "ok"}).status_code == 400


def test_latency_status_returns_the_watcher_snapshot_without_cache(env, monkeypatch, tmp_path):
    _, client = env
    path = tmp_path / "status.json"
    path.write_text('{"available":true,"sample_count":0}')
    monkeypatch.setattr(main, "_LATENCY_STATUS_PATH", path)
    response = client.get("/admin/latency-status")
    assert response.status_code == 200
    assert response.json()["sample_count"] == 0
    assert response.headers["cache-control"] == "no-store"


def test_mobile_can_edit_the_same_free_fields(env):
    sid, client = env
    response = client.post(f"/sheet/{sid}/rows/4/edit", headers={"User-Agent": "iPhone"}, json={
        "expected_revision": db.get_sheet(sid)["revision"], "values": {"of": "266303"}})
    assert response.status_code == 200
    assert db.get_sheet(sid)["sheet_data"]["rows"][4]["of"] == "266303"
