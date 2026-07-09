"""R253/F2 — invariantes do posterior (fuzz com seed fixa, sem hypothesis).

Propriedades que qualquer versão do posterior tem de manter — inclui o
teste de invariância à ORDEM do pool com >50 entries empatadas, desenhado
para DESCOBRIR instabilidade (não para a assumir ausente).
"""
from __future__ import annotations

import random

import pytest

from app.pipeline.scoring_engine import (
    SCORING_VARIANT,
    select_winner,
    set_scoring_variant,
)

_CLIENTES = ("TSO CATENAIRES", "PETITJEAN", "ECOLIGHT", "MTG BELUX")


@pytest.fixture()
def _variant_next():
    tok = set_scoring_variant("next")
    yield
    SCORING_VARIANT.reset(tok)


def _entry(of: str, cliente: str, ov: str, code: str) -> dict:
    return {
        "of": of, "cliente": cliente, "ov": ov,
        "designacao": f"FAM{code[:2]} - {code} 1/2",
        "esp": 8.0, "lbase": 500, "ltopo": 300, "comp": 6000,
    }


def _random_refs(rng: random.Random, n_ofs: int) -> dict:
    of_to_entries: dict[str, list[dict]] = {}
    for k in range(n_ofs):
        of = str(260000 + rng.randrange(9000)).zfill(6)
        cliente = rng.choice(_CLIENTES)
        ov = str(2600000 + rng.randrange(90000))
        n_sib = rng.randrange(1, 4)
        of_to_entries.setdefault(of, []).extend(
            _entry(of, cliente, ov, f"CD{rng.randrange(10, 99)}T{500 + s}")
            for s in range(n_sib)
        )
    return {"available": True, "of_to_entries": of_to_entries,
            "clientes_plan": frozenset(_CLIENTES), "lotes_sap_full": {}}


def _row_from(refs: dict, rng: random.Random) -> dict:
    of = rng.choice(list(refs["of_to_entries"]))
    e = refs["of_to_entries"][of][0]
    return {"of": of, "ov": e["ov"], "cliente": e["cliente"], "qtd": "1"}


class TestInvariants:
    def test_probability_axioms_over_random_pools(self, _variant_next):
        rng = random.Random(42)
        for trial in range(30):
            refs = _random_refs(rng, rng.randrange(1, 40))
            row = _row_from(refs, rng)
            w = select_winner(row, refs, template_name="gasparini")
            if w is None:
                continue
            assert 0.0 <= w["_p_entry"] <= 1.0
            assert 0.0 <= w["_p_of"] <= 1.0
            assert 0.0 <= w["_p_h0"] <= 1.0
            assert w["_p_of"] >= w["_p_entry"] - 1e-9
            assert w["_p_of"] + w["_p_h0"] <= 1.0 + 1e-9
            for v in (w.get("_p_field") or {}).values():
                assert 0.0 <= v <= 1.0
            assert w["_posterior_entropy_bits"] >= 0.0

    def test_monotone_in_bits_via_extra_bias(self, _variant_next):
        rng = random.Random(7)
        refs = _random_refs(rng, 25)
        row = _row_from(refs, rng)
        w0 = select_winner(row, refs, template_name="gasparini")
        assert w0 is not None
        of_key = str(w0.get("_of") or w0.get("of"))
        prev = w0["_p_of"]
        for delta in (1.0, 2.0, 4.0, 8.0):
            w = select_winner(row, refs, template_name="gasparini",
                              extra_bias={"of": {of_key: delta}})
            assert w is not None
            assert w["_p_of"] >= prev - 1e-6, (
                f"+{delta} bits ao winner DESCEU p_of: {prev} → {w['_p_of']}")
            prev = w["_p_of"]

    def test_pi_h0_age_monotone_in_posterior(self, _variant_next):
        rng = random.Random(11)
        refs = _random_refs(rng, 10)
        row = _row_from(refs, rng)
        prev_h0 = -1.0
        for age in (0, 1, 3, 7, 14, 30, 60, 120):
            w = select_winner(row, refs, template_name="gasparini",
                              extra_bias={"plan_age_days": float(age)})
            assert w is not None
            assert w["_p_h0"] >= prev_h0 - 1e-6, (
                f"idade {age}d desceu p_h0 ({prev_h0} → {w['_p_h0']}) — "
                "π_H0 isotónico devia ser não-decrescente")
            prev_h0 = w["_p_h0"]

    def test_pool_order_invariance_with_ties(self, _variant_next):
        """>50 entries EMPATADAS: p_of/p_h0 não podem depender da ordem de
        inserção no dict (o desempate por ordem + top-K era o suspeito)."""
        base_entries = [
            _entry("262593", "TSO CATENAIRES", "2601149", f"CD{10 + i}T500")
            for i in range(60)
        ]
        row = {"of": "262593", "ov": "2601149",
               "cliente": "TSO CATENAIRES", "qtd": "1"}
        results = []
        for seed in range(5):
            rng = random.Random(seed)
            shuffled = list(base_entries)
            rng.shuffle(shuffled)
            refs = {"available": True,
                    "of_to_entries": {"262593": shuffled},
                    "clientes_plan": frozenset({"TSO CATENAIRES"}),
                    "lotes_sap_full": {}}
            w = select_winner(row, refs, template_name="gasparini")
            assert w is not None
            results.append((w["_p_of"], w["_p_h0"]))
        p_ofs = [r[0] for r in results]
        p_h0s = [r[1] for r in results]
        assert max(p_ofs) - min(p_ofs) < 0.01, (
            f"p_of varia com a ordem do pool: {p_ofs}")
        assert max(p_h0s) - min(p_h0s) < 0.01, (
            f"p_h0 varia com a ordem do pool: {p_h0s}")

    def test_empty_pool_returns_none(self, _variant_next):
        refs = {"available": True, "of_to_entries": {},
                "clientes_plan": frozenset(), "lotes_sap_full": {}}
        row = {"of": "262593", "qtd": "1"}
        assert select_winner(row, refs, template_name="gasparini") is None

    def test_single_entry_pool(self, _variant_next):
        refs = {"available": True,
                "of_to_entries": {"262593": [
                    _entry("262593", "TSO CATENAIRES", "2601149",
                           "CD11T500")]},
                "clientes_plan": frozenset({"TSO CATENAIRES"}),
                "lotes_sap_full": {}}
        row = {"of": "262593", "ov": "2601149",
               "cliente": "TSO CATENAIRES", "qtd": "1"}
        w = select_winner(row, refs, template_name="gasparini")
        assert w is not None
        assert w["_p_of"] > 0.5
        assert abs(w["_p_entry"] - w["_p_of"]) < 1e-6  # sem irmãos

    def test_minimal_plan_only_of_written(self, _variant_next):
        refs = {"available": True,
                "of_to_entries": {"262593": [
                    _entry("262593", "TSO CATENAIRES", "2601149",
                           "CD11T500")]},
                "clientes_plan": frozenset({"TSO CATENAIRES"}),
                "lotes_sap_full": {}}
        row = {"of": "262593"}  # só a OF escrita
        w = select_winner(row, refs, template_name="gasparini")
        assert w is not None  # não rebenta com evidência mínima
