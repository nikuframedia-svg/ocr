"""Field-level comparison and the five Phase 0 baseline metrics.

A *comparison* is one expected/actual pair for a named field on a named
sheet. The benchmark turns extractions + ground truth into a flat list
of comparisons, then this module aggregates them.

Comparison rules:

- Strings are matched after Unicode NFKD decomposition (strips combining
  marks, e.g. ``"Júlio" == "Julio"``), case-folding, and whitespace
  trimming.
- Date fields are already normalised by the schema (``/`` and ``.`` →
  ``-``); no extra handling here.
- Both empty after normalisation → match.

Row-count mismatches are handled by extending the shorter list with
empty :class:`Row` instances, so missing rows get penalised as N missing
fields and extra rows as N hallucinations.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.pipeline.inference.schemas import (
    CRITICAL_ROW_FIELDS,
    FOOTER_FIELDS,
    HEADER_FIELDS,
    ROW_FIELDS,
    KanbanExtraction,
    Row,
)


# Date pattern: D-M-YYYY or DD-MM-YYYY (PT-style with hyphens). Used in
# `_normalise` to zero-pad before comparison so '09-4-2026' and
# '09-04-2026' are treated as equal (Round 24b).
_DATE_PADDABLE_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")


def _normalise(value: str) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    s = no_marks.casefold().strip()
    # Round 24b: zero-pad day/month in DD-MM-YYYY dates so unpadded
    # GT ('09-4-2026') matches padded prod ('09-04-2026'). Only applies
    # to strings shaped like dates — won't false-match other fields.
    m = _DATE_PADDABLE_RE.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}-{int(mo):02d}-{y}"
    return s


def _normalise_case_sensitive(value: str) -> str:
    """Normalise accents/space/date formatting while preserving letter case."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    s = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()
    m = _DATE_PADDABLE_RE.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}-{int(mo):02d}-{y}"
    return s


# Fields where character-level partial credit is meaningful: anything
# the model has to *read* glyph-by-glyph (handwritten or lexical).
# Numeric fields like comp_mm/qtd are excluded because reading "5000"
# vs "5001" is a discrete error, not a slow degradation.
_CER_FIELDS = frozenset(
    {"operador", "cliente", "modelo", "lote", "of", "ov", "data"}
)


def character_error_rate(expected: str, actual: str) -> float:
    """Return the character error rate of ``actual`` vs ``expected``.

    CER = edit-distance(expected, actual) / max(len(expected), 1).
    Both strings are normalised first (NFKD, casefold, strip).

    - Identical strings (after normalisation) → 0.0
    - Totally different / either side empty → 1.0
    - 1 char wrong in a 6-char string → ~0.17

    Implementation: uses :class:`difflib.SequenceMatcher.ratio` (1 -
    similarity ratio gets us a close enough proxy for normalised
    Levenshtein for our short strings).
    """
    e = _normalise(expected)
    a = _normalise(actual)
    if e == a:
        return 0.0
    if not e or not a:
        return 1.0
    return 1.0 - SequenceMatcher(a=e, b=a).ratio()


@dataclass(frozen=True)
class FieldComparison:
    """Outcome of comparing one field on one sheet."""

    sheet: str
    field_path: str
    expected: str
    actual: str
    is_match: bool
    is_hallucination: bool
    is_critical: bool

    @property
    def is_case_match(self) -> bool:
        return _normalise_case_sensitive(self.expected) == _normalise_case_sensitive(self.actual)

    @classmethod
    def make(
        cls,
        sheet: str,
        path: str,
        expected: str,
        actual: str,
        *,
        critical: bool,
    ) -> FieldComparison:
        e_norm = _normalise(expected)
        a_norm = _normalise(actual)
        return cls(
            sheet=sheet,
            field_path=path,
            expected=expected,
            actual=actual,
            is_match=(e_norm == a_norm),
            is_hallucination=(e_norm == "" and a_norm != ""),
            is_critical=critical,
        )


