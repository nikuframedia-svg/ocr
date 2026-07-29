"""Controlled correction of ``header.data`` after sheet validation."""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from app.cross_check import storage
from app.learning import metrics as learning_metrics
from app.pipeline import obras_status, of_consumption
from app.web import db, main
from fastapi.testclient import TestClient

_DESKTOP = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "X-Forwarded-User": "supervisor@example.test",
}
_MOBILE = {"User-Agent": "Mozilla/5.0 (iPhone; Mobile)"}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def client():
    return TestClient(main.app)


def _mk_sheet(*, validated: bool = True, data: str = "15-04-2026") -> int:
    sid = db.insert_sheet("images/date-correction.jpg")
    sheet_data = {
        "template_name": "bobine_formato",
        "header": {
            "operador": "JULIO LIMA",
            "n_operador": "537",
            "data": data,
            "setor_maquina": "BOBINE-FORMATO",
            "cod_maquina": "M032",
        },
        "rows": [{
            "of": "262107",
            "modelo": "CFC5F45RIV",
            "qtd": "4",
            "lote": "H26B0546",
            "larg_mm": "1200",
            "esp": "2.6",
        }],
        "footer": {"horas_trabalhadas": "8"},
    }
    raw = copy.deepcopy(sheet_data)
    db.update_extraction(
        sheet_id=sid,
        raw_extraction=raw,
        dq_audit={"cells": {}},
        sheet_data=sheet_data,
    )
    if validated:
        db.validate_sheet(sid, "JULIO LIMA")
    return sid


def _audit_rows(sheet_id: int) -> list[dict]:
    with db.conn() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT id, field_path, old_value, new_value, source, actor, "
                "reason, sheet_revision FROM edits WHERE sheet_id = ? "
                "ORDER BY id",
                (sheet_id,),
            ).fetchall()
        ]


def test_db_correction_is_narrow_transactional_and_audited(tmp_db):
    sid = _mk_sheet()
    db.set_factory_csv_name(sid, "JulioLima_2026.04.15.csv")
    before = db.get_sheet(sid)
    before_data = copy.deepcopy(before["sheet_data"])
    before_raw = copy.deepcopy(before["raw_extraction"])

    result = db.correct_validated_date(
        sid,
        "2026-04-29",
        expected_revision=before["revision"],
        actor="supervisor",
        reason="Data escrita incorretamente",
    )

    after = db.get_sheet(sid)
    expected_data = copy.deepcopy(before_data)
    expected_data["header"]["data"] = "29-04-2026"
    assert after["sheet_data"] == expected_data
    assert after["raw_extraction"] == before_raw
    assert after["status"] == "validated"
    assert after["validated_at"] == before["validated_at"]
    assert after["operador"] == before["operador"]
    assert after["factory_csv_name"] == "JulioLima_2026.04.15.csv"
    assert after["revision"] == before["revision"] + 1

    assert result == {
        "sheet_id": sid,
        "old_date": "15-04-2026",
        "new_date": "29-04-2026",
        "new_date_iso": "2026-04-29",
        "revision": after["revision"],
        "status": "validated",
        "audit_id": result["audit_id"],
        "actor": "supervisor",
        "reason": "Data escrita incorretamente",
    }
    assert isinstance(result["audit_id"], int)

    with db.conn() as c:
        production = dict(c.execute(
            "SELECT sheet_date, sheet_iso_date, sheet_status, validated_at, "
            "qtd, lote FROM production_rows WHERE sheet_id = ?",
            (sid,),
        ).fetchone())
    assert production["sheet_date"] == "29-04-2026"
    assert production["sheet_iso_date"] == "2026-04-29"
    assert production["sheet_status"] == "validated"
    assert production["validated_at"] == before["validated_at"]
    assert production["qtd"] == 4
    assert production["lote"] == "H26B0546"

    audit = _audit_rows(sid)
    assert audit == [{
        "id": result["audit_id"],
        "field_path": "header.data",
        "old_value": "15-04-2026",
        "new_value": "29-04-2026",
        "source": db.VALIDATED_DATE_CORRECTION_SOURCE,
        "actor": "supervisor",
        "reason": "Data escrita incorretamente",
        "sheet_revision": after["revision"],
    }]
    assert learning_metrics.corrections_per_sheet() == 0.0


def test_multiple_corrections_keep_chain_and_optional_reason(tmp_db):
    sid = _mk_sheet()
    rev1 = db.get_sheet(sid)["revision"]
    first = db.correct_validated_date(
        sid, "2026-04-16", expected_revision=rev1, reason=""
    )
    second = db.correct_validated_date(
        sid,
        "2026-04-17",
        expected_revision=first["revision"],
        actor="chefe",
        reason="confirmação",
    )

    assert second["revision"] == rev1 + 2
    assert db.get_sheet(sid)["sheet_data"]["header"]["data"] == "17-04-2026"
    rows = _audit_rows(sid)
    assert [(r["old_value"], r["new_value"]) for r in rows] == [
        ("15-04-2026", "16-04-2026"),
        ("16-04-2026", "17-04-2026"),
    ]
    assert rows[0]["reason"] is None
    assert rows[0]["actor"] == "web"
    assert rows[1]["reason"] == "confirmação"


