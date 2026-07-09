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
from collections import Counter
from pathlib import Path

_CROSSABLE = ("of", "ov", "cliente", "modelo", "lote",
              "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo")


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
        "SELECT id, cross_check, shadow_scoring_json FROM sheets "
        "WHERE id >= ? AND shadow_scoring_json IS NOT NULL "
        "AND cross_check IS NOT NULL ORDER BY id",
        (args.since_id,),
    ):
        try:
            prod = json.loads(s["cross_check"] or "{}")
            shad = json.loads(s["shadow_scoring_json"] or "{}")
        except (TypeError, ValueError):
            continue
        p_rows = prod.get("rows") or []
        s_rows = shad.get("rows") or []
        if not s_rows:
            continue
        n_sheets += 1
        for i in range(min(len(p_rows), len(s_rows))):
            pf = (p_rows[i] or {}).get("fields") or {}
            sf = (s_rows[i] or {}).get("fields") or {}
            for field in _CROSSABLE:
                pc, sc = pf.get(field) or {}, sf.get(field) or {}
                pv = str(pc.get("value") or pc.get("ref") or "").strip()
                sv = str(sc.get("value") or "").strip()
                if not pv and not sv:
                    continue
                n_cells += 1
                ps = str(pc.get("status") or "")
                ss = str(sc.get("status") or "")
                if pv.upper() != sv.upper():
                    n_diff += 1
                    diff_by_field[field] += 1
                    rows_out.append([
                        s["id"], i, field, pv[:40], sv[:40], ps, ss,
                        sc.get("decision_confidence"),
                        sc.get("decision_reason") or "",
                    ])
                elif ps != ss:
                    status_flips[(ps, ss)] += 1

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
