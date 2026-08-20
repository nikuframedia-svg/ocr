"""R267 — hourly app.db copy into the Drive-synced folder."""
from __future__ import annotations

import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.web import db, db_backup, main


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(db_backup, "_dv_conn", None)
    monkeypatch.setattr(db_backup, "_dv_last_backed_up", None)
    with db.conn() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
    yield tmp_path / "app.db"
    # Close the module-level pragma connection so tmp_path can be removed.
    if db_backup._dv_conn is not None:
        db_backup._dv_conn.close()


def test_backup_to_creates_standalone_consistent_copy(tmp_db, tmp_path):
    dest = tmp_path / "drive" / "app.db"

    result = db.backup_to(dest)

    assert result["ok"] is True
    assert result["size"] > 0
    copy = sqlite3.connect(dest)
    try:
        assert copy.execute("SELECT x FROM t").fetchone()[0] == 1
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()
    assert not dest.with_name("app.db-wal").exists()
    assert not list(dest.parent.glob("app.db.tmp-*"))


def test_backup_to_retries_permission_error(tmp_db, tmp_path, monkeypatch):
    dest = tmp_path / "drive" / "app.db"
    real_replace = os.replace
    fails = {"n": 2}

    def flaky_replace(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("ficheiro em uso")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    assert db.backup_to(dest)["ok"] is True
    assert dest.exists()
    assert not list(dest.parent.glob("app.db.tmp-*"))

    def always_fails(_src, _dst):
        raise PermissionError("ficheiro em uso")

    monkeypatch.setattr(os, "replace", always_fails)
    result = db.backup_to(dest)
    assert result["ok"] is False
    assert "em uso" in result["error"]
    assert not list(dest.parent.glob("app.db.tmp-*"))


def test_run_backup_once_skips_when_unchanged(tmp_db, tmp_path):
    dest_dir = tmp_path / "drive"

    first = db_backup.run_backup_once(dest_dir)
    assert first["ok"] is True and "skipped" not in first

    second = db_backup.run_backup_once(dest_dir)
    assert second["ok"] is True
    assert second["skipped"] == "sem alterações desde o último backup"
    assert db_backup.status()["backups_done"] == 1

    with db.conn() as c:
        c.execute("INSERT INTO t VALUES (2)")
    third = db_backup.run_backup_once(dest_dir)
    assert third["ok"] is True and "skipped" not in third
    copy = sqlite3.connect(dest_dir / "app.db")
    try:
        assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    finally:
        copy.close()


def test_run_backup_once_without_config_reports_error(monkeypatch):
    monkeypatch.setattr(db_backup, "_config_value", lambda _name: None)

    result = db_backup.run_backup_once()

    assert result["ok"] is False
    assert "KANBAN_DB_BACKUP_DIR" in result["error"]


def test_start_background_backup_disabled_without_env(monkeypatch):
    monkeypatch.setattr(db_backup, "_config_value", lambda _name: None)

    assert db_backup.start_background_backup() is False
    assert db_backup.status()["enabled"] is False


def test_backup_dir_preserves_windows_paths(monkeypatch):
    monkeypatch.setenv(
        db_backup.BACKUP_DIR_ENV, r"G:\O meu Disco\MTG _ Kanban Digital"
    )
    assert (
        str(db_backup.configured_backup_dir())
        == r"G:\O meu Disco\MTG _ Kanban Digital"
    )

    monkeypatch.setenv(db_backup.BACKUP_DIR_ENV, "")
    monkeypatch.setattr(db_backup, "_dotenv_value", lambda _root, _name: None)
    assert db_backup.configured_backup_dir() is None


def test_backup_interval_clamped(monkeypatch):
    monkeypatch.setenv(db_backup.BACKUP_INTERVAL_ENV, "5")
    assert db_backup.configured_interval_seconds() == 60
    monkeypatch.setenv(db_backup.BACKUP_INTERVAL_ENV, "7200")
    assert db_backup.configured_interval_seconds() == 7200
    monkeypatch.setenv(db_backup.BACKUP_INTERVAL_ENV, "nope")
    assert db_backup.configured_interval_seconds() == 3600


def test_admin_db_backup_endpoint(tmp_db, tmp_path, monkeypatch):
    db.init_db()  # request middleware needs the full schema
    dest_dir = tmp_path / "drive"
    monkeypatch.setattr(
        db_backup, "_config_value",
        lambda name: str(dest_dir) if name == db_backup.BACKUP_DIR_ENV else None,
    )
    client = TestClient(main.app)

    resp = client.post("/admin/db-backup")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert (dest_dir / "app.db").exists()

    monkeypatch.setattr(db_backup, "_config_value", lambda _name: None)
    resp = client.post("/admin/db-backup")
    assert resp.status_code == 400
