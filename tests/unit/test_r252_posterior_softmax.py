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

    def test_p_top_is_p_of_under_next_with_logistic_kept(
            self, _variant_next, monkeypatch):
        # R253.5 — p_top = leitura Platt de p_of; sem fit é identidade.
        # Independente do cross_params fitted no repo (testa o MECANISMO).
        import app.pipeline.scoring_engine as se
        real = dict(se._load_cross_params())
        cal = dict(real.get("calibration") or {})
        for k in list(cal):
            if k.startswith("posterior_") or k.startswith("b_h0"):
                cal.pop(k)
        real["calibration"] = cal
        monkeypatch.setattr(se, "_load_cross_params", lambda: real)
        w = _winner("5100T742A")
        assert w["_p_top"] == w["_p_of"]
        assert "_p_top_logistic" in w

    def test_p_top_is_platt_readout_of_p_of_when_fitted(
            self, _variant_next, monkeypatch):
        import app.pipeline.scoring_engine as se
        real = dict(se._load_cross_params())
        cal = dict(real.get("calibration") or {})
        cal["posterior_platt_by_field"] = {"of": [0.5, 0.2]}
        real["calibration"] = cal
        monkeypatch.setattr(se, "_load_cross_params", lambda: real)
        w = _winner("5100T742A")
        assert w["_p_top"] == se._platt_calibrate(w["_p_of"], "of")

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

    def test_isotonic_preferred_and_monotone(self, monkeypatch):
        # R253.2 — série crua NÃO monótona + isotonic presente: _pi_h0 usa a
        # isotonic e a série por idade fica não-decrescente.
        import app.pipeline.scoring_engine as se
        params = {"quant7_ood_by_age": {"buckets": {
            "0-3": {"p_ood": 0.059, "p_ood_isotonic": 0.059},
            "4-7": {"p_ood": 0.203, "p_ood_isotonic": 0.2028},
            "8-14": {"p_ood": 0.313, "p_ood_isotonic": 0.2164},
            "15-30": {"p_ood": 0.195, "p_ood_isotonic": 0.2164},
            ">30": {"p_ood": 0.312, "p_ood_isotonic": 0.312},
        }}}
        monkeypatch.setattr(se, "_load_cross_params", lambda: params)
        series = [se._pi_h0(a) for a in (1, 5, 10, 20, 40)]
        assert series == sorted(series), f"não monótona: {series}"
        assert abs(series[2] - 0.2164) < 1e-9  # usa isotonic, não o cru 0.313

    def test_raw_fallback_without_isotonic(self, monkeypatch):
        import app.pipeline.scoring_engine as se
        params = {"quant7_ood_by_age": {"buckets": {
            "8-14": {"p_ood": 0.313},
        }}}
        monkeypatch.setattr(se, "_load_cross_params", lambda: params)
        assert abs(se._pi_h0(10) - 0.313) < 1e-9


class TestCellConfidence:
    def test_modelo_cell_gets_field_marginal_under_next(
            self, _variant_next, monkeypatch):
        # Sem Platt fitted (mecanismo puro): OF confiante, modelo incerto.
        import app.pipeline.scoring_engine as se
        real = dict(se._load_cross_params())
        cal = dict(real.get("calibration") or {})
        for k in list(cal):
            if k.startswith("posterior_") or k.startswith("b_h0"):
                cal.pop(k)
        real["calibration"] = cal
        monkeypatch.setattr(se, "_load_cross_params", lambda: real)
        sheet = {"template_name": "gasparini", "header": {}, "footer": {},
                 "rows": [dict(_ROW, modelo="5100TME")]}
        scoring, *_ = shadow_score(sheet, None, _REFS)
        fields = scoring["rows"][0]["fields"]
        conf_mod = fields["modelo"].get("decision_confidence")
        conf_of = fields["of"].get("decision_confidence")
        assert conf_mod is not None and conf_of is not None
        assert conf_mod < 0.5 < conf_of  # OF confiante, modelo incerto


