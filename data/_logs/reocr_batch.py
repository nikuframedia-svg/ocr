"""R84 — Batch re-OCR de todas as folhas non-bobine.

Aplica o fix de schema (ocr6.normalize_extraction agora recebe schemas
template-específicos). Substitui raw_extraction + sheet_data + dq_audit
e re-corre cross-check.

Usage:
    OCR_NO_THINK=1 .venv/Scripts/python.exe data/_logs/reocr_batch.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

os.environ.setdefault("OCR_NO_THINK", "1")

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

from app.web.ocr_runner import rerun_pipeline_for_template  # noqa: E402


def list_non_bobine_sheets(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        return [
            dict(r) for r in c.execute(
                "SELECT id, image_path, "
                "       json_extract(sheet_data, '$.template_name') AS tpl "
                "  FROM sheets "
                " WHERE json_extract(sheet_data, '$.template_name') IS NOT NULL "
                "   AND json_extract(sheet_data, '$.template_name') != 'bobine_formato' "
                "   AND status != 'deleted' "
                " ORDER BY id"
            )
        ]


def apply_reocr(db_path: Path, sheet_id: int, raw_dict: dict, dq_dict: dict, current_dict: dict) -> None:
    with sqlite3.connect(db_path) as c:
        c.execute(
            "UPDATE sheets SET raw_extraction=?, sheet_data=?, dq_audit=? WHERE id=?",
            (
                json.dumps(raw_dict, ensure_ascii=False),
                json.dumps(current_dict, ensure_ascii=False),
                json.dumps(dq_dict, ensure_ascii=False),
                sheet_id,
            ),
        )
        c.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-tpl", default=None, help="Filter to single template")
    args = parser.parse_args()

    db_path = _REPO / "data" / "app.db"
    images_dir = _REPO / "data"

    sheets = list_non_bobine_sheets(db_path)
    if args.only_tpl:
        sheets = [s for s in sheets if s["tpl"] == args.only_tpl]
    if args.limit:
        sheets = sheets[: args.limit]

    print(f"R84 batch re-OCR: {len(sheets)} sheets (dry_run={args.dry_run})")
    print(f"OCR_NO_THINK={os.environ.get('OCR_NO_THINK')}")
    print()

    ok, fail = 0, 0
    t0 = time.time()
    for i, s in enumerate(sheets, 1):
        sid = s["id"]
        tpl = s["tpl"]
        img_rel = (s["image_path"] or "").replace("\\", "/")
        img = images_dir / img_rel
        if not img.exists():
            print(f"[{i}/{len(sheets)}] sid={sid} tpl={tpl}  SKIP — image not found: {img}")
            fail += 1
            continue
        try:
            result = rerun_pipeline_for_template(img, tpl)
        except Exception as e:
            print(f"[{i}/{len(sheets)}] sid={sid} tpl={tpl}  FAIL: {type(e).__name__}: {e}")
            fail += 1
            continue
        raw = result["raw"]
        rows = raw.get("rows") or []
        keys = list(rows[0].keys()) if rows else []
        print(f"[{i}/{len(sheets)}] sid={sid:>3} tpl={tpl:<11}  rows={len(rows):>2}  keys={keys}")
        if not args.dry_run:
            apply_reocr(db_path, sid, raw, result["dq"], result["current"])
        ok += 1

    dt = time.time() - t0
    print()
    print(f"DONE: {ok} ok, {fail} fail  in {dt:.1f}s  ({dt/max(1,ok):.1f}s/sheet)")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
