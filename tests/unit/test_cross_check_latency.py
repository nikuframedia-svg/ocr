"""Regressions for the production incident: queued work must not block views."""
from contextvars import ContextVar
import random
import threading

import pytest
from fastapi.testclient import TestClient

from app.pipeline import scoring_engine
from app.web import cross_check_queue, db, main


@pytest.fixture
def queue():
    worker = cross_check_queue.ScoringQueue()
    yield worker
    worker.close()
    assert worker.wait_idle(10)


def test_many_edits_coalesce_to_latest_revision(queue):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def first():
        seen.append(1)
        entered.set()
        assert release.wait(5)

    queue.submit(("cross", 1), 1, first)
    assert entered.wait(2)
    try:
        for revision in range(2, 101):
            queue.submit(("cross", 1), revision, lambda r=revision: seen.append(r))
        assert not queue.submit(("cross", 1), 100, lambda: seen.append("duplicate"))
        assert not queue.submit(("cross", 1), 99, lambda: seen.append("older"))
        assert not queue.submit(("cross", 1), 1, lambda: seen.append("active duplicate"))
        assert queue.snapshot()["pending"] == 1
    finally:
        release.set()
    assert queue.wait_idle()
    assert seen == [1, 100]


def test_foreground_precedes_shadow_and_context_does_not_leak(queue):
    entered, release = threading.Event(), threading.Event()
    variant = ContextVar("test_variant", default="production")
    seen = []

    def hold():
        variant.set("shadow")
        entered.set()
        assert release.wait(5)

    queue.submit("hold", 1, hold)
    assert entered.wait(2)
    try:
        queue.submit("shadow", 1, lambda: seen.append(("shadow", variant.get())), shadow=True)
        queue.submit("foreground", 1, lambda: seen.append(("foreground", variant.get())))
    finally:
        release.set()
    assert queue.wait_idle()
    assert seen == [("foreground", "production"), ("shadow", "production")]


def test_worker_survives_failed_job(queue):
    def fail():
        raise ValueError("synthetic scoring failure")
    done = threading.Event()
    queue.submit("bad", 1, fail)
    queue.submit("good", 1, done.set)
    assert done.wait(2)


def test_view_reference_snapshot_never_waits_for_excel_reload(tmp_path, monkeypatch):
    from app.cross_check.ref_watcher import RefWatcher

    watcher = RefWatcher(doc_dir=tmp_path, repo_root=tmp_path)
    old = {"available": True, "colaboradores": {1: {"sname": "OLD"}}}
    updated = {"available": True, "colaboradores": {1: {"sname": "NEW"}}}
    watcher._refs = old
    entered, release = threading.Event(), threading.Event()
    calls = []

    def reload():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        watcher._refs = updated
        return updated

    monkeypatch.setattr(watcher, "get_refs", reload)
    try:
        assert watcher.peek_refs() is old
        assert entered.wait(2)
        for _ in range(10):
            assert watcher.peek_refs() is old
        assert calls == [1]
    finally:
        release.set()
    watcher._refresh_thread.join(2)
    assert watcher.peek_refs() is updated


