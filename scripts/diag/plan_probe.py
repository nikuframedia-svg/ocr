#!/usr/bin/env python3
"""R230 -- sonda ao plano para as linhas weak_guess (ou suspeitas).

Junta TRES coisas por linha: (1) o que o OCR leu, (2) o que o cross escolheu (e
o que essa OF e no plano), e (3) o que daria PROCURAR o valor da coluna OF como
MODELO no plano. Assim ve-se se o resultado certo estava la (o cross falhou por
procurar no indice errado: numeros de OF em vez do indice de modelos) ou se a
linha e mesmo ilegivel (e devia manter o OCR + marcar para rever).

Le data/_logs/profile.jsonl (OCR + decisoes do cross) E carrega o plano vivo.

Uso (na fabrica, com o python do .venv):
    .venv\\Scripts\\python.exe scripts\\diag\\plan_probe.py            # so weak_guess
    .venv\\Scripts\\python.exe scripts\\diag\\plan_probe.py --sheet 1976
    .venv\\Scripts\\python.exe scripts\\diag\\plan_probe.py --mode all --last 60
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOG = _REPO / "data" / "_logs" / "profile.jsonl"
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))


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


def _has_letters(s):
    return any(c.isalpha() for c in str(s or ""))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Sonda ao plano (R230).")
    ap.add_argument("--log", type=Path, default=_DEFAULT_LOG)
    ap.add_argument("--sheet", type=int, default=None)
    ap.add_argument("--last", type=int, default=80)
    ap.add_argument("--mode", choices=["weak", "all"], default="weak")
    ap.add_argument("--forte", type=float, default=80.0,
                    help="limiar de similaridade (0-100) para considerar recuperavel")
    args = ap.parse_args()

    from app.cross_check.ref_watcher import get_watcher
    from app.pipeline import scoring_engine as se
    refs = get_watcher().get_refs()
    idx = se._get_indices(refs)
    of2e = idx["of_to_entries"]

    # pre-computa os 'heads' de modelo (1o pedaco da designacao) do plano todo
    heads = []
    for of, ents in of2e.items():
        e = ents[0]
        head = str(e.get("designacao") or "").split(" - ")[0].strip()
        if head:
            heads.append((of, e.get("cliente"), head, se._model_compact(head)))

    def search_model(code, k=4):
        cc = se._model_compact(code)
        if not cc:
            return []
        out = [(float(se._str_sim(cc, hc)), of, cli, head)
               for of, cli, head, hc in heads if hc]
        out.sort(reverse=True)
        return out[:k]

    def of_info(of):
        e = (of2e.get(str(of)) or [{}])[0]
        if not e:
            return "(OF nao existe no meu plano)"
        return str(e.get("cliente")) + " / " + str(e.get("designacao"))

    recs = _read_records(args.log)
    if args.sheet is not None:
        recs = [r for r in recs if r.get("sheet_id") == args.sheet]
    else:
        recs = recs[-args.last:]

    n = recup = fraco = 0
    for rec in recs:
        for row in (rec.get("scoring") or []):
            mode = row.get("winner_mode")
            if args.mode == "weak" and mode != "weak_guess":
                continue
            decs = {d.get("field"): d for d in (row.get("decisions") or [])}
            ocr_of = str((decs.get("of") or {}).get("ocr_value") or "").strip()
            ocr_mod = str((decs.get("modelo") or {}).get("ocr_value") or "").strip()
            ocr_cli = str((decs.get("cliente") or {}).get("ocr_value") or "").strip()
            win = row.get("winner_of")
            # interessa quando a coluna OF (ou modelo) tem um codigo com LETRAS
            probe = ocr_of if _has_letters(ocr_of) else (ocr_mod if _has_letters(ocr_mod) else "")
            if not probe:
                continue
            n += 1
            print("")
            print("=== Folha " + str(rec.get("sheet_id")) + " L"
                  + str(int(row.get("row_index", 0)) + 1) + "  (" + str(mode) + ") ===")
            print("  OCR leu:        of=" + repr(ocr_of) + "  modelo=" + repr(ocr_mod)
                  + "  cliente=" + repr(ocr_cli))
            print("  CROSS escolheu: OF " + str(win) + "  ->  " + of_info(win))
            print("  procurar " + repr(probe) + " como MODELO no plano:")
            best = search_model(probe)
            top = best[0][0] if best else 0.0
            for s, of, cli, head in best:
                print("     " + ("%5.1f" % s) + "  OF " + str(of) + "  "
                      + str(cli) + "  " + str(head))
            if top >= args.forte:
                recup += 1
                print("  >> RECUPERAVEL: o modelo certo estava no plano (sim "
                      + ("%.0f" % top) + ") -- o cross procurou no indice errado")
            else:
                fraco += 1
                print("  >> sem match forte (melhor " + ("%.0f" % top)
                      + ") -- ilegivel, devia manter o OCR + rever")

    print("")
    print("=== RESUMO (" + str(n) + " linhas com codigo na coluna OF) ===")
    print("  RECUPERAVEIS por modelo (cross devia ter acertado) : " + str(recup))
    print("  fracas/ilegiveis (devia manter OCR + rever) ....... : " + str(fraco))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
