#!/usr/bin/env python3
"""R233 -- re-corre o cross AO VIVO (motor atual) sobre o OCR CRU guardado de uma
folha, e mostra, por linha, o que o OCR leu e o que o cross escolhe AGORA.

Nao depende do profile.jsonl (que tem resultados antigos) nem de reprocessar pela
UI -- e a prova direta de que o motor faz o que deve, com o plano vivo.

Uso (na fabrica, com o python do .venv):
    .venv\\Scripts\\python.exe scripts\\diag\\recheck.py --sheet 1976
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Re-cross ao vivo de uma folha (R233).")
    ap.add_argument("--sheet", type=int, required=True)
    args = ap.parse_args()

    from app.web import db
    from app.cross_check.ref_watcher import get_watcher
    from app.pipeline import scoring_engine as se

    sheet = db.get_sheet(args.sheet)
    if not sheet:
        print("Folha nao encontrada: " + str(args.sheet))
        return 1
    raw = sheet.get("raw_extraction") or sheet.get("sheet_data")
    if not raw or not raw.get("rows"):
        print("Sem raw_extraction/sheet_data com linhas.")
        return 1

    refs = get_watcher().get_refs()
    idx = se._get_indices(refs)
    of2e = idx["of_to_entries"]

    def of_info(of):
        e = (of2e.get(str(of)) or [{}])[0]
        if not e:
            return "(OF nao esta no plano)"
        return str(e.get("cliente")) + " / " + str(e.get("designacao"))

    res = se.cross_check_sheet(raw, None, refs)
    tpl = raw.get("template_name") or sheet.get("template_name")
    print("=== Folha " + str(args.sheet) + " (" + str(tpl)
          + ") -- CROSS AO VIVO (motor " + str(se.ENGINE_VERSION) + ") ===")
    rows_in = raw.get("rows", [])
    for i, rout in enumerate(res.get("rows", [])):
        rin = rows_in[i] if i < len(rows_in) else {}
        win = rout.get("winner_of")
        ocr = ("of=" + repr(str(rin.get("of") or "")) + "  modelo="
               + repr(str(rin.get("modelo") or "")) + "  cliente="
               + repr(str(rin.get("cliente") or "")) + "  ov="
               + repr(str(rin.get("ov") or "")))
        print("")
        print("  Linha " + str(i + 1) + "  [" + str(rout.get("winner_mode")) + "]")
        print("    OCR leu:        " + ocr)
        print("    CROSS escolheu: OF " + str(win) + "  ->  " + of_info(win))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
