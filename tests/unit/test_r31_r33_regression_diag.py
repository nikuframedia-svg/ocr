import importlib.util
import sys
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "diag" / "r31_r33_regression.py"
_SPEC = importlib.util.spec_from_file_location("r31_r33_regression_diag", _SCRIPT)
assert _SPEC and _SPEC.loader
r31_r33 = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = r31_r33
_SPEC.loader.exec_module(r31_r33)


def _classify(row, diff=None, repeat_group_count=1):
    return r31_r33._classify_no_match(
        row,
        diff,
        repeat_group_count=repeat_group_count,
    )


def test_repeated_261840_is_classed_as_inflated_repetition():
    klass, cause, recommendation = _classify(
        {
            "sheet_id": "2314",
            "template": "quinadora_pav4",
            "path": "rows[0].of",
            "field": "of",
            "value": "261840",
            "ref": "",
            "reason": "OF '261840' nao esta em plan_colunas",
        },
        {"resultado_atual": "261810", "kind": "ok_corrigiu"},
        repeat_group_count=77,
    )

    assert klass == "inflated_repetition"
    assert cause == "repeated_of_not_in_plan"
    assert "one cause" in recommendation


def test_expedicao_model_text_in_of_is_classed_as_r33_exposed_realignment():
    klass, cause, _recommendation = _classify(
        {
            "sheet_id": "2352",
            "template": "expedicao",
            "path": "rows[0].of",
            "field": "of",
            "value": "CGC2E10D",
            "ref": "",
            "reason": "OF 'CGC2E10D' nao esta em plan_colunas",
        },
        {"resultado_atual": "262762", "kind": "nada_a_ver"},
    )

    assert klass == "r33_exposed_no_autofill"
    assert cause == "of_field_contains_model_or_text"


def test_cliente_canonical_gap_is_ref_gap_not_real_error():
    klass, cause, _recommendation = _classify(
        {
            "sheet_id": "2301",
            "template": "maq_fustes",
            "path": "rows[0].cliente",
            "field": "cliente",
            "value": "ALFA",
            "ref": "ALFA GMBH",
            "reason": "OCR 'ALFA' != plan 'ALFA GMBH'",
        },
        {"resultado_atual": "ALFA GMBH", "kind": "ok_corrigiu"},
    )

    assert klass == "ref_missing_or_stale"
    assert cause == "cliente_alias_or_canonical_gap"


def test_ov_truncated_or_digit_error_is_r33_exposed_no_autofill():
    klass, cause, _recommendation = _classify(
        {
            "sheet_id": "2323",
            "template": "bobine_formato",
            "path": "rows[0].ov",
            "field": "ov",
            "value": "260495",
            "ref": "2604995",
            "reason": "OCR '260495' != plan '2604995'",
        },
        {"resultado_atual": "2604995", "kind": "ok_corrigiu"},
    )

    assert klass == "r33_exposed_no_autofill"
    assert cause == "ov_truncated_or_digit_error"


def test_lote_missing_stocksap_is_ref_gap():
    klass, cause, _recommendation = _classify(
        {
            "sheet_id": "2304",
            "template": "bobine_formato",
            "path": "rows[1].lote",
            "field": "lote",
            "value": "H26B0546",
            "ref": "",
            "reason": "lote 'H26B0546' nao esta em StockSAP",
        },
        {"resultado_atual": "H26B0546", "kind": "igual"},
    )

    assert klass == "ref_missing_or_stale"
    assert cause == "lote_missing_stocksap"


def test_summary_distributes_r33_and_delta_impact():
    classified = [
        {"error_class": "inflated_repetition", "root_cause": "repeated_of_not_in_plan"},
        {"error_class": "ref_missing_or_stale", "root_cause": "cliente_alias_or_canonical_gap"},
        {"error_class": "real_ocr_or_system", "root_cause": "numeric_parse_error"},
    ]

    rows, metrics = r31_r33._summary_rows(
        classified,
        rates=r31_r33.Rates(r31=0.08, r33=0.13),
    )

    assert metrics["total_no_match"] == 3
    assert round(metrics["estimated_denominator"], 4) == round(3 / 0.13, 4)
    assert {row["root_cause"] for row in rows} == {
        "repeated_of_not_in_plan",
        "cliente_alias_or_canonical_gap",
        "numeric_parse_error",
    }
    assert sum(row["impact_pp_inside_13pct"] for row in rows) == 12.99


def test_summary_counts_repeat_extras_once_per_repeated_group():
    classified = [
        {
            "sheet_id": "2314",
            "field": "of",
            "value": "261840",
            "ref": "",
            "reason": "OF '261840' nao esta em plan_colunas",
            "path": "rows[0].of",
            "resultado_atual": "261810",
            "error_class": "inflated_repetition",
            "root_cause": "repeated_of_not_in_plan",
            "repeat_group_count": 2,
        },
        {
            "sheet_id": "2314",
            "field": "of",
            "value": "261840",
            "ref": "",
            "reason": "OF '261840' nao esta em plan_colunas",
            "path": "rows[1].of",
            "resultado_atual": "261810",
            "error_class": "inflated_repetition",
            "root_cause": "repeated_of_not_in_plan",
            "repeat_group_count": 2,
        },
    ]

    rows, _metrics = r31_r33._summary_rows(
        classified,
        rates=r31_r33.Rates(r31=0.08, r33=0.13),
    )

    assert rows[0]["occurrences"] == 2
    assert rows[0]["unique_causes"] == 1
    assert rows[0]["repeat_extra_occurrences"] == 1
