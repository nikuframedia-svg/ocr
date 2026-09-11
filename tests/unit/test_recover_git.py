"""Exercise recovery against real Git repositories and SQLite WAL data."""
import importlib.util
from pathlib import Path
import sqlite3
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ops/recover_git.py"
spec = importlib.util.spec_from_file_location("recover_git", SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(root):
    repair.git(root, "add", ".")
    repair.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "-qm", "test")


@pytest.fixture
def installation(tmp_path):
    remote = tmp_path / "origin"
    remote.mkdir()
    repair.git(remote, "init", "-b", "main")
    for name in ["backend/app/web/main.py", "scripts/ops/update.ps1", "ocr6.py"]:
        put(remote, name, "new code\n")
    for name in ["data/app.db", "kanban_refs/04_Documentacao/plan_colunas_cpis.xlsx",
                 "lexicons/learned.json", "inputs/scan.png", "reports/production.csv"]:
        put(remote, name, "repository sample -- MUST NOT install\n")
    commit(remote)
    root = tmp_path / "OCR original"
    for name in ["backend/app/web/main.py", "scripts/ops/update.ps1"]:
        put(root, name, "old code\n")
    for name in [".env", ".venv/keep", "kanban_refs/04_Documentacao/plan_colunas_cpis.xlsx",
                 "lexicons/learned.json", "data/photo.png"]:
        put(root, name, "production data\n")
    (root / ".git").mkdir()
    (root / "data").mkdir(exist_ok=True)
    db = sqlite3.connect(root / "data/app.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE production (value TEXT)")
    db.execute("INSERT INTO production VALUES ('local production')")
    db.commit()
    yield root, remote, db
    db.close()


def test_recovers_code_preserves_runtime_and_supports_next_pull(installation):
    root, remote, db = installation
    kept = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()
            and not p.is_relative_to(root / "backend") and not p.is_relative_to(root / "scripts")
            and p.name not in {"app.db-shm", "app.db-wal"}}
    sha, backup = repair.recover(root, str(remote))
    assert sha == repair.git(remote, "rev-parse", "HEAD").decode().strip()
    assert (root / "ocr6.py").read_text() == "new code\n"
    assert (backup / "code-before/backend/app/web/main.py").read_text() == "old code\n"
    for path, content in kept.items():
        assert path.read_bytes() == content
    assert not (root / "inputs/scan.png").exists()
    with sqlite3.connect(backup / "app.db") as snapshot:
        assert snapshot.execute("SELECT value FROM production").fetchone() == ("local production",)
    assert not repair.git(root, "status", "--porcelain", "--untracked-files=no")
    put(remote, "ocr6.py", "next update\n")
    commit(remote)
    repair.git(root, "pull", "--ff-only", "origin", "main")
    assert (root / "ocr6.py").read_text() == "next update\n"
    assert db.execute("SELECT value FROM production").fetchone() == ("local production",)
    assert (root / "kanban_refs/04_Documentacao/plan_colunas_cpis.xlsx").read_text() == "production data\n"


def test_refuses_nonempty_metadata(installation):
    root, remote, _ = installation
    put(root, ".git/unknown", "keep")
    with pytest.raises(RuntimeError, match="nao esta vazia"):
        repair.recover(root, str(remote))
    assert (root / ".git/unknown").read_text() == "keep"


def test_fetch_failure_leaves_installation_untouched(installation, tmp_path):
    root, _, _ = installation
    with pytest.raises(RuntimeError):
        repair.recover(root, str(tmp_path / "missing-remote"))
    assert not list((root / ".git").iterdir())
    assert (root / "backend/app/web/main.py").read_text() == "old code\n"


def test_copy_failure_rolls_back_code_and_git(installation, monkeypatch):
    root, remote, _ = installation
    original = repair.replace_file

    def fail(source, destination):
        if destination == root / "scripts/ops/update.ps1" and "source" in source.parts:
            raise OSError("injected disk failure")
        original(source, destination)

    monkeypatch.setattr(repair, "replace_file", fail)
    with pytest.raises(OSError, match="injected disk failure"):
        repair.recover(root, str(remote))
    assert (root / "backend/app/web/main.py").read_text() == "old code\n"
    assert not (root / "ocr6.py").exists()
    assert not list((root / ".git").iterdir())


def test_refuses_code_symlink(installation, tmp_path):
    root, remote, _ = installation
    outside = tmp_path / "outside.py"
    outside.write_text("must stay")
    (root / "ocr6.py").symlink_to(outside)
    with pytest.raises(RuntimeError, match="Link/junction"):
        repair.recover(root, str(remote))
    assert outside.read_text() == "must stay"
    assert not list((root / ".git").iterdir())


def test_updater_snapshot_is_local_and_includes_wal(installation):
    root, _, _ = installation
    updater = SCRIPT.with_name("update.ps1").read_text()
    source = updater.split("$backupCode = @'\n", 1)[1].split("\n'@", 1)[0]
    subprocess.run([__import__("sys").executable, "-", str(root)], input=source,
                   text=True, check=True)
    with sqlite3.connect(root / "data/backups/app-pre-update.db") as snapshot:
        assert snapshot.execute("SELECT value FROM production").fetchone() == ("local production",)
