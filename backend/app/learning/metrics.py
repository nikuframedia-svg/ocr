"""Success metric — human corrections per validated sheet.

If the learning loop works, this number falls over time: the OCR gets
more right on the first pass, so operators correct less.
"""
from __future__ import annotations

from app.learning import store
from app.web import db


def corrections_per_sheet(window: int = 50) -> float:
    """Mean number of human edits across the last ``window`` validated
    sheets. System auto-snaps are excluded (``source='human'`` only)."""
    with db.conn() as c:
        sheet_rows = c.execute(
            "SELECT id FROM sheets WHERE status = 'validated' "
            "ORDER BY validated_at DESC LIMIT ?",
            (window,),
        ).fetchall()
        ids = [r["id"] for r in sheet_rows]
        if not ids:
            return 0.0
        placeholders = ",".join("?" * len(ids))
        n = c.execute(
            f"SELECT COUNT(*) n FROM edits "
            f"WHERE source = 'human' AND sheet_id IN ({placeholders})",
            ids,
        ).fetchone()["n"]
    return round(n / len(ids), 3)


def corrections_trend() -> list[dict]:
    """Time series for the dashboard: one point per completed learning run."""
    return [
        {
            "run_id": r["id"],
            "validated_count": r["validated_count_at_run"],
            "corrections_per_sheet": r["corrections_per_sheet"],
            # str() — the column can come back as a datetime (PARSE_DECLTYPES),
            # which Jinja's `tojson` cannot serialize.
            "finished_at": str(r["finished_at"]) if r["finished_at"] else None,
        }
        for r in store.list_runs()
    ]
