"""R110.A — Tools de leitura do app.db expostas ao agente Qwen.

Cada tool é uma função pura com type hints + docstring. O wrapper
`qwen_agent.py` extrai o schema automaticamente via Pydantic e
transforma em OpenAI/Ollama function spec.

Todas as queries SQL passam por `sql_validator.validate` — não há
escape possível. Operações de escrita são rejeitadas. Tabelas fora
da whitelist são rejeitadas.

Outputs limitados em tamanho (max 100 linhas por chamada) para o
context window do Qwen não explodir.
"""
from __future__ import annotations

import json
from typing import Any

from app.pipeline.sql_validator import SqlValidationError, validate
from app.web import db


_MAX_ROWS_OUT = 100


# ----- tool: query_db -----------------------------------------------------

def query_db(sql: str) -> dict:
    """Executa um SELECT contra o app.db e devolve as primeiras 100 linhas.

    O SQL é validado: só SELECT, tabelas da whitelist (sheets, edits,
    production_rows, learnings, learning_runs, shadow_runs, qwen_sessions,
    qwen_charts, proposals, policy_versions, template_overlay), LIMIT
    obrigatório ≤ 10000.

    Args:
        sql: Query SQL em sintaxe SQLite. Tem de incluir LIMIT.

    Returns:
        dict com:
        - status: "ok" ou "error"
        - rows: lista de dicts (até 100 linhas) se ok
        - n_rows: número de linhas devolvidas
        - error: mensagem PT-PT se falhou
    """
    try:
        validated_sql = validate(sql)
    except SqlValidationError as e:
        return {"status": "error", "error": str(e), "rows": [], "n_rows": 0}

    try:
        with db.conn() as c:
            cur = c.execute(validated_sql)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(_MAX_ROWS_OUT)]
            return {
                "status": "ok",
                "rows": rows,
                "n_rows": len(rows),
                "columns": cols,
            }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Falhou ao executar a query: {e}",
            "rows": [],
            "n_rows": 0,
        }


# ----- tool: get_sheet ----------------------------------------------------

def get_sheet(sheet_id: int) -> dict:
    """Devolve detalhe completo de uma folha — header, rows, footer + edits.

    Args:
        sheet_id: ID inteiro da folha em sheets.id.

    Returns:
        dict com sheet_data (JSON parsed), dq_audit, edits recentes,
        ou {"status": "error", "error": ...} se não existir.
    """
    try:
        sheet = db.get_sheet(sheet_id)
        if sheet is None:
            return {"status": "error", "error": f"Folha {sheet_id} não existe."}
        # Parse JSON fields
        sd = sheet.get("sheet_data")
        if isinstance(sd, str):
            try:
                sd = json.loads(sd)
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "status": "ok",
            "sheet_id": sheet_id,
            "status_in_db": sheet.get("status"),
            "captured_at": sheet.get("captured_at"),
            "validated_at": sheet.get("validated_at"),
            "operador": sheet.get("operador"),
            "template_name": (sd.get("template_name") if isinstance(sd, dict) else None),
            "header": (sd.get("header") if isinstance(sd, dict) else None),
            "rows": (sd.get("rows") if isinstance(sd, dict) else None),
            "footer": (sd.get("footer") if isinstance(sd, dict) else None),
        }
    except Exception as e:
        return {"status": "error", "error": f"Falhou ao ler folha: {e}"}


# ----- tool: list_recent_edits --------------------------------------------

def list_recent_edits(n_days: int = 7, source: str = "human") -> dict:
    """Devolve edições humanas (ou de sistema) das últimas N dias.

    Útil para o agente ver padrões de correcção recentes.

    Args:
        n_days: Janela em dias (max 90, default 7).
        source: 'human' (default), 'system' ou 'all'.

    Returns:
        dict com lista de edits (max 100) — sheet_id, field_path,
        old_value, new_value, edited_at, source.
    """
    n_days = max(1, min(n_days, 90))
    where_src = "" if source == "all" else "AND source = ?"
    params: list = [n_days]
    if source != "all":
        params.append(source)
    sql = f"""
        SELECT id, sheet_id, field_path, old_value, new_value, source, edited_at
        FROM edits
        WHERE edited_at >= datetime('now', '-' || ? || ' days')
        {where_src}
        ORDER BY edited_at DESC
        LIMIT {_MAX_ROWS_OUT}
    """
    try:
        with db.conn() as c:
            cur = c.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return {"status": "ok", "n_rows": len(rows), "rows": rows}
    except Exception as e:
        return {"status": "error", "error": str(e), "rows": [], "n_rows": 0}


# ----- tool: list_templates -----------------------------------------------

def list_templates() -> dict:
    """Devolve schemas de todos os templates registados (bobine, gasparini,
    quinadora, soldline, laser, paragens, expedição) — com lista de campos
    por secção (header / row / footer).
    """
    try:
        from app.templates_registry import TEMPLATES
        out = []
        for name, spec in TEMPLATES.items():
            out.append({
                "name": name,
                "csv_block_label": getattr(spec, "csv_block_label", ""),
                "header_fields": list(getattr(spec, "header_fields", []) or []),
                "row_fields": list(getattr(spec, "row_fields", []) or []),
                "footer_fields": list(getattr(spec, "footer_fields", []) or []),
            })
        return {"status": "ok", "templates": out}
    except Exception as e:
        return {"status": "error", "error": str(e), "templates": []}


