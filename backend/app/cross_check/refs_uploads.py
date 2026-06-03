"""Upload log for the reference workbooks (Round 106).

Just an audit trail — one line per workbook upload through the /refs page.
No data accumulation: the refs themselves come straight from the current
Excel files (the Round 104 cumulative merge was removed in R106).

Stored at ``data/refs_uploads.json`` as ``{"uploads": [...]}``.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOG_PATH = _REPO_ROOT / "data" / "refs_uploads.json"
_lock = threading.Lock()
_MAX_LOG = 50


def recent() -> list[dict]:
    """The upload log, most-recent-first. Empty list if absent/corrupt."""
    if not _LOG_PATH.exists():
        return []
    try:
        data = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    uploads = data.get("uploads") if isinstance(data, dict) else None
    return uploads if isinstance(uploads, list) else []


def record(
    kind: str,
    filename: str,
    n_rows: int,
    *,
    sha256: str = "",
    n_ofs: int | None = None,
    n_ovs: int | None = None,
    size: int | None = None,
) -> None:
    """Append one upload entry for plan/StockSAP/maquinas/colaboradores."""
    with _lock:
        uploads = recent()
        entry = {
            "at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "filename": filename,
            "n_rows": n_rows,
        }
        if sha256:
            entry["sha256"] = sha256
        if n_ofs is not None:
            entry["n_ofs"] = n_ofs
        if n_ovs is not None:
            entry["n_ovs"] = n_ovs
        if size is not None:
            entry["size"] = size
        uploads.insert(0, entry)
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _LOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"uploads": uploads[:_MAX_LOG]}, ensure_ascii=False),
            encoding="utf-8")
        os.replace(tmp, _LOG_PATH)
