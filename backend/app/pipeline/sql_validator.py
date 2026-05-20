"""R110.A — Validador de SQL via AST (sqlglot).

Garante que SQL gerado pelo LLM contra o app.db é seguro:
- Só SELECT (sem DDL/DML write)
- Tabelas whitelisted
- Sem subqueries profundas (depth > 3)
- LIMIT obrigatório, max 10.000 linhas

Levanta `SqlValidationError` com mensagem em PT-PT se algo falha — a
mensagem volta para o LLM para ele aprender dentro da sessão e tentar
de novo (function calling harness retry pattern).
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp


class SqlValidationError(ValueError):
    """Levantada quando o SQL não é seguro. Mensagem é mostrada ao LLM."""


# Tabelas que o LLM pode ler. Tudo o resto é rejeitado.
_ALLOWED_TABLES = frozenset({
    "sheets",
    "edits",
    "production_rows",
    "learnings",
    "learning_runs",
    "shadow_runs",
    "qwen_sessions",
    "qwen_charts",
    "proposals",
    "policy_versions",
    "template_overlay",
})

_MAX_DEPTH = 3
_MAX_LIMIT = 10_000


def validate(sql: str) -> str:
    """Devolve o SQL canonicalizado (whitespace normalizado) se válido.

    Levanta SqlValidationError com mensagem PT-PT caso contrário.
    """
    if not sql or not sql.strip():
        raise SqlValidationError("SQL vazio.")

    try:
        parsed = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as e:
        raise SqlValidationError(
            f"SQL não parseável: {e}. Verifica a sintaxe."
        ) from e

    if parsed is None:
        raise SqlValidationError("SQL vazio após parsing.")

    # Só permitimos SELECT no topo
    if not isinstance(parsed, exp.Select):
        raise SqlValidationError(
            "Apenas queries SELECT são permitidas. "
            f"Recebi: {type(parsed).__name__}."
        )

    # Rejeitar qualquer DDL/DML write em qualquer nível
    write_nodes = (exp.Insert, exp.Update, exp.Delete, exp.Create,
                   exp.Drop, exp.Alter, exp.TruncateTable)
    for node in parsed.walk():
        if isinstance(node, write_nodes):
            raise SqlValidationError(
                f"Operação de escrita não permitida: {type(node).__name__}. "
                "Só posso ler dados."
            )

    # Verificar tabelas referenciadas — todas têm de estar na whitelist
    tables_seen: set[str] = set()
    for table in parsed.find_all(exp.Table):
        name = (table.name or "").lower()
        if name:
            tables_seen.add(name)
    forbidden = tables_seen - _ALLOWED_TABLES
    if forbidden:
        raise SqlValidationError(
            f"Tabelas não autorizadas: {', '.join(sorted(forbidden))}. "
            f"Posso ler de: {', '.join(sorted(_ALLOWED_TABLES))}."
        )
    if not tables_seen:
        raise SqlValidationError("SQL não referencia nenhuma tabela.")

    # Verificar profundidade de subqueries
    depth = _max_subquery_depth(parsed)
    if depth > _MAX_DEPTH:
        raise SqlValidationError(
            f"Subqueries demasiado profundas (depth={depth}, max={_MAX_DEPTH}). "
            "Simplifica a query."
        )

    # Verificar LIMIT
    limit_node = parsed.args.get("limit")
    if limit_node is None:
        raise SqlValidationError(
            f"Tens de incluir LIMIT (máximo {_MAX_LIMIT})."
        )
    # Extrair valor numérico do LIMIT
    limit_expr = limit_node.expression if hasattr(limit_node, "expression") else limit_node
    try:
        limit_value = int(str(limit_expr).strip())
    except (ValueError, TypeError):
        raise SqlValidationError(
            "LIMIT tem de ser um número inteiro."
        )
    if limit_value > _MAX_LIMIT:
        raise SqlValidationError(
            f"LIMIT excede o máximo permitido ({_MAX_LIMIT}). "
            f"Recebi: {limit_value}."
        )
    if limit_value <= 0:
        raise SqlValidationError("LIMIT tem de ser positivo.")

    return parsed.sql(dialect="sqlite")


def _max_subquery_depth(node, depth: int = 0) -> int:
    """Profundidade máxima de nested subqueries. Top-level SELECT = 0."""
    max_d = depth
    for child in node.walk():
        if child is node:
            continue
        if isinstance(child, exp.Subquery):
            d = _max_subquery_depth(child.this, depth + 1)
            if d > max_d:
                max_d = d
    return max_d


__all__ = ["validate", "SqlValidationError"]
