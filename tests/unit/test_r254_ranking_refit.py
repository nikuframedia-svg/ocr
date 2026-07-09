"""R254 — ranking HONESTO da identidade (variante "next2").

O teste decisivo é o padrão que partiu a experiência R250 no ranking:
OFs ADJACENTES (Δ=1) da mesma encomenda-mãe, com OV e cliente PARTILHADOS
(casos reais 263305/263304, 263359/263358, 263203/263202, 263185/263183).
A formulação por conjuntos (u conjunto) não os separava — a discriminação
vive no lado m do canal: P(escrita exata|certa) / P(exata|vizinha) ≈ 7 bits.

A fórmula next2:
  bits_id = log2 m̂_M + Σ_{f∈C}[log2(1−m_f) − custo_canal]
          − log2 u_inter(M) − Σ_{f∈C} log2 û_f|∩M   (+ Σ_D w_disagree)
com o kernel de colisão RESTRINGIDO à interseção (um produto independente
sobre-contaria a raridade e o vizinho ultrapassava o exato).
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    SCORING_VARIANT,
    select_winner,
    set_scoring_variant,
)


@pytest.fixture()
def _variant_next2():
    tok = set_scoring_variant("next2")
    yield
    SCORING_VARIANT.reset(tok)


def _entry(of: str, code: str) -> dict:
    return {
        "of": of, "cliente": "MTG BELUX", "ov": "2603852",
        "designacao": f"FAM - {code} 1/1",
        "esp": 10.0, "lbase": 600, "ltopo": 400, "comp": 7000,
    }


# Encomenda-mãe com 3 OFs adjacentes, OV+cliente partilhados (caso 2554).
_REFS = {
    "available": True,
    "of_to_entries": {
        "263304": [_entry("263304", "CD11M504")],
        "263305": [_entry("263305", "CD11M505")],
        "263300": [_entry("263300", "CD11M500")],
    },
    "clientes_plan": frozenset({"MTG BELUX"}),
    "lotes_sap_full": {},
}


class TestAdjacentOFs:
    def test_exact_of_beats_adjacent_sibling(self, _variant_next2):
        """GOOD inviolável: o operador escreveu a OF certa — a vizinha com
        OV+cliente partilhados NÃO pode ganhar."""
        row = {"of": "263305", "ov": "2603852", "cliente": "MTG BELUX",
               "qtd": "2"}
        w = select_winner(row, _REFS, template_name="gasparini")
        assert w is not None
        assert str(w.get("_of") or w.get("of")) == "263305", (
            f"vizinha adjacente destronou a OF exata: {w.get('_of')}")

    def test_margin_to_adjacent_is_decisive(self, _variant_next2):
        """A separação exato-vs-adjacente tem de manter a ordem de grandeza
        do canal (~7-10 bits) — era isto que a deflação R250 perdia."""
        row = {"of": "263305", "ov": "2603852", "cliente": "MTG BELUX",
               "qtd": "2"}
        w = select_winner(row, _REFS, template_name="gasparini")
        assert w is not None
        assert float(w.get("_margin_bits") or 0.0) >= 5.0, (
            f"margem para a vizinha ficou marginal: {w.get('_margin_bits')}")

    def test_each_adjacent_of_wins_its_own_row(self, _variant_next2):
        for of in ("263304", "263305", "263300"):
            row = {"of": of, "ov": "2603852", "cliente": "MTG BELUX"}
            w = select_winner(row, _REFS, template_name="gasparini")
            assert w is not None
            assert str(w.get("_of") or w.get("of")) == of

    def test_misread_of_recovers_via_channel(self, _variant_next2):
        """OF escrita com 1 misread plausível (263705, 3↔7 barato no canal
        fitted 7>3=6.70): o canal tem de recuperar a família certa, não
        morrer no disagree."""
        row = {"of": "263705", "ov": "2603852", "cliente": "MTG BELUX"}
        w = select_winner(row, _REFS, template_name="gasparini")
        assert w is not None
        assert str(w.get("_of") or w.get("of")) in (
            "263305", "263304", "263300")

    def test_v30_and_next_unchanged_by_next2_code(self):
        """Equivalência: sem a variante next2, o ranking é byte-idêntico
        (as somas v30) — o caminho novo não pode vazar."""
        row = {"of": "263305", "ov": "2603852", "cliente": "MTG BELUX"}
        w_v30 = select_winner(row, _REFS, template_name="gasparini")
        tok = set_scoring_variant("next")
        try:
            w_next = select_winner(row, _REFS, template_name="gasparini")
        finally:
            SCORING_VARIANT.reset(tok)
        assert w_v30 is not None and w_next is not None
        assert abs(float(w_v30["_bits"]) - float(w_next["_bits"])) < 1e-9
        assert str(w_v30.get("_of")) == str(w_next.get("_of")) == "263305"