# ----- tool: get_refs_summary ---------------------------------------------

def get_refs_summary() -> dict:
    """Estatísticas resumidas dos refs vivos (StockSAP + plan_colunas).

    Devolve contagens (ofs, ovs, lotes, clientes, modelos) e timestamp
    de quando foram carregados — não devolve os dados em si.
    """
    try:
        from app.cross_check.ref_watcher import get_watcher
        refs = get_watcher().get_refs() or {}
        stats = refs.get("stats") or {}
        return {
            "status": "ok",
            "available": refs.get("available", False),
            "loaded_at": refs.get("loaded_at"),
            "n_ofs": stats.get("n_ofs", 0),
            "n_ovs": stats.get("n_ovs", 0),
            "n_clientes": stats.get("n_clientes", 0),
            "n_lotes_sap": stats.get("n_lotes", 0),
            "n_plan_rows": stats.get("n_plan_rows", 0),
            "n_modelos": stats.get("n_modelos_fts", 0),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ----- tool: query_learnings ----------------------------------------------

def query_learnings(status: str = "", kind: str = "", limit: int = 30) -> dict:
    """Lista proposals do motor R98 (regras de aprendizagem).

    Args:
        status: '' (todos), 'approved', 'quarantine', 'rejected'.
        kind: '' (todos), 'cliente_alias', 'modelo_alias', 'confusion_pair',
              'observed_value', 'fewshot_example', 'snap_rule'.
        limit: Max 100.
    """
    limit = max(1, min(limit, _MAX_ROWS_OUT))
    where: list[str] = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, kind, field, template_name, payload, risk, status,
               evidence_count, created_at
        FROM learnings
        {where_sql}
        ORDER BY created_at DESC
        LIMIT {limit}
    """
    try:
        with db.conn() as c:
            cur = c.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = []
            for r in cur.fetchall():
                d = dict(zip(cols, r))
                # parse payload JSON
                if isinstance(d.get("payload"), str):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                rows.append(d)
            return {"status": "ok", "n_rows": len(rows), "rows": rows}
    except Exception as e:
        return {"status": "error", "error": str(e), "rows": [], "n_rows": 0}


# ----- exports + schemas --------------------------------------------------

DB_TOOLS: dict[str, dict[str, Any]] = {
    "query_db": {
        "fn": query_db,
        "description": "Executa um SELECT contra a base de dados do sistema. "
                       "Tem de incluir LIMIT (max 10000). Devolve até 100 "
                       "linhas. Tabelas disponíveis: sheets, edits, "
                       "production_rows, learnings, learning_runs, shadow_runs, "
                       "qwen_sessions, qwen_charts, proposals, policy_versions, "
                       "template_overlay.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Query SELECT em sintaxe SQLite. Tem de "
                                   "incluir LIMIT.",
                },
            },
            "required": ["sql"],
        },
    },
    "get_sheet": {
        "fn": get_sheet,
        "description": "Devolve detalhe completo de uma folha — header, rows, "
                       "footer, edits. Usa o sheet_id obtido via query_db.",
        "parameters": {
            "type": "object",
            "properties": {
                "sheet_id": {"type": "integer",
                             "description": "ID da folha em sheets.id."},
            },
            "required": ["sheet_id"],
        },
    },
    "list_recent_edits": {
        "fn": list_recent_edits,
        "description": "Devolve edições das últimas N dias. Útil para ver "
                       "padrões de correcção recentes.",
        "parameters": {
            "type": "object",
            "properties": {
                "n_days": {"type": "integer",
                           "description": "Janela em dias (max 90). Default 7."},
                "source": {"type": "string", "enum": ["human", "system", "all"],
                           "description": "Filtrar por origem da edição."},
            },
        },
    },
    "list_templates": {
        "fn": list_templates,
        "description": "Devolve schemas de todos os templates (bobine, "
                       "gasparini, quinadora, etc.) — campos por secção.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_refs_summary": {
        "fn": get_refs_summary,
        "description": "Estatísticas dos refs vivos (StockSAP + plan_colunas): "
                       "contagens de OFs, OVs, clientes, lotes, modelos.",
        "parameters": {"type": "object", "properties": {}},
    },
    "query_learnings": {
        "fn": query_learnings,
        "description": "Lista proposals do motor de aprendizagem R98. Filtros "
                       "por status (approved/quarantine/rejected) e kind "
                       "(cliente_alias, modelo_alias, confusion_pair, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "description": "Filtro: '' (todos), 'approved', "
                                          "'quarantine', 'rejected'."},
                "kind": {"type": "string",
                         "description": "Filtro: '' (todos), 'cliente_alias', "
                                        "etc."},
                "limit": {"type": "integer",
                          "description": "Max 100. Default 30."},
            },
        },
    },
}


__all__ = [
    "DB_TOOLS",
    "query_db",
    "get_sheet",
    "list_recent_edits",
    "list_templates",
    "get_refs_summary",
    "query_learnings",
]
