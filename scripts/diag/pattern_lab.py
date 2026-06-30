#!/usr/bin/env python3
"""Pattern lab for OCR-vs-cross diagnostics.

Builds a row/cell-level dataset from the profiling trace and looks for
failure modes: which fields acted as anchors, which fields were overwritten,
and where a small signal propagated into many changed cells.

Inputs:
  - profile.jsonl written by the app; or
  - text copied from ``profile_report.py --detail``.

Examples:
    .venv\\Scripts\\python.exe scripts\\diag\\pattern_lab.py --last 400
    .venv\\Scripts\\python.exe scripts\\diag\\pattern_lab.py --profile-text pasted.txt
    .venv\\Scripts\\python.exe scripts\\diag\\pattern_lab.py --profile-text pasted.txt \
        --out-dir reports\\pattern_lab
"""
from __future__ import annotations

import argparse
import ast
import csv
import difflib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOG = _REPO / "data" / "_logs" / "profile.jsonl"
_IDENTITY_FIELDS = {"of", "ov", "cliente", "modelo"}
_DIM_FIELDS = {"comp_mm", "larg_mm", "esp", "lbase", "ltopo", "dbase", "dtopo", "lote"}
_RISK_KINDS = {"xx", "dubio"}


def _read_records(log: Path) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    for p in (log.with_suffix(".jsonl.1"), log):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _parse_profile_text(path: Path) -> list[dict[str, Any]]:
    header_re = re.compile(r"^### Folha (\d+) — ([^—]+) — ([^—]+) — (\d+) linhas")
    timing_re = re.compile(r"^\s*tempos:\s*(.*)$")
    row_re = re.compile(
        r"^\s*• linha (\d+): winner=([^ ]+) "
        r"\(mode=([^,)]*), combined=([^,)]*), pool=(\d+), "
        r"cand_por_campo=(\{.*?\})(?:, FULLSCAN)?\)"
    )
    cand_re = re.compile(
        r"^\s*-\s*([^:]+): agree=(\d+) exact=(\d+) "
        r"combined=([\d.]+) \[(.*)\]"
    )
    dec_re = re.compile(r"^\s*↳\s*([^:]+): '(.*?)' → '(.*?)' \(([^/]+)/([^)]*)\)")

    recs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    currow: dict[str, Any] | None = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = header_re.match(line)
        if m:
            if cur:
                recs.append(cur)
            cur = {
                "sheet_id": int(m.group(1)),
                "trigger": m.group(2).strip(),
                "template": m.group(3).strip(),
                "n_rows": int(m.group(4)),
                "timing": {},
                "scoring": [],
            }
            currow = None
            continue
        if cur is None:
            continue

        m = timing_re.match(line)
        if m:
            timing: dict[str, int] = {}
            for part in m.group(1).split(","):
                if "=" not in part:
                    continue
                key, value = part.strip().split("=", 1)
                try:
                    timing[key.strip()] = int(float(value.strip()))
                except ValueError:
                    pass
            cur["timing"] = timing
            continue

        m = row_re.match(line)
        if m:
            try:
                cpc = ast.literal_eval(m.group(6))
            except (SyntaxError, ValueError):
                cpc = {}
            currow = {
                "row_index": int(m.group(1)),
                "winner_of": None if m.group(2) == "None" else m.group(2),
                "winner_mode": None if m.group(3) == "None" else m.group(3),
                "winner_combined": None if m.group(4) == "None" else float(m.group(4)),
                "pool_size": int(m.group(5)),
                "candidates_by_field": cpc,
                "fallback_full_scan": "FULLSCAN" in line,
                "candidates": [],
                "decisions": [],
            }
            cur["scoring"].append(currow)
            continue

        m = cand_re.match(line)
        if m and currow is not None:
            sims = []
            for part in [p.strip() for p in m.group(5).split(",") if p.strip()]:
                if "=" not in part:
                    continue
                field, sim = part.split("=", 1)
                try:
                    sims.append({"field": field.strip(), "sim": float(sim)})
                except ValueError:
                    pass
            currow["candidates"].append({
                "of": m.group(1).strip(),
                "agree": int(m.group(2)),
                "exact": int(m.group(3)),
                "combined": float(m.group(4)),
                "field_sims": sims,
            })
            continue

        m = dec_re.match(line)
        if m and currow is not None:
            currow["decisions"].append({
                "field": m.group(1).strip(),
                "ocr_value": m.group(2),
                "final_value": m.group(3),
                "engine_status": m.group(4),
                "source": m.group(5),
            })

    if cur:
        recs.append(cur)
    return recs


