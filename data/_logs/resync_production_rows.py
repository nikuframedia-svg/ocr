"""R86 — Re-sync production_rows for all sheets.

After adding m2 + nesting columns to production_rows, the 133 existing
sheets have those fields NULL. This script iterates all sheets and
re-runs `_sync_production_rows` to populate the new fields from the
sheet_data JSON blobs.

Usage:
    .venv/Scripts/python.exe data/_logs/resync_production_rows.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from app.web.db import _sync_production_rows, conn, init_db


def main() -> int:
    init_db()  # ensure migrations are applied
    n_synced = 0
    n_m2 = 0
    n_nesting = 0
    with conn() as c:
        rows = c.execute(
            "SELECT id, sheet_data FROM sheets WHERE sheet_data IS NOT NULL"
        ).fetchall()
        for r in rows:
            sd = json.loads(r["sheet_data"])
            _sync_production_rows(c, r["id"], sd)
            n_synced += 1
        c.commit()

        # Post-sync sanity check
        n_m2 = c.execute(
            "SELECT COUNT(*) FROM production_rows WHERE m2 IS NOT NULL"
        ).fetchone()[0]
        n_nesting = c.execute(
            "SELECT COUNT(*) FROM production_rows WHERE nesting IS NOT NULL"
        ).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM production_rows").fetchone()[0]

    print(f"Synced {n_synced} sheets")
    print(f"production_rows total: {total}")
    print(f"  with m2:      {n_m2}")
    print(f"  with nesting: {n_nesting}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
