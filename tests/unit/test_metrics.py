"""Tests for the field-level comparison and aggregate baseline metrics."""
from __future__ import annotations

from app.evaluation.metrics import (
    FieldComparison,
    _normalise,
    compare_extractions,
    compute_metrics,
    worst_errors,
)
from app.pipeline.inference.schemas import (
    Footer,
    Header,
    KanbanExtraction,
    Row,
)


def _make(
    *,
    operador: str = "",
    of: str = "",
    modelo: str = "",
    qtd: str = "",
    cliente: str = "",
    rows: int = 1,
) -> KanbanExtraction:
    """Build an extraction with N rows where every row has the same values.

    Convenience for tests; only the listed columns are populated.
    """
    return KanbanExtraction(
        header=Header(operador=operador, n_operador="1", setor_maquina="S", data="01-01-2026"),
        rows=[Row(of=of, modelo=modelo, qtd=qtd, cliente=cliente) for _ in range(rows)],
        footer=Footer(colunas_produzidas="0", horas_trabalhadas="0"),
    )


def test_normalise_strips_accents_and_casefolds() -> None:
    assert _normalise("Júlio Lima") == _normalise("julio lima")
    assert _normalise("MTG  ") == "mtg"
    assert _normalise("") == ""


def test_perfect_sheet_yields_all_matches() -> None:
    sheet = _make(operador="X", of="261860", modelo="CGC2E10D", qtd="5")
    comps = compare_extractions("a.jpg", sheet, sheet)
    assert all(c.is_match for c in comps)
    assert all(not c.is_hallucination for c in comps)


def test_perfect_sheet_metrics() -> None:
    sheet = _make(operador="X", of="261860", modelo="CGC2E10D", qtd="5")
    comps = compare_extractions("a.jpg", sheet, sheet)
    metrics = compute_metrics({"a.jpg": comps})
    assert metrics.field_accuracy == 1.0
    assert metrics.perfect_sheet_rate == 1.0
    assert metrics.critical_field_accuracy == 1.0
    assert metrics.hallucination_rate == 0.0
    assert metrics.case_sensitive_accuracy == 1.0


def test_case_only_error_is_visible_without_changing_legacy_accuracy() -> None:
    expected = _make(modelo="CFC5F45Riv")
    actual = _make(modelo="CFC5F45RIV")
    metrics = compute_metrics({"a.jpg": compare_extractions("a.jpg", expected, actual)})
    assert metrics.field_accuracy == 1.0
    assert metrics.case_sensitive_accuracy < 1.0
    assert metrics.case_sensitive_accuracy_by_field["modelo"] == 0.0


def test_critical_field_mismatch_drops_critical_accuracy() -> None:
    expected = _make(of="261860", modelo="CGC2E10D", qtd="5")
    actual = _make(of="261861", modelo="CGC2E10D", qtd="5")  # of off by one
    comps = compare_extractions("a.jpg", expected, actual)
    metrics = compute_metrics({"a.jpg": comps})
    # 3 critical fields per row x 1 row = 3 critical comparisons; 2 matches
    assert metrics.critical_field_accuracy == 2 / 3
    assert metrics.perfect_sheet_rate == 0.0


def test_hallucination_when_actual_filled_but_expected_empty() -> None:
    expected = _make(cliente="", of="261860", modelo="CGC2E10D", qtd="5")
    actual = _make(cliente="MTG", of="261860", modelo="CGC2E10D", qtd="5")
    comps = compare_extractions("a.jpg", expected, actual)
    cliente_comp = next(c for c in comps if c.field_path.endswith(".cliente"))
    assert cliente_comp.is_hallucination
    assert not cliente_comp.is_match