@pytest.mark.parametrize(
    "bad_date",
    [
        "",
        "29-04-2026",
        "2026-02-30",
        "2023-12-31",
        "2031-01-01",
    ],
)
def test_invalid_dates_do_not_write(tmp_db, bad_date):
    sid = _mk_sheet()
    before = db.get_sheet(sid)

    with pytest.raises(ValueError):
        db.correct_validated_date(
            sid, bad_date, expected_revision=before["revision"]
        )

    after = db.get_sheet(sid)
    assert after["sheet_data"] == before["sheet_data"]
    assert after["revision"] == before["revision"]
    assert _audit_rows(sid) == []


def test_same_date_unvalidated_and_stale_revision_are_rejected(tmp_db):
    validated = _mk_sheet()
    current = db.get_sheet(validated)
    with pytest.raises(db.NoDateChangeError):
        db.correct_validated_date(
            validated,
            "2026-04-15",
            expected_revision=current["revision"],
        )
    with pytest.raises(db.StaleSheetRevisionError):
        db.correct_validated_date(
            validated,
            "2026-04-16",
            expected_revision=current["revision"] - 1,
        )

    extracted = _mk_sheet(validated=False)
    extracted_revision = db.get_sheet(extracted)["revision"]
    with pytest.raises(db.SheetNotValidatedError):
        db.correct_validated_date(
            extracted,
            "2026-04-16",
            expected_revision=extracted_revision,
        )

    assert _audit_rows(validated) == []
    assert _audit_rows(extracted) == []


def test_sqlite_failure_rolls_back_everything(tmp_db, monkeypatch):
    sid = _mk_sheet()
    before = db.get_sheet(sid)

    def fail_sync(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "_sync_production_rows", fail_sync)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        db.correct_validated_date(
            sid,
            "2026-04-29",
            expected_revision=before["revision"],
        )

    after = db.get_sheet(sid)
    assert after["sheet_data"] == before["sheet_data"]
    assert after["revision"] == before["revision"]
    assert _audit_rows(sid) == []