def test_open_and_poll_never_wait_for_scoring(tmp_path, monkeypatch, queue):
    from app.cross_check import storage

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    sid = db.insert_sheet("test.jpg")
    data = {"template_name": "acabamento", "header": {}, "rows": [], "footer": {}}
    db.update_extraction(sid, data, {}, data)
    monkeypatch.setattr(cross_check_queue, "queue", queue)
    monkeypatch.setattr(storage, "load_sheet_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(main, "_get_operadores", lambda: ())
    entered, release = threading.Event(), threading.Event()
    runs = []

    def slow(*args, **kwargs):
        runs.append(args)
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(main, "_run_and_store_cross_check", slow)
    client = TestClient(main.app)
    try:
        response = client.get(f"/sheet/{sid}")
        assert response.status_code == 200
        assert "cross-check-progress" in response.text
        assert "Server-Timing" in response.headers
        assert entered.wait(2)
        for _ in range(3):
            assert client.get(f"/sheet/{sid}").status_code == 200
        poll = client.get(f"/sheet/{sid}/cross-check-status")
        assert "A verificar" in poll.text
        assert "HX-Refresh" not in poll.headers
        assert len(runs) == 1
        assert queue.snapshot()["pending"] == 0
    finally:
        release.set()
    assert queue.wait_idle()


def test_stale_revision_colors_hidden_and_latest_revision_queued(tmp_path, monkeypatch):
    from app.cross_check import storage

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    sid = db.insert_sheet("test.jpg")
    data = {"header": {}, "rows": [{"of": "123456"}], "footer": {}}
    db.update_extraction(sid, data, {}, data)
    revision = db.get_sheet(sid)["revision"]
    cc = {"engine_version": scoring_engine.ENGINE_VERSION, "sheet_revision": revision - 1,
          "rows": [{"row_index": 0, "fields": {"of": {"status": "MATCH"}}}]}
    monkeypatch.setattr(storage, "load_sheet_cross_check", lambda *a, **k: cc)
    scheduled = []
    monkeypatch.setattr(main, "_start_sheet_cross_check", lambda *a, **k: scheduled.append(a))
    assert main._build_cc_maps(sid)[0] == {}
    assert scheduled == [({sid},)]


def test_completion_offers_refresh_without_discarding_unsaved_input(tmp_path, monkeypatch):
    from app.cross_check import storage

    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    sid = db.insert_sheet("test.jpg")
    monkeypatch.setattr(storage, "load_sheet_cross_check", lambda *a, **k: {"checked_at": "new"})
    response = TestClient(main.app).get(f"/sheet/{sid}/cross-check-status?seen=old")
    assert response.status_code == 200
    assert "Atualizar resultado" in response.text
    assert "HX-Refresh" not in response.headers


def test_restart_resumes_only_incomplete_extracted_sheets(monkeypatch, queue):
    from app.cross_check import storage

    sheets = [{"id": 1, "status": "extracted", "revision": 1},
              {"id": 2, "status": "extracted", "revision": 1},
              {"id": 3, "status": "pending"}, {"id": 4, "status": "validated"},
              {"id": 5, "status": "extracted", "needs_review": True, "revision": 1}]
    monkeypatch.setattr(db, "list_sheets", lambda **k: sheets)
    monkeypatch.setattr(db, "get_sheet", lambda sid: next(s for s in sheets if s["id"] == sid))
    monkeypatch.setattr(storage, "load_sheet_cross_check", lambda sid, **k: {} if sid == 2 else None)
    monkeypatch.setattr(cross_check_queue, "queue", queue)
    scored, deposited = [], []

    def score(sid, **kw):
        scored.append(sid)
        return {}

    monkeypatch.setattr(main, "_run_and_store_cross_check", score)
    monkeypatch.setattr(main, "_deposit_csv_to_factory", deposited.append)
    main._recover_incomplete_cross_checks()
    assert queue.wait_idle()
    assert scored == [1, 5]
    assert deposited == [1]


def test_native_levenshtein_preserves_original_metric_and_cutoff():
    def original(a, b):
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        if abs(len(a) - len(b)) > 5:
            return 999
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            current = [i]
            for j, cb in enumerate(b, 1):
                current.append(min(current[-1] + 1, previous[j] + 1,
                                   previous[j - 1] + (ca != cb)))
            previous = current
        return previous[-1]

    rng = random.Random(7955)
    alphabet = "0123456789ABHMOçá- /"
    for _ in range(1000):
        a, b = ("".join(rng.choices(alphabet, k=rng.randrange(25))) for _ in range(2))
        assert scoring_engine._lev_distance(a, b) == original(a, b)


def test_ranking_reuses_consumption_and_only_explains_trace_top_candidates(monkeypatch):
    from app.pipeline import of_consumption

    entries = {(str(260000 + i), "2400001", "MODEL"): {
        "ov": "2400001", "cliente": "CLIENT", "designacao": "MODEL",
        "quanttrp": 100, "fases": {"c": 10},
    } for i in range(100)}
    refs = {"available": True, "of_to_entries": {k[0]: [e] for k, e in entries.items()}}
    consumption_calls, explanation_calls = [], []
    score = scoring_engine._entry_global_score

    def get_consumption(phase=None, *, refs=None):
        consumption_calls.append(phase)
        return {}

    def observed_score(*args, **kwargs):
        if kwargs.get("collect_reasons"):
            explanation_calls.append(1)
        return score(*args, **kwargs)

    monkeypatch.setattr(of_consumption, "get_consumption", get_consumption)
    monkeypatch.setattr(scoring_engine, "_entry_global_score", observed_score)
    trace = {}
    winner = scoring_engine._best_scored_entry(
        entries, {"of": "260001", "modelo": "MODEL"}, refs, "c", trace=trace)
    assert winner is not None
    assert consumption_calls == ["c"]
    assert trace["pool_size"] == 100
    assert len(explanation_calls) == min(100, scoring_engine._TRACE_TOP_K) + 1
    assert all(c["field_sims"] for c in trace["candidates"])


def test_incremental_rows_match_full_calculation_and_invalidate_context(monkeypatch):
    from app.pipeline import of_consumption

    consumption = {}
    monkeypatch.setattr(of_consumption, "get_consumption", lambda *a, **k: dict(consumption))
    monkeypatch.setattr(of_consumption, "recent_active_ofs", lambda *a, **k: frozenset())
    monkeypatch.setattr(scoring_engine, "_load_cross_params", lambda: {})
    refs = {"available": True, "loaded_at": "snapshot-1", "of_to_entries": {
        "260001": [{"ov": "2400001", "cliente": "CLIENT", "designacao": "MODEL-A", "quanttrp": 100}],
        "260002": [{"ov": "2400002", "cliente": "CLIENT", "designacao": "MODEL-B", "quanttrp": 100}],
    }}
    data = {"template_name": "acabamento", "header": {}, "footer": {}, "rows": [
        {"of": "260001", "modelo": "MODEL-A", "qtd": "4"},
        {"of": "260002", "modelo": "MODEL-B", "qtd": "8"},
    ]}
    calls = []
    original = scoring_engine._score_row

    def observed(*a, **k):
        calls.append(a[0])
        return original(*a, **k)

    monkeypatch.setattr(scoring_engine, "_score_row", observed)

    def run():
        result = scoring_engine.cross_check_sheet(data, None, refs, collect_trace=True)
        return {k: v for k, v in result.items() if k not in ("duration_ms", "checked_at")}

    first = run()
    assert calls == [0, 1]
    calls.clear()
    assert run() == first
    assert calls == []
    # A consumer mutating returned cells must not poison the saved row.
    first["rows"][0]["fields"]["qtd"]["value"] = "POISON"
    assert run()["rows"][0]["fields"]["qtd"]["value"] != "POISON"

    data["rows"][0]["qtd"] = "17"
    incremental = run()
    assert calls == [0]
    scoring_engine._get_indices(refs)["row_result_cache"].clear()
    assert run() == incremental

    calls.clear()
    consumption[("260001", "MODEL-A")] = 99
    changed_consumption = run()
    assert calls == [0, 1]
    scoring_engine._get_indices(refs)["row_result_cache"].clear()
    assert run() == changed_consumption

    calls.clear()
    token = scoring_engine.set_scoring_variant("next")
    try:
        run()
        assert calls == [0, 1]
    finally:
        scoring_engine.SCORING_VARIANT.reset(token)

    calls.clear()
    refs["loaded_at"] = "snapshot-2"
    run()
    assert calls == [0, 1]
