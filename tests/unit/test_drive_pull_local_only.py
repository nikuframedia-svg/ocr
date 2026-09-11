"""Drive remains available to MES; the original OCR consumes only local refs."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def drive(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[2] / "scripts" / "drive_pull.py"
    spec = importlib.util.spec_from_file_location("drive_local_only", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "ocr"
    repo.mkdir()
    monkeypatch.setattr(module, "_REPO", repo)
    monkeypatch.setattr(module, "_STAGING", repo / "data" / "_drive_staging")
    monkeypatch.setattr(module, "log", lambda _message: None)
    monkeypatch.setenv("KANBAN_REFS_IMPORT_DIR", str(tmp_path / "local-input"))
    return module


def test_refresh_keeps_local_ocr_files_and_notifies_only_mes(drive, tmp_path, monkeypatch):
    local = drive.refs_import_dir()
    local.mkdir()
    plan = local / "plan_colunas_cpis.xlsx"
    machines = local / "maquinas.xlsx"
    plan.write_bytes(b"local plan")
    machines.write_bytes(b"local machines")
    mirror = tmp_path / "suite" / "drive"

    def download(_id, staging, **_kwargs):
        staging.mkdir(parents=True)
        for name, content in {
            "plan_colunas_cpis.xlsx": b"old drive plan",
            "maquinas.xlsx": b"drive machines",
            "Kanban's MTG2/11-09-2026.pdf": b"MTG2 scan",
            "Kanban's MTG3/11-09-2026.pdf": b"MTG3 scan",
        }.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return list(staging.rglob("*"))

    calls = []
    def post(url, **_kwargs):
        calls.append(url)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(drive, "download_folder", download)
    monkeypatch.setattr(drive.httpx, "post", post)
    monkeypatch.setenv("DRIVE_PULL_PUSH_REMOTE", "")
    monkeypatch.setenv("DRIVE_PULL_NOTIFY", ";".join([
        "http://127.0.0.1:8080/admin/import-refs",
        "http://127.0.0.1:8100/ingest/drive",
        "http://127.0.0.1:8101/ingest/drive",
    ]))
    monkeypatch.setattr(sys, "argv", ["drive_pull.py", "--mirror-to", str(mirror)])
    assert drive.main() == 0
    assert plan.read_bytes() == b"local plan"
    assert machines.read_bytes() == b"local machines"
    assert calls == [
        "http://127.0.0.1:8100/ingest/drive", "http://127.0.0.1:8101/ingest/drive",
    ]
    assert (mirror / "Kanban's MTG2/11-09-2026.pdf").read_bytes() == b"MTG2 scan"
    assert (mirror / "Kanban's MTG3/11-09-2026.pdf").read_bytes() == b"MTG3 scan"
    assert not (drive._REPO / "data" / "drive_pull_state.json").exists()


def test_original_ocr_export_is_never_fetched(drive, tmp_path, monkeypatch):
    calls = []
    def get(url, **_kwargs):
        calls.append(url)
        return httpx.Response(200, content=b"MES workbook", request=httpx.Request("GET", url))
    monkeypatch.setattr(drive.httpx, "get", get)
    result = drive.fetch_exports([
        "Kanbans_Producao_NOVO=http://127.0.0.1:8080/export",
        "BaseDados_Perfis_MTG2=http://127.0.0.1:8080/export/basedados",
        "BaseDados_Cantoneiras_MTG3=http://127.0.0.1:8100/export/basedados",
        "BaseDados_Perfis_MTG2=http://127.0.0.1:8101/export/basedados",
    ], tmp_path / "exports", False)
    assert calls == [
        "http://127.0.0.1:8100/export/basedados", "http://127.0.0.1:8101/export/basedados",
    ]
    assert {p.name for p in result} == {
        "BaseDados_Cantoneiras_MTG3.xlsx", "BaseDados_Perfis_MTG2.xlsx",
    }


@pytest.mark.parametrize("destination", ["refs", "refs-child", "repo", "repo-child", "parent"])
def test_drive_mirror_cannot_overwrite_ocr_paths(drive, tmp_path, destination):
    targets = {
        "refs": drive.refs_import_dir(),
        "refs-child": drive.refs_import_dir() / "incoming",
        "repo": drive._REPO,
        "repo-child": drive._REPO / "kanban_refs",
        "parent": tmp_path,
    }
    with pytest.raises(ValueError, match="OCR original"):
        drive.mirror_tree(drive._STAGING, targets[destination], False)


def test_rclone_upload_excludes_original_outputs_and_preserves_mes(drive, tmp_path):
    # Real local rclone copy: tests filter semantics, without accessing Drive.
    rclone = Path("/home/luis/bin/rclone")
    if not rclone.is_file():
        pytest.skip("local rclone unavailable")
    saida, remote = tmp_path / "saida", tmp_path / "remote"
    files = {
        "app.db": b"private OCR database",
        "Kanbans_Producao_NOVO.xlsx": b"private OCR production",
        "nested/app.db": b"another OCR copy",
        "backups/ocr-original/app-old.db": b"old OCR backup",
        "BaseDados_Perfis_MTG2.xlsx": b"MES perfis",
        "BaseDados_Cantoneiras_MTG3.xlsx": b"MES cantoneiras",
        "backups/kanban-mes/app-20260911.db": b"MES cantoneiras backup",
        "backups/kanban-mes-mtg2/app-20260911.db": b"MES perfis backup",
    }
    for name, content in files.items():
        path = saida / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    assert drive.push_outputs(str(remote), [saida], str(rclone), False) == 1
    actual = {str(p.relative_to(remote)) for p in remote.rglob("*") if p.is_file()}
    expected = {name for name in files if name.startswith(("BaseDados_", "backups/kanban-mes"))}
    assert actual == expected
    for name in expected:
        assert (remote / name).read_bytes() == files[name]
    assert drive.push_outputs(str(remote), [saida / "app.db", saida / "Kanbans_Producao_NOVO.xlsx"], str(rclone), False) == 0
    assert (saida / "app.db").read_bytes() == files["app.db"]


def test_retention_only_targets_mes_backup_folders(drive, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(drive, "fetch_exports", lambda *_args: [])
    monkeypatch.setattr(drive, "push_outputs", lambda *_args: 0)
    monkeypatch.setattr(drive, "download_folder", lambda *_args, **_kw: [])
    monkeypatch.setenv("DRIVE_PULL_PUSH_REMOTE", "gdrive:MTG/SAIDA")
    monkeypatch.setenv("DRIVE_PULL_MIRROR_TO", "")
    monkeypatch.setenv("DRIVE_PULL_NOTIFY", "")
    monkeypatch.setattr(sys, "argv", ["drive_pull.py"])
    assert drive.main() == 0
    assert [call[2] for call in calls] == [
        "gdrive:MTG/SAIDA/backups/kanban-mes",
        "gdrive:MTG/SAIDA/backups/kanban-mes-mtg2",
    ]
