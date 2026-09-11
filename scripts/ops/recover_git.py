"""Recover an OCR installation with missing/empty .git; never check out over data.

Run with the installation's Python, then use the existing update.ps1 normally.
Only standard-library modules and Git are required. No services are restarted.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

REMOTE = "https://github.com/nikuframedia-svg/ocr.git"
CODE_DIRS = {"backend", "scripts", "prompts", "infra", "docs", "tests"}
CODE_FILES = {
    ".env.example", ".gitignore", "CLAUDE.md", "README.md", "docker-compose.yml",
    "ocr6.py", "ocr6_crops.py", "pyproject.toml", "uv.lock",
}


def git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def code_path(name: str) -> bool:
    return name in CODE_FILES or name.split("/", 1)[0] in CODE_DIRS


def digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_path(root: Path, name: str) -> Path:
    path = root / name
    if path.is_absolute() and not path.is_relative_to(root):
        raise RuntimeError(f"Caminho fora da instalacao: {name}")
    for part in [path, *path.parents]:
        if part == root.parent:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise RuntimeError(f"Link/junction recusado: {part}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f"Caminho fora da instalacao: {name}")
    return path


def replace_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ocr-recovery-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recover(root: Path, remote: str = REMOTE) -> tuple[str, Path]:
    root = root.absolute()
    check_path(root, ".git")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        if os.environ.get(name):
            raise RuntimeError(f"Variavel {name} definida; abrir uma consola limpa.")
    for name in ("backend/app/web/main.py", "scripts/ops/update.ps1", "data/app.db"):
        if not check_path(root, name).is_file():
            raise RuntimeError(f"Instalacao OCR incompleta: falta {name}")
    metadata = root / ".git"
    if metadata.exists() and (not metadata.is_dir() or any(metadata.iterdir())):
        raise RuntimeError("A .git nao esta vazia. Recuperacao recusada; conservar o historico existente.")

    recovery = Path(tempfile.mkdtemp(prefix=f"{root.name}-git-recovery-", dir=root.parent))
    print(f"RECOVERY_BACKUP={recovery}", flush=True)
    staging = recovery / "source"
    # Clone into a separate directory: Git never checks out production data.
    git(root.parent, "clone", "--no-hardlinks", "--single-branch", "--branch", "main", remote, str(staging))
    sha = git(staging, "rev-parse", "HEAD").decode().strip()
    names = [n.decode("utf-8") for n in git(staging, "ls-files", "-z").split(b"\0") if n]
    if not {"backend/app/web/main.py", "scripts/ops/update.ps1"}.issubset(names):
        raise RuntimeError("O repositorio descarregado nao corresponde ao OCR.")
    changed = []
    preserved = []
    for name in names:
        if not code_path(name):
            preserved.append(name)
            continue
        target = check_path(root, name)
        source = check_path(staging, name)
        before = digest(target)
        if before != digest(source):
            changed.append({"path": name, "before": before})
            if before is not None:
                backup = recovery / "code-before" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
    # Preserve every tracked runtime/data file, even when absent locally.
    if preserved:
        git(staging, "update-index", "--skip-worktree", "-z", "--stdin",
            input=b"\0".join(n.encode("utf-8") for n in preserved) + b"\0")

    # SQLite's online backup API includes committed WAL data without stopping OCR.
    db = check_path(root, "data/app.db")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=30)) as source_db:
        with closing(sqlite3.connect(recovery / "app.db")) as backup_db:
            source_db.backup(backup_db)
            if backup_db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup SQLite invalido; instalacao nao alterada.")
    (recovery / "manifest.json").write_text(json.dumps({
        "root": str(root), "commit": sha, "changed": changed, "preserved": preserved,
    }, indent=2), encoding="utf-8")
    applied = []
    old_metadata = recovery / "git-before"
    installed_git = False
    try:
        for item in changed:
            name = item["path"]
            target = check_path(root, name)
            if digest(target) != item["before"]:
                raise RuntimeError(f"Ficheiro alterado durante a recuperacao: {name}")
            applied.append(item)
            replace_file(staging / name, target)
        if metadata.exists():
            if any(metadata.iterdir()):
                raise RuntimeError("A .git mudou durante a recuperacao; operacao cancelada.")
            metadata.rename(old_metadata)
        (staging / ".git").rename(metadata)
        installed_git = True
        if git(root, "rev-parse", "HEAD").decode().strip() != sha:
            raise RuntimeError("A verificacao final do Git falhou.")
        # A full comparison proves all permitted code is actually installed.
        for name in names:
            if code_path(name) and digest(root / name) != digest(staging / name):
                raise RuntimeError(f"Verificacao do codigo falhou: {name}")
    except BaseException:
        if installed_git:
            metadata.rename(staging / ".git")
        if old_metadata.exists():
            old_metadata.rename(metadata)
        for item in reversed(applied):
            target = root / item["path"]
            if item["before"] is None:
                target.unlink(missing_ok=True)
            else:
                replace_file(recovery / "code-before" / item["path"], target)
        raise
    print(f"RECOVERY_OK: main {sha}", flush=True)
    print("Dados, referencias, .env e .venv preservados. Executar agora scripts\\ops\\update.ps1.", flush=True)
    return sha, recovery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        recover(args.root)
    except Exception as exc:
        print(f"RECOVERY_FAILED: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
