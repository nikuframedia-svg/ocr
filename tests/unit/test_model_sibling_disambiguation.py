"""R247/R248 — Irmãos da mesma OF: o código-peça embebido decide o modelo.

Reprodutores dos casos reais da fábrica (folhas #2786/#2755/#2754 de
07-07-2026 e trocas históricas do app.db — sheets 503/854/1211/1323/1686):
entries da MESMA OF com dims idênticas cujas designações diferem 1 dígito no
código-peça ('5100TME1 - CC4H1 5100T742 1/2' … 'TME4/745'). Antes do R247 o
fuzzy contra o token-família escolhia o irmão errado pelo ÚLTIMO dígito
(742→'TME2') e pintava verde com p_top 0.93-0.99.
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    _designacao_code_tokens_cached,
    _efs_compute,
    _get_indices,
    _is_very_different,
    _model_code_cores_cached,
    _model_matches_designacao,
    _model_sibling_ambiguous,
    select_winner,
    shadow_score,
    write_confidence_threshold,
)


def _entry(fam: str, code: str) -> dict:
    # Réplica fiel do plano real: OF 262593, dims IDÊNTICAS entre irmãos.
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
            _entry("5100TME4", "5100T745"),
            _entry("5100TMF2", "5100T755"),
        ],
        "262429": [
            {
                "cliente": "TECPOLES GMBH", "ov": "2602396", "of": "262429",
                "designacao": "1234TJ11 - TSA5 16M Nº1 1234T811",
                "esp": 4.0, "lbase": 1243, "ltopo": 543, "comp": 10918,
            },
            {
                "cliente": "TECPOLES GMBH", "ov": "2602396", "of": "262429",
                "designacao": "1234TJ41 - TSATH Nº2 1234T841 1/2",
                "esp": 6.0, "lbase": 1449, "ltopo": 921, "comp": 8989,
            },
        ],
        # Caso sheet 814: o MESMO código-peça em duas entries irmãs.
        "262500": [
            {
                "cliente": "TECPOLES GMBH", "ov": "2602300", "of": "262500",
                "designacao": "TSA20 16M Nº1 1234TJ01 - 1234T800 1/2",
                "esp": 4.0, "lbase": 854, "ltopo": 335, "comp": 11809,
            },
            {
                "cliente": "TECPOLES GMBH", "ov": "2602300", "of": "262500",
                "designacao": "TSA20 18M A 26M Nº1 1234TJ02 - 1234T800 1/2",
                "esp": 4.0, "lbase": 854, "ltopo": 335, "comp": 11809,
            },
        ],
    },
    "clientes_plan": frozenset({"TSO CATENAIRES", "TECPOLES GMBH"}),
    "lotes_sap_full": {},
}


def _sheet(of: str, modelo: str, cliente: str = "TSO") -> dict:
    return {
        "template_name": "gasparini", "header": {}, "footer": {},
        "rows": [{"of": of, "cliente": cliente, "modelo": modelo, "qtd": "2"}],
    }


def _modelo_cell(of: str, modelo: str, cliente: str = "TSO") -> dict:
    scoring, *_ = shadow_score(_sheet(of, modelo, cliente), None, _REFS)
    return scoring["rows"][0]["fields"]["modelo"]


class TestEmbeddedPieceCodeWinner:
    """O código-peça escrito escolhe a entry irmã certa (Fase 1 / R247)."""

    @pytest.mark.parametrize(
        ("ocr_modelo", "expected_code"),
        [
            ("5100T742A", "5100T742"),        # sufixo A/B (folha 2786)
            ("5100T743B", "5100T743"),
            ("N° 5100.T.743", "5100T743"),    # marcador Nº (folha 2755)
            ("5100T755A", "5100T755"),        # antes ia parar ao T745
        ],
    )
    def test_sibling_shift_fixed(self, ocr_modelo, expected_code):
        w = select_winner(
            {"of": "262593", "cliente": "TSO", "modelo": ocr_modelo},
            _REFS, template_name="gasparini",
        )
        assert w is not None
        assert expected_code in w["designacao"]

    def test_glued_no_prefix_and_part_marker(self):
        # Folha 2754: compact('No→1234.T.841(-1) 1/2') = 'NO1234T841112'
        # não era substring de '…N21234T84112' → empate na zona morta →
        # desempate cego escolhia o TJ11 (T811, errado).
        w = select_winner(
            {"of": "262429", "cliente": "TECPOLES", "modelo": "No→1234.T.841(-1) 1/2"},
            _REFS, template_name="gasparini",
        )
        assert w is not None
        assert "1234T841" in w["designacao"]

    @pytest.mark.parametrize(
        ("ocr_modelo", "expected_code"),
        [
            ("5100T742A", "5100T742"),
            ("N° 5100.T.743", "5100T743"),
            ("5100T755A", "5100T755"),
        ],
    )
    def test_cell_snaps_green_on_unique_code(self, ocr_modelo, expected_code):
        cell = _modelo_cell("262593", ocr_modelo)
        assert cell["status"] == "snapped"
        assert expected_code in cell["value"]

    def test_sibling_margin_exposed(self):
        w = select_winner(
            {"of": "262593", "cliente": "TSO", "modelo": "5100T742A"},
            _REFS, template_name="gasparini",
        )
        assert w is not None
        assert 0.0 < float(w["_sibling_margin_bits"]) < 99.0
        # OF de 1 entry não tem irmãos → sentinela 99.0.
        single = {
            "available": True,
            "of_to_entries": {"262593": [_entry("5100TME1", "5100T742")]},
            "clientes_plan": frozenset({"TSO CATENAIRES"}),
            "lotes_sap_full": {},
        }
        w2 = select_winner(
            {"of": "262593", "cliente": "TSO", "modelo": "5100T742A"},
            single, template_name="gasparini",
        )
        assert w2 is not None
        assert float(w2["_sibling_margin_bits"]) == 99.0


class TestSiblingAmbiguityGuard:
    """Modelo que não discrimina irmãos fica em revisão (Fase 2 / R248)."""

    def test_family_prefix_is_ambiguous(self):
        # '5100TME' está contido em TODOS os irmãos (sim 1.0 em empate) —
        # o gate antigo (sim<1.0) deixava passar verde por desempate.
        cell = _modelo_cell("262593", "5100TME")
        assert cell["status"] == "very_different"
        assert cell["decision_reason"] == "ambiguous_sibling_designacao"
        assert cell["decision_confidence"] < write_confidence_threshold("modelo")

    def test_repeated_piece_code_is_ambiguous(self):
        # Sheet 814: '1234T800' existe em DUAS entries irmãs.
        cell = _modelo_cell("262500", "1234 T 800", cliente="TECPOLES")
        assert cell["status"] == "very_different"
        assert cell["decision_reason"] == "ambiguous_sibling_designacao"

    def test_code_not_in_plan_never_green(self):
        # Operador escreve um código que não existe em nenhum irmão.
        cell = _modelo_cell("262593", "1234T756")
        assert cell["status"] == "very_different"

    def test_identity_cells_stay_clean_on_model_ambiguity(self):
        scoring, *_ = shadow_score(_sheet("262593", "5100TME"), None, _REFS)
        fields = scoring["rows"][0]["fields"]
        assert fields["modelo"]["status"] == "very_different"
        assert fields["of"]["status"] != "very_different"
        assert fields["cliente"]["status"] != "very_different"

    def test_unique_code_is_not_ambiguous(self):
        row = {"of": "262593", "cliente": "TSO", "modelo": "5100T742A"}
        w = select_winner(row, _REFS, template_name="gasparini")
        assert w is not None
        assert not _model_sibling_ambiguous(w, row, _REFS, _get_indices(_REFS))


class TestPureHelpers:
    """Funções puras R247 (extração de cores e tokens)."""

    @pytest.mark.parametrize(
        ("raw", "expected_core"),
        [
            ("No→1234.T.841(-1) 1/2", "1234T841"),
            ("1234 T 859-1", "1234T859"),
            ("N° 5100.T.743", "5100T743"),
        ],
    )
    def test_cores_strip_decorations(self, raw, expected_core):
        pure, _ab = _model_code_cores_cached(raw)
        assert expected_core in pure

    def test_ab_suffix_goes_to_separate_tier(self):
        pure, ab = _model_code_cores_cached("5100T742A")
        assert "5100T742" in ab
        assert "5100T742" not in pure  # A/B pode ser código real → tier 0.97

    def test_plain_code_yields_no_new_cores(self):
        pure, ab = _model_code_cores_cached("CGCAE05D1")
        assert pure == () and ab == ()

    def test_designacao_tokens_require_digit_and_letter(self):
        toks = _designacao_code_tokens_cached("5100TME2 - CC4H1 5100T743 1/2")
        assert toks == ("5100TME2", "CC4H1", "5100T743")
        assert _designacao_code_tokens_cached("CLCAF06DI_V - PONTEIRA") == ("CLCAF06DI",)
        assert _designacao_code_tokens_cached("OMEGA 1500 H") == ()

    def test_core_containment_is_full_match(self):
        assert _model_matches_designacao(
            "No→1234.T.841(-1) 1/2", "1234TJ41 - TSATH Nº2 1234T841 1/2")
        assert not _model_matches_designacao(
            "No→1234.T.841(-1) 1/2", "1234TJ11 - TSA5 16M Nº1 1234T811")

    def test_efs_tiers_discriminate_siblings(self):
        # 0.97 (A/B) no irmão certo ≫ fuzzy no irmão do "dígito de sorte".
        right = _efs_compute(
            "modelo", _entry("5100TME1", "5100T742"),
            {"modelo": "5100T742A"}, {}, "5100T742A")
        lucky = _efs_compute(
            "modelo", _entry("5100TME2", "5100T743"),
            {"modelo": "5100T742A"}, {}, "5100T742A")
        assert right == pytest.approx(0.97)
        assert lucky < right - 0.05

    def test_decorated_winner_is_not_very_different(self):
        # Cor honesta: '5100T742A' vs a designação certa não é "muito
        # diferente" (antes: compacto dava 0 pelo guard len>5 → vermelho).
        assert not _is_very_different(
            "modelo", "5100T742A", "5100TME1 - CC4H1 5100T742 1/2")
        assert _is_very_different(
            "modelo", "1234T756", "5100TMF2 - CC4H1 5100T755 1/2")
