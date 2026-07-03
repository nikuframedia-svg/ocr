"""R236 — Backtest do WINNER contra verdade humana (harness institucional).

Compara a seleção de winner do motor CANDIDATO (o scoring_engine do checkout
atual) contra um BASELINE (git ref, default R231/601fe7d), sobre três conjuntos
rotulados extraídos do app.db da fábrica:

  - GOOD: linhas validadas em que a OF escrita pelo operador é correta e existe
    no plano — mede ``regressed_good_raw`` diretamente (obrigatório 100%).
  - CORR: linhas em que um humano corrigiu um campo de IDENTIDADE — verdade
    forte, mede ``corrected_to_truth``.
  - ENG:  linhas em que o motor mudou a OF e o humano validou em bloco —
    verdade FRACA (vantagem de casa do baseline); reportada à parte.

Este harness é o ciclo de calibração v1→v2 do plano R236 institucionalizado:
qualquer variante do cross corre isto ANTES do gate oficial.

Uso:
    uv run python scripts/diag/backtest_winner.py \
        --db ~/Downloads/auditoria_humana/app.db \
        --plan "~/Downloads/plan_colunas_cpis (8).xlsx" \
        --sap "~/Downloads/StockSAP_Dinamico (5).xlsx" \
        [--baseline-ref 601fe7d] [--good-cap 110] [--eng-cap 70] \
        [--out-dir reports/backtest_winner]

Notas de fidelidade:
  - As refs vêm do loader REAL (``ref_watcher._mine_from_excel``).
  - Cada motor usa o SEU próprio realinhamento + candidatos + winner
    (``select_winner`` quando exposto — R236+; senão o caminho R231 clássico).
  - ``of_consumption.remaining`` é neutralizado (sem DB de produção).
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))

_ID_EDIT_RE = re.compile(r"^rows\[(\d+)\]\.(of|ov|cliente|modelo)$")


def _load_engine_from_ref(ref: str, out_dir: Path):
    """Extrai backend/app/pipeline/scoring_engine.py de um git ref e importa-o
    como módulo isolado (partilha o package ``app`` do checkout atual)."""
    src = subprocess.run(
        ["git", "-C", str(_REPO), "show",
         f"{ref}:backend/app/pipeline/scoring_engine.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    path = out_dir / f"_baseline_engine_{ref}.py"
    path.write_text(src, encoding="utf-8")
    name = f"baseline_engine_{ref}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _neutralize_of_consumption() -> None:
    import app.pipeline.of_consumption as ofc

    ofc.remaining = lambda entry, phase=None: float("inf")


def _winner_of(engine, row: dict, refs: dict, idx: dict, tpl: str | None,
               extra_bias: dict | None = None):
    """Winner de UMA linha pelo caminho do próprio motor. Devolve
    (of_normalizada, margem_bits|None)."""
    select = getattr(engine, "select_winner", None)
    if callable(select):  # R236+ — caminho encapsulado do candidato
        try:
            winner = select(row, refs, template_name=tpl,
                            extra_bias=extra_bias)
        except TypeError:  # motor sem R242 (sem extra_bias)
            winner = select(row, refs, template_name=tpl)
        of = engine.normalize_of(
            (winner or {}).get("_of") or (winner or {}).get("of") or "")
        margin = (winner or {}).get("_margin_bits")
        return of, margin
    # Caminho clássico (R231): realinhar → candidatos → winner.
    row2 = engine._realign_misplaced_of(dict(row), idx, tpl)
    if hasattr(engine, "_realign_misplaced_cliente"):
        row2 = engine._realign_misplaced_cliente(row2, refs, idx)
    cbf = {
        f: engine._candidates_for_field(f, row2, refs, idx)
        for f in engine._ROW_FIELDS
    }
    winner = engine._find_winner_entry(
        cbf, row2, refs, idx, None, None, force_top1=True)
    of = engine.normalize_of(
        (winner or {}).get("_of") or (winner or {}).get("of") or "")
    return of, (winner or {}).get("_margin_bits")


_SHIFT_TOKEN_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")


def _labeled_sets(db: Path, engine, plan_of_keys: set[str],
                  good_cap: int, eng_cap: int) -> dict[str, list]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    id_edit_rows: dict[int, set[int]] = defaultdict(set)
    for r in con.execute(
        "SELECT sheet_id, field_path FROM edits "
        "WHERE source='human' AND old_value <> new_value"
    ):
        m = _ID_EDIT_RE.match(r["field_path"])
        if m:
            id_edit_rows[r["sheet_id"]].add(int(m.group(1)))
    sets: dict[str, list] = {"CORR": [], "GOOD": [], "ENG": [], "SHIFT": []}
    for s in con.execute(
        "SELECT id, sheet_data, raw_extraction FROM sheets "
        "WHERE status='validated' AND raw_extraction IS NOT NULL "
        "ORDER BY id DESC"
    ):
        try:
            raw = json.loads(s["raw_extraction"])
            fin = json.loads(s["sheet_data"])
        except (TypeError, ValueError):
            continue
        tpl = fin.get("template_name") or raw.get("template_name")
        rr, rf = raw.get("rows") or [], fin.get("rows") or []
        for i in range(min(len(rr), len(rf))):
            if not any(str(v or "").strip() for v in (rr[i] or {}).values()):
                continue
            t_of = engine.normalize_of((rf[i] or {}).get("of"))
            if not t_of or t_of not in plan_of_keys:
                continue  # plano de hoje não cobre — fora do backtest
            r_of = engine.normalize_of((rr[i] or {}).get("of"))
            rec = (s["id"], i, tpl, dict(rr[i]), t_of)
            # R240 — conjunto SHIFT: a OF verdadeira estava escrita NOUTRA
            # coluna (validação humana do realinhamento). Independente dos
            # outros conjuntos (uma linha pode estar em ambos).
            if r_of != t_of:
                for src in ("ov", "pri", "cliente", "modelo", "lote"):
                    src_text = str((rr[i] or {}).get(src) or "")
                    if any(
                        engine.normalize_of(m.group(1)) == t_of
                        for m in _SHIFT_TOKEN_RE.finditer(src_text)
                    ):
                        sets["SHIFT"].append((s["id"], i, tpl, dict(rr[i]),
                                              t_of, src))
                        break
            if i in id_edit_rows.get(s["id"], set()):
                sets["CORR"].append(rec)
            elif r_of == t_of and len(sets["GOOD"]) < good_cap:
                sets["GOOD"].append(rec)
            elif r_of != t_of and len(sets["ENG"]) < eng_cap:
                sets["ENG"].append(rec)
    return sets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--sap", required=True, type=Path)
    ap.add_argument("--baseline-ref", default="601fe7d")
    ap.add_argument("--good-cap", type=int, default=110)
    ap.add_argument("--eng-cap", type=int, default=70)
    ap.add_argument("--out-dir", type=Path,
                    default=Path("reports/backtest_winner"))
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    _neutralize_of_consumption()
    import app.pipeline.scoring_engine as cand
    base = _load_engine_from_ref(args.baseline_ref, args.out_dir)
    from app.cross_check.ref_watcher import _mine_from_excel

    refs = _mine_from_excel(args.sap.expanduser(), args.plan.expanduser())
    refs.setdefault("loaded_at", "backtest")
    plan_of_keys = set((refs.get("of_to_entries") or {}).keys())
    idx_c = cand._get_indices(refs)
    idx_b = base._get_indices(refs)
    print(f"plano: {sum(len(v) for v in refs['of_to_entries'].values())} "
          f"entries, {len(plan_of_keys)} OFs | baseline={args.baseline_ref} "
          f"({base.ENGINE_VERSION}) | candidato={cand.ENGINE_VERSION}")

    sets = _labeled_sets(args.db, cand, plan_of_keys,
                         args.good_cap, args.eng_cap)
    print({k: len(v) for k, v in sets.items()})

    # R242/D1 — prior de produção com a DATA HISTÓRICA de cada folha (a
    # atividade vem do próprio --db; janela 14d estritamente antes do dia).
    conb = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sheet_dates: dict[int, str] = {
        int(r[0]): str(r[1] or "")[:10]
        for r in conb.execute("SELECT id, captured_at FROM sheets")
    }
    prod_events: list[tuple[str, str]] = [
        (cand.normalize_of(r[0]), str(r[1] or "")[:10])
        for r in conb.execute(
            "SELECT of, sheet_iso_date FROM production_rows "
            "WHERE sheet_status='validated' AND of IS NOT NULL"
        )
    ]
    q6 = {}
    try:
        q6 = (json.loads((_REPO / "lexicons" / "cross_params.json")
                         .read_text(encoding="utf-8"))
              .get("quant6_production_prior") or {})
    except (OSError, ValueError):
        pass
    _ab = float(q6.get("production_prior_bits") or 2.0)
    _ib = float(q6.get("production_prior_inactive_bits") or -1.77)
    _bias_cache: dict[str, dict | None] = {}

    def _prod_bias_for(day: str) -> dict | None:
        if not day or len(day) < 10:
            return None
        if day not in _bias_cache:
            import datetime as _dt
            try:
                d = _dt.date.fromisoformat(day)
            except ValueError:
                _bias_cache[day] = None
                return None
            lo = (d - _dt.timedelta(days=14)).isoformat()
            active = {o for o, dd in prod_events if o and lo <= dd < day}
            _bias_cache[day] = (
                {"of": {k: _ab for k in active}, "of_default": _ib}
                if active else None
            )
        return _bias_cache[day]

    summary: dict = {
        "baseline_ref": args.baseline_ref,
        "baseline_version": base.ENGINE_VERSION,
        "candidate_version": cand.ENGINE_VERSION,
        "plan": str(args.plan), "sap": str(args.sap), "sets": {},
    }
    flips_path = args.out_dir / "flips.csv"
    with flips_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "sheet_id", "row", "template", "truth_of",
                    "baseline_of", "candidate_of", "candidate_margin_bits",
                    "ocr_of", "ocr_ov", "ocr_modelo", "ocr_cliente"])
        t0 = time.time()
        for name, S in sets.items():
            b_ok = c_ok = b_only = c_only = neither = errs = 0
            margins_ok: list[float] = []
            margins_bad: list[float] = []
            shift_detail: list[tuple[str, bool, bool]] = []
            for rec in S:
                sid, i, tpl, row, t_of = rec[:5]  # SHIFT traz a coluna-fonte em rec[5]
                try:
                    b_of, _ = _winner_of(base, row, refs, idx_b, tpl)
                    c_of, mg = _winner_of(
                        cand, row, refs, idx_c, tpl,
                        extra_bias=_prod_bias_for(sheet_dates.get(sid, "")),
                    )
                except Exception as exc:  # noqa: BLE001 — não parar o batch
                    errs += 1
                    print(f"  ERRO s{sid} r{i}: {exc}", file=sys.stderr)
                    continue
                B, C = b_of == t_of, c_of == t_of
                if name == "SHIFT" and len(rec) > 5:
                    shift_detail.append((str(rec[5]), B, C))
                b_ok += B
                c_ok += C
                b_only += B and not C
                c_only += C and not B
                neither += not B and not C
                if mg is not None:
                    (margins_ok if C else margins_bad).append(float(mg))
                if B != C:
                    w.writerow([name, sid, i, tpl, t_of, b_of or "-",
                                c_of or "-",
                                "" if mg is None else round(float(mg), 2),
                                row.get("of"), row.get("ov"),
                                str(row.get("modelo"))[:24],
                                str(row.get("cliente"))[:18]])
            n = len(S)

            def _q(v: list[float], p: float) -> float | None:
                if not v:
                    return None
                v = sorted(v)
                return round(v[int(p * (len(v) - 1))], 2)

            summary["sets"][name] = {
                "n": n, "baseline_ok": b_ok, "candidate_ok": c_ok,
                "baseline_pct": round(100 * b_ok / n, 1) if n else None,
                "candidate_pct": round(100 * c_ok / n, 1) if n else None,
                "baseline_only": b_only, "candidate_only": c_only,
                "neither": neither, "errors": errs,
                "cand_margin_ok_p25_p50_p75": [
                    _q(margins_ok, .25), _q(margins_ok, .5), _q(margins_ok, .75)],
                "cand_margin_bad_p25_p50_p75": [
                    _q(margins_bad, .25), _q(margins_bad, .5), _q(margins_bad, .75)],
            }
            if shift_detail:
                by_src: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
                for src, okb, okc in shift_detail:
                    by_src[src].append((okb, okc))
                summary["sets"][name]["by_source"] = {
                    src: {"n": len(v), "baseline_ok": sum(b for b, _ in v),
                          "candidate_ok": sum(c for _, c in v)}
                    for src, v in sorted(by_src.items())
                }
            s = summary["sets"][name]
            print(f"=== {name} (n={n}) baseline {s['baseline_pct']}% | "
                  f"candidato {s['candidate_pct']}% "
                  f"(só-base {b_only} / só-cand {c_only} / nenhum {neither})")
            for src, d in (s.get("by_source") or {}).items():
                print(f"      fonte {src}: n={d['n']} base {d['baseline_ok']} "
                      f"| cand {d['candidate_ok']}")
        summary["elapsed_s"] = round(time.time() - t0, 1)

    # Total HISTÓRICO comparável (CORR+GOOD+ENG — a referência 89.2% do R236
    # foi medida sem SHIFT; linhas SHIFT podem duplicar as de CORR/ENG).
    core = [s for k, s in summary["sets"].items() if k != "SHIFT"]
    tot_b = sum(s["baseline_ok"] for s in core)
    tot_c = sum(s["candidate_ok"] for s in core)
    tot_n = sum(s["n"] for s in core)
    summary["total"] = {
        "n": tot_n, "baseline_ok": tot_b, "candidate_ok": tot_c,
        "baseline_pct": round(100 * tot_b / tot_n, 1) if tot_n else None,
        "candidate_pct": round(100 * tot_c / tot_n, 1) if tot_n else None,
    }
    # Gate: GOOD a 100% no candidato; total (core) >= baseline; SHIFT (se
    # existir) não pior que o baseline.
    good = summary["sets"].get("GOOD") or {}
    shift = summary["sets"].get("SHIFT") or {}
    gate_shift = (
        True if not shift.get("n")
        else int(shift.get("candidate_ok") or 0) >= int(shift.get("baseline_ok") or 0)
    )
    summary["gate"] = {
        "good_100pct": good.get("candidate_ok") == good.get("n"),
        "total_not_worse": tot_c >= tot_b,
        "shift_not_worse": gate_shift,
        "passed": (
            good.get("candidate_ok") == good.get("n")
            and tot_c >= tot_b
            and gate_shift
        ),
    }
    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"TOTAL baseline {summary['total']['baseline_pct']}% | "
          f"candidato {summary['total']['candidate_pct']}% | "
          f"gate passed={summary['gate']['passed']}")
    print(f"outputs: {args.out_dir}/summary.json, {flips_path}")


if __name__ == "__main__":
    main()