def _norm(value: object) -> str:
    s = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return s.replace("O", "0").replace("I", "1").replace("L", "1")


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _relatedness(ocr: object, cross: object) -> float:
    left = _norm(ocr)
    if not left:
        return 1.0
    full = _norm(cross)
    if len(left) >= 3 and (left in full or full in left):
        return 1.0
    candidates = [full, _norm(str(cross or "").split(" - ")[0])]
    candidates += [_norm(t) for t in str(cross or "").replace(" - ", " ").split()]
    return max((_ratio(left, cand) for cand in candidates if cand), default=0.0)


def _classify(decision: dict[str, Any]) -> tuple[str, float]:
    ocr = str(decision.get("ocr_value") or "").strip()
    final = str(decision.get("final_value") or "").strip()
    source = str(decision.get("source") or "").lower()
    if not final and not ocr:
        return "vazio", 1.0
    if _norm(ocr) == _norm(final):
        return "igual", 1.0
    if not ocr and final:
        return "fill", 1.0
    if source == "ocr_raw":
        return "rever", 1.0
    rel = _relatedness(ocr, final)
    if rel >= 0.70:
        return "ok", rel
    if rel >= 0.45:
        return "dubio", rel
    return "xx", rel


def _top_sims(row: dict[str, Any]) -> dict[str, float]:
    candidates = row.get("candidates") or []
    if not candidates:
        return {}
    sims: dict[str, float] = {}
    for fs in candidates[0].get("field_sims") or []:
        field = fs.get("field")
        if field:
            sims[str(field)] = float(fs.get("sim") or 0.0)
    return sims


def _has_letters(value: object) -> bool:
    text = str(value or "")
    return any(ch.isalpha() for ch in text)


def _empty_cell_value(value: object) -> bool:
    return str(value or "").strip() in {"", "(vazio)"}


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _looks_like_of(value: object) -> bool:
    return len(_digits(value)) == 6


def _looks_like_reference(value: object) -> bool:
    compact = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if len(compact) < 5:
        return False
    prefixes = (
        "CA", "CB", "CBC", "CBO", "CD", "CF", "CFC", "CFH", "CG", "CGC",
        "CL", "CLC", "CR", "CS", "CSC", "CO", "GC",
    )
    if compact.startswith(prefixes):
        return True
    return any(ch.isalpha() for ch in compact) and any(ch.isdigit() for ch in compact)


def _bobine_acabamento_smell(
    template: str, row_cells: list[dict[str, Any]]
) -> tuple[bool, list[str]]:
    if template != "bobine_formato":
        return False, []
    by_field = {str(c.get("field") or ""): c for c in row_cells}

    def ocr(field: str) -> str:
        return str((by_field.get(field) or {}).get("ocr") or "")

    def final(field: str) -> str:
        return str((by_field.get(field) or {}).get("final") or "")

    reasons: list[str] = []
    if _empty_cell_value(ocr("ov")):
        reasons.append("ov_empty")
    if _empty_cell_value(ocr("cliente")) or _looks_like_reference(ocr("cliente")):
        reasons.append("cliente_empty_or_reference")
    if _looks_like_of(ocr("of")):
        reasons.append("of_6_digits")
    if _empty_cell_value(ocr("modelo")) or _looks_like_reference(ocr("modelo")):
        reasons.append("modelo_empty_or_reference")

    filled_dims = sum(
        1 for field in ("comp_mm", "esp", "lbase", "ltopo")
        if _empty_cell_value(ocr(field)) and not _empty_cell_value(final(field))
    )
    if filled_dims >= 2:
        reasons.append(f"bobine_dims_filled_{filled_dims}")

    return len(reasons) >= 4, reasons


