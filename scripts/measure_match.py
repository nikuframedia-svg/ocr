"""Measure cross-check MATCH rate over all sheet JSONs.

Round 43 — used to track progression as we apply 10 solutions.

Usage:
    .venv/Scripts/python.exe scripts/measure_match.py
    .venv/Scripts/python.exe scripts/measure_match.py --baseline=R42 --label="Sol 1"
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


CC_DIR = Path(r"C:\kanban\nifruka\03_Cross_Check")


def measure(label: str = "current") -> dict:
    files = sorted([f for f in CC_DIR.rglob("*sheet*.json") if not f.name.startswith("_")])
    total = 0; match = 0; no_match = 0; na = 0
    by_field: Counter = Counter()
    no_match_by_field: Counter = Counter()
    no_match_examples: dict[str, list[dict]] = defaultdict(list)
    cells_by_id: dict[str, str] = {}  # cell_id → status (for diff vs baseline)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sheet_name = f.stem
        for r in d.get("rows", []):
            ri = r.get("row_index", -1)
            for fld_name, fld in r.get("fields", {}).items():
                s = fld.get("status")
                cell_id = f"{sheet_name}::row{ri}::{fld_name}"
                cells_by_id[cell_id] = s
                if s in ("MATCH", "NO_MATCH"):
                    by_field[fld_name] += 1
                if s == "MATCH":
                    match += 1; total += 1
                elif s == "NO_MATCH":
                    no_match += 1; total += 1
                    no_match_by_field[fld_name] += 1
                    if len(no_match_examples[fld_name]) < 5:
                        no_match_examples[fld_name].append({
                            "sheet": sheet_name, "row": ri,
                            "value": fld.get("value"), "ref": fld.get("ref"),
                        })
                else:
                    na += 1
    return {
        "label": label,
        "n_files": len(files),
        "total": total,
        "match": match,
        "no_match": no_match,
        "na": na,
        "match_rate": match / total if total else 0.0,
        "by_field_total": dict(by_field),
        "by_field_no_match": dict(no_match_by_field),
        "no_match_examples": {k: v for k, v in no_match_examples.items()},
        "cells_by_id": cells_by_id,
    }


def diff_vs_baseline(current: dict, baseline_path: Path | None) -> dict:
    if not baseline_path or not baseline_path.exists():
        return {"flipped_to_match": [], "flipped_to_no_match": [], "regressions": 0}
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_cells = base.get("cells_by_id", {})
    cur_cells = current.get("cells_by_id", {})
    flipped_to_match = []
    flipped_to_no_match = []
    for cell_id, cur_status in cur_cells.items():
        base_status = base_cells.get(cell_id)
        if base_status == "NO_MATCH" and cur_status == "MATCH":
            flipped_to_match.append(cell_id)
        elif base_status == "MATCH" and cur_status == "NO_MATCH":
            flipped_to_no_match.append(cell_id)
    return {
        "flipped_to_match": flipped_to_match,
        "flipped_to_no_match": flipped_to_no_match,
        "regressions": len(flipped_to_no_match),
    }


def append_progression(round_label: str, current: dict, diff: dict,
                       progression_md: Path) -> None:
    """Append row to reports/round43_match_progression.md."""
    progression_md.parent.mkdir(parents=True, exist_ok=True)
    if not progression_md.exists():
        progression_md.write_text(
            "# Round 43 — Match rate progression\n\n"
            "| Round | Label | Match% | Total | MATCH | NO_MATCH | Δ flipped+ | Δ regressions | Notes |\n"
            "|---|---|---|---|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    line = (
        f"| {round_label} | {current['label']} | "
        f"{current['match_rate']*100:.1f}% | "
        f"{current['total']} | {current['match']} | {current['no_match']} | "
        f"{len(diff['flipped_to_match'])} | {len(diff['flipped_to_no_match'])} | "
        f"{'(see below)' if diff['flipped_to_no_match'] else 'no regressions'} |\n"
    )
    with progression_md.open("a", encoding="utf-8") as fh:
        fh.write(line)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="current")
    p.add_argument("--baseline", default=None,
                   help="Path to baseline measurement JSON (e.g. reports/r42_baseline.json)")
    p.add_argument("--save", default=None,
                   help="Save measurement to this JSON path")
    p.add_argument("--append-round", default=None,
                   help="Append row to reports/round43_match_progression.md")
    args = p.parse_args()

    cur = measure(label=args.label)
    base_path = Path(args.baseline) if args.baseline else None
    diff = diff_vs_baseline(cur, base_path)

    print(f"=== {args.label} ===")
    print(f"  files validated: {cur['n_files']}")
    print(f"  total cells:     {cur['total']}")
    print(f"  MATCH:           {cur['match']} ({cur['match_rate']*100:.2f}%)")
    print(f"  NO_MATCH:        {cur['no_match']}")
    print(f"  NA:              {cur['na']}")
    print()
    print("=== By field ===")
    for fld in sorted(cur["by_field_total"].keys()):
        tot = cur["by_field_total"][fld]
        nm = cur["by_field_no_match"].get(fld, 0)
        m = tot - nm
        rate = m / tot * 100 if tot else 0.0
        print(f"  {fld:18s}: {m:4d}/{tot:4d}  ({rate:.1f}%)")

    if base_path and base_path.exists():
        print()
        print("=== Diff vs baseline ===")
        print(f"  flipped NO_MATCH→MATCH: {len(diff['flipped_to_match'])}")
        print(f"  flipped MATCH→NO_MATCH (regressions): {len(diff['flipped_to_no_match'])}")
        if diff["flipped_to_no_match"]:
            print("  REGRESSIONS:")
            for c in diff["flipped_to_no_match"][:10]:
                print(f"    {c}")

    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved to {save_path}")

    if args.append_round:
        prog = Path("reports/round43_match_progression.md")
        append_progression(args.append_round, cur, diff, prog)
        print(f"\nAppended round '{args.append_round}' to {prog}")


if __name__ == "__main__":
    main()
