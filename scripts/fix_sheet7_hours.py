"""One-off (Round 34c): re-sync sheet 7 production_rows after the
_parse_hours sanity-clamp fix. Sheet 7 had footer.horas_trabalhadas =
'10100' (OCR misread of '10:00') which polluted month/year hours
aggregates. After the clamp, _sync_production_rows re-parses the same
raw value and stores sheet_hours=NULL.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.web.db import _sync_production_rows  # noqa: E402

DB_PATH = ROOT / "data" / "app.db"


def main() -> int:
    if not DB_PATH.exists():
        print(f"DB not found at {DB_PATH}", file=sys.stderr)
        return 1

    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")

    row = c.execute("SELECT sheet_data FROM sheets WHERE id = 7").fetchone()
    if row is None:
        print("Sheet 7 not in DB.")
        return 0

    sheet_data = json.loads(row["sheet_data"])
    raw_before = sheet_data.get("footer", {}).get("horas_trabalhadas")
    print(f"sheet 7 footer.horas_trabalhadas (raw, unchanged) = {raw_before!r}")

    before = c.execute(
        "SELECT DISTINCT sheet_hours FROM production_rows WHERE sheet_id = 7"
    ).fetchall()
    print(f"  production_rows.sheet_hours BEFORE: {[dict(r)['sheet_hours'] for r in before]}")

    _sync_production_rows(c, 7, sheet_data)
    c.commit()

    after = c.execute(
        "SELECT DISTINCT sheet_hours FROM production_rows WHERE sheet_id = 7"
    ).fetchall()
    print(f"  production_rows.sheet_hours AFTER:  {[dict(r)['sheet_hours'] for r in after]}")
    print()
    print("Done. The raw value in sheets.sheet_data is preserved so the user")
    print("can fix it via the desktop UI at /sheet/7 (set horas_trabalhadas")
    print("to the real value, e.g. '10:00').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
