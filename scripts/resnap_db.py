"""Re-snap existing DB sheets with current snap rules (Round 27+).

Reads each sheet's `raw_extraction` from data/app.db, re-runs the DQ
pipeline (which loads the current snap.py rules), and writes back the
new `sheet_data` and `dq_audit`. Idempotent — safe to run repeatedly.

Sheets with status='error' or 'pending' are skipped (no raw_extraction yet).

Usage:
    .venv\\Scripts\\python.exe scripts\\resnap_db.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding='utf-8')

from app.dq.pipeline import run_dq  # noqa: E402
from app.web import db  # noqa: E402


def _diff_rows(old: list[dict], new: list[dict]) -> list[tuple[int, str, str, str]]:
    """Return list of (row_index, field, old_value, new_value) for cells changed."""
    diffs = []
    for i, (o, n) in enumerate(zip(old, new)):
        for field in (o.keys() | n.keys()):
            ov = str(o.get(field, ""))
            nv = str(n.get(field, ""))
            if ov != nv:
                diffs.append((i, field, ov, nv))
    return diffs


def main() -> None:
    sheets = db.list_sheets(limit=10000)
    eligible = [s for s in sheets if s["status"] not in ("error", "pending")]
    print(f"Total sheets: {len(sheets)} | eligible to re-snap: {len(eligible)}")
    if not eligible:
        print("Nothing to do.")
        return

    total_diffs = 0
    for s in eligible:
        sheet_id = s["id"]
        full = db.get_sheet(sheet_id)
        if full is None:
            continue
        raw = full.get("raw_extraction")
        if not raw:
            print(f"  Sheet {sheet_id}: no raw_extraction, skip")
            continue

        old_data = full.get("sheet_data") or {}
        sheet_name = Path(full["image_path"]).stem

        result, fixed = run_dq(raw, sheet_name=sheet_name)
        dq_audit = {
            "cells": {p: c.to_dict() for p, c in result.cells.items()},
            "violations": [v.__dict__ for v in result.violations],
            "aggregate_score": result.aggregate_score,
            "stp_eligible": result.stp_eligible,
        }
        # Convert any non-serializable objects in violations
        dq_audit_json = json.loads(json.dumps(dq_audit, ensure_ascii=False, default=str))

        diffs = _diff_rows(old_data.get("rows", []), fixed.get("rows", []))
        total_diffs += len(diffs)

        print(f"  Sheet {sheet_id} ({sheet_name}): {len(diffs)} cells changed")
        for i, field, ov, nv in diffs:
            print(f"    row[{i}] {field:>10}: {ov!r:>20} -> {nv!r}")

        db.update_extraction(
            sheet_id=sheet_id,
            raw_extraction=raw,
            dq_audit=dq_audit_json,
            sheet_data=fixed,
        )

    print(f"\nDone. Total cells changed across all sheets: {total_diffs}")


if __name__ == "__main__":
    main()
