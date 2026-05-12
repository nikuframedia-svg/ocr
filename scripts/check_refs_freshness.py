"""Round 48 Phase D — report sheets with OFs/lotes missing in refs.

Cross-references the OFs and lotes from existing DB sheets against
plan_colunas (mined as ofs_plan) and StockSAP (mined as lotes_sap).
Output: list of cells that are NO_MATCH because the reference is
absent, NOT because of OCR error or operator divergence.

Useful for:
  - Telling the user which sheets remain "stuck" until external refs
    are refreshed (StockSAP.xlsx + plan_colunas_cpis.xlsx)
  - Distinguishing system bugs from data hygiene issues
  - Estimating impact of a refs refresh

Usage:
    .venv/Scripts/python.exe scripts/check_refs_freshness.py
    .venv/Scripts/python.exe scripts/check_refs_freshness.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from app.dq.snap import _OFS_PLAN_STR, _LOTES_SAP  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="emit JSON report")
    args = p.parse_args()

    c = sqlite3.connect(REPO / "data" / "app.db")
    c.row_factory = sqlite3.Row
    sheets = c.execute(
        "SELECT id, sheet_data FROM sheets WHERE status NOT IN ('error', 'pending')"
    ).fetchall()
    c.close()

    missing_ofs: dict[str, list[tuple[int, int]]] = defaultdict(list)  # of → [(sheet_id, row_idx)]
    missing_lotes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    seen_ofs: set[str] = set()
    seen_lotes: set[str] = set()

    for s in sheets:
        if not s["sheet_data"]:
            continue
        try:
            sd = json.loads(s["sheet_data"])
        except json.JSONDecodeError:
            continue
        for ri, row in enumerate(sd.get("rows", [])):
            of = str(row.get("of") or "").strip()
            lote = str(row.get("lote") or "").strip().upper()
            if of:
                seen_ofs.add(of)
                if of not in _OFS_PLAN_STR:
                    missing_ofs[of].append((s["id"], ri))
            if lote:
                seen_lotes.add(lote)
                if lote not in _LOTES_SAP:
                    missing_lotes[lote].append((s["id"], ri))

    n_of_cells = sum(len(v) for v in missing_ofs.values())
    n_lote_cells = sum(len(v) for v in missing_lotes.values())

    if args.json:
        out = {
            "summary": {
                "total_distinct_ofs_in_db": len(seen_ofs),
                "missing_ofs_distinct": len(missing_ofs),
                "missing_of_cells": n_of_cells,
                "total_distinct_lotes_in_db": len(seen_lotes),
                "missing_lotes_distinct": len(missing_lotes),
                "missing_lote_cells": n_lote_cells,
                "n_lotes_in_sap": len(_LOTES_SAP),
                "n_ofs_in_plan": len(_OFS_PLAN_STR),
            },
            "missing_ofs": {of: cells for of, cells in missing_ofs.items()},
            "missing_lotes": {lote: cells for lote, cells in missing_lotes.items()},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("=== Refs freshness report ===")
    print(f"Refs loaded: {len(_OFS_PLAN_STR)} OFs in plan, {len(_LOTES_SAP)} lotes in SAP")
    print(f"DB has: {len(seen_ofs)} distinct OFs, {len(seen_lotes)} distinct lotes")
    print()
    print(f"OFs MISSING in plan: {len(missing_ofs)} distinct → "
          f"{n_of_cells} cells flagged NO_MATCH")
    if missing_ofs:
        for of, cells in sorted(missing_ofs.items(), key=lambda x: -len(x[1])):
            sheet_ids = sorted({sid for sid, _ in cells})
            print(f"  {of}: {len(cells)}x cells in sheets {sheet_ids}")
    print()
    print(f"Lotes MISSING in SAP: {len(missing_lotes)} distinct → "
          f"{n_lote_cells} cells flagged NO_MATCH")
    if missing_lotes:
        for lote, cells in sorted(missing_lotes.items(), key=lambda x: -len(x[1])):
            sheet_ids = sorted({sid for sid, _ in cells})
            print(f"  {lote}: {len(cells)}x cells in sheets {sheet_ids}")
    print()
    print(f"=== Total cells dependent on refs refresh: {n_of_cells + n_lote_cells} ===")
    print()
    print("Action: refresh the source XLSX files and re-mine:")
    print(f"  - C:\\kanban\\nifruka\\04_Documentacao\\plan_colunas_cpis.xlsx")
    print(f"  - C:\\kanban\\nifruka\\04_Documentacao\\StockSAP.xlsx")
    print("Then: POST /admin/reload-refs to pick up new entries.")

    # R52 F5: also write a curated markdown report to reports/refs_to_refresh.md
    # for sharing with the factory team. Sorted by frequency, with sheet IDs.
    out_md = REPO / "reports" / "refs_to_refresh.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Refs a actualizar — relatório para factory team",
        "",
        f"Gerado: {sum(len(v) for v in missing_ofs.values()) + sum(len(v) for v in missing_lotes.values())} "
        "cells em sheets actuais dependem de refs ausentes em plan_colunas/StockSAP.",
        "",
        "## OFs ausentes em plan_colunas_cpis.xlsx",
        "",
    ]
    if missing_ofs:
        lines.append("| OF | # cells | Sheets |")
        lines.append("|---|---|---|")
        for of, cells in sorted(missing_ofs.items(), key=lambda x: -len(x[1])):
            sheet_ids = sorted({sid for sid, _ in cells})
            lines.append(f"| `{of}` | {len(cells)} | {', '.join(map(str, sheet_ids))} |")
    else:
        lines.append("_(nenhuma)_")
    lines.append("")
    lines.append("## Lotes ausentes em StockSAP.xlsx")
    lines.append("")
    if missing_lotes:
        lines.append("| Lote | # cells | Sheets |")
        lines.append("|---|---|---|")
        for lote, cells in sorted(missing_lotes.items(), key=lambda x: -len(x[1])):
            sheet_ids = sorted({sid for sid, _ in cells})
            lines.append(f"| `{lote}` | {len(cells)} | {', '.join(map(str, sheet_ids))} |")
    else:
        lines.append("_(nenhum)_")
    lines.append("")
    lines.append("## Acção")
    lines.append("")
    lines.append("1. Verificar com a factory se estes OFs/lotes existem em sistema interno")
    lines.append("2. Actualizar os XLSX em `C:\\kanban\\nifruka\\04_Documentacao\\`")
    lines.append("3. `curl -X POST http://127.0.0.1:8080/admin/reload-refs` para recarregar")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nRelatório markdown escrito: {out_md}")


if __name__ == "__main__":
    main()
