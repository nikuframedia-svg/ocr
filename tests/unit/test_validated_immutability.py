"""R257 — imutabilidade de folhas validadas + gate needs_review no validate.

Round 50 declarou "folha validada é final", mas /reprocess e /resolve-side
não verificavam o status: um POST direto punha a folha em 'pending' e o
worker reescrevia raw/sheet_data/dq e revertia para 'extracted'. E o
/validate não verificava needs_review — o depósito do CSV no validate
contornava a guarda do worker que suspende folhas de lado duvidoso.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web import db, main

_DESKTOP = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    # Isola efeitos de disco / threads do validate.
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "_spawn_shadow_scoring", lambda *a, **k: None)
    monkeypatch.setattr(main.kernel, "emit_event", lambda *a, **k: None)
    return TestClient(main.app)


def _mk_sheet(*, status: str = "extracted", needs_review: bool = False) -> int:
    sid = db.insert_sheet("t.jpg")
    sheet_data = {
        "header": {"operador": "Julio Lima", "n_operador": "537",
                   "data": "15-04-2026"},
        "rows": [],
    }
    db.update_extraction(sheet_id=sid, raw_extraction=sheet_data,
                         dq_audit={}, sheet_data=sheet_data)
    if needs_review:
        db.set_needs_review(sid, "side_indeterminate")
    if status == "validated":
        db.validate_sheet(sid, "Julio Lima")
    return sid


class TestValidatedIsFinal:
    def test_reprocess_validated_sheet_409(self, client):
        sid = _mk_sheet(status="validated")
        r = client.post(f"/sheet/{sid}/reprocess", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 409
        assert db.get_sheet(sid)["status"] == "validated"

    def test_resolve_side_validated_sheet_409(self, client):
        sid = _mk_sheet(status="validated")
        r = client.post(f"/sheet/{sid}/resolve-side", data={"side": "F"},
                        headers=_DESKTOP, follow_redirects=False)
        assert r.status_code == 409
        assert db.get_sheet(sid)["status"] == "validated"

    def test_db_rejects_single_and_batch_edits_after_validation(self, client):
        sid = _mk_sheet(status="validated")

        with pytest.raises(db.SheetValidatedError):
            db.apply_edit(sid, "header.operador", "OUTRO")
        with pytest.raises(db.SheetValidatedError):
            db.apply_edits_batch(sid, [("header.operador", "OUTRO")])

        sheet = db.get_sheet(sid)
        assert sheet["sheet_data"]["header"]["operador"] == "Julio Lima"

    def test_db_rejects_stale_revision_without_partial_write(self, client):
        sid = _mk_sheet()
        revision = db.get_sheet(sid)["revision"]
        db.apply_edit(
            sid,
            "header.operador",
            "PRIMEIRO",
            expected_revision=revision,
        )

        with pytest.raises(db.StaleSheetRevisionError):
            db.apply_edits_batch(
                sid,
                [
                    ("header.operador", "STALE"),
                    ("header.n_operador", "999"),
                ],
                expected_revision=revision,
            )

        header = db.get_sheet(sid)["sheet_data"]["header"]
        assert header["operador"] == "PRIMEIRO"
        assert header["n_operador"] == "537"

    def test_validation_advances_final_revision(self, client):
        sid = _mk_sheet()
        before = db.get_sheet(sid)["revision"]
        returned = db.validate_sheet(sid, "Julio Lima")
        after = db.get_sheet(sid)

        assert returned == before + 1
        assert after["revision"] == returned
        assert after["status"] == "validated"

    def test_all_db_sheet_data_mutators_reject_validated_sheet(self, client):
        sid = _mk_sheet(status="validated")
        replacement = {
            "header": {"operador": "OUTRO"},
            "rows": [{"of": "999999"}],
        }

        with pytest.raises(db.SheetValidatedError):
            db.update_extraction(sid, replacement, {}, replacement)
        with pytest.raises(db.SheetValidatedError):
            db.replace_sheet_data(sid, replacement)
        with pytest.raises(db.SheetValidatedError):
            db.add_row(sid)
        with pytest.raises(db.SheetValidatedError):
            db.delete_row(sid, 0)
        with pytest.raises(db.SheetValidatedError):
            db.validate_sheet(sid, "OUTRO")

    def test_replace_sheet_data_rejects_missing_and_stale_sheet(self, client):
        replacement = {"header": {}, "rows": []}
        with pytest.raises(ValueError, match="not found"):
            db.replace_sheet_data(999999, replacement)

        sid = _mk_sheet()
        stale_revision = db.get_sheet(sid)["revision"]
        db.apply_edit(sid, "header.operador", "NOVO")
        with pytest.raises(db.StaleSheetRevisionError):
            db.replace_sheet_data(
                sid,
                replacement,
                expected_revision=stale_revision,
            )

    def test_reprocess_error_sheet_still_allowed(self, client, monkeypatch,
                                                 tmp_path):
        # O caminho legítimo (Round 59/71: re-processar folha com erro)
        # continua a funcionar.
        monkeypatch.setattr(main.ocr_queue, "enqueue", lambda *_a, **_k: None)
        monkeypatch.setattr(main, "_DATA_DIR", tmp_path)
        (tmp_path / "t.jpg").write_bytes(b"jpg")
        sid = _mk_sheet()
        db.update_status(sid, "error")
        r = client.post(f"/sheet/{sid}/reprocess", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 303
        assert db.get_sheet(sid)["status"] == "pending"


class TestValidateNeedsReviewGate:
    def test_validate_needs_review_sheet_409(self, client):
        sid = _mk_sheet(needs_review=True)
        r = client.post(f"/sheet/{sid}/validate", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 409
        assert db.get_sheet(sid)["status"] != "validated"

    def test_validate_clean_sheet_still_works(self, client):
        sid = _mk_sheet()
        r = client.post(f"/sheet/{sid}/validate", headers=_DESKTOP,
                        follow_redirects=False)
        assert r.status_code == 303
        assert db.get_sheet(sid)["status"] == "validated"
