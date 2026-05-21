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
        assert scoring["engine_version"] == "shadow_v5_R108"
        assert scoring["template_name"] == "bobine_formato"
        assert "checked_at" in scoring
        assert scoring["summary"]["total"] == total
        assert total == snapped + confirmed + na
        assert dur_ms >= 0
        assert len(scoring["rows"]) == 1
        # Header/footer always NA
        for v in scoring["header"].values():
            assert v["status"] == "NA"
        for v in scoring["footer"].values():
            assert v["status"] == "NA"

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
        assert confirmed == 0
        # R120: campos validáveis com OCR ≠ vazio agora marcam very_different.
        row0 = scoring["rows"][0]["fields"]
        assert row0["cliente"]["status"] == "very_different"
        assert row0["of"]["status"] == "very_different"
        assert row0["lote"]["status"] == "very_different"
        # Campos sem OCR (ov, modelo, comp_mm, ...) ficam NA.
        assert row0["ov"]["status"] == "NA"
        # Header/_NO_REF/footer continuam NA — comportamento intencional.
        for v in scoring["header"].values():
            assert v["status"] == "NA"

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
