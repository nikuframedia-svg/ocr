"""R110.B+C — Testes para chart_renderer + propose_tools."""
from __future__ import annotations

import pytest

from app.pipeline.chart_renderer import (
    ChartValidationError,
    to_chartjs_envelope,
    validate_chart,
)
from app.pipeline.tools import (
    ALL_TOOLS,
    get_session_charts,
    get_session_proposals,
    propose_chart,
    propose_cpis_change,
    propose_rule,
    propose_template_change,
    reset_session_buffers,
)
from app.web import db


# ----- ChartSpec -----------------------------------------------------------

class TestChartSpec:
    def test_valid_bar(self):
        spec = {
            "type": "bar",
            "title": "Edits por operador",
            "labels": ["Augusto", "Vitor", "Julio"],
            "datasets": [{"label": "Edits", "data": [12, 8, 15]}],
        }
        out = validate_chart(spec)
        assert out["type"] == "bar"
        assert len(out["labels"]) == 3
        assert out["datasets"][0]["data"] == [12, 8, 15]

    def test_invalid_type_rejected(self):
        with pytest.raises(ChartValidationError):
            validate_chart({
                "type": "bubble",
                "labels": ["a", "b"],
                "datasets": [{"label": "x", "data": [1, 2]}],
            })

    def test_one_label_rejected(self):
        with pytest.raises(ChartValidationError, match="pelo menos 2 labels"):
            validate_chart({
                "type": "bar",
                "labels": ["solo"],
                "datasets": [{"label": "x", "data": [1]}],
            })

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ChartValidationError, match="pontos"):
            validate_chart({
                "type": "bar",
                "labels": ["a", "b", "c"],
                "datasets": [{"label": "x", "data": [1, 2]}],
            })

    def test_empty_datasets_rejected(self):
        with pytest.raises(ChartValidationError):
            validate_chart({"type": "bar", "labels": ["a", "b"], "datasets": []})

    def test_chartjs_envelope_shape(self):
        spec = validate_chart({
            "type": "line", "title": "Trend",
            "labels": ["jan", "fev"], "datasets": [{"label": "x", "data": [1, 2]}],
        })
        env = to_chartjs_envelope(spec)
        assert env["type"] == "line"
        assert env["title"] == "Trend"
        assert env["labels"] == ["jan", "fev"]
        assert env["datasets"][0]["data"] == [1, 2]


# ----- propose_chart tool --------------------------------------------------

class TestProposeChartTool:
    def setup_method(self):
        reset_session_buffers()

    def test_valid_chart_accepted(self):
        result = propose_chart(
            type="bar", title="Test", labels=["a", "b"],
            datasets=[{"label": "x", "data": [1, 2]}],
            narrative="testing",
        )
        assert result["status"] == "ok"
        assert "index" in result
        charts = get_session_charts()
        assert len(charts) == 1
        assert charts[0]["title"] == "Test"
        assert charts[0]["narrative"] == "testing"

    def test_invalid_chart_rejected_with_hint(self):
        result = propose_chart(
            type="bar", title="solo", labels=["only"],
            datasets=[{"label": "x", "data": [1]}],
        )
        assert result["status"] == "error"
        assert "hint" in result
        assert get_session_charts() == []

    def test_buffer_resets(self):
        propose_chart(type="bar", title="A", labels=["a", "b"],
                      datasets=[{"label": "x", "data": [1, 2]}])
        assert len(get_session_charts()) == 1
        reset_session_buffers()
        assert get_session_charts() == []


# ----- propose_rule tool ---------------------------------------------------

