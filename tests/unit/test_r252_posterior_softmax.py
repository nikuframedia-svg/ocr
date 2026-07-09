"""R252 — Posterior softmax + hipótese nula H0 explícita.

P(e_i|obs) ∝ 2^{b_i/T} sobre o pool + H0 com odds π_H0(idade do plano,
quant7 operacionalizado) + cauda do plano a b_floor. Confiança POR CÉLULA =
marginal sobre as entries que concordam no valor do campo. Telemetria em
AMBAS as variantes; consumidores (p_top, decision_confidence) só mudam na
variante "next".
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    SCORING_VARIANT,
    _pi_h0,
    select_winner,
    set_scoring_variant,
    shadow_score,
)


def _entry(fam: str, code: str) -> dict:
    return {
        "cliente": "TSO CATENAIRES", "ov": "2601149", "of": "262593",
        "designacao": f"{fam} - CC4H1 {code} 1/2",
        "esp": 12.0, "lbase": 737, "ltopo": 438, "comp": 8483,
    }


_REFS = {
    "available": True,
    "of_to_entries": {
        "262593": [
            _entry("5100TME1", "5100T742"),
            _entry("5100TME2", "5100T743"),
            _entry("5100TME3", "5100T744"),
        ],
    },
    "clientes_plan": frozenset({"TSO CATENAIRES"}),
    "lotes_sap_full": {},
}

_ROW = {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES",
        "qtd": "2"}


@pytest.fixture()
def _variant_next():
    tok = set_scoring_variant("next")
    yield
    SCORING_VARIANT.reset(tok)


def _winner(modelo: str, extra_bias: dict | None = None) -> dict:
    row = dict(_ROW, modelo=modelo)
    w = select_winner(row, _REFS, template_name="gasparini",
                      extra_bias=extra_bias)
    assert w is not None
    return w


class TestPosterior:
    def test_probabilities_bounded_and_consistent(self, _variant_next):
        w = _winner("5100T742A")
        for key in ("_p_entry", "_p_of", "_p_h0"):
            assert 0.0 <= w[key] <= 1.0
        # marginal da OF >= átomo da entry; soma com H0 não excede 1.
        assert w["_p_of"] >= w["_p_entry"] - 1e-9
        assert w["_p_of"] + w["_p_h0"] <= 1.0 + 1e-9
        for v in w["_p_field"].values():
            assert 0.0 <= v <= 1.0

    def test_cell_confidence_of_high_modelo_low_on_family_prefix(
            self, _variant_next):
        # A promessa central: '5100TME' contém em TODOS os irmãos — a OF é
        # certa mas a peça não. p_of alto, p_field[modelo] ~ 1/3.
        w = _winner("5100TME")
        assert w["_p_of"] > 0.9
        assert w["_p_field"]["modelo"] < 0.5

    def test_exact_code_concentrates_modelo_marginal(self, _variant_next):
        w = _winner("5100T742A")
        assert w["_p_field"]["modelo"] > 0.6

    def test_pi_h0_raises_p_h0_and_lowers_p_of(self, _variant_next):
        fresh = _winner("5100T742A", extra_bias={"plan_age_days": 1.0})
        stale = _winner("5100T742A", extra_bias={"plan_age_days": 40.0})
        assert stale["_p_h0"] > fresh["_p_h0"]
        assert stale["_p_of"] < fresh["_p_of"] + 1e-9

    def test_entropy_orders_ambiguity(self, _variant_next):
        exact = _winner("5100T742A")
        ambig = _winner("5100TME")
        assert ambig["_posterior_entropy_bits"] > exact["_posterior_entropy_bits"]

    def test_p_top_is_p_of_under_next_with_logistic_kept(self, _variant_next):
        w = _winner("5100T742A")
        assert w["_p_top"] == w["_p_of"]
        assert "_p_top_logistic" in w

    def test_v30_p_top_untouched(self):
        w = _winner("5100T742A")
        assert "_p_top_logistic" not in w
        # telemetria existe nas duas variantes
        assert "_p_of" in w and "_p_h0" in w


class TestPiH0:
    def test_buckets_and_clamps(self):
        assert 0.05 <= _pi_h0(None) <= 0.5
        assert 0.05 <= _pi_h0(0) <= 0.5
        assert 0.05 <= _pi_h0(100) <= 0.5
        assert _pi_h0(40) > _pi_h0(1)  # >30d é mais OOD do que fresco


class TestCellConfidence:
    def test_modelo_cell_gets_field_marginal_under_next(self, _variant_next):
        sheet = {"template_name": "gasparini", "header": {}, "footer": {},
                 "rows": [dict(_ROW, modelo="5100TME")]}
        scoring, *_ = shadow_score(sheet, None, _REFS)
        fields = scoring["rows"][0]["fields"]
        conf_mod = fields["modelo"].get("decision_confidence")
        conf_of = fields["of"].get("decision_confidence")
        assert conf_mod is not None and conf_of is not None
        assert conf_mod < 0.5 < conf_of  # OF confiante, modelo incerto
