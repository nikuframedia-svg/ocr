"""R253/F2 — análise de sensibilidade paramétrica do cross engine.

Perturba cada constante fitted de lexicons/cross_params.json em ±pct (default
20%) e corre o backtest_winner com o pacote fixo, medindo o efeito nos gates.
Critério de robustez (pré-flip): NENHUMA perturbação de ±20% pode mudar o
SINAL de gate.passed — se mudar, o gate está "on the edge" de um parâmetro
com incerteza de amostra e o flip não é seguro só porque o ponto central
passa.

Mecânica: o motor lê cross_params.json do path do repo (lru_cache) — cada
variante ESCREVE temporariamente o ficheiro perturbado e corre o backtest em
SUBPROCESSO (isolamento total de caches); o original é restaurado em finally,
byte-idêntico, mesmo com Ctrl-C.

Uso (caps reduzidos p/ uma varredura ~2h; sem caps é uma corrida overnight):
    uv run python scripts/diag/sensitivity_analysis.py \
        --db ~/Downloads/auditoria_humana/app.db \
        --plan "~/Downloads/plan_colunas_cpis (8).xlsx" \
        --sap "~/Downloads/StockSAP_Dinamico (5).xlsx" \
        [--pct 0.2] [--variant next] [--good-cap 60 --eng-cap 30]
        [--params calibration.temperature_bits,...]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PARAMS_PATH = _REPO / "lexicons" / "cross_params.json"

# Escalar → path no JSON (dot-notation). Ordem = prioridade de leitura.
_DEFAULT_PARAMS = (
    "calibration.temperature_bits",
    "calibration.s_ood_bits",
    "calibration.posterior_temperature_bits",
    "calibration.b_h0_raw_bits",
    "calibration.posterior_floor_a_bits",
    "quant6_production_prior.production_prior_bits",
    "quant6_production_prior.production_prior_inactive_bits",
    "quant7_ood_by_age.buckets.0-3.p_ood_isotonic",
    "quant7_ood_by_age.buckets.>30.p_ood_isotonic",
    "quant8_identity_joint.m_joint.of.m",
    "quant8_identity_joint.m_joint.of+ov+cliente.m",
    "char_channel.cost_default_bits",
    "char_channel.cost_indel_bits",
    "model_token_lr.oov_alpha",
)


def _get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set(d: dict, path: str, value) -> None:
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value


def _run_backtest(args, tag: str) -> dict | None:
    out_dir = args.out_dir / tag
    env = dict(os.environ)
    env["CROSS_SCORING_VARIANT"] = args.variant
    cmd = [
        "uv", "run", "python", "scripts/diag/backtest_winner.py",
        "--db", str(args.db), "--plan", str(args.plan),
        "--sap", str(args.sap), "--baseline-ref", args.baseline_ref,
        "--good-cap", str(args.good_cap), "--eng-cap", str(args.eng_cap),
        "--model-cap", str(args.model_cap), "--out-dir", str(out_dir),
    ]
    proc = subprocess.run(cmd, cwd=_REPO, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  ERRO {tag}: {proc.stderr[-400:]}", file=sys.stderr)
        return None
    try:
        return json.loads((out_dir / "summary.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--sap", required=True, type=Path)
    ap.add_argument("--baseline-ref", default="12476cf")
    ap.add_argument("--variant", default="next", choices=("v30", "next"))
    ap.add_argument("--pct", type=float, default=0.20)
    ap.add_argument("--good-cap", type=int, default=110)
    ap.add_argument("--eng-cap", type=int, default=70)
    ap.add_argument("--model-cap", type=int, default=150)
    ap.add_argument("--params", default=None,
                    help="lista dot-notation separada por vírgulas")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out_dir = args.out_dir or Path("reports/sensitivity_analysis") / stamp
    args.out_dir.mkdir(parents=True, exist_ok=True)

    original = _PARAMS_PATH.read_bytes()
    base_params = json.loads(original.decode("utf-8"))
    wanted = (args.params.split(",") if args.params else _DEFAULT_PARAMS)
    targets = [(p, _get(base_params, p)) for p in wanted]
    targets = [(p, v) for p, v in targets
               if isinstance(v, (int, float)) and v is not None]
    print(f"{len(targets)} parâmetros × 2 sinais + 1 baseline "
          f"(pct=±{args.pct:.0%}, variant={args.variant})")

    rows: list[dict] = []
    try:
        base_sum = _run_backtest(args, "baseline")
        if base_sum is None:
            sys.exit("baseline falhou — abortar")
        base_total = float(base_sum["total"]["candidate_pct"] or 0)
        base_gate = bool(base_sum["gate"]["passed"])
        print(f"baseline: TOTAL {base_total}% gate={base_gate}")
        for path, val in targets:
            for sign in (+1, -1):
                pert = json.loads(original.decode("utf-8"))
                new_val = val * (1 + sign * args.pct)
                _set(pert, path, new_val)
                _PARAMS_PATH.write_text(
                    json.dumps(pert, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
                tag = f"{path.replace('.', '_')}_{'up' if sign > 0 else 'dn'}"
                s = _run_backtest(args, tag)
                if s is None:
                    continue
                row = {
                    "param": path, "sign": "+" if sign > 0 else "-",
                    "value": round(new_val, 4), "base_value": val,
                    "total_pct": s["total"]["candidate_pct"],
                    "d_total": round(float(s["total"]["candidate_pct"] or 0)
                                     - base_total, 2),
                    "good_ok": s["sets"]["GOOD"]["candidate_ok"],
                    "shift_pct": s["sets"]["SHIFT"]["candidate_pct"],
                    "gate_passed": s["gate"]["passed"],
                    "gate_flip": s["gate"]["passed"] != base_gate,
                }
                rows.append(row)
                print(f"  {tag}: TOTAL {row['total_pct']}% "
                      f"(Δ{row['d_total']:+.2f}) gate={row['gate_passed']}"
                      f"{'  <-- MUDA O SINAL DO GATE' if row['gate_flip'] else ''}")
    finally:
        _PARAMS_PATH.write_bytes(original)  # restauro byte-idêntico
        print("cross_params.json restaurado.")

    rows.sort(key=lambda r: -abs(r["d_total"]))
    out_csv = args.out_dir / "summary.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["param"])
        w.writeheader()
        w.writerows(rows)
    flips = [r for r in rows if r["gate_flip"]]
    print(f"\n{len(rows)} corridas → {out_csv}")
    if flips:
        print(f"⚠ {len(flips)} perturbações MUDAM o sinal do gate — o flip "
              "não é robusto a ±20% nesses parâmetros:")
        for r in flips:
            print(f"  {r['param']} {r['sign']}{args.pct:.0%}")
    else:
        print("✅ nenhum flip de gate — robusto a ±20% nos parâmetros testados.")


if __name__ == "__main__":
    main()
