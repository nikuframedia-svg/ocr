"""Round 43 — Test 10 stub-accept variants offline.

Imports cross_check_sheet directly and runs each sheet through it with
different CC_STUB_VARIANT env values. Measures match rate per variant
WITHOUT touching uvicorn or persistent JSONs.

Usage:
    .venv/Scripts/python.exe scripts/test_stub_variants.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

VARIANTS = ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10",
            "v11", "v12", "v13", "v14", "v15",
            "w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9", "w10",
            "w11", "w12", "w13", "w14", "w15")


def measure_with_variant(variant: str) -> dict:
    """Run all sheets through cross_check_sheet with given variant.
    Returns aggregate match stats."""
    os.environ["CC_STUB_VARIANT"] = variant

    # Import fresh to ensure env is read
    from app.cross_check.engine import cross_check_sheet
    from app.cross_check.ref_watcher import RefWatcher

    refs = RefWatcher().get_refs()
    conn = sqlite3.connect(REPO / "data" / "app.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, sheet_data, dq_audit FROM sheets WHERE status NOT IN ('error', 'pending')"
    ).fetchall()
    conn.close()

    total = 0
    match = 0
    no_match = 0
    na = 0
    by_field_total = {}
    by_field_match = {}

    for row in rows:
        sd_raw = row["sheet_data"]
        if not sd_raw:
            continue
        try:
            sd = json.loads(sd_raw)
            dq = json.loads(row["dq_audit"]) if row["dq_audit"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        result = cross_check_sheet(sd, dq, refs)
        for r in result.get("rows", []):
            for fld_name, fld in r.get("fields", {}).items():
                s = fld.get("status")
                if s in ("MATCH", "NO_MATCH"):
                    by_field_total[fld_name] = by_field_total.get(fld_name, 0) + 1
                    total += 1
                    if s == "MATCH":
                        match += 1
                        by_field_match[fld_name] = by_field_match.get(fld_name, 0) + 1
                    else:
                        no_match += 1
                else:
                    na += 1
    return {
        "variant": variant,
        "total": total,
        "match": match,
        "no_match": no_match,
        "na": na,
        "match_rate": match / total if total else 0.0,
        "by_field_total": by_field_total,
        "by_field_match": by_field_match,
    }


def main():
    print(f"{'Variant':<8} {'MATCH':>6} {'NO_M':>6} {'NA':>5} {'Total':>6} {'Rate':>8}")
    print("-" * 50)
    results = []
    for v in VARIANTS:
        r = measure_with_variant(v)
        results.append(r)
        print(f"{v:<8} {r['match']:>6} {r['no_match']:>6} {r['na']:>5} {r['total']:>6} "
              f"{r['match_rate']*100:>7.2f}%")

    print()
    print("=== Per-field MATCH count, by variant ===")
    fields = sorted(results[0]["by_field_total"].keys())
    print(f"{'Field':<12}", end="")
    for v in VARIANTS:
        print(f"{v:>6}", end="")
    print()
    for fld in fields:
        print(f"{fld:<12}", end="")
        for r in results:
            m = r["by_field_match"].get(fld, 0)
            t = r["by_field_total"].get(fld, 0)
            pct = (m / t * 100) if t else 0
            print(f"{pct:>5.0f}%", end=" ")
        print()

    print()
    best = max(results, key=lambda x: x["match_rate"])
    print(f"=== BEST: {best['variant']} with {best['match_rate']*100:.2f}% match rate ===")
    print(f"  MATCH: {best['match']}, NO_MATCH: {best['no_match']}, NA: {best['na']}")

    # Save best for the loop
    save_path = REPO / "reports" / "round43_variants_results.json"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved all results to {save_path}")


if __name__ == "__main__":
    main()
