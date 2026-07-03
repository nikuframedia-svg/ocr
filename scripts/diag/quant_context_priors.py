"""R242 — Medições dos priors de CONTEXTO (quant5 + quant6), do app.db.

quant5 · Coerência de FOLHA: numa folha multi-linha, com que força as linhas
partilham cliente/OF? (calibra o passe 2 — a folha é uma conversa).

quant6 · Prior de PRODUÇÃO: a linha certa costuma ser uma OF com atividade
recente? w = log2(P(ativa | OF verdadeira) / P(ativa | OF aleatória do
plano)) — cap ±2 bits no motor (o prior nunca decide sozinho).

Uso:
    uv run python scripts/diag/quant_context_priors.py \
        --db ~/Downloads/auditoria_humana/app.db \
        --plan "~/Downloads/plan_colunas_cpis (8).xlsx" \
        [--window-days 14] [--merge]  # --merge grava em lexicons/cross_params.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _norm_of(v: object) -> str:
    s = re.sub(r"\D", "", str(v or ""))
    return (s.zfill(6) if len(s) <= 6 else s) if s else ""


def _iso(v: object) -> date | None:
    s = str(v or "")[:10]
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    # lixo pontual (ex.: 2096) fora
    return d if date(2025, 1, 1) <= d <= date(2027, 12, 31) else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db.expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---------------- quant5: coerência de folha ----------------
    # A estatística CERTA é o LIFT vs pares aleatórios (não a taxa absoluta —
    # as folhas são mistas: só 18% têm cliente único; mas partilhar OF/cliente
    # com a linha ADJACENTE é 21x/8x mais provável do que ao acaso).
    from collections import Counter

    adj_cli = same_cli = adj_of_n = same_of = 0
    all_cli: Counter = Counter()
    all_of: Counter = Counter()
    for s in con.execute(
        "SELECT sheet_data FROM sheets WHERE status='validated'"
    ):
        try:
            fin = json.loads(s["sheet_data"] or "{}")
        except (TypeError, ValueError):
            continue
        rows = [r for r in (fin.get("rows") or [])
                if any(str(v or "").strip() for v in (r or {}).values())]
        clis = [str((r or {}).get("cliente") or "").strip().upper() for r in rows]
        ofs = [_norm_of((r or {}).get("of")) for r in rows]
        for c in clis:
            if c:
                all_cli[c] += 1
        for o in ofs:
            if o:
                all_of[o] += 1
        for a, b in zip(clis, clis[1:]):
            if a and b:
                adj_cli += 1
                same_cli += a == b
        for a, b in zip(ofs, ofs[1:]):
            if a and b:
                adj_of_n += 1
                same_of += a == b

    def _pcol(c: Counter) -> float:
        t = sum(c.values())
        return sum((n / t) ** 2 for n in c.values()) if t else 1.0

    p_adj_cli = (same_cli + 1) / (adj_cli + 2)
    p_adj_of = (same_of + 1) / (adj_of_n + 2)
    quant5 = {
        "pares_adjacentes_cliente": adj_cli, "mesmo_cliente": same_cli,
        "p_mesmo_cliente_adjacente": round(p_adj_cli, 3),
        "p_mesmo_cliente_aleatorio": round(_pcol(all_cli), 3),
        "pares_adjacentes_of": adj_of_n, "mesma_of": same_of,
        "p_mesma_of_adjacente": round(p_adj_of, 3),
        "p_mesma_of_aleatorio": round(_pcol(all_of), 4),
        # bits de coerência = log2(lift), cap ±2 aplicado no MOTOR
        "coherence_cliente_bits": round(
            math.log2(p_adj_cli / max(_pcol(all_cli), 1e-6)), 2),
        "coherence_of_bits": round(
            math.log2(p_adj_of / max(_pcol(all_of), 1e-6)), 2),
    }

    # ---------------- quant6: prior de produção ----------------
    # Atividade: OF com produção VALIDADA no intervalo [D-K, D-1].
    act_by_of: dict[str, list[date]] = defaultdict(list)
    for r in con.execute(
        "SELECT of, sheet_iso_date FROM production_rows "
        "WHERE sheet_status='validated' AND of IS NOT NULL"
    ):
        o, d = _norm_of(r["of"]), _iso(r["sheet_iso_date"])
        if o and d:
            act_by_of[o].append(d)
    for o in act_by_of:
        act_by_of[o].sort()

    def active(of_key: str, d: date) -> bool:
        lo = d - timedelta(days=args.window_days)
        return any(lo <= x < d for x in act_by_of.get(of_key, ()))

    import openpyxl

    wb = openpyxl.load_workbook(args.plan.expanduser(), read_only=True,
                                data_only=True)
    ws = wb["plan_colunas_cpis"] if "plan_colunas_cpis" in wb.sheetnames else wb.active
    it = ws.iter_rows(values_only=True)
    hdr = [str(h or "").lower() for h in next(it)]
    iof = hdr.index("of")
    plan_ofs = sorted({_norm_of(r[iof]) for r in it if _norm_of(r[iof])})

    n = true_act = 0
    rand_act = rand_n = 0
    for r in con.execute(
        "SELECT sheet_id, of, sheet_iso_date FROM production_rows "
        "WHERE sheet_status='validated' AND of IS NOT NULL ORDER BY id"
    ):
        o, d = _norm_of(r["of"]), _iso(r["sheet_iso_date"])
        if not o or not d:
            continue
        n += 1
        true_act += active(o, d)
        # contraste: 3 OFs "aleatórias" determinísticas do plano
        base = (r["sheet_id"] * 31 + n) % max(len(plan_ofs) - 3, 1)
        for k in range(3):
            rand_n += 1
            rand_act += active(plan_ofs[(base + k * 977) % len(plan_ofs)], d)
    p_true = (true_act + 1) / (n + 2)
    p_rand = (rand_act + 1) / (rand_n + 2)
    quant6 = {
        "window_days": args.window_days,
        "n_linhas": n, "ativa_quando_verdadeira": true_act,
        "p_ativa_true": round(p_true, 3),
        "n_controlo": rand_n, "ativa_controlo": rand_act,
        "p_ativa_random": round(p_rand, 3),
        "production_prior_bits": round(
            max(-2.0, min(2.0, math.log2(p_true / p_rand))), 2),
        "production_prior_inactive_bits": round(
            max(-2.0, min(2.0, math.log2((1 - p_true) / (1 - p_rand)))), 2),
    }

    out = {"quant5_sheet_coherence": quant5, "quant6_production_prior": quant6}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if args.merge:
        path = _REPO / "lexicons" / "cross_params.json"
        params = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        params.update(out)
        path.write_text(json.dumps(params, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        print(f"merged -> {path}")


if __name__ == "__main__":
    main()
