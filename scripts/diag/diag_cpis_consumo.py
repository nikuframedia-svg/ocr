#!/usr/bin/env python3
"""R266 -- diagnostico das linhas Bobine/Formato sem KPIs de consumo no CPIS.

Para cada linha de production_rows do dia (default: so Bobine/Formato),
recalcula os pesos com o motor atual e imprime PORQUE e que o bloco
Nº Chapas / Peso Consumido / Desperdicio saiu vazio:
  - a OF bateu no plano? com que npecas?
  - o lote resolveu no StockSAP? (exato/alias, correcao H->M aceite/rejeitada,
    ou nao encontrado)
  - larg/esp/comp resolvidos e o veredicto das guardas de sanidade.

Correr NA FABRICA (dados vivos em data/app.db + kanban_refs/04_Documentacao):
    .venv\\Scripts\\python.exe scripts\\diag\\diag_cpis_consumo.py --date 2026-08-11
    .venv\\Scripts\\python.exe scripts\\diag\\diag_cpis_consumo.py --date 2026-08-11 --all-setores
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from math import ceil
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="dia (YYYY-MM-DD, sheet_iso_date)")
    ap.add_argument("--db", default=str(_REPO / "data" / "app.db"))
    ap.add_argument(
        "--all-setores",
        action="store_true",
        help="inclui todas as maquinas (default: so linhas direct-consumption)",
    )
    args = ap.parse_args()

    from app.cross_check import get_watcher
    from app.production.weights import (
        _resolve_npecas,
        _valid_consumption_input,
        calculate_row_weights,
        find_plan_entry,
        is_direct_consumption_row,
    )
    from app.pipeline.scoring_engine import _resolve_row_lote

    refs = get_watcher().get_refs()
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT pr.*, json_extract(s.sheet_data, '$.header.setor_maquina') AS setor_maquina
        FROM production_rows pr JOIN sheets s ON s.id = pr.sheet_id
        WHERE pr.sheet_iso_date = ? AND s.status = 'validated'
        ORDER BY pr.operador, pr.sheet_id, pr.row_index
        """,
        (args.date,),
    ).fetchall()
    if not rows:
        print(f"sem production_rows validadas para {args.date} em {args.db}")
        return 1

    n_shown = n_blank = 0
    for r in rows:
        row = dict(r)
        if not args.all_setores and not is_direct_consumption_row(row):
            continue
        n_shown += 1
        out = calculate_row_weights(row, refs)
        plan_entry = find_plan_entry(row, refs)
        canonical, _entry, lote_kind = _resolve_row_lote(refs, row)
        npecas = _resolve_npecas(
            plan_entry=plan_entry,
            larg=out.larg_mm,
            lbase=out.lbase,
            ltopo=out.ltopo,
            comp=out.comp_mm,
        )
        valid = _valid_consumption_input(
            float(row.get("qtd") or 0) or None,
            out.larg_mm,
            out.comp_mm,
            out.esp_mm,
            npecas,
        )
        blank = out.n_chapas is None
        n_blank += 1 if blank else 0
        tag = "VAZIO" if blank else "ok   "
        plan_np = plan_entry.get("npecas") if plan_entry else "(sem plano)"
        print(
            f"[{tag}] sheet={row['sheet_id']} row={row['row_index']} "
            f"op={row['operador']} of={row['of']} modelo={str(row['modelo'])[:20]!r}"
        )
        print(
            f"        plano: match={'sim' if plan_entry else 'NAO'} npecas={plan_np} | "
            f"lote: '{row.get('lote') or ''}' -> {canonical or '-'} ({lote_kind})"
        )
        print(
            f"        resolvido: qtd={row.get('qtd')} larg={out.larg_mm} "
            f"comp={out.comp_mm} esp={out.esp_mm} lbase={out.lbase} ltopo={out.ltopo} "
            f"npecas={npecas} guardas={'PASSA' if valid else 'FALHA'}"
        )
        if blank:
            if npecas is None:
                why = "npecas irresoluvel (plano vazio E geometria invalida: ver lbase/ltopo/larg)"
            elif not valid:
                why = "guardas de sanidade (larg 200-3000, esp 0.5-30, comp<=20000)"
            else:
                why = "qtd em falta/<=0"
            print(f"        CAUSA: {why}")
        else:
            chapas = ceil(float(row["qtd"]) / npecas) if npecas else None
            print(
                f"        chapas={chapas} consumido={out.peso_consumido_kg:.1f}kg "
                f"produzido={out.peso_produzido_kg or 0:.1f}kg "
                f"desperdicio={(out.desperdicio_kg or 0):.1f}kg"
            )
    print()
    print(f"{n_shown} linhas analisadas, {n_blank} com bloco de consumo vazio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
