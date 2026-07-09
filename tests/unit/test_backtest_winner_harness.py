"""R253/F0 — guardas estruturais do harness backtest_winner.

O viés histórico (baseline sem extra_bias, candidato com) inflava o candidato
~+1.1pp TOTAL / +4.3pp SHIFT com código byte-idêntico (provado HEAD-vs-HEAD).
Estes testes leem o AST do script e falham se o padrão regressar — não
precisam do DB/plano da fábrica, correm em CI.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "diag"
    / "backtest_winner.py"
)
_TREE = ast.parse(_SRC_PATH.read_text(encoding="utf-8"))


def _winner_of_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_winner_of"
    ]


def test_baseline_and_candidate_share_extra_bias():
    calls = _winner_of_calls(_TREE)
    # As duas invocações do loop principal (base e cand).
    main_calls = [
        c for c in calls
        if c.args and isinstance(c.args[0], ast.Name)
        and c.args[0].id in ("base", "cand")
    ]
    assert len(main_calls) == 2, (
        f"esperava exatamente 2 chamadas _winner_of(base|cand, ...), "
        f"encontrei {len(main_calls)}"
    )
    bias_exprs = []
    for call in main_calls:
        engine_arg = call.args[0]
        engine_name = engine_arg.id if isinstance(engine_arg, ast.Name) else "?"
        kw = {k.arg: k.value for k in call.keywords}
        assert "extra_bias" in kw, (
            f"_winner_of({engine_name}, ...) sem extra_bias — o viés "
            "assimétrico do harness regressou"
        )
        bias_exprs.append(ast.dump(kw["extra_bias"]))
    assert bias_exprs[0] == bias_exprs[1], (
        "extra_bias diferente entre baseline e candidato: "
        f"{bias_exprs[0]} vs {bias_exprs[1]}"
    )


def test_baseline_engine_is_pinned_to_v30():
    """_load_engine_from_ref tem de fixar a variante do baseline a v30 —
    senão um baseline pós-R250 herda CROSS_SCORING_VARIANT do processo e a
    comparação vira next-vs-next."""
    fn = next(
        node for node in ast.walk(_TREE)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_load_engine_from_ref"
    )
    pinned = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_scoring_variant"
        and any(
            isinstance(a, ast.Constant) and a.value == "v30"
            for a in node.args
        )
        for node in ast.walk(fn)
    )
    assert pinned, (
        "_load_engine_from_ref não fixa set_scoring_variant('v30') no "
        "módulo baseline"
    )


def test_model_reliability_uses_p_field_modelo():
    """A reliability do MODELO tem de amostrar _p_field['modelo'] (o que o
    gate consome na variante next), não _p_top (marginal da OF)."""
    appends = [
        node for node in ast.walk(_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "model_rel_samples"
    ]
    assert appends, "model_rel_samples.append desapareceu do harness"
    # A variável amostrada tem de vir de uma expressão que lê _p_field.
    src = _SRC_PATH.read_text(encoding="utf-8")
    assert '.get("_p_field")' in src, (
        "o harness deixou de ler _p_field — a reliability de modelo voltou "
        "a medir _p_top (marginal da OF) em vez da confiança da célula"
    )
