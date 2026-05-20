"""R110 — Registry central de tools que o agente Qwen pode invocar.

Cada submódulo (db_tools, chart_tools, propose_tools) exporta um dict
nome→{fn, description, parameters}. O wrapper qwen_agent.py concatena
todos via ALL_TOOLS.
"""
from app.pipeline.tools.chart_tools import (
    CHART_TOOLS,
    get_session_charts,
    propose_chart,
    reset_session_charts,
)
from app.pipeline.tools.db_tools import (
    DB_TOOLS,
    get_refs_summary,
    get_sheet,
    list_recent_edits,
    list_templates,
    query_db,
    query_learnings,
)
from app.pipeline.tools.propose_tools import (
    PROPOSE_TOOLS,
    get_session_proposals,
    propose_cpis_change,
    propose_rule,
    propose_template_change,
    reset_session_proposals,
)


# Concatena todos os tool registries para o agente.
ALL_TOOLS: dict = {}
ALL_TOOLS.update(DB_TOOLS)
ALL_TOOLS.update(CHART_TOOLS)
ALL_TOOLS.update(PROPOSE_TOOLS)


def reset_session_buffers() -> None:
    """Limpa buffers de charts + proposals no início de cada chat turn."""
    reset_session_charts()
    reset_session_proposals()


def collect_session_outputs() -> dict:
    """Recolhe charts + proposals acumulados na sessão actual."""
    return {
        "charts": get_session_charts(),
        "proposed_rules": get_session_proposals(),
    }


__all__ = [
    "ALL_TOOLS", "DB_TOOLS", "CHART_TOOLS", "PROPOSE_TOOLS",
    "query_db", "get_sheet", "list_recent_edits", "list_templates",
    "get_refs_summary", "query_learnings",
    "propose_chart", "propose_rule", "propose_template_change",
    "propose_cpis_change",
    "reset_session_buffers", "collect_session_outputs",
    "reset_session_charts", "get_session_charts",
    "reset_session_proposals", "get_session_proposals",
]
