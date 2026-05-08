"""Round 46 — retroactively auto-crop all existing kanban images in DB.

Runs the auto_crop pipeline over every sheet's image. Saves cropped
versions next to originals as <stem>_cropped.jpg. Idempotent: skips
images that already have a cropped sibling unless --force.

Usage:
    .venv/Scripts/python.exe scripts/auto_crop_existing.py
    .venv/Scripts/python.exe scripts/auto_crop_existing.py --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.web import db  # noqa: E402
from app.web.image_crop import auto_crop, cropped_path_for  # noqa: E402

_DATA_DIR = REPO / "data"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="re-crop even if _cropped sibling exists")
    args = p.parse_args()

    sheets = db.list_sheets(limit=10000)
    print(f"Found {len(sheets)} sheets")
    ok = 0
    skip = 0
    fail = 0
    not_found = 0
    for s in sheets:
        sid = s["id"]
        img_rel = s.get("image_path")
        if not img_rel:
            continue
        img_path = _DATA_DIR / img_rel
        if not img_path.exists():
            print(f"  sheet {sid}: image missing ({img_rel})")
            not_found += 1
            continue
        out = cropped_path_for(img_path)
        if out.exists() and not args.force:
            skip += 1
            continue
        result = auto_crop(img_path)
        if result is not None:
            print(f"  sheet {sid}: OK → {result.name}")
            ok += 1
        else:
            print(f"  sheet {sid}: detection failed (will fallback to original)")
            fail += 1

    print()
    print(f"Done. OK={ok}, skipped={skip}, detect_failed={fail}, not_found={not_found}")


if __name__ == "__main__":
    main()
