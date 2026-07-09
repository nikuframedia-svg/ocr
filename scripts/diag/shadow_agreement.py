"""R250 — Concordância PRODUÇÃO vs SOMBRA (A/B real na fábrica).

Com CROSS_SHADOW_VARIANT=next, a thread de sombra corre a matemática nova
(R250-R252) por folha real e grava em sheets.shadow_scoring_json; a produção
fica intocada. Este script compara os dois outputs célula-a-célula para a
triagem que antecede o flip.

Uso (na fábrica ou sobre uma cópia do app.db):
    uv run python scripts/diag/shadow_agreement.py \
        --db data/app.db [--since-id 0] [--out reports/shadow_agreement.csv]

Critérios de saída do soak (plano R250): >=300 folhas com sombra "next";
divergência de valor final por campo cruzável <= 2%; cada divergência
triada à mão via /sheet/<id>/shadow-view; 0 falhas de sombra.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))

# R253/F2 — a régua do diff é PARTILHADA com /sheet/<id>/shadow-view: a
# mesma decisão de "diverge" no CSV offline e no ecrã de triagem.
from app.cross_check.shadow_diff import diff_prod_vs_shadow
# R253/F2 — a produção NÃO vive em sheets.cross_check (a coluna nunca
# existiu — o SELECT antigo rebentava na primeira corrida real); vive no
# storage por ficheiros (kanban_refs/03_Cross_Check + índice).
from app.cross_check.storage import load_sheet_cross_check


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--since-id", type=int, default=0)
    ap.add_argument("--out", type=Path,
                    default=Path("reports/shadow_agreement.csv"))
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db.expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    n_sheets = n_cells = n_diff = 0
    diff_by_field: Counter = Counter()
    status_flips: Counter = Counter()
    rows_out: list[list] = []
    for s in con.execute(
        "SELECT id, shadow_scoring_json FROM sheets "
        "WHERE id >= ? AND shadow_scoring_json IS NOT NULL ORDER BY id",
        (args.since_id,),
    ):
        try:
            shad = json.loads(s["shadow_scoring_json"] or "{}")
        except (TypeError, ValueError):
            continue
        prod = load_sheet_cross_check(int(s["id"]), include_stale=True)
        if not prod:
            continue
        if not (shad.get("rows") or []):
            continue
        n_sheets += 1
        d = diff_prod_vs_shadow(prod, shad)
        n_cells += d["n_cells"]
        n_diff += len(d["diffs"])
        for dd in d["diffs"]:
            diff_by_field[dd["field"]] += 1
            rows_out.append([
                s["id"], dd["row"], dd["field"], dd["producao"][:40],
                dd["sombra"][:40], dd["status_prod"], dd["status_sombra"],
                dd["sombra_conf"], dd["sombra_reason"],
            ])
        for fl in d["status_flips"]:
            status_flips[(fl["de"], fl["para"])] += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sheet_id", "row", "field", "producao", "sombra",
                    "status_prod", "status_sombra", "sombra_conf",
                    "sombra_reason"])
        w.writerows(rows_out)

    pct = round(100 * n_diff / n_cells, 2) if n_cells else None
    print(f"folhas com sombra: {n_sheets} | células comparadas: {n_cells}")
    print(f"divergências de VALOR: {n_diff} ({pct}%) -> {args.out}")
    for f, k in diff_by_field.most_common():
        print(f"  {f}: {k}")
    if status_flips:
        print("flips de STATUS (valor igual):")
        for (a, b), k in status_flips.most_common(8):
            print(f"  {a} -> {b}: {k}")
    if n_sheets >= 300 and pct is not None and pct <= 2.0:
        print("SOAK OK: n>=300 e divergência <=2% — triagem manual e flip.")
    else:
        print("SOAK AINDA NÃO CUMPRIDO (n>=300 folhas e <=2% divergência).")


if __name__ == "__main__":
    main()