def test_endpoint_success_updates_internal_side_effects_only(
    tmp_db, client, monkeypatch,
):
    sid = _mk_sheet()
    db.set_factory_csv_name(sid, "JulioLima_2026.04.15.csv")
    before = db.get_sheet(sid)
    cache_calls: list[str] = []
    cross_calls: list[tuple[set[int], str | None]] = []
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        of_consumption, "invalidate_cache",
        lambda: cache_calls.append("consumption"),
    )
    monkeypatch.setattr(
        obras_status, "invalidate_cache",
        lambda: cache_calls.append("obras"),
    )
    monkeypatch.setattr(
        main,
        "_start_sheet_cross_check",
        lambda ids, profile_trigger=None: cross_calls.append(
            (set(ids), profile_trigger)
        ),
    )
    monkeypatch.setattr(
        main.kernel,
        "emit_event",
        lambda kind, payload: events.append((kind, payload)),
    )
    monkeypatch.setattr(
        main,
        "_deposit_csv_to_factory",
        lambda *_a, **_k: pytest.fail("factory deposit must not run"),
    )

    response = client.post(
        f"/sheet/{sid}/correct-date",
        data={
            "new_date": "2026-04-29",
            "reason": "",
            "expected_revision": before["revision"],
        },
        headers=_DESKTOP,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["new_date"] == "29-04-2026"
    assert payload["new_date_iso"] == "2026-04-29"
    assert payload["revision"] == before["revision"] + 1
    assert payload["actor"] == "supervisor@example.test"
    assert payload["reason"] is None
    assert cache_calls == ["consumption", "obras"]
    assert cross_calls == [({sid}, "sheet_date_correction")]
    assert events[0][0] == "sheet_date_corrected"
    assert events[0][1]["audit_id"] == payload["audit_id"]
    assert db.get_sheet(sid)["factory_csv_name"] == "JulioLima_2026.04.15.csv"


def test_endpoint_rejections_and_database_error_are_not_success(
    tmp_db, client, monkeypatch,
):
    validated = _mk_sheet()
    revision = db.get_sheet(validated)["revision"]
    extracted = _mk_sheet(validated=False)

    mobile = client.post(
        f"/sheet/{validated}/correct-date",
        data={
            "new_date": "2026-04-29",
            "expected_revision": revision,
        },
        headers=_MOBILE,
    )
    assert mobile.status_code == 403

    not_validated = client.post(
        f"/sheet/{extracted}/correct-date",
        data={
            "new_date": "2026-04-29",
            "expected_revision": db.get_sheet(extracted)["revision"],
        },
        headers=_DESKTOP,
    )
    assert not_validated.status_code == 409

    stale = client.post(
        f"/sheet/{validated}/correct-date",
        data={
            "new_date": "2026-04-29",
            "expected_revision": revision - 1,
        },
        headers=_DESKTOP,
    )
    assert stale.status_code == 409

    invalid = client.post(
        f"/sheet/{validated}/correct-date",
        data={
            "new_date": "2026-02-30",
            "expected_revision": revision,
        },
        headers=_DESKTOP,
    )
    assert invalid.status_code == 400

    monkeypatch.setattr(
        db,
        "correct_validated_date",
        lambda *_a, **_k: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    failed = client.post(
        f"/sheet/{validated}/correct-date",
        data={
            "new_date": "2026-04-29",
            "expected_revision": revision,
        },
        headers=_DESKTOP,
    )
    assert failed.status_code == 500
    assert failed.json()["detail"] == "Não foi possível guardar a correção da data"


def test_validated_date_button_only_appears_in_canonical_validated_views(
    tmp_db, client, monkeypatch,
):
    validated = _mk_sheet()
    extracted = _mk_sheet(validated=False)
    monkeypatch.setattr(
        main, "_build_cc_maps", lambda *_a, **_k: ({}, {}, {}, {}, {}, {})
    )
    monkeypatch.setattr(main, "_get_operadores", lambda: ("JULIO LIMA",))

    final_view = client.get(f"/sheet/{validated}", headers=_DESKTOP)
    assert final_view.status_code == 200
    assert "Corrigir data" in final_view.text
    assert "/sheet/' + config.sheetId + '/correct-date" in final_view.text

    raw_view = client.get(f"/sheet/{validated}?view=raw", headers=_DESKTOP)
    assert raw_view.status_code == 200
    assert "Corrigir data" not in raw_view.text

    editable_view = client.get(f"/sheet/{extracted}", headers=_DESKTOP)
    assert editable_view.status_code == 200
    assert "Corrigir data" not in editable_view.text

    viewer = client.get(
        f"/kanbans?status=validated&sheet_id={validated}", headers=_DESKTOP
    )
    assert viewer.status_code == 200
    assert "Corrigir data" in viewer.text
    assert "kanbanViewer: true" in viewer.text
    assert "cell-locked" in viewer.text
    assert f'hx-post="/sheet/{validated}/edit"' not in viewer.text


def _cross_result() -> dict:
    return {
        "checked_at": "2026-04-29T10:00:00+00:00",
        "summary": {"match": 0, "no_match": 0, "na": 0, "total": 0},
        "rows": [],
        "header": {},
        "footer": {},
        "to_analisar": [],
    }


def test_cross_check_started_before_correction_cannot_store_stale_date(
    tmp_db, monkeypatch,
):
    sid = _mk_sheet()
    before = db.get_sheet(sid)
    entered = threading.Event()
    release = threading.Event()
    stored: list[dict] = []

    class Watcher:
        plan_path = None

        def get_refs(self):
            return {"available": True, "loaded_at": "test"}

    def blocking_cross(*_args, **_kwargs):
        entered.set()
        assert release.wait(5), "test did not release cross-check"
        return _cross_result()

    watcher = Watcher()
    monkeypatch.setattr(main, "get_watcher", lambda: watcher)
    monkeypatch.setattr(main, "cross_check_sheet", blocking_cross)
    monkeypatch.setattr(
        main, "store_cross_check", lambda **kwargs: stored.append(kwargs)
    )
    monkeypatch.setattr(main, "_spawn_shadow_scoring", lambda *_a, **_k: None)

    worker = threading.Thread(
        target=main._run_and_store_cross_check, args=(sid,)
    )
    worker.start()
    assert entered.wait(5), "cross-check did not start"

    db.correct_validated_date(
        sid,
        "2026-04-29",
        expected_revision=before["revision"],
    )
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert stored == []

    main._run_and_store_cross_check(sid)
    assert len(stored) == 1
    assert stored[0]["date_iso"] == "2026-04-29"


def test_cross_check_storage_relocates_file_without_orphan(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(storage, "_base_dir", lambda: tmp_path)
    first = storage.store_cross_check(
        sheet_id=7,
        image_path="images/a.jpg",
        operador="JULIO LIMA",
        date_iso="2026-04-15",
        sheet_status="validated",
        cross_check_result=_cross_result(),
    )
    first_path = Path(first["file"])
    assert first_path.exists()

    second = storage.store_cross_check(
        sheet_id=7,
        image_path="images/a.jpg",
        operador="JULIO LIMA",
        date_iso="2026-04-29",
        sheet_status="validated",
        cross_check_result=_cross_result(),
    )
    second_path = Path(second["file"])
    assert second_path.exists()
    assert not first_path.exists()

    index = json.loads((tmp_path / "_index.json").read_text(encoding="utf-8"))
    entry = index["sheets"]["7"]
    assert entry["date"] == "2026-04-29"
    assert entry["file"] == second["rel_key"]
