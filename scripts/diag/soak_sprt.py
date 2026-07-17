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
    é a garantia GOOD 110/110 do gate offline verificada em produção real;
  - dv (voto declarado, soak da feature "+dv"): INVARIANTE DO CAP — um flip
    de winner (winner_of prod != sombra) numa linha cuja margem de produção
    é > 2.0 bits é impossível para um termo one-sided capado a 2.0; se
    acontecer, o cap está furado (bug) → ABORT imediato.

Braço de UTILIDADE do dv (relatório, não SPRT): flips de winner em folhas
depois validadas — de que lado ficou a verdade humana (OF final). O gate do
flip exige melhorias >= 2× pioras com >= 10 flips decidíveis; senão o
veredito é "SAFE, utilidade não provada" (fica em shadow).

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
# dv — cap do voto declarado (espelho de _DECLARED_VOTE_CAP_BITS) + epsilon
# de arredondamento (as margens gravadas vêm com round(2)).
DV_CAP_BITS = 2.0
DV_CAP_EPS = 0.01
DV_MIN_DECIDABLE_FLIPS = 10


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
    # dv — flips de winner + invariante do cap + braço de utilidade
    winner_flips: list[dict] = []
    dv_cap_violations: list[dict] = []
    dv_util = {"sombra_certa": 0, "prod_certa": 0, "indecidivel": 0}
    n_sheets_with_dv = 0

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
        if any((r or {}).get("winner_declared_vote") for r in s_rows):
            n_sheets_with_dv += 1
        # GOOD-proxy: linhas validadas onde o operador escreveu a OF certa.
        good_rows: set[int] = set()
        fin_rows: list = []
        if s["status"] == "validated":
            try:
                raw = json.loads(s["raw_extraction"] or "{}").get("rows") or []
                fin_rows = json.loads(s["sheet_data"] or "{}").get("rows") or []
                for i in range(min(len(raw), len(fin_rows))):
                    r_of = _norm_of((raw[i] or {}).get("of"))
                    f_of = _norm_of((fin_rows[i] or {}).get("of"))
                    if r_of and r_of == f_of:
                        good_rows.add(i)
            except (TypeError, ValueError):
                fin_rows = []
        # dv — flips de winner por linha: invariante do cap + utilidade.
        for i in range(min(len(p_rows), len(s_rows))):
            p_of = str((p_rows[i] or {}).get("winner_of") or "")
            s_of = str((s_rows[i] or {}).get("winner_of") or "")
            if not p_of and not s_of:
                continue
            if p_of == s_of:
                continue
            prod_margin = (p_rows[i] or {}).get("winner_margin_bits")
            dv_bits = (s_rows[i] or {}).get("winner_declared_vote_bits")
            flip = {"sheet_id": int(s["id"]), "row": i,
                    "prod_of": p_of, "sombra_of": s_of,
                    "prod_margin_bits": prod_margin,
                    "sombra_dv_bits": dv_bits}
            # verdade humana (folha validada): de que lado ficou a OF final?
            truth_of = ""
            if i < len(fin_rows):
                truth_of = _norm_of((fin_rows[i] or {}).get("of"))
            if truth_of and _norm_of(s_of) == truth_of != _norm_of(p_of):
                flip["verdade"] = "sombra_certa"
                dv_util["sombra_certa"] += 1
            elif truth_of and _norm_of(p_of) == truth_of != _norm_of(s_of):
                flip["verdade"] = "prod_certa"
                dv_util["prod_certa"] += 1
            else:
                flip["verdade"] = "indecidivel"
                dv_util["indecidivel"] += 1
            winner_flips.append(flip)
            # invariante do cap: só acusável quando a sombra VOTOU nesta
            # linha (dv_bits > 0) e a margem de produção está gravada.
            if (dv_bits and float(dv_bits) > 0.0
                    and prod_margin is not None
                    and float(prod_margin) > DV_CAP_BITS + DV_CAP_EPS):
                dv_cap_violations.append(flip)
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
    if dv_cap_violations:
        aborts.append(
            f"dv: {len(dv_cap_violations)} flip(s) de winner com margem de "
            f"produção > {DV_CAP_BITS} bits — o cap do voto declarado está "
            "furado (bug), não é decisão estatística")
    for name, arm in arms.items():
        if arm["decision"] == "ABORT":
            aborts.append(f"SPRT braço {name} cruzou A (divergência acima "
                          f"do aceitável: {arm['diffs']}/{arm['n']})")

    # dv — veredito do braço de utilidade (gate do flip, não SPRT).
    n_decidable = dv_util["sombra_certa"] + dv_util["prod_certa"]
    if n_decidable >= DV_MIN_DECIDABLE_FLIPS:
        dv_verdict = (
            "UTILIDADE PROVADA (melhorias >= 2x pioras)"
            if dv_util["sombra_certa"] >= 2 * dv_util["prod_certa"]
            else "UTILIDADE NEGATIVA — não flipar"
        )
    elif winner_flips:
        dv_verdict = (f"SAFE, utilidade NÃO provada "
                      f"({n_decidable}/{DV_MIN_DECIDABLE_FLIPS} flips "
                      "decidíveis) — continuar em shadow")
    else:
        dv_verdict = "sem flips de winner no soak"

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
        # dv — soak do voto declarado (feature "+dv")
        "dv": {
            "n_sheets_with_vote": n_sheets_with_dv,
            "winner_flips": winner_flips,
            "cap_violations": dv_cap_violations,
            "utility": dv_util,
            "verdict": dv_verdict,
        },
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
    dv = state["dv"]
    if dv["winner_flips"] or dv["n_sheets_with_vote"]:
        u = dv["utility"]
        print(f"dv: folhas c/ voto={dv['n_sheets_with_vote']} | "
              f"flips de winner={len(dv['winner_flips'])} "
              f"(sombra certa {u['sombra_certa']} / prod certa "
              f"{u['prod_certa']} / indecidíveis {u['indecidivel']}) "
              f"→ {dv['verdict']}")
    for a in state["aborts"]:
        print(f"ABORT: {a}")
    print(f"estado: {state['status']}")
    print(f"trajetória completa: {out / 'state.json'}")


if __name__ == "__main__":
    main()
