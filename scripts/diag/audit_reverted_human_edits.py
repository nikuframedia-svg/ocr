#!/usr/bin/env python3
"""R260 — Auditoria: correções manuais de identidade revertidas por `system`.

Dois bugs (corrigidos no R260) faziam uma escrita `source='system'` cair por
cima de uma correção manual do operador nos campos of/ov/cliente/modelo,
tornando-a `last-source=system` — desprotegida e depositada errada na fábrica:
  1. a varinha /apply-of-entry escrevia a linha inteira sem respeitar a
     proteção de edições humanas;
  2. threads de cross-check concorrentes revertiam a correção acabada de fazer
     (snapshot `protected` desactualizado).

Este CLI é READ-ONLY: percorre a tabela `edits` e lista as folhas onde um campo
de identidade tem `last-source=system` mas teve antes uma edição `human` com
valor DIFERENTE — a correção do operador que se perdeu. O valor humano original
fica no audit trail (coluna new_value da linha human), pelo que a remediação é
possível — mas NUNCA automática: folhas validadas são imutáveis (EN 1090 /
ISO 9001) e a reinjeção + re-depósito do CSV exige decisão explícita do Luís.

Uso:
    uv run python scripts/diag/audit_reverted_human_edits.py [--db data/app.db] [--json]

Exit codes:
- 0 — nenhuma folha afetada
- 3 — há folhas afetadas (lista impressa)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_ID_FIELDS = ("of", "ov", "cliente", "modelo")


def _id_field(field_path: str) -> bool:
    return any(field_path.endswith("." + f) for f in _ID_FIELDS)


def find_reverted(db_path: Path) -> list[dict]:
    """Campos de identidade cuja última edição é system mas teve um human antes
    com valor diferente (a correção perdida)."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT sheet_id, field_path, new_value, source, id "
            "FROM edits ORDER BY sheet_id, field_path, id"
        ).fetchall()
        status_by_sheet = {
            r["id"]: r["status"]
            for r in con.execute("SELECT id, status FROM sheets").fetchall()
        }
    finally:
        con.close()

    # agrupa por (sheet, path) preservando ordem cronológica (id ASC)
    seq: dict[tuple[int, str], list[sqlite3.Row]] = {}
    for r in rows:
        if not _id_field(r["field_path"]):
            continue
        seq.setdefault((r["sheet_id"], r["field_path"]), []).append(r)

    out: list[dict] = []
    for (sheet_id, field_path), edits in seq.items():
        last = edits[-1]
        if last["source"] != "system":
            continue
        last_human = next(
            (e for e in reversed(edits) if e["source"] == "human"), None
        )
        if last_human is None:
            continue
        human_val = str(last_human["new_value"] or "")
        system_val = str(last["new_value"] or "")
        if human_val == system_val:
            continue  # o system só reformatou/confirmou — sem perda de intenção
        out.append({
            "sheet_id": sheet_id,
            "status": status_by_sheet.get(sheet_id, "?"),
            "field_path": field_path,
            "operador_escreveu": human_val,
            "ficou": system_val,
        })
    out.sort(key=lambda d: (d["sheet_id"], d["field_path"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(_REPO / "data" / "app.db"),
                    help="caminho para o app.db (default: data/app.db)")
    ap.add_argument("--json", action="store_true", help="saída em JSON")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"app.db não encontrado: {db_path}", file=sys.stderr)
        return 2

    affected = find_reverted(db_path)
    if args.json:
        print(json.dumps(affected, ensure_ascii=False, indent=2))
    else:
        if not affected:
            print("Nenhuma correção manual de identidade revertida. ✓")
        else:
            sheets = sorted({a["sheet_id"] for a in affected})
            validated = sorted({a["sheet_id"] for a in affected
                                if a["status"] == "validated"})
            print(f"{len(affected)} campo(s) em {len(sheets)} folha(s) "
                  f"({len(validated)} já validadas) com correção manual revertida:\n")
            for a in affected:
                flag = " [VALIDADA]" if a["status"] == "validated" else ""
                print(f"  folha {a['sheet_id']}{flag}  {a['field_path']}: "
                      f"operador escreveu {a['operador_escreveu']!r}, "
                      f"ficou {a['ficou']!r}")
            print("\nRemediação (reinjeção do valor humano + re-depósito do CSV) "
                  "só com OK do Luís — folhas validadas são imutáveis (EN 1090).")
    return 3 if affected else 0


if __name__ == "__main__":
    raise SystemExit(main())
