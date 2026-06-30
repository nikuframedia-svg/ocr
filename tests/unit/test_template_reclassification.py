from __future__ import annotations

from pathlib import Path

from app.web import db

from scripts.diag import template_audit
from scripts.ops import reclassify_templates


def _bobine_acabamento_candidate() -> dict:
    return {
        "template_name": "bobine_formato",
        "header": {
            "operador": "JULIO LIMA",
            "n_operador": "0537",
            "setor_maquina": "ACABAMENTO MTG2",
            "cod_maquina": "M061",
            "data": "25-05-2026",
        },
        "rows": [
            {"of": "262892", "modelo": "CGC2E10D", "qtd": "4"},
        ],
        "footer": {},
    }


def _acabamento_result() -> dict:
    current = {
        "template_name": "acabamento",
        "header": {
            "operador": "JULIO LIMA",
            "n_operador": "0537",
            "setor_maquina": "ACABAMENTO MTG2",
            "cod_maquina": "M061",
            "data": "25-05-2026",
        },
        "rows": [
            {"of": "262892", "modelo": "CGC2E10D", "qtd": "4"},
        ],
        "footer": {},
    }
    return {"raw": current, "current": current, "dq": {}}


def _init_tmp_db(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(db, "_DB_PATH", db_path)
    db.init_db()
    return db_path


def _insert_candidate(image_path: str) -> int:
    sheet_id = db.insert_sheet(image_path)
    data = _bobine_acabamento_candidate()
    db.update_extraction(
        sheet_id,
        raw_extraction=data,
        dq_audit={},
        sheet_data=data,
    )
    return sheet_id


def test_template_audit_proposes_acabamento_for_bobine_with_acabamento_setor(
    tmp_path,
    monkeypatch,
):
    db_path = _init_tmp_db(tmp_path, monkeypatch)
    sheet_id = _insert_candidate("acabamento.jpg")

    records = template_audit.audit_db(db_path, limit=None)

    assert [rec.sheet_id for rec in records] == [sheet_id]
    assert records[0].status == "flip_acabamento_setor"
    assert records[0].proposed_template == "acabamento"


def test_reclassify_apply_skips_validated_candidates(tmp_path, monkeypatch):
    db_path = _init_tmp_db(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "validated.jpg").write_bytes(b"not really an image")
    sheet_id = _insert_candidate("validated.jpg")
    db.validate_sheet(sheet_id, "JULIO LIMA")
    rerun_calls = []

    summary = reclassify_templates.run_reclassification(
        db_path=db_path,
        data_dir=data_dir,
        apply=True,
        rerun_func=lambda image, template: rerun_calls.append((image, template)),
        cross_check_func=lambda sheet_id: None,
    )

    assert summary["validated_skipped"] == 1
    assert summary["processed"] == []
    assert rerun_calls == []
    sheet = db.get_sheet(sheet_id)
    assert sheet["status"] == "validated"
    assert sheet["sheet_data"]["template_name"] == "bobine_formato"


def test_reclassify_apply_reruns_non_validated_acabamento_candidate(
    tmp_path,
    monkeypatch,
):
    db_path = _init_tmp_db(tmp_path, monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "candidate.jpg").write_bytes(b"not really an image")
    sheet_id = _insert_candidate("candidate.jpg")
    rerun_calls: list[tuple[Path, str]] = []
    cross_calls: list[int] = []

    def fake_rerun(image: Path, template: str) -> dict:
        rerun_calls.append((image, template))
        return _acabamento_result()

    def fake_cross_check(sid: int) -> None:
        cross_calls.append(sid)

    summary = reclassify_templates.run_reclassification(
        db_path=db_path,
        data_dir=data_dir,
        apply=True,
        rerun_func=fake_rerun,
        cross_check_func=fake_cross_check,
    )

    assert summary["processed"] == [sheet_id]
    assert rerun_calls == [(data_dir / "candidate.jpg", "acabamento")]
    assert cross_calls == [sheet_id]
    sheet = db.get_sheet(sheet_id)
    assert sheet["status"] == "extracted"
    assert sheet["sheet_data"]["template_name"] == "acabamento"