class TestProposeRuleTool:
    def setup_method(self):
        reset_session_buffers()
        db.init_db()

    def test_alias_with_strong_evidence_is_auto(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "TECNICAS", "to": "TÉCNICAS REUNIDAS"},
            field="cliente",
            justification="5 folhas confirmam",
            evidence_sheet_ids=[1, 2, 3, 4, 5, 6],
            qwen_confidence=0.92,
        )
        assert result["status"] == "ok"
        assert result["risk_class"] == "auto"

    def test_alias_weak_evidence_is_review(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "X", "to": "Y"},
            evidence_sheet_ids=[1],
            qwen_confidence=0.5,
        )
        assert result["status"] == "ok"
        assert result["risk_class"] == "review"

    def test_confusion_pair_single_char_only(self):
        bad = propose_rule(
            kind="confusion_pair",
            payload={"gold_char": "AB", "ocr_char": "AB"},
        )
        assert bad["status"] == "error"
        assert "EXACTAMENTE 1" in bad["error"]

        good = propose_rule(
            kind="confusion_pair",
            payload={"gold_char": "0", "ocr_char": "O"},
        )
        assert good["status"] == "ok"
        assert good["risk_class"] == "review"

    def test_snap_rule_always_approval(self):
        result = propose_rule(
            kind="snap_rule",
            payload={"field": "esp", "pattern": "X", "target": "Y"},
            qwen_confidence=0.99,
        )
        assert result["status"] == "ok"
        assert result["risk_class"] == "approval"

    def test_alias_same_from_to_rejected(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "Same", "to": "same"},
        )
        assert result["status"] == "error"
        assert "iguais" in result["error"]

    def test_unknown_kind_rejected(self):
        result = propose_rule(kind="garbage", payload={})
        assert result["status"] == "error"

    def test_proposal_persisted(self):
        result = propose_rule(
            kind="cliente_alias",
            payload={"from": "AAA", "to": "BBB"},
            evidence_sheet_ids=[10, 11, 12, 13, 14, 15],
            qwen_confidence=0.95,
        )
        assert result["status"] == "ok"
        proposals = db.list_proposals(kind="cliente_alias", limit=10)
        assert any(p["id"] == result["proposal_id"] for p in proposals)


# ----- propose_template_change --------------------------------------------

class TestProposeTemplateChange:
    def setup_method(self):
        reset_session_buffers()
        db.init_db()

    def test_add_field_to_existing_template(self):
        result = propose_template_change(
            template_name="quinadora_pav8",
            change_type="add_field",
            field_name="ov",
            field_position="after_cliente",
            justification="23 folhas têm OV",
        )
        assert result["status"] == "ok"
        assert result["risk_class"] == "review"

    def test_nonexistent_template_rejected(self):
        result = propose_template_change(
            template_name="ghost_template",
            change_type="add_field",
            field_name="x",
        )
        assert result["status"] == "error"
        assert "não existe" in result["error"]

    def test_add_field_requires_name(self):
        result = propose_template_change(
            template_name="bobine_formato",
            change_type="add_field",
        )
        assert result["status"] == "error"
        assert "field_name" in result["error"]

    def test_invalid_change_type(self):
        result = propose_template_change(
            template_name="bobine_formato",
            change_type="upside_down",
            field_name="x",
        )
        assert result["status"] == "error"


# ----- propose_cpis_change ------------------------------------------------

class TestProposeCpisChange:
    def setup_method(self):
        reset_session_buffers()
        db.init_db()

    def test_add_column_ok(self):
        result = propose_cpis_change(
            scope="VANTAGE TOWERS",
            change_type="add_column",
            column_name="MATERIAL_SPEC",
            justification="cliente exige",
        )
        assert result["status"] == "ok"
        assert result["risk_class"] == "review"

    def test_add_requires_column_name(self):
        result = propose_cpis_change(
            scope="all", change_type="add_column",
        )
        assert result["status"] == "error"

    def test_invalid_change_rejected(self):
        result = propose_cpis_change(
            scope="all", change_type="rename_column",
            column_name="X",
        )
        assert result["status"] == "error"


# ----- Tool registry integration ------------------------------------------

class TestToolRegistryIntegration:
    def test_all_tools_registered(self):
        # 6 DB + 1 chart + 3 propose = 10
        assert len(ALL_TOOLS) == 10
        expected = {
            "query_db", "get_sheet", "list_recent_edits", "list_templates",
            "get_refs_summary", "query_learnings",
            "propose_chart",
            "propose_rule", "propose_template_change", "propose_cpis_change",
        }
        assert set(ALL_TOOLS.keys()) == expected

    def test_collect_session_outputs_merges(self):
        from app.pipeline.tools import collect_session_outputs
        reset_session_buffers()
        propose_chart(type="bar", title="X", labels=["a", "b"],
                      datasets=[{"label": "x", "data": [1, 2]}])
        propose_rule(kind="cliente_alias",
                     payload={"from": "A", "to": "B"})
        out = collect_session_outputs()
        assert len(out["charts"]) == 1
        assert len(out["proposed_rules"]) == 1
        reset_session_buffers()
        assert collect_session_outputs() == {"charts": [], "proposed_rules": []}
