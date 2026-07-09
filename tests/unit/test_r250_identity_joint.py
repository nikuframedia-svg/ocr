"""R250 — Identidade por COMBINAÇÃO (variante "next").

of/ov/cliente são ~UMA variável latente ("encomenda") observada 3× —
P(OF→OV)=99,8%, H(OF|OV)=1,4 bits vs H(OF)=12,5 medidos no plano. O v30
soma os três bits (inflação 2,6× medida: OF 262593 real dava 23,7 bits
quando o valor conjunto é 9,1); a variante "next" aplica à identidade o
mesmo u conjunto por interseção que as dims têm desde R236, com m̂_A
MEDIDO (quant8) e guarda de monotonia.
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    SCORING_VARIANT,
    _entry_bits_score,
    _fs_row_context,
    _get_indices,
    select_winner,
    set_scoring_variant,
    scoring_variant,
)


@pytest.fixture()
def _variant_next():
    tok = set_scoring_variant("next")
    yield
    SCORING_VARIANT.reset(tok)


def _entry(fam: str, code: str, *, of: str = "262593", ov: str = "2601149",
           cliente: str = "TSO CATENAIRES") -> dict:
    return {
        "cliente": cliente, "ov": ov, "of": of,
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
        # Encomenda rival com identidade própria
        "254856": [
            {
                "cliente": "TECPOLES GMBH", "ov": "2507714", "of": "254856",
                "designacao": "0854UJ72 - Nº2 0854U572 1/2",
                "esp": 8.0, "lbase": 1480, "ltopo": 1118, "comp": 5419,
            },
        ],
    },
    "clientes_plan": frozenset({"TSO CATENAIRES", "TECPOLES GMBH"}),
    "lotes_sap_full": {},
}


def _bits(row: dict, entry_of: str = "262593", entry_i: int = 0) -> float:
    idx = _get_indices(_REFS)
    entry = dict(_REFS["of_to_entries"][entry_of][entry_i], _of=entry_of)
    ctx = _fs_row_context(row, idx)
    return _entry_bits_score(entry, row, _REFS, idx, ctx)


class TestJointIdentity:
    def test_ranking_bits_keep_v30_sums_for_identity(self, _variant_next):
        # O RANKING mantém as somas v30 (o backtest provou que o equilíbrio
        # R236-R243 é co-adaptado: deflacionar o ranking partia 6 GOOD em
        # OFs adjacentes). A correção vive no POSTERIOR.
        row = {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES"}
        b_next = _bits(row)
        tok = set_scoring_variant("v30")
        try:
            b_v30 = _bits(row)
        finally:
            SCORING_VARIANT.reset(tok)
        assert b_next == pytest.approx(b_v30, abs=1e-9)

    def test_posterior_uses_deflated_bits(self, _variant_next):
        # Triple exato: a inflação (Σ singles − joint) é subtraída no
        # posterior → p_h0 sobe e p_of desce vs a telemetria v30 (que usa
        # os bits crus inflados).
        row = {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES",
               "modelo": "5100T742A", "qtd": "2"}
        w_next = select_winner(dict(row), _REFS, template_name="gasparini")
        tok = set_scoring_variant("v30")
        try:
            w_v30 = select_winner(dict(row), _REFS, template_name="gasparini")
        finally:
            SCORING_VARIANT.reset(tok)
        assert w_next is not None and w_v30 is not None
        assert w_next["designacao"] == w_v30["designacao"]  # ranking igual
        assert w_next["_p_h0"] > w_v30["_p_h0"]
        assert w_next["_p_of"] < w_v30["_p_of"] + 1e-9

    def test_single_field_path_identical_to_v30(self, _variant_next):
        # Só OF escrita (sem modelo — o R251 muda esse termo na variante
        # next): |A|=1 → caminho por-campo da identidade, igual ao v30.
        row = {"of": "262593"}
        b_next = _bits(row)
        tok = set_scoring_variant("v30")
        try:
            b_v30 = _bits(row)
        finally:
            SCORING_VARIANT.reset(tok)
        assert b_next == pytest.approx(b_v30, abs=1e-9)

    def test_monotonia_more_agreement_never_scores_less(self, _variant_next):
        # Acrescentar concordância exata em OV nunca baixa os bits.
        base = {"of": "262593", "cliente": "TSO CATENAIRES", "modelo": "5100T742A"}
        more = dict(base, ov="2601149")
        assert _bits(more) >= _bits(base) - 1e-9

    def test_winner_stable_under_next(self, _variant_next):
        row = {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES",
               "modelo": "5100T742A", "qtd": "2"}
        w = select_winner(dict(row), _REFS, template_name="gasparini")
        assert w is not None
        assert "5100T742" in w["designacao"]

    def test_rival_order_between_encomendas_preserved(self, _variant_next):
        # A entry certa continua acima da encomenda rival sem identidade.
        row = {"of": "262593", "ov": "2601149", "cliente": "TSO CATENAIRES",
               "modelo": "5100T742A"}
        assert _bits(row, "262593", 0) > _bits(row, "254856", 0)

    def test_o_zero_variants_enter_id_sets(self, _variant_next):
        # OF escrita com O em vez de 0 continua a contar como exata no joint.
        row = {"of": "262593", "ov": "26O1149", "cliente": "TSO CATENAIRES"}
        idx = _get_indices(_REFS)
        ctx = _fs_row_context(row, idx)
        assert ctx["id_sets"].get("ov")  # variantes O/0 resolvem o conjunto

    def test_alias_cliente_stays_on_per_field_path(self, _variant_next):
        # Cliente escrito que NÃO existe compacto nos índices → id_sets None
        # (fica no caminho por-campo; a régua do m̂ quant8 é compacta).
        row = {"of": "262593", "cliente": "TSO"}
        idx = _get_indices(_REFS)
        ctx = _fs_row_context(row, idx)
        assert ctx["id_sets"].get("cliente") is None

    def test_v30_context_has_no_id_sets(self):
        assert scoring_variant() == "v30"
        idx = _get_indices(_REFS)
        ctx = _fs_row_context({"of": "262593", "ov": "2601149"}, idx)
        assert ctx["id_sets"] == {}