def compare_extractions(
    sheet: str,
    expected: KanbanExtraction,
    actual: KanbanExtraction,
) -> list[FieldComparison]:
    """Return one :class:`FieldComparison` per field in the union of both extractions."""
    out: list[FieldComparison] = []

    for field in HEADER_FIELDS:
        out.append(
            FieldComparison.make(
                sheet,
                f"header.{field}",
                getattr(expected.header, field),
                getattr(actual.header, field),
                critical=False,
            )
        )

    row_count = max(len(expected.rows), len(actual.rows))
    empty_row = Row()
    for i in range(row_count):
        e_row = expected.rows[i] if i < len(expected.rows) else empty_row
        a_row = actual.rows[i] if i < len(actual.rows) else empty_row
        for field in ROW_FIELDS:
            out.append(
                FieldComparison.make(
                    sheet,
                    f"rows[{i}].{field}",
                    getattr(e_row, field),
                    getattr(a_row, field),
                    critical=(field in CRITICAL_ROW_FIELDS),
                )
            )

    for field in FOOTER_FIELDS:
        out.append(
            FieldComparison.make(
                sheet,
                f"footer.{field}",
                getattr(expected.footer, field),
                getattr(actual.footer, field),
                critical=False,
            )
        )

    return out


@dataclass(frozen=True)
class BaselineMetrics:
    """The aggregate metrics reported in ``baseline_v0.md``."""

    total_sheets: int
    total_comparisons: int
    field_accuracy: float
    perfect_sheet_rate: float
    hallucination_rate: float
    critical_field_accuracy: float
    accuracy_by_field: dict[str, float]
    # Mean character error rate per field, restricted to the lexical
    # fields in :data:`_CER_FIELDS` (where char-level partial credit
    # makes sense).
    cer_by_field: dict[str, float]
    # Separate diagnostic: legacy field_accuracy remains case-insensitive.
    case_sensitive_accuracy: float
    case_sensitive_accuracy_by_field: dict[str, float]


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def compute_metrics(
    comparisons_by_sheet: dict[str, list[FieldComparison]],
) -> BaselineMetrics:
    """Aggregate per-sheet field comparisons into the five baseline metrics."""
    total_sheets = len(comparisons_by_sheet)
    flat: list[FieldComparison] = [
        comp for sheet_comps in comparisons_by_sheet.values() for comp in sheet_comps
    ]
    total = len(flat)

    matches = sum(1 for c in flat if c.is_match)
    perfect_sheets = sum(
        1 for sheet_comps in comparisons_by_sheet.values() if all(c.is_match for c in sheet_comps)
    )
    expected_empty = [c for c in flat if _normalise(c.expected) == ""]
    hallucinations = sum(1 for c in expected_empty if c.is_hallucination)
    critical = [c for c in flat if c.is_critical]
    critical_matches = sum(1 for c in critical if c.is_match)

    per_field_hits: dict[str, list[bool]] = {}
    per_field_case_hits: dict[str, list[bool]] = {}
    per_field_cer: dict[str, list[float]] = {}
    for c in flat:
        leaf = c.field_path.rsplit(".", 1)[-1]
        per_field_hits.setdefault(leaf, []).append(c.is_match)
        per_field_case_hits.setdefault(leaf, []).append(c.is_case_match)
        if leaf in _CER_FIELDS:
            per_field_cer.setdefault(leaf, []).append(
                character_error_rate(c.expected, c.actual)
            )

    accuracy_by_field = {
        name: _safe_div(sum(hits), len(hits)) for name, hits in per_field_hits.items()
    }
    cer_by_field = {
        name: sum(values) / len(values) for name, values in per_field_cer.items() if values
    }
    case_accuracy_by_field = {
        name: _safe_div(sum(hits), len(hits))
        for name, hits in per_field_case_hits.items()
    }

    return BaselineMetrics(
        total_sheets=total_sheets,
        total_comparisons=total,
        field_accuracy=_safe_div(matches, total),
        perfect_sheet_rate=_safe_div(perfect_sheets, total_sheets),
        hallucination_rate=_safe_div(hallucinations, len(expected_empty)),
        critical_field_accuracy=_safe_div(critical_matches, len(critical)),
        accuracy_by_field=accuracy_by_field,
        cer_by_field=cer_by_field,
        case_sensitive_accuracy=_safe_div(sum(c.is_case_match for c in flat), total),
        case_sensitive_accuracy_by_field=case_accuracy_by_field,
    )


def worst_errors(
    comparisons_by_sheet: dict[str, list[FieldComparison]],
    *,
    n: int = 5,
) -> list[FieldComparison]:
    """Return up to *n* mismatches that are most worth eyeballing first.

    Priority: critical-field mismatches, then hallucinations, then plain
    mismatches.
    """
    mismatches = [
        c
        for sheet_comps in comparisons_by_sheet.values()
        for c in sheet_comps
        if not c.is_match
    ]

    def priority(c: FieldComparison) -> tuple[int, str]:
        if c.is_critical:
            band = 0
        elif c.is_hallucination:
            band = 1
        else:
            band = 2
        return band, c.field_path

    mismatches.sort(key=priority)
    return mismatches[:n]
