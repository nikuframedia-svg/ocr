"""Hourly copy of ``data/app.db`` into a Drive-synced folder.

R267 — the factory PC runs Google Drive for Desktop; ``KANBAN_DB_BACKUP_DIR``
points at the local mount of the shared folder ("MTG | Kanban Digital"). The
snapshot itself is :func:`app.web.db.backup_to` (sqlite3 backup API — safe
under WAL); this module only owns scheduling, change detection and status.

Feature is OFF unless ``KANBAN_DB_BACKUP_DIR`` is set (dev/CI stay inert).
Follows the ``ref_importer`` idiom: daemon thread + sleep loop + module
``_state`` under a lock, surfaced via :func:`status` in /admin/refs-status.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from app.config import _dotenv_value
from app.web import db

_REPO_ROOT = Path(__file__).resolve().parents[3]

BACKUP_DIR_ENV = "KANBAN_DB_BACKUP_DIR"
BACKUP_INTERVAL_ENV = "KANBAN_DB_BACKUP_INTERVAL_SEC"
DEFAULT_BACKUP_INTERVAL_SECONDS = 3600
BACKUP_FILENAME = "app.db"
_STALE_TMP_MAX_AGE_SECONDS = 24 * 3600

_state_lock = threading.Lock()
_thread_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "dest_dir": "",
    "interval_seconds": None,
    "last_run_at": None,
    "last_ok": None,
    "last_error": None,
    "last_backup_at": None,
    "last_skipped": None,
    "last_size": None,
    "backups_done": 0,
}

# Change detection: PRAGMA data_version only moves when ANOTHER connection
# writes, so it must be read from one persistent connection owned here (the
# app writes through per-operation connections in db.conn()). File mtime is
# NOT a substitute — under WAL, commits land in the -wal sidecar.
_dv_lock = threading.Lock()
_dv_conn: sqlite3.Connection | None = None
_dv_last_backed_up: int | None = None


def _config_value(name: str) -> str | None:
    return os.environ.get(name) or _dotenv_value(_REPO_ROOT, name)


def _is_absolute_backup_path(raw: str | Path) -> bool:
    return Path(raw).is_absolute() or PureWindowsPath(str(raw)).is_absolute()


def _resolve_dir(raw: str | Path) -> Path:
    path = Path(raw)
    if _is_absolute_backup_path(raw):
        return path
    return _REPO_ROOT / path


def configured_backup_dir() -> Path | None:
    """Destination folder, or ``None`` when the feature is off (env unset)."""
    val = _config_value(BACKUP_DIR_ENV)
    if not val or not val.strip():
        return None
    return _resolve_dir(val.strip())


def configured_interval_seconds() -> int:
    raw = _config_value(BACKUP_INTERVAL_ENV)
    if not raw:
        return DEFAULT_BACKUP_INTERVAL_SECONDS
    try:
        return max(60, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_BACKUP_INTERVAL_SECONDS


def _current_data_version() -> int | None:
    global _dv_conn
    with _dv_lock:
        for _ in range(2):
            try:
                if _dv_conn is None:
                    # check_same_thread=False: the connection is shared by the
                    # hourly thread and the /admin/db-backup endpoint thread,
                    # always serialized under _dv_lock.
                    _dv_conn = sqlite3.connect(
                        db.db_path(), timeout=10.0, check_same_thread=False
                    )
                row = _dv_conn.execute("PRAGMA data_version").fetchone()
                return int(row[0])
            except Exception:
                try:
                    if _dv_conn is not None:
                        _dv_conn.close()
                except Exception:
                    pass
                _dv_conn = None
        return None


def _db_changed() -> bool:
    """False only when we are certain nothing changed since the last backup.

    Fails open: an unreadable data_version costs one redundant ~9MB copy,
    which is cheaper than a missed backup.
    """
    dv = _current_data_version()
    if dv is None or _dv_last_backed_up is None:
        return True
    return dv != _dv_last_backed_up


def _sweep_stale_tmp(dest_dir: Path) -> None:
    now = time.time()
    try:
        for tmp in dest_dir.glob(f"{BACKUP_FILENAME}.tmp-*"):
            try:
                if now - tmp.stat().st_mtime > _STALE_TMP_MAX_AGE_SECONDS:
                    tmp.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def run_backup_once(
    dest_dir: Path | None = None, *, force: bool = False
) -> dict[str, Any]:
    """One snapshot attempt; updates ``_state`` and returns the result dict."""
    global _dv_last_backed_up
    dest = dest_dir if dest_dir is not None else configured_backup_dir()
    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    if dest is None:
        result = {"ok": False, "error": f"{BACKUP_DIR_ENV} não definido"}
        with _state_lock:
            _state.update({
                "last_run_at": now_iso,
                "last_ok": False,
                "last_error": result["error"],
                "last_skipped": None,
            })
        return result

    with _state_lock:
        _state["running"] = True
        _state["last_run_at"] = now_iso
        _state["dest_dir"] = str(dest)
    try:
        _sweep_stale_tmp(dest)
        # Read data_version BEFORE the copy: writes that land mid-backup keep
        # the stored value stale, so the next tick backs up again (never lost).
        dv_before = _current_data_version()
        if not force and not _db_changed():
            result = {"ok": True, "skipped": "sem alterações desde o último backup"}
            with _state_lock:
                _state.update({
                    "last_ok": True,
                    "last_error": None,
                    "last_skipped": result["skipped"],
                })
            return result
        result = db.backup_to(dest / BACKUP_FILENAME)
        if result.get("ok"):
            _dv_last_backed_up = dv_before
            with _state_lock:
                _state.update({
                    "last_ok": True,
                    "last_error": None,
                    "last_skipped": None,
                    "last_backup_at": now_iso,
                    "last_size": result.get("size"),
                    "backups_done": int(_state.get("backups_done") or 0) + 1,
                })
        else:
            with _state_lock:
                _state.update({
                    "last_ok": False,
                    "last_error": result.get("error"),
                    "last_skipped": None,
                })
        return result
    finally:
        with _state_lock:
            _state["running"] = False


def _run_loop(dest_dir: Path, interval_seconds: int) -> None:
    while True:
        try:
            run_backup_once(dest_dir)
        except Exception:
            traceback.print_exc()
            with _state_lock:
                _state.update({"last_ok": False, "last_error": "erro inesperado"})
        time.sleep(interval_seconds)


def start_background_backup(
    *,
    dest_dir: Path | None = None,
    interval_seconds: int | None = None,
) -> bool:
    """Start the hourly backup thread. False (not an error) when disabled."""
    global _thread
    dest = dest_dir if dest_dir is not None else configured_backup_dir()
    if dest is None:
        with _state_lock:
            _state["enabled"] = False
        return False
    interval = interval_seconds or configured_interval_seconds()
    is_absolute_dest = _is_absolute_backup_path(dest)
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return True
        with _state_lock:
            _state.update({
                "enabled": False,
                "dest_dir": str(dest),
                "interval_seconds": interval,
            })
        if (not dest.exists() or not dest.is_dir()) and not is_absolute_dest:
            with _state_lock:
                _state["last_error"] = "pasta de backup não existe"
            return False
        with _state_lock:
            _state.update({
                "enabled": True,
                "last_error": (
                    None if dest.exists() else "pasta de backup não existe"
                ),
            })
        _thread = threading.Thread(
            target=_run_loop,
            args=(dest, interval),
            name="db-backup",
            daemon=True,
        )
        _thread.start()
        return True


def status() -> dict[str, Any]:
    with _state_lock:
        out = dict(_state)
    out["thread_alive"] = _thread is not None and _thread.is_alive()
    return out
