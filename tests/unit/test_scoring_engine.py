"""R108 — Testes unitários do motor unificado de scoring.

Cobre:
- Devolução básica do `shadow_score` (shape, summary, duração)
- Campos preenchidos sem plan/SAP validam por regra local ou vão a revisão
- Geração de candidatos por campo (top-K)
- Funcionamento com refs vazias: vazios ficam NA; preenchidos ficam comparáveis
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    _candidates_for_field,
    _best_scored_entry,
    _cliente_values_match,
    ENGINE_VERSION,
    _find_winner_entry,
    _format_value,
    _get_indices,
    _is_very_different,
    _lev_distance,
    _lote_h_correction,
    _lote_variants,
    _num_sim,
    _realign_misplaced_of,
    _resolve_row_lote,
    _str_sim,
    cross_check_sheet,
    shadow_score,
)


# Refs sintéticas — pequeno plano + SAP para testes determinísticos
_REFS = {
    "available": True,
    "of_to_entries": {
        "262107": [
            {
                "ov": "2410001",
                "cliente": "ELECNOR",
                "designacao": "OMEGA 1200 H",
                "comp": 1200,
                "larg": 250,
                "lbase": 50,
                "ltopo": 30,
                "esp": 2.6,
                "material": "S355",
            },
        ],
        "262108": [
            {
                "ov": "2410002",
                "cliente": "MTG BELUX",
                "designacao": "OMEGA 1500 H",
                "comp": 1500,
                "larg": 250,
                "lbase": 60,
                "ltopo": 40,
                "esp": 3.0,
                "material": "S355",
            },
        ],
    },
    "of_to_ovs": {"262107": frozenset({"2410001"}), "262108": frozenset({"2410002"})},
    "lotes_sap_full": {
        "M26B0307": {"desc": "S355 BOBINE", "larg": 250, "esp": 2.6},
        "M26B0308": {"desc": "S355 BOBINE", "larg": 250, "esp": 3.0},
    },
    "clientes_plan": frozenset({"ELECNOR", "MTG BELUX", "TÉCNICAS REUNIDAS"}),
}



def _sans_r243(items):
    """Compara itens da fila to_analisar ignorando os campos novos do R243
    (decision_confidence, review_priority) — os testes estruturais antigos
    validam o resto do item; os campos novos têm testes próprios."""
    drop = ("decision_confidence", "review_priority")
    if isinstance(items, dict):
        return {k: v for k, v in items.items() if k not in drop}
    return [{k: v for k, v in it.items() if k not in drop} for it in items]

class TestUtilities:
    def test_lev_distance_basic(self):
        assert _lev_distance("abc", "abc") == 0
        assert _lev_distance("abc", "abd") == 1
        assert _lev_distance("kitten", "sitting") == 3
        assert _lev_distance("", "abc") == 3
        assert _lev_distance("abc", "") == 3

    def test_lev_distance_short_circuit(self):
        # length diff > 5 → returns 999 (short-circuit, not real distance)
        assert _lev_distance("a", "aaaaaaaaaa") == 999

    def test_str_sim_exact(self):
        assert _str_sim("ELECNOR", "ELECNOR") == 100.0
        assert _str_sim("elecnor", "ELECNOR") == 100.0  # case-insensitive

    def test_str_sim_substring(self):
        assert _str_sim("MTG", "MTG BELUX") == 80.0

    def test_str_sim_disjoint(self):
        # No overlap, full Lev distance
        result = _str_sim("ABCXYZ", "QQQQQQ")
        assert 0 <= result <= 20

    def test_num_sim(self):
        assert _num_sim(100, 100, 50) == 100.0  # exact
        assert _num_sim(100, 105, 50) == 100.0  # within 10% of delta
        assert _num_sim(100, 125, 50) == 50.0   # half-way
        assert _num_sim(100, 200, 50) == 0.0    # beyond delta


class TestCandidates:
    def test_of_candidates_direct_hit(self):
        row = {"of": "262107"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("of", row, _REFS, idx)
        assert len(cands) >= 1
        values = [c["value"] for c in cands]
        assert "262107" in values
        # The direct hit should have sim=100
        match = next(c for c in cands if c["value"] == "262107")
        assert match["sim"] == 100.0
        assert len(match["plan_entries"]) == 1
        assert match["plan_entries"][0]["_of"] == "262107"

    def test_of_candidates_topk_when_no_hit(self):
        row = {"of": "999999"}  # not in plan
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("of", row, _REFS, idx)
        # Top-K from the 2 available OFs + OCR raw fallback
        assert len(cands) <= 3

    def test_cliente_candidates_returns_plan_clientes(self):
        row = {"cliente": "Elecnor"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("cliente", row, _REFS, idx)
        assert any(c["value"] == "ELECNOR" for c in cands)
        # ELECNOR (exact upper) should rank highest
        assert cands[0]["value"] == "ELECNOR"
        # _REFS intentionally lacks refs["plan_by_cliente"]; the engine must
        # derive it from of_to_entries so an existing client has real entries.
        assert cands[0]["plan_entries"]
        assert cands[0]["plan_entries"][0]["_of"] == "262107"

    def test_modelo_candidates_use_first_token_with_o_zero_error(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262200": [{
                    "ov": "2410200",
                    "cliente": "ACME",
                    "designacao": "CGC2E10D - COLUNA TRONCO CONICA 10M",
                }],
            },
            "lotes_sap_full": {},
        }
        row = {"modelo": "CGC2E1OD"}  # OCR O em vez de zero
        idx = _get_indices(refs)

        cands = _candidates_for_field("modelo", row, refs, idx)

        assert cands
        assert cands[0]["plan_entries"][0]["_of"] == "262200"

    def test_lote_candidates_from_sap(self):
        row = {"lote": "M26B0307"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("lote", row, _REFS, idx)
        assert cands[0]["value"] == "M26B0307"
        assert cands[0]["sim"] == 100.0

    def test_dim_candidates_include_nearest_outside_delta(self):
        row = {"comp_mm": "1210"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("comp_mm", row, _REFS, idx)
        # R213: o campo numérico também traz o mais próximo fora da tolerância,
        # para o winner global poder escolher sempre a melhor linha possível.
        values = [c["value"] for c in cands if not c.get("is_ocr_raw")]
        assert 1200 in values
        assert 1500 in values

    def test_no_ref_field_returns_empty(self):
        row = {"qtd": "12"}
        idx = _get_indices(_REFS)
        assert _candidates_for_field("qtd", row, _REFS, idx) == []

    def test_empty_value_returns_empty(self):
        row = {"of": ""}
        idx = _get_indices(_REFS)
        assert _candidates_for_field("of", row, _REFS, idx) == []


class TestShadowScore:
    def test_basic_shape(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "AUGUSTO MONTEIRO", "data": "10-05-2026"},
            "footer": {"horas_trabalhadas": "8:00"},
            "rows": [
                {
                    "pri": "1", "cliente": "ELECNOR", "ov": "2410001",
                    "of": "262107", "modelo": "OMEGA 1200 H", "qtd": "5",
                    "comp_mm": "1200", "larg_mm": "250", "lote": "M26B0307",
                    "coni": "10", "esp": "2,6", "lbase": "50", "ltopo": "30",
                }
            ],
        }
        scoring, total, snapped, confirmed, na, dur_ms = shadow_score(
            sheet_data, None, _REFS
        )
        assert scoring["engine_version"] == ENGINE_VERSION
        assert scoring["template_name"] == "bobine_formato"
        assert "checked_at" in scoring
        assert scoring["summary"]["total"] == total
        assert total == snapped + confirmed + na
        assert dur_ms >= 0
        assert len(scoring["rows"]) == 1
        # R123 (B9) — header/footer agora validados (já não forçados a NA).
        # rev00 — `turno` passou a fazer parte do header por defeito.
        assert set(scoring["header"]) == {
            "operador", "n_operador", "setor_maquina", "cod_maquina", "data",
            "turno",
        }
        assert set(scoring["footer"]) == {"colunas_produzidas", "horas_trabalhadas"}
        _valid = {"confirmed", "snapped", "very_different", "NA"}
        for v in scoring["header"].values():
            assert v["status"] in _valid
        for v in scoring["footer"].values():
            assert v["status"] in _valid

    def test_missing_expected_header_footer_fields_are_rule_empty(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)

        cod_maquina = scoring["header"]["cod_maquina"]
        colunas = scoring["footer"]["colunas_produzidas"]
        assert cod_maquina["status"] == "confirmed"
        assert cod_maquina["match_kind"] == "MATCH_REGRA_VAZIO"
        assert colunas["status"] == "confirmed"
        assert colunas["match_kind"] == "MATCH_REGRA_VAZIO"

    def test_perfect_row_all_confirmed(self):
        """Linha que bate perfeitamente com plan: tudo confirmed, nada snapped."""
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [
                {
                    "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                    "modelo": "OMEGA 1200 H", "comp_mm": "1200",
                    "larg_mm": "250", "lbase": "50", "ltopo": "30",
                    "esp": "2,6", "lote": "M26B0307",
                }
            ],
        }
        scoring, total, snapped, confirmed, na, _ = shadow_score(sheet_data, None, _REFS)
        # Esperamos: a maior parte dos campos confirmed (motor escolheu ==
        # OCR raw). Aceitar pequenas snaps (e.g., esp "2,6" -> "2.6").
        assert confirmed >= 5

    def test_esp_missing_decimal_candidate_is_recovered(self):
        idx = _get_indices(_REFS)

        cands = _candidates_for_field("esp", {"esp": "26"}, _REFS, idx)

        assert cands
        assert cands[0]["value"] == 2.6
        assert cands[0]["sim"] == 100.0

    def test_esp_missing_decimal_is_soft_snap_not_review(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 1200 H", "esp": "26",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        esp = scoring["rows"][0]["fields"]["esp"]

        assert esp["value"] == "2,6"
        assert esp["status"] == "snapped"
        assert esp["source"] == "plan"

    def test_unrelated_esp_digits_fills_plan_value_but_stays_review(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 1200 H", "esp": "66",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        esp = scoring["rows"][0]["fields"]["esp"]

        assert esp["value"] == "2,6"
        assert esp["status"] == "very_different"
        assert esp["source"] == "plan"

    def test_esp_uses_stocksap_when_lote_has_reference(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [{
                    "ov": "2410001",
                    "cliente": "ELECNOR",
                    "designacao": "OMEGA 1200 H",
                    # Sem esp no plan: SAP deve continuar a validar a célula.
                }],
            },
            "lotes_sap_full": {
                "M26B0307": {"larg": 250, "esp": 2.6},
            },
            "clientes_plan": frozenset({"ELECNOR"}),
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "of": "262107",
                "lote": "M26B0307",
                "esp": "4,8",
            }],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        esp = result["rows"][0]["fields"]["esp"]

        assert esp["value"] == "2,6"
        assert esp["status"] == "NO_MATCH"
        assert esp["ref"] == "2,6"
        assert esp["ref_source"] == "sap"
        assert {
            "section": "rows",
            "row_index": 0,
            "field": "esp",
            "field_path": "rows[0].esp",
            "value": "4,8",
            "ref": "2,6",
            "ref_source": "sap",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(result["to_analisar"])

    def test_esp_prefers_stocksap_over_plan_when_lote_present(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [{
                    "ov": "2410001",
                    "cliente": "ELECNOR",
                    "designacao": "OMEGA 1200 H",
                    "esp": 4.8,
                }],
            },
            "lotes_sap_full": {
                "M26B0307": {"larg": 250, "esp": 2.6},
            },
            "clientes_plan": frozenset({"ELECNOR"}),
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "of": "262107",
                "lote": "M26B0307",
                "esp": "2,6",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        esp = scoring["rows"][0]["fields"]["esp"]

        assert esp["value"] == "2,6"
        assert esp["status"] == "confirmed"
        assert esp["source"] == "sap"

    def test_blank_esp_with_winner_stays_green_empty_rule(self):
        # R223 — `lote` (LOTEX) é só um lote SAP, não uma OF do plano, por isso
        # sozinho não dá winner. A linha leva a OF real (111111) para haver
        # winner; com winner, o `esp` em branco fica verde por regra de vazio.
        refs = {
            "available": True,
            "of_to_entries": {
                "111111": [{
                    "ov": "9100001",
                    "cliente": "ACME",
                    "designacao": "AAA",
                    "comp": 1000,
                }],
            },
            "lotes_sap_full": {"LOTEX": {"larg": 250, "esp": 2.6}},
            "clientes_plan": frozenset({"ACME"}),
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "111111", "lote": "LOTEX", "esp": ""}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        esp = scoring["rows"][0]["fields"]["esp"]

        assert scoring["rows"][0]["winner_of"] == "111111"
        # OF exata é identidade real → winner forte (já não "forced_top1").
        assert scoring["rows"][0]["winner_mode"] == "strong"
        assert esp["value"] == ""
        assert esp["status"] == "confirmed"
        assert esp["empty_ok"] is True
        assert esp["match_kind"] == "MATCH_REGRA_VAZIO"

    @pytest.mark.parametrize(
        ("row", "field", "ref_value", "ref_source"),
        [
            ({"of": "262107", "comp_mm": "ABC"}, "comp_mm", "1200", "plan"),
            ({"of": "262107", "lbase": "ABC"}, "lbase", "50", "plan"),
            ({"of": "262107", "ltopo": "ABC"}, "ltopo", "30", "plan"),
            ({"lote": "M26B0307", "larg_mm": "ABC"}, "larg_mm", "250", "sap"),
            ({"lote": "M26B0307", "esp": "ABC"}, "esp", "2,6", "sap"),
        ],
    )
    def test_invalid_numeric_ocr_substitutes_reference(
        self, row, field, ref_value, ref_source
    ):
        """R217 (30/05) — substitute-everything: OCR ilegível ("ABC") num campo
        numérico é substituído pelo valor canónico do plan/SAP. Como o OCR não
        é numérico, fica `snapped` (verde/Substituído), o estado que garante o
        auto-apply, e não review-only vermelho como no R216."""
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [row],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        cell = result["rows"][0]["fields"][field]

        # Valor passa a ser o canónico (não fica "ABC") e é snapped/auto-aplicável.
        assert cell["value"] == ref_value
        assert cell["status"] == "MATCH"
        assert cell["snapped"] is True
        assert cell["ref_source"] == ref_source
        # Snapped não vai para a fila to_analisar (essa é só para NO_MATCH).
        assert not any(
            item["field_path"] == f"rows[0].{field}"
            for item in _sans_r243(result["to_analisar"])
        )

    def test_larg_mm_stocksap_over_color_tolerance_substitutes_reference(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        # R222/D7 — limiar de COR de larg = 50 (30/05). 320 vs 250 = 70 > 50.
        # R223 — já não se força verde: a divergência fica vermelha (NO_MATCH).
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"lote": "M26B0307", "larg_mm": "320"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        larg = result["rows"][0]["fields"]["larg_mm"]

        # O valor canónico do SAP é substituído, mas a cor é honesta (vermelho).
        assert larg["value"] == "250"
        assert larg["status"] == "NO_MATCH"
        assert larg["ref"] == "250"
        assert larg["ref_source"] == "sap"
        # R223 — sem MATCH_FORCADO/forced_from_status: o campo diverge do
        # canónico e fica very_different (vermelho), nunca verde-confiante.
        assert "forced_from_status" not in larg

    def test_larg_mm_stocksap_within_10mm_can_snap(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"lote": "M26B0307", "larg_mm": "260"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        larg = scoring["rows"][0]["fields"]["larg_mm"]

        assert larg["value"] == "250"
        assert larg["status"] == "snapped"
        assert larg["source"] == "sap"

    def test_larg_mm_uses_plan_when_stocksap_unavailable(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262500": [{
                    "ov": "2450000",
                    "cliente": "ACME",
                    "designacao": "OMEGA 250 H",
                    "larg": 250,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "262500", "larg_mm": "250"}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        larg = result["rows"][0]["fields"]["larg_mm"]

        assert larg["value"] == "250"
        assert larg["status"] == "MATCH"
        assert larg["ref_source"] == "plan"

    def test_larg_mm_plan_over_color_tolerance_substitutes_reference_without_sap(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262500": [{
                    "ov": "2450000",
                    "cliente": "ACME",
                    "designacao": "OMEGA 250 H",
                    "larg": 250,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        # R222/D7 — 320 vs 250 = 70 > limiar de cor 50 → very_different. R223 —
        # sem SAP, o winner do plano substitui pelo valor canónico, mas a célula
        # fica vermelha (NO_MATCH), já não verde forçado.
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "262500", "larg_mm": "320"}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        larg = result["rows"][0]["fields"]["larg_mm"]

        assert larg["value"] == "250"
        assert larg["status"] == "NO_MATCH"
        assert larg["ref_source"] == "plan"
        assert larg["ref"] == "250"
        assert "forced_from_status" not in larg

    def test_larg_mm_prefers_stocksap_over_plan_and_substitutes_when_lote_present(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262500": [{
                    "ov": "2450000",
                    "cliente": "ACME",
                    "designacao": "OMEGA 250 H",
                    "larg": 250,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {"LOTEX": {"larg": 300, "esp": 2.5}},
        }
        # R222/D7 — SAP larg 300; OCR 370 (delta 70 > limiar de cor 50) → revisão.
        # R223 — substitui pelo SAP (preferido sobre o plano) mas fica vermelho.
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "262500", "lote": "LOTEX", "larg_mm": "370"}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        larg = result["rows"][0]["fields"]["larg_mm"]

        assert larg["value"] == "300"
        assert larg["status"] == "NO_MATCH"
        assert larg["ref"] == "300"
        assert larg["ref_source"] == "sap"
        assert "forced_from_status" not in larg

    @pytest.mark.parametrize(
        ("field", "ocr_value", "ref_value"),
        [
            # R222/D7 — deltas abaixo do limiar de COR de 30/05 (comp 200,
            # lbase/ltopo 30, esp 0,5): substituem (snapped/MATCH), já não
            # vermelho como na versão apertada.
            ("comp_mm", "1251", "1200"),
            ("lbase", "61", "50"),
            ("ltopo", "41", "30"),
            ("esp", "2,7", "2,6"),
        ],
    )
    def test_plan_numeric_small_diffs_snap_within_color_tolerance(
        self, field, ocr_value, ref_value
    ):
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "262107", field: ocr_value}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        cell = result["rows"][0]["fields"][field]

        assert cell["value"] == ref_value
        assert cell["status"] == "MATCH"
        assert cell["ref_source"] == "plan"
        assert cell["ref"] == ref_value
        # já não vai para a fila de revisão
        assert all(item.get("field") != field for item in result["to_analisar"])

    def test_plan_numeric_big_diff_with_strong_winner_goes_to_review(self):
        """R222/D7 — acima do limiar de COR de 30/05, com winner forte (a
        identidade ainda bate), a célula fica vermelha (NO_MATCH/rever).

        R223 — a linha leva identidade real (of+cliente+comp) para o winner ser
        inequivocamente a 262107; com a votação holística, uma linha só com
        `of` + `esp` divergente escolheria a peça errada (ver nota SUSPEITO no
        relatório). Com a identidade certa, o esp diverge do canónico → vermelho.
        """
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "of": "262107", "cliente": "ELECNOR", "comp_mm": "1200",
                "esp": "3,3",
            }],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        esp = row["fields"]["esp"]

        assert row["winner_of"] == "262107"
        assert esp["value"] == "2,6"
        assert esp["status"] == "NO_MATCH"
        assert esp["ref_source"] == "plan"

    def test_winner_without_canonical_fills_field_from_sibling_entry(self):
        """R222/D4 — o winner (linha do plano) não tem `ov`, mas uma linha-irmã
        da mesma OF tem. Em vez de ficar em MATCH_REGRA_VAZIO, o motor vai
        buscar o `ov` coerente à irmã (olhando os outros campos da linha)."""
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            "available": True,
            "of_to_entries": {
                "300001": [
                    # winner: bate of+cliente+comp+lbase, mas sem ov
                    {"of": "300001", "ov": "", "cliente": "ACME",
                     "comp": 5000, "lbase": 100, "designacao": "OMEGA 5000 H"},
                    # irmã da mesma OF: tem o ov, mas diverge no resto
                    {"of": "300001", "ov": "2599", "cliente": "BETA",
                     "comp": 9000, "designacao": "SIGMA 9000 H"},
                ],
            },
            "of_to_ovs": {"300001": frozenset({"2599"})},
            "clientes_plan": frozenset({"ACME", "BETA"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "300001", "cliente": "ACME",
                      "comp_mm": "5000", "lbase": "100", "ov": ""}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        ov = result["rows"][0]["fields"]["ov"]
        assert ov["value"] == "2599"
        assert ov["source"] == "plan"
        assert ov["ref"] == "2599"

    def test_winner_without_canonical_and_no_sibling_stays_empty_rule(self):
        """R222/D4 — controlo: sem nenhuma linha de onde tirar o `ov`, mantém o
        comportamento anterior (MATCH_REGRA_VAZIO, sem inventar valor)."""
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            "available": True,
            "of_to_entries": {
                "300001": [
                    {"of": "300001", "ov": "", "cliente": "ACME",
                     "comp": 5000, "lbase": 100, "designacao": "OMEGA 5000 H"},
                ],
            },
            "of_to_ovs": {"300001": frozenset()},
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "300001", "cliente": "ACME",
                      "comp_mm": "5000", "lbase": "100", "ov": ""}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        ov = result["rows"][0]["fields"]["ov"]
        assert ov["value"] == ""
        assert ov.get("match_kind") == "MATCH_REGRA_VAZIO"

    @pytest.mark.parametrize(
        ("field", "ocr_value", "ref_value"),
        [
            ("comp_mm", "1250", "1200"),
            ("lbase", "60", "50"),
            ("ltopo", "40", "30"),
        ],
    )
    def test_plan_numeric_values_at_validation_tolerance_can_snap(
        self, field, ocr_value, ref_value
    ):
        result, *_ = shadow_score(
            {
                "template_name": "bobine_formato",
                "header": {},
                "footer": {},
                "rows": [{"of": "262107", field: ocr_value}],
            },
            None,
            _REFS,
        )
        cell = result["rows"][0]["fields"][field]

        if field in ("lbase", "ltopo"):
            assert cell["value"] == ref_value
            assert cell["status"] == "snapped"
            assert result["rows"][0]["winner_of"] == "262107"
            return

        assert cell["value"] == ref_value
        assert cell["status"] == "snapped"
        assert cell["source"] == "plan"

    def test_empty_refs_mark_filled_cross_fields_for_review(self):
        """Sem refs, campos preenchidos cruzáveis não ficam NA/cinza."""
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "X"},
            "footer": {},
            "rows": [
                {"cliente": "ELECNOR", "of": "262107", "lote": "M26B0307"}
            ],
        }
        empty_refs = {"available": False, "of_to_entries": {}, "lotes_sap_full": {}}
        scoring, total, snapped, confirmed, na, _ = shadow_score(
            sheet_data, None, empty_refs
        )
        # Sem refs, nenhuma célula cruzável de LINHA confirma.
        for r in scoring["rows"]:
            for cell in r["fields"].values():
                assert cell["status"] != "confirmed"
        # Campos validáveis com OCR ≠ vazio marcam very_different.
        row0 = scoring["rows"][0]["fields"]
        assert row0["cliente"]["status"] == "very_different"
        assert row0["of"]["status"] == "very_different"
        assert row0["lote"]["status"] == "very_different"
        # Campos sem OCR (ov, modelo, comp_mm, ...) ficam NA.
        assert row0["ov"]["status"] == "NA"
        # Sem ListaColaboradores, operador preenchido valida por regra local.
        assert scoring["header"]["operador"]["status"] == "confirmed"

    def test_zero_score_dimension_yields_no_winner(self):
        # R223 — uma dimensão isolada (comp 9999) que não bate com nenhuma entry
        # (score zero) já não força a "melhor peça plausível": sem campo a
        # concordar, não há winner e o campo fica vermelho para revisão.
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"comp_mm": "9999"}],
        }
        scoring, total, snapped, confirmed, na, _ = shadow_score(
            sheet_data, None, _REFS
        )

        row = scoring["rows"][0]
        comp = row["fields"]["comp_mm"]
        assert row["winner_of"] is None
        assert row["winner_mode"] is None
        assert comp["value"] == "9999"
        assert comp["status"] == "very_different"
        assert total == snapped + confirmed + na + scoring["summary"]["very_different"]

    def test_unknown_cliente_with_plan_pool_yields_no_winner(self):
        # R223 — um cliente fantasma que não bate com nenhuma entry não força a
        # melhor peça plausível: sem campo a concordar, não há winner e o
        # cliente fica vermelho (rever), preservando o valor lido pelo OCR.
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "CLIENTE FANTASMA"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)

        row = scoring["rows"][0]
        cliente = row["fields"]["cliente"]
        assert row["winner_of"] is None
        assert row["winner_mode"] is None
        assert cliente["value"] == "CLIENTE FANTASMA"
        assert cliente["status"] == "very_different"

    def test_known_cliente_alone_can_fill_row_with_may_policy(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "cliente": "ELECNOR",
                "of": "999999",
                "ov": "8888888",
                "modelo": "ZZZ",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        cliente = row["fields"]["cliente"]

        assert row["winner_of"] == "262107"
        assert row["winner_score"] >= 1
        assert cliente["status"] == "confirmed"
        assert cliente["value"] == "ELECNOR"
        assert cliente["source"] == "plan"
        # R223 — o winner substitui o OCR errado pelo valor canónico do plano,
        # mas como diverge do que o operador escreveu (999999/8888888/ZZZ) a
        # célula fica vermelha (very_different/NO_MATCH), nunca verde forçado.
        assert row["fields"]["of"]["value"] == "262107"
        assert row["fields"]["of"]["status"] == "very_different"
        assert "match_kind" not in row["fields"]["of"]
        assert row["fields"]["ov"]["value"] == "2410001"
        assert row["fields"]["ov"]["status"] == "very_different"
        assert row["fields"]["modelo"]["value"] == "OMEGA 1200 H"
        assert row["fields"]["modelo"]["status"] == "very_different"
        assert result["rows"][0]["fields"]["cliente"]["status"] == "MATCH"

    def test_cliente_without_plan_pool_goes_to_review(self):
        """Cliente preenchido sem pool de plan já não fica NA neutro."""
        refs = {
            "available": True,
            "of_to_entries": {},
            "clientes_lexicon": ["ACME"],
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "ACME"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)

        assert scoring["rows"][0]["winner_of"] is None
        assert scoring["rows"][0]["fields"]["cliente"]["status"] == "very_different"

    def test_existing_cliente_alone_fills_row_with_may_policy(self):
        """Cliente existente sozinho pode escolher a linha e preencher o resto."""
        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [{
                    "ov": "2410001",
                    "cliente": "ELECNOR",
                    "designacao": "OMEGA 1200 H",
                    "comp": 1200,
                }],
            },
            "clientes_plan": frozenset({"ELECNOR"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "ELECNOR"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        fields = scoring["rows"][0]["fields"]

        assert scoring["rows"][0]["winner_of"] == "262107"
        assert scoring["rows"][0]["winner_score"] >= 1
        assert fields["cliente"]["status"] == "confirmed"
        assert fields["cliente"]["value"] == "ELECNOR"
        assert fields["of"]["value"] == "262107"
        assert fields["ov"]["value"] == "2410001"
        assert fields["modelo"]["value"] == "OMEGA 1200 H"
        assert fields["comp_mm"]["value"] == "1200"
        for field in ("of", "ov", "modelo", "comp_mm"):
            assert fields[field]["status"] == "snapped"
            assert fields[field]["source"] == "plan"

    def test_lote_without_stocksap_pool_goes_to_review(self):
        refs = {
            **_REFS,
            "lotes_sap_full": {},
            "lotes_sap": frozenset(),
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"of": "262107", "lote": "M26B0307"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)

        assert scoring["rows"][0]["fields"]["lote"]["status"] == "very_different"

    def test_header_data_validates_date_shape(self):
        bad_sheet = {
            "template_name": "bobine_formato",
            "header": {"data": "99-99-2026"},
            "footer": {},
            "rows": [],
        }
        good_sheet = {
            "template_name": "bobine_formato",
            "header": {"data": "10/05/2026"},
            "footer": {},
            "rows": [],
        }

        bad, *_ = shadow_score(bad_sheet, None, _REFS)
        good, *_ = shadow_score(good_sheet, None, _REFS)

        assert bad["header"]["data"]["status"] == "very_different"
        assert good["header"]["data"]["status"] == "confirmed"

    def test_header_footer_without_refs_validate_by_syntax(self):
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {
                "operador": "X",
                "n_operador": "0537",
                "setor_maquina": "BOBINE-FORMATO",
                "cod_maquina": "M032",
                "data": "10-05-2026",
            },
            "footer": {
                "colunas_produzidas": "10",
                "horas_trabalhadas": "8:00",
            },
            "rows": [],
        }
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}}

        scoring, *_ = shadow_score(sheet_data, None, refs)

        assert scoring["header"]["operador"]["status"] == "confirmed"
        assert scoring["header"]["n_operador"]["status"] == "confirmed"
        assert scoring["header"]["setor_maquina"]["status"] == "confirmed"
        assert scoring["header"]["cod_maquina"]["status"] == "confirmed"
        assert scoring["header"]["data"]["status"] == "confirmed"
        assert scoring["footer"]["colunas_produzidas"]["status"] == "confirmed"
        assert scoring["footer"]["horas_trabalhadas"]["status"] == "confirmed"

    def test_header_n_operador_invalid_syntax_without_colaboradores_enters_review(self):
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}, "colaboradores": {}}
        for value in ("ABC", "53A", "123456"):
            result = cross_check_sheet(
                {
                    "template_name": "bobine_formato",
                    "header": {"n_operador": value},
                    "footer": {},
                    "rows": [],
                },
                None,
                refs,
            )
            cell = result["header"]["n_operador"]

            assert cell["value"] == value
            assert cell["status"] == "NO_MATCH"
            assert cell["ref_source"] == "syntax"
            assert {
                "section": "header",
                "row_index": None,
                "field": "n_operador",
                "field_path": "header.n_operador",
                "value": value,
                "ref": "",
                "ref_source": "syntax",
                "reason": "Valor inválido para o formato esperado",
            } in _sans_r243(result["to_analisar"])

    def test_header_n_operador_valid_syntax_without_colaboradores_confirms(self):
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}, "colaboradores": {}}
        for value in ("537", "0537", "00000"):
            scoring, *_ = shadow_score(
                {
                    "template_name": "bobine_formato",
                    "header": {"n_operador": value},
                    "footer": {},
                    "rows": [],
                },
                None,
                refs,
            )
            assert scoring["header"]["n_operador"]["status"] == "confirmed"

    def test_header_cod_maquina_invalid_syntax_without_maquinas_enters_review(self):
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}}
        for value in ("ABC", "032", "M32", "M0000", "M0A2"):
            result = cross_check_sheet(
                {
                    "template_name": "bobine_formato",
                    "header": {"cod_maquina": value},
                    "footer": {},
                    "rows": [],
                },
                None,
                refs,
            )
            cell = result["header"]["cod_maquina"]

            assert cell["value"] == value
            assert cell["status"] == "NO_MATCH"
            assert cell["ref_source"] == "syntax"
            assert {
                "section": "header",
                "row_index": None,
                "field": "cod_maquina",
                "field_path": "header.cod_maquina",
                "value": value,
                "ref": "",
                "ref_source": "syntax",
                "reason": "Valor inválido para o formato esperado",
            } in _sans_r243(result["to_analisar"])

    def test_header_cod_maquina_valid_syntax_without_maquinas_confirms(self):
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}}
        for value in ("M032", "m032"):
            scoring, *_ = shadow_score(
                {
                    "template_name": "bobine_formato",
                    "header": {"cod_maquina": value},
                    "footer": {},
                    "rows": [],
                },
                None,
                refs,
            )
            assert scoring["header"]["cod_maquina"]["status"] == "confirmed"

    def test_invalid_footer_values_enter_review_queue(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {
                "colunas_produzidas": "dez",
                "horas_trabalhadas": "8:99",
            },
            "rows": [],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)

        assert result["footer"]["colunas_produzidas"]["status"] == "NO_MATCH"
        assert result["footer"]["horas_trabalhadas"]["status"] == "NO_MATCH"
        assert "ref" not in result["footer"]["colunas_produzidas"]
        assert "ref" not in result["footer"]["horas_trabalhadas"]
        assert {item["field_path"] for item in _sans_r243(result["to_analisar"])} == {
            "footer.colunas_produzidas",
            "footer.horas_trabalhadas",
        }
        assert {item["ref"] for item in _sans_r243(result["to_analisar"])} == {""}
        assert {item["ref_source"] for item in _sans_r243(result["to_analisar"])} == {"syntax"}
        assert {item["reason"] for item in _sans_r243(result["to_analisar"])} == {
            "Valor inválido para o formato esperado",
        }

    @pytest.mark.parametrize(
        ("value", "expected_status"),
        [
            ("10", "MATCH"),
            ("10,0", "MATCH"),
            ("0", "MATCH"),
            ("10.5", "NO_MATCH"),
            ("10,5", "NO_MATCH"),
        ],
    )
    def test_footer_colunas_produzidas_must_be_integer(self, value, expected_status):
        result = cross_check_sheet(
            {
                "template_name": "bobine_formato",
                "header": {},
                "footer": {"colunas_produzidas": value},
                "rows": [],
            },
            None,
            _REFS,
        )

        assert result["footer"]["colunas_produzidas"]["status"] == expected_status
        if expected_status == "NO_MATCH":
            assert {
                "section": "footer",
                "row_index": None,
                "field": "colunas_produzidas",
                "field_path": "footer.colunas_produzidas",
                "value": value,
                "ref": "",
                "ref_source": "syntax",
                "reason": "Valor inválido para o formato esperado",
            } in _sans_r243(result["to_analisar"])

    @pytest.mark.parametrize(
        ("value", "expected_status"),
        [
            ("24:00", "MATCH"),
            ("24:01", "NO_MATCH"),
            ("24:59", "NO_MATCH"),
            ("8:30", "MATCH"),
            ("8h", "MATCH"),
            ("8 h", "MATCH"),
            ("8H", "MATCH"),
            ("8:30h", "MATCH"),
            ("8 30h", "MATCH"),
            ("830", "MATCH"),
            ("0830", "MATCH"),
            ("24h", "MATCH"),
            ("24 h", "MATCH"),
            ("24:00h", "MATCH"),
            ("24:01h", "NO_MATCH"),
            ("2460", "NO_MATCH"),
            ("8,5", "MATCH"),
        ],
    )
    def test_footer_hours_caps_hh_mm_at_24h(self, value, expected_status):
        result = cross_check_sheet(
            {
                "template_name": "bobine_formato",
                "header": {},
                "footer": {"horas_trabalhadas": value},
                "rows": [],
            },
            None,
            _REFS,
        )

        assert result["footer"]["horas_trabalhadas"]["status"] == expected_status
        if expected_status == "NO_MATCH":
            assert {
                "section": "footer",
                "row_index": None,
                "field": "horas_trabalhadas",
                "field_path": "footer.horas_trabalhadas",
                "value": value,
                "ref": "",
                "ref_source": "syntax",
                "reason": "Valor inválido para o formato esperado",
            } in _sans_r243(result["to_analisar"])

    def test_header_n_operador_validates_against_colaboradores(self):
        refs = {
            **_REFS,
            "colaboradores": {
                537: {"sname": "JULIO LIMA", "pernr": "10000537"},
            },
        }
        good_sheet = {
            "template_name": "bobine_formato",
            "header": {"n_operador": "0537"},
            "footer": {},
            "rows": [],
        }
        bad_sheet = {
            "template_name": "bobine_formato",
            "header": {"n_operador": "9999"},
            "footer": {},
            "rows": [],
        }

        good, *_ = shadow_score(good_sheet, None, refs)
        bad, *_ = shadow_score(bad_sheet, None, refs)

        assert good["header"]["n_operador"]["status"] == "confirmed"
        assert bad["header"]["n_operador"]["status"] == "very_different"

    def test_header_operador_and_code_must_belong_to_same_colaborador(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            **_REFS,
            "colaboradores": {
                95: {"sname": "AUGUSTO MONTEIRO", "pernr": "10000095"},
                537: {"sname": "JULIO LIMA", "pernr": "10000537"},
            },
        }
        good_sheet = {
            "template_name": "bobine_formato",
            "header": {"operador": "JULIO LIMA", "n_operador": "537"},
            "footer": {},
            "rows": [],
        }
        bad_sheet = {
            "template_name": "bobine_formato",
            "header": {"operador": "JULIO LIMA", "n_operador": "95"},
            "footer": {},
            "rows": [],
        }

        good = cross_check_sheet(good_sheet, None, refs)
        bad = cross_check_sheet(bad_sheet, None, refs)

        assert good["header"]["operador"]["status"] == "MATCH"
        assert good["header"]["n_operador"]["status"] == "MATCH"
        assert bad["header"]["operador"]["status"] == "NO_MATCH"
        assert bad["header"]["operador"]["ref"] == "AUGUSTO MONTEIRO"
        assert bad["header"]["n_operador"]["status"] == "NO_MATCH"
        assert bad["header"]["n_operador"]["ref"] == "537"
        assert {
            "section": "header",
            "row_index": None,
            "field": "operador",
            "field_path": "header.operador",
            "value": "JULIO LIMA",
            "ref": "AUGUSTO MONTEIRO",
            "ref_source": "colaboradores",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(bad["to_analisar"])
        assert {
            "section": "header",
            "row_index": None,
            "field": "n_operador",
            "field_path": "header.n_operador",
            "value": "95",
            "ref": "537",
            "ref_source": "colaboradores",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(bad["to_analisar"])

    def test_header_operador_alias_matches_colaborador_identity(self):
        refs = {
            **_REFS,
            "colaboradores": {
                95: {"sname": "JOSE MONTEIRO", "pernr": "10000095"},
                537: {"sname": "MANUEL LIMA", "pernr": "10000537"},
            },
            "operador_aliases": {
                "AUGUSTO MONTEIRO": {
                    "cod": 95,
                    "pernr": "10000095",
                    "sname": "JOSE MONTEIRO",
                },
                "JULIO LIMA": {
                    "cod": 537,
                    "pernr": "10000537",
                    "sname": "MANUEL LIMA",
                },
            },
        }

        augusto = cross_check_sheet(
            {
                "template_name": "bobine_formato",
                "header": {"operador": "AUGUSTO MONTEIRO", "n_operador": "95"},
                "footer": {},
                "rows": [],
            },
            None,
            refs,
        )
        julio = cross_check_sheet(
            {
                "template_name": "bobine_formato",
                "header": {"operador": "JÚLIO LIMA", "n_operador": "537"},
                "footer": {},
                "rows": [],
            },
            None,
            refs,
        )

        assert augusto["header"]["operador"]["status"] == "MATCH"
        assert augusto["header"]["n_operador"]["status"] == "MATCH"
        assert julio["header"]["operador"]["status"] == "MATCH"
        assert julio["header"]["n_operador"]["status"] == "MATCH"
        assert augusto["to_analisar"] == []
        assert julio["to_analisar"] == []

    def test_header_pernr_must_match_colaborador_identity(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            **_REFS,
            "colaboradores": {
                95: {"sname": "AUGUSTO MONTEIRO", "pernr": "10000095"},
                537: {"sname": "JULIO LIMA", "pernr": "10000537"},
            },
        }
        good_sheet = {
            "template_name": "bobine_formato",
            "header": {
                "operador": "JULIO LIMA",
                "n_operador": "537",
                "pernr": "10000537",
            },
            "footer": {},
            "rows": [],
        }
        wrong_pernr_sheet = {
            "template_name": "bobine_formato",
            "header": {
                "operador": "JULIO LIMA",
                "n_operador": "537",
                "pernr": "10000095",
            },
            "footer": {},
            "rows": [],
        }
        code_pernr_conflict_sheet = {
            "template_name": "bobine_formato",
            "header": {"n_operador": "95", "pernr": "10000537"},
            "footer": {},
            "rows": [],
        }

        good = cross_check_sheet(good_sheet, None, refs)
        wrong_pernr = cross_check_sheet(wrong_pernr_sheet, None, refs)
        conflict = cross_check_sheet(code_pernr_conflict_sheet, None, refs)

        assert good["header"]["pernr"]["status"] == "MATCH"
        assert wrong_pernr["header"]["operador"]["status"] == "MATCH"
        assert wrong_pernr["header"]["n_operador"]["status"] == "MATCH"
        assert wrong_pernr["header"]["pernr"]["status"] == "NO_MATCH"
        assert wrong_pernr["header"]["pernr"]["ref"] == "10000537"
        assert {
            "section": "header",
            "row_index": None,
            "field": "pernr",
            "field_path": "header.pernr",
            "value": "10000095",
            "ref": "10000537",
            "ref_source": "colaboradores",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(wrong_pernr["to_analisar"])

        assert conflict["header"]["n_operador"]["status"] == "NO_MATCH"
        assert conflict["header"]["n_operador"]["ref"] == "537"
        assert conflict["header"]["pernr"]["status"] == "NO_MATCH"
        assert conflict["header"]["pernr"]["ref"] == "10000095"

    def test_header_setor_maquina_validates_against_maquinas(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            **_REFS,
            "maquinas_by_kanban": {
                "BOBINE-FORMATO": {"codmaq": "M032", "colunaexcel": "bf"},
            },
            "maquinas_by_codmaq": {
                "M032": {"codmaq": "M032", "colunaexcel": "bf"},
            },
        }
        good_sheet = {
            "template_name": "bobine_formato",
            "header": {"setor_maquina": "BOBINE-FORMATO"},
            "footer": {},
            "rows": [],
        }
        bad_sheet = {
            "template_name": "bobine_formato",
            "header": {"setor_maquina": "MAQUINA INEXISTENTE"},
            "footer": {},
            "rows": [],
        }

        good, *_ = shadow_score(good_sheet, None, refs)
        bad, *_ = shadow_score(bad_sheet, None, refs)
        bad_legacy = cross_check_sheet(bad_sheet, None, refs)

        assert good["header"]["setor_maquina"]["status"] == "confirmed"
        assert bad["header"]["setor_maquina"]["status"] == "very_different"
        assert {
            "section": "header",
            "row_index": None,
            "field": "setor_maquina",
            "field_path": "header.setor_maquina",
            "value": "MAQUINA INEXISTENTE",
            "ref": "",
            "ref_source": "maquinas",
            "reason": "Valor não encontrado no catálogo de máquinas",
        } in _sans_r243(bad_legacy["to_analisar"])

    def test_header_cod_maquina_must_match_resolved_setor(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            **_REFS,
            "maquinas_by_kanban": {
                "BOBINE-FORMATO": {"codmaq": "M032", "colunaexcel": "bf"},
                "ACABAMENTO MTG4": {"codmaq": "M061", "colunaexcel": "a"},
            },
            "maquinas_by_codmaq": {
                "M032": {"codmaq": "M032", "colunaexcel": "bf"},
                "M061": {"codmaq": "M061", "colunaexcel": "a"},
            },
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"setor_maquina": "BOBINE-FORMATO", "cod_maquina": "M061"},
            "footer": {},
            "rows": [],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        result = cross_check_sheet(sheet_data, None, refs)

        cell = scoring["header"]["cod_maquina"]
        assert cell["status"] == "very_different"
        assert cell["proposed"] == "M032"
        assert result["header"]["cod_maquina"]["ref"] == "M032"
        assert {
            "section": "header",
            "row_index": None,
            "field": "cod_maquina",
            "field_path": "header.cod_maquina",
            "value": "M061",
            "ref": "M032",
            "ref_source": "maquinas",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(result["to_analisar"])

    def test_no_ref_fields_validate_by_local_rule(self):
        """PRI/QTD preenchidos ficam comparáveis por regra local."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "pri": "F", "cliente": "ELECNOR", "of": "262107",
                "qtd": "5", "coni": "OCT",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        # pri/qtd: MATCH por regra local + valor OCR preservado
        for field in ("pri", "qtd"):
            assert row["fields"][field]["status"] == "confirmed"
            assert row["fields"][field]["source"] == "syntax"
            assert row["fields"][field]["match_kind"] == "MATCH_REGRA"
        assert row["fields"]["coni"]["status"] == "confirmed"
        assert row["fields"]["coni"]["value"] == "OCT"

    def test_no_ref_rule_match_kind_is_exposed_in_legacy_cell(self):
        result = cross_check_sheet(
            {
                "template_name": "bobine_formato",
                "header": {}, "footer": {},
                "rows": [{"qtd": "5"}],
            },
            None,
            _REFS,
        )

        qtd = result["rows"][0]["fields"]["qtd"]
        assert qtd["status"] == "MATCH"
        assert qtd["match_kind"] == "MATCH_REGRA"

    def test_exact_of_does_not_override_contradicting_cliente(self):
        # R226 — uma OF exata NÃO pode mandar sozinha. Se o OCR lê a OF errada
        # (262108, que no plano é de OUTRO cliente) mas o cliente e o modelo
        # apontam claramente para 262107, a COMBINAÇÃO decide: ganha 262107
        # (ELECNOR), não 262108 (MTG BELUX). Era o bug real ENEDIS->ANIBAL PALMA:
        # um único dígito mal lido trocava a encomenda toda.
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262108", "cliente": "ELECNOR", "modelo": "OMEGA 1200 H"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        cliente = row["fields"]["cliente"]
        assert cliente["value"] == "ELECNOR"     # cliente preservado (não MTG BELUX)
        assert cliente["status"] == "MATCH"

    def test_invalid_pri_with_winner_is_forced_rule_match(self):
        for value in ("xxx", "ABC", "--", "A1234", "123456"):
            result = cross_check_sheet(
                {
                    "template_name": "bobine_formato",
                    "header": {}, "footer": {},
                    # R223 — precisa de uma OF real para haver winner (uma linha
                    # só com `pri` inválido já não força peça aleatória).
                    "rows": [{"of": "262107", "pri": value}],
                },
                None,
                _REFS,
            )
            pri = result["rows"][0]["fields"]["pri"]

            assert pri["value"] == value
            assert pri["status"] == "MATCH"
            assert pri["ref_source"] == "syntax"
            assert pri["match_kind"] == "MATCH_REGRA_FORCADO"
            assert pri["warning"] == "Valor local inválido; aceite por winner da linha."
            assert not result["to_analisar"]

    def test_plausible_pri_no_ref_field_confirms(self):
        for value in ("F", "1", "A12", "P1", "P.1", "REP C12", "rep. c12"):
            scoring, *_ = shadow_score(
                {
                    "template_name": "bobine_formato",
                    "header": {}, "footer": {},
                    "rows": [{"pri": value}],
                },
                None,
                _REFS,
            )
            assert scoring["rows"][0]["fields"]["pri"]["status"] == "confirmed"

    def test_invalid_qtd_with_winner_is_forced_rule_match(self):
        for value in ("ABC", "5,0", "5.0", "-1", "12345"):
            sheet_data = {
                "template_name": "bobine_formato", "header": {}, "footer": {},
                "rows": [{"of": "262107", "qtd": value}],
            }

            result = cross_check_sheet(sheet_data, None, _REFS)
            qtd = result["rows"][0]["fields"]["qtd"]

            assert qtd["value"] == value
            assert qtd["status"] == "MATCH"
            assert qtd["ref_source"] == "syntax"
            assert qtd["match_kind"] == "MATCH_REGRA_FORCADO"
            assert qtd["warning"] == "Valor local inválido; aceite por winner da linha."
            assert not result["to_analisar"]

    def test_plausible_qtd_no_ref_field_confirms(self):
        for value in ("0", "5", "0005", "9999"):
            scoring, *_ = shadow_score(
                {
                    "template_name": "bobine_formato",
                    "header": {}, "footer": {},
                    "rows": [{"qtd": value}],
                },
                None,
                _REFS,
            )
            assert scoring["rows"][0]["fields"]["qtd"]["status"] == "confirmed"

    def test_ferramenta_coni_alias_is_normalised(self):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "qtd": "5", "coni": "OCT."}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        cell = scoring["rows"][0]["fields"]["coni"]
        assert cell["value"] == "OCT"
        assert cell["status"] == "snapped"
        assert cell["source"] == "lexicon"

    def test_ferramenta_coni_decimal_preserves_separator(self):
        """R236 — "13,7" NUNCA pode virar "137" (erro de 10× auto-gravado em
        produção: folhas 2366/2367/2368/2517). O decimal é canónico com
        vírgula; "13.7" normaliza para "13,7" (snap cosmético)."""
        for raw, expected, status in [
            ("13,7", "13,7", "confirmed"),
            ("13.7", "13,7", "snapped"),
        ]:
            sheet_data = {
                "template_name": "bobine_formato", "header": {}, "footer": {},
                "rows": [{"of": "262107", "qtd": "5", "coni": raw}],
            }
            scoring, *_ = shadow_score(sheet_data, None, _REFS)
            cell = scoring["rows"][0]["fields"]["coni"]
            assert cell["value"] == expected, (raw, cell)
            assert cell["status"] == status, (raw, cell)

    @pytest.mark.parametrize("bad", ["T", "ABC"])
    def test_ferramenta_coni_invalid_value_with_winner_is_forced_rule_match(self, bad):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "qtd": "5", "coni": bad}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        result = cross_check_sheet(sheet_data, None, _REFS)
        cell = scoring["rows"][0]["fields"]["coni"]
        assert cell["value"] == bad
        assert cell["status"] == "confirmed"
        assert cell["source"] == "syntax"
        assert cell["ref_source"] == "ferramenta"
        assert "CONI" in cell["proposed"]
        assert "número" in cell["proposed"]
        assert cell["match_kind"] == "MATCH_REGRA_FORCADO"
        legacy = result["rows"][0]["fields"]["coni"]
        assert legacy["status"] == "MATCH"
        assert legacy["ref_source"] == "ferramenta"
        assert legacy["ref"] == cell["proposed"]
        assert legacy["match_kind"] == "MATCH_REGRA_FORCADO"
        assert legacy["warning"] == "Valor CONI fora do vocabulário; aceite por winner da linha."
        assert not result["to_analisar"]

    def test_lote_near_sap_match_goes_to_review_not_autosnap(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"lote": "M26B0309"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        result = cross_check_sheet(sheet_data, None, _REFS)
        cell = scoring["rows"][0]["fields"]["lote"]

        assert cell["value"] == "M26B0309"
        assert cell["status"] == "very_different"
        assert cell["source"] == "ocr_raw"
        assert cell["proposed"] in {"M26B0307", "M26B0308"}
        legacy = result["rows"][0]["fields"]["lote"]
        assert legacy["status"] == "NO_MATCH"
        assert legacy["engine_status"] == "very_different"
        assert legacy["ref"] in {"M26B0307", "M26B0308"}
        assert legacy["ref_source"] == "sap"
        assert result["to_analisar"][0]["field_path"] == "rows[0].lote"
        assert result["to_analisar"][0]["ref_source"] == "sap"

    def test_lote_h_prefix_variant_uses_stocksap_for_lote_larg_and_esp(self):
        # R259 — o H já não fica verde com o valor OCR: as medidas validam a
        # correção H→M e a célula é SNAPPED para o lote canónico M.
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"lote": "H26B0307", "larg_mm": "250", "esp": "2,6"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        fields = result["rows"][0]["fields"]

        assert fields["lote"]["status"] == "MATCH"
        assert fields["lote"]["value"] == "M26B0307"
        assert fields["lote"]["snapped"] is True
        assert fields["lote"]["engine_status"] == "snapped"
        assert fields["lote"]["ref"] == "M26B0307"
        assert fields["lote"]["ref_source"] == "sap"
        assert fields["larg_mm"]["status"] == "MATCH"
        assert fields["larg_mm"]["ref_source"] == "sap"
        assert fields["esp"]["status"] == "MATCH"
        assert fields["esp"]["ref_source"] == "sap"
        assert result["to_analisar"] == []

    def test_lote_unknown_in_sap_reports_sap_source(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{"lote": "ZZZZZZ"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        legacy = result["rows"][0]["fields"]["lote"]

        assert legacy["status"] == "NO_MATCH"
        assert legacy.get("ref", "") == ""
        assert legacy["ref_source"] == "sap"
        assert _sans_r243(result["to_analisar"][0]) == {
            "section": "rows",
            "row_index": 0,
            "field": "lote",
            "field_path": "rows[0].lote",
            "value": "ZZZZZZ",
            "ref": "",
            "ref_source": "sap",
            "reason": "Valor não encontrado no SAP",
        }

    def test_numeric_very_different_uses_plan_value_and_exposes_ref(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [{
                "of": "262107",
                "cliente": "ELECNOR",
                "comp_mm": "9999",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        result = cross_check_sheet(sheet_data, None, _REFS)
        cell = scoring["rows"][0]["fields"]["comp_mm"]
        legacy = result["rows"][0]["fields"]["comp_mm"]

        assert cell["value"] == "1200"
        assert cell["status"] == "very_different"
        assert cell["source"] == "plan"
        assert "proposed" not in cell
        assert legacy["value"] == "1200"
        assert legacy["ref"] == "1200"
        assert legacy["ref_source"] == "plan"
        assert {
            "section": "rows",
            "row_index": 0,
            "field": "comp_mm",
            "field_path": "rows[0].comp_mm",
            "value": "9999",
            "ref": "1200",
            "ref_source": "plan",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(result["to_analisar"])

    def test_winner_picks_entry_via_agreement(self):
        """Linha com cliente+of+modelo a apontar para a mesma entry deve
        encontrar winner e propor coerência."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX", "of": "262108", "modelo": "OMEGA 1500 H",
                "comp_mm": "1500", "larg_mm": "250", "esp": "3,0",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        # Deve ter winner_of=262108
        assert row["winner_of"] == "262108"
        assert row["winner_score"] >= 3  # cliente + of + modelo + comp + esp

    def test_weak_dimension_only_can_fill_row_with_may_policy(self):
        """Uma dimensão isolada pode escolher e preencher a linha no modo maio."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"comp_mm": "1200"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        assert scoring["rows"][0]["winner_of"] == "262107"
        assert scoring["rows"][0]["winner_score"] >= 1
        assert fields["comp_mm"]["status"] == "confirmed"
        for field in ("cliente", "ov", "of", "modelo", "esp", "lbase", "ltopo"):
            assert fields[field]["status"] == "snapped"
            assert fields[field]["source"] == "plan"

    def test_sap_lote_and_width_are_evidence_without_special_anchor(self):
        """Lote/largura SAP validam SAP, mas não criam uma âncora especial.

        O lote é uma referência SAP da bobine, não uma ligação à entry do
        plan_colunas. Cliente + comp escolhem a entry; lote/largura ficam
        confirmados por SAP fora do score de Plan.
        """
        refs = {
            "available": True,
            "of_to_entries": {
                "111111": [{
                    "ov": "9100001",
                    "cliente": "ACME",
                    "designacao": "AAA WRONG",
                    "comp": 1000,
                    "lbase": 10,
                    "ltopo": 10,
                    "esp": 2,
                }],
                "222222": [{
                    "ov": "9200002",
                    "cliente": "ACME",
                    "designacao": "BBB TARGET",
                    "comp": 2000,
                    "lbase": 20,
                    "ltopo": 20,
                    "esp": 3,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "plan_by_cliente": {
                "ACME": [
                    {
                        "_of": "111111",
                        "ov": "9100001",
                        "cliente": "ACME",
                        "designacao": "AAA WRONG",
                        "comp": 1000,
                        "lbase": 10,
                        "ltopo": 10,
                        "esp": 2,
                    },
                    {
                        "_of": "222222",
                        "ov": "9200002",
                        "cliente": "ACME",
                        "designacao": "BBB TARGET",
                        "comp": 2000,
                        "lbase": 20,
                        "ltopo": 20,
                        "esp": 3,
                    },
                ],
            },
            "lotes_sap_full": {"LOTEX": {"larg": 250, "esp": 2}},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "ACME",
                "lote": "LOTEX",
                "larg_mm": "250",
                "comp_mm": "1010",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_score"] == 2  # cliente + comp; lote/larg SAP não contam.
        assert fields["cliente"]["status"] == "confirmed"
        assert fields["lote"]["status"] == "confirmed"
        assert fields["larg_mm"]["status"] == "confirmed"
        assert fields["comp_mm"]["value"] == "1000"
        assert fields["comp_mm"]["status"] == "snapped"
        for field in ("of", "ov", "modelo", "esp", "lbase", "ltopo"):
            assert fields[field]["status"] == "snapped"
            assert fields[field]["source"] == "plan"

    def test_out_of_tolerance_dimensions_do_not_inflate_winner_score(self):
        """Medidas fora de tolerância não contam no score, mas winner propõe refs."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX",
                "modelo": "OMEGA 1500 H",
                "comp_mm": "1551",
                "lbase": "71",
                "ltopo": "51",
                "esp": "3,0",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262108"
        assert row["winner_score"] > 5  # score contínuo inclui semelhança parcial.
        assert fields["of"]["status"] == "snapped"
        assert fields["ov"]["status"] == "snapped"
        assert fields["cliente"]["status"] == "confirmed"
        assert fields["modelo"]["status"] == "confirmed"
        # R222/D7 — deltas (comp 51, lbase 11, ltopo 11) ficam abaixo do limiar
        # de COR de 30/05 (200/30/30): substituem (snapped) mas já não vermelho.
        # Continuam fora da tolerância de SCORING (não inflam o winner_score).
        for field, ref in (
            ("comp_mm", "1500"),
            ("lbase", "60"),
            ("ltopo", "40"),
        ):
            assert fields[field]["value"] == ref
            assert fields[field]["status"] == "snapped"


class TestLoteHCorrection:
    """R259 — H e M são códigos diferentes: um lote H nunca confirma verde só
    porque a variante M existe no StockSAP. A confusão OCR H→M vira correção
    explícita (snapped para o canónico M) quando as medidas da linha validam
    a entry SAP, ou revisão sem gravação (no_auto_write) quando divergem."""

    # Espelha o ref_watcher: a linha real sem prefixo e o alias M partilham
    # a MESMA entry (setdefault com a mesma referência).
    _ENTRY_26B0473 = {"desc": "S355 BOBINE", "larg": 1320, "esp": 4.8}
    _REFS_H = {
        "available": True,
        "of_to_entries": {},
        "lotes_sap_full": {
            "M26B0330": {"desc": "S355 BOBINE", "larg": 1500, "esp": 4},
            "26B0473": _ENTRY_26B0473,
            "M26B0473": _ENTRY_26B0473,
            "H24B1003": {"desc": "S355 BOBINE", "larg": 1000, "esp": 3},
        },
        "clientes_plan": frozenset(),
    }

    def _sheet(self, row: dict) -> dict:
        return {
            "template_name": "bobine_formato",
            "header": {},
            "footer": {},
            "rows": [row],
        }

    def test_lote_h_to_m_accepted_snaps_to_canonical(self):
        # Caso a — OCR H26B0330, medidas batem com M26B0330 {1500, 4}:
        # auto-snap para o canónico M; nunca H verde.
        sheet = self._sheet({"lote": "H26B0330", "larg_mm": "1500", "esp": "4"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)
        result = cross_check_sheet(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "snapped"
        assert cell["value"] == "M26B0330"
        assert cell["source"] == "sap"
        assert cell["decision_reason"] == "lote_h_ocr_confusion"

        legacy = result["rows"][0]["fields"]["lote"]
        assert legacy["status"] == "MATCH"
        assert legacy["value"] == "M26B0330"
        assert legacy["snapped"] is True
        assert legacy["ref"] == "M26B0330"
        assert result["to_analisar"] == []
        assert result["rows"][0]["fields"]["larg_mm"]["status"] == "MATCH"
        assert result["rows"][0]["fields"]["esp"]["status"] == "MATCH"

    def test_lote_m_exact_confirmed(self):
        # Caso b — OCR M26B0330: match exato, confirmado sem proposta.
        sheet = self._sheet({"lote": "M26B0330", "larg_mm": "1500", "esp": "4"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "confirmed"
        assert cell["value"] == "M26B0330"
        assert cell["source"] == "sap"
        assert "proposed" not in cell

    def test_lote_h_to_m_rejected_on_measure_divergence(self):
        # Caso c — OCR H26B0473 com larg 1265/esp 4; SAP só tem 26B0473
        # {1320, 4.8}: NÃO confirma, NÃO grava; revisão com as divergências.
        sheet = self._sheet({"lote": "H26B0473", "larg_mm": "1265", "esp": "4"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)
        result = cross_check_sheet(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "very_different"
        assert cell["value"] == "H26B0473"
        assert cell["source"] == "ocr_raw"
        assert cell["proposed"] == "M26B0473"
        assert cell["no_auto_write"] is True

        legacy = result["rows"][0]["fields"]["lote"]
        assert legacy["status"] == "NO_MATCH"
        assert legacy["value"] == "H26B0473"
        assert legacy["ref"] == "M26B0473"
        assert legacy["no_auto_write"] is True

        item = next(
            it for it in result["to_analisar"]
            if it["field_path"] == "rows[0].lote"
        )
        assert item["ref"] == "M26B0473"
        assert "1265" in item["reason"] and "1320" in item["reason"]
        assert "4,8" in item["reason"]

        # As medidas do operador NÃO são substituídas pela entry do lote
        # não confirmado (antes: larg 1265 → 1320 gravada via source=sap).
        larg = result["rows"][0]["fields"]["larg_mm"]
        assert larg["value"] == "1265"
        assert larg["source"] != "sap"
        assert larg.get("ref", "") == ""
        esp = result["rows"][0]["fields"]["esp"]
        assert esp["value"] == "4"
        assert esp["source"] != "sap"

    def test_lote_no_prefix_alias_confirms_never_h(self):
        # Caso d — OCR M26B0473 casa o registo SAP 26B0473 via alias M:
        # confirmado com a forma M, nunca convertido para H.
        sheet = self._sheet({"lote": "M26B0473", "larg_mm": "1320", "esp": "4,8"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)
        result = cross_check_sheet(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "confirmed"
        assert cell["value"] == "M26B0473"
        assert result["rows"][0]["fields"]["larg_mm"]["status"] == "MATCH"
        assert result["rows"][0]["fields"]["esp"]["status"] == "MATCH"
        assert result["to_analisar"] == []

    def test_lote_h_to_m_accepted_when_no_measures(self):
        # Sem larg/esp legíveis a correção aplica-se na mesma (decisão Luís
        # 2026-07-16): medidas só bloqueiam quando existem E divergem.
        sheet = self._sheet({"lote": "H26B0330"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "snapped"
        assert cell["value"] == "M26B0330"

    def test_lote_h_literal_in_sap_confirms(self):
        # Lote H que existe LITERALMENTE no SAP → match exato normal.
        sheet = self._sheet({"lote": "H24B1003", "larg_mm": "1000", "esp": "3"})
        scoring, *_ = shadow_score(sheet, None, self._REFS_H)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "confirmed"
        assert cell["value"] == "H24B1003"
        assert "proposed" not in cell

    def test_lote_h_without_sap_pool_keeps_current_review(self):
        # StockSAP indisponível: comportamento atual (revisão sem proposta).
        refs = {"available": True, "of_to_entries": {}, "lotes_sap_full": {}}
        sheet = self._sheet({"lote": "H26B0330"})
        scoring, *_ = shadow_score(sheet, None, refs)

        cell = scoring["rows"][0]["fields"]["lote"]
        assert cell["status"] == "very_different"
        assert cell["value"] == "H26B0330"
        assert cell["source"] == "ocr_raw"

    def test_lote_variants_no_h(self):
        # A variante H→M saiu das variantes legítimas de confirmação.
        assert _lote_variants("H26B0330") == ["H26B0330"]
        assert _lote_variants("26B0473") == ["26B0473", "M26B0473"]
        assert _lote_variants("M26B0330") == ["M26B0330"]
        assert _lote_h_correction("H26B0330") == "M26B0330"
        assert _lote_h_correction("M26B0330") == ""
        assert _lote_h_correction("26B0473") == ""

    @pytest.mark.parametrize(
        "row,expected_canonical,expected_kind",
        [
            ({"lote": "M26B0330"}, "M26B0330", "exact_or_alias"),
            ({"lote": "26B0473"}, "26B0473", "exact_or_alias"),
            (
                {"lote": "H26B0330", "larg_mm": "1500", "esp": "4"},
                "M26B0330",
                "h_correction_accepted",
            ),
            (
                {"lote": "H26B0473", "larg_mm": "1265", "esp": "4"},
                "M26B0473",
                "h_correction_rejected",
            ),
            ({"lote": "H99B9999"}, "", "none"),
            ({"lote": ""}, "", "none"),
        ],
    )
    def test_resolve_row_lote_kinds(self, row, expected_canonical, expected_kind):
        canonical, _entry, kind = _resolve_row_lote(self._REFS_H, row)
        assert canonical == expected_canonical
        assert kind == expected_kind

    def test_resolve_row_lote_h_via_suffix_without_m_alias(self):
        # Refs sintéticas sem o alias M (só a linha sem prefixo): a correção
        # resolve pelo sufixo mas propõe SEMPRE a forma M canónica.
        refs = {
            "available": True,
            "lotes_sap_full": {"26B0473": {"larg": 1320, "esp": 4.8}},
        }
        canonical, entry, kind = _resolve_row_lote(
            refs, {"lote": "H26B0473", "larg_mm": "1320", "esp": "4,8"}
        )
        assert canonical == "M26B0473"
        assert entry == {"larg": 1320, "esp": 4.8}
        assert kind == "h_correction_accepted"


class TestFindWinner:
    def test_no_plan_entries_returns_none(self):
        winner = _find_winner_entry({}, {}, {"of_to_entries": {}})
        assert winner is None

    def test_scores_all_plan_entries_not_only_topk_candidates(self):
        refs = {
            "available": True,
            "of_to_entries": {
                f"260{i:03d}": [{
                    "ov": f"240{i:04d}",
                    "cliente": f"CLIENTE {i}",
                    "designacao": f"MODELO {i}",
                    "comp": 1000 + i,
                    "lbase": 100 + i,
                    "ltopo": 50 + i,
                    "esp": 3,
                }]
                for i in range(20)
            },
            "clientes_plan": frozenset({f"CLIENTE {i}" for i in range(20)}),
            "lotes_sap_full": {},
        }
        idx = _get_indices(refs)
        row = {
            "cliente": "CLIENTE 19",
            "modelo": "MODELO 19",
            "comp_mm": "1019",
            "lbase": "119",
            "ltopo": "69",
            "esp": "3",
        }

        winner = _find_winner_entry({}, row, refs, idx)

        assert winner is not None
        assert winner["_of"] == "260019"
        assert winner["_score"] == 6

    def test_best_scored_entry_returns_stamped_of_copy(self):
        entries = {
            ("262107", "2410001", "OMEGA 1200 H"): {
                "ov": "2410001",
                "cliente": "ELECNOR",
                "designacao": "OMEGA 1200 H",
            },
        }

        winner = _best_scored_entry(
            entries,
            {"cliente": "ELECNOR", "modelo": "OMEGA 1200 H"},
            _REFS,
            current_phase=None,
        )

        assert winner is not None
        assert winner["_of"] == "262107"
        assert winner["_score"] == 2
        assert winner["_exact_score"] == 2


class TestGlobalWinnerScoring:
    """O winner é sempre o maior score global, sem âncoras especiais OF/OV.

    Cada campo lido vale no máximo 1. Com winner concreto, campos divergentes
    carregam o valor canónico do plan/SAP (filosofia R134/R135).
    """

    def test_of_mismatch_fills_winner_ref_and_marks_review(self):
        """OCR de OF que não existe não bloqueia o winner global."""
        from app.pipeline.scoring_engine import cross_check_sheet

        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX", "of": "999999",  # OF inexistente no plan
                "ov": "2410002", "modelo": "OMEGA 1500 H",
                "comp_mm": "1500", "esp": "3,0",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        of_cell = scoring["rows"][0]["fields"]["of"]
        assert of_cell["value"] == "262108"
        assert of_cell["status"] == "very_different"
        assert of_cell["source"] == "plan"
        result = cross_check_sheet(sheet_data, None, _REFS)
        legacy = result["rows"][0]["fields"]["of"]
        assert legacy["value"] == "262108"
        assert legacy["ref"] == "262108"
        assert legacy["ref_source"] == "plan"
        assert {
            "section": "rows",
            "row_index": 0,
            "field": "of",
            "field_path": "rows[0].of",
            "value": "999999",
            "ref": "262108",
            "ref_source": "plan",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(result["to_analisar"])

    def test_of_agreement_confirms(self):
        """OCR de OF que bate exactamente com plan → confirmed (verde)."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "cliente": "ELECNOR"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        assert scoring["rows"][0]["fields"]["of"]["status"] == "confirmed"
        assert scoring["rows"][0]["fields"]["of"]["value"] == "262107"

    @pytest.mark.parametrize("ocr_cliente", ["STAEK MTG", "STACK MTG", "STACA MTG"])
    def test_cliente_ocr_alias_selects_real_plan_row_and_fills_line(self, ocr_cliente):
        refs = {
            "available": True,
            "of_to_entries": {
                "262771": [{
                    "ov": "260001",
                    "cliente": "ESTOQUE MTG",
                    "designacao": "CGC2E45DI",
                    "esp": 3,
                    "lbase": 431,
                    "ltopo": 180,
                }],
            },
            "clientes_plan": frozenset({"ESTOQUE MTG"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "quinadora_pav4", "header": {}, "footer": {},
            "rows": [{"cliente": ocr_cliente, "qtd": "90"}],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        row = result["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262771"
        assert fields["cliente"]["value"] == "ESTOQUE MTG"
        assert fields["of"]["value"] == "262771"
        assert fields["ov"]["value"] == "260001"
        assert fields["modelo"]["value"] == "CGC2E45DI"
        assert fields["esp"]["value"] == "3"
        assert fields["lbase"]["value"] == "431"
        assert fields["ltopo"]["value"] == "180"
        assert fields["qtd"]["match_kind"] == "MATCH_REGRA"
        assert all(
            fields[f]["status"] != "NA"
            for f in ("cliente", "of", "ov", "modelo", "esp", "lbase", "ltopo", "qtd")
        )

    def test_forced_model_candidate_selects_real_plan_row_and_fills_line(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262771": [{
                    "ov": "260001",
                    "cliente": "ESTOQUE MTG",
                    "designacao": "CGC2E45DI",
                    "esp": 3,
                    "lbase": 431,
                    "ltopo": 180,
                }],
            },
            "clientes_plan": frozenset({"ESTOQUE MTG"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "quinadora_pav4", "header": {}, "footer": {},
            "rows": [{"modelo": "C6C2E45DI", "qtd": "90"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262771"
        # R236 — em FS, um modelo a 1 carácter de uma designação quase-única é
        # evidência FORTE (~8 bits; m_modelo=0.52, u≈1/1000): sem rival com OF
        # diferente, o modo é "strong" (o R223 contava campos: 1 campo = weak).
        assert row["winner_mode"] == "strong"
        assert fields["cliente"]["value"] == "ESTOQUE MTG"
        assert fields["of"]["value"] == "262771"
        assert fields["ov"]["value"] == "260001"
        assert fields["modelo"]["value"] == "CGC2E45DI"
        assert fields["esp"]["value"] == "3"
        assert fields["lbase"]["value"] == "431"
        assert fields["ltopo"]["value"] == "180"
        assert all(
            fields[f]["status"] != "NA"
            for f in ("cliente", "of", "ov", "modelo", "esp", "lbase", "ltopo", "qtd")
        )

    def test_unmatched_cliente_yields_no_winner_and_no_forced_fill(self):
        # R223 — sem votação holística a concordar em pelo menos 1 campo, NÃO
        # há winner: um cliente que não bate com nada ("ABC") já não força a
        # melhor peça plausível ("só se não encontrar mesmo nada é que não põe").
        refs = {
            "available": True,
            "of_to_entries": {
                "262771": [{
                    "ov": "260001",
                    "cliente": "ESTOQUE MTG",
                    "designacao": "CGC2E45DI",
                    "esp": 3,
                    "lbase": 431,
                    "ltopo": 180,
                }],
            },
            "clientes_plan": frozenset({"ESTOQUE MTG"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "quinadora_pav4", "header": {}, "footer": {},
            "rows": [{"cliente": "ABC", "qtd": "90"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] is None
        assert row["winner_mode"] is None
        # Cliente preenchido sem winner não fica verde forçado: fica vermelho
        # (very_different) para o operador conferir, e não invade outros campos.
        assert fields["cliente"]["value"] == "ABC"
        assert fields["cliente"]["status"] == "very_different"
        assert fields["of"]["status"] == "NA"
        assert fields["modelo"]["status"] == "NA"
        # qtd informativo continua a validar por regra local.
        assert fields["qtd"]["match_kind"] == "MATCH_REGRA"

    def test_empty_local_fields_with_winner_are_rule_matches_not_na(self):
        sheet_data = {
            "template_name": "robot", "header": {}, "footer": {},
            "rows": [{
                "cliente": "ELECNOR",
                "of": "262107",
                "modelo": "OMEGA 1200 H",
                "qtd": "2",
                "pri": "",
                "sobras": "",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        assert scoring["rows"][0]["winner_of"] == "262107"
        assert fields["pri"]["status"] == "confirmed"
        assert fields["pri"]["match_kind"] == "MATCH_REGRA_VAZIO"
        assert fields["sobras"]["status"] == "confirmed"
        assert fields["sobras"]["match_kind"] == "MATCH_REGRA_VAZIO"

    def test_of_ov_o_zero_variants_snap_to_plan_value(self):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "2621O7",
                "ov": "241O001",
                "cliente": "ELECNOR",
                "modelo": "OMEGA 1200 H",
            }],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        fields = result["rows"][0]["fields"]

        assert fields["of"]["value"] == "262107"
        assert fields["of"]["status"] == "MATCH"
        assert fields["of"]["ref"] == "262107"
        assert fields["ov"]["value"] == "2410001"
        assert fields["ov"]["status"] == "MATCH"
        assert fields["ov"]["ref"] == "2410001"
        assert result["to_analisar"] == []

    def test_ov_single_extra_zero_variant_snaps_to_plan_value(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262300": [{
                    "ov": "2600885",
                    "cliente": "ACME",
                    "designacao": "OMEGA 1200 H",
                }],
            },
            "of_to_ovs": {"262300": frozenset({"2600885"})},
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262300",
                "ov": "26000885",
                "cliente": "ACME",
                "modelo": "OMEGA 1200 H",
            }],
        }

        result = cross_check_sheet(sheet_data, None, refs)
        ov_cell = result["rows"][0]["fields"]["ov"]

        assert ov_cell["value"] == "2600885"
        assert ov_cell["status"] == "MATCH"
        assert ov_cell["ref"] == "2600885"
        assert result["to_analisar"] == []

    def test_ov_mismatch_fills_winner_ref_and_marks_review(self):
        """OV diferente do winner mostra a OV da melhor linha."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "of": "262107",  # OV no plan = 2410001
                "ov": "9999999",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        ov_cell = scoring["rows"][0]["fields"]["ov"]
        assert ov_cell["value"] == "2410001"
        assert ov_cell["status"] == "very_different"
        assert ov_cell["source"] == "plan"

    def test_of_ov_conflict_uses_global_winner_when_rest_agrees(self):
        """OF e OV válidas mas incompatíveis não ancoram a escolha."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                # OF aponta para 262107; OV/cliente/modelo/comp apontam para 262108.
                "of": "262107", "ov": "2410002",
                "cliente": "MTG BELUX", "modelo": "OMEGA 1500 H",
                "comp_mm": "1500",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262108"
        assert row["identity_conflict"] is False
        assert fields["of"]["value"] == "262108"
        # R236 — a OF escrita (262107) EXISTE no plano e o winner contradi-la:
        # substitui (R219) mas fica vermelha/rever. É a classe de erro provada
        # no backtest (folha 2557: OF válida 263185 reescrita para 263183
        # errada) — contradizer uma OF válida nunca mais passa verde-silencioso.
        assert fields["of"]["status"] == "very_different"
        assert fields["ov"]["status"] == "confirmed"
        assert fields["cliente"]["status"] == "confirmed"
        assert fields["modelo"]["status"] == "confirmed"
        assert fields["comp_mm"]["status"] == "confirmed"
        for field in ("esp", "lbase", "ltopo"):
            assert fields[field]["status"] == "snapped"
            assert fields[field]["source"] == "plan"

    def test_of_ov_conflict_substitutes_winner_and_flags(self):
        """R219 — OF (262107) e OV (2410002) apontam para linhas DIFERENTES
        (262108 tem essa OV): conflito de identidade. A OF confirma (o OCR bate
        com o winner). Os campos onde as duas linhas discordam são SUBSTITUÍDOS
        pelo valor da linha vencedora (262107) mas marcados very_different."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "ov": "2410002"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        assert scoring["rows"][0]["winner_of"] == "262107"
        assert scoring["rows"][0]["winner_score"] >= 1
        # OF: o OCR confirma o winner → verde.
        assert fields["of"]["value"] == "262107"
        assert fields["of"]["status"] == "confirmed"
        # OV: o OCR (2410002) não bate com o winner (2410001) e as linhas
        # discordam → SUBSTITUI pelo do vencedor e marca para revisão.
        assert fields["ov"]["value"] == "2410001"
        assert fields["ov"]["status"] == "very_different"
        # Campos vazios em disputa: preenchidos do vencedor (262107) + vermelho.
        assert fields["cliente"]["value"] == "ELECNOR"
        assert fields["cliente"]["status"] == "very_different"
        assert fields["modelo"]["value"] == "OMEGA 1200 H"
        assert fields["comp_mm"]["value"] == "1200"
        for field in ("cliente", "modelo", "comp_mm", "esp"):
            assert fields[field]["status"] == "very_different"

    def test_single_exact_of_can_fill_row_in_expedicao(self):
        """No modo maio, uma OF isolada pode escolher winner e preencher."""
        refs = {
            "available": True,
            "of_to_entries": {
                "257083": [{
                    "ov": "2510730",
                    "cliente": "ASVITAE TECNOLOGIAS",
                    "designacao": "A730UF00 - A730U500 + CUTELOS",
                }],
            },
            "clientes_plan": frozenset({"ASVITAE TECNOLOGIAS"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "expedicao", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTGBELUX",
                "ov": "2511344",
                "of": "257083",
                "modelo": "CBCBE06DI",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "257083"
        assert row["winner_score"] >= 1
        assert fields["of"]["status"] == "confirmed"
        assert fields["cliente"]["value"] == "ASVITAE TECNOLOGIAS"
        assert fields["cliente"]["status"] == "very_different"
        assert fields["ov"]["value"] == "2510730"
        assert fields["ov"]["status"] == "very_different"
        assert fields["modelo"]["value"] == "A730UF00 - A730U500 + CUTELOS"
        assert fields["modelo"]["status"] == "very_different"

    def test_wrong_of_loses_to_stronger_global_score(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "257083": [{
                    "ov": "2510730",
                    "cliente": "ASVITAE TECNOLOGIAS",
                    "designacao": "A730UF00 - A730U500 + CUTELOS",
                }],
                "257093": [{
                    "ov": "2511344",
                    "cliente": "MTG BELUX",
                    "designacao": "CBCBE06DI - 2 SC",
                }],
                "257098": [{
                    "ov": "2511344",
                    "cliente": "MTG BELUX",
                    "designacao": "CIBCE05D - TR AB 177 125",
                }],
            },
            "clientes_plan": frozenset({
                "ASVITAE TECNOLOGIAS",
                "MTG BELUX",
            }),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "expedicao", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTGBELUX",
                "ov": "2511344",
                "of": "257083",
                "modelo": "CBCBE06DI",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "257093"
        assert row["winner_score"] > 3
        assert fields["cliente"]["status"] == "snapped"
        assert fields["ov"]["status"] == "confirmed"
        assert fields["of"]["value"] == "257093"
        # R236 — a OF escrita (257083) existe no plano e aponta para OUTRA
        # encomenda: substitui pelo winner (R219) mas fica vermelha/rever.
        assert fields["of"]["status"] == "very_different"
        assert fields["modelo"]["status"] == "snapped"

    def test_nonexistent_ocr_of_is_replaced_by_coherent_global_winner(self):
        """OCR 288478 não existe; plan 288476 ganha pela coerência da linha."""
        refs = {
            "available": True,
            "of_to_entries": {
                "288476": [{
                    "ov": "2603487",
                    "cliente": "VANTAGE TOWERS",
                    "designacao": "A4.504",
                    "comp": 1200,
                    "larg": 250,
                    "lbase": 50,
                    "ltopo": 30,
                    "esp": 2.6,
                }],
            },
            "of_to_ovs": {"288476": frozenset({"2603487"})},
            "clientes_plan": frozenset({"VANTAGE TOWERS"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "VANTAGE TOWERS",
                "ov": "2603487",
                "of": "288478",
                "modelo": "A4 504",
                "comp_mm": "1200",
                "larg_mm": "250",
                "lbase": "50",
                "ltopo": "30",
                "esp": "2,6",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "288476"
        assert row["winner_score"] > 5
        assert fields["of"]["value"] == "288476"
        assert fields["of"]["status"] == "snapped"
        assert fields["of"]["source"] == "plan"
        for field in ("cliente", "ov", "modelo", "comp_mm", "larg_mm", "lbase", "ltopo", "esp"):
            assert fields[field]["source"] == "plan"

    def test_soft_of_ov_ocr_errors_snap_with_strong_winner(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "257504": [{
                    "ov": "250010",
                    "cliente": "MTG",
                    "designacao": "CAO8E10B - ESP 3",
                }],
            },
            "clientes_plan": frozenset({"MTG"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "expedicao", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG",
                "ov": "250410",
                "of": "257509",
                "modelo": "CA08E10B",
                "qtd": "16",
                "cesta_n": "4554",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "257504"
        assert row["winner_score"] > 3
        assert fields["ov"]["value"] == "250010"
        assert fields["ov"]["status"] == "snapped"
        assert fields["of"]["value"] == "257504"
        assert fields["of"]["status"] == "snapped"
        assert fields["modelo"]["status"] == "snapped"
        assert fields["qtd"]["status"] == "confirmed"
        assert fields["cesta_n"]["status"] == "confirmed"

    def test_duplicate_plan_entries_with_same_score_use_tiebreak(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262532": [
                    {
                        "ov": "2603512",
                        "cliente": "LE HAVRE",
                        "designacao": "8661SF00 - 8661S500 + BASE INOX - TOPO",
                        "quanttrp": 1,
                        "fases": {"exp": 0},
                    },
                    {
                        "ov": "2603512",
                        "cliente": "LE HAVRE",
                        "designacao": "8661SF00 - 8661SF00 + BASE INOX + FL PL - BASE",
                        "quanttrp": 1,
                        "fases": {"exp": 0},
                    },
                ],
            },
            "clientes_plan": frozenset({"LE HAVRE"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "expedicao",
            "header": {"setor_maquina": "EXPEDIÇÃO"},
            "footer": {},
            "rows": [{
                "cliente": "LE HAVRE",
                "ov": "2603512",
                "of": "262532",
                "modelo": "8661SF00",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]

        assert row["winner_of"] == "262532"
        assert row["winner_score"] == 4
        # R248 — "8661SF00" aparece nas DUAS designações irmãs (TOPO e BASE:
        # peças diferentes) — o modelo escrito não discrimina a sub-linha.
        # O desempate continua a escolher uma entry (substitute-everything),
        # mas a célula fica em revisão em vez de verde-por-desempate.
        modelo = row["fields"]["modelo"]
        assert modelo["status"] == "very_different"
        assert modelo["decision_reason"] == "ambiguous_sibling_designacao"

    def test_two_fuzzy_identifiers_without_exact_support_select_best_winner(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "257093": [{
                    "ov": "2511344",
                    "cliente": "STOCK MTG BELUX",
                    "designacao": "CBCBE06DI - 2 SC",
                }],
            },
            "clientes_plan": frozenset({"STOCK MTG BELUX"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "robot", "header": {}, "footer": {},
            "rows": [{
                "cliente": "RELUX",
                "ov": "2517344",
                "of": "252093",
                "modelo": "CFC R606Di",
                "qtd": "101",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]

        assert row["winner_of"] == "257093"
        assert row["winner_score"] is not None

    def test_cliente_leading_ocr_character_contributes_to_global_score(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262414": [{
                    "ov": "2602568",
                    "cliente": "MTG GMBH",
                    "designacao": "CD300J07 - 4KN/15M Nº1",
                }],
            },
            "clientes_plan": frozenset({"MTG GMBH"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "expedicao", "header": {}, "footer": {},
            "rows": [{
                "cliente": "GMTG GMBH",
                "ov": "2602568",
                "of": "222414",
                "modelo": "CD300J07",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]

        assert row["winner_of"] == "262414"
        assert row["winner_score"] > 3
        assert row["fields"]["cliente"]["value"] == "MTG GMBH"
        assert row["fields"]["cliente"]["status"] == "snapped"
        assert row["fields"]["of"]["value"] == "262414"
        assert row["fields"]["of"]["status"] == "snapped"

    def test_cliente_mismatch_fills_winner_ref_and_marks_review(self):
        """Cliente diferente também usa a melhor linha quando há candidatos."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"cliente": "SUNNA", "of": "262107"}],  # OF da ELECNOR
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        cli = scoring["rows"][0]["fields"]["cliente"]
        assert cli["value"] == "ELECNOR"
        assert cli["status"] == "very_different"
        assert cli["source"] == "plan"
        assert scoring["rows"][0]["winner_of"] == "262107"

    @pytest.mark.parametrize(
        ("ocr_cliente", "plan_cliente", "matches"),
        [
            ("MTGBELUX", "MTG BELUX", True),
            ("MTGBELUX", "STOCK MTG BELUX", False),
            ("MTG BELUX", "STOCK MTG BELUX", False),
            ("TECPOLES", "TECPOLES GMBH", True),
            ("LUMIERE", "WE EF LUMIERE", False),
            ("SOVEC", "SOVEC ENTREPRISES", False),
            ("MTG", "MTG BELUX", False),
            ("MTG", "STOCK MTG BELUX", False),
            ("HTG", "MTG", False),
        ],
    )
    def test_cliente_only_matches_strict_compact_equality(
        self, ocr_cliente, plan_cliente, matches
    ):
        assert _cliente_values_match(ocr_cliente, plan_cliente, _REFS) is matches

    def test_cliente_compact_does_not_match_inside_other_words(self):
        assert not _cliente_values_match(
            "RODEL",
            "COMPANHIA CARRIS DE FERRO DE LISBOA SA",
            _REFS,
        )
        assert not _cliente_values_match("METAL", "METALOGALVA LTD", _REFS)
        assert not _cliente_values_match("COMP", "COMPRING SAC", _REFS)
        assert _cliente_values_match("RODEL", "RODEL", _REFS)
        assert _cliente_values_match("MTGBELUX", "MTG BELUX", _REFS)
        assert not _cliente_values_match("SOVEC", "SOVEC ENTREPRISES", _REFS)

    def test_cliente_does_not_use_token_subset_aliases_or_stock_prefix(self):
        refs = {
            **_REFS,
            "cliente_aliases": {
                "HTG": "MTG",
                "SK-T-BELUX": "STOCK MTG BELUX",
            },
        }

        assert not _cliente_values_match("MTG", "STOCK MTG", refs)
        assert not _cliente_values_match("MTG BELUX", "STOCK MTG BELUX", refs)
        assert not _cliente_values_match("HTG BELUX", "STOCK MTG BELUX", refs)
        assert _cliente_values_match("STOCK MTG", "STOCK MTG GMBH", refs)
        assert not _cliente_values_match("SK-T-BELUX", "STOCK MTG BELUX", refs)

    def test_cliente_alias_is_ignored_but_single_of_replaces_cliente(self):
        refs = {
            **_REFS,
            "of_to_entries": {
                "262301": [{
                    "ov": "2600301",
                    "cliente": "MTG",
                    "designacao": "OMEGA 1200 H",
                }],
            },
            "clientes_plan": frozenset({"MTG"}),
            "cliente_aliases": {"HTG": "MTG"},
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"cliente": "HTG", "of": "262301"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        result = cross_check_sheet(sheet_data, None, refs)
        cli = scoring["rows"][0]["fields"]["cliente"]

        assert not _cliente_values_match("HTG", "MTG", refs)
        assert scoring["rows"][0]["winner_of"] == "262301"
        assert cli["value"] == "MTG"
        assert cli["source"] == "plan"
        assert result["rows"][0]["fields"]["cliente"]["value"] == "MTG"

    def test_cliente_token_alias_is_ignored_but_single_of_replaces_cliente(self):
        refs = {
            **_REFS,
            "of_to_entries": {
                "262302": [{
                    "ov": "2600302",
                    "cliente": "MTG BELUX",
                    "designacao": "OMEGA 1200 H",
                }],
            },
            "clientes_plan": frozenset({"MTG BELUX"}),
            "cliente_aliases": {"HTG": "MTG"},
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"cliente": "HTG BELUX", "of": "262302"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        result = cross_check_sheet(sheet_data, None, refs)
        cli = scoring["rows"][0]["fields"]["cliente"]

        assert not _cliente_values_match("HTG BELUX", "MTG BELUX", refs)
        assert scoring["rows"][0]["winner_of"] == "262302"
        assert cli["value"] == "MTG BELUX"
        assert cli["source"] == "plan"
        assert result["rows"][0]["fields"]["cliente"]["value"] == "MTG BELUX"

    def test_cliente_token_alias_different_suffix_is_not_a_match(self):
        refs = {
            **_REFS,
            "of_to_entries": {
                "262303": [{
                    "ov": "2600303",
                    "cliente": "MTG BELUX",
                    "designacao": "OMEGA 1200 H",
                }],
            },
            "clientes_plan": frozenset({"MTG BELUX"}),
            "cliente_aliases": {"HTG": "MTG"},
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"cliente": "HTG BELGIUM", "of": "262303"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        cli = scoring["rows"][0]["fields"]["cliente"]

        assert not _cliente_values_match("HTG BELGIUM", "MTG BELUX", refs)
        assert scoring["rows"][0]["winner_of"] == "262303"
        assert cli["value"] == "MTG BELUX"
        assert cli["status"] == "snapped"
        assert cli["source"] == "plan"

    def test_dimensional_only_can_select_global_winner(self):
        """Medidas fortes podem escolher a linha mesmo com identidade OCR errada."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                # nada disto bate o plan por identidade; só as medidas da 262108
                "cliente": "DESCONHECIDO", "of": "111111", "ov": "8888888",
                "modelo": "ZZZ", "comp_mm": "1500", "larg_mm": "250",
                "lbase": "60", "ltopo": "40", "esp": "3,0",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]
        assert scoring["rows"][0]["winner_of"] == "262108"
        assert scoring["rows"][0]["winner_score"] > 5
        assert scoring["rows"][0]["identity_conflict"] is False
        assert fields["of"]["value"] == "262108"
        assert fields["of"]["status"] == "very_different"
        assert fields["of"]["source"] == "plan"
        assert fields["ov"]["value"] == "2410002"
        assert fields["ov"]["status"] == "very_different"
        assert fields["ov"]["source"] == "plan"
        for field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp"):
            assert fields[field]["status"] == "confirmed"

    def test_field_outside_template_does_not_produce_winner(self):
        # R223 — `comp_mm` existe no JSON sujo mas o template `soldline` não o
        # cruza, por isso não entra na votação holística: nenhum campo cruzável
        # tem sinal → sem winner (em vez de forçar a melhor peça plausível).
        refs = {
            "available": True,
            "of_to_entries": {
                "262900": [{
                    "ov": "2602900",
                    "cliente": "ACME",
                    "designacao": "SOLDLINE TARGET",
                    "comp": 1200,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "soldline", "header": {}, "footer": {},
            # comp_mm can exist in dirty/OCR JSON, but soldline does not cross it.
            "rows": [{"comp_mm": "1200"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]

        assert row["winner_of"] is None
        assert row["winner_mode"] is None
        assert row["winner_score"] is None
        # O campo fora do schema valida por regra local (não fica NA neutro).
        assert row["fields"]["comp_mm"]["status"] == "confirmed"
        assert row["fields"]["comp_mm"]["source"] == "syntax"

    def test_wrong_of_with_strong_non_identity_evidence_wins_globally(self):
        """OF mal lida não bloqueia a linha que ganha no score total."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX", "of": "999999",
                "modelo": "OMEGA 1500 H", "comp_mm": "1500",
                "lbase": "60", "ltopo": "40", "esp": "3,0",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262108"
        assert row["winner_score"] >= 4
        assert row["identity_conflict"] is False
        assert fields["of"]["value"] == "262108"
        assert fields["of"]["status"] == "very_different"
        assert fields["of"]["source"] == "plan"
        assert fields["cliente"]["status"] == "confirmed"
        assert fields["modelo"]["status"] == "confirmed"
        for field in ("comp_mm", "lbase", "ltopo", "esp"):
            assert fields[field]["status"] == "confirmed"

    def test_wrong_of_with_o_zero_model_still_gets_global_winner(self):
        """O/0 no modelo continua a ser normalização permitida."""
        refs = {
            "available": True,
            "of_to_entries": {
                "262200": [{
                    "ov": "2410200",
                    "cliente": "ACME",
                    "designacao": "CGC2E10D - COLUNA TRONCO CONICA 10M",
                    "comp": 10000,
                    "lbase": 250,
                    "ltopo": 120,
                    "esp": 3,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "999999",
                "modelo": "CGC2E1OD",
                "comp_mm": "10000",
                "lbase": "250",
                "ltopo": "120",
                "esp": "3",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262200"
        assert row["identity_conflict"] is False
        assert fields["of"]["status"] == "very_different"
        assert fields["of"]["value"] == "262200"
        assert fields["modelo"]["value"] == "CGC2E10D - COLUNA TRONCO CONICA 10M"
        assert fields["modelo"]["status"] == "snapped"
        for field in ("comp_mm", "lbase", "ltopo", "esp"):
            assert fields[field]["status"] == "confirmed"

    def test_wrong_of_model_exact_first_token_snaps_to_designacao(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262220": [{
                    "ov": "2410220",
                    "cliente": "ACME",
                    "designacao": "CUP2F05RI - SEM PORTA + FL ESP - REVISAO A",
                    "comp": 5000,
                    "lbase": 400,
                    "ltopo": 180,
                    "esp": 2.6,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "999999",
                "modelo": "CUP2F05Ri",
                "comp_mm": "5000",
                "lbase": "400",
                "ltopo": "180",
                "esp": "2,6",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262220"
        assert row["identity_conflict"] is False
        assert fields["of"]["status"] == "very_different"
        assert fields["modelo"]["status"] == "snapped"
        assert fields["modelo"]["value"] == "CUP2F05RI - SEM PORTA + FL ESP - REVISAO A"
        assert fields["modelo"]["source"] == "plan"

    def test_wrong_of_model_exact_code_inside_designacao_snaps_to_designacao(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262221": [{
                    "ov": "2410221",
                    "cliente": "ACME",
                    "designacao": "CD03P10A - CD03P502 + FURACAO + TAMPA TOPO",
                    "comp": 5000,
                    "lbase": 400,
                    "ltopo": 180,
                    "esp": 2.6,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "999999",
                "modelo": "CD03P502",
                "comp_mm": "5000",
                "lbase": "400",
                "ltopo": "180",
                "esp": "2,6",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262221"
        assert row["identity_conflict"] is False
        assert fields["of"]["status"] == "very_different"
        assert fields["modelo"]["status"] == "snapped"
        assert fields["modelo"]["value"] == "CD03P10A - CD03P502 + FURACAO + TAMPA TOPO"

    def test_wrong_of_model_missing_i_before_v_is_very_different(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262222": [{
                    "ov": "2410222",
                    "cliente": "ACME",
                    "designacao": "CLCAF06DI_V - PONTEIRA",
                    "comp": 1415,
                    "lbase": 536,
                    "ltopo": 180,
                    "esp": 3,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "999999",
                "modelo": "CLCAF06DV",
                "comp_mm": "1415",
                "lbase": "536",
                "ltopo": "180",
                "esp": "3",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "262222"
        assert row["identity_conflict"] is False
        assert fields["of"]["status"] == "very_different"
        assert fields["modelo"]["status"] == "very_different"
        assert fields["modelo"]["value"] == "CLCAF06DI_V - PONTEIRA"
        assert fields["modelo"]["source"] == "plan"

    def test_close_non_identity_delta_snaps_after_global_winner(self):
        """Deltas dentro da tolerância são corrigidos depois do winner global."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX", "of": "999999",
                "modelo": "OMEGA 1500 H", "comp_mm": "1510",
                "lbase": "60", "ltopo": "40", "esp": "3,0",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        comp = row["fields"]["comp_mm"]

        assert row["identity_conflict"] is False
        assert comp["value"] == "1500"
        assert comp["status"] == "snapped"
        assert comp["source"] == "plan"

    def test_stale_refs_can_select_best_geometry_match(self):
        """Mesmo sem OF/OV novas nas refs, ganha a melhor linha pelo score global."""
        refs = {
            **_REFS,
            "of_to_entries": {
                "232976": [{
                    "ov": "2305550",
                    "cliente": "ABILIO E PAULO PEIXOTO",
                    "designacao": "CAC4E10B - 1ud- Anulada email Helena Silva 19-06",
                    "comp": 11050, "lbase": 659, "ltopo": 242, "esp": 4,
                    "quanttrp": 0,
                }],
            },
            "of_to_ovs": {"232976": frozenset({"2305550"})},
            "clientes_plan": frozenset({"ABILIO E PAULO PEIXOTO"}),
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "CODELBA", "ov": "2603977", "of": "263348",
                "modelo": "CA04E10B", "comp_mm": "11050",
                "lbase": "659", "ltopo": "242", "esp": "4",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, refs)
        fields = scoring["rows"][0]["fields"]
        assert scoring["rows"][0]["winner_of"] == "232976"
        assert scoring["rows"][0]["winner_score"] > 5
        assert fields["of"]["value"] == "232976"
        assert fields["of"]["status"] == "very_different"
        assert fields["of"]["source"] == "plan"
        assert fields["ov"]["value"] == "2305550"
        assert fields["ov"]["status"] == "very_different"
        for field in ("comp_mm", "lbase", "ltopo", "esp"):
            assert fields[field]["status"] == "confirmed"

    def test_obra_concluida_paints_whole_row_red(self):
        """R222 (reverte R163): obra concluída na fase pinta a linha inteira de
        vermelho (very_different / source="obra_concluida")."""
        refs = {
            **_REFS,
            "of_to_entries": {
                "232976": [{
                    "ov": "2305550",
                    "cliente": "ABILIO E PAULO PEIXOTO",
                    "designacao": "CAC4E10B",
                    "comp": 11050, "lbase": 659, "ltopo": 242, "esp": 4,
                    "quanttrp": 1,
                    "fases": {"bf": 1},
                }],
            },
            "of_to_ovs": {"232976": frozenset({"2305550"})},
            "clientes_plan": frozenset({"ABILIO E PAULO PEIXOTO"}),
            "maquinas_by_kanban": {
                "BOBINE-FORMATO": {"codmaq": "M032", "colunaexcel": "bf"},
            },
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"setor_maquina": "BOBINE-FORMATO"},
            "footer": {},
            "rows": [{
                "cliente": "CODELBA", "ov": "2603977", "of": "263348",
                "modelo": "CA04E10B", "comp_mm": "11050",
                "lbase": "659", "ltopo": "242", "esp": "4",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        assert row["winner_of"] == "232976"
        assert row["winner_score"] > 5
        assert row["obra_concluida"] is True
        assert all(
            cell.get("source") == "obra_concluida"
            and cell["status"] == "very_different"
            for cell in row["fields"].values()
        )

    def test_obra_concluida_forces_whole_row_to_review(self):
        """R222 (reverte R163): mesmo com a linha a bater certo com o plano, a
        fase cheia força very_different/source="obra_concluida" (rever)."""
        refs = {
            **_REFS,
            "of_to_entries": {
                "262107": [{
                    "ov": "2410001",
                    "cliente": "ELECNOR",
                    "designacao": "OMEGA 1200 H",
                    "comp": 1200,
                    "larg": 250,
                    "lbase": 50,
                    "ltopo": 30,
                    "esp": 2.6,
                    "quanttrp": 5,
                    "fases": {"bf": 5},
                }],
            },
            "maquinas_by_kanban": {
                "BOBINE-FORMATO": {"codmaq": "M032", "colunaexcel": "bf"},
            },
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"setor_maquina": "BOBINE-FORMATO"},
            "footer": {},
            "rows": [{
                "pri": "1", "cliente": "ELECNOR", "ov": "2410001",
                "of": "262107", "modelo": "OMEGA 1200 H", "qtd": "5",
                "comp_mm": "1200", "larg_mm": "250", "lote": "M26B0307",
                "coni": "", "esp": "2,6", "lbase": "50", "ltopo": "30",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["obra_concluida"] is True
        # R222 — obra concluída força very_different/source="obra_concluida" em
        # toda a linha, mesmo quando o OCR bate certo com o plano.
        assert all(
            cell.get("source") == "obra_concluida"
            and cell["status"] == "very_different"
            for cell in fields.values()
        )

    def test_score_propagated_to_legacy_cell(self):
        """Legacy cell carrega `score` (winner score) — usado no audit."""
        from app.pipeline.scoring_engine import cross_check_sheet
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "MTG BELUX", "of": "262108", "ov": "2410002",
                "modelo": "OMEGA 1500 H", "comp_mm": "1500",
            }],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        of_legacy = result["rows"][0]["fields"]["of"]
        assert of_legacy.get("score") is not None
        assert of_legacy["score"] >= 3

    def test_of_empty_autofills_with_winner(self):
        """OCR vazio em OF: motor preenche com a do winner (snapped autofill)."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "", "cliente": "MTG BELUX", "ov": "2410002",
                "modelo": "OMEGA 1500 H",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        of_cell = scoring["rows"][0]["fields"]["of"]
        if of_cell.get("status") != "NA":
            assert of_cell["status"] == "snapped"
            assert of_cell["value"] == "262108"

    def test_modelo_o_zero_first_token_snaps_to_plan_designacao(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262200": [{
                    "ov": "2410200",
                    "cliente": "ACME",
                    "designacao": "CGC2E10D - COLUNA TRONCO CONICA 10M",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262200",
                "cliente": "ACME",
                "modelo": "CGC2E1OD",  # OCR O em vez de zero
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == "CGC2E10D - COLUNA TRONCO CONICA 10M"

    def test_modelo_i_one_letter_context_is_very_different(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262211": [{
                    "ov": "2410211",
                    "cliente": "ACME",
                    "designacao": "CGCAE05DI - FURACAO",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262211",
                "cliente": "ACME",
                "modelo": "CGCAE05D1",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "very_different"
        assert modelo["value"] == "CGCAE05DI - FURACAO"
        assert modelo["source"] == "plan"

    def test_modelo_i_one_digit_context_can_snap_by_similarity(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262212": [{
                    "ov": "2410212",
                    "cliente": "ACME",
                    "designacao": "CGC2E06DI - 1 PRIORIDADE",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262212",
                "cliente": "ACME",
                "modelo": "CGC2E06D1-2ªPRIORIDADE",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == "CGC2E06DI - 1 PRIORIDADE"
        assert modelo["source"] == "plan"

    def test_modelo_missing_i_before_v_is_very_different(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262213": [{
                    "ov": "2410213",
                    "cliente": "ACME",
                    "designacao": "CLCAF06DI_V - PONTEIRA",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262213",
                "cliente": "ACME",
                "modelo": "CLCAF06DV",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "very_different"
        assert modelo["value"] == "CLCAF06DI_V - PONTEIRA"
        assert modelo["source"] == "plan"

    def test_modelo_missing_i_before_v_numeric_difference_can_snap(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262214": [{
                    "ov": "2410214",
                    "cliente": "ACME",
                    "designacao": "CFC5F45RI_V",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262214",
                "cliente": "ACME",
                "modelo": "CFC5F05RiV",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == "CFC5F45RI_V"
        assert modelo["source"] == "plan"

    @pytest.mark.parametrize(
        ("ocr_modelo", "plan_designacao"),
        [
            ("0641-S-515", "36044610 - 0641S515 + 2 PORTAS"),
            ("CA06F18D N1", "CAO6F18D - Nº1 CI7012A4500 - CI70H500"),
            ("CONIPROT.", "2203VF00 - 2203V500 - CONIPROT89100119 - FL PL"),
        ],
    )
    def test_modelo_compact_code_inside_designacao_snaps(self, ocr_modelo, plan_designacao):
        refs = {
            "available": True,
            "of_to_entries": {
                "262210": [{
                    "ov": "2410210",
                    "cliente": "ACME",
                    "designacao": plan_designacao,
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262210",
                "cliente": "ACME",
                "modelo": ocr_modelo,
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == plan_designacao

    @pytest.mark.parametrize("ocr_modelo", ["OMEGA 1200 H", "OMEGA 1300 H"])
    def test_modelo_same_family_different_number_substitutes_winner(self, ocr_modelo):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262108",
                "cliente": "MTG BELUX",
                "modelo": ocr_modelo,
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        result = cross_check_sheet(sheet_data, None, _REFS)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == "OMEGA 1500 H"
        assert modelo["source"] == "plan"
        legacy = result["rows"][0]["fields"]["modelo"]
        assert legacy["status"] == "MATCH"
        assert legacy["ref"] == "OMEGA 1500 H"

    def test_modelo_o_zero_numeric_group_still_snaps(self):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262108",
                "cliente": "MTG BELUX",
                "modelo": "OMEGA 150O H",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "snapped"
        assert modelo["value"] == "OMEGA 1500 H"

    def test_modelo_similar_but_different_first_token_substitutes_winner(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "262201": [{
                    "ov": "2410201",
                    "cliente": "ACME",
                    "designacao": "B713UP01 - COLUNA",
                }],
            },
            "clientes_plan": frozenset({"ACME"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262201",
                "cliente": "ACME",
                "modelo": "B713U503",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        modelo = scoring["rows"][0]["fields"]["modelo"]

        assert modelo["status"] == "very_different"
        assert modelo["value"] == "B713UP01 - COLUNA"
        assert modelo["source"] == "plan"


class TestAcabamentoPreservesOperatorReference:
    """Acabamento TPL086 também segue o winner global R213."""

    def test_acabamento_single_of_fills_non_empty_modelo_with_winner(self):
        sheet_data = {
            "template_name": "acabamento", "header": {}, "footer": {},
            "rows": [{
                # of existe no plan (262108 → designacao "OMEGA 1500 H"); o modelo
                # OCR diverge da designacao do plan de propósito.
                "of": "262108", "modelo": "PEÇA-X", "qtd": "4",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        assert fields["of"]["value"] == "262108"
        assert fields["of"]["source"] == "plan"

        assert fields["modelo"]["value"] == "OMEGA 1500 H"
        assert fields["modelo"]["source"] == "plan"
        assert fields["modelo"]["status"] == "very_different"
        assert scoring["rows"][0]["winner_of"] == "262108"

        from app.pipeline.scoring_engine import cross_check_sheet

        result = cross_check_sheet(sheet_data, None, _REFS)
        modelo = result["rows"][0]["fields"]["modelo"]
        assert modelo["source"] == "plan"
        assert modelo["ref_source"] == "plan"
        assert modelo.get("ref", "") == "OMEGA 1500 H"
        assert {
            "section": "rows",
            "row_index": 0,
            "field": "modelo",
            "field_path": "rows[0].modelo",
            "value": "PEÇA-X",
            "ref": "OMEGA 1500 H",
            "ref_source": "plan",
            "reason": "Motor propõe valor muito diferente do OCR",
        } in _sans_r243(result["to_analisar"])

    def test_acabamento_soft_of_and_model_errors_snap_with_winner(self):
        refs = {
            "available": True,
            "of_to_entries": {
                "257504": [{
                    "ov": "250010",
                    "cliente": "MTG",
                    "designacao": "CAO8E10B - ESP 3",
                }],
            },
            "clientes_plan": frozenset({"MTG"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "acabamento", "header": {}, "footer": {},
            "rows": [{"of": "957504", "modelo": "CA08E10B", "qtd": "4"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        row = scoring["rows"][0]
        fields = row["fields"]

        assert row["winner_of"] == "257504"
        assert row["winner_score"] is not None
        assert fields["of"]["value"] == "257504"
        assert fields["of"]["status"] == "snapped"
        # R222/D8 — modelo no Acabamento volta à designação COMPLETA do plan.
        assert fields["modelo"]["value"] == "CAO8E10B - ESP 3"
        assert fields["modelo"]["status"] == "snapped"
        assert fields["qtd"]["status"] == "confirmed"

    def test_bobine_fills_written_identity_from_winner(self):
        """Sanidade: Bobine também mostra a OF da melhor linha."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "999999", "ov": "2410002",
                "cliente": "MTG BELUX", "modelo": "OMEGA 1500 H",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        cell = scoring["rows"][0]["fields"]["of"]
        assert cell["value"] == "262108"
        assert cell["status"] == "very_different"
        assert cell["source"] == "plan"


class TestLaserDbaseDtopo:
    def test_dbase_dtopo_use_numeric_formatting_and_diff_rules(self):
        assert _format_value("dbase", 1000.0) == "1000"
        assert _format_value("dtopo", "1200.0") == "1200"

        assert _is_very_different("dbase", "1000", "1200") is True
        assert _is_very_different("dtopo", "1000", "1200") is True

    def test_dbase_dtopo_without_canonical_values_go_to_review(self):
        """R223 — dbase/dtopo preenchidos mas a única entry do plano não tem
        esses campos (logo não concordam em nada): sem winner, as células ficam
        vermelhas (very_different) para revisão, em vez de forçar um winner sem
        valor canónico e pintar de verde-confiante."""
        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [{
                    "ov": "2410001",
                    "cliente": "ELECNOR",
                    "designacao": "OMEGA 1200 H",
                    "comp": 1200,
                }],
            },
            "clientes_plan": frozenset({"ELECNOR"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "laser",
            "header": {},
            "footer": {},
            "rows": [{"dbase": "1000", "dtopo": "1200"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        fields = scoring["rows"][0]["fields"]

        assert scoring["rows"][0]["winner_of"] is None
        assert scoring["rows"][0]["winner_mode"] is None
        assert fields["dbase"]["status"] == "very_different"
        assert fields["dtopo"]["status"] == "very_different"


class TestNoRefTemplateFields:
    def test_gemini_specific_fields_validate_by_local_rule(self):
        sheet_data = {
            "template_name": "gasparini",
            "header": {}, "footer": {},
            "rows": [{
                "pf": "PF1", "cliente": "ELECNOR", "of": "262107",
                "modelo": "OMEGA 1200 H", "cf": "C1", "m2": "12,5",
                "qtd": "2", "nesting": "N123", "inicio": "08:00",
                "fim": "09:00", "np": "NP9",
            }],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        for field in ("pf", "cf", "m2", "nesting", "np"):
            assert fields[field]["status"] == "confirmed"
            assert fields[field]["source"] == "syntax"

    def test_invalid_gemini_numeric_with_winner_is_forced_rule_match(self):
        # R223 — precisa de uma identidade real do plano (of) para haver winner;
        # é o winner que perdoa o `m2` ilegível como MATCH_REGRA_FORCADO.
        sheet_data = {
            "template_name": "gasparini",
            "header": {}, "footer": {},
            "rows": [{"of": "262107", "m2": "ABC"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        m2 = result["rows"][0]["fields"]["m2"]

        assert m2["value"] == "ABC"
        assert m2["status"] == "MATCH"
        assert m2["ref_source"] == "syntax"
        assert m2["match_kind"] == "MATCH_REGRA_FORCADO"
        assert not result["to_analisar"]

    def test_invalid_sobras_with_winner_is_forced_rule_match(self):
        # R223 — `of` real dá winner; o winner perdoa o `sobras` ilegível.
        sheet_data = {
            "template_name": "manual",
            "header": {}, "footer": {},
            "rows": [{"of": "262107", "qtd": "5", "sobras": "ABC"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        sobras = result["rows"][0]["fields"]["sobras"]

        assert result["rows"][0]["fields"]["qtd"]["status"] == "MATCH"
        assert sobras["value"] == "ABC"
        assert sobras["status"] == "MATCH"
        assert sobras["ref_source"] == "syntax"
        assert sobras["match_kind"] == "MATCH_REGRA_FORCADO"
        assert not result["to_analisar"]

    def test_plausible_sobras_no_ref_field_confirms(self):
        sheet_data = {
            "template_name": "robot",
            "header": {}, "footer": {},
            "rows": [{"qtd": "5", "sobras": "2,5"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        assert scoring["rows"][0]["fields"]["sobras"]["status"] == "confirmed"

    def test_invalid_cesta_n_with_winner_is_forced_rule_match(self):
        # R223 — `of` real dá winner; o winner perdoa o `cesta_n` ilegível.
        sheet_data = {
            "template_name": "expedicao",
            "header": {}, "footer": {},
            "rows": [{"of": "262107", "qtd": "5", "cesta_n": "ABC"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        cesta = result["rows"][0]["fields"]["cesta_n"]

        assert cesta["value"] == "ABC"
        assert cesta["status"] == "MATCH"
        assert cesta["ref_source"] == "syntax"
        assert cesta["match_kind"] == "MATCH_REGRA_FORCADO"
        assert not result["to_analisar"]

    def test_plausible_cesta_n_no_ref_field_confirms(self):
        for value in ("12", "CESTA 12"):
            scoring, *_ = shadow_score(
                {
                    "template_name": "expedicao",
                    "header": {}, "footer": {},
                    "rows": [{"qtd": "5", "cesta_n": value}],
                },
                None,
                _REFS,
            )
            assert scoring["rows"][0]["fields"]["cesta_n"]["status"] == "confirmed"


class TestTemplateCrossCheckContract:
    def test_row_field_not_declared_for_cross_check_uses_local_rule(self, monkeypatch):
        import app.templates_registry as registry

        class _FakeTemplate:
            name = "fake_template"
            row_fields = ("cliente", "of")
            cross_check_fields = ("of",)

        monkeypatch.setattr(registry, "get_template", lambda _name: _FakeTemplate())
        sheet_data = {
            "template_name": "fake_template",
            "header": {},
            "footer": {},
            "rows": [{"cliente": "ELECNOR", "of": "262107"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        fields = scoring["rows"][0]["fields"]

        assert fields["of"]["status"] == "confirmed"
        assert fields["cliente"]["status"] == "confirmed"


class TestDeclaredCross:
    """Cross declarado (fase A) — campos custom cruzados contra colunas
    extra do plano (winner["extra"]), informativo puro. Contratos fixados:
    value=OCR sempre; source=ref_source="declared_plan"; nunca "snapped";
    ranking byte-idêntico com/sem declarado."""

    @staticmethod
    def _refs(extra):
        refs = {**_REFS, "of_to_entries": {
            of: [dict(e) for e in entries]
            for of, entries in _REFS["of_to_entries"].items()}}
        refs["of_to_entries"]["262107"][0]["extra"] = extra
        return refs

    @staticmethod
    def _install_fake(monkeypatch, declared):
        import app.templates_registry as registry

        class _FakeTemplate:
            name = "fake_template"
            row_fields = ("cliente", "of", "pbase", "obs")
            cross_check_fields = ("of",)
            declared_cross = declared

        monkeypatch.setattr(
            registry, "get_template", lambda _name: _FakeTemplate())

    @staticmethod
    def _declared(**over):
        from app.templates_registry import DeclaredRef
        d = {"pbase": DeclaredRef(column="pbase", cmp="num", tol=2.0),
             "obs": DeclaredRef(column="obs", cmp="text")}
        d.update(over)
        return d

    def _score(self, monkeypatch, row, extra, declared=None):
        self._install_fake(
            monkeypatch, self._declared() if declared is None else declared)
        sheet = {"template_name": "fake_template", "header": {},
                 "footer": {}, "rows": [row]}
        scoring, *_ = shadow_score(sheet, None, self._refs(extra))
        return scoring["rows"][0]["fields"]

    _ROW = {"cliente": "ELECNOR", "of": "262107"}
    _EXTRA = {"pbase": 250.0, "obs": "REFORÇO X"}

    def _assert_declared_contract(self, cell, ocr_value):
        assert cell["source"] == "declared_plan"
        assert cell["ref_source"] == "declared_plan"
        assert cell["value"] == ocr_value      # NUNCA o valor do plano
        assert cell["status"] != "snapped"     # nunca a porta de escrita

    def test_num_within_tol_confirmed(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "251"}, self._EXTRA)
        cell = fields["pbase"]
        assert cell["status"] == "confirmed"
        assert cell["proposed"] == "250"       # 250.0 do openpyxl → "250"
        self._assert_declared_contract(cell, "251")

    def test_num_decimal_comma_confirmed(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "250,0"}, self._EXTRA)
        assert fields["pbase"]["status"] == "confirmed"

    def test_num_outside_tol_very_different(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "260"}, self._EXTRA)
        cell = fields["pbase"]
        assert cell["status"] == "very_different"
        assert cell["proposed"] == "250"
        self._assert_declared_contract(cell, "260")

    def test_num_ocr_not_numeric_very_different(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "ilegível"}, self._EXTRA)
        cell = fields["pbase"]
        assert cell["status"] == "very_different"
        assert cell["proposed"] == "250"

    def test_num_ref_not_numeric_is_na(self, monkeypatch):
        # declaração "num" numa coluna com texto — desalinhada, NA
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "250"},
            {"pbase": "texto livre"})
        cell = fields["pbase"]
        assert cell["status"] == "NA"
        self._assert_declared_contract(cell, "250")

    def test_text_accent_insensitive_confirmed(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "obs": "reforco x"}, self._EXTRA)
        cell = fields["obs"]
        assert cell["status"] == "confirmed"
        assert cell["proposed"] == "REFORÇO X"
        self._assert_declared_contract(cell, "reforco x")

    def test_text_garbled_above_threshold_confirmed(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "obs": "REFORQO X"}, self._EXTRA)
        assert fields["obs"]["status"] == "confirmed"
        assert fields["obs"]["score"] >= 0.55

    def test_text_unrelated_very_different(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "obs": "AVEIRO"}, self._EXTRA)
        cell = fields["obs"]
        assert cell["status"] == "very_different"
        assert cell["proposed"] == "REFORÇO X"

    def test_no_winner_is_na(self, monkeypatch):
        fields = self._score(
            monkeypatch, {"cliente": "", "of": "", "pbase": "250"},
            self._EXTRA)
        cell = fields["pbase"]
        assert cell["status"] == "NA"
        assert cell["source"] == "declared_plan"

    def test_column_missing_from_extra_is_na(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": "250"}, {"obs": "X"})
        assert fields["pbase"]["status"] == "NA"

    def test_empty_ocr_is_na(self, monkeypatch):
        fields = self._score(
            monkeypatch, {**self._ROW, "pbase": ""}, self._EXTRA)
        assert fields["pbase"]["status"] == "NA"

    def test_legacy_funnel_and_review_reason(self, monkeypatch):
        self._install_fake(monkeypatch, self._declared())
        sheet = {"template_name": "fake_template", "header": {},
                 "footer": {},
                 "rows": [{**self._ROW, "pbase": "260"}]}
        result = cross_check_sheet(sheet, None, self._refs(self._EXTRA))
        cell = result["rows"][0]["fields"]["pbase"]
        assert cell["status"] == "NO_MATCH"
        assert cell["engine_status"] == "very_different"
        assert cell["ref"] == "250"
        assert cell["source"] == "declared_plan"
        assert cell["ref_source"] == "declared_plan"
        assert cell["snapped"] is False
        items = [i for i in result["to_analisar"] if i["field"] == "pbase"]
        assert items and items[0]["reason"] == (
            "Plano (cross informativo) tem valor diferente do OCR")

    def test_ranking_byte_identical_with_and_without_declared(
            self, monkeypatch):
        """Invariante central: o declarado NÃO altera winner/candidatos —
        todas as células não-declaradas e a metadata do winner são
        byte-idênticas com e sem declared_cross no template."""
        import json

        sheet = {"template_name": "fake_template", "header": {},
                 "footer": {},
                 "rows": [{**self._ROW, "pbase": "260", "obs": "AVEIRO"}]}
        refs = self._refs(self._EXTRA)

        self._install_fake(monkeypatch, self._declared())
        scoring_with, *_ = shadow_score(sheet, None, refs)
        self._install_fake(monkeypatch, {})
        scoring_without, *_ = shadow_score(sheet, None, refs)

        row_with = scoring_with["rows"][0]
        row_without = scoring_without["rows"][0]
        # metadata do winner byte-idêntica (tudo menos as células)
        meta_with = {k: v for k, v in row_with.items() if k != "fields"}
        meta_without = {k: v for k, v in row_without.items() if k != "fields"}
        assert json.dumps(meta_with, sort_keys=True, default=str) == \
            json.dumps(meta_without, sort_keys=True, default=str)
        # células não-declaradas byte-idênticas
        for f in ("cliente", "of"):
            assert json.dumps(row_with["fields"][f], sort_keys=True) == \
                json.dumps(row_without["fields"][f], sort_keys=True)
        # sem declarado, o campo custom cai na regra local (não declared)
        assert row_without["fields"]["pbase"]["source"] != "declared_plan"
        assert row_with["fields"]["pbase"]["source"] == "declared_plan"


class TestDeclaredVote:
    """Fase C-lite ("dv") — voto de campos declarados na escolha da linha.

    Invariantes de "não piora" por construção: termo one-sided ∈ [0, cap];
    cap < margem decisiva ⇒ flip nunca sai strong; posterior subtrai o
    bónus (calibração intacta); realinhamento nunca vê o termo; inativo ⇒
    byte-idêntico."""

    @staticmethod
    def _set_env(monkeypatch, mode):
        import app.config as config

        class _S:
            cross_declared_vote = mode

        monkeypatch.setattr(config, "get_settings", lambda: _S())

    @staticmethod
    def _install_fake(monkeypatch, declared):
        import app.templates_registry as registry

        class _FakeTemplate:
            name = "fake_dv"
            row_fields = ("cliente", "ov", "of", "destino_col", "pbase")
            cross_check_fields = ("of", "ov", "cliente")
            declared_cross = declared

        monkeypatch.setattr(
            registry, "get_template", lambda _name: _FakeTemplate())

    @staticmethod
    def _declared(vote=True):
        from app.templates_registry import DeclaredRef
        return {
            "destino_col": DeclaredRef(column="destino", cmp="text",
                                       vote=vote),
            "pbase": DeclaredRef(column="pbase", cmp="num", tol=2.0,
                                 vote=vote),
        }

    @staticmethod
    def _refs():
        """Duas OFs gémeas (evidência canónica simétrica) que só diferem
        nas colunas declaradas — o caso de empate que o dv deve decidir."""
        base = {"cliente": "ELECNOR", "designacao": "OMEGA 1200 H",
                "comp": 1200, "lbase": 50, "ltopo": 30, "esp": 2.6,
                "material": "S355"}
        return {
            "available": True,
            "of_to_entries": {
                "262107": [{**base, "ov": "2410001",
                            "extra": {"destino": "GUILHOTINA 9M",
                                      "pbase": 250.0}}],
                "262108": [{**base, "ov": "2410002",
                            "extra": {"destino": "LASER 5",
                                      "pbase": 300.0}}],
            },
            "of_to_ovs": {"262107": frozenset({"2410001"}),
                          "262108": frozenset({"2410002"})},
            "lotes_sap_full": {},
            "clientes_plan": frozenset({"ELECNOR"}),
        }

    def _score(self, monkeypatch, row, *, env="on", vote=True,
               variant=None):
        from app.pipeline.scoring_engine import (
            SCORING_VARIANT, shadow_score,
        )
        self._set_env(monkeypatch, env)
        self._install_fake(monkeypatch, self._declared(vote=vote))
        sheet = {"template_name": "fake_dv", "header": {}, "footer": {},
                 "rows": [row]}
        token = SCORING_VARIANT.set(variant) if variant else None
        try:
            scoring, *_ = shadow_score(sheet, None, self._refs())
        finally:
            if token is not None:
                SCORING_VARIANT.reset(token)
        return scoring["rows"][0]

    # --- invariante estrutural -------------------------------------------
    def test_cap_below_decisive_margin(self):
        from app.pipeline.scoring_engine import (
            _DECLARED_VOTE_CAP_BITS, _FS_MARGIN_DECISIVE,
        )
        assert _DECLARED_VOTE_CAP_BITS < _FS_MARGIN_DECISIVE

    # --- T1: inativo ⇒ byte-idêntico --------------------------------------
    @pytest.mark.parametrize("env,vote,variant", [
        ("on", False, None),        # sem vote no spec
        ("off", True, None),        # env off
        ("shadow", True, None),     # shadow sem feature +dv na variante
    ])
    def test_inactive_is_byte_identical(self, monkeypatch, env, vote,
                                        variant):
        import json as _json
        row = {"cliente": "ELECNOR", "destino_col": "GUILHOTINA 9M"}
        baseline = self._score(monkeypatch, row, env="off", vote=False)
        candidate = self._score(monkeypatch, row, env=env, vote=vote,
                                variant=variant)
        assert _json.dumps(baseline, sort_keys=True, default=str) == \
            _json.dumps(candidate, sort_keys=True, default=str)

    # --- T2: sentinela da variante composta -------------------------------
    def test_variant_suffix_neutral_without_votes(self, monkeypatch):
        import json as _json
        row = {"cliente": "ELECNOR", "of": "262107"}
        plain = self._score(monkeypatch, row, env="off", vote=False,
                            variant="v30")
        suffixed = self._score(monkeypatch, row, env="off", vote=False,
                               variant="v30+dv")
        assert _json.dumps(plain, sort_keys=True, default=str) == \
            _json.dumps(suffixed, sort_keys=True, default=str)

    def test_variant_helpers(self):
        from app.pipeline.scoring_engine import (
            _variant_base, _variant_features,
        )
        assert _variant_base("v30+dv") == "v30"
        assert _variant_base("v30cal+dv") == "v30cal"
        assert _variant_features("v30+dv") == frozenset({"dv"})
        assert _variant_features("v30") == frozenset()

    # --- T3: termo one-sided ∈ [0, cap] ------------------------------------
    def test_entry_votes_bounded_one_sided(self):
        from app.pipeline.scoring_engine import _declared_entry_votes
        dv = {"votes": (
            ("a", "col_a", "text", 0.0, "X", 1.5),
            ("b", "col_b", "text", 0.0, "Y", 1.5),
            ("c", "col_c", "num", 2.0, 100.0, 1.5),
        ), "cap": 2.0}
        # tudo concorda → soma 4.5 mas cap CONJUNTO = 2.0
        entry = {"extra": {"col_a": "X", "col_b": "Y", "col_c": 101.0}}
        bonus, detail = _declared_entry_votes(entry, dv, with_detail=True)
        assert bonus == 2.0
        assert all(d["agree"] for d in detail)
        # mismatch/coluna vazia → 0, nunca negativo
        for extra in ({}, {"col_a": "ERRADO"}, {"col_a": ""},
                      {"col_c": 103.0}):
            bonus, _ = _declared_entry_votes({"extra": extra}, dv)
            assert bonus == 0.0

    # --- T4 (direção): gap canónico grande ⇒ winner imutável ---------------
    def test_adversarial_cannot_beat_canonical_evidence(self, monkeypatch):
        # OV escrita aponta 262108 (evidência forte); o declarado concorda
        # com a RIVAL 262107 — o winner não pode mudar (gap ≫ cap).
        row = {"cliente": "ELECNOR", "ov": "2410002",
               "destino_col": "GUILHOTINA 9M", "pbase": "250"}
        out = self._score(monkeypatch, row, env="on", vote=True)
        assert out["winner_of"] == "262108"

    # --- T5: dv decide empates; flip nunca sai strong ----------------------
    def test_tie_decided_by_vote_is_weak_guess(self, monkeypatch):
        row = {"cliente": "ELECNOR", "destino_col": "GUILHOTINA 9M"}
        out = self._score(monkeypatch, row, env="on", vote=True)
        assert out["winner_of"] == "262107"     # a entry que concorda
        assert out["winner_mode"] == "weak_guess"
        assert out["winner_declared_vote_bits"] and \
            out["winner_declared_vote_bits"] <= 2.0
        votes = {d["field"]: d for d in out["winner_declared_vote"]}
        assert votes["destino_col"]["agree"] is True
        # reason auditável pós-slice
        assert any(r.get("field") == "declared:destino_col"
                   for r in out["winner_score_reasons"])

    def test_tie_decided_by_num_vote(self, monkeypatch):
        row = {"cliente": "ELECNOR", "pbase": "299"}  # ±2 → 300 (262108)
        out = self._score(monkeypatch, row, env="on", vote=True)
        assert out["winner_of"] == "262108"
        assert out["winner_mode"] == "weak_guess"

    # --- T7: posterior/calibração intactos (sem flip) ----------------------
    def test_posterior_invariant_without_flip(self, monkeypatch):
        row = {"cliente": "ELECNOR", "ov": "2410002",
               "destino_col": "LASER 5"}   # declarado concorda com o winner
        with_dv = self._score(monkeypatch, row, env="on", vote=True)
        without = self._score(monkeypatch, row, env="off", vote=False)
        assert with_dv["winner_of"] == without["winner_of"] == "262108"
        for key in ("winner_p_of", "winner_p_h0", "winner_p_field",
                    "winner_posterior_entropy_bits"):
            assert with_dv.get(key) == without.get(key), key

    # --- T8: auto-proteção (u alto ⇒ w=0) -----------------------------------
    def test_common_value_gets_zero_weight(self, monkeypatch):
        from app.pipeline.scoring_engine import (
            _fs_row_context, _get_indices,
        )
        # 600 entries com o MESMO destino → u=600/1000=0.6 > m=0.5 → w=0
        entries = {
            str(100000 + i): [{"ov": f"24{i:05d}", "cliente": f"C{i}",
                               "designacao": f"D{i}", "comp": 1000 + i,
                               "extra": {"destino": "COMUM"}}]
            for i in range(600)
        }
        entries["999999"] = [{"ov": "2499999", "cliente": "Z",
                              "designacao": "DZ", "comp": 9999,
                              "extra": {"destino": "RARISSIMO"}}]
        refs = {"available": True, "of_to_entries": entries,
                "of_to_ovs": {}, "lotes_sap_full": {},
                "clientes_plan": frozenset()}
        idx = _get_indices(refs)
        dv = {"specs": (("destino_col", "destino", "text", 0.0, 0.5),),
              "cap": 2.0}
        ctx = _fs_row_context({"destino_col": "COMUM"}, idx,
                              declared_vote=dv)
        assert ctx["declared"] is None          # valor comum não arma voto
        ctx = _fs_row_context({"destino_col": "RARISSIMO"}, idx,
                              declared_vote=dv)
        assert ctx["declared"] is not None      # valor raro arma, capado
        assert ctx["declared"]["votes"][0][5] <= 2.0

    # --- T9: o dv não cria winners do nada ----------------------------------
    def test_declared_only_row_has_no_winner(self, monkeypatch):
        row = {"destino_col": "GUILHOTINA 9M"}
        out = self._score(monkeypatch, row, env="on", vote=True)
        assert out["winner_of"] is None
        assert out["fields"]["destino_col"]["status"] == "NA"

    # --- T10: realinhamento nunca vê o termo --------------------------------
    def test_alignment_signature_has_no_declared_vote(self):
        import inspect
        from app.pipeline.scoring_engine import _choose_row_alignment
        assert "declared_vote" not in inspect.signature(
            _choose_row_alignment).parameters

    # --- contrato de segurança da célula com voto ativo ---------------------
    def test_cell_contract_with_vote_on(self, monkeypatch):
        from app.pipeline.scoring_engine import cross_check_sheet
        self._set_env(monkeypatch, "on")
        self._install_fake(monkeypatch, self._declared(vote=True))
        sheet = {"template_name": "fake_dv", "header": {}, "footer": {},
                 "rows": [{"cliente": "ELECNOR", "ov": "2410001",
                           "destino_col": "VALOR ERRADO"}]}
        result = cross_check_sheet(sheet, None, self._refs())
        cell = result["rows"][0]["fields"]["destino_col"]
        assert cell["value"] == "VALOR ERRADO"      # value = OCR sempre
        assert cell["engine_status"] == "very_different"
        assert cell["source"] == "declared_plan"
        assert cell["snapped"] is False
        assert cell["vote"] is True                 # marcador do voto
        items = [i for i in result["to_analisar"]
                 if i["field"] == "destino_col"]
        assert items and "contou para a escolha da linha" in items[0]["reason"]

    def test_cell_vote_marker_absent_when_inactive(self, monkeypatch):
        from app.pipeline.scoring_engine import cross_check_sheet
        self._set_env(monkeypatch, "off")
        self._install_fake(monkeypatch, self._declared(vote=True))
        sheet = {"template_name": "fake_dv", "header": {}, "footer": {},
                 "rows": [{"cliente": "ELECNOR", "ov": "2410001",
                           "destino_col": "GUILHOTINA 9M"}]}
        result = cross_check_sheet(sheet, None, self._refs())
        assert "vote" not in result["rows"][0]["fields"]["destino_col"]


class TestToAnalisarCoverage:
    def test_header_no_match_enters_review_queue(self):
        from app.pipeline.scoring_engine import cross_check_sheet

        refs = {
            **_REFS,
            "colaboradores": {
                "0000000537": {"sname": "AUGUSTO MONTEIRO", "pernr": "0000000537"},
            },
        }
        sheet_data = {
            "template_name": "bobine_formato",
            "header": {"operador": "OPERADOR DESCONHECIDO"},
            "footer": {},
            "rows": [],
        }

        result = cross_check_sheet(sheet_data, None, refs)

        assert result["summary"]["no_match"] == 1
        assert _sans_r243(result["to_analisar"]) == [{
            "section": "header",
            "row_index": None,
            "field": "operador",
            "field_path": "header.operador",
            "value": "OPERADOR DESCONHECIDO",
            "ref": "",
            "ref_source": "colaboradores",
            "reason": "Valor não encontrado na ListaColaboradores",
        }]


class TestR132MaqFustes:
    """R132 — Novo template TPL103 MÁQUINA DE FUSTES (frente + verso).
    Cobre: detect_template, header dinâmico com turno, paragens NA,
    cross-check por regra local em campos sem ref."""

    def test_detect_template_maq_fustes_default_frente(self):
        """`MÁQUINA DE FUSTES` (qualquer variante) → frente, NUNCA verso."""
        from app.templates_registry import detect_template
        assert detect_template("MÁQUINA DE FUSTES").name == "maq_fustes"
        assert detect_template("MAQUINA DE FUSTES").name == "maq_fustes"
        assert detect_template("MAQ FUSTES").name == "maq_fustes"
        assert detect_template("MAQ DE FUSTES").name == "maq_fustes"

    def test_maq_fustes_paragens_explicit_get_only(self):
        """`maq_fustes_paragens` só obtém-se via `get_template` directo,
        nunca por `detect_template` (setor_aliases=())."""
        from app.templates_registry import detect_template, get_template
        assert detect_template("MÁQUINA DE FUSTES").name != "maq_fustes_paragens"
        v = get_template("maq_fustes_paragens")
        assert v.name == "maq_fustes_paragens"
        assert not v.has_production_rows
        assert v.tpl_code == "TPL103"
        assert v.row_fields == ("motivo", "inicio", "fim", "duracao", "resolvido")

    def test_maq_fustes_has_turno_in_header(self):
        from app.templates_registry import get_template
        assert "turno" in get_template("maq_fustes").header_fields
        assert "turno" in get_template("maq_fustes_paragens").header_fields

    def test_dynamic_header_skel_includes_turno(self):
        """R132 fix do bug pré-existente: o header_skel agora inclui turno
        quando o template o tem em header_fields. Antes do refactor,
        acabamento e maq_fustes nunca recebiam turno do OCR."""
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template
        prompt = build_prompt(get_template("maq_fustes"))
        assert '"turno"' in prompt
        assert "M | R | XM | T" in prompt
        # qtd_metros vem na coluna line via _FIELD_LABELS
        assert "QTD (METROS)" in prompt

    def test_production_prompt_uses_ferramenta_contract(self):
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template

        prompt = build_prompt(get_template("bobine_formato"))

        assert "FERRAMENTA / CONI" in prompt
        assert "CONI, TORRES, OCT, CIL, CIO, CIB" in prompt
        assert '"OCT."' not in prompt
        assert "text (T, OCT, TORRES)" not in prompt

    def test_acabamento_prompt_uses_tpl086_contract(self):
        """Acabamento usa o formato TPL086: OF/REFERÊNCIA-PEÇA/QTD."""
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template

        tpl = get_template("acabamento_mtg2")
        prompt = build_prompt(tpl)

        assert tpl.name == "acabamento"
        assert tpl.row_fields == ("of", "modelo", "qtd")
        assert tpl.footer_fields == ("colunas_produzidas",)
        assert '"turno"' in prompt
        assert "OF | REFERÊNCIA / PEÇA | QTD" in prompt
        assert "TOTAL QTD" in prompt
        assert '"modelo":""' in prompt
        assert "FERR." not in prompt
        assert '"coni"' not in prompt

    def test_maq_fustes_paragens_validate_by_local_rule(self):
        """Paragens preenchidas válidas ficam comparáveis por regra local."""
        sheet_data = {
            "template_name": "maq_fustes_paragens",
            "header": {"operador": "X", "data": "10-05-2026"},
            "footer": {},
            "rows": [{"motivo": "AVARIA HIDRAULICA", "inicio": "08:30",
                      "fim": "09:00", "duracao": "00:30", "resolvido": "SIM"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        for f in ("motivo", "inicio", "fim", "duracao", "resolvido"):
            assert scoring["rows"][0]["fields"][f]["status"] == "confirmed"

    def test_maq_fustes_paragens_invalid_syntax_goes_to_review(self):
        sheet_data = {
            "template_name": "maq_fustes_paragens",
            "header": {},
            "footer": {},
            "rows": [{
                "motivo": "AVARIA HIDRAULICA",
                "inicio": "99:99",
                "fim": "texto",
                "duracao": "24:01",
                "resolvido": "talvez",
            }],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        fields = result["rows"][0]["fields"]

        assert fields["motivo"]["status"] == "MATCH"
        for field in ("inicio", "fim", "duracao", "resolvido"):
            assert fields[field]["status"] == "NO_MATCH"
            assert fields[field]["ref_source"] == "syntax"
        assert {item["field_path"] for item in _sans_r243(result["to_analisar"])} == {
            "rows[0].inicio",
            "rows[0].fim",
            "rows[0].duracao",
            "rows[0].resolvido",
        }

    def test_maq_fustes_qtd_metros_confirms_by_local_rule(self):
        """qtd_metros informativo preenchido valida por regra local."""
        sheet_data = {
            "template_name": "maq_fustes",
            "header": {}, "footer": {},
            "rows": [{"cliente": "ELECNOR", "of": "262107", "ov": "2410001",
                      "modelo": "OMEGA 1200 H", "qtd": "5", "qtd_metros": "12.5"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        assert scoring["rows"][0]["fields"]["qtd_metros"]["status"] == "confirmed"

    def test_maq_fustes_invalid_qtd_metros_with_winner_is_forced_rule_match(self):
        # R223 — `of` real dá winner; o winner perdoa o `qtd_metros` ilegível.
        sheet_data = {
            "template_name": "maq_fustes",
            "header": {}, "footer": {},
            "rows": [{"of": "262107", "qtd_metros": "ABC"}],
        }

        result = cross_check_sheet(sheet_data, None, _REFS)
        qtd_metros = result["rows"][0]["fields"]["qtd_metros"]

        assert qtd_metros["status"] == "MATCH"
        assert qtd_metros["ref_source"] == "syntax"
        assert qtd_metros["match_kind"] == "MATCH_REGRA_FORCADO"
        assert not result["to_analisar"]

    def test_two_sided_templates_map(self, monkeypatch):
        """rev00 — TODAS as folhas de produção têm verso; o map aponta cada
        template de produção → template genérico `paragens`. Os próprios
        templates de paragens não estão no map."""
        import importlib
        import sys
        from types import SimpleNamespace

        if "app.web.ocr_runner" in sys.modules:
            ocr_runner = sys.modules["app.web.ocr_runner"]
        else:
            monkeypatch.setitem(
                sys.modules,
                "ocr6",
                SimpleNamespace(
                    PROMPT="",
                    PROMPT_HASH="",
                    load_prompt=lambda _path: ("", ""),
                    process_image=lambda *_args, **_kwargs: None,
                ),
            )
            ocr_runner = importlib.import_module("app.web.ocr_runner")
        from app.templates_registry import TEMPLATES

        m = ocr_runner.TWO_SIDED_TEMPLATES
        # maq_fustes (e qualquer produção) → paragens genérico
        assert m.get("maq_fustes") == "paragens"
        assert m.get("bobine_formato") == "paragens"
        assert m.get("gasparini") == "paragens"
        # os templates de paragens não são 2-lados
        assert "paragens" not in m
        assert "maq_fustes_paragens" not in m
        # cobre exactamente os templates de produção do registry
        assert set(m.keys()) == {
            n for n, t in TEMPLATES.items() if t.has_production_rows
        }
        assert all(v == "paragens" for v in m.values())

    def test_side_detect_prompt_mentions_both_options(self):
        """O prompt de side-detect tem que mencionar ambos os cabeçalhos
        para o Qwen poder discriminar."""
        from app.pipeline.prompt_builder import build_side_detect_prompt
        prompt = build_side_detect_prompt()
        assert "PRI" in prompt
        assert "MOTIVO DA PARAGEM" in prompt
        assert '"side"' in prompt


class TestR217NumericJunkSubstitution:
    """R217 (restore 30/05) — substitute-everything também em campos numéricos
    cujo OCR seja texto/lixo. Removido o guarda de sintaxe numérica do R216 e a
    célula review-only com `auto_apply=False`: o valor canónico do plan/SAP
    volta a substituir sempre; a divergência só afeta a cor."""

    def test_garbage_ocr_in_numeric_field_is_substituted_by_plan(self):
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "ov": "2410001", "of": "262107",
                "modelo": "OMEGA 1200 H", "comp_mm": "ABC",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        comp = scoring["rows"][0]["fields"]["comp_mm"]

        # O valor canónico do plan substitui o OCR ilegível (não fica "ABC").
        assert comp["value"] == "1200"
        assert comp["source"] == "plan"
        # Já não existe a flag review-only que bloqueava o auto-apply (R216).
        assert "auto_apply" not in comp

    def test_finish_cell_substitutes_value_for_non_numeric_ocr(self):
        from app.pipeline.scoring_engine import _finish_cell

        cell = _finish_cell("comp_mm", "abc", "1500", "plan", 5)

        assert cell["value"] == "1500"
        assert "auto_apply" not in cell


class TestR218WinnerMixAndAmbiguityGuard:
    """R218 — winner por MISTURA (contagem de acertos + soma graduada) e guarda
    de ambiguidade (rivais quase-empatados que discordam num campo → não
    substituir esse campo)."""

    def test_mix_prefers_entry_that_agrees_in_more_fields(self):
        """R223 — votação HOLÍSTICA: ganha quem concorda em MAIS campos, todos
        com peso igual (substitui o critério R218 do nº de exatos). A 262107
        concorda em 3 campos REAIS (cliente + comp + larg, dentro da tolerância)
        e a 262108 só em 1 (cliente) — as suas medidas estão TODAS um pouco fora
        da tolerância, e dimensão fora da tolerância NÃO concorda (R223: medida
        0,4 ao lado é mesmo outra medida, não conta pela cauda do decay). Logo
        ganha a 262107 (3 concordâncias reais > 1)."""
        refs = {
            "available": True,
            "of_to_entries": {
                # A: 3 exatos (cliente, comp, larg) mas lbase/ltopo/esp absurdos.
                "262107": [{
                    "ov": "A1", "cliente": "ELECNOR", "designacao": "LINHA A",
                    "comp": 1000, "larg": 200, "lbase": 9999, "ltopo": 9999, "esp": 99.0,
                }],
                # B: consistentemente perto em todos os 6 campos (agree=6).
                "262108": [{
                    "ov": "B1", "cliente": "ELECNOR", "designacao": "LINHA B",
                    "comp": 1070, "larg": 214, "lbase": 114, "ltopo": 74, "esp": 3.45,
                }],
            },
            "clientes_plan": frozenset({"ELECNOR"}),
            "lotes_sap_full": {},
        }
        idx = _get_indices(refs)
        row = {
            "cliente": "ELECNOR", "comp_mm": "1000", "larg_mm": "200",
            "lbase": "100", "ltopo": "60", "esp": "3,0",
        }

        winner = _find_winner_entry({}, row, refs, idx)

        assert winner is not None
        # Votação holística: 262107 concorda em 3 campos reais (cliente+comp+larg
        # dentro da tolerância); as medidas da 262108 estão fora da tolerância e
        # não contam → só concorda no cliente (1). Ganha a 262107.
        assert winner["_of"] == "262107"
        assert winner["_agree"] == 3

    def test_ambiguity_substitutes_winner_and_flags_red(self):
        """R219 — duas linhas com a MESMA OF e o resto ilegível: a OF confirma;
        modelo/comp (onde as candidatas discordam) são SUBSTITUÍDOS pelo valor
        da linha vencedora mas marcados very_different (vermelho/rever)."""
        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [
                    {"ov": "A1", "cliente": "X", "designacao": "POLE-A", "comp": 6000},
                    {"ov": "A2", "cliente": "X", "designacao": "POLE-B", "comp": 8000},
                ],
            },
            "clientes_plan": frozenset({"X"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "modelo": "", "comp_mm": ""}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        fields = scoring["rows"][0]["fields"]

        # OF: ambas as candidatas concordam → confirmada.
        assert fields["of"]["value"] == "262107"
        # modelo/comp: candidatas discordam → SUBSTITUI pela vencedora (POLE-A /
        # 6000, por desempate de ordem) e marca very_different para revisão.
        assert fields["modelo"]["value"] == "POLE-A"
        assert fields["modelo"]["status"] == "very_different"
        assert fields["comp_mm"]["value"] == "6000"
        assert fields["comp_mm"]["status"] == "very_different"

    def test_clear_winner_still_substitutes_illegible_field(self):
        """Sem ambiguidade (winner é líder claro), o R217 mantém-se: campo
        numérico com OCR-lixo é substituído pelo valor do plano."""
        refs = {
            "available": True,
            "of_to_entries": {
                "262107": [
                    {"ov": "A1", "cliente": "X", "designacao": "POLE-A", "comp": 6000},
                    {"ov": "A2", "cliente": "X", "designacao": "POLE-B", "comp": 8000},
                ],
            },
            "clientes_plan": frozenset({"X"}),
            "lotes_sap_full": {},
        }
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            # modelo bate POLE-A → L1 é líder claro (sem rival na margem).
            "rows": [{"of": "262107", "modelo": "POLE-A", "comp_mm": "zzz"}],
        }

        scoring, *_ = shadow_score(sheet_data, None, refs)
        comp = scoring["rows"][0]["fields"]["comp_mm"]

        assert comp["value"] == "6000"
        assert "auto_apply" not in comp


class TestContentRealign:
    """R231 — um código de modelo na coluna OF é encaminhado para o campo
    modelo (o erro mais comum: a linha desliza porque a OV vem em branco)."""

    _IDX = {
        "of_keys": {"262882", "260078"},
        "model_ft_keys": ["CGC2E06D", "CLC8F08R", "OMEGA 1200 H"],
    }

    def _realign(self, of, modelo="", ov="", pri="", tpl=None):
        return _realign_misplaced_of(
            {"of": of, "modelo": modelo, "ov": ov, "pri": pri}, self._IDX, tpl
        )

    def test_model_code_in_of_moves_to_modelo(self):
        out = self._realign("CGC2E6D", "60")        # CGC2E6D ~ CGC2E06D
        assert out["modelo"] == "CGC2E6D"           # encaminhado p/ índice de modelos
        assert out["of"] == "CGC2E6D"               # of preservado (não apaga)

    def test_model_code_with_empty_modelo_moves(self):
        out = self._realign("CLC8F09R", "")         # CLC8F09R ~ CLC8F08R
        assert out["modelo"] == "CLC8F09R"

    def test_illegible_code_stays(self):
        out = self._realign("(49566D)", "")         # sem match forte de modelo
        assert out["modelo"] == ""                  # fica como está (rever)
        assert out["of"] == "(49566D)"

    def test_numeric_of_not_touched(self):
        out = self._realign("262882", "CD18M507B")  # OF numérica válida
        assert out["modelo"] == "CD18M507B"         # Etapa 2 não dispara

    def test_real_model_in_modelo_not_overwritten(self):
        out = self._realign("CGC2E6D", "CBCBE06DI")  # já há modelo legível
        assert out["modelo"] == "CBCBE06DI"

    def test_acabamento_guard(self):
        out = self._realign("CGC2E06D", "", tpl="acabamento")
        assert out["modelo"] == ""                  # acabamento não realinha (branch próprio)

    def test_of_in_ov_still_realigned_first(self):
        # Etapa 1 (R223) intacta e prioritária: OF válida na coluna OV volta p/ OF
        out = self._realign(of="PTJ19846T", modelo="3V", ov="262882")
        assert out["of"] == "262882"
        assert out["ov"] == ""

    def test_full_cross_recovers_model_from_of_column(self):
        # Integração: of traz o código de modelo (OMEGA1200H), modelo vazio →
        # o cross encaminha-o e o winner resolve para a peça certa (262107).
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "OMEGA1200H", "modelo": "", "cliente": "", "ov": ""}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["cliente"]["value"] == "ELECNOR"


class TestAlignmentHypotheses:
    """R236 — realinhamento por busca de hipóteses: as regras R223/R231/R232
    viram geradores de variantes e o scoring FS decide qual tem evidência.
    A assinatura completa do shift (modelo na OF + OF na OV) existe em 3.1%
    das linhas reais; a OF vem da OV em 5.2%; modelo na OF em 4.6%."""

    def test_full_shift_recovers_of_and_model(self):
        # Shift completo: OF real na coluna OV, código de modelo na coluna OF.
        # O R231 sozinho recuperava a OF mas PERDIA o modelo; a hipótese
        # realign_of_keep_model preserva-o quando a evidência paga.
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "OMEGA1200H", "ov": "262107", "modelo": "",
                "cliente": "ELECNOR",
            }],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["of"]["value"] == "262107"
        assert "OMEGA" in row["fields"]["modelo"]["value"]

    def test_embedded_of_in_free_text(self):
        # OF embebida em texto livre na coluna OF → extraída e validada.
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "OF 262107 dobrar", "cliente": "ELECNOR"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["of"]["value"] == "262107"

    def test_cliente_in_modelo_column_moves_when_evidence_pays(self):
        # Cliente na coluna modelo (cliente vazio): a hipótese move-o quando
        # o nome bate um cliente do plano e a linha ganha evidência com isso.
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "of": "262107", "cliente": "", "modelo": "ELECNOR",
            }],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["cliente"]["value"] == "ELECNOR"

    def test_no_realign_without_evidence(self):
        # Texto garbled sem correspondência: nenhuma hipótese paga o prior —
        # a linha fica tal-qual (H0) e vai para revisão, não se inventa.
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "(49566D)", "modelo": "", "cliente": ""}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] is None

    def test_of_in_cliente_column_recovered(self):
        """R240 — OF válida na coluna CLIENTE (8 casos confirmados por humanos
        no app.db; custo medido 0.29 bits). Coberto por forma, sem regra."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "", "cliente": "262107", "modelo": "OMEGA 1200 H"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["of"]["value"] == "262107"
        assert row["fields"]["cliente"]["value"] == "ELECNOR"

    def test_of_in_modelo_column_recovered_with_corroboration(self):
        """R240 — OF válida na coluna MODELO (o caso perguntado pelo
        utilizador). Nunca confirmado nos dados (0/1) → exige corroboração
        (>=2 campos): aqui o cliente corrobora."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "", "modelo": "262107", "cliente": "ELECNOR"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["of"]["value"] == "262107"

    def test_of_in_modelo_column_alone_stays_for_review(self):
        """R240 — OF válida na coluna modelo SEM mais nada escrito: sem
        corroboração não se move (a validade sozinha já está contada na
        assinatura medida — mover seria dupla contagem). Fica para revisão."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "", "modelo": "262107"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        assert result["rows"][0]["winner_of"] != "262107"

    def test_of_embedded_in_cliente_column(self):
        """R240 — OF embebida em texto da coluna cliente: extrai a OF e
        preserva o resto como cliente."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "", "cliente": "ELECNOR 262107", "modelo": ""}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        row = result["rows"][0]
        assert row["winner_of"] == "262107"
        assert row["fields"]["of"]["value"] == "262107"

    def test_lote_lookalike_not_moved_without_corroboration(self):
        """R240 — token na coluna LOTE que parece uma OF válida (0/11
        confirmados nos dados; custo 3.7 bits + corroboração obrigatória):
        sem mais evidência, NÃO move."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "", "lote": "262107"}],
        }
        result = cross_check_sheet(sheet_data, None, _REFS)
        assert result["rows"][0]["winner_of"] != "262107"


class TestContextPriors:
    """R242 — o contexto como evidência: prior de produção (D1, quant6:
    P(ativa 14d|verdadeira)=71.2% vs 2.2% aleatória) e coerência de folha
    (D2, quant5: mesma OF adjacente lift 21×). Ambos com cap ±2 bits —
    quebram empates, nunca vencem evidência real."""

    def _tied_refs(self):
        # Duas encomendas quase-gémeas: mesmo cliente, designações e dims
        # iguais — sem contexto, o winner é um empate por ordem.
        return {
            "of_to_entries": {
                "300001": [{"ov": "2900001", "cliente": "GEMEA SA",
                            "designacao": "TWIN 100", "comp": 1000,
                            "lbase": 50, "ltopo": 30, "esp": 2.0}],
                "300002": [{"ov": "2900002", "cliente": "GEMEA SA",
                            "designacao": "TWIN 100", "comp": 1000,
                            "lbase": 50, "ltopo": 30, "esp": 2.0}],
            },
        }

    def test_production_prior_breaks_tie_toward_active_of(self):
        from app.pipeline.scoring_engine import select_winner

        row = {"cliente": "GEMEA SA", "modelo": "TWIN 100",
               "comp_mm": "1000", "lbase": "50", "ltopo": "30", "esp": "2,0"}
        refs = self._tied_refs()
        # Sem prior: empate resolvido por ordem (300001).
        w0 = select_winner(row, refs, "bobine_formato")
        assert w0 and w0.get("_of") == "300001"
        # Com prior de produção: a OF ativa (300002) ganha o empate.
        bias = {"of": {"300002": 2.0}, "of_default": -1.77}
        w1 = select_winner(row, refs, "bobine_formato", extra_bias=bias)
        assert w1 and w1.get("_of") == "300002"

    def test_production_prior_never_beats_real_evidence(self):
        from app.pipeline.scoring_engine import select_winner

        # OF exata escrita para 300001; prior ativo em 300002 (±2 bits)
        # NÃO pode vencer a evidência real (~9.4 bits da OF exata).
        row = {"of": "300001", "cliente": "GEMEA SA", "modelo": "TWIN 100"}
        bias = {"of": {"300002": 2.0}, "of_default": -1.77}
        w = select_winner(row, self._tied_refs(), "bobine_formato",
                          extra_bias=bias)
        assert w and w.get("_of") == "300001"

    def test_sheet_coherence_pulls_weak_row_to_neighbor_of(self):
        # Linha 0 confiante (OF exata + cliente + modelo); linha 1 só com as
        # dims partilhadas pelas gémeas (empate) → o passe 2 puxa para a OF
        # do vizinho confiante.
        refs = self._tied_refs()
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [
                {"of": "300002", "cliente": "GEMEA SA", "modelo": "TWIN 100",
                 "comp_mm": "1000", "lbase": "50", "ltopo": "30", "esp": "2,0"},
                {"comp_mm": "1000", "lbase": "50", "ltopo": "30", "esp": "2,0"},
            ],
        }
        scoring, *_ = shadow_score(sheet_data, None, refs)
        assert scoring["rows"][0]["winner_of"] == "300002"
        assert scoring["rows"][1]["winner_of"] == "300002"
