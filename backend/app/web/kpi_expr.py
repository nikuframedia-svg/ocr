"""Task C E3 — avaliador seguro de fórmulas de KPI (mini-DSL aritmética).

O dono edita as fórmulas dos KPIs do dashboard de produção em /admin/kpis.
Uma fórmula é uma expressão aritmética sobre variáveis nomeadas, p.ex.
``qtd / horas`` ou ``max(kg_produzido - kg_desperdicio, 0) / 1000``.

Segurança: NUNCA usar eval/exec — walker próprio sobre ast.parse(mode="eval")
com whitelist estrita:
  - BinOp {+, -, *, /}, UnaryOp {+, -}
  - Constant numérico (int/float; bool excluído)
  - Name (tem de estar nas variáveis do scope)
  - Call apenas {min, max, round, abs}, argumentos posicionais
Tudo o resto é rejeitado (Attribute, Subscript, Pow, Compare, lambda,
comprehensions, keywords, …). Limites: 200 chars, 60 nós, profundidade 15.

Semântica de runtime: divisão por zero e variável a None propagam ``None``
(o chamador decide o compat: 0 ou ocultar) — uma fórmula nunca crasha o
dashboard.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping

__all__ = ["MAX_CHARS", "KpiExprError", "eval_expr", "validate_expr"]

MAX_CHARS = 200
_MAX_NODES = 60
_MAX_DEPTH = 15

_ALLOWED_FUNCS: dict[str, object] = {
    "min": min, "max": max, "round": round, "abs": abs,
}
_ALLOWED_BINOPS: dict[type, str] = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
}
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


class KpiExprError(ValueError):
    """Erro de validação de fórmula — mensagem PT-PT pronta a mostrar."""


def _parse(expr: str) -> ast.expr:
    if not isinstance(expr, str) or not expr.strip():
        raise KpiExprError("a fórmula está vazia")
    if len(expr) > MAX_CHARS:
        raise KpiExprError(f"fórmula demasiado longa (máx. {MAX_CHARS} caracteres)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise KpiExprError("sintaxe inválida na fórmula") from None
    n_nodes = sum(1 for _ in ast.walk(tree))
    if n_nodes > _MAX_NODES:
        raise KpiExprError(f"fórmula demasiado complexa (máx. {_MAX_NODES} elementos)")
    return tree.body


def _check(node: ast.expr, variables: frozenset[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise KpiExprError(
            f"fórmula com aninhamento demasiado profundo (máx. {_MAX_DEPTH} níveis)")
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BINOPS:
            raise KpiExprError(
                "operação não permitida — só + - * / (sem potências)")
        _check(node.left, variables, depth + 1)
        _check(node.right, variables, depth + 1)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARY):
            raise KpiExprError("operador unário não permitido")
        _check(node.operand, variables, depth + 1)
        return
    if isinstance(node, ast.Constant):
        # bool é subclasse de int — excluir explicitamente.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise KpiExprError("só são permitidas constantes numéricas")
        return
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise KpiExprError(
                f"variável desconhecida: {node.id} "
                f"(disponíveis: {', '.join(sorted(variables))})")
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise KpiExprError(
                "função não permitida — só min, max, round e abs")
        if node.keywords:
            raise KpiExprError("argumentos nomeados não são permitidos")
        if not node.args:
            raise KpiExprError(f"{node.func.id}() precisa de argumentos")
        for arg in node.args:
            _check(arg, variables, depth + 1)
        return
    # Attribute, Subscript, Compare, Lambda, comprehensions, f-strings, …
    raise KpiExprError(
        "elemento não permitido na fórmula — só aritmética simples "
        "(+ - * /), números, variáveis e min/max/round/abs")


def validate_expr(expr: str, variables: Iterable[str]) -> None:
    """Valida sintaxe + whitelist. Levanta KpiExprError (PT-PT) se inválida."""
    _check(_parse(expr), frozenset(variables), depth=0)


def _eval(node: ast.expr, values: Mapping[str, float | None]) -> float | None:
    """Avaliação recursiva. None propaga; divisão por zero → None."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        v = values.get(node.id)
        return float(v) if v is not None else None
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, values)
        if v is None:
            return None
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, values)
        right = _eval(node.right, values)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        # Div — única restante permitida pelo _check.
        if right == 0:
            return None
        return left / right
    if isinstance(node, ast.Call):
        args = [_eval(a, values) for a in node.args]
        if any(a is None for a in args):
            return None
        fn = _ALLOWED_FUNCS[node.func.id]  # type: ignore[index]
        try:
            return fn(*args)  # type: ignore[operator]
        except (TypeError, ValueError):
            return None
    # _check garante que não chegamos aqui.
    return None


def eval_expr(expr: str, values: Mapping[str, float | None]) -> float | None:
    """Valida e avalia. Devolve None em div/0 ou variável em falta/None.

    Levanta KpiExprError se a fórmula for estruturalmente inválida — o
    chamador de runtime deve ter validado antes (fallback ao default).
    """
    node = _parse(expr)
    _check(node, frozenset(values.keys()), depth=0)
    result = _eval(node, values)
    if result is None:
        return None
    return float(result)
