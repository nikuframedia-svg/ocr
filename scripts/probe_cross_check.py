"""R123 — Probe do cross-check sobre a app.db (read-only, diagnóstico).

Corre ``cross_check_sheet`` em memória sobre todas as folhas ``extracted``
e agrega % MATCH / NO_MATCH / NA — global, por template e por campo. Não
grava nada; serve para medir o efeito de cada fase do R123 sem depender
dos JSONs já gravados em 03_Cross_Check.

NA == cinza na UI (sem cor). MATCH == verde. NO_MATCH == vermelho/amarelo.

Uso:
    uv run python scripts/probe_cross_check.py
    uv run python scripts/probe_cross_check.py --label "Fase 1"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "backend"))

from app.cross_check import cross_check_sheet, get_watcher  # noqa: E402

_DB = _REPO / "data" / "app.db"


def _load_sheets() -> list[tuple[int, dict]]:
    con = sqlite3.connect(str(_DB))
    con.row_factory = sqlite3.Row
    out: list[tuple[int, dict]] = []
    for r in con.execute(
        "SELECT id, sheet_data FROM sheets "
        "WHERE status = 'extracted' AND sheet_data IS NOT NULL"
    ):
        try:
            out.append((r["id"], json.loads(r["sheet_data"])))
        except (json.JSONDecodeError, TypeError):
            continue
    con.close()
    return out


def _line(name: str, d: dict) -> str:
    m, nm, na = d.get("MATCH", 0), d.get("NO_MATCH", 0), d.get("NA", 0)
    t = m + nm + na
    pct = 100 * m / t if t else 0.0
    gray = 100 * na / t if t else 0.0
    return (
        f"  {name:<24} M={m:<5} NM={nm:<5} NA={na:<5} "
        f"tot={t:<5} MATCH={pct:5.1f}% cinza={gray:5.1f}%"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="current")
    args = ap.parse_args()

    refs = get_watcher().get_refs()
    if not refs.get("available"):
        print("AVISO: refs indisponíveis — cross-check vai degradar tudo a NA.")

    sheets = _load_sheets()
    glob: dict[str, int] = defaultdict(int)
    by_tpl: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_field: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    hdr: dict[str, int] = defaultdict(int)
    errors = 0

    for sid, sd in sheets:
        try:
            res = cross_check_sheet(sd, None, refs)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  sheet {sid}: ERRO {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        tpl = res.get("template_name") or sd.get("template_name") or "?"
        for row in res.get("rows", []):
            for fld, info in (row.get("fields") or {}).items():
                st = (info or {}).get("status", "NA")
                glob[st] += 1
                by_tpl[tpl][st] += 1
                by_field[fld][st] += 1
        for sec in ("header", "footer"):
            for info in (res.get(sec) or {}).values():
                hdr[(info or {}).get("status", "NA")] += 1

    print(f"=== PROBE cross-check — {args.label} ===")
    print(f"folhas: {len(sheets)}  erros: {errors}")
    print()
    print("GLOBAL (celulas de linha):")
    print(_line("global", glob))
    print()
    print("POR TEMPLATE:")
    for tpl in sorted(by_tpl, key=lambda t: -sum(by_tpl[t].values())):
        print(_line(tpl, by_tpl[tpl]))
    print()
    print("POR CAMPO:")
    for fld in sorted(by_field):
        print(_line(fld, by_field[fld]))
    print()
    print("HEADER+FOOTER:")
    print(_line("header+footer", hdr))


if __name__ == "__main__":
    main()
