from pathlib import Path

from app import config


def test_dotenv_value_reads_simple_key(tmp_path):
    (tmp_path / ".env").write_text(
        "CROSS_CHECK_DIR=kanban_refs/03_Cross_Check\n"
        "OTHER='quoted value'\n",
        encoding="utf-8",
    )

    assert config._dotenv_value(tmp_path, "CROSS_CHECK_DIR") == "kanban_refs/03_Cross_Check"
    assert config._dotenv_value(tmp_path, "OTHER") == "quoted value"
    assert config._dotenv_value(tmp_path, "MISSING") is None


def test_resolve_kanban_path_ignores_windows_default_on_posix(monkeypatch):
    monkeypatch.delenv("TEST_CROSS_CHECK_DIR", raising=False)

    resolved = config.resolve_kanban_path(
        "TEST_CROSS_CHECK_DIR",
        r"C:\kanban\nifruka\03_Cross_Check",
        "kanban_refs/03_Cross_Check",
    )

    assert resolved == Path(config.__file__).resolve().parents[2] / "kanban_refs/03_Cross_Check"
