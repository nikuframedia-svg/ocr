"""R251 — Fit do LR de TOKEN do modelo (α OOV), do app.db + plano.

O ranking do modelo na variante "next" usa
    bits = log2( m_mod · 2^(−custo_NW(token→core)) / û(core) )
    û(core) = ( Σ_t 2^(−custo(t→core))·|entries(t)| + α ) / N
Este script (a) reporta a taxa de colisão base dos cores reais contra os
tokens do plano (o conteúdo informativo que fundamenta o LR), e (b) fita a
pseudo-contagem α maximizando a DISCRIMINAÇÃO DE IRMÃOS medida: nas linhas
validadas de OFs multi-irmã com modelo escrito e verdade decidível, a
fração em que a entry da verdade tem o maior LR entre os irmãos.

Uso:
    uv run python scripts/diag/fit_model_token_lr.py \
        --db ~/Downloads/auditoria_humana/app.db \
        --plan "~/Downloads/plan_colunas_cpis (8).xlsx" \
        --sap "~/Downloads/StockSAP_Dinamico (5).xlsx" [--merge]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))

_MODEL_CODE_TOKEN_RE = re.compile(r"[A-Z0-9]{4,}")


def _truth_consistent(truth: str, designacao: str) -> bool:
    """Mesma régua standalone do backtest (anti-circular)."""
    t = re.sub(r"[^A-Z0-9]+", " ", str(truth or "").upper()).strip()
    d = re.sub(r"[^A-Z0-9]+", "", str(designacao or "").upper())
    if not t or not d:
        return False
    tc = t.replace(" ", "")
    if tc == d or (len(tc) >= 5 and (tc in d or d in tc)):
        return True
    toks = [x for x in _MODEL_CODE_TOKEN_RE.findall(t)
            if any(c.isdigit() for c in x) and any(c.isalpha() for c in x)]
    return any(x in d for x in toks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--sap", required=True, type=Path)
    ap.add_argument("--alphas", type=str, default="0.1,0.25,0.5,1.0,2.0,4.0")
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()

    from app.cross_check.ref_watcher import _mine_from_excel
    import app.pipeline.scoring_engine as se

    refs = _mine_from_excel(args.sap.expanduser(), args.plan.expanduser())
    refs.setdefault("loaded_at", "fit_model_token_lr")
    idx = se._get_indices(refs)
    of_to = refs.get("of_to_entries") or {}

    con = sqlite3.connect(f"file:{args.db.expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # Casos: linhas validadas em OFs multi-irmã, modelo escrito, verdade
    # decidível (a designação final é consistente com >=1 irmão).
    cases: list[tuple[str, str, str]] = []  # (of, raw_modelo, truth_modelo)
    for s in con.execute(
        "SELECT raw_extraction, sheet_data FROM sheets "
        "WHERE status='validated' AND raw_extraction IS NOT NULL"
    ):
        try:
            raw = json.loads(s["raw_extraction"] or "{}")
            fin = json.loads(s["sheet_data"] or "{}")
        except (TypeError, ValueError):
            continue
        rr, rf = raw.get("rows") or [], fin.get("rows") or []
        for i in range(min(len(rr), len(rf))):
            of = se.normalize_of((rf[i] or {}).get("of"))
            entries = of_to.get(of) or []
            if len({str(e.get("designacao") or "").upper() for e in entries}) < 2:
                continue
            r_mod = str((rr[i] or {}).get("modelo") or "").strip()
            t_mod = str((rf[i] or {}).get("modelo") or "").strip()
            if not r_mod or not t_mod:
                continue
            if any(_truth_consistent(t_mod, e.get("designacao")) for e in entries):
                cases.append((of, r_mod, t_mod))
    print(f"casos de discriminação de irmãos: {len(cases)}")

    # Colisão base dos cores reais (relatório — fundamenta o denominador).
    import random
    random.seed(42)
    cores = []
    for _of, r_mod, _t in cases:
        p, ab = se._model_code_cores_cached(r_mod)
        c = ab[0] if ab else (p[0] if p else se._model_compact(r_mod))
        if len(c) >= 6:
            cores.append(c)
    sample = random.sample(cores, min(120, len(cores))) if cores else []
    counts = idx.get("fs_token_counts") or {}
    coll = {"d0": 0.0, "d1": 0.0, "d2": 0.0}
    for c in sample:
        for t in se._token_neighbors(c, idx):
            d = se._lev_distance(c, t)
            if d <= 2:
                coll[f"d{d}"] += counts.get(t, 1)
    n_s = max(len(sample), 1)
    collision = {k: round(v / n_s, 2) for k, v in coll.items()}
    print("colisão média (entries) por distância:", collision)

    # Fit de α por discriminação de irmãos (grid).
    base_params = dict(se._load_cross_params())
    results = {}
    for alpha in [float(a) for a in args.alphas.split(",")]:
        patched = dict(base_params)
        patched["model_token_lr"] = {"oov_alpha": alpha, "ab_eps_bits": 0.2}
        orig = se._load_cross_params
        se._load_cross_params = lambda p=patched: p  # type: ignore[assignment]
        idx["fs_uhat"] = {}
        ok = n = 0
        try:
            for of, r_mod, t_mod in cases:
                entries = of_to.get(of) or []
                scored = []
                for e in entries:
                    lr = se._model_lr_bits(r_mod, e.get("designacao"), idx)
                    scored.append((lr if lr is not None else -99.0, e))
                if not scored:
                    continue
                scored.sort(key=lambda t: -t[0])
                top_lr = scored[0][0]
                if top_lr <= -99.0:
                    continue
                winners = [e for lr, e in scored if lr >= top_lr - 1e-9]
                n += 1
                ok += any(
                    _truth_consistent(t_mod, e.get("designacao"))
                    for e in winners
                ) and len({str(e.get("designacao") or "").upper()
                           for e in winners}) == 1
        finally:
            se._load_cross_params = orig
            se._load_cross_params.cache_clear()
        acc = round(ok / n, 4) if n else None
        results[str(alpha)] = {"n": n, "top1_unico_certo": ok, "acc": acc}
        print(f"  alpha={alpha}: n={n} acc={acc}")

    # α é INSENSÍVEL ao ranking de irmãos (û(core) é constante comum dentro
    # da OF) — só fixa a ESCALA absoluta do LR, que a calibração do posterior
    # R252 (T, b_H0) absorve. Escolha: melhor acc com desempate em 1.0 (uma
    # pseudo-entry de massa OOV; exato-único ≈ log2(N/2) ≈ 13,3 bits,
    # coerente com o w_cap 14 do motor).
    accs = {a: r["acc"] for a, r in results.items() if r["acc"] is not None}
    best = max(accs.values()) if accs else None
    tied = [a for a, v in accs.items() if v == best]
    best_alpha = "1.0" if "1.0" in tied else (tied[0] if tied else "1.0")
    out = {
        "model_token_lr": {
            "oov_alpha": float(best_alpha),
            "ab_eps_bits": 0.2,
            "collision_entries_by_distance": collision,
            "sibling_acc_by_alpha": results,
            "n_cases": len(cases),
            "fitted_at": datetime.now(timezone.utc).isoformat(),
            "source_db_sha256_16": hashlib.sha256(
                args.db.expanduser().read_bytes()).hexdigest()[:16],
        }
    }
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