class TestRowConditionalPosteriorParams:
    """R253.3/.4 — b_floor/b_H0 condicionais à linha + N explícito na H0."""

    def _params(self, monkeypatch, cal: dict):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_load_cross_params",
                            lambda: {"calibration": cal})
        return se._posterior_params

    def test_floor_less_negative_on_sparse_rows(self, monkeypatch):
        pp = self._params(monkeypatch, {})
        full = dict(_ROW, modelo="X", lote="L")   # of+ov+cliente+modelo
        sparse = {"of": "262593", "cliente": "TSO"}
        _, _, floor_full = pp(full, n_dims_written=2)
        _, _, floor_sparse = pp(sparse, n_dims_written=0)
        assert floor_sparse > floor_full
        # linha esparsa: Σ disagrees of+cliente = −1.7−1.2 = −2.9
        assert abs(floor_sparse - (-2.9)) < 1e-9

    def test_floor_full_row_consistent_with_legacy(self, monkeypatch):
        pp = self._params(monkeypatch, {})
        full = dict(_ROW, modelo="X")
        _, _, floor_full = pp(full, n_dims_written=6)
        # of+ov+cliente+modelo (−4.9) + cap dims (−5.0) = −9.9 ≈ −10 legado
        assert abs(floor_full - (-9.9)) < 1e-9

    def test_empty_row_falls_back_to_legacy_floor(self, monkeypatch):
        pp = self._params(monkeypatch, {"b_floor_bits": -12.0})
        _, _, floor_empty = pp({}, n_dims_written=0)
        assert floor_empty == -12.0

    def test_b_h0_bucket_table(self, monkeypatch):
        pp = self._params(monkeypatch, {
            "b_h0_by_n_fields": {"0-2": 4.0, "3-4": 8.0, "5-6": 11.0,
                                 "7+": 13.0},
        })
        sparse = {"of": "262593"}
        full = dict(_ROW, modelo="X")
        _, h0_sparse, _ = pp(sparse, 0)
        _, h0_full, _ = pp(full, 3)
        assert h0_sparse == 4.0 and h0_full == 13.0

    def test_n_explicit_h0_scales_with_plan_size(self, monkeypatch):
        import math
        pp = self._params(monkeypatch, {"b_h0_raw_bits": -4.35,
                                        "posterior_temperature_bits": 1.0})
        row = {"of": "262593"}
        _, h0_small, _ = pp(row, 0, n_plan=5000)
        _, h0_big, _ = pp(row, 0, n_plan=40000)
        assert abs((h0_big - h0_small) - math.log2(40000 / 5000)) < 1e-9

    def test_no_raw_keys_keeps_legacy_form(self, monkeypatch):
        pp = self._params(monkeypatch, {"s_ood_bits": 10.0})
        row = {"of": "262593"}
        _, h0_a, _ = pp(row, 0, n_plan=5000)
        _, h0_b, _ = pp(row, 0, n_plan=40000)
        assert h0_a == h0_b == 10.0  # sem fit, N não entra (byte-idêntico)


class TestV30Cal:
    """R255 — variante híbrida "v30cal": ranking BYTE-IDÊNTICO ao v30 (o
    melhor medido no backtest honesto) + leitura calibrada do posterior.
    É a candidata ao flip orientado a RESULTADOS: mesma escolha de winner,
    mais deteção de fora-do-plano (P(H0)>P(OF)) e confiança por célula."""

    @pytest.fixture()
    def _variant_v30cal(self):
        tok = set_scoring_variant("v30cal")
        yield
        SCORING_VARIANT.reset(tok)

    def _winner_as(self, variant: str, modelo: str) -> dict:
        tok = set_scoring_variant(variant)
        try:
            return _winner(modelo)
        finally:
            SCORING_VARIANT.reset(tok)

    def test_ranking_bits_identical_to_v30_with_modelo(self):
        # Com modelo escrito, a `next` muda os bits (LR R251); a v30cal NÃO.
        for modelo in ("5100T742A", "5100TME", "5100T743"):
            w_v30 = self._winner_as("v30", modelo)
            w_cal = self._winner_as("v30cal", modelo)
            assert abs(float(w_cal["_bits"]) - float(w_v30["_bits"])) < 1e-9
            assert w_cal["designacao"] == w_v30["designacao"]
            assert w_cal["_margin_bits"] == w_v30["_margin_bits"]

    def test_next_differs_from_v30_where_v30cal_does_not(self):
        # Guarda de sentido: o desacoplamento é real — a next continua a
        # usar o LR (bits diferentes), a v30cal não.
        w_v30 = self._winner_as("v30", "5100T742A")
        w_next = self._winner_as("next", "5100T742A")
        w_cal = self._winner_as("v30cal", "5100T742A")
        assert abs(float(w_next["_bits"]) - float(w_v30["_bits"])) > 0.5
        assert abs(float(w_cal["_bits"]) - float(w_v30["_bits"])) < 1e-9

    def test_calibrated_readout_active(self, _variant_v30cal):
        import app.pipeline.scoring_engine as se
        w = _winner("5100T742A")
        assert w.get("_p_of") is not None and w.get("_p_h0") is not None
        assert "_p_top_logistic" in w
        assert w["_p_top"] == se._platt_calibrate(w["_p_of"], "of")

    def test_cell_confidence_by_field(self, _variant_v30cal):
        sheet = {"template_name": "gasparini", "header": {}, "footer": {},
                 "rows": [dict(_ROW, modelo="5100TME")]}
        scoring, *_ = shadow_score(sheet, None, _REFS)
        fields = scoring["rows"][0]["fields"]
        conf_mod = fields["modelo"].get("decision_confidence")
        conf_of = fields["of"].get("decision_confidence")
        assert conf_mod is not None and conf_of is not None
        assert conf_mod < conf_of  # OF confiante, peça ambígua (3 irmãs)


