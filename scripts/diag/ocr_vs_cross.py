#!/usr/bin/env python3
"""R228 -- OCR vs CROSS, limpo.

Para cada folha e cada linha, mostra campo a campo: o que o OCR LEU e o que o
CROSS ESCOLHEU (mais o estado: igual / corrigiu / trocou por algo nada-a-ver /
manteve para rever). Le o mesmo data/_logs/profile.jsonl do profile_report, mas
SEM ruido (tempos, pools, candidatos) -- so a comparacao OCR -> cross.

Uso (na fabrica, com o python do .venv):
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --sheet 1952
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --last 5
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --sheet 1952 --so-trocas
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOG = _REPO / "data" / "_logs" / "profile.jsonl"


def _read_records(log: Path) -> list[dict]:
    recs: list[dict] = []
    # rotacionado (.jsonl.1) primeiro = mais antigo, depois o atual.
    for p in (log.with_suffix(".jsonl.1"), log):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return recs


def _show(v: object) -> str:
    s = "" if v is None else str(v)
    return s if s.strip() else "(vazio)"


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _estado(d: dict) -> str:
    ocr = str(d.get("ocr_value") or "").strip()
    fin = str(d.get("final_value") or "").strip()
    st = (d.get("engine_status") or "").lower()
    src = (d.get("source") or "").lower()
    if not fin and not ocr:
        return "vazio"
    if ocr == fin:
        return "= igual"
    if not ocr and fin:
        return "+ preencheu pelo plano" if src == "plan" else "+ preencheu"
    if src == "ocr_raw":
        return "~ manteve o OCR (marcado p/ rever)"
    if st == "very_different":
        return "XX TROCOU por valor do plano  <-- NADA A VER?"
    return "~ corrigiu pelo plano"


def main() -> int:
    # Windows PowerShell/console pode ser cp1252; garante que valores acentuados
    # do plano (ex.: TRAPEZIOS) nunca rebentam o print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="OCR vs Cross por folha (R228).")
    ap.add_argument("--log", type=Path, default=_DEFAULT_LOG)
    ap.add_argument("--sheet", type=int, default=None, help="so esta folha")
    ap.add_argument("--last", type=int, default=None, help="ultimas N folhas")
    ap.add_argument("--so-trocas", action="store_true",
                    help="mostra so os campos onde o cross trocou o que o OCR leu")
    args = ap.parse_args()

    recs = _read_records(args.log)
    if args.sheet is not None:
        recs = [r for r in recs if r.get("sheet_id") == args.sheet]
    elif args.last is not None:
        recs = recs[-args.last:]
    if not recs:
        print(f"Sem registos em {args.log}.")
        print("Processa/reprocessa uma folha primeiro (o profiling so grava a partir do R224).")
        return 1

    suspeitos = 0
    for rec in recs:
        print(f"\n=== Folha {rec.get('sheet_id')} | {rec.get('template')} | "
              f"{rec.get('n_rows')} linhas | {rec.get('trigger')} ===")
        for row in (rec.get("scoring") or []):
            decs = row.get("decisions") or []
            if args.so_trocas:
                decs = [d for d in decs
                        if str(d.get("ocr_value") or "").strip()
                        != str(d.get("final_value") or "").strip()]
            if not decs:
                continue
            win = row.get("winner_of")
            mode = row.get("winner_mode") or "-"
            win_txt = win if win not in (None, "") else "(nenhuma)"
            print(f"\n  Linha {int(row.get('row_index', 0)) + 1}  "
                  f"(cross escolheu OF {win_txt}, confianca: {mode})")
            print(f"    {'CAMPO':<9} | {'OCR leu':<20} | {'CROSS escolheu':<34} | ESTADO")
            print(f"    {'-'*9}-+-{'-'*20}-+-{'-'*34}-+-{'-'*12}")
            for d in decs:
                est = _estado(d)
                if "NADA A VER" in est:
                    suspeitos += 1
                print(f"    {str(d.get('field','')):<9} | "
                      f"{_trunc(_show(d.get('ocr_value')), 20):<20} | "
                      f"{_trunc(_show(d.get('final_value')), 34):<34} | {est}")

    print(f"\n>>> Campos suspeitos (TROCOU por valor do plano muito diferente): {suspeitos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