def _entropy(values: list[float]) -> float:
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return 0.0
    probs = [v / total for v in values if v > 0]
    if len(probs) <= 1:
        return 0.0
    return -sum(p * math.log(p, 2) for p in probs) / math.log(len(probs), 2)


def _hhi(values: list[float]) -> float:
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return 0.0
    return sum((v / total) ** 2 for v in values if v > 0)


def _anchor_sets(
    sims: dict[str, float], threshold: float
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    anchors = tuple(sorted(k for k, v in sims.items() if v >= threshold))
    identity = tuple(k for k in anchors if k in _IDENTITY_FIELDS)
    dims = tuple(k for k in anchors if k in _DIM_FIELDS)
    return anchors, identity, dims


def _anchor_class(identity: tuple[str, ...], dims: tuple[str, ...]) -> str:
    identity_set = set(identity)
    dim_set = set(dims)
    if len(identity_set) >= 2 and "of" in identity_set:
        return "multi_identity_with_of"
    if identity_set and dim_set:
        if len(identity_set) == 1:
            return f"{next(iter(identity_set))}_plus_dims"
        return "mixed_identity_dims"
    if dim_set:
        return "dims_only"
    if len(identity_set) == 1:
        return f"{next(iter(identity_set))}_only"
    if len(identity_set) > 1:
        return "identity_no_of_misc"
    return "no_anchor"


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * q
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def build_dataset(
    recs: list[dict[str, Any]], threshold: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cells: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for rec in recs:
        sheet = rec.get("sheet_id")
        template = rec.get("template") or "?"
        template_detection = rec.get("template_detection") or {}
        for row in rec.get("scoring") or []:
            row_index = int(row.get("row_index") or 0)
            row_strategy = row.get("proposal_strategy") or {}
            sims = _top_sims(row)
            anchors, id_anchors, dim_anchors = _anchor_sets(sims, threshold)
            vals = list(sims.values())
            top_sim = max(vals) if vals else 0.0
            second_sim = sorted(vals, reverse=True)[1] if len(vals) > 1 else 0.0
            anchor_entropy = _entropy(vals)
            anchor_hhi = _hhi(vals)
            row_cells: list[dict[str, Any]] = []
            for dec in row.get("decisions") or []:
                kind, rel = _classify(dec)
                field = str(dec.get("field") or "")
                cell = {
                    "sheet": sheet,
                    "template": template,
                    "row": row_index + 1,
                    "field": field,
                    "ocr": str(dec.get("ocr_value") or ""),
                    "final": str(dec.get("final_value") or ""),
                    "kind": kind,
                    "relatedness": round(rel, 4),
                    "engine_status": dec.get("engine_status"),
                    "source": dec.get("source"),
                    "alteration_rule": dec.get("alteration_rule") or "",
                    "proposal_source": dec.get("proposal_source") or "",
                    "hypothesis_level": (
                        dec.get("hypothesis_level")
                        or row_strategy.get("hypothesis_level")
                        or ""
                    ),
                    "reconstruction_source": row_strategy.get("reconstruction_source") or "",
                    "winner_of": row.get("winner_of"),
                    "winner_mode": row.get("winner_mode") or "-",
                    "winner_combined": row.get("winner_combined"),
                    "pool_size": row.get("pool_size") or 0,
                    "anchor_class": _anchor_class(id_anchors, dim_anchors),
                    "anchors": ",".join(anchors),
                    "id_anchors": ",".join(id_anchors),
                    "dim_anchors": ",".join(dim_anchors),
                    "sim_this_field": sims.get(field, 0.0),
                }
                cells.append(cell)
                row_cells.append(cell)

            counts = Counter(c["kind"] for c in row_cells)
            changed = sum(c["kind"] not in {"igual", "vazio"} for c in row_cells)
            risk = counts["xx"] + counts["dubio"]
            risk_fields = [c["field"] for c in row_cells if c["kind"] in _RISK_KINDS]
            xx_fields = [c["field"] for c in row_cells if c["kind"] == "xx"]
            of_cell = next((c for c in row_cells if c["field"] == "of"), None)
            modelo_cell = next((c for c in row_cells if c["field"] == "modelo"), None)
            acabamento_smell, smell_reasons = _bobine_acabamento_smell(
                str(template), row_cells,
            )
            n_anchor = len(anchors)
            rows.append({
                "sheet": sheet,
                "template": template,
                "template_detection_source": template_detection.get("source") or "",
                "template_raw_setor": template_detection.get("raw_setor") or "",
                "template_structural_score": (
                    template_detection.get("structural_score") or ""
                ),
                "row": row_index + 1,
                "winner_of": row.get("winner_of") or "",
                "winner_mode": row.get("winner_mode") or "-",
                "winner_combined": row.get("winner_combined") or "",
                "hypothesis_level": row_strategy.get("hypothesis_level") or "",
                "proposal_anchor_class": row_strategy.get("anchor_class") or "",
                "reconstruction_source": row_strategy.get("reconstruction_source") or "",
                "reconstruction_hypothesis": row_strategy.get("hypothesis_name") or "",
                "reconstruction_margin": row_strategy.get("score_margin") or "",
                "reconstruction_tokens": row_strategy.get("tokens_explained") or "",
                "token_assignments": ",".join(row_strategy.get("token_assignments") or []),
                "structural_flags": ",".join(row_strategy.get("structural_flags") or []),
                "strategy_of_class": row_strategy.get("of_class") or "",
                "pool_size": row.get("pool_size") or 0,
                "fullscan": bool(row.get("fallback_full_scan")),
                "anchors": ",".join(anchors),
                "id_anchors": ",".join(id_anchors),
                "dim_anchors": ",".join(dim_anchors),
                "anchor_class": _anchor_class(id_anchors, dim_anchors),
                "anchor_entropy": round(anchor_entropy, 4),
                "anchor_hhi": round(anchor_hhi, 4),
                "top_sim": round(top_sim, 4),
                "sim_gap": round(top_sim - second_sim, 4),
                "changed": changed,
                "fills": counts["fill"],
                "ok": counts["ok"],
                "dubio": counts["dubio"],
                "xx": counts["xx"],
                "risk": risk,
                "risk_fields": ",".join(risk_fields),
                "xx_fields": ",".join(xx_fields),
                "cells": len(row_cells),
                "amplification": round(changed / max(n_anchor, 1), 3),
                "risk_amplification": round(risk / max(n_anchor, 1), 3),
                "of_ocr": (of_cell or {}).get("ocr", ""),
                "of_kind": (of_cell or {}).get("kind", ""),
                "of_has_letters": _has_letters((of_cell or {}).get("ocr", "")),
                "modelo_ocr": (modelo_cell or {}).get("ocr", ""),
                "modelo_kind": (modelo_cell or {}).get("kind", ""),
                "bobine_acabamento_smell": acabamento_smell,
                "bobine_acabamento_reasons": ",".join(smell_reasons),
                "alteration_rules": ",".join(
                    sorted(
                        set(
                            c.get("alteration_rule") or ""
                            for c in row_cells
                            if c.get("alteration_rule")
                        )
                    )
                ),
                "proposal_sources": ",".join(
                    sorted(
                        set(
                            c.get("proposal_source") or ""
                            for c in row_cells
                            if c.get("proposal_source")
                        )
                    )
                ),
                "top_sims": json.dumps(sims, ensure_ascii=False, sort_keys=True),
            })
    return rows, cells


def _counter_table(counter: Counter, headers: tuple[str, str], limit: int = 20) -> list[str]:
    out = [f"| {headers[0]} | {headers[1]} |", "|---|---:|"]
    for key, value in counter.most_common(limit):
        out.append(f"| {key} | {value} |")
    return out


def _rate(num: float, den: float) -> float:
    return 100.0 * num / den if den else 0.0


def build_report(rows: list[dict[str, Any]], cells: list[dict[str, Any]], threshold: float) -> str:
    out: list[str] = []
    w = out.append
    row_count = len(rows)
    cell_count = len(cells)
    risk_rows = sum(1 for r in rows if r["risk"] > 0)
    xx_rows = sum(1 for r in rows if r["xx"] > 0)
    clean_rows = row_count - risk_rows

    w("# Cross Pattern Lab")
    w("")
    w(f"Anchor threshold: `{threshold}`. Rows: `{row_count}`. Cells: `{cell_count}`.")
    if rows:
        w(f"Sheets: `{min(r['sheet'] for r in rows)}` to `{max(r['sheet'] for r in rows)}`.")
    w("")
    w("## Executive Pattern")
    w("")
    w(
        "The dominant failure mode is not random OCR noise. It is error propagation: "
        "a weak or wrong anchor selects a plan row, then the canonical plan row rewrites "
        "several unrelated fields."
    )
    w("")
    w("## Row Outcome")
    w("")
    w("| Metric | Value |")
    w("|---|---:|")
    w(f"| Clean rows | {clean_rows} ({_rate(clean_rows, row_count):.1f}%) |")
    w(f"| Rows with `??` or `XX` | {risk_rows} ({_rate(risk_rows, row_count):.1f}%) |")
    w(f"| Rows with `XX` | {xx_rows} ({_rate(xx_rows, row_count):.1f}%) |")
    w("")

    w("## Template Confusion")
    w("")
    w("| Segment | Rows | Sheets |")
    w("|---|---:|---:|")
    smell_rows = [r for r in rows if r.get("bobine_acabamento_smell")]
    row_structure_rows = [
        r for r in rows if r.get("template_detection_source") == "row_structure"
    ]
    bobine_real_rows = [
        r for r in rows
        if r["template"] == "bobine_formato" and not r.get("bobine_acabamento_smell")
    ]
    for label, sub in [
        ("`bobine_formato` rows with Acabamento smell", smell_rows),
        ("Recovered by `row_structure`", row_structure_rows),
        ("`bobine_formato` rows without Acabamento smell", bobine_real_rows),
    ]:
        w(f"| {label} | {len(sub)} | {len({r['sheet'] for r in sub})} |")
    w("")
    source_counter = Counter(r.get("template_detection_source") or "-" for r in rows)
    if source_counter:
        w("| Detection source | Rows |")
        w("|---|---:|")
        for source, count in source_counter.most_common():
            w(f"| {source} | {count} |")
        w("")

    w("## Cells By Kind")
    w("")
    for line in _counter_table(Counter(c["kind"] for c in cells), ("Kind", "Cells"), 10):
        w(line)
    w("")

    w("## Risk By Field")
    w("")
    w("| Field | Cells | XX | ?? | Risk % |")
    w("|---|---:|---:|---:|---:|")
    for field in sorted({c["field"] for c in cells}):
        sub = [c for c in cells if c["field"] == field and c["kind"] != "vazio"]
        if not sub:
            continue
        xx = sum(c["kind"] == "xx" for c in sub)
        dubio = sum(c["kind"] == "dubio" for c in sub)
        w(f"| {field} | {len(sub)} | {xx} | {dubio} | {_rate(xx + dubio, len(sub)):.1f}% |")
    w("")

    w("## Risk By Anchor Class")
    w("")
    w("| Anchor class | Rows | XX rows | Risk rows | XX row % | Risk row % | Mean amp |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_class[row["anchor_class"]].append(row)
    for cls, sub in sorted(
        by_class.items(),
        key=lambda kv: (sum(r["xx"] > 0 for r in kv[1]), sum(r["risk"] > 0 for r in kv[1])),
        reverse=True,
    ):
        xx = sum(r["xx"] > 0 for r in sub)
        risk = sum(r["risk"] > 0 for r in sub)
        amp = mean(float(r["amplification"]) for r in sub)
        w(
            f"| {cls} | {len(sub)} | {xx} | {risk} | "
            f"{_rate(xx, len(sub)):.1f}% | {_rate(risk, len(sub)):.1f}% | "
            f"{amp:.2f} |"
        )
    w("")

    w("## Proposal Strategy")
    w("")
    w("| Alteration rule | Cells | XX | ?? | Risk % |")
    w("|---|---:|---:|---:|---:|")
    for rule in sorted({c.get("alteration_rule") or "-" for c in cells}):
        sub = [
            c for c in cells
            if (c.get("alteration_rule") or "-") == rule and c["kind"] != "vazio"
        ]
        if not sub:
            continue
        xx = sum(c["kind"] == "xx" for c in sub)
        dubio = sum(c["kind"] == "dubio" for c in sub)
        w(f"| {rule} | {len(sub)} | {xx} | {dubio} | {_rate(xx + dubio, len(sub)):.1f}% |")
    w("")
    w("| Hypothesis level | Rows | XX rows | Risk rows |")
    w("|---|---:|---:|---:|")
    by_level: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_level[row.get("hypothesis_level") or "-"].append(row)
    for level, sub in sorted(by_level.items(), key=lambda kv: len(kv[1]), reverse=True):
        w(
            f"| {level} | {len(sub)} | {sum(r['xx'] > 0 for r in sub)} | "
            f"{sum(r['risk'] > 0 for r in sub)} |"
        )
    w("")

    w("## Cross-Field Reconstruction")
    w("")
    w("| Reconstruction source | Rows | XX rows | Risk rows | Mean margin |")
    w("|---|---:|---:|---:|---:|")
    by_recon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_recon[row.get("reconstruction_source") or "-"].append(row)
    for source, sub in sorted(by_recon.items(), key=lambda kv: len(kv[1]), reverse=True):
        margins = [
            float(r.get("reconstruction_margin") or 0.0)
            for r in sub
            if r.get("reconstruction_margin") not in ("", None)
        ]
        w(
            f"| {source} | {len(sub)} | {sum(r['xx'] > 0 for r in sub)} | "
            f"{sum(r['risk'] > 0 for r in sub)} | "
            f"{(mean(margins) if margins else 0.0):.2f} |"
        )
    w("")
    shifted = [
        r for r in rows
        if r.get("reconstruction_source") == "cross_field_fuzzy"
    ]
    if shifted:
        w("| Token assignments | Rows |")
        w("|---|---:|")
        for assignment, count in Counter(
            r.get("token_assignments") or "-" for r in shifted
        ).most_common(15):
            w(f"| {assignment} | {count} |")
        w("")

    w("## OF-Letters Pattern")
    w("")
    of_letters = [r for r in rows if r["of_has_letters"]]
    of_letters_changed = [r for r in of_letters if r["of_kind"] not in {"", "igual", "vazio"}]
    w("| Segment | Rows | XX rows | Risk rows |")
    w("|---|---:|---:|---:|")
    for label, sub in [
        ("OCR `of` contains letters", of_letters),
        ("OCR `of` contains letters and cross changed it", of_letters_changed),
    ]:
        w(
            f"| {label} | {len(sub)} | {sum(r['xx'] > 0 for r in sub)} | "
            f"{sum(r['risk'] > 0 for r in sub)} |"
        )
    w("")

    w("## Chaos/Instability Proxies")
    w("")
    w(
        "These are not formal chaotic-system proofs. They are operational proxies: "
        "`amplification = changed_fields / anchor_count`, "
        "`risk_amplification = risk_fields / anchor_count`, "
        "`anchor_hhi` measures concentration in one anchor, and `anchor_entropy` "
        "measures dispersed evidence."
    )
    w("")
    for metric in (
        "amplification", "risk_amplification", "anchor_hhi", "anchor_entropy",
        "pool_size",
    ):
        vals = [float(r[metric]) for r in rows if r.get(metric) not in ("", None)]
        if vals:
            w(
                f"- `{metric}`: mean={mean(vals):.2f}, median={median(vals):.2f}, "
                f"p90={_pct(vals, .90):.2f}, p95={_pct(vals, .95):.2f}, max={max(vals):.2f}"
            )
    w("")

    w("## Highest Risk Rows")
    w("")
    w("| Sheet | Template | Row | Mode | Winner | Class | Anchors | Changed | XX | ?? | Fields |")
    w("|---:|---|---:|---|---|---|---|---:|---:|---:|---|")
    for r in sorted(
        rows,
        key=lambda x: (x["xx"], x["risk"], x["risk_amplification"], x["changed"]),
        reverse=True,
    )[:30]:
        if not r["risk"]:
            continue
        w(
            f"| {r['sheet']} | {r['template']} | {r['row']} | {r['winner_mode']} | "
            f"{r['winner_of']} | {r['anchor_class']} | {r['anchors']} | {r['changed']} | "
            f"{r['xx']} | {r['dubio']} | {r['risk_fields']} |"
        )
    w("")

    w("## Field Propagation")
    w("")
    w("| XX field | Most common anchor classes |")
    w("|---|---|")
    by_field_class: dict[str, Counter] = defaultdict(Counter)
    row_map = {(r["sheet"], r["row"]): r for r in rows}
    for c in cells:
        if c["kind"] == "xx":
            r = row_map.get((c["sheet"], c["row"]))
            by_field_class[c["field"]][r["anchor_class"] if r else "?"] += 1
    for field, counter in sorted(
        by_field_class.items(),
        key=lambda kv: sum(kv[1].values()),
        reverse=True,
    ):
        top = ", ".join(f"{k}={v}" for k, v in counter.most_common(5))
        w(f"| {field} | {top} |")
    w("")
    return "\n".join(out)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Deep pattern analysis for OCR-vs-cross traces.")
    ap.add_argument("--log", type=Path, default=_DEFAULT_LOG)
    ap.add_argument(
        "--profile-text", type=Path, default=None,
        help="Text copied from profile_report.py --detail",
    )
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument(
        "--threshold", type=float, default=0.55,
        help="Similarity threshold to call a field an anchor",
    )
    ap.add_argument("--out-dir", type=Path, default=_REPO / "reports" / "pattern_lab")
    args = ap.parse_args()

    if args.profile_text:
        recs = _parse_profile_text(args.profile_text)
    else:
        recs = _read_records(args.log)
    if args.last:
        recs = recs[-args.last:]
    if not recs:
        print("No profiling records found.")
        return 1

    rows, cells = build_dataset(recs, args.threshold)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.out_dir / "rows.csv", rows)
    _write_csv(args.out_dir / "cells.csv", cells)
    risky = [r for r in rows if r["risk"] > 0]
    _write_csv(args.out_dir / "risk_rows.csv", risky)
    report = build_report(rows, cells, args.threshold)
    (args.out_dir / "summary.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[written: {args.out_dir / 'summary.md'}]")
    print(f"[written: {args.out_dir / 'rows.csv'}]")
    print(f"[written: {args.out_dir / 'cells.csv'}]")
    print(f"[written: {args.out_dir / 'risk_rows.csv'}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