def test_hallucination_rate_uses_expected_empty_denominator() -> None:
    # Build a sheet where ONLY cliente is expected-empty.
    expected = KanbanExtraction(
        header=Header(operador="X", n_operador="1", setor_maquina="S", data="01-01-2026"),
        rows=[Row(of="261860", modelo="CGC2E10D", qtd="5", cliente="")],
        footer=Footer(colunas_produzidas="0", horas_trabalhadas="0"),
    )
    # Predict with cliente filled in (a hallucination).
    actual = KanbanExtraction(
        header=expected.header,
        rows=[Row(of="261860", modelo="CGC2E10D", qtd="5", cliente="MTG")],
        footer=expected.footer,
    )
    comps = compare_extractions("a.jpg", expected, actual)
    metrics = compute_metrics({"a.jpg": comps})
    # Expected-empty comparisons in this fixture: cliente, ov, lote, coni, esp, lbase, ltopo, pri.
    # Of those, only cliente is hallucinated.
    expected_empty_count = sum(1 for c in comps if c.expected == "")
    hallucination_count = sum(1 for c in comps if c.is_hallucination)
    assert hallucination_count == 1
    assert metrics.hallucination_rate == 1 / expected_empty_count


def test_extra_row_in_actual_counts_as_hallucinations() -> None:
    expected = _make(of="261860", rows=1)
    actual = _make(of="261860", rows=2)  # one extra row
    comps = compare_extractions("a.jpg", expected, actual)
    extra_row_comps = [c for c in comps if c.field_path.startswith("rows[1].")]
    # The extra row has of/modelo/qtd populated → 3 hallucinations on critical fields
    hallucinations = [c for c in extra_row_comps if c.is_hallucination]
    assert len(hallucinations) >= 1


def test_missing_row_in_actual_counts_as_mismatches() -> None:
    expected = _make(of="261860", rows=2)
    actual = _make(of="261860", rows=1)  # one row missing
    comps = compare_extractions("a.jpg", expected, actual)
    missing_row_comps = [c for c in comps if c.field_path.startswith("rows[1].")]
    mismatches = [c for c in missing_row_comps if not c.is_match]
    assert mismatches  # at least one
    # Missing rows are NOT hallucinations (expected non-empty, actual empty)
    assert not any(c.is_hallucination for c in mismatches)


def test_per_field_accuracy_aggregates_across_sheets() -> None:
    # Two sheets: of matches in one, fails in the other. Operador matches in both.
    expected_a = _make(operador="X", of="261860")
    actual_a_match = _make(operador="X", of="261860")  # full match
    expected_b = _make(operador="X", of="261861")
    actual_b_wrong = _make(operador="X", of="999999")  # of mismatch only

    comps_a = compare_extractions("a.jpg", expected_a, actual_a_match)
    comps_b = compare_extractions("b.jpg", expected_b, actual_b_wrong)
    metrics = compute_metrics({"a.jpg": comps_a, "b.jpg": comps_b})

    assert metrics.accuracy_by_field["operador"] == 1.0
    assert metrics.accuracy_by_field["of"] == 0.5
    assert metrics.perfect_sheet_rate == 0.5  # only sheet A is perfect


def test_worst_errors_prioritises_critical_fields() -> None:
    expected = _make(of="261860", modelo="CGC2E10D", qtd="5", cliente="MTG", operador="X")
    actual = _make(of="999999", modelo="CGC2E10D", qtd="5", cliente="ABC", operador="Y")
    comps = compare_extractions("a.jpg", expected, actual)
    worst = worst_errors({"a.jpg": comps}, n=3)
    # The critical OF mismatch must appear before the non-critical operador / cliente mismatches.
    critical_indices = [i for i, c in enumerate(worst) if c.is_critical]
    non_critical_indices = [i for i, c in enumerate(worst) if not c.is_critical]
    assert critical_indices and non_critical_indices
    assert max(critical_indices) < min(non_critical_indices)


def test_compare_makes_deterministic_field_paths() -> None:
    sheet = _make(of="261860", rows=1)
    comps = compare_extractions("a.jpg", sheet, sheet)
    paths = [c.field_path for c in comps]
    assert "header.operador" in paths
    assert "rows[0].of" in paths
    assert "footer.colunas_produzidas" in paths


def test_field_comparison_make_handles_unicode_normalisation() -> None:
    c = FieldComparison.make("a.jpg", "header.operador", "Júlio Lima", "Julio lima", critical=False)
    assert c.is_match
