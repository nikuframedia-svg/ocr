#!/usr/bin/env python3
"""Compare cross-check engines against the same OCR/ref package.

This runner is intentionally boring and auditable:

* the baseline is checked out in a temporary git worktree;
* both engines read the same sample directory and KANBAN_DOC_DIR;
* ignored alias files are copied into the baseline worktree;
* each engine uses the same evaluate_cross_outputs.py script;
* the output includes package hashes and a +3pp gate versus the baseline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import csv

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_SAMPLE_DIR = Path("/Users/martimnicolau/Downloads/ultimos_150_ocr")
_DEFAULT_DOC_DIR = _REPO / "kanban_refs" / "04_Documentacao"
_DEFAULT_OUT_DIR = _REPO / "reports" / "cross_engine_compare"
_CROSSABLE_FIELDS = {
    "of", "ov", "cliente", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo",
}


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=merged_env,
        check=True,
        text=True,
        capture_output=capture,
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digest(paths: list[Path]) -> dict[str, Any]:
    files = []
    h = hashlib.sha256()
    for path in sorted(p for p in paths if p.exists() and p.is_file()):
        digest = _sha256(path)
        rel = str(path)
        files.append({"path": rel, "sha256": digest, "size": path.stat().st_size})
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return {"sha256": h.hexdigest(), "files": files}


def _sample_files(sample_dir: Path) -> list[Path]:
    manifest = sample_dir / "manifest.csv"
    files = [manifest]
    if manifest.exists():
        import csv

        with manifest.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                for key in ("ocr_original", "resultado_atual"):
                    rel = str(row.get(key) or "").replace("\\", "/")
                    if rel:
                        files.append(sample_dir / rel)
    return files


def _package_manifest(repo: Path, sample_dir: Path, doc_dir: Path) -> dict[str, Any]:
    ref_files = [
        doc_dir / "plan_colunas_cpis.xlsx",
        doc_dir / "StockSAP.xlsx",
        doc_dir / "ListaColaboradores.xlsx",
        doc_dir / "maquinas.xlsx",
        repo / "lexicons" / "cliente_aliases.json",
        repo / "lexicons" / "operador_aliases.json",
    ]
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_dir": str(sample_dir),
        "doc_dir": str(doc_dir),
        "sample": _tree_digest(_sample_files(sample_dir)),
        "refs": _tree_digest(ref_files),
    }


def _copy_eval_script(src_repo: Path, dst_repo: Path) -> None:
    dst = dst_repo / "scripts" / "diag" / "evaluate_cross_outputs.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_repo / "scripts" / "diag" / "evaluate_cross_outputs.py", dst)


def _copy_ignored_lexicons(src_repo: Path, dst_repo: Path) -> None:
    (dst_repo / "lexicons").mkdir(exist_ok=True)
    for name in ("cliente_aliases.json", "operador_aliases.json"):
        src = src_repo / "lexicons" / name
        if src.exists():
            shutil.copy2(src, dst_repo / "lexicons" / name)


def _git_info(repo: Path) -> dict[str, Any]:
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    status = _run(["git", "status", "--short"], cwd=repo).stdout
    diff_stat = _run(["git", "diff", "--stat"], cwd=repo).stdout
    return {
        "sha": sha,
        "dirty": bool(status.strip()),
        "status_short": status.splitlines(),
        "diff_stat": diff_stat.splitlines(),
    }


def _run_eval(
    repo: Path,
    *,
    sample_dir: Path,
    doc_dir: Path,
    out_dir: Path,
    sections: str,
    scoring_variant: str = "v30",
) -> tuple[dict[str, Any], Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "uv", "run", "python", "scripts/diag/evaluate_cross_outputs.py",
            "--sample-dir", str(sample_dir),
            "--out-dir", str(out_dir),
            "--sections", sections,
        ],
        cwd=repo,
        # R253/F2 — a variante é SEMPRE explícita nos dois subprocessos: o
        # baseline fica pinned a v30 (não herda CROSS_SCORING_VARIANT do
        # ambiente do operador) e o candidato corre a variante pedida por
        # --scoring-variant. Antes disto nenhuma corrida oficial conseguia
        # medir a "next" sem export manual — e um export manual contaminava
        # também o baseline.
        env={"KANBAN_DOC_DIR": str(doc_dir),
             "CROSS_SCORING_VARIANT": scoring_variant},
    )
    report_dir = None
    for line in proc.stdout.splitlines():
        if line.startswith("report_dir="):
            report_dir = Path(line.split("=", 1)[1])
            break
    if report_dir is None:
        candidates = sorted(out_dir.glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise RuntimeError(f"evaluation did not produce summary in {out_dir}")
        report_dir = candidates[-1].parent
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    return summary, report_dir, proc.stdout


def _read_cells(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _candidate_contract(report_dir: Path) -> dict[str, Any]:
    cells_path = report_dir / "cells.csv"
    if not cells_path.exists():
        return {"checked": False, "violations": ["cells.csv missing"]}
    violations = []
    for row in _read_cells(cells_path):
        field = row.get("field") or ""
        if field not in _CROSSABLE_FIELDS:
            continue
        source = row.get("source") or ""
        decision = row.get("decision_source") or ""
        output = row.get("output") or ""
        if source == "ocr_selected" or decision == "ocr_candidate":
            violations.append({
                "path": row.get("path"),
                "field": field,
                "source": source,
                "decision_source": decision,
            })
        if source in {"ocr_raw", "raw_observation", "syntax"} and output.strip():
            violations.append({
                "path": row.get("path"),
                "field": field,
                "source": source,
                "output": output,
            })
        if source == "ref_unavailable" and output.strip():
            violations.append({
                "path": row.get("path"),
                "field": field,
                "source": source,
                "output": output,
            })
    return {
        "checked": True,
        "violations_count": len(violations),
        "violations_sample": violations[:50],
    }


def _cell_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        str(row.get("sheet_id") or ""),
        str(row.get("template") or ""),
        str(row.get("path") or ""),
        str(row.get("field") or ""),
    )


def _compare_cells(
    baseline_report: Path,
    candidate_report: Path,
    out_dir: Path,
) -> dict[str, Any]:
    baseline_rows = {_cell_key(row): row for row in _read_cells(baseline_report / "cells.csv")}
    candidate_rows = {_cell_key(row): row for row in _read_cells(candidate_report / "cells.csv")}
    keys = sorted(set(baseline_rows) | set(candidate_rows))

    summary = {
        "cells_compared": len(keys),
        "both_ok": 0,
        "both_wrong": 0,
        "baseline_ok_candidate_wrong": 0,
        "candidate_ok_baseline_wrong": 0,
        "missing_in_baseline": 0,
        "missing_in_candidate": 0,
    }
    lost: list[dict[str, Any]] = []
    gained: list[dict[str, Any]] = []
    by_field: dict[str, dict[str, Any]] = {}
    by_template: dict[str, dict[str, Any]] = {}

    def bucket(store: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        return store.setdefault(key or "", {
            "key": key or "",
            "both_ok": 0,
            "both_wrong": 0,
            "baseline_ok_candidate_wrong": 0,
            "candidate_ok_baseline_wrong": 0,
        })

    for key in keys:
        base = baseline_rows.get(key)
        cand = candidate_rows.get(key)
        if base is None:
            summary["missing_in_baseline"] += 1
            continue
        if cand is None:
            summary["missing_in_candidate"] += 1
            continue
        base_ok = str(base.get("output_equals_truth") or "") == "1"
        cand_ok = str(cand.get("output_equals_truth") or "") == "1"
        if base_ok and cand_ok:
            outcome = "both_ok"
        elif base_ok and not cand_ok:
            outcome = "baseline_ok_candidate_wrong"
        elif cand_ok and not base_ok:
            outcome = "candidate_ok_baseline_wrong"
        else:
            outcome = "both_wrong"
        summary[outcome] += 1
        for store, dim in ((by_field, key[3]), (by_template, key[1])):
            bucket(store, dim)[outcome] += 1
        if outcome in {"baseline_ok_candidate_wrong", "candidate_ok_baseline_wrong"}:
            row = {
                "outcome": outcome,
                "sheet_id": key[0],
                "template": key[1],
                "path": key[2],
                "field": key[3],
                "raw": cand.get("raw") or base.get("raw") or "",
                "resultado_atual": cand.get("resultado_atual") or base.get("resultado_atual") or "",
                "baseline_output": base.get("output") or "",
                "baseline_status": base.get("status") or "",
                "baseline_source": base.get("source") or "",
                "candidate_output": cand.get("output") or "",
                "candidate_status": cand.get("status") or "",
                "candidate_source": cand.get("source") or "",
                "candidate_decision_source": cand.get("decision_source") or "",
                "candidate_ref": cand.get("ref") or "",
                "candidate_ref_source": cand.get("ref_source") or "",
                "candidate_match_kind": cand.get("match_kind") or "",
                "candidate_decision_reason": cand.get("decision_reason") or "",
            }
            if outcome == "baseline_ok_candidate_wrong":
                lost.append(row)
            else:
                gained.append(row)

    fields = [
        "outcome", "sheet_id", "template", "path", "field", "raw",
        "resultado_atual", "baseline_output", "baseline_status",
        "baseline_source", "candidate_output", "candidate_status",
        "candidate_source", "candidate_decision_source", "candidate_ref",
        "candidate_ref_source", "candidate_match_kind", "candidate_decision_reason",
    ]
    _write_csv(out_dir / "lost_vs_baseline.csv", lost, fields)
    _write_csv(out_dir / "gained_vs_baseline.csv", gained, fields)

    def grouped_rows(store: dict[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
        rows = []
        for value, counts in store.items():
            rows.append({
                name: value,
                "both_ok": counts["both_ok"],
                "both_wrong": counts["both_wrong"],
                "baseline_ok_candidate_wrong": counts["baseline_ok_candidate_wrong"],
                "candidate_ok_baseline_wrong": counts["candidate_ok_baseline_wrong"],
                "net_gain": counts["candidate_ok_baseline_wrong"] - counts["baseline_ok_candidate_wrong"],
            })
        rows.sort(key=lambda row: (int(row["net_gain"]), -int(row["baseline_ok_candidate_wrong"]), str(row[name])))
        return rows

    _write_csv(out_dir / "diff_by_field.csv", grouped_rows(by_field, "field"), [
        "field", "both_ok", "both_wrong", "baseline_ok_candidate_wrong",
        "candidate_ok_baseline_wrong", "net_gain",
    ])
    _write_csv(out_dir / "diff_by_template.csv", grouped_rows(by_template, "template"), [
        "template", "both_ok", "both_wrong", "baseline_ok_candidate_wrong",
        "candidate_ok_baseline_wrong", "net_gain",
    ])
    return summary


def _metric(summary: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(summary.get(key, default))
    except (TypeError, ValueError):
        return default


def _metric_delta(
    candidate_summary: dict[str, Any],
    baseline_summary: dict[str, Any],
    key: str,
) -> float:
    return round(_metric(candidate_summary, key) - _metric(baseline_summary, key), 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="601fe7d")
    parser.add_argument("--candidate-repo", type=Path, default=_REPO)
    parser.add_argument("--sample-dir", type=Path, default=_DEFAULT_SAMPLE_DIR)
    parser.add_argument("--doc-dir", type=Path, default=_DEFAULT_DOC_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--sections", default="rows")
    parser.add_argument("--gate-pp", type=float, default=3.0)
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument("--scoring-variant", default="v30",
                        choices=("v30", "next"),
                        help="variante do CANDIDATO (o baseline fica "
                             "sempre pinned a v30)")
    args = parser.parse_args()

    candidate_repo = args.candidate_repo.resolve()
    sample_dir = args.sample_dir.resolve()
    doc_dir = args.doc_dir.resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = (args.out_dir / run_id).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    manifest = _package_manifest(candidate_repo, sample_dir, doc_dir)
    (out_root / "package_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    temp_root = Path(tempfile.mkdtemp(prefix="ocr-cross-compare."))
    baseline_repo = temp_root / "baseline"
    try:
        _run(
            ["git", "worktree", "add", "--detach", str(baseline_repo), args.baseline_ref],
            cwd=candidate_repo,
        )
        _copy_eval_script(candidate_repo, baseline_repo)
        _copy_ignored_lexicons(candidate_repo, baseline_repo)

        baseline_summary, baseline_report, baseline_stdout = _run_eval(
            baseline_repo,
            sample_dir=sample_dir,
            doc_dir=doc_dir,
            out_dir=out_root / "baseline",
            sections=args.sections,
        )
        candidate_summary, candidate_report, candidate_stdout = _run_eval(
            candidate_repo,
            sample_dir=sample_dir,
            doc_dir=doc_dir,
            out_dir=out_root / "candidate",
            sections=args.sections,
            scoring_variant=args.scoring_variant,
        )

        baseline_acc = float(baseline_summary["output_accuracy_vs_resultado_atual_pct"])
        candidate_acc = float(candidate_summary["output_accuracy_vs_resultado_atual_pct"])
        target_acc = round(baseline_acc + args.gate_pp, 2)
        contract = _candidate_contract(candidate_report)
        cell_diff = _compare_cells(baseline_report, candidate_report, out_root)
        # R236 — impor as 5 condições do CROSS_EVALUATION_PROTOCOL.md (antes o
        # código só verificava 2: accuracy e contrato). `regressed_good_raw`
        # usa <= (igual = não piorou); runtime tolera +10% e só é verificado
        # quando ambos os summaries trazem `elapsed_s` (R236+).
        gate_regressed = (
            int(candidate_summary.get("regressed_good_raw", 0))
            <= int(baseline_summary.get("regressed_good_raw", 0))
        )
        gate_corrected = (
            int(candidate_summary.get("corrected_to_truth", 0))
            >= int(baseline_summary.get("corrected_to_truth", 0))
        )
        base_elapsed = baseline_summary.get("elapsed_s")
        cand_elapsed = candidate_summary.get("elapsed_s")
        gate_runtime = (
            True if base_elapsed is None or cand_elapsed is None
            else float(cand_elapsed) <= float(base_elapsed) * 1.10
        )
        gate_conditions = {
            "accuracy_vs_target": candidate_acc >= target_acc,
            "contract_zero_violations": bool(
                contract.get("checked")
                and contract.get("violations_count", 1) == 0
            ),
            "regressed_good_raw_not_worse": gate_regressed,
            "corrected_to_truth_not_worse": gate_corrected,
            "runtime_within_10pct": gate_runtime,
        }
        passed = all(gate_conditions.values())

        comparison = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "baseline_ref": args.baseline_ref,
            "candidate_scoring_variant": args.scoring_variant,
            "gate_pp": args.gate_pp,
            "target_accuracy_pct": target_acc,
            "passed": passed,
            "gate_conditions": gate_conditions,
            "baseline": {
                "git": _git_info(baseline_repo),
                "summary": baseline_summary,
                "report_dir": str(baseline_report),
            },
            "candidate": {
                "git": _git_info(candidate_repo),
                "summary": candidate_summary,
                "report_dir": str(candidate_report),
                "contract": contract,
            },
            "delta": {
                "accuracy_pp": round(candidate_acc - baseline_acc, 2),
                "output_equals_truth": (
                    int(candidate_summary["output_equals_truth"])
                    - int(baseline_summary["output_equals_truth"])
                ),
                "corrected_to_truth": (
                    int(candidate_summary["corrected_to_truth"])
                    - int(baseline_summary["corrected_to_truth"])
                ),
                "regressed_good_raw": (
                    int(candidate_summary["regressed_good_raw"])
                    - int(baseline_summary["regressed_good_raw"])
                ),
                "crossable_output_accuracy_pp": _metric_delta(
                    candidate_summary,
                    baseline_summary,
                    "crossable_output_accuracy_pct",
                ),
                "crossable_reachable_accuracy_pp": _metric_delta(
                    candidate_summary,
                    baseline_summary,
                    "crossable_reachable_accuracy_pct",
                ),
                "validated_output_accuracy_pp": _metric_delta(
                    candidate_summary,
                    baseline_summary,
                    "validated_output_accuracy_pct",
                ),
                "cross_contract_violations": (
                    int(candidate_summary.get("cross_contract_violations", 0))
                    - int(baseline_summary.get("cross_contract_violations", 0))
                ),
            },
            "cell_diff": cell_diff,
        }
        (out_root / "comparison.json").write_text(
            json.dumps(comparison, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (out_root / "baseline_stdout.txt").write_text(baseline_stdout, encoding="utf-8")
        (out_root / "candidate_stdout.txt").write_text(candidate_stdout, encoding="utf-8")
        print(json.dumps(comparison, indent=2, ensure_ascii=False))
        print(f"comparison_dir={out_root}")
        return 0 if passed else 2
    finally:
        if args.keep_worktree:
            print(f"kept_worktree={baseline_repo}", file=sys.stderr)
        else:
            try:
                _run(
                    ["git", "worktree", "remove", "--force", str(baseline_repo)],
                    cwd=candidate_repo,
                    capture=True,
                )
            except Exception:
                shutil.rmtree(baseline_repo, ignore_errors=True)
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
