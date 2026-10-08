"""Missing official identities stay reviewable; trusted codes remain consistent."""
from __future__ import annotations

import pytest
from app.dq.operador_snap import snap_operador
from app.pipeline.scoring_engine import cross_check_sheet
from app.web import db, main

_COLABS = {95: {"sname": "AUGUSTO MONTEIRO", "pernr": "10000095"},
           537: {"sname": "JULIO LIMA", "pernr": "10000537"}}


@pytest.fixture()
def sheet(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db")
    db.init_db()
    sid = db.insert_sheet("test.jpg")
    data = {"header": {"operador": "JULIO LIMA", "n_operador": "95"},
            "rows": [], "footer": {}}
    db.update_extraction(sid, data, {}, data)
    return sid


def test_real_incident_identity_without_reference_has_actionable_review():
    data = {"template_name": "acabamento", "header": {
        "operador": "Mohammed Abdus SeGan", "n_operador": "4496"}, "rows": []}
    result = cross_check_sheet(data, None, {"colaboradores": _COLABS})
    for field in ("operador", "n_operador"):
        assert result["header"][field]["status"] == "NO_MATCH"
        assert "4496" in result["header"][field]["warning"]
        assert "atualizar" in result["header"][field]["warning"]
    assert all("ListaColaboradores" in item["reason"] for item in result["to_analisar"])


def test_full_pernr_resolves_exactly_and_normalizes_short_code():
    resolved = snap_operador("JULIO LIMA", "10000537", _COLABS)
    assert resolved.snapped_cod == "537" and resolved.pernr == "10000537"
    assert resolved.applied and not resolved.suspended


def test_missing_pernr_does_not_identify_a_person_from_an_empty_number():
    resolved = snap_operador("UNKNOWN", "", {95: {"sname": "AUGUSTO", "pernr": ""}})
    assert not resolved.applied and resolved.snapped_name == "UNKNOWN"


def test_ambiguous_full_pernr_stays_in_review():
    refs = {95: {"sname": "AUGUSTO", "pernr": "10000095"},
            537: {"sname": "JULIO", "pernr": "10000095"}}
    resolved = snap_operador("AUGUSTO", "10000095", refs)
    assert not resolved.applied and resolved.suspended
    assert resolved.snapped_cod == "10000095"


def test_alias_cannot_create_employee_missing_from_official_catalog():
    alias = {"UNKNOWN": {"cod": "4496", "sname": "UNKNOWN", "pernr": "10004496"}}
    resolved = snap_operador("UNKNOWN", "", _COLABS, aliases=alias)
    assert not resolved.applied and resolved.pernr == ""
    result = cross_check_sheet({"header": {"operador": "UNKNOWN"}, "rows": []}, None,
                               {"colaboradores": _COLABS, "operador_aliases": alias})
    assert result["header"]["operador"]["status"] == "NO_MATCH"


def test_known_number_determines_identity_without_swapping_conflicting_number(sheet):
    refs = {"colaboradores": _COLABS}
    before = db.get_sheet(sheet)
    scoring = cross_check_sheet(before["sheet_data"], None, refs)
    assert scoring["header"]["n_operador"]["no_auto_write"] is True
    main._apply_auto_overwrites(sheet, scoring)
    main._apply_operador_snap(sheet, db.get_sheet(sheet), refs)
    after = db.get_sheet(sheet)
    assert after["sheet_data"]["header"] == {
        "operador": "AUGUSTO MONTEIRO", "n_operador": "95", "pernr": "10000095"}
    rescored = cross_check_sheet(after["sheet_data"], None, refs)
    assert all(rescored["header"][field]["status"] == "MATCH"
               for field in ("operador", "n_operador", "pernr"))
    assert after["raw_extraction"] == before["raw_extraction"]


def test_manual_name_and_pernr_are_not_overwritten(sheet):
    db.apply_edit(sheet, "header.operador", "NOME CONFIRMADO À MÃO")
    db.apply_edit(sheet, "header.pernr", "MANUAL")
    main._apply_operador_snap(sheet, db.get_sheet(sheet), {"colaboradores": _COLABS},
                             frozenset({"header.operador", "header.pernr"}))
    header = db.get_sheet(sheet)["sheet_data"]["header"]
    assert header["operador"] == "NOME CONFIRMADO À MÃO" and header["pernr"] == "MANUAL"


def test_review_reason_is_rendered_without_a_proposed_reference(sheet, monkeypatch):
    from app.cross_check import storage
    data = {"header": {"operador": "UNKNOWN", "n_operador": "4496"}, "rows": []}
    cc = cross_check_sheet(data, None, {"colaboradores": _COLABS})
    monkeypatch.setattr(storage, "load_sheet_cross_check", lambda *a, **k: cc)
    maps = main._build_cc_maps(sheet, allow_regen=False)
    assert "4496" in maps[2]["header.operador"]
    assert "ListaColaboradores" in maps[2]["header.n_operador"]
