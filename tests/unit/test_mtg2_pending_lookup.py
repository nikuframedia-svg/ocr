"""Sector quantities, original plan row selection and explicit closed status."""
from __future__ import annotations

import json

import pytest
from app.pipeline import of_consumption as ofc
from app.pipeline.plan_status import closed_status
from app.web import db, main
from fastapi.testclient import TestClient


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()
    ofc.invalidate_cache()
    monkeypatch.setattr(ofc, "_plan_cutoff_iso", lambda: "2026-10-08")
    refs = {
        "plan_sha256": "snapshot-A",
        "of_to_entries": {"266068": [{
            "ov": "2600001", "designacao": "FST026909", "comp": 1076,
            "quanttrp": 20, "fases": {"c": 20, "a": 10, "exp": 0}, "fechado": "0",
        }]},
        "maquinas_by_kanban": {
            "ACABAMENTO MTG2": {"codmaq": "M001", "colunaexcel": "a"},
            "CORTE": {"codmaq": "M002", "colunaexcel": "c"},
        },
    }

    class Watcher:
        def get_refs(self):
            return refs

    monkeypatch.setattr(main, "get_watcher", Watcher)
    monkeypatch.setattr(main, "_start_sheet_cross_check", lambda *a, **k: None)
    sid = db.insert_sheet("test.jpg")
    data = {"template_name": "acabamento", "header": {"setor_maquina": "ACABAMENTO MTG2"},
            "rows": [{"of": "266068", "modelo": "OLD", "qtd": "4"}], "footer": {}}
    db.update_extraction(sid, data, {}, data)
    yield sid, refs, TestClient(main.app)
    ofc.invalidate_cache()


def _production(sector, quantity, *, date="2026-10-08", status="validated"):
    sid = db.insert_sheet("production.jpg")
    with db.conn() as conn:
        conn.execute("UPDATE sheets SET status=?, sheet_data=? WHERE id=?",
                     (status, json.dumps({"header": {"setor_maquina": sector}}), sid))
        conn.execute("""INSERT INTO production_rows
                        (sheet_id,row_index,sheet_iso_date,sheet_status,of,modelo,qtd)
                        VALUES (?,0,?,?, '266068','FST026909',?)""", (sid, date, status, quantity))


@pytest.mark.parametrize(("value", "expected"), [
    ("0", False), (0, False), ("0.0", False), (False, False),
    ("1", True), (1, True), ("1.0", True), (True, True),
    (" true ", True), (None, None), ("", None), ("X", None),
])
def test_explicit_plan_status(value, expected):
    assert closed_status(value) is expected


def test_lookup_counts_only_validated_same_phase_after_snapshot(setup):
    sid, _refs, client = setup
    _production("ACABAMENTO MTG2", 2)
    _production("CORTE", 99)
    _production("ACABAMENTO MTG2", 99, date="2026-10-07")
    _production("ACABAMENTO MTG2", 99, status="extracted")
    result = client.get(f"/sheet/{sid}/of-lookup?q=266068").json()
    entry = result["entries"][0]
    assert result["phase"] == "a"
    assert result["setor"] == "ACABAMENTO MTG2"
    assert entry["remaining"] == 8
    assert entry["produced_erp"] == 10
    assert entry["kanban_qty"] == 2
    assert entry["fechado"] is False  # string "0" must never become True
    assert entry["status_known"] is True


def test_sector_completion_does_not_close_open_of(setup):
    sid, refs, client = setup
    refs["of_to_entries"]["266068"][0]["fases"]["a"] = 20
    assert client.get(f"/sheet/{sid}/of-lookup?q=266068").json()["entries"] == []
    e = client.get(f"/sheet/{sid}/of-lookup?q=266068&include_done=1").json()["entries"][0]
    assert e["remaining"] == 0 and e["done"] is True
    assert e["fechado"] is False
    refs["of_to_entries"]["266068"][0]["fechado"] = "1"
    e = client.get(f"/sheet/{sid}/of-lookup?q=266068&include_done=1").json()["entries"][0]
    assert e["fechado"] is True


@pytest.mark.parametrize("missing", ["mapping", "phase", "quantity", "invalid_phase"])
def test_missing_data_returns_unavailable_instead_of_false_quantity(setup, missing):
    sid, refs, client = setup
    entry = refs["of_to_entries"]["266068"][0]
    if missing == "mapping":
        refs["maquinas_by_kanban"] = {}
    elif missing == "phase":
        del entry["fases"]["a"]
    elif missing == "quantity":
        entry["quanttrp"] = None
    else:
        entry["fases"]["a"] = "NaN"
    result = client.get(f"/sheet/{sid}/of-lookup?q=266068").json()["entries"][0]
    assert result["remaining"] is None
    assert result["pending_valid"] is False
    assert result["done"] is False


