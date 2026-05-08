"""Apply new snap_lote/snap_of rules (R27) to existing R24 *_fixed.json outputs.

Doesn't re-run OCR. Only re-snaps the `lote` and `of` row fields with the
new rules (SAP/plan lookup + Lev-1 + OV disambiguation). Other fields are
preserved as-is.

Output: reports/extractions_v_qwen35_round27/<stem>_fixed.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.stdout.reconfigure(encoding='utf-8')

from app.dq.snap import snap_lote, snap_of, snap_modelo, snap_ov  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "reports" / "extractions_v_qwen35_round24"
DST = REPO / "reports" / "extractions_v_qwen35_round27"
DST.mkdir(exist_ok=True)


def main():
    fixeds = sorted(SRC.glob("*_fixed.json"))
    print(f"Re-snapping {len(fixeds)} sheets")
    diffs = []
    for fp in fixeds:
        data = json.loads(fp.read_text(encoding="utf-8"))
        rows = data.get("rows", [])
        for i, row in enumerate(rows):
            # of (with row context for OV disambiguation) — runs first so
            # post-snap OF is in row when modelo snap reads it.
            of_old = row.get("of", "")
            of_res = snap_of(of_old, row_context=row)
            if of_res.applied and of_res.snapped != of_old:
                diffs.append((fp.stem, i, "of", of_old, of_res.snapped, of_res.rule))
                row["of"] = of_res.snapped
            # lote
            lote_old = row.get("lote", "")
            lote_res = snap_lote(lote_old)
            if lote_res.applied and lote_res.snapped != lote_old:
                diffs.append((fp.stem, i, "lote", lote_old, lote_res.snapped, lote_res.rule))
                row["lote"] = lote_res.snapped
            # ov (uses post-snap OF for plan-OV Lev-1 lookup) — must run AFTER of
            ov_old = row.get("ov", "")
            ov_res = snap_ov(ov_old, row_context=row)
            if ov_res.applied and ov_res.snapped != ov_old:
                diffs.append((fp.stem, i, "ov", ov_old, ov_res.snapped, ov_res.rule))
                row["ov"] = ov_res.snapped
            # modelo (uses post-snap OF for plan.designacao lookup)
            modelo_old = row.get("modelo", "")
            modelo_res = snap_modelo(modelo_old, row_context=row)
            if modelo_res.applied and modelo_res.snapped != modelo_old:
                diffs.append((fp.stem, i, "modelo", modelo_old, modelo_res.snapped, modelo_res.rule))
                row["modelo"] = modelo_res.snapped
        out_path = DST / fp.name
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote → {DST}")
    print(f"\nApplied snaps ({len(diffs)} cells changed):")
    for stem, i, field, old, new, rule in diffs:
        sheet = stem.replace("_fixed", "")
        print(f"  {sheet:42s} row[{i}] {field:5s}: {old!r:>14} -> {new!r:<14} ({rule})")


if __name__ == "__main__":
    main()
