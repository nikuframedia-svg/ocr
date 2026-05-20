"""Tests for Round 106 — refs upload log, OF normalisation, phase priority."""
from __future__ import annotations

import pytest

from app.cross_check import ref_watcher, refs_uploads
from app.pipeline.scoring_engine import normalize_of


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(refs_uploads, "_LOG_PATH", tmp_path / "refs_uploads.json")
    return tmp_path / "refs_uploads.json"


# --- upload log -----------------------------------------------------------

def test_upload_log_records_most_recent_first(log_path):
    assert refs_uploads.recent() == []
    refs_uploads.record("plan", "plan_colunas_cpis.xlsx", 16617)
    refs_uploads.record("stocksap", "StockSAP.xlsx", 2770)
    rows = refs_uploads.recent()
    assert len(rows) == 2
    assert rows[0]["kind"] == "stocksap"        # most recent first
    assert rows[1]["kind"] == "plan"
    assert rows[1]["n_rows"] == 16617


# --- OF normalisation -----------------------------------------------------

def test_normalize_of():
    assert normalize_of("52306") == "052306"     # zero-pad short
    assert normalize_of("254877") == "254877"    # already 6
    assert normalize_of(254877) == "254877"      # int input
    assert normalize_of("05000A") == "05000A"    # has letters — untouched
    assert normalize_of("2502343") == "2502343"  # 7 digits — untouched
    assert normalize_of("") == ""
    assert normalize_of(None) == ""


# --- phase helpers --------------------------------------------------------

def test_phase_columns_detected_between_quanttrp_and_esp():
    hdrs = {"cliente": 0, "ov": 1, "of": 2, "designacao": 3, "quanttrp": 4,
            "bf": 5, "c": 6, "q": 7, "s": 8, "r": 9, "a": 10, "exp": 11,
            "esp": 12, "lbase": 13}
    assert ref_watcher._phase_columns(hdrs) == ["bf", "c", "q", "s", "r", "a", "exp"]


def test_phase_columns_empty_when_no_quanttrp():
    assert ref_watcher._phase_columns({"of": 0, "esp": 1}) == []


def test_fase_incompleta():
    # all phases == quanttrp → concluded
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": 20, "exp": 20}) is False
    # one phase below quanttrp → still in production
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": 20, "exp": 10}) is True
    # blank phase counts as 0 → incomplete
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": None}) is True
    # quanttrp 0 / blank → cannot judge → not flagged
    assert ref_watcher._fase_incompleta(0, {"bf": 0}) is False
    assert ref_watcher._fase_incompleta(None, {"bf": 5}) is False
