"""The CPU worker preserves decisions, variants and fresh production inputs."""
import copy
from dataclasses import replace
import datetime as dt

import pytest

from app import templates_registry
from app.pipeline import scoring_engine, scoring_process, of_consumption
from app.web import db


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    yield
    scoring_process.shutdown()
    of_consumption.invalidate_cache()


def without_time(value):
    if isinstance(value, dict):
        return {k: without_time(v) for k, v in value.items()
                if k not in {"checked_at", "duration_ms", "refs_loaded_at"}}
    if isinstance(value, list):
        return [without_time(v) for v in value]
    return value


def sample():
    refs = {"available": True, "loaded_at": "process-test", "plan_sha256": "A",
            "of_to_entries": {
                "260001": [{"designacao": "PART10", "quanttrp": 100}],
                "260002": [{"designacao": "PART10", "quanttrp": 90}],
            }, "maquinas_by_kanban": {"CORTE": {"codmaq": "M002", "colunaexcel": "c"}}}
    data = {"template_name": "acabamento", "header": {"setor_maquina": "CORTE"},
            "rows": [{"of": "", "modelo": "PART10", "qtd": "2"}], "footer": {}}
    return data, refs


def test_process_output_matches_inline_and_does_not_mutate_input(worker):
    data, refs = sample()
    before = copy.deepcopy(data)
    expected = scoring_engine.cross_check_sheet(data, None, refs, collect_trace=True)
    actual = scoring_process.run("legacy", data, None, refs, collect_trace=True)
    assert without_time(actual) == without_time(expected)
    assert data == before


def test_process_consumption_refreshes_immediately_after_validation(worker):
    data, refs = sample()
    initial = scoring_process.run("legacy", data, None, refs)
    assert initial["rows"][0]["winner_of"] == "260002"
    sid = db.insert_sheet("synthetic-production.jpg")
    with db.conn() as conn:
        conn.execute("UPDATE sheets SET status='validated',sheet_data=? WHERE id=?",
                     ('{"header":{"setor_maquina":"CORTE"}}', sid))
        conn.execute("""INSERT INTO production_rows
                        (sheet_id,row_index,sheet_iso_date,sheet_status,of,modelo,qtd)
                        VALUES (?,0,?,'validated','260001','PART10',99)""",
                     (sid, dt.date.today().isoformat()))
    updated = scoring_process.run("legacy", data, None, refs)
    assert updated["rows"][0]["winner_of"] == "260001"


def test_process_variant_is_reset_and_custom_templates_are_transferred(worker):
    data, refs = sample()
    custom = replace(templates_registry.get_template("acabamento"), name="u_test_custom",
                     row_fields=("modelo", "qtd", "custom_note"))
    templates_registry.set_runtime_templates([custom])
    data["template_name"] = custom.name
    data["rows"][0]["custom_note"] = "preserved"
    try:
        baseline = scoring_process.run("legacy", data, None, refs)
        assert baseline["template_name"] == custom.name
        assert "custom_note" in baseline["rows"][0]["fields"]
        token = scoring_engine.set_scoring_variant("next")
        try:
            scoring_process.run("shadow", data, None, refs)
        finally:
            scoring_engine.SCORING_VARIANT.reset(token)
        current = scoring_process.run("legacy", data, None, refs)
        assert without_time(current) == without_time(baseline)
    finally:
        templates_registry.set_runtime_templates([])