def test_missing_status_is_not_reported_as_open(setup):
    sid, refs, client = setup
    refs["of_to_entries"]["266068"][0]["fechado_known"] = False
    result = client.get(f"/sheet/{sid}/of-lookup?q=266068").json()["entries"][0]
    assert result["status_known"] is False


def test_filters_and_pages_keep_duplicate_plan_rows_and_original_indices(setup):
    sid, refs, client = setup
    original = refs["of_to_entries"]["266068"][0]
    refs["of_to_entries"]["266068"] = [
        {**original, "comp": i, "quanttrp": 100 + i} for i in range(55)
    ]
    first = client.get(f"/sheet/{sid}/of-lookup?q=266068").json()
    second = client.get(f"/sheet/{sid}/of-lookup?q=266068&offset=50").json()
    assert first["total"] == 55 and first["has_more"] is True
    assert [e["orig_idx"] for e in second["entries"]] == list(range(50, 55))
    assert second["has_more"] is False
    filtered = client.get(f"/sheet/{sid}/of-lookup?modelo=FST026&comp_mm=53").json()
    assert filtered["entries"][0]["orig_idx"] == 53
    response = client.post(f"/sheet/{sid}/apply-of-entry", json={
        "row_index": 0, "of": "266068", "entry_idx": 53,
        "expected_revision": filtered["revision"], "plan_sha256": filtered["plan_sha256"],
    })
    assert response.status_code == 200
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["comp_mm"] == "53"
    assert db.get_sheet(sid)["sheet_data"]["rows"][0]["qtd"] == "4"


def test_filtered_sibling_receives_only_its_original_share_of_consumption(setup):
    sid, refs, client = setup
    original = refs["of_to_entries"]["266068"][0]
    refs["of_to_entries"]["266068"] = [
        {**original, "comp": n, "quanttrp": 10, "fases": {"a": 0}} for n in (1000, 2000)
    ]
    _production("ACABAMENTO MTG2", 12)
    filtered = client.get(f"/sheet/{sid}/of-lookup?comp_mm=2000").json()["entries"][0]
    assert filtered["orig_idx"] == 1
    assert filtered["remaining"] == 8 and filtered["kanban_qty"] == 2


@pytest.mark.parametrize("change", ["plan", "sheet"])
def test_selection_rejects_stale_plan_or_sheet(setup, change):
    sid, refs, client = setup
    result = client.get(f"/sheet/{sid}/of-lookup?q=266068").json()
    if change == "plan":
        refs["plan_sha256"] = "snapshot-B"
    else:
        db.apply_edit(sid, "rows[0].qtd", "6")
    before = db.get_sheet(sid)
    response = client.post(f"/sheet/{sid}/apply-of-entry", json={
        "row_index": 0, "of": "266068", "entry_idx": 0,
        "plan_sha256": result["plan_sha256"], "expected_revision": result["revision"],
    })
    assert response.status_code == 409
    assert db.get_sheet(sid)["sheet_data"] == before["sheet_data"]


def test_phase_cache_is_separate_and_invalidates_for_same_day_plan_or_mapping(setup):
    _sid, refs, _client = setup
    _production("ACABAMENTO MTG2", 2)
    _production("CORTE", 7)
    assert ofc.get_consumption("a", refs=refs)[("266068", "FST026909")] == 2
    assert ofc.get_consumption("c", refs=refs)[("266068", "FST026909")] == 7
    _production("ACABAMENTO MTG2", 3)
    refs["plan_sha256"] = "new-snapshot-same-day"
    assert ofc.get_consumption("a", refs=refs)[("266068", "FST026909")] == 5
    refs["maquinas_by_kanban"]["CORTE"]["colunaexcel"] = "a"
    assert ofc.get_consumption("a", refs=refs)[("266068", "FST026909")] == 12
    _production("ACABAMENTO MTG2", 1)
    ofc.invalidate_cache()
    assert ofc.get_consumption("a", refs=refs)[("266068", "FST026909")] == 13


@pytest.mark.parametrize("query", ["comp_mm=abc", "comp_mm=NaN", "offset=-1"])
def test_invalid_filters_are_actionable(setup, query):
    sid, _refs, client = setup
    assert client.get(f"/sheet/{sid}/of-lookup?{query}").status_code == 422
