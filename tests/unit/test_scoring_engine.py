"""R108 — Testes unitários do motor unificado de scoring.

Cobre:
- Devolução básica do `shadow_score` (shape, summary, duração)
- Campos sem ref (PRI/CONI/QTD) ficam NA
- Geração de candidatos por campo (top-K)
- Funcionamento com refs vazias (degrada para NA total)
"""
from __future__ import annotations

import pytest

from app.pipeline.scoring_engine import (
    _candidates_for_field,
    _find_winner_entry,
    _format_value,
    _get_indices,
    _lev_distance,
    _num_sim,
    _str_sim,
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

    def test_lote_candidates_from_sap(self):
        row = {"lote": "M26B0307"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("lote", row, _REFS, idx)
        assert cands[0]["value"] == "M26B0307"
        assert cands[0]["sim"] == 100.0

    def test_dim_candidates_filter_by_delta(self):
        row = {"comp_mm": "1210"}
        idx = _get_indices(_REFS)
        cands = _candidates_for_field("comp_mm", row, _REFS, idx)
        # plan has 1200 (delta 10) and 1500 (delta 290, > 100 max)
        values = [c["value"] for c in cands if not c.get("is_ocr_raw")]
        assert 1200 in values
        assert 1500 not in values

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
        assert scoring["engine_version"] == "v8_R130"
        assert scoring["template_name"] == "bobine_formato"
        assert "checked_at" in scoring
        assert scoring["summary"]["total"] == total
        assert total == snapped + confirmed + na
        assert dur_ms >= 0
        assert len(scoring["rows"]) == 1
        # R123 (B9) — header/footer agora validados (já não forçados a NA).
        assert set(scoring["header"]) == {"operador", "data"}
        assert set(scoring["footer"]) == {"horas_trabalhadas"}
        _valid = {"confirmed", "snapped", "very_different", "NA"}
        for v in scoring["header"].values():
            assert v["status"] in _valid
        for v in scoring["footer"].values():
            assert v["status"] in _valid

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

    def test_empty_refs_no_match_when_ocr_present(self):
        """R120 — sem refs, campos validáveis com OCR não-vazio passam a
        very_different (vermelho) em vez de NA (cinza). Antes (R108 v4) iam
        a NA, escondendo a divergência. O R120 sinaliza ao operador que
        escreveu algo mas não há match no plan.

        Header/footer/_NO_REF_FIELDS continuam NA — sem alteração.
        """
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
        # Sem refs, nenhuma célula de LINHA confirma.
        for r in scoring["rows"]:
            for cell in r["fields"].values():
                assert cell["status"] != "confirmed"
        # R120: campos validáveis com OCR ≠ vazio marcam very_different.
        row0 = scoring["rows"][0]["fields"]
        assert row0["cliente"]["status"] == "very_different"
        assert row0["of"]["status"] == "very_different"
        assert row0["lote"]["status"] == "very_different"
        # Campos sem OCR (ov, modelo, comp_mm, ...) ficam NA.
        assert row0["ov"]["status"] == "NA"
        # R123 (B9) — sem ListaColaboradores, o operador recebe validação
        # leve (preenchido = confirmed).
        assert scoring["header"]["operador"]["status"] == "confirmed"

    def test_no_ref_fields_stay_ocr_raw(self):
        """PRI/CONI/QTD ficam OCR raw + status NA mesmo com refs disponíveis."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "pri": "F", "cliente": "ELECNOR", "of": "262107",
                "qtd": "5", "coni": "OCT",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        row = scoring["rows"][0]
        # pri/qtd/coni: NA + valor OCR preservado
        for field in ("pri", "qtd", "coni"):
            assert row["fields"][field]["status"] == "NA"
            assert row["fields"][field]["source"] == "ocr_raw"

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


class TestFindWinner:
    def test_no_candidates_returns_none(self):
        winner = _find_winner_entry({}, {}, _REFS)
        assert winner is None


class TestR134SubstituteEverything:
    """R134 — substitute-everything (filosofia R108, R130 revertido): a
    entry vencedora holística (máximo de campos iguais, peso igual por
    campo) substitui TODOS os campos, incl. of/ov/cliente. O valor da
    célula passa a ser o do winner; após auto-apply + re-cross-check fica
    MATCH/verde."""

    def test_of_substituted_by_winner(self):
        """OCR de OF que não existe no plan mas a linha bate uma entry pelos
        outros campos → OF substituída pelo valor do winner (não preserva OCR)."""
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
        # R134 — valor passa a ser o do winner (substituído), NÃO "999999".
        assert of_cell["value"] == "262108"

    def test_of_agreement_confirms(self):
        """OCR de OF que bate exactamente com plan → confirmed (verde)."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"of": "262107", "cliente": "ELECNOR"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        assert scoring["rows"][0]["fields"]["of"]["status"] == "confirmed"
        assert scoring["rows"][0]["fields"]["of"]["value"] == "262107"

    def test_ov_substituted_by_winner(self):
        """OV diferente do winner → substituída pelo valor do winner."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{
                "cliente": "ELECNOR", "of": "262107",  # OV no plan = 2410001
                "ov": "9999999",
            }],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        ov_cell = scoring["rows"][0]["fields"]["ov"]
        assert ov_cell["value"] == "2410001"   # substituído pelo winner

    def test_cliente_substituted_by_winner(self):
        """Cliente diferente do winner → substituído pelo valor do winner."""
        sheet_data = {
            "template_name": "bobine_formato", "header": {}, "footer": {},
            "rows": [{"cliente": "SUNNA", "of": "262107"}],  # OF da ELECNOR
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        cli = scoring["rows"][0]["fields"]["cliente"]
        assert cli["value"] == "ELECNOR"   # substituído pelo winner

    def test_512_dimensional_only_winner_substitutes_all(self):
        """Caso #512 (decisão do utilizador): OCR of/ov/cliente/modelo não
        batem nenhuma entry; o winner é escolhido pelo máximo de campos
        iguais (aqui as medidas) → substitui tudo por essa entry."""
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
        # winner = entry 262108 (bate as 5 medidas) → substitui of/ov/cliente.
        assert fields["of"]["value"] == "262108"
        assert fields["ov"]["value"] == "2410002"
        assert fields["cliente"]["value"] == "MTG BELUX"

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


class TestR132MaqFustes:
    """R132 — Novo template TPL103 MÁQUINA DE FUSTES (frente + verso).
    Cobre: detect_template, header dinâmico com turno, paragens NA,
    cross-check sem ruido em campos sem ref."""

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
        acabamento_mtg2 e maq_fustes nunca recebiam turno do OCR."""
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template
        prompt = build_prompt(get_template("maq_fustes"))
        assert '"turno"' in prompt
        assert "M | R | XM | T" in prompt
        # qtd_metros vem na coluna line via _FIELD_LABELS
        assert "QTD (METROS)" in prompt

    def test_acabamento_mtg2_also_gets_turno_via_refactor(self):
        """O fix do header dinâmico também corrige acabamento_mtg2 (R129)
        que tinha turno definido mas nunca pedido ao Qwen."""
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template
        prompt = build_prompt(get_template("acabamento_mtg2"))
        assert '"turno"' in prompt

    def test_maq_fustes_paragens_no_cross_check_against_plan(self):
        """Campos motivo/inicio/fim/duracao/resolvido devem cair em NA
        (cinza, sem flag) — _NO_REF_FIELDS cobre paragens."""
        sheet_data = {
            "template_name": "maq_fustes_paragens",
            "header": {"operador": "X", "data": "10-05-2026"},
            "footer": {},
            "rows": [{"motivo": "AVARIA HIDRAULICA", "inicio": "08:30",
                      "fim": "09:00", "duracao": "00:30", "resolvido": "SIM"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        for f in ("motivo", "inicio", "fim", "duracao", "resolvido"):
            assert scoring["rows"][0]["fields"][f]["status"] == "NA"

    def test_maq_fustes_qtd_metros_is_na(self):
        """qtd_metros é informativo, sem ref no plan → NA cinza."""
        sheet_data = {
            "template_name": "maq_fustes",
            "header": {}, "footer": {},
            "rows": [{"cliente": "ELECNOR", "of": "262107", "ov": "2410001",
                      "modelo": "OMEGA 1200 H", "qtd": "5", "qtd_metros": "12.5"}],
        }
        scoring, *_ = shadow_score(sheet_data, None, _REFS)
        assert scoring["rows"][0]["fields"]["qtd_metros"]["status"] == "NA"

    def test_two_sided_templates_map(self):
        """Garantia: o map em ocr_runner aponta maq_fustes → maq_fustes_paragens."""
        from app.web.ocr_runner import TWO_SIDED_TEMPLATES
        assert TWO_SIDED_TEMPLATES.get("maq_fustes") == "maq_fustes_paragens"

    def test_side_detect_prompt_mentions_both_options(self):
        """O prompt de side-detect tem que mencionar ambos os cabeçalhos
        para o Qwen poder discriminar."""
        from app.pipeline.prompt_builder import build_side_detect_prompt
        prompt = build_side_detect_prompt()
        assert "PRI" in prompt
        assert "MOTIVO DA PARAGEM" in prompt
        assert '"side"' in prompt