class TestSiblingAwareWriteThreshold:
    """R253/F3 — o limiar de gravação sobe um tier com irmão plausível
    (<2 bits), para TODOS os campos da decisão (Sadinle/reject-option)."""

    def test_threshold_rises_under_ambiguity(self):
        from app.pipeline.scoring_engine import write_confidence_threshold
        assert write_confidence_threshold("of") == 0.95
        assert write_confidence_threshold("of", sibling_margin_bits=1.0) == 0.98
        assert write_confidence_threshold("esp", sibling_margin_bits=0.5) == 0.99
        assert write_confidence_threshold("larg_mm",
                                          sibling_margin_bits=0.0) == 0.95

    def test_threshold_unchanged_with_clear_margin(self):
        from app.pipeline.scoring_engine import write_confidence_threshold
        assert write_confidence_threshold("of", sibling_margin_bits=8.0) == 0.95
        assert write_confidence_threshold("modelo",
                                          sibling_margin_bits=99.0) == 0.95


class TestTelemetryGate:
    """R253.7 — CROSS_POSTERIOR_TELEMETRY=0 desliga o bloco do posterior em
    v30 (recuperação de runtime) sem tocar em decisões; na next corre sempre."""

    def test_v30_skips_posterior_when_off(self, monkeypatch):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_POSTERIOR_TELEMETRY", False)
        w = _winner("5100T742A")  # v30 (default)
        assert "_p_of" not in w and "_p_h0" not in w
        assert w.get("_p_top") is not None  # logística R243 intacta

    def test_next_always_runs_posterior(self, monkeypatch, _variant_next):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_POSTERIOR_TELEMETRY", False)
        w = _winner("5100T742A")
        assert w.get("_p_of") is not None and w.get("_p_h0") is not None


class TestPlattReadout:
    """R253.5/.6 — leitura calibrada nos pontos de consumo; identidade sem
    fit (guarda do estado pré-calibração)."""

    def test_identity_without_fit(self, monkeypatch):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_load_cross_params", lambda: {})
        assert se._platt_calibrate(0.73) == 0.73
        assert se._platt_calibrate(None) is None

    def test_exact_transform_when_fitted(self, monkeypatch):
        import math
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_load_cross_params", lambda: {
            "calibration": {"posterior_platt_a": 2.0,
                            "posterior_platt_b": -1.0}})
        p = 0.73
        x = 2.0 * math.log(p / (1 - p)) - 1.0
        assert abs(se._platt_calibrate(p) - 1 / (1 + math.exp(-x))) < 1e-4

    def test_field_platt_takes_precedence(self, monkeypatch):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_load_cross_params", lambda: {
            "calibration": {
                "posterior_platt_a": 1.0, "posterior_platt_b": 0.0,
                "posterior_platt_by_field": {"modelo": [1.0, -2.0]},
            }})
        # global = identidade (a=1,b=0); modelo tem shift -2 → mais baixo
        assert se._platt_calibrate(0.7, "of") == pytest.approx(0.7, abs=1e-3)
        assert se._platt_calibrate(0.7, "modelo") < 0.4

    def test_monotone(self, monkeypatch):
        import app.pipeline.scoring_engine as se
        monkeypatch.setattr(se, "_load_cross_params", lambda: {
            "calibration": {"posterior_platt_a": 0.4,
                            "posterior_platt_b": -0.8}})
        ps = [se._platt_calibrate(p) for p in (0.1, 0.3, 0.5, 0.7, 0.9, 0.99)]
        assert ps == sorted(ps)


class TestExactMarginals:
    """R253.1 — as marginais somam TODO o pool dentro da margem de 30 bits,
    não só o top-50 do traço (que subestimava p_of em pools empatados)."""

    def _refs_many_siblings(self, n: int) -> dict:
        return {
            "available": True,
            "of_to_entries": {
                "262593": [_entry(f"5100TME{i}", f"5100T{700 + i}")
                           for i in range(n)],
            },
            "clientes_plan": frozenset({"TSO CATENAIRES"}),
            "lotes_sap_full": {},
        }

    def test_p_of_counts_mass_beyond_top50(self, _variant_next):
        # 60 irmãos empatados na MESMA OF: a marginal da OF tem de somar os
        # 60 (top-50 antigo dava ~50/60 da massa do pool).
        refs = self._refs_many_siblings(60)
        row = dict(_ROW, modelo="5100TME")
        w = select_winner(row, refs, template_name="gasparini")
        assert w is not None
        assert w["_p_of"] > 0.95, (
            f"p_of={w['_p_of']} — massa além do top-50 ficou de fora")
        # cada átomo vale ~1/60 da massa da OF
        assert w["_p_entry"] < w["_p_of"] / 30

    def test_entropy_exact_grows_with_tied_pool(self, _variant_next):
        # entropia exata: 60 átomos ~uniformes => H >~ log2(60) ≈ 5.9 (o
        # lump antigo saturava ~log2(50)+resíduo). Margem folgada p/ caudas.
        refs = self._refs_many_siblings(60)
        row = dict(_ROW, modelo="5100TME")
        w = select_winner(row, refs, template_name="gasparini")
        assert w is not None
        assert w["_posterior_entropy_bits"] > 5.5
