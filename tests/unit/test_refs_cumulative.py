"""Tests for the cumulative historical refs layer (Round 104).

Covers: merge accumulation, fresh-wins-on-conflict, empty-fresh is a
no-op, the upload log, and the RefWatcher index derivation over a merged
superset.
"""
from __future__ import annotations

import pytest

from app.cross_check import ref_watcher, refs_cumulative


@pytest.fixture
def cum_path(tmp_path, monkeypatch):
    """Point the cumulative layer at an isolated tmp file."""
    p = tmp_path / "refs_cumulative.json"
    monkeypatch.setattr(refs_cumulative, "_CUMULATIVE_PATH", p)
    return p


def _of(of, cliente="MTG", ov="900001"):
    return [{"of": int(of), "cliente": cliente, "ov": ov, "designacao": "M1 - X"}]


def test_merge_accumulates_across_uploads(cum_path):
    m1, _ = refs_cumulative.merge_and_persist({"260001": _of(260001)}, {})
    assert set(m1) == {"260001"}

    m2, _ = refs_cumulative.merge_and_persist({"260002": _of(260002)}, {})
    # second upload keeps the first OF and adds the new one
    assert set(m2) == {"260001", "260002"}
    assert cum_path.exists()


def test_fresh_wins_on_conflict(cum_path):
    refs_cumulative.merge_and_persist({"260001": _of(260001, cliente="OLD")}, {})
    merged, _ = refs_cumulative.merge_and_persist(
        {"260001": _of(260001, cliente="NEW")}, {})
    assert merged["260001"][0]["cliente"] == "NEW"


def test_empty_fresh_is_noop(cum_path):
    refs_cumulative.merge_and_persist({"260001": _of(260001)}, {"26B1": {"qtd": 5}})
    merged_of, merged_lotes = refs_cumulative.merge_and_persist({}, {})
    assert set(merged_of) == {"260001"}
    assert set(merged_lotes) == {"26B1"}


def test_lotes_accumulate(cum_path):
    _, l1 = refs_cumulative.merge_and_persist({}, {"26B1": {"qtd": 1}})
    _, l2 = refs_cumulative.merge_and_persist({}, {"26B2": {"qtd": 2}})
    assert set(l2) == {"26B1", "26B2"}


def test_upload_log(cum_path):
    refs_cumulative.merge_and_persist({"260001": _of(260001)}, {})
    refs_cumulative.record_upload("plan", "plan_colunas_cpis.xlsx", 0, 1)
    st = refs_cumulative.stats()
    assert st["n_ofs"] == 1
    assert st["uploads"][0]["kind"] == "plan"
    assert st["uploads"][0]["added"] == 1


def test_derive_plan_indexes_over_merged_superset(cum_path):
    """RefWatcher's index derivation sees the cumulative superset."""
    merged_of, _ = refs_cumulative.merge_and_persist(
        {
            "260001": _of(260001, cliente="A", ov="900001"),
            "260002": _of(260002, cliente="B", ov="900002"),
            "260003": _of(260003, cliente="A", ov="900003"),
        },
        {},
    )
    idx = ref_watcher._derive_plan_indexes(merged_of)
    assert idx["n_ofs"] == 3
    assert idx["clientes_plan"] == frozenset({"A", "B"})
    assert idx["of_to_ovs"]["260001"] == frozenset({"900001"})
    assert len(idx["plan_by_cliente"]["A"]) == 2  # OFs 260001 + 260003
