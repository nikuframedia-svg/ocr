"""R253/F2 — SPRT de Wald sobre o soak da variante "next" (produção vs sombra).

O critério clássico ("≥300 folhas, divergência ≤2%") é um teste de amostra
fixa sem α/β declarados. Este script formaliza-o como DOIS braços SPRT sobre
a divergência POR CÉLULA (a mesma unidade do shadow_agreement.py):

  braço GERAL      H0: p=0.02  H1: p=0.06   (todas as células cruzáveis)
  braço IDENTIDADE H0: p=0.005 H1: p=0.02   (of/ov/cliente — consequência
                                             de auditoria EN1090/ISO9001)

α=0.05 (falso abort — custo: repetir o soak), β=0.01 (falso accept — custo:
variante má em produção certificável; tem de ser MUITO pequeno).

Fronteiras de Wald:  A = ln((1-β)/α) ≈ 2.986  → cruzar p/ CIMA = ABORT
                     B = ln(β/(1-α)) ≈ -4.554 → cruzar p/ BAIXO = ACCEPT
S_n = Σ llr(x_i);  llr(1)=ln(p1/p0), llr(0)=ln((1-p1)/(1-p0)).

FLIP só quando: ACCEPT nos DOIS braços ∧ ≥300 folhas ∧ todas as divergências
triadas via /sheet/<id>/shadow-view (o SPRT dá o abort RÁPIDO — E[N|H0]≈241
células ≈ ~25 folhas; o piso de 300 folhas protege a cobertura de casos
raros: MODEL_SIB, OOD).

Aborts imediatos (não-estatísticos):
  - shadow_runs com status='error' em >1% das folhas do soak;
  - linha "GOOD-proxy" divergente: folha validada onde o operador escreveu a
    OF certa (raw==final) mas a sombra discorda da produção nessa célula —
    é a garantia GOOD 110/110 do gate offline verificada em produção real.

Uso:
    uv run python scripts/diag/soak_sprt.py --db data/app.db \
        [--since-id 0] [--out-dir reports/soak_sprt]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))

# A produção vive no storage por ficheiros (kanban_refs/03_Cross_Check),
# NÃO numa coluna sheets.cross_check — mesma régua do shadow_agreement.
from app.cross_check.storage import load_sheet_cross_check

_CROSSABLE = ("of", "ov", "cliente", "modelo", "lote",
              "comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo")
_IDENTITY = ("of", "ov", "cliente")

# Parâmetros do teste (propostos ao Luís; ver docstring).
ALPHA, BETA = 0.05, 0.01
ARMS = {
    "geral": {"p0": 0.02, "p1": 0.06},
    "identidade": {"p0": 0.005, "p1": 0.02},
}
MIN_SHEETS_FOR_ACCEPT = 300
MAX_ERROR_RATE = 0.01


def wald_bounds(alpha: float = ALPHA, beta: float = BETA) -> tuple[float, float]:
    """(A, B) — fronteiras de log-verosimilhança de Wald."""
    return math.log((1 - beta) / alpha), math.log(beta / (1 - alpha))


def llr(x: int, p0: float, p1: float) -> float:
    return math.log(p1 / p0) if x else math.log((1 - p1) / (1 - p0))


def _norm_of(v: object) -> str:
    s = "".join(c for c in str(v or "") if c.isalnum()).upper().replace("O", "0")
    return s.zfill(6) if s.isdigit() and 0 < len(s) <= 6 else s


def run(db: Path, since_id: int) -> dict:
    con = sqlite3.connect(f"file:{db.expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    arms = {
        name: {"s": 0.0, "n": 0, "diffs": 0, "trajectory": [],
               "decision": "a decorrer", **cfg}
        for name, cfg in ARMS.items()
    }
    a_bound, b_bound = wald_bounds()
    n_sheets = 0
    good_proxy_violations: list[dict] = []
    durations: list[float] = []

    for s in con.execute(
        "SELECT id, shadow_scoring_json, sheet_data, "
        "raw_extraction, status FROM sheets "
        "WHERE id >= ? AND shadow_scoring_json IS NOT NULL ORDER BY id",
        (since_id,),
    ):
        try:
            shad = json.loads(s["shadow_scoring_json"] or "{}")
        except (TypeError, ValueError):
            continue
        prod = load_sheet_cross_check(int(s["id"]), include_stale=True) or {}
        p_rows = prod.get("rows") or []
        s_rows = shad.get("rows") or []
        if not p_rows or not s_rows:
            continue
        n_sheets += 1
        # GOOD-proxy: linhas validadas onde o operador escreveu a OF certa.
        good_rows: set[int] = set()
        if s["status"] == "validated":
            try:
                raw = json.loads(s["raw_extraction"] or "{}").get("rows") or []
                fin = json.loads(s["sheet_data"] or "{}").get("rows") or []
                for i in range(min(len(raw), len(fin))):
                    r_of = _norm_of((raw[i] or {}).get("of"))
                    f_of = _norm_of((fin[i] or {}).get("of"))
                    if r_of and r_of == f_of:
                        good_rows.add(i)
            except (TypeError, ValueError):
                pass
        for i in range(min(len(p_rows), len(s_rows))):
            pf = (p_rows[i] or {}).get("fields") or {}
            sf = (s_rows[i] or {}).get("fields") or {}
            for field in _CROSSABLE:
                pc, sc = pf.get(field) or {}, sf.get(field) or {}
                pv = str(pc.get("value") or pc.get("ref") or "").strip()
                sv = str(sc.get("value") or "").strip()
                if not pv and not sv:
                    continue
                x = int(pv.upper() != sv.upper())
                for name, arm in arms.items():
                    if name == "identidade" and field not in _IDENTITY:
                        continue
                    if arm["decision"] != "a decorrer":
                        continue  # congela no cruzamento (SPRT clássico)
                    arm["n"] += 1
                    arm["diffs"] += x
                    arm["s"] += llr(x, arm["p0"], arm["p1"])
                    arm["trajectory"].append(round(arm["s"], 4))
                    if arm["s"] >= a_bound:
                        arm["decision"] = "ABORT"
                    elif arm["s"] <= b_bound:
                        arm["decision"] = "ACCEPT"
                if x and field == "of" and i in good_rows:
                    good_proxy_violations.append(
                        {"sheet_id": s["id"], "row": i,
                         "producao": pv[:40], "sombra": sv[:40]})

    # Falhas de execução da sombra (tabela shadow_runs, se existir).
    n_err = n_runs = 0
    try:
        for r in con.execute(
            "SELECT status, duration_ms FROM shadow_runs "
            "WHERE sheet_id >= ?", (since_id,),
        ):
            n_runs += 1
            n_err += r["status"] == "error"
            if r["duration_ms"]:
                durations.append(float(r["duration_ms"]))
    except sqlite3.OperationalError:
        pass

    err_rate = (n_err / n_runs) if n_runs else 0.0
    aborts: list[str] = []
    if err_rate > MAX_ERROR_RATE:
        aborts.append(
            f"shadow_runs error rate {err_rate:.1%} > {MAX_ERROR_RATE:.0%}")
    if good_proxy_violations:
        aborts.append(
            f"{len(good_proxy_violations)} divergência(s) GOOD-proxy "
            "(OF escrita certa, sombra discorda da produção)")
    for name, arm in arms.items():
        if arm["decision"] == "ABORT":
            aborts.append(f"SPRT braço {name} cruzou A (divergência acima "
                          f"do aceitável: {arm['diffs']}/{arm['n']})")

    all_accept = all(a["decision"] == "ACCEPT" for a in arms.values())
    if aborts:
        status = "ABORT"
    elif all_accept and n_sheets >= MIN_SHEETS_FOR_ACCEPT:
        status = "OK PARA FLIP (falta só a triagem manual via shadow-view)"
    elif all_accept:
        status = (f"ACCEPT nos 2 braços — falta o piso de folhas "
                  f"({n_sheets}/{MIN_SHEETS_FOR_ACCEPT})")
    else:
        status = "a decorrer"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db), "since_id": since_id,
        "alpha": ALPHA, "beta": BETA,
        "bounds": {"A_abort": round(a_bound, 4), "B_accept": round(b_bound, 4)},
        "n_sheets": n_sheets,
        "shadow_runs": {"n": n_runs, "errors": n_err,
                        "error_rate": round(err_rate, 4),
                        "duration_ms_median": (
                            round(statistics.median(durations), 1)
                            if durations else None)},
        "arms": {
            name: {k: v for k, v in arm.items() if k != "trajectory"}
            for name, arm in arms.items()
        },
        "trajectories": {name: arm["trajectory"] for name, arm in arms.items()},
        "good_proxy_violations": good_proxy_violations,
        "aborts": aborts,
        "status": status,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--since-id", type=int, default=0)
    ap.add_argument("--out-dir", type=Path, default=Path("reports/soak_sprt"))
    args = ap.parse_args()

    state = run(args.db, args.since_id)
    stamp = state["generated_at"].replace(":", "").replace("-", "")[:15]
    out = args.out_dir / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"folhas com sombra: {state['n_sheets']} | "
          f"shadow_runs: {state['shadow_runs']['n']} "
          f"({state['shadow_runs']['errors']} erros)")
    for name, arm in state["arms"].items():
        print(f"braço {name}: S={arm['s']:.3f} "
              f"[{state['bounds']['B_accept']}, {state['bounds']['A_abort']}] "
              f"| células={arm['n']} divergentes={arm['diffs']} "
              f"→ {arm['decision']}")
    for a in state["aborts"]:
        print(f"ABORT: {a}")
    print(f"estado: {state['status']}")
    print(f"trajetória completa: {out / 'state.json'}")


if __name__ == "__main__":
    main()
