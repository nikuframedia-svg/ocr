#!/usr/bin/env python3
"""Evaluate cross-check final values against exported OCR/current-result pairs.

Read-only diagnostic. It does not update the DB, refs, cross-check JSON, or
sheet data. Run it from any engine revision to produce comparable metrics:

    uv run python scripts/diag/evaluate_cross_outputs.py

The quality metric is the final value emitted by the cross-check, not the
legacy MATCH/NO_MATCH colour.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from app.cross_check.ref_watcher import get_watcher
from app.pipeline.scoring_engine import (
    ENGINE_VERSION,
    _cliente_tokens,
    _format_value,
    _get_indices,
    _model_compact,
    _num,
    cross_check_sheet,
    normalize_of,
)

_DEFAULT_SAMPLE_DIR = Path("/Users/martimnicolau/Downloads/ultimos_150_ocr")
_DEFAULT_OUT_DIR = _REPO / "reports" / "cross_output_eval"

_NUMERIC_FIELDS = {
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo",
    "qtd", "qtd_metros", "m2", "sobras", "colunas_produzidas",
}

_CROSSABLE_FIELDS = {
    "of", "ov", "cliente", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo",
}

_UNVALIDATED_CROSS_SOURCES = {
    "ocr_selected", "ocr_raw", "raw_observation", "syntax", "ref_unavailable",
}

_DIM_REF_ATTR = {
    "comp_mm": "comp",
    "larg_mm": "larg",
    "lbase": "lbase",
    "ltopo": "ltopo",
    "esp": "esp",
    "dbase": "dbase",
    "dtopo": "dtopo",
}


@dataclass(frozen=True)
class CellCase:
    sheet_id: str
    template: str
    section: str
    row_index: int | None
    field: str
    path: str
    raw: str
    truth: str
    output: str
    status: str
    engine_status: str
    source: str
    decision_source: str
    ref: str
    ref_source: str
    proposed: str
    match_kind: str
    decision_reason: str
    decision_confidence: str
    truth_ref_available: bool
    validated_output_equals_truth: bool
    cross_contract_violation: bool


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _norm(field: str, value: object) -> str:
    text = _format_value(field, value) if field in _NUMERIC_FIELDS else str(value or "").strip()
    return " ".join(text.upper().split())


def _cliente_compact_keys(value: object) -> set[str]:
    tokens = tuple(_cliente_tokens(value))
    keys = {"".join(tokens)} if tokens else set()
    if len(tokens) > 1:
        keys.add(" ".join(tokens))
    return {key for key in keys if key}


def _truth_ref_available(field: str, truth: object, refs: dict, idx: dict) -> bool:
    """Whether the expected final value is present in loaded refs.

    This is an audit dimension, not a pass/fail excuse: it separates engine
    mistakes from cases where the silver truth cannot be validated by the
    package supplied to both engines.
    """
    value = str(truth or "").strip()
    if not value:
        return True
    if field == "of":
        return normalize_of(value) in (refs.get("of_to_entries") or {})
    if field == "ov":
        compact = "".join(ch for ch in value if ch.isdigit())
        return bool(compact and compact in (idx.get("ov_to_entries") or {}))
    if field == "modelo":
        compact = _model_compact(value)
        if not compact:
            return False
        for key in idx.get("des_keys") or ():
            key_compact = _model_compact(key)
            if (
                key_compact == compact
                or key_compact.startswith(compact)
                or compact.startswith(key_compact)
            ):
                return True
        for key in idx.get("model_ft_keys") or ():
            key_compact = _model_compact(key)
            if (
                key_compact
                and (
                    key_compact == compact
                    or key_compact.startswith(compact)
                    or compact.startswith(key_compact)
                )
            ):
                return True
        return False
    if field == "cliente":
        if not _cliente_tokens(value):
            return False
        value_keys = _cliente_compact_keys(value)
        return any(
            value_keys & _cliente_compact_keys(cliente)
            for cliente in (refs.get("clientes_plan") or ())
        )
    if field == "lote":
        compact = "".join(ch for ch in value.upper() if ch.isalnum())
        return compact in (refs.get("lotes_sap_full") or {})
    attr = _DIM_REF_ATTR.get(field)
    if attr:
        n = _num(value)
        return n is not None and n in (idx.get("dim_indices") or {}).get(attr, {})
    return True


def _cross_contract_violation(case: "CellCase") -> bool:
    if case.field not in _CROSSABLE_FIELDS:
        return False
    if not str(case.output or "").strip():
        return False
    return (
        case.source in _UNVALIDATED_CROSS_SOURCES
        or case.decision_source == "ocr_candidate"
    )


def _sheet_paths(sheet: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for section in ("header", "footer"):
        data = sheet.get(section) or {}
        for field, value in data.items():
            out[f"{section}.{field}"] = "" if value is None else str(value)
    for i, row in enumerate(sheet.get("rows") or []):
        for field, value in (row or {}).items():
            out[f"rows[{i}].{field}"] = "" if value is None else str(value)
    return out


def _cross_paths(result: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for section in ("header", "footer"):
        for field, cell in (result.get(section) or {}).items():
            out[f"{section}.{field}"] = cell or {}
    for row in result.get("rows") or []:
        i = int(row.get("row_index") or 0)
        for field, cell in (row.get("fields") or {}).items():
            out[f"rows[{i}].{field}"] = cell or {}
    return out


def _path_parts(path: str) -> tuple[str, int | None, str]:
    if path.startswith("rows["):
        left, field = path.split("].", 1)
        return "rows", int(left[5:]), field
    section, field = path.split(".", 1)
    return section, None, field


def _iter_cases(sample_dir: Path, refs: dict, sections: set[str]) -> list[CellCase]:
    manifest = _read_manifest(sample_dir / "manifest.csv")
    idx = _get_indices(refs)
    cases: list[CellCase] = []
    for item in manifest:
        sheet_id = str(item.get("sheet_id") or "").strip()
        raw_rel = Path(str(item.get("ocr_original") or "").replace("\\", "/"))
        truth_rel = Path(str(item.get("resultado_atual") or "").replace("\\", "/"))
        raw_path = sample_dir / raw_rel
        truth_path = sample_dir / truth_rel
        if not raw_path.exists() or not truth_path.exists():
            continue
        raw_sheet = _read_json(raw_path)
        truth_sheet = _read_json(truth_path)
        result = cross_check_sheet(raw_sheet, None, refs)
        raw_values = _sheet_paths(raw_sheet)
        truth_values = _sheet_paths(truth_sheet)
        cells = _cross_paths(result)
        template = str(truth_sheet.get("template_name") or raw_sheet.get("template_name") or "")
        for path in sorted(set(raw_values) | set(truth_values) | set(cells)):
            section, row_index, field = _path_parts(path)
            if section not in sections:
                continue
            cell = cells.get(path) or {}
            raw = raw_values.get(path, "")
            truth = truth_values.get(path, "")
            output = str(cell.get("value") if cell else raw)
            source = str(cell.get("source") or "")
            decision_source = str(cell.get("decision_source") or "")
            truth_available = _truth_ref_available(field, truth, refs, idx)
            provisional = CellCase(
                sheet_id=sheet_id,
                template=template,
                section=section,
                row_index=row_index,
                field=field,
                path=path,
                raw=raw,
                truth=truth,
                output=output,
                status=str(cell.get("status") or ""),
                engine_status=str(cell.get("engine_status") or ""),
                source=source,
                decision_source=decision_source,
                ref=str(cell.get("ref") or ""),
                ref_source=str(cell.get("ref_source") or ""),
                proposed=str(cell.get("proposed") or ""),
                match_kind=str(cell.get("match_kind") or ""),
                decision_reason=str(cell.get("decision_reason") or ""),
                decision_confidence=str(cell.get("decision_confidence") or ""),
                truth_ref_available=truth_available,
                validated_output_equals_truth=False,
                cross_contract_violation=False,
            )
            out_eq = _norm(field, output) == _norm(field, truth)
            violation = _cross_contract_violation(provisional)
            cases.append(CellCase(
                **{
                    **provisional.__dict__,
                    "validated_output_equals_truth": bool(
                        out_eq and field in _CROSSABLE_FIELDS and not violation
                    ),
                    "cross_contract_violation": violation,
                }
            ))
    return cases


def _metrics(cases: list[CellCase]) -> dict[str, Any]:
    evaluated = len(cases)
    raw_ok = output_ok = corrected = regressed = changed_other_wrong = 0
    crossable = crossable_output_ok = crossable_truth_available = 0
    crossable_reachable_ok = validated_output_ok = contract_violations = 0
    status_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    unavailable_by_field: Counter[str] = Counter()
    no_match_but_output_truth = 0
    for case in cases:
        raw_eq = _norm(case.field, case.raw) == _norm(case.field, case.truth)
        out_eq = _norm(case.field, case.output) == _norm(case.field, case.truth)
        out_eq_raw = _norm(case.field, case.output) == _norm(case.field, case.raw)
        if case.field in _CROSSABLE_FIELDS:
            crossable += 1
            if out_eq:
                crossable_output_ok += 1
            if case.truth_ref_available:
                crossable_truth_available += 1
                if out_eq:
                    crossable_reachable_ok += 1
            else:
                unavailable_by_field[case.field] += 1
            if case.validated_output_equals_truth:
                validated_output_ok += 1
            if case.cross_contract_violation:
                contract_violations += 1
        if raw_eq:
            raw_ok += 1
            if not out_eq:
                regressed += 1
        if out_eq:
            output_ok += 1
        if not raw_eq and out_eq:
            corrected += 1
        if not raw_eq and not out_eq and not out_eq_raw:
            changed_other_wrong += 1
        if case.status:
            status_counts[case.status] += 1
        if case.decision_source:
            decision_counts[case.decision_source] += 1
        if case.status == "NO_MATCH" and out_eq:
            no_match_but_output_truth += 1
    needs_correction = evaluated - raw_ok
    return {
        "engine_version": ENGINE_VERSION,
        "evaluated": evaluated,
        "raw_already_ok": raw_ok,
        "needs_correction": needs_correction,
        "output_equals_truth": output_ok,
        "corrected_to_truth": corrected,
        "regressed_good_raw": regressed,
        "changed_to_other_wrong": changed_other_wrong,
        "output_accuracy_vs_resultado_atual_pct": round((output_ok / evaluated * 100.0) if evaluated else 0.0, 2),
        "correction_rate_when_raw_wrong_pct": round((corrected / needs_correction * 100.0) if needs_correction else 0.0, 2),
        "preserve_rate_when_raw_ok_pct": round(((raw_ok - regressed) / raw_ok * 100.0) if raw_ok else 0.0, 2),
        "status_counts": dict(status_counts),
        "decision_source_counts": dict(decision_counts),
        "no_match_but_output_truth": no_match_but_output_truth,
        "crossable_evaluated": crossable,
        "crossable_output_equals_truth": crossable_output_ok,
        "crossable_output_accuracy_pct": round(
            (crossable_output_ok / crossable * 100.0) if crossable else 0.0,
            2,
        ),
        "crossable_truth_ref_available": crossable_truth_available,
        "crossable_truth_ref_unavailable": crossable - crossable_truth_available,
        "crossable_reachable_output_equals_truth": crossable_reachable_ok,
        "crossable_reachable_accuracy_pct": round(
            (
                crossable_reachable_ok / crossable_truth_available * 100.0
            ) if crossable_truth_available else 0.0,
            2,
        ),
        "validated_output_equals_truth": validated_output_ok,
        "validated_output_accuracy_pct": round(
            (validated_output_ok / crossable * 100.0) if crossable else 0.0,
            2,
        ),
        "cross_contract_violations": contract_violations,
        "truth_ref_unavailable_by_field": dict(unavailable_by_field),
    }


def _breakdown(cases: list[CellCase], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[CellCase]] = defaultdict(list)
    for case in cases:
        grouped[str(getattr(case, key))].append(case)
    rows: list[dict[str, Any]] = []
    for value, items in grouped.items():
        m = _metrics(items)
        rows.append({
            key: value,
            "evaluated": m["evaluated"],
            "output_accuracy_pct": m["output_accuracy_vs_resultado_atual_pct"],
            "corrected_to_truth": m["corrected_to_truth"],
            "regressed_good_raw": m["regressed_good_raw"],
            "changed_to_other_wrong": m["changed_to_other_wrong"],
            "no_match": m["status_counts"].get("NO_MATCH", 0),
            "crossable_reachable_accuracy_pct": m["crossable_reachable_accuracy_pct"],
            "validated_output_accuracy_pct": m["validated_output_accuracy_pct"],
            "cross_contract_violations": m["cross_contract_violations"],
        })
    rows.sort(key=lambda row: (-int(row["regressed_good_raw"]), -int(row["changed_to_other_wrong"]), str(row[key])))
    return rows


def _regression_rows(cases: list[CellCase]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        raw_eq = _norm(case.field, case.raw) == _norm(case.field, case.truth)
        out_eq = _norm(case.field, case.output) == _norm(case.field, case.truth)
        if raw_eq and not out_eq:
            kind = "regressed_good_raw"
        elif not raw_eq and not out_eq and _norm(case.field, case.output) != _norm(case.field, case.raw):
            kind = "changed_to_other_wrong"
        else:
            continue
        rows.append({
            "kind": kind,
            "sheet_id": case.sheet_id,
            "template": case.template,
            "path": case.path,
            "field": case.field,
            "raw": case.raw,
            "output": case.output,
            "resultado_atual": case.truth,
            "status": case.status,
            "source": case.source,
            "decision_source": case.decision_source,
            "ref": case.ref,
            "ref_source": case.ref_source,
            "proposed": case.proposed,
            "match_kind": case.match_kind,
            "decision_reason": case.decision_reason,
            "decision_confidence": case.decision_confidence,
            "truth_ref_available": int(case.truth_ref_available),
            "validated_output_equals_truth": int(case.validated_output_equals_truth),
            "cross_contract_violation": int(case.cross_contract_violation),
        })
    return rows


def _case_rows(cases: list[CellCase]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        raw_eq = _norm(case.field, case.raw) == _norm(case.field, case.truth)
        out_eq = _norm(case.field, case.output) == _norm(case.field, case.truth)
        out_eq_raw = _norm(case.field, case.output) == _norm(case.field, case.raw)
        rows.append({
            "sheet_id": case.sheet_id,
            "template": case.template,
            "path": case.path,
            "field": case.field,
            "raw": case.raw,
            "output": case.output,
            "resultado_atual": case.truth,
            "raw_equals_truth": int(raw_eq),
            "output_equals_truth": int(out_eq),
            "output_equals_raw": int(out_eq_raw),
            "status": case.status,
            "engine_status": case.engine_status,
            "source": case.source,
            "decision_source": case.decision_source,
            "ref": case.ref,
            "ref_source": case.ref_source,
            "proposed": case.proposed,
            "match_kind": case.match_kind,
            "decision_reason": case.decision_reason,
            "decision_confidence": case.decision_confidence,
            "truth_ref_available": int(case.truth_ref_available),
            "validated_output_equals_truth": int(case.validated_output_equals_truth),
            "cross_contract_violation": int(case.cross_contract_violation),
        })
    return rows


def _write_report(out_dir: Path, metrics: dict[str, Any], elapsed_s: float) -> None:
    lines = [
        f"# Cross output evaluation — {metrics['engine_version']}",
        "",
        f"- evaluated: {metrics['evaluated']}",
        f"- output accuracy vs resultado_atual: {metrics['output_accuracy_vs_resultado_atual_pct']}%",
        f"- correction rate when raw wrong: {metrics['correction_rate_when_raw_wrong_pct']}%",
        f"- preserve rate when raw ok: {metrics['preserve_rate_when_raw_ok_pct']}%",
        f"- regressed good raw: {metrics['regressed_good_raw']}",
        f"- changed to other wrong: {metrics['changed_to_other_wrong']}",
        f"- NO_MATCH but output truth: {metrics['no_match_but_output_truth']}",
        f"- crossable output accuracy: {metrics['crossable_output_accuracy_pct']}%",
        f"- crossable reachable accuracy: {metrics['crossable_reachable_accuracy_pct']}%",
        f"- validated output accuracy: {metrics['validated_output_accuracy_pct']}%",
        f"- cross contract violations: {metrics['cross_contract_violations']}",
        f"- elapsed seconds: {elapsed_s:.2f}",
        "",
        "See `summary.json`, `field_breakdown.csv`, `template_breakdown.csv`, "
        "`sheet_breakdown.csv`, and `regressions.csv`.",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", type=Path, default=_DEFAULT_SAMPLE_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument(
        "--sections",
        default="rows",
        help="Comma-separated sections to evaluate: rows,header,footer,all",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    refs = get_watcher().get_refs()
    if not refs.get("available"):
        raise SystemExit("refs unavailable; cannot evaluate")
    requested_sections = {
        s.strip()
        for s in str(args.sections or "rows").split(",")
        if s.strip()
    }
    sections = {"rows", "header", "footer"} if "all" in requested_sections else requested_sections
    cases = _iter_cases(args.sample_dir, refs, sections)
    metrics = _metrics(cases)
    elapsed_s = time.perf_counter() - started
    # R236 — runtime no summary.json: a condição 5 do protocolo (não piorar
    # >10% em tempo) precisa deste número; antes só existia no report.md.
    metrics["elapsed_s"] = round(elapsed_s, 2)

    out_dir = args.out_dir / ENGINE_VERSION
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_csv(out_dir / "field_breakdown.csv", _breakdown(cases, "field"), [
        "field", "evaluated", "output_accuracy_pct", "corrected_to_truth",
        "regressed_good_raw", "changed_to_other_wrong", "no_match",
        "crossable_reachable_accuracy_pct", "validated_output_accuracy_pct",
        "cross_contract_violations",
    ])
    _write_csv(out_dir / "template_breakdown.csv", _breakdown(cases, "template"), [
        "template", "evaluated", "output_accuracy_pct", "corrected_to_truth",
        "regressed_good_raw", "changed_to_other_wrong", "no_match",
        "crossable_reachable_accuracy_pct", "validated_output_accuracy_pct",
        "cross_contract_violations",
    ])
    _write_csv(out_dir / "sheet_breakdown.csv", _breakdown(cases, "sheet_id"), [
        "sheet_id", "evaluated", "output_accuracy_pct", "corrected_to_truth",
        "regressed_good_raw", "changed_to_other_wrong", "no_match",
        "crossable_reachable_accuracy_pct", "validated_output_accuracy_pct",
        "cross_contract_violations",
    ])
    _write_csv(out_dir / "regressions.csv", _regression_rows(cases), [
        "kind", "sheet_id", "template", "path", "field", "raw", "output",
        "resultado_atual", "status", "source", "decision_source", "ref",
        "ref_source", "proposed", "match_kind", "decision_reason",
        "decision_confidence", "truth_ref_available",
        "validated_output_equals_truth", "cross_contract_violation",
    ])
    _write_csv(out_dir / "cells.csv", _case_rows(cases), [
        "sheet_id", "template", "path", "field", "raw", "output",
        "resultado_atual", "raw_equals_truth", "output_equals_truth",
        "output_equals_raw", "status", "engine_status", "source",
        "decision_source", "ref", "ref_source", "proposed", "match_kind",
        "decision_reason", "decision_confidence", "truth_ref_available",
        "validated_output_equals_truth", "cross_contract_violation",
    ])
    _write_report(out_dir, metrics, elapsed_s)
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(f"report_dir={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
