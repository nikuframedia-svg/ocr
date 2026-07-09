#!/usr/bin/env python3
"""Diagnose the R31 -> R33 apparent error-rate regression.

This is intentionally read-only with respect to production data: it consumes
the exported "last 150 OCR" analysis CSVs and writes a diagnostic pack under
reports/.  It does not run OCR, change refs, or alter the cross-check engine.

Usage:
    uv run python scripts/diag/r31_r33_regression.py
    uv run python scripts/diag/r31_r33_regression.py \
        --analysis-dir reports/cross_r31_r33_ultimos_150 \
        --out-dir reports/r31_r33_regression
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_ANALYSIS_DIR = _REPO / "reports" / "cross_r31_r33_ultimos_150"
_DEFAULT_OUT_DIR = _REPO / "reports" / "r31_r33_regression"

_KNOWN_CLIENT_ALIASES = {
    "TECPOLES": "TECPOLES GMBH",
    "LEVITEC": "LEVITEC SISTEMAS SL",
    "MTG": "MTG GMBH",
    "GMBH": "MTG GMBH",
    "SUNVA": "SUNNA",
    "SUNIVA": "SUNNA",
    "COL MAR": "COLMAR",
    "OIL MAR": "COLMAR",
}


@dataclass(frozen=True)
class Rates:
    r31: float = 0.08
    r33: float = 0.13

    @property
    def delta(self) -> float:
        return max(self.r33 - self.r31, 0.0)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _norm(value: object) -> str:
    text = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return text.replace("O", "0").replace("I", "1").replace("L", "1")


def _norm_words(value: object) -> str:
    text = str(value or "").upper()
    text = text.replace(".", " ").replace(",", " ")
    return " ".join(re.findall(r"[A-Z0-9]+", text))


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _has_letters(value: object) -> bool:
    return any(ch.isalpha() for ch in str(value or ""))


def _has_digits(value: object) -> bool:
    return any(ch.isdigit() for ch in str(value or ""))


def _numeric_token(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _is_well_formed_numeric_of(value: object) -> bool:
    token = _numeric_token(value)
    return len(token) == 6 and str(value or "").strip() == token


def _looks_like_model_or_text_in_of(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _is_well_formed_numeric_of(text):
        return False
    return _has_letters(text) or (" " in text and _has_digits(text))


def _client_alias_gap(value: object, ref: object) -> bool:
    left_words = _norm_words(value)
    right_words = _norm_words(ref)
    if not left_words or not right_words:
        return False

    alias_target = _KNOWN_CLIENT_ALIASES.get(left_words)
    if alias_target and _norm_words(alias_target) in right_words:
        return True
    if len(left_words) >= 3 and (left_words in right_words or right_words in left_words):
        return True

    left = _norm(left_words)
    right = _norm(right_words)
    return _ratio(left, right) >= 0.78


def _close_digit_error(value: object, ref: object) -> bool:
    left = _numeric_token(value)
    right = _numeric_token(ref)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    return _ratio(left, right) >= 0.82


def _diff_key(row: dict[str, str]) -> tuple[str, str]:
    return str(row.get("sheet_id") or ""), str(row.get("path") or "")


def _group_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("sheet_id") or ""),
        str(row.get("field") or ""),
        str(row.get("value") or ""),
        str(row.get("reason") or ""),
        str(row.get("ref") or ""),
    )


def _classify_no_match(
    row: dict[str, str],
    diff: dict[str, str] | None,
    *,
    repeat_group_count: int,
) -> tuple[str, str, str]:
    """Return (error_class, root_cause, recommendation)."""
    field = str(row.get("field") or "")
    value = str(row.get("value") or "")
    ref = str(row.get("ref") or "")
    reason = str(row.get("reason") or "")
    template = str(row.get("template") or "")
    diff_kind = str((diff or {}).get("kind") or "")
    current_value = str((diff or {}).get("resultado_atual") or "")

    if field == "of":
        if repeat_group_count >= 4:
            return (
                "inflated_repetition",
                "repeated_of_not_in_plan",
                "Group repeated same OF failure per sheet; report as one cause plus occurrences.",
            )
        if _looks_like_model_or_text_in_of(value) or template == "expedicao":
            return (
                "r33_exposed_no_autofill",
                "of_field_contains_model_or_text",
                "Try safe row realignment before counting OF as a real error.",
            )
        if _is_well_formed_numeric_of(value) and current_value:
            return (
                "r33_exposed_no_autofill",
                "of_numeric_corrected_by_later_engine",
                "Validate contextual OF recovery against modelo/cliente/ov/comp.",
            )
        if _is_well_formed_numeric_of(value):
            return (
                "ref_missing_or_stale",
                "of_numeric_missing_in_plan",
                "Check whether plan_colunas snapshot is stale before counting as OCR error.",
            )
        return (
            "real_ocr_or_system",
            "of_unrecoverable",
            "Manual review; not enough context for safe recovery.",
        )

    if field == "lote" and "StockSAP" in reason:
        return (
            "ref_missing_or_stale",
            "lote_missing_stocksap",
            "Check StockSAP freshness or mark as REF_MISSING when row context is strong.",
        )

    if field == "cliente":
        if _client_alias_gap(value, ref):
            return (
                "ref_missing_or_stale",
                "cliente_alias_or_canonical_gap",
                "Normalize client aliases before NO_MATCH.",
            )
        if diff_kind in {"ok_corrigiu", "parecido"}:
            return (
                "r33_exposed_no_autofill",
                "cliente_corrected_by_later_engine",
                "Keep correction candidate, but distinguish from real unfixable error.",
            )
        return (
            "real_ocr_or_system",
            "cliente_wrong_or_wrong_winner",
            "Review winner selection and OCR value.",
        )

    if field == "ov":
        if _close_digit_error(value, ref):
            return (
                "r33_exposed_no_autofill",
                "ov_truncated_or_digit_error",
                "Recover only with strong OF/modelo context; otherwise keep red.",
            )
        return (
            "real_ocr_or_system",
            "ov_wrong_candidate",
            "Review row winner; OV mismatch is not a safe alias.",
        )

    if field == "modelo":
        if diff_kind in {"ok_corrigiu", "parecido"}:
            return (
                "r33_exposed_no_autofill",
                "modelo_corrected_or_expanded_by_later_engine",
                "Use contextual model recovery, but keep unrelated swaps visible.",
            )
        return (
            "real_ocr_or_system",
            "modelo_wrong_or_unrelated",
            "Manual review; model does not relate cleanly to plan.",
        )

    if field in {"esp", "larg_mm", "comp_mm", "lbase", "ltopo"}:
        if "não numérico" in reason or "nao numerico" in reason:
            return (
                "real_ocr_or_system",
                "numeric_parse_error",
                "Fix OCR/field parsing; do not hide non-numeric values as refs issue.",
            )
        return (
            "real_ocr_or_system",
            "numeric_delta",
            "Keep as real production/OCR difference unless row winner is proven wrong.",
        )

    return (
        "real_ocr_or_system",
        "other_no_match",
        "Manual review.",
    )


def classify_no_match_rows(
    no_matches: list[dict[str, str]],
    diffs: list[dict[str, str]],
) -> list[dict[str, Any]]:
    diff_by_key = {_diff_key(row): row for row in diffs}
    group_counts = Counter(_group_key(row) for row in no_matches)
    out: list[dict[str, Any]] = []
    for row in no_matches:
        diff = diff_by_key.get(_diff_key(row))
        group_count = group_counts[_group_key(row)]
        error_class, root_cause, recommendation = _classify_no_match(
            row,
            diff,
            repeat_group_count=group_count,
        )
        out.append({
            "sheet_id": row.get("sheet_id", ""),
            "template": row.get("template", ""),
            "path": row.get("path", ""),
            "field": row.get("field", ""),
            "value": row.get("value", ""),
            "ref": row.get("ref", ""),
            "reason": row.get("reason", ""),
            "resultado_atual": (diff or {}).get("resultado_atual", ""),
            "diff_kind": (diff or {}).get("kind", ""),
            "error_class": error_class,
            "root_cause": root_cause,
            "repeat_group_count": group_count,
            "repeat_extra_occurrences": max(group_count - 1, 0),
            "recommendation": recommendation,
        })
    return out


def _summary_rows(
    classified: list[dict[str, Any]],
    *,
    rates: Rates,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    total_no_match = len(classified)
    denominator = total_no_match / rates.r33 if rates.r33 else 0.0
    expected_r31_errors = denominator * rates.r31 if denominator else 0.0
    delta_errors = denominator * rates.delta if denominator else 0.0

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    unique_group_keys: dict[tuple[str, str], set[tuple[str, str, str, str, str]]] = defaultdict(set)
    counted_repeat_groups: dict[tuple[str, str], set[tuple[str, str, str, str, str]]] = defaultdict(set)
    for row in classified:
        key = (str(row["error_class"]), str(row["root_cause"]))
        bucket = grouped.setdefault(
            key,
            {
                "error_class": key[0],
                "root_cause": key[1],
                "occurrences": 0,
                "repeat_extra_occurrences": 0,
                "examples": [],
            },
        )
        bucket["occurrences"] += 1
        if len(bucket["examples"]) < 5:
            bucket["examples"].append(
                (
                    f"{row.get('sheet_id', '')}:{row.get('path', '')}="
                    f"{row.get('value', '')} -> {row.get('resultado_atual', '')}"
                )
            )
        group_key = (
            str(row.get("sheet_id", "")),
            str(row.get("field", "")),
            str(row.get("value", "")),
            str(row.get("reason", "")),
            str(row.get("ref", "")),
        )
        unique_group_keys[key].add(group_key)
        if group_key not in counted_repeat_groups[key]:
            counted_repeat_groups[key].add(group_key)
            bucket["repeat_extra_occurrences"] += max(
                int(row.get("repeat_group_count") or 1) - 1,
                0,
            )

    rows: list[dict[str, Any]] = []
    for key, bucket in grouped.items():
        occ = int(bucket["occurrences"])
        rows.append({
            "error_class": bucket["error_class"],
            "root_cause": bucket["root_cause"],
            "occurrences": occ,
            "unique_causes": len(unique_group_keys[key]),
            "share_of_r33_no_match_pct": round(100 * occ / total_no_match, 2)
            if total_no_match
            else 0.0,
            "impact_pp_inside_13pct": round(100 * occ / denominator, 2)
            if denominator
            else 0.0,
            "share_of_5pp_delta_upper_pct": round(100 * occ / delta_errors, 2)
            if delta_errors
            else 0.0,
            "repeat_extra_occurrences": bucket["repeat_extra_occurrences"],
            "examples": " | ".join(bucket["examples"]),
        })
    rows.sort(key=lambda item: (-int(item["occurrences"]), item["error_class"]))
    metrics = {
        "total_no_match": float(total_no_match),
        "estimated_denominator": denominator,
        "expected_r31_errors": expected_r31_errors,
        "delta_errors": delta_errors,
    }
    return rows, metrics


def _sheet_rows(classified: list[dict[str, Any]], sheet_summary: list[dict[str, str]]) -> list[dict[str, Any]]:
    by_sheet: dict[str, Counter] = defaultdict(Counter)
    causes_by_sheet: dict[str, Counter] = defaultdict(Counter)
    for row in classified:
        sid = str(row["sheet_id"])
        by_sheet[sid][str(row["error_class"])] += 1
        causes_by_sheet[sid][str(row["root_cause"])] += 1

    out: list[dict[str, Any]] = []
    for row in sheet_summary:
        sid = str(row.get("sheet_id") or "")
        class_counts = by_sheet.get(sid, Counter())
        cause_counts = causes_by_sheet.get(sid, Counter())
        no_match = int(row.get("r33_no_match") or 0)
        match = int(row.get("r33_match") or 0)
        comparable = match + no_match
        top_class = class_counts.most_common(1)[0][0] if class_counts else ""
        top_cause = cause_counts.most_common(1)[0][0] if cause_counts else ""
        out.append({
            "sheet_id": sid,
            "template": row.get("template", ""),
            "captured_at": row.get("captured_at", ""),
            "r33_match": match,
            "r33_no_match": no_match,
            "comparable": comparable,
            "r33_error_rate_pct": round(100 * no_match / comparable, 2)
            if comparable
            else 0.0,
            "top_error_class": top_class,
            "top_root_cause": top_cause,
            "classified_no_match": sum(class_counts.values()),
        })
    out.sort(key=lambda item: (-int(item["r33_no_match"]), item["sheet_id"]))
    return out


def _class_totals(classified: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counts = Counter(str(row["error_class"]) for row in classified)
    return counts.most_common()


def _top_repeat_groups(classified: list[dict[str, Any]], limit: int = 12) -> list[tuple[tuple[str, str, str, str], int]]:
    counts = Counter(
        (
            str(row["sheet_id"]),
            str(row["field"]),
            str(row["value"]),
            str(row["root_cause"]),
        )
        for row in classified
    )
    return [(key, count) for key, count in counts.most_common(limit) if count > 1]


def _write_report(
    path: Path,
    *,
    classified: list[dict[str, Any]],
    cause_summary: list[dict[str, Any]],
    sheet_impact: list[dict[str, Any]],
    metrics: dict[str, float],
    rates: Rates,
) -> None:
    class_totals = _class_totals(classified)
    repeat_groups = _top_repeat_groups(classified)
    explainable = sum(
        int(row["occurrences"])
        for row in cause_summary
        if row["error_class"] in {
            "inflated_repetition",
            "ref_missing_or_stale",
            "r33_exposed_no_autofill",
        }
    )
    delta_errors = metrics["delta_errors"]
    explainable_delta_pct = 100 * explainable / delta_errors if delta_errors else 0.0

    lines = [
        "# R31 -> R33 regression diagnostic",
        "",
        "## Executive summary",
        "",
        f"- R31 target error rate: {rates.r31 * 100:.1f}%.",
        f"- R33 target error rate: {rates.r33 * 100:.1f}%.",
        f"- Delta to explain: {rates.delta * 100:.1f} pp.",
        f"- R33 NO_MATCH rows analysed: {int(metrics['total_no_match'])}.",
        (
            "- Estimated shared denominator from 13%: "
            f"{metrics['estimated_denominator']:.0f} checked cells/events."
        ),
        (
            "- Expected R31 errors at same denominator: "
            f"{metrics['expected_r31_errors']:.0f}; implied delta: "
            f"{metrics['delta_errors']:.0f} errors/events."
        ),
        (
            "- Non-real-error candidates "
            "(repetition + ref gaps + R33 exposed no-autofill) cover "
            f"{explainable} occurrences, {explainable_delta_pct:.1f}% of the "
            "implied 5 pp delta as an upper bound."
        ),
        "",
        "## Error classes",
        "",
    ]
    for klass, count in class_totals:
        lines.append(f"- {klass}: {count}")

    lines.extend([
        "",
        "## Biggest causes",
        "",
        "| Class | Cause | Occurrences | Unique causes | Impact inside 13% | Delta upper-bound |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in cause_summary[:15]:
        lines.append(
            "| "
            f"{row['error_class']} | {row['root_cause']} | {row['occurrences']} | "
            f"{row['unique_causes']} | {row['impact_pp_inside_13pct']} pp | "
            f"{row['share_of_5pp_delta_upper_pct']}% |"
        )

    lines.extend([
        "",
        "## Repetition hotspots",
        "",
    ])
    for (sheet_id, field, value, cause), count in repeat_groups:
        lines.append(f"- sheet {sheet_id}, {field}={value!r}, {cause}: {count} occurrences")

    lines.extend([
        "",
        "## Worst sheets",
        "",
        "| Sheet | Template | R33 NO_MATCH | Comparable | Error% | Top class | Top cause |",
        "|---:|---|---:|---:|---:|---|---|",
    ])
    for row in sheet_impact[:15]:
        lines.append(
            "| "
            f"{row['sheet_id']} | {row['template']} | {row['r33_no_match']} | "
            f"{row['comparable']} | {row['r33_error_rate_pct']} | "
            f"{row['top_error_class']} | {row['top_root_cause']} |"
        )

    lines.extend([
        "",
        "## Fix candidates to validate",
        "",
        "- Count repeated same-sheet failures as one root cause plus occurrence count in quality reports.",
        "- Add a safe OF recovery pass before treating text/model-like OF cells as real errors.",
        "- Normalize client aliases before `NO_MATCH` for canonical name differences.",
        "- Report missing StockSAP/plan evidence separately as `REF_MISSING` when row context is strong.",
        "- Keep unrelated `modelo` swaps and large numeric deltas visible for human review.",
        "",
        "## Generated files",
        "",
        "- `classified_no_match.csv`",
        "- `cause_summary.csv`",
        "- `sheet_impact.csv`",
        "- `summary.json`",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(analysis_dir: Path, out_dir: Path, rates: Rates) -> dict[str, Any]:
    no_match_path = analysis_dir / "r33_no_match_cells.csv"
    diffs_path = analysis_dir / "raw_r31_vs_resultado_atual_diffs.csv"
    sheet_summary_path = analysis_dir / "sheet_summary.csv"
    for required in (no_match_path, diffs_path, sheet_summary_path):
        if not required.exists():
            raise FileNotFoundError(f"Missing required analysis file: {required}")

    no_matches = _read_csv(no_match_path)
    diffs = _read_csv(diffs_path)
    sheet_summary = _read_csv(sheet_summary_path)

    classified = classify_no_match_rows(no_matches, diffs)
    cause_summary, metrics = _summary_rows(classified, rates=rates)
    sheet_impact = _sheet_rows(classified, sheet_summary)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        out_dir / "classified_no_match.csv",
        classified,
        [
            "sheet_id",
            "template",
            "path",
            "field",
            "value",
            "ref",
            "reason",
            "resultado_atual",
            "diff_kind",
            "error_class",
            "root_cause",
            "repeat_group_count",
            "repeat_extra_occurrences",
            "recommendation",
        ],
    )
    _write_csv(
        out_dir / "cause_summary.csv",
        cause_summary,
        [
            "error_class",
            "root_cause",
            "occurrences",
            "unique_causes",
            "share_of_r33_no_match_pct",
            "impact_pp_inside_13pct",
            "share_of_5pp_delta_upper_pct",
            "repeat_extra_occurrences",
            "examples",
        ],
    )
    _write_csv(
        out_dir / "sheet_impact.csv",
        sheet_impact,
        [
            "sheet_id",
            "template",
            "captured_at",
            "r33_match",
            "r33_no_match",
            "comparable",
            "r33_error_rate_pct",
            "top_error_class",
            "top_root_cause",
            "classified_no_match",
        ],
    )

    payload = {
        "analysis_dir": str(analysis_dir),
        "rates": {
            "r31": rates.r31,
            "r33": rates.r33,
            "delta": rates.delta,
        },
        "metrics": metrics,
        "classes": dict(Counter(row["error_class"] for row in classified)),
        "top_causes": cause_summary[:20],
        "top_sheets": sheet_impact[:20],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_report(
        out_dir / "report.md",
        classified=classified,
        cause_summary=cause_summary,
        sheet_impact=sheet_impact,
        metrics=metrics,
        rates=rates,
    )
    return payload


def _parse_rate(value: str) -> float:
    parsed = float(value)
    return parsed / 100.0 if parsed > 1 else parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", type=Path, default=_DEFAULT_ANALYSIS_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--r31-error-rate", type=_parse_rate, default=0.08)
    parser.add_argument("--r33-error-rate", type=_parse_rate, default=0.13)
    args = parser.parse_args()

    payload = run(
        args.analysis_dir,
        args.out_dir,
        Rates(r31=args.r31_error_rate, r33=args.r33_error_rate),
    )
    metrics = payload["metrics"]
    print(f"Wrote diagnostic pack: {args.out_dir}")
    print(f"R33 NO_MATCH analysed: {int(metrics['total_no_match'])}")
    print(f"Estimated delta errors: {metrics['delta_errors']:.1f}")
    for klass, count in payload["classes"].items():
        print(f"  {klass}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
