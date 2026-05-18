"""Cumulative historical refs layer (Round 104).

The ``plan_colunas`` / ``StockSAP`` Excel exports are *snapshots* — OFs and
lotes that close drop off newer exports. This module keeps a growing union
of every ``OF → plan-entries`` and ``lote → stock-record`` ever mined, so
the cross-check can still validate kanbans that reference now-closed OFs.

Stored at ``data/refs_cumulative.json``. The :mod:`ref_watcher` folds each
fresh mine into this layer (fresh wins per key; keys only in the layer are
kept) and rebuilds its indexes from the merged result.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CUMULATIVE_PATH = _REPO_ROOT / "data" / "refs_cumulative.json"
_lock = threading.Lock()
_MAX_UPLOAD_LOG = 50


def _empty() -> dict[str, Any]:
    return {
        "updated_at": None,
        "of_to_entries": {},
        "lotes_sap_full": {},
        "uploads": [],
    }


def load_cumulative() -> dict[str, Any]:
    """Read the cumulative layer; an empty structure if absent or corrupt."""
    if not _CUMULATIVE_PATH.exists():
        return _empty()
    try:
        data = json.loads(_CUMULATIVE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    base = _empty()
    if isinstance(data, dict):
        if isinstance(data.get("of_to_entries"), dict):
            base["of_to_entries"] = data["of_to_entries"]
        if isinstance(data.get("lotes_sap_full"), dict):
            base["lotes_sap_full"] = data["lotes_sap_full"]
        if isinstance(data.get("uploads"), list):
            base["uploads"] = data["uploads"]
        base["updated_at"] = data.get("updated_at")
    return base


def _write(data: dict) -> None:
    """Atomic write — .tmp + os.replace, so readers never see a half file."""
    _CUMULATIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CUMULATIVE_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, _CUMULATIVE_PATH)


def merge_and_persist(
    fresh_of: dict[str, list[dict]],
    fresh_lotes: dict[str, dict],
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """Fold a fresh mine into the cumulative layer.

    Fresh keys overwrite (most recent export wins); keys present only in the
    layer are kept (historical OFs/lotes never lost). An empty ``fresh``
    (missing or corrupt Excel) leaves the layer untouched. Persists only
    when something actually changed. Returns the merged
    ``(of_to_entries, lotes_sap_full)``.
    """
    with _lock:
        cum = load_cumulative()
        merged_of: dict[str, list[dict]] = dict(cum["of_to_entries"])
        merged_lotes: dict[str, dict] = dict(cum["lotes_sap_full"])
        changed = False
        for k, v in (fresh_of or {}).items():
            if merged_of.get(k) != v:
                merged_of[k] = v
                changed = True
        for k, v in (fresh_lotes or {}).items():
            if merged_lotes.get(k) != v:
                merged_lotes[k] = v
                changed = True
        if changed:
            cum["of_to_entries"] = merged_of
            cum["lotes_sap_full"] = merged_lotes
            cum["updated_at"] = datetime.now(
                tz=timezone.utc).isoformat(timespec="seconds")
            _write(cum)
        return merged_of, merged_lotes


def record_upload(kind: str, filename: str, n_before: int, n_after: int) -> None:
    """Append a line to the upload log (most-recent-first, capped)."""
    with _lock:
        cum = load_cumulative()
        uploads = cum.get("uploads") or []
        uploads.insert(0, {
            "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "filename": filename,
            "added": max(0, n_after - n_before),
            "total": n_after,
        })
        cum["uploads"] = uploads[:_MAX_UPLOAD_LOG]
        _write(cum)


def stats() -> dict[str, Any]:
    """Cumulative totals — distinct OFs and lotes ever seen + upload log."""
    cum = load_cumulative()
    return {
        "n_ofs": len(cum["of_to_entries"]),
        "n_lotes": len(cum["lotes_sap_full"]),
        "updated_at": cum.get("updated_at"),
        "uploads": cum.get("uploads") or [],
    }
