"""Task C E3 — avaliador de fórmulas de KPI: corretude + SEGURANÇA.

O avaliador nunca pode executar código arbitrário — walker próprio com
whitelist, nunca eval builtin. Os testes de segurança são a parte crítica.
"""
from __future__ import annotations

import pytest

from app.web.kpi_expr import MAX_CHARS, KpiExprError, eval_expr, validate_expr

VARS = {"qtd": 12.0, "horas": 4.0, "kg": 1500.0}


class TestCorrectness:
    def test_basic_division(self):
        assert eval_expr("qtd / horas", VARS) == 3.0

    def test_precedence(self):
        assert eval_expr("horas * 60 / qtd", VARS) == 20.0

    def test_parens_and_constants(self):
        assert eval_expr("(qtd + 8) / 2", VARS) == 10.0

    def test_unary_minus(self):
        assert eval_expr("-qtd + 20", VARS) == 8.0

    def test_functions(self):
        assert eval_expr("max(qtd - 20, 0)", VARS) == 0.0
        assert eval_expr("min(qtd, horas)", VARS) == 4.0
        assert eval_expr("abs(0 - qtd)", VARS) == 12.0
        assert eval_expr("round(qtd / 7, 2)", VARS) == pytest.approx(1.71)

    def test_float_constants(self):
        assert eval_expr("kg / 1000.0", VARS) == 1.5

    def test_div_by_zero_none(self):
        assert eval_expr("qtd / (horas - 4)", VARS) is None

    def test_none_variable_propagates(self):
        assert eval_expr("qtd / horas", {"qtd": None, "horas": 4.0}) is None

    def test_missing_variable_at_eval_none(self):
        # variável válida no scope mas sem valor no dict → None, não crash
        with pytest.raises(KpiExprError):
            eval_expr("qtd / outra", VARS)

    def test_none_inside_function(self):
        assert eval_expr("max(qtd, horas)", {"qtd": None, "horas": 4.0}) is None


class TestValidation:
    def test_ok(self):
        validate_expr("qtd / horas", ["qtd", "horas"])

    def test_empty(self):
        with pytest.raises(KpiExprError, match="vazia"):
            validate_expr("   ", ["qtd"])

    def test_unknown_variable(self):
        with pytest.raises(KpiExprError, match="variável desconhecida"):
            validate_expr("qtd / horaz", ["qtd", "horas"])

    def test_syntax_error(self):
        with pytest.raises(KpiExprError, match="sintaxe"):
            validate_expr("qtd //", ["qtd"])

    def test_too_long(self):
        with pytest.raises(KpiExprError, match="longa"):
            validate_expr("qtd + " * 60 + "qtd", ["qtd"])
        assert MAX_CHARS == 200

    def test_too_many_nodes(self):
        # < 200 chars mas > 60 nós
        expr = "+".join(["1"] * 35)
        with pytest.raises(KpiExprError, match="complexa"):
            validate_expr(expr, ["qtd"])

    def test_too_deep(self):
        expr = "(" * 20 + "qtd" + ")" * 20 + " + 1" * 0
        with pytest.raises(KpiExprError):
            validate_expr("min(" * 17 + "qtd" + ", 1)" * 17, ["qtd"])
        # parênteses puros não criam nós — aninhar chamadas cria
        validate_expr(expr, ["qtd"])


class TestSecurity:
    """Tudo o que não é aritmética simples tem de ser REJEITADO."""

    @pytest.mark.parametrize("evil", [
        "__import__('os').system('rm -rf /')",
        "().__class__.__bases__[0].__subclasses__()",
        "qtd.__class__",
        "qtd.real",                      # Attribute
        "x[0]",                          # Subscript
        "lambda: 1",                     # Lambda
        "2 ** 100000",                   # Pow (DoS)
        "qtd if horas else 0",           # IfExp
        "qtd > horas",                   # Compare
        "[i for i in (1, 2)]",           # comprehension
        "{'a': 1}",                      # Dict
        "'abc'",                         # constante não-numérica
        "True",                          # bool
        "f'{qtd}'",                      # JoinedStr
        "pow(2, 10)",                    # função fora da whitelist
        "eval('1')",
        "open('/etc/passwd')",
        "round(qtd, ndigits=1)",         # keyword args
        "min()",                         # call sem args
        "qtd @ horas",                   # MatMult
        "qtd // horas",                  # FloorDiv fora da whitelist
        "qtd % horas",                   # Mod fora da whitelist
        "not qtd",                       # Not
    ])
    def test_rejected(self, evil):
        with pytest.raises(KpiExprError):
            validate_expr(evil, ["qtd", "horas", "x"])

    def test_eval_expr_also_validates(self):
        # eval_expr não pode ser um bypass da validação
        with pytest.raises(KpiExprError):
            eval_expr("__import__('os')", VARS)

    def test_huge_number_ok_but_pow_blocked(self):
        # multiplicação é permitida; potência não (DoS por bigint)
        assert eval_expr("1000000 * 1000000", {}) == 1e12
        with pytest.raises(KpiExprError):
            eval_expr("10 ** 10", {})
