#!/usr/bin/env python3
"""R229 -- OCR vs CROSS, limpo + deteta troca estupida.

Para cada folha e cada linha, mostra campo a campo o que o OCR LEU e o que o
CROSS ESCOLHEU, e classifica a mudanca pela RELACAO entre os dois valores:
  = igual                 -> o cross manteve o que o OCR leu
  ok corrigiu/expandiu    -> mesma coisa, so corrigida/expandida (ex.: CB04E63D
                             -> CBO4E63D - 2 SC; ou MTG BELUX -> STOCK MTG BELUX)
  + preencheu             -> campo vazio preenchido pelo plano
  ?? parecido (verificar) -> mudou para algo SO parecido (confirmar a olho)
  XX NADA A VER           -> trocou por um valor SEM relacao = a troca estupida
Le o data/_logs/profile.jsonl (igual ao profile_report), sem ruido.

Uso (na fabrica, com o python do .venv):
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --sheet 1952
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --last 50 --so-trocas
    .venv\\Scripts\\python.exe scripts\\diag\\ocr_vs_cross.py --last 50 --so-nada-a-ver
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOG = _REPO / "data" / "_logs" / "profile.jsonl"


def _read_records(log):
    recs = []
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


def _show(v):
    s = "" if v is None else str(v)
    return s if s.strip() else "(vazio)"


def _trunc(s, n):
    return s if len(s) <= n else s[: n - 3] + "..."


def _norm(s):
    # so alfanumerico, maiusculas, e desfaz confusoes comuns do OCR (O/0, I/L/1)
    s = "".join(ch for ch in str(s or "").upper() if ch.isalnum())
    return s.replace("O", "0").replace("I", "1").replace("L", "1")


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _relatedness(ocr, cross):
    """0..1 -- quao relacionado o valor do cross esta com o que o OCR leu."""
    a = _norm(ocr)
    if not a:
        return 1.0  # vazio -> preenchimento, nao e troca
    full = _norm(cross)
    if len(a) >= 3 and (a in full or full in a):
        return 1.0  # substring: MTG BELUX dentro de STOCK MTG BELUX, etc.
    cands = [full, _norm(str(cross or "").split(" - ")[0])]
    cands += [_norm(t) for t in str(cross or "").replace(" - ", " ").split()]
    return max((_ratio(a, c) for c in cands if c), default=0.0)


def _classify(d):
    ocr = str(d.get("ocr_value") or "").strip()
    fin = str(d.get("final_value") or "").strip()
    src = (d.get("source") or "").lower()
    if not fin and not ocr:
        return "vazio", "vazio"
    if _norm(ocr) == _norm(fin):
        return "igual", "= igual"
    if not ocr and fin:
        return "fill", "+ preencheu pelo plano"
    if src == "ocr_raw":
        return "rever", "~ manteve o OCR (p/ rever)"
    rel = _relatedness(ocr, fin)
    if rel >= 0.70:
        return "ok", "ok corrigiu/expandiu"
    if rel >= 0.45:
        return "dubio", "?? parecido (verificar)"
    return "nada", "XX NADA A VER (troca estupida)"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="OCR vs Cross por folha (R229).")
    ap.add_argument("--log", type=Path, default=_DEFAULT_LOG)
    ap.add_argument("--sheet", type=int, default=None, help="so esta folha")
    ap.add_argument("--last", type=int, default=None, help="ultimas N folhas")
    ap.add_argument("--so-trocas", action="store_true",
                    help="esconde os campos que ficaram iguais")
    ap.add_argument("--so-nada-a-ver", action="store_true",
                    help="mostra so as trocas estupidas (XX NADA A VER)")
    args = ap.parse_args()

    recs = _read_records(args.log)
    if args.sheet is not None:
        recs = [r for r in recs if r.get("sheet_id") == args.sheet]
    elif args.last is not None:
        recs = recs[-args.last:]
    if not recs:
        print("Sem registos em " + str(args.log) + ".")
        return 1

    tot = {"igual": 0, "ok": 0, "fill": 0, "rever": 0, "dubio": 0, "nada": 0, "vazio": 0}
    for rec in recs:
        printed = False
        for row in (rec.get("scoring") or []):
            decs = []
            for d in (row.get("decisions") or []):
                kind, label = _classify(d)
                tot[kind] = tot.get(kind, 0) + 1
                if args.so_nada_a_ver and kind != "nada":
                    continue
                if args.so_trocas and kind in ("igual", "vazio"):
                    continue
                decs.append((d, label))
            if not decs:
                continue
            if not printed:
                print("")
                print("=== Folha " + str(rec.get("sheet_id")) + " | "
                      + str(rec.get("template")) + " | " + str(rec.get("trigger")) + " ===")
                printed = True
            win = row.get("winner_of")
            mode = row.get("winner_mode") or "-"
            win_txt = win if win not in (None, "") else "(nenhuma)"
            print("")
            print("  Linha " + str(int(row.get("row_index", 0)) + 1)
                  + "  (cross escolheu OF " + str(win_txt) + ", confianca: " + str(mode) + ")")
            print("    " + "CAMPO".ljust(9) + " | " + "OCR leu".ljust(20) + " | "
                  + "CROSS escolheu".ljust(34) + " | ESTADO")
            print("    " + "-" * 9 + "-+-" + "-" * 20 + "-+-" + "-" * 34 + "-+-" + "-" * 14)
            for d, label in decs:
                print("    " + str(d.get("field", "")).ljust(9) + " | "
                      + _trunc(_show(d.get("ocr_value")), 20).ljust(20) + " | "
                      + _trunc(_show(d.get("final_value")), 34).ljust(34) + " | " + label)

    print("")
    print("=== RESUMO (todos os campos vistos) ===")
    print("  iguais ............... " + str(tot["igual"]))
    print("  ok corrigiu/expandiu . " + str(tot["ok"]))
    print("  preencheu vazio ...... " + str(tot["fill"]))
    print("  manteve OCR (rever) .. " + str(tot["rever"]))
    print("  ?? parecido (verif.) . " + str(tot["dubio"]))
    print("  XX NADA A VER ........ " + str(tot["nada"]) + "   <=== trocas estupidas a serio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
