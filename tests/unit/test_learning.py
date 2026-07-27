"""Tests for the learning engine (backend/app/learning/) + overlay wiring.

Covers: schema migration, mining, risk classification, the proposal
store, overlay materialisation, the success metric, attractors, the
snap overlay closed loop, and the few-shot prompt block.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def learn_db(tmp_path, monkeypatch):
    """Isolated SQLite DB + overlay path for one test."""
    from app.learning import materialize
    from app.web import db

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(materialize, "OVERLAY_PATH", tmp_path / "overlay.json")
    db.init_db()
    return db


def _add_sheet(db, *, status="validated", operador="OP A",
               sheet_data=None, validated_at="2026-05-01 10:00:00"):
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO sheets (image_path, status, operador, sheet_data, "
            "validated_at) VALUES (?,?,?,?,?)",
            ("img.jpg", status, operador,
             json.dumps(sheet_data or {}), validated_at),
        )
        return cur.lastrowid


def _add_edit(db, sheet_id, field_path, old, new, source="human"):
    with db.conn() as c:
        c.execute(
            "INSERT INTO edits (sheet_id, field_path, old_value, new_value, "
            "source) VALUES (?,?,?,?,?)",
            (sheet_id, field_path, old, new, source),
        )


# ---- schema ---------------------------------------------------------------

def test_schema_creates_learning_tables(learn_db):
    with learn_db.conn() as c:
        tables = {
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        edit_cols = {
            r["name"] for r in c.execute("PRAGMA table_info(edits)").fetchall()
        }
    assert {"learnings", "learning_runs"} <= tables
    assert "source" in edit_cols


def test_init_db_is_idempotent(learn_db):
    learn_db.init_db()  # second call must not raise
    learn_db.init_db()


# ---- mining ---------------------------------------------------------------

def test_derive_aliases_groups_repeated_corrections(learn_db):
    from app.learning import mining

    sid = _add_sheet(learn_db)
    for _ in range(3):
        _add_edit(learn_db, sid, "rows[0].cliente",
                  "SKYR BELUX", "STOCK MTG BELUX")
    proposals = mining.derive_aliases(mining.mine_edits())
    aliases = [p for p in proposals if p.kind == "cliente_alias"]
    assert len(aliases) == 1
    assert aliases[0].evidence_count == 3
    assert aliases[0].payload == {"from": "SKYR BELUX", "to": "STOCK MTG BELUX"}


def test_derive_aliases_ignores_single_occurrence(learn_db):
    from app.learning import mining

    sid = _add_sheet(learn_db)
    _add_edit(learn_db, sid, "rows[0].cliente", "ACME", "ACME CORP")
    assert mining.derive_aliases(mining.mine_edits()) == []


def test_mine_edits_excludes_system_source(learn_db):
    from app.learning import mining

    sid = _add_sheet(learn_db)
    _add_edit(learn_db, sid, "rows[0].cliente", "X", "Y", source="system")
    assert mining.mine_edits() == []


def test_case_only_edits_generate_case_rule(learn_db):
    from app.learning import mining

    sid = _add_sheet(
        learn_db,
        sheet_data={"template_name": "bobine_formato", "rows": []},
    )
    for _ in range(2):
        _add_edit(learn_db, sid, "rows[0].modelo", "CFC5F45RIV", "CFC5F45Riv")
    edits = mining.mine_edits()
    assert len(edits) == 2
    rules = mining.derive_case_rules(edits)
    assert len(rules) == 1
    assert rules[0].payload["to"] == "CFC5F45Riv"
    assert rules[0].template_name == "bobine_formato"


def test_conflicting_case_rules_are_quarantined(learn_db):
    from app.learning import mining, risk

    sid = _add_sheet(
        learn_db,
        sheet_data={"template_name": "bobine_formato", "rows": []},
    )
    for canonical in ("CFC5F45Riv", "Cfc5f45riv"):
        for _ in range(2):
            _add_edit(
                learn_db,
                sid,
                "rows[0].modelo",
                "CFC5F45RIV",
                canonical,
            )

    rules = mining.derive_case_rules(mining.mine_edits())

    assert len(rules) == 2
    assert all(rule.payload["conflict"] is True for rule in rules)
    assert all(risk.classify_risk(rule) == "behavior" for rule in rules)


def test_case_rule_closed_loop_applies_to_next_sheet(learn_db, monkeypatch):
    from app.dq import snap
    from app.learning import materialize, mining, risk, store
    from app.pipeline.scoring_engine import (
        _apply_winner_to_field,
        _score_header_footer,
    )

    sid = _add_sheet(
        learn_db,
        sheet_data={"template_name": "bobine_formato", "rows": []},
    )
    corrections = (
        ("rows[0].modelo", "CFC5F45RIV", "CFC5F45Riv"),
        ("rows[0].cliente", "CLIENTE RIV", "Cliente Riv"),
        ("header.operador", "ANA SILVA", "Ana Silva"),
    )
    for field_path, old, new in corrections:
        for _ in range(2):
            _add_edit(learn_db, sid, field_path, old, new)
    rules = mining.derive_case_rules(mining.mine_edits())
    assert {rule.field for rule in rules} == {"cliente", "modelo", "operador"}
    for rule in rules:
        stored = store.upsert_proposal(rule, risk.classify_risk(rule))
        assert stored["status"] == "approved"
    overlay = materialize.build_overlay()
    original_loader = snap._load_overlay
    monkeypatch.setattr(snap, "_load_overlay", lambda _repo: overlay)
    try:
        snap.reload_lexicons()
        modelo = _apply_winner_to_field(
            "modelo", "CFC5F45RIV", None, [], {}, {},
            has_field_reference=True,
            template_name="bobine_formato",
        )
        cliente = _apply_winner_to_field(
            "cliente", "CLIENTE RIV", None, [], {}, {},
            has_field_reference=True,
            template_name="bobine_formato",
        )
        header, _footer = _score_header_footer(
            {"operador": "ANA SILVA"},
            {},
            {},
            header_fields=("operador",),
            template_name="bobine_formato",
        )
        assert modelo["status"] == "snapped"
        assert modelo["value"] == "CFC5F45Riv"
        assert modelo["decision_reason"] == "learned_canonical_case"
        assert cliente["value"] == "Cliente Riv"
        assert cliente["decision_reason"] == "learned_canonical_case"
        assert header["operador"]["value"] == "Ana Silva"
        assert header["operador"]["decision_reason"] == "learned_canonical_case"
    finally:
        monkeypatch.setattr(snap, "_load_overlay", original_loader)
        snap.reload_lexicons()


def test_derive_confusions_finds_char_substitution(learn_db):
    from app.learning import mining

    sid = _add_sheet(learn_db)
    for _ in range(3):
        _add_edit(learn_db, sid, "rows[0].modelo", "CGC2E1OD", "CGC2E10D")
    confusions = mining.derive_confusions(mining.mine_edits())
    pairs = {(c.payload["gold_char"], c.payload["ocr_char"]) for c in confusions}
    assert ("0", "O") in pairs


def test_derive_observed_values(learn_db):
    from app.learning import mining

    sd = {"rows": [{"cliente": "ZZNEWCLIENTE", "modelo": "M1"}]}
    _add_sheet(learn_db, sheet_data=sd)
    _add_sheet(learn_db, sheet_data=sd)
    obs = mining.derive_observed_values(mining.load_validated_sheets())
    values = {p.payload["value"] for p in obs}
    assert "ZZNEWCLIENTE" in values


# ---- risk -----------------------------------------------------------------

def test_classify_risk():
    from app.learning.models import Proposal
    from app.learning.risk import classify_risk

    confusion = Proposal(kind="confusion_pair", payload={}, dedup_key="c")
    fewshot = Proposal(kind="fewshot_example", payload={}, dedup_key="f")
    observed = Proposal(kind="observed_value", payload={}, dedup_key="o")
    unknown_alias = Proposal(kind="cliente_alias",
                             payload={"from": "A", "to": "ZZ_NOT_REAL"},
                             dedup_key="a")

    assert classify_risk(confusion) == "behavior"
    assert classify_risk(fewshot) == "low"
    assert classify_risk(observed) == "low"
    assert classify_risk(unknown_alias) == "behavior"


# ---- store ----------------------------------------------------------------

def test_upsert_proposal_status_by_risk(learn_db):
    from app.learning import store
    from app.learning.models import Proposal

    p = Proposal(kind="fewshot_example", payload={"sheet_id": 1},
                 dedup_key="fewshot|t|1")
    res = store.upsert_proposal(p, "low")
    assert res["created"] is True
    assert res["status"] == "approved"

    p2 = Proposal(kind="confusion_pair", payload={"gold_char": "0",
                  "ocr_char": "O"}, dedup_key="confusion|0>O")
    res2 = store.upsert_proposal(p2, "behavior")
    assert res2["status"] == "quarantine"


def test_upsert_proposal_is_idempotent(learn_db):
    from app.learning import store
    from app.learning.models import Proposal

    p = Proposal(kind="confusion_pair", payload={}, dedup_key="dup",
                 evidence_count=2)
    first = store.upsert_proposal(p, "behavior")
    p.evidence_count = 9
    second = store.upsert_proposal(p, "behavior")
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert store.get_proposal(first["id"])["evidence_count"] == 9


def test_approve_reject_proposal(learn_db):
    from app.learning import store
    from app.learning.models import Proposal

    res = store.upsert_proposal(
        Proposal(kind="confusion_pair", payload={}, dedup_key="k"), "behavior"
    )
    store.approve_proposal(res["id"], "tester")
    assert store.get_proposal(res["id"])["status"] == "approved"
    store.reject_proposal(res["id"], "tester", note="ruído")
    rec = store.get_proposal(res["id"])
    assert rec["status"] == "rejected"
    assert rec["note"] == "ruído"


# ---- materialize ----------------------------------------------------------

def test_materialize_overlay_only_includes_approved(learn_db):
    from app.learning import materialize, store
    from app.learning.models import Proposal

    approved = store.upsert_proposal(
        Proposal(kind="cliente_alias", field="cliente",
                 payload={"from": "AAA", "to": "BBB"}, dedup_key="a1"),
        "low",
    )
    store.approve_proposal(approved["id"], "tester")
    store.upsert_proposal(
        Proposal(kind="cliente_alias", field="cliente",
                 payload={"from": "CCC", "to": "DDD"}, dedup_key="a2"),
        "behavior",
    )  # stays quarantine

    overlay = materialize.materialize_overlay()
    assert overlay["cliente_aliases"] == {"AAA": "BBB"}
    assert materialize.OVERLAY_PATH.exists()


# ---- metrics --------------------------------------------------------------

def test_corrections_per_sheet_counts_human_only(learn_db):
    from app.learning import metrics

    s1 = _add_sheet(learn_db)
    s2 = _add_sheet(learn_db)
    _add_edit(learn_db, s1, "rows[0].of", "1", "2")
    _add_edit(learn_db, s1, "rows[0].ov", "3", "4")
    _add_edit(learn_db, s2, "rows[0].of", "5", "6", source="system")
    # 2 human edits across 2 validated sheets => 1.0 per sheet
    assert metrics.corrections_per_sheet() == 1.0


# ---- attractors -----------------------------------------------------------

def test_compute_attractors_ranks_by_volume(learn_db):
    from app.web import attractors

    sid = _add_sheet(learn_db)
    for _ in range(5):
        _add_edit(learn_db, sid, "rows[0].modelo", "A", "B")
    _add_edit(learn_db, sid, "rows[0].cliente", "C", "D")

    found = attractors.compute_attractors()
    field_rows = [a for a in found if a["scope"] == "campo"]
    assert field_rows[0]["label"] == "rows[].modelo"
    assert field_rows[0]["correction_count"] == 5


# ---- snap overlay (closed loop) -------------------------------------------

def test_confusion_overlay_closed_loop():
    from app.dq import snap

    original = snap._load_overlay
    try:
        snap._load_overlay = lambda repo: {
            "confusion_overlay": {"Z": {"7": 0.6}}
        }
        snap.reload_lexicons()
        assert snap._CONFUSION.get("Z", {}).get("7", 0.0) >= 0.6
    finally:
        snap._load_overlay = original
        snap.reload_lexicons()


def test_empty_overlay_is_noop():
    from app.dq import snap

    base_clientes = snap._BASE_CLIENTES
    snap.reload_lexicons()  # real overlay file is absent => no-op
    assert snap._CLIENTES == base_clientes


# ---- prompt few-shot ------------------------------------------------------

def test_fewshot_block_empty_when_no_overlay(monkeypatch, tmp_path):
    from app.pipeline import prompt_builder

    monkeypatch.setattr(prompt_builder, "_OVERLAY_PATH", tmp_path / "missing.json")
    assert prompt_builder._fewshot_block("bobine_formato") == ""


def test_build_prompt_still_works():
    from app.pipeline.prompt_builder import build_prompt
    from app.templates_registry import DEFAULT_TEMPLATE

    prompt = build_prompt(DEFAULT_TEMPLATE)
    assert "Return this exact JSON structure:" in prompt
