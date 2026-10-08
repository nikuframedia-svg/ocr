"""Bound historical JSON reads without weakening the storage consistency checks."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.cross_check import storage


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(storage, "_current_engine_version", lambda: "test-engine")
    storage._aggregate_payload.cache_clear()
    yield tmp_path
    storage._aggregate_payload.cache_clear()


def _store(sheet_id, *, count=1, date="2026-10-08", operator="TEST"):
    return storage.store_cross_check(
        sheet_id=sheet_id,
        image_path=f"images/{sheet_id}.jpg",
        operador=operator,
        date_iso=date,
        sheet_status="extracted",
        cross_check_result={
            "summary": {"match": count, "no_match": 1, "na": 0, "total": count + 1},
            "rows": [{"diagnostic": "x" * 20000}],
            "to_analisar": [{"field": "modelo", "row_index": 0, "value": "OCR", "ref": "REF"}],
        },
    )


def _count_payload_reads(monkeypatch, root):
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.is_relative_to(root) and path.parent != root:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return reads


def test_new_sheets_do_not_reread_unchanged_history(archive, monkeypatch):
    for sid in range(1, 31):
        _store(sid)
    reads = _count_payload_reads(monkeypatch, archive)
    _store(31)
    _store(32)
    assert len(reads) == 2
    summary = storage.load_summary()
    inbox = storage.load_to_analisar(limit=3)
    assert len(reads) == 2
    assert summary["n_sheets"] == 32
    assert summary["totals"] == {"match": 32, "no_match": 32, "na": 0, "total": 64}
    assert inbox["total"] == 32
    assert len(inbox["items"]) == 3


def test_cold_cache_reads_each_payload_once_for_both_exports(archive, monkeypatch):
    for sid in range(1, 11):
        _store(sid)
    storage._aggregate_payload.cache_clear()
    reads = _count_payload_reads(monkeypatch, archive)
    _store(11)
    assert len(reads) == 11
    assert len(set(reads)) == 11
    assert "rows" not in next(iter(storage._aggregate_payloads(storage._read_index()).values()))


def test_edit_move_and_delete_update_all_aggregates(archive):
    first = _store(1, count=2)
    _store(2, count=3)
    moved = _store(1, count=7, date="2026-10-09", operator="OTHER")
    assert not Path(first["file"]).exists()
    assert Path(moved["file"]).exists()
    summary = storage.load_summary()
    assert summary["totals"]["match"] == 10
    assert set(summary["by_day"]) == {"2026-10-08", "2026-10-09"}
    assert set(summary["by_operador"]) == {"TEST", "OTHER"}
    assert len(storage.load_to_analisar()["items"]) == 2
    storage.remove_sheet_cross_check(1)
    summary = storage.load_summary()
    assert summary["totals"]["match"] == 3
    assert "2026-10-09" not in summary["by_day"]
    assert "OTHER" not in summary["by_operador"]
    assert [item["sheet_id"] for item in storage.load_to_analisar()["items"]] == [2]


@pytest.mark.parametrize("change", ["corrupt", "missing", "wrong_engine", "wrong_sheet"])
def test_external_changes_invalidate_warm_cache(archive, change):
    path = Path(_store(1)["file"])
    original = path.read_text(encoding="utf-8")
    if change == "corrupt":
        path.write_text("{broken", encoding="utf-8")
    elif change == "missing":
        path.unlink()
    else:
        data = json.loads(original)
        data["engine_version" if change == "wrong_engine" else "sheet_id"] = "different"
        path.write_text(json.dumps(data), encoding="utf-8")
    summary = storage.load_summary()
    inbox = storage.load_to_analisar()
    assert summary["n_sheets"] == 0
    assert summary["stale_sheets"] == inbox["stale_sheets"] == 1
    assert inbox["items"] == []
    path.write_text(original, encoding="utf-8")
    assert storage.load_summary()["n_sheets"] == 1
    assert storage.load_to_analisar()["total"] == 1


def test_same_size_and_mtime_atomic_replacement_is_detected(archive):
    path = Path(_store(1)["file"])
    stat = path.stat()
    changed = path.read_text(encoding="utf-8").replace('"match": 1', '"match": 9')
    replacement = path.with_suffix(".replacement")
    replacement.write_text(changed, encoding="utf-8")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(path)
    assert storage.load_summary()["totals"]["match"] == 9


def test_transient_read_failure_is_not_cached(archive, monkeypatch):
    path = Path(_store(1)["file"])
    storage._aggregate_payload.cache_clear()
    original = Path.read_text

    def fail(target, *args, **kwargs):
        if target == path:
            raise PermissionError("transient sharing violation")
        return original(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail)
        assert storage.load_summary()["n_sheets"] == 0
    assert storage.load_summary()["n_sheets"] == 1


def test_engine_change_does_not_reuse_old_engine_counts(archive, monkeypatch):
    _store(1)
    monkeypatch.setattr(storage, "_current_engine_version", lambda: "next-engine")
    assert storage.load_summary()["n_sheets"] == 0
    assert storage.load_to_analisar()["items"] == []
    _store(1, count=4)
    assert storage.load_summary()["totals"]["match"] == 4


def test_concurrent_stores_do_not_lose_sheets_or_counts(archive):
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(_store, range(1, 21)))
    summary = storage.load_summary()
    assert summary["n_sheets"] == 20
    assert summary["totals"]["match"] == 20
    assert storage.load_to_analisar()["total"] == 20
    disk_summary = json.loads((archive / "_summary.json").read_text(encoding="utf-8"))
    assert disk_summary["totals"] == summary["totals"]


def test_returned_inbox_cannot_mutate_cached_values(archive):
    path = Path(_store(1)["file"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["to_analisar"][0]["ref"] = ["REFERENCE"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = storage.load_to_analisar()
    first["items"][0]["ref_value"].append("CHANGED BY CALLER")
    assert storage.load_to_analisar()["items"][0]["ref_value"] == ["REFERENCE"]
