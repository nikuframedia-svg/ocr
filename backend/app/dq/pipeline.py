"""DQ pipeline orchestration: L1 → L2 → L3 → cell-level audit trail.

Takes a raw extraction (output of ``ocr6.py``) and returns a
:class:`DQResult` with:

- ``cells`` — per (row_index, field) audit: raw, L1 issues, L2 snap,
  L3 violations, final value, confidence, requires_review flag.
- ``violations`` — flat list of all L3 violations (cross-cell rules).
- ``aggregate_score`` — mean confidence across cells.
- ``stp_eligible`` — true iff zero hard violations and zero
  requires_review cells.

Also exposes ``apply_fixes`` to produce a fixed extraction (post-L2)
that can replace the raw output downstream.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.templates_registry import (
    DEFAULT_TEMPLATE,
    TemplateSpec,
    get_template,
)

from .constraints import (
    CellRef,
    Severity,
    Violation,
    check_constraints,
)
from .cross_sheet_index import CrossSheetIndex
from .per_field_matcher import cross_validate_cell
from .snap import snap_field
from .validators import (
    ValidationIssue,
    validate_extraction,
)

# Header is identical across all 11 templates (per templates_registry).
_HEADER_FIELDS = ("operador", "n_operador", "setor_maquina", "data")

# Bobine-Formato fallback fields — only used when an extraction has no
# template_name and detection fails. R54 makes everything else flow
# from the TemplateSpec instead.
_LEGACY_FOOTER_FIELDS = ("colunas_produzidas", "horas_trabalhadas")
_LEGACY_ROW_FIELDS = (
    "pri", "cliente", "ov", "of", "modelo", "qtd",
    "comp_mm", "larg_mm", "lote", "coni", "esp", "lbase", "ltopo",
)

# Canonical snap dependency order. `of` must snap before `ov`/`modelo`
# (which look up `plan_to_entries[of]`). Fields not in this order get
# snapped in their natural template.row_fields order. Fields not in the
# template are simply skipped.
_SNAP_ORDER_PRIORITY = (
    "of",        # OF first — its post-snap value is used by ov/modelo
    "ov",        # uses post-snap OF for plan-OV Lev-1 lookup
    "modelo",    # uses post-snap OF for plan.designacao lookup
    "lote",      # independent (SAP exact)
)


def _template_for_extraction(extraction: dict) -> TemplateSpec:
    """Read template_name from extraction; fall back to bobine_formato.

    Used by run_dq + cell map builder + snap iterator so the same
    `TemplateSpec` is consistent across all DQ phases.
    """
    tname = (extraction or {}).get("template_name")
    if tname:
        return get_template(tname)
    return DEFAULT_TEMPLATE


def _row_snap_order(template: TemplateSpec) -> tuple[str, ...]:
    """Return the snap order tailored to a template's row_fields.

    Priority fields (of/ov/modelo/lote) come first IF they exist in the
    template. Remaining fields keep their template-declared order.
    """
    fields_set = set(template.row_fields)
    head = tuple(f for f in _SNAP_ORDER_PRIORITY if f in fields_set)
    tail = tuple(f for f in template.row_fields if f not in head)
    return head + tail


@dataclass
class CellAudit:
    """Per-cell audit trail spanning L1 → L2 → L3."""
    field_path: str
    row_index: int | None
    field: str
    raw: str
    snapped: str                          # post-L2 (may equal raw)
    final: str                            # current best value (= snapped for now)
    fix_chain: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)       # L1
    violations: list[dict] = field(default_factory=list)   # L3
    confidence: float = 1.0
    requires_review: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DQResult:
    sheet: str
    cells: dict[str, CellAudit]            # field_path -> CellAudit
    violations: list[Violation]
    aggregate_score: float
    stp_eligible: bool
    summary: dict[str, int]                # quick counts

    def to_dict(self) -> dict:
        return {
            "sheet": self.sheet,
            "cells": {k: v.to_dict() for k, v in self.cells.items()},
            "violations": [
                {
                    "rule": v.rule,
                    "severity": v.severity,
                    "expected": v.expected,
                    "actual": v.actual,
                    "detail": v.detail,
                    "weight": v.weight,
                    "affected": [{"row_index": cr.row_index, "field": cr.field, "path": cr.path()}
                                 for cr in v.affected],
                }
                for v in self.violations
            ],
            "aggregate_score": round(self.aggregate_score, 3),
            "stp_eligible": self.stp_eligible,
            "summary": self.summary,
        }


def _path(row_index: int | None, field_name: str) -> str:
    """Return ``"rows[i].field"`` or ``"header.field"`` / ``"footer.field"``.

    For section detection (row_index is None), assumes the field belongs
    to header if it's one of the canonical header names; otherwise treats
    as footer. Header is constant across templates so this is safe.
    """
    if row_index is None:
        section = "header" if field_name in _HEADER_FIELDS else "footer"
        return f"{section}.{field_name}"
    return f"rows[{row_index}].{field_name}"


def _seed_cell(row_index: int | None, field_name: str, value: str) -> CellAudit:
    return CellAudit(
        field_path=_path(row_index, field_name),
        row_index=row_index,
        field=field_name,
        raw=value or "",
        snapped=value or "",
        final=value or "",
    )


def _build_cell_map(
    extraction: dict, template: TemplateSpec,
) -> dict[str, CellAudit]:
    """Build initial cell_map keyed by field_path.

    Round 54 — template-aware: iterates `template.row_fields` and
    `template.footer_fields` instead of hardcoded constants. Cells for
    fields not in the template aren't created (so the UI / cross-check
    never sees them).
    """
    cells: dict[str, CellAudit] = {}
    header = extraction.get("header", {}) or {}
    for f in _HEADER_FIELDS:
        cells[_path(None, f)] = _seed_cell(None, f, str(header.get(f, "") or ""))
    rows = extraction.get("rows", []) or []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for f in template.row_fields:
            cells[_path(i, f)] = _seed_cell(i, f, str(row.get(f, "") or ""))
    footer = extraction.get("footer", {}) or {}
    for f in template.footer_fields:
        cells[_path(None, f)] = _seed_cell(None, f, str(footer.get(f, "") or ""))
    return cells


def _apply_l1(cells: dict[str, CellAudit], issues: list[ValidationIssue]) -> None:
    for issue in issues:
        cell = cells.get(issue.field_path)
        if cell is None:
            continue
        cell.issues.append({
            "severity": issue.severity,
            "rule": issue.rule,
            "expected": issue.expected,
            "detail": issue.detail,
        })


def _apply_l2(
    cells: dict[str, CellAudit], template: TemplateSpec,
) -> dict:
    """Run snap on each cell. Returns a fixed extraction dict matching
    the raw shape (header/rows/footer).

    Round 54 — template-aware: only iterates the template's row_fields
    and footer_fields. Snap order is derived from
    ``_row_snap_order(template)`` so dependencies still resolve (OF
    before OV/MODELO).
    """
    fixed: dict = {"header": {}, "rows": [], "footer": {}}

    snap_order = _row_snap_order(template)
    row_fields = template.row_fields
    footer_fields = template.footer_fields

    # Header (same for all templates)
    for f in _HEADER_FIELDS:
        path = _path(None, f)
        cell = cells[path]
        res = snap_field(f, cell.raw)
        if res is not None and res.applied:
            cell.snapped = res.snapped
            cell.final = res.snapped
            cell.fix_chain.append(f"L2:{res.rule}:{res.raw!r}->{res.snapped!r}")
        fixed["header"][f] = cell.final

    # Rows — iterate template fields, in dependency order for snap
    row_indices = sorted({c.row_index for c in cells.values() if c.row_index is not None})
    for i in row_indices:
        # Live row state for cross-field snap context (e.g. modelo reads
        # post-snap OF for plan.designacao lookup).
        row_state: dict[str, str] = {}
        for f in row_fields:
            cell = cells.get(_path(i, f))
            if cell is not None:
                row_state[f] = cell.raw

        # Snap pass — priority order (OF/OV/MODELO/LOTE) then template
        # order. Fields not in this template's row_fields are simply
        # absent from the iteration.
        cascade_rules: dict[str, tuple[str, float]] = {}
        for f in snap_order:
            cell = cells.get(_path(i, f))
            if cell is None:
                continue
            res = snap_field(f, cell.raw, row_context=row_state)
            if res is not None and res.applied:
                cell.snapped = res.snapped
                cell.final = res.snapped
                cell.fix_chain.append(f"L2:{res.rule}:{res.raw!r}->{res.snapped!r}")
                row_state[f] = res.snapped
                cascade_rules[f] = (res.rule, res.distance)

        # Round 42 cross-validator — only meaningful if modelo/cliente
        # are in this template. For templates without them (paragens),
        # this loop is a no-op.
        for f in ("modelo", "cliente"):
            if f not in row_fields:
                continue
            cell = cells.get(_path(i, f))
            if cell is None or f not in cascade_rules:
                continue
            rule, dist = cascade_rules[f]
            cv = cross_validate_cell(f, cell.raw, cell.final, rule, dist)
            if cv is not None:
                cell.snapped = cv.best
                cell.final = cv.best
                cell.fix_chain.append(
                    f"L2:cascade_reverted_by_perfield:{cell.raw!r}->{cv.best!r}"
                )
                row_state[f] = cv.best

        # Build pass — natural template order for output dict
        row_out: dict[str, str] = {}
        for f in row_fields:
            cell = cells.get(_path(i, f))
            row_out[f] = cell.final if cell is not None else ""
        fixed["rows"].append(row_out)

    # Footer
    for f in footer_fields:
        cell = cells[_path(None, f)]
        res = snap_field(f, cell.raw)
        if res is not None and res.applied:
            cell.snapped = res.snapped
            cell.final = res.snapped
            cell.fix_chain.append(f"L2:{res.rule}:{res.raw!r}->{res.snapped!r}")
        fixed["footer"][f] = cell.final

    return fixed


def _apply_l3(
    cells: dict[str, CellAudit],
    violations: list[Violation],
) -> None:
    for v in violations:
        for cr in v.affected:
            path = cr.path()
            cell = cells.get(path)
            if cell is None:
                continue
            cell.violations.append({
                "rule": v.rule,
                "severity": v.severity,
                "expected": v.expected,
                "actual": v.actual,
                "detail": v.detail,
                "weight": v.weight,
            })


def _compute_confidence(cell: CellAudit) -> tuple[float, bool]:
    """Cell confidence = 1 - sum(violation weights), clamped to [0, 1].
    Hard violations (severity=error from L1 or L3) trigger requires_review
    regardless of confidence threshold.
    """
    weight_sum = sum(v.get("weight", 0.0) for v in cell.violations)
    # L1 errors contribute 0.20, warnings 0.10
    for issue in cell.issues:
        weight_sum += 0.20 if issue.get("severity") == "error" else 0.10
    confidence = max(0.0, min(1.0, 1.0 - weight_sum))

    has_hard = any(i.get("severity") == "error" for i in cell.issues)
    has_hard = has_hard or any(v.get("severity") == "error" for v in cell.violations)
    requires_review = has_hard or confidence < 0.7
    return confidence, requires_review


def run_dq(
    extraction: dict,
    sheet_name: str,
    learned: dict | None = None,
    index: CrossSheetIndex | None = None,
) -> tuple[DQResult, dict]:
    """Run the full DQ pipeline.

    Returns:
        (DQResult, fixed_extraction): the audit + the post-L2 fixed
        version of the extraction (suitable for downstream use).

    Side effects:
        ``index`` is updated via ``index.observe(sheet_name, fixed)`` —
        cross-sheet store grows as sheets are processed.
    """
    learned = learned or {}
    index = index or CrossSheetIndex()

    # Round 54 — template-aware. Defaults to bobine_formato for legacy
    # extractions (no template_name field present).
    template = _template_for_extraction(extraction)

    cells = _build_cell_map(extraction, template)

    # L1
    issues = validate_extraction(extraction)
    _apply_l1(cells, issues)

    # L2 — produce fixed extraction (template-aware iteration)
    fixed = _apply_l2(cells, template)

    # Carry template_name through to fixed so downstream (cross-check,
    # UI, CSV writer) keeps using the right schema. For legacy
    # extractions (no template_name) we still emit one — bobine_formato.
    fixed["template_name"] = template.name

    # L3 — run on FIXED (post-L2) values so cliente/modelo snap is
    # already applied (avoids spurious L3 violations from typos that
    # L2 already corrected).
    violations = check_constraints(fixed, sheet_name, learned, index)
    _apply_l3(cells, violations)

    # Score each cell
    n_review = 0
    total_conf = 0.0
    for cell in cells.values():
        cell.confidence, cell.requires_review = _compute_confidence(cell)
        total_conf += cell.confidence
        if cell.requires_review:
            n_review += 1

    aggregate = total_conf / len(cells) if cells else 1.0
    has_hard_violation = any(v.severity == "error" for v in violations)
    stp_eligible = (n_review == 0) and (not has_hard_violation)

    summary = {
        "n_cells": len(cells),
        "n_l1_issues": len(issues),
        "n_l3_violations": len(violations),
        "n_l3_errors": sum(1 for v in violations if v.severity == "error"),
        "n_l2_fixes": sum(1 for c in cells.values() if c.fix_chain),
        "n_requires_review": n_review,
    }

    # Update cross-sheet index (using fixed extraction)
    index.observe(sheet_name, fixed)

    return (
        DQResult(
            sheet=sheet_name,
            cells=cells,
            violations=violations,
            aggregate_score=aggregate,
            stp_eligible=stp_eligible,
            summary=summary,
        ),
        fixed,
    )
