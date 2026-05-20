"""R110.A — Testes do agente Qwen + tools + validador SQL.

Não invoca o Ollama real. Tests unitários do harness, sql_validator
e tools de leitura puras (que correm contra o app.db real em modo
read-only).
"""
from __future__ import annotations

import pytest

from app.pipeline.sql_validator import SqlValidationError, validate
from app.pipeline.tools import (
    DB_TOOLS,
    get_refs_summary,
    list_recent_edits,
    list_templates,
    query_db,
    query_learnings,
)


# ----- SQL Validator ------------------------------------------------------

class TestSqlValidator:
    def test_select_basic_passes(self):
        validate("SELECT id FROM sheets LIMIT 10")

    def test_select_with_joins_passes(self):
        validate(
            "SELECT s.id, COUNT(e.id) FROM sheets s "
            "LEFT JOIN edits e ON e.sheet_id = s.id "
            "GROUP BY s.id LIMIT 50"
        )

    def test_delete_rejected(self):
        with pytest.raises(SqlValidationError, match="SELECT"):
            validate("DELETE FROM sheets LIMIT 1")

    def test_insert_rejected(self):
        with pytest.raises(SqlValidationError):
            validate("INSERT INTO sheets (id) VALUES (1)")

    def test_drop_rejected(self):
        with pytest.raises(SqlValidationError):
            validate("DROP TABLE sheets")

    def test_update_rejected(self):
        with pytest.raises(SqlValidationError):
            validate("UPDATE sheets SET status='x' LIMIT 1")

    def test_unauthorized_table_rejected(self):
        with pytest.raises(SqlValidationError, match="autorizadas"):
            validate("SELECT * FROM sqlite_master LIMIT 5")

    def test_no_limit_rejected(self):
        with pytest.raises(SqlValidationError, match="LIMIT"):
            validate("SELECT * FROM sheets")

    def test_limit_too_high_rejected(self):
        with pytest.raises(SqlValidationError, match="excede"):
            validate("SELECT * FROM sheets LIMIT 99999")

    def test_limit_zero_rejected(self):
        with pytest.raises(SqlValidationError, match="positivo"):
            validate("SELECT * FROM sheets LIMIT 0")

    def test_empty_sql_rejected(self):
        with pytest.raises(SqlValidationError, match="vazio"):
            validate("")

    def test_malformed_sql_rejected(self):
        with pytest.raises(SqlValidationError):
            validate("SELEKT * FROM sheets LIMIT 10")


# ----- DB Tools -----------------------------------------------------------

class TestDBTools:
    def test_all_tools_registered(self):
        expected = {
            "query_db", "get_sheet", "list_recent_edits",
            "list_templates", "get_refs_summary", "query_learnings",
        }
        assert set(DB_TOOLS.keys()) == expected

    def test_all_tools_have_schema(self):
        for name, info in DB_TOOLS.items():
            assert callable(info["fn"]), f"{name} missing fn"
            assert info["description"], f"{name} missing description"
            assert "parameters" in info, f"{name} missing parameters"

    def test_query_db_rejects_bad_sql(self):
        result = query_db("DELETE FROM sheets")
        assert result["status"] == "error"
        assert "SELECT" in result["error"]
        assert result["rows"] == []

    def test_query_db_runs_valid_select(self):
        result = query_db(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 5"
        )
        # sqlite_master não está na whitelist — deve falhar
        assert result["status"] == "error"

    def test_query_db_runs_real_query(self):
        result = query_db("SELECT id FROM sheets LIMIT 5")
        assert result["status"] == "ok"
        assert isinstance(result["rows"], list)
        assert result["n_rows"] >= 0
        assert "columns" in result

    def test_list_templates_returns_templates(self):
        result = list_templates()
        assert result["status"] == "ok"
        names = [t["name"] for t in result["templates"]]
        # Pelo menos os templates principais têm de aparecer
        assert "bobine_formato" in names
        assert len(names) >= 5

    def test_get_refs_summary_shape(self):
        result = get_refs_summary()
        # Pode falhar se refs não estão carregados, mas shape é constante
        assert "status" in result

    def test_query_learnings_basic(self):
        result = query_learnings(limit=5)
        assert result["status"] == "ok"
        assert isinstance(result["rows"], list)
        assert result["n_rows"] <= 5

    def test_list_recent_edits_bounded(self):
        result = list_recent_edits(n_days=1)
        assert result["status"] == "ok"
        assert result["n_rows"] <= 100


# ----- Qwen Agent (sem chamar Ollama) -------------------------------------

class TestQwenAgentHarness:
    def test_module_imports(self):
        from app.pipeline.qwen_agent import chat  # noqa: F401

    def test_build_tools_spec(self):
        from app.pipeline.qwen_agent import _build_ollama_tools_spec
        spec = _build_ollama_tools_spec()
        assert len(spec) >= 6
        for tool in spec:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_execute_tool_unknown(self):
        from app.pipeline.qwen_agent import _execute_tool
        result = _execute_tool("nonexistent_tool", {})
        assert result["status"] == "error"
        assert "desconhecida" in result["error"]

    def test_execute_tool_real(self):
        from app.pipeline.qwen_agent import _execute_tool
        result = _execute_tool("list_templates", {})
        assert result["status"] == "ok"
        assert "templates" in result

    def test_parse_envelope_valid_json(self):
        from app.pipeline.qwen_agent import _parse_envelope
        content = '{"reply": "olá", "charts": [], "proposed_rules": []}'
        env = _parse_envelope(content)
        assert env["reply"] == "olá"
        assert env["charts"] == []
        assert env["proposed_rules"] == []

    def test_parse_envelope_markdown_fallback(self):
        """Quando o modelo devolve markdown puro, fica como reply."""
        from app.pipeline.qwen_agent import _parse_envelope
        env = _parse_envelope("# Título\n\nTexto livre.")
        assert "Título" in env["reply"]
        assert env["charts"] == []
        assert env["proposed_rules"] == []

    def test_parse_envelope_with_code_fence(self):
        from app.pipeline.qwen_agent import _parse_envelope
        content = '```json\n{"reply": "x", "charts": [], "proposed_rules": []}\n```'
        env = _parse_envelope(content)
        assert env["reply"] == "x"

    def test_parse_envelope_empty(self):
        from app.pipeline.qwen_agent import _parse_envelope
        env = _parse_envelope("")
        assert env == {"reply": "", "charts": [], "proposed_rules": []}


# ----- DB persistence -----------------------------------------------------

class TestQwenSessionsPersistence:
    def test_save_and_list_session(self):
        from app.web import db
        db.init_db()
        envelope = {
            "reply": "resposta de teste",
            "charts": [],
            "proposed_rules": [],
            "_meta": {
                "tools_used": ["query_db"],
                "tool_calls_count": 1,
                "rounds": 2,
                "duration_ms": 1234,
                "status": "ok",
            },
        }
        sid = db.save_qwen_session("pergunta teste", envelope, trigger="manual")
        assert sid > 0
        sessions = db.list_qwen_sessions(limit=5)
        ids = [s["id"] for s in sessions]
        assert sid in ids
