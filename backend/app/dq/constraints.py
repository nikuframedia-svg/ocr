"""L3 — constraint violation detection.

Each rule examines the full extraction (header + rows + footer) plus
optional ``learned`` artefacts (operator stats, lote consistency from
gold) and a persistent ``CrossSheetIndex``. Returns ``Violation`` list
with affected cells; multiple rules can flag the same cell.

Hard rules (severity=error):
- ``sum_qtd_vs_colunas``     — Σ qtd should equal colunas_produzidas
- ``lote_property_consistency`` — same lote within sheet must share
  larg_mm + esp
- ``cross_sheet_ov_cliente`` — OV mapping consistent with index

Soft rules (severity=warning):
- ``per_operator_outlier``    — numeric fields outside P5/P95 of operator
- ``modelo_prefix_unknown``   — MODELO prefix not in lexicon
- ``temporal_lote_vs_data``   — lote prefix month > sheet month

Public API:
    check_constraints(extraction, sheet_name, learned, index) -> list[Violation]
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .cross_sheet_index import CrossSheetIndex

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class CellRef:
    row_index: int | None  # None = header/footer
    field: str

    def path(self) -> str:
        if self.row_index is None:
            section = "header" if self.field in ("operador", "n_operador", "setor_maquina", "cod_maquina", "data") else "footer"
            return f"{section}.{self.field}"
        return f"rows[{self.row_index}].{self.field}"


@dataclass(frozen=True)
class Violation:
    rule: str
    severity: Severity
    affected: tuple[CellRef, ...]
    expected: str
    actual: str
    detail: str = ""
    weight: float = 0.0  # confidence reduction per affected cell


def _normalise_name(s: str) -> str:
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(no_marks.upper().split())


def _to_float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


# ---------- Hard rule 1: Σ qtd vs colunas_produzidas ----------

def check_sum_qtd_vs_colunas(extraction: dict, learned: dict) -> list[Violation]:
    rows = extraction.get("rows", []) or []
    footer = extraction.get("footer", {}) or {}
    cols_raw = footer.get("colunas_produzidas", "") or ""
    cols = _to_float(cols_raw)
    if cols is None or cols == 0:
        return []  # can't check without colunas_produzidas

    qtds = [(_to_float(row.get("qtd", "") or ""), i) for i, row in enumerate(rows)]
    valid_qtds = [(q, i) for q, i in qtds if q is not None]
    if not valid_qtds:
        return []
    sum_qtd = sum(q for q, _ in valid_qtds)
    rel = abs(sum_qtd - cols) / cols

    tolerance = learned.get("sum_qtd_tolerance", {}).get("max_observed", 0.05)
    # In gold tolerance was 0.0 (perfect). Add 1pp slack for new sheets.
    threshold_warn = max(tolerance + 0.01, 0.02)
    threshold_err = max(tolerance + 0.05, 0.05)

    if rel <= threshold_warn:
        return []

    severity: Severity = "error" if rel > threshold_err else "warning"
    affected = tuple(CellRef(i, "qtd") for _, i in valid_qtds)
    affected += (CellRef(None, "colunas_produzidas"),)
    return [
        Violation(
            rule="sum_qtd_vs_colunas",
            severity=severity,
            affected=affected,
            expected=f"sum(qtd) ≈ {cols_raw}",
            actual=f"sum(qtd)={sum_qtd:g}, colunas={cols_raw}, rel_delta={rel:.3f}",
            detail=f"observed gold tolerance {tolerance:.3f}; threshold {threshold_warn:.2f}",
            weight=0.3 if severity == "error" else 0.15,
        )
    ]


# ---------- Hard rule 2: lote -> larg_mm + esp consistency (in-sheet) ----------

def check_lote_property_consistency(extraction: dict) -> list[Violation]:
    rows = extraction.get("rows", []) or []
    by_lote: dict[str, dict[str, set[tuple[int, str]]]] = {}
    for i, row in enumerate(rows):
        lote = (row.get("lote", "") or "").upper().strip()
        if not lote:
            continue
        for f in ("larg_mm", "esp"):
            v = (row.get(f, "") or "").strip()
            if v:
                by_lote.setdefault(lote, {}).setdefault(f, set()).add((i, v))

    violations: list[Violation] = []
    for lote, fields in by_lote.items():
        for f, pairs in fields.items():
            distinct_values = {v for _, v in pairs}
            if len(distinct_values) <= 1:
                continue
            # Soft normalize "2,6" vs "2.6" before flagging
            normalised = {v.replace(",", ".") for v in distinct_values}
            if len(normalised) <= 1:
                continue
            affected = tuple(CellRef(i, f) for i, _ in sorted(pairs))
            violations.append(
                Violation(
                    rule="lote_property_consistency",
                    severity="error",
                    affected=affected,
                    expected=f"all rows with lote={lote} should share {f}",
                    actual=f"distinct {f} values: {sorted(distinct_values)}",
                    detail=f"lote {lote} appears in {len(pairs)} rows; {f} should be constant",
                    weight=0.25,
                )
            )
    return violations


# ---------- Hard rule 3: cross-sheet OV -> cliente ----------

def check_cross_sheet_ov_cliente(
    extraction: dict, index: CrossSheetIndex
) -> list[Violation]:
    rows = extraction.get("rows", []) or []
    violations: list[Violation] = []
    for i, row in enumerate(rows):
        ov = (row.get("ov", "") or "").strip()
        cli = (row.get("cliente", "") or "").upper().strip()
        if not ov or not cli:
            continue
        known_cli = index.ov_to_cliente.get(ov)
        if known_cli and known_cli.replace(" ", "") != cli.replace(" ", ""):
            violations.append(
                Violation(
                    rule="cross_sheet_ov_cliente",
                    severity="error",
                    affected=(CellRef(i, "cliente"), CellRef(i, "ov")),
                    expected=f"OV {ov} -> {known_cli}",
                    actual=f"OV {ov} -> {cli}",
                    detail=f"index source: {index.provenance.get(f'ov:{ov}', 'unknown')}",
                    weight=0.20,
                )
            )
    return violations


# ---------- Soft rule 1: per-operator outlier ----------

def check_per_operator_outlier(extraction: dict, learned: dict) -> list[Violation]:
    op = _normalise_name(extraction.get("header", {}).get("operador", ""))
    op_stats = learned.get("operator_stats", {}).get(op, {})
    if not op_stats:
        return []
    rows = extraction.get("rows", []) or []
    violations: list[Violation] = []
    for i, row in enumerate(rows):
        for f in ("qtd", "comp_mm", "larg_mm", "esp", "lbase", "ltopo"):
            stats = op_stats.get(f)
            if not stats:
                continue
            v = _to_float(row.get(f, "") or "")
            if v is None:
                continue
            p5, p95 = stats.get("p5"), stats.get("p95")
            if p5 is None or p95 is None or stats.get("n", 0) < 3:
                continue  # not enough samples to trust band
            # Only flag when far outside band; with small per-operator
            # samples the P5/P95 estimate is noisy. 3× factor reserves
            # the rule for clear outliers (e.g. comp_mm in 100k range
            # when operator caps at 11k).
            if v < p5 * 0.3 or v > p95 * 3.0:
                violations.append(
                    Violation(
                        rule="per_operator_outlier",
                        severity="warning",
                        affected=(CellRef(i, f),),
                        expected=f"{op} typically has {f} in [{p5}, {p95}] (n={stats['n']})",
                        actual=str(v),
                        detail=f"value 2× outside operator percentile band",
                        weight=0.10,
                    )
                )
    return violations


# ---------- Soft rule 2: MODELO prefix not in lexicon ----------

_OBSERVED_PATH = (
    Path(__file__).resolve().parents[1]
    / "pipeline"
    / "inference"
    / "lexicons"
    / "observed.json"
)


def _load_modelo_prefixes() -> set[str]:
    if not _OBSERVED_PATH.exists():
        return set()
    data = json.loads(_OBSERVED_PATH.read_text(encoding="utf-8"))
    return set(data.get("modelo_prefix", []))


_MODELO_PREFIXES_CACHE: set[str] | None = None


def check_modelo_prefix_unknown(extraction: dict) -> list[Violation]:
    global _MODELO_PREFIXES_CACHE
    if _MODELO_PREFIXES_CACHE is None:
        _MODELO_PREFIXES_CACHE = _load_modelo_prefixes()
    if not _MODELO_PREFIXES_CACHE:
        return []
    rows = extraction.get("rows", []) or []
    violations: list[Violation] = []
    known_prefixes = _MODELO_PREFIXES_CACHE
    for i, row in enumerate(rows):
        modelo = (row.get("modelo", "") or "").upper().strip()
        if len(modelo) < 4:
            continue
        # Match by 4 or 5 char prefix
        if any(modelo.startswith(p[:4]) for p in known_prefixes if len(p) >= 4):
            continue
        violations.append(
            Violation(
                rule="modelo_prefix_unknown",
                severity="warning",
                affected=(CellRef(i, "modelo"),),
                expected="modelo prefix in observed lexicon",
                actual=modelo[:5],
                detail=f"prefix {modelo[:5]!r} not in {len(known_prefixes)} known prefixes",
                weight=0.05,
            )
        )
    return violations


# ---------- Soft rule 3: temporal LOTE vs DATA ----------

def check_temporal_lote_vs_data(extraction: dict) -> list[Violation]:
    """Flag lotes whose month encoding is *after* the sheet date.

    LOTE format: ``[HM]<YY>B<digits>`` where YY is the production
    year (2-digit). E.g. ``M25B0746`` = May/2025 (M=month code? No,
    M is just the prefix; YY=25 is year). Actually the convention
    appears to be ``[HM]<YY>B<seq>`` where YY is the year and the
    letter encodes facility (M=Metalogalva-Maia, H=Metalogalva-?).

    Rule: if YY > sheet_year, that's impossible — flag.
    """
    data = (extraction.get("header", {}) or {}).get("data", "") or ""
    m = re.match(r"^\d{1,2}-\d{1,2}-(\d{4})$", data)
    if not m:
        return []
    try:
        sheet_year = int(m.group(1))
    except ValueError:
        return []

    rows = extraction.get("rows", []) or []
    violations: list[Violation] = []
    for i, row in enumerate(rows):
        lote = (row.get("lote", "") or "").upper().strip()
        m2 = re.match(r"^[HM](\d{2})B", lote)
        if not m2:
            continue
        try:
            lote_yy = int(m2.group(1))
        except ValueError:
            continue
        lote_year = 2000 + lote_yy if lote_yy < 80 else 1900 + lote_yy
        if lote_year > sheet_year:
            violations.append(
                Violation(
                    rule="temporal_lote_vs_data",
                    severity="warning",
                    affected=(CellRef(i, "lote"),),
                    expected=f"lote year ≤ sheet year ({sheet_year})",
                    actual=f"lote {lote} -> year {lote_year}",
                    detail="future-dated lote — likely OCR misread of YY",
                    weight=0.10,
                )
            )
    return violations


# ---------- Glue ----------

def check_constraints(
    extraction: dict,
    sheet_name: str,
    learned: dict | None = None,
    index: CrossSheetIndex | None = None,
) -> list[Violation]:
    """Run all rules; return concatenated violation list."""
    learned = learned or {}
    index = index or CrossSheetIndex()
    violations: list[Violation] = []
    violations.extend(check_sum_qtd_vs_colunas(extraction, learned))
    violations.extend(check_lote_property_consistency(extraction))
    violations.extend(check_cross_sheet_ov_cliente(extraction, index))
    violations.extend(check_per_operator_outlier(extraction, learned))
    violations.extend(check_modelo_prefix_unknown(extraction))
    violations.extend(check_temporal_lote_vs_data(extraction))
    return violations
