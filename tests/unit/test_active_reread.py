"""R246 — descodificação ativa: parte DETERMINÍSTICA (seleção + normalização).

A fiabilidade do re-read do VLM não é testável offline (o Ollama e as imagens
rotuladas vivem na fábrica — calibração pendente); estes testes cobrem a
lógica que decide QUE célula re-ler e como interpretar a resposta, que tem de
estar certa antes de qualquer calibração.
"""
from __future__ import annotations

from app.pipeline import active_reread as ar


class TestAnswerNormalization:
    def test_clean_and_noisy_answers(self):
        opts = ("262107", "262109")
        assert ar._normalize_answer("262107", opts) == "262107"
        assert ar._normalize_answer("é o 262109.", opts) == "262109"  # texto à volta
        assert ar._normalize_answer("262 107", opts) == "262107"       # espaço

    def test_refuses_third_option_and_garbage(self):
        opts = ("262107", "262109")
        assert ar._normalize_answer("262108", opts) is None   # nem A nem B → não inventa
        assert ar._normalize_answer("não sei", opts) is None
        assert ar._normalize_answer("", opts) is None

    def test_nested_options_decided_by_exact(self):
        opts = ("100", "1000")
        assert ar._normalize_answer("1000", opts) == "1000"
        assert ar._normalize_answer("100", opts) == "100"


class TestCandidateSelection:
    def _item(self, **kw):
        base = {"section": "rows", "row_index": 0, "field": "of",
                "field_path": "rows[0].of", "value": "A", "ref": "B",
                "decision_confidence": 0.7, "review_priority": 1.0}
        base.update(kw)
        return base

    def test_only_grey_zone_two_hypotheses(self):
        result = {"to_analisar": [
            self._item(field="of", field_path="rows[0].of",
                       decision_confidence=0.72, review_priority=2.1),
            self._item(field="of", field_path="rows[1].of", row_index=1,
                       decision_confidence=0.98, review_priority=0.1),   # confiante → fora
            self._item(field="esp", field_path="rows[2].esp", row_index=2,
                       value="3", ref="4", decision_confidence=0.60,
                       review_priority=3.0),                              # crítica → 1º
            self._item(field="of", field_path="rows[3].of", row_index=3,
                       value="5", ref="5", decision_confidence=0.7),      # value==ref → fora
            {"section": "header", "field": "operador",
             "field_path": "header.operador", "decision_confidence": 0.6},
        ]}
        cands = ar.candidates_for_reread(result)
        assert [c["field"] for c in cands] == ["esp", "of"]  # prioridade desc
        assert len(cands) <= ar.MAX_REREADS_PER_SHEET

    def test_caps_at_three(self):
        result = {"to_analisar": [
            self._item(field_path=f"rows[{i}].of", row_index=i,
                       value=f"A{i}", ref=f"B{i}",
                       decision_confidence=0.7, review_priority=float(i))
            for i in range(6)
        ]}
        assert len(ar.candidates_for_reread(result)) == 3
