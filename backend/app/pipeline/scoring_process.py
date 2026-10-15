"""A persistent CPU process keeps scoring off the HTTP interpreter's GIL."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing
import os
from pathlib import Path
import threading

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_sent_refs = None
_worker_refs = None
_worker_overlay_mtime = None


def enabled() -> bool:
    return os.environ.get("OCR_SCORING_PROCESS", "0") == "1"


def _compute(mode, data, audit, incoming_refs, variant, db_path, templates, collect_trace):
    global _worker_refs, _worker_overlay_mtime
    from app.pipeline import scoring_engine as engine, of_consumption
    from app.cross_check.ref_watcher import get_watcher
    from app.templates_registry import set_runtime_templates
    from app.web import db
    from app.dq import snap

    if incoming_refs is not None:
        _worker_refs = incoming_refs
    if _worker_refs is None:
        raise RuntimeError("scoring process has no reference snapshot")
    db._DB_PATH = Path(db_path)
    watcher = get_watcher()
    watcher._refs = _worker_refs
    # Inputs are the exact snapshot selected by the parent, not a second
    # independent Excel import partway through a scoring pass.
    watcher._last_check_ts = float("inf")
    # Parent invalidations cannot cross a process boundary. Refresh quantities
    # on EVERY job; row-cache keys then change whenever consumption changes.
    of_consumption.invalidate_cache()
    set_runtime_templates(templates)
    overlay = Path(engine.__file__).resolve().parents[3] / "lexicons/learned_overlay.json"
    overlay_mtime = overlay.stat().st_mtime_ns if overlay.exists() else None
    if overlay_mtime != _worker_overlay_mtime:
        snap.reload_lexicons()
        _worker_overlay_mtime = overlay_mtime
    token = engine.set_scoring_variant(variant)
    try:
        if mode == "warm":
            engine._get_indices(_worker_refs)
            return {"pid": os.getpid()}
        if mode == "shadow":
            return engine.shadow_score(data, audit, _worker_refs)
        return engine.cross_check_sheet(data, audit, _worker_refs, collect_trace=collect_trace)
    finally:
        engine.SCORING_VARIANT.reset(token)


def run(mode, data, audit, refs, *, collect_trace=False):
    global _pool, _sent_refs
    from app.pipeline.scoring_engine import scoring_variant
    from app.templates_registry import TEMPLATES
    from app.web.db import db_path

    # One process and one pending revision per sheet avoid another work storm.
    # Reuse its reference indices and row caches across uploads and edits.
    with _lock:
        for attempt in range(2):
            if _pool is None:
                _pool = ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn"))
                _sent_refs = None
            ref_key = (id(refs), refs.get("loaded_at"))
            payload = refs if ref_key != _sent_refs else None
            try:
                result = _pool.submit(
                    _compute, mode, data, audit, payload, scoring_variant(),
                    str(db_path()), tuple(TEMPLATES.values()), collect_trace,
                ).result()
                _sent_refs = ref_key
                return result
            except BrokenProcessPool:
                _pool.shutdown(wait=False, cancel_futures=True)
                _pool, _sent_refs = None, None
                if attempt:
                    raise


def shutdown() -> None:
    global _pool, _sent_refs
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
        _pool, _sent_refs = None, None


def warmup() -> None:
    if enabled():
        from app.cross_check.ref_watcher import get_watcher
        refs = get_watcher().get_refs()
        if refs.get("available"):
            run("warm", {}, None, refs)
