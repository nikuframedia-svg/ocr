"""R251 — LR de TOKEN no modelo (variante "next").

bits = log2( m_mod · 2^(−custo_NW(token→core)) / û(core) ), com û = taxa de
colisão real do core no plano (canal fitted R241 + pseudo-contagem α). O
caminho `sim` (_efs_compute) fica intocado — guardas R248/R249 e cores
continuam nos sims. Sentinelas: os hazards s2369/s2510/s2375 documentados
nos comentários R247 do motor.
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    SCORING_VARIANT,
    _get_indices,
    _model_lr_bits,
    _uhat_core,
    select_winner,
    set_scoring_variant,
)


def _entry(fam: str, code: str, of: str = "262593", **kw) -> dict:
    e = {
        "cliente": "TSO CATENAIRES", "ov": "2601149", "of": of,
        "designacao": f"{fam} - CC4H1 {code} 1/2",
        "esp": 12.0, "lbase": 737, "ltopo": 438, "comp": 8483,
    }
    e.update(kw)
    return e


_REFS = {
    "available": True,
    "of_to_entries": {
        "262593": [
            _entry("5100TME1", "5100T742"),
            _entry("5100TME2", "5100T743"),
            _entry("5100TME3", "5100T744"),
        ],
        # s2375 — designação REPETIDA k× no plano (u maior) vs rara:
        "261591": [
            {"cliente": "EDF", "ov": "2601866", "of": "261591",
             "designacao": "CD11MJ09 - CD11M507 + SUP DEGRAUS",
             "esp": 6.0, "lbase": 1000, "ltopo": 500, "comp": 9000},
            {"cliente": "EDF", "ov": "2601866", "of": "261591",
             "designacao": "CD13MJ05 - CD13M504 MEIOS + SUP DEGRAUS",
             "esp": 6.0, "lbase": 1000, "ltopo": 500, "comp": 9000},
        ],
        # réplicas da designação CD11MJ09 noutras OFs (freq no plano = 4)
        "261592": [dict({"cliente": "EDF", "ov": "2601867", "of": "261592",
                         "designacao": "CD11MJ09 - CD11M507 + SUP DEGRAUS",
                         "esp": 6.0, "lbase": 1000, "ltopo": 500,
                         "comp": 9000})],
        "261593": [dict({"cliente": "EDF", "ov": "2601868", "of": "261593",
                         "designacao": "CD11MJ09 - CD11M507 + SUP DEGRAUS",
                         "esp": 6.0, "lbase": 1000, "ltopo": 500,
                         "comp": 9000})],
        "261594": [dict({"cliente": "EDF", "ov": "2601869", "of": "261594",
                         "designacao": "CD11MJ09 - CD11M507 + SUP DEGRAUS",
                         "esp": 6.0, "lbase": 1000, "ltopo": 500,
                         "comp": 9000})],
    },
    "clientes_plan": frozenset({"TSO CATENAIRES", "EDF"}),
    "lotes_sap_full": {},
}


@pytest.fixture()
def _variant_next():
    tok = set_scoring_variant("next")
    yield
    SCORING_VARIANT.reset(tok)


class TestTokenLR:
    def test_exact_beats_d1_by_wide_margin(self):
        # Exato (containment) ≫ irmão a d=1 — a separação que o sim·w
        # não dava (1,3 bits) passa a >4 bits.
        idx = _get_indices(_REFS)
        right = _model_lr_bits("5100T742A", "5100TME1 - CC4H1 5100T742 1/2", idx)
        lucky = _model_lr_bits("5100T742A", "5100TME2 - CC4H1 5100T743 1/2", idx)
        assert right is not None
        assert lucky is None or right > lucky + 4.0

    def test_channel_monotonia(self):
        # Confusão plausível (4↔9 fitted; 4↔A tem piso de glifos) > par
        # genuinamente não-confundível (4↔X: default 10 bits).
        idx = _get_indices(_REFS)
        common = _model_lr_bits("5100T792A", "5100TME1 - CC4H1 5100T742 1/2", idx)
        rare = _model_lr_bits("5100T7X2A", "5100TME1 - CC4H1 5100T742 1/2", idx)
        assert common is not None
        assert rare is None or common > rare

    def test_family_prefix_ties_all_siblings(self):
        # '5100TME' contém em todos → LR igual → empate (guarda R248 pinta).
        idx = _get_indices(_REFS)
        vals = {
            round(_model_lr_bits("5100TME", e["designacao"], idx) or -99, 6)
            for e in _REFS["of_to_entries"]["262593"]
        }
        assert len(vals) == 1

    def test_uhat_grows_with_designacao_frequency(self):
        # s2375 — û do core exato de uma designação repetida 4× é maior do
        # que o de um core exato único → menos bits (raridade honesta).
        idx = _get_indices(_REFS)
        u_repeated = _uhat_core("CD11M507", idx)
        u_unique = _uhat_core("5100T742", idx)
        assert u_repeated > u_unique

    def test_s2375_exact_on_repeated_beats_d2_on_rare(self):
        # O hazard morre por construção: exato numa designação repetida
        # ainda vale muito mais do que um d>=2 numa rara.
        idx = _get_indices(_REFS)
        exact_rep = _model_lr_bits(
            "CD11.M.507", "CD11MJ09 - CD11M507 + SUP DEGRAUS", idx)
        d2_rare = _model_lr_bits(
            "CD11.M.507", "CD13MJ05 - CD13M504 MEIOS + SUP DEGRAUS", idx)
        assert exact_rep is not None
        assert d2_rare is None or exact_rep > d2_rare + 5.0

    def test_no_tokens_falls_back_to_none(self):
        # Designação sem token-código (família OMEGA) → None → ladder v30.
        idx = _get_indices(_REFS)
        assert _model_lr_bits("OMEGA 1300 H", "OMEGA 1500 H", idx) is None

    def test_s2510_weak_coincidence_stays_out(self):
        # Coincidência fraca de token ('CD24T5061' vs 'CD11M501'-like) não
        # ganha LR: fora da vizinhança do canal → None → ladder (zona morta).
        idx = _get_indices(_REFS)
        lr = _model_lr_bits("CD24T5061", "CD13MJ05 - CD13M504 MEIOS", idx)
        assert lr is None or lr < 2.0

    def test_winner_next_still_correct_and_sims_untouched(self, _variant_next):
        row = {"of": "262593", "cliente": "TSO", "modelo": "5100T743B",
               "qtd": "1"}
        w = select_winner(dict(row), _REFS, template_name="gasparini")
        assert w is not None
        assert "5100T743" in w["designacao"]

    def test_s2369_channel_never_dethrones_exact(self, _variant_next):
        # Escrito exato de um irmão: o canal (confusão barata noutra
        # família) nunca o destrona no ranking next.
        row = {"of": "262593", "cliente": "TSO", "modelo": "5100T742A",
               "qtd": "1"}
        w = select_winner(dict(row), _REFS, template_name="gasparini")
        assert w is not None
        assert "5100T742" in w["designacao"]
