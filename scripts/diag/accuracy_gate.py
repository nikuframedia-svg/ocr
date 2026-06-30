#!/usr/bin/env python3
"""Gate de aceitacao: compara dois relatorios do accuracy_eval (antes/depois).

Imprime PASS/FAIL com as clausulas que falharam. Decide se a lei nova e um
ganho liquido SEM precisar de palpite. Correr SEMPRE sobre a MESMA amostra
(use --sheets-file no accuracy_eval para fixar os ids).

Clausulas (todas tem de passar):
  1. wrong_auto_overwrite_per_1k (global) cai >= FATOR (default 50%).
  2. good_correction_recall (global) cai <= 0.05 absoluto.
  3. classes perigosas (dims_only/of_only/ov_only/cliente_only/modelo_only):
     identity_subst_precision nao regride E identity_subst nao aumenta.
  4. substitution_precision (global) nao regride.
  5. substitution_coverage (global) >= 0.7 x antes.
  6. recall por template dim-heavy (laser/guilhotina/bobine_formato) cai <= 0.05.

Uso:
    .venv\\Scripts\\python.exe scripts\\diag\\accuracy_gate.py before.json after.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_DANGEROUS = ("dims_only", "of_only", "ov_only", "cliente_only", "modelo_only")
_DIM_HEAVY = ("laser", "guilhotina", "bobine_formato")


def _g(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate de aceitacao antes/depois.")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--harm-drop", type=float, default=0.5,
                    help="fracao maxima de wrong_AO/1k que o 'depois' pode ter "
                         "(default 0.5 = corte de 50%%)")
    ap.add_argument("--recall-tol", type=float, default=0.05)
    args = ap.parse_args()

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))

    fails: list[str] = []
    notes: list[str] = []

    def num(x, d=0.0):
        return d if x is None else float(x)

    # 1. dano cai
    b_harm = _g(before, "global", "wrong_auto_overwrite_per_1k")
    a_harm = _g(after, "global", "wrong_auto_overwrite_per_1k")
    if b_harm is None:
        notes.append("clausula 1: sem dano no baseline (denominador 0) - ignorada")
    elif num(a_harm) > args.harm_drop * num(b_harm) + 1e-9:
        fails.append(f"1) wrong_AO/1k {a_harm} > {args.harm_drop}x{b_harm}")

    # 2. recall global nao desaba
    b_rec = _g(before, "global", "good_correction_recall")
    a_rec = _g(after, "global", "good_correction_recall")
    if b_rec is not None and a_rec is not None and num(a_rec) < num(b_rec) - args.recall_tol:
        fails.append(f"2) good_correction_recall {a_rec} < {b_rec} - {args.recall_tol}")

    # 3. classes perigosas: precisao de identidade nao regride E substitui menos identidade
    for cls in _DANGEROUS:
        b_prec = _g(before, "by_anchor", cls, "identity_subst_precision")
        a_prec = _g(after, "by_anchor", cls, "identity_subst_precision")
        b_ids = _g(before, "by_anchor", cls, "identity_subst")
        a_ids = _g(after, "by_anchor", cls, "identity_subst")
        if b_ids is None and a_ids is None:
            continue
        if b_prec is not None and a_prec is not None and num(a_prec) < num(b_prec) - 1e-9:
            fails.append(f"3) {cls}: identity_subst_precision regrediu {b_prec}->{a_prec}")
        if b_ids is not None and a_ids is not None and num(a_ids) > num(b_ids) + 1e-9:
            fails.append(f"3) {cls}: substitui MAIS identidade {b_ids}->{a_ids}")

    # 4. precisao global de substituicao nao regride
    b_sp = _g(before, "global", "substitution_precision")
    a_sp = _g(after, "global", "substitution_precision")
    if b_sp is not None and a_sp is not None and num(a_sp) < num(b_sp) - 1e-9:
        fails.append(f"4) substitution_precision regrediu {b_sp}->{a_sp}")

    # 5. coverage nao desaba (trava o 'ganho' trivial de nao substituir nada)
    b_cov = _g(before, "global", "substitution_coverage")
    a_cov = _g(after, "global", "substitution_coverage")
    if b_cov is not None and a_cov is not None and num(a_cov) < 0.7 * num(b_cov) - 1e-9:
        fails.append(f"5) substitution_coverage {a_cov} < 0.7x{b_cov}")

    # 6. recall por template dim-heavy
    for tpl in _DIM_HEAVY:
        b_t = _g(before, "by_template", tpl, "good_correction_recall")
        a_t = _g(after, "by_template", tpl, "good_correction_recall")
        if b_t is not None and a_t is not None and num(a_t) < num(b_t) - args.recall_tol:
            fails.append(f"6) template {tpl}: recall {a_t} < {b_t} - {args.recall_tol}")

    print(f"\n=== ACCURACY GATE  {Path(args.before).name} -> {Path(args.after).name} ===")
    print(f"  before: {_g(before,'engine_tag')}  after: {_g(after,'engine_tag')}")
    print(f"  wrong_AO/1k  {b_harm} -> {a_harm}")
    print(f"  good_recall  {b_rec} -> {a_rec}")
    print(f"  subst_prec   {b_sp} -> {a_sp}")
    print(f"  coverage     {b_cov} -> {a_cov}")
    for n in notes:
        print(f"  nota: {n}")
    if fails:
        print("\nFAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("\nPASS: a lei nova e um ganho liquido na amostra.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
