#!/usr/bin/env python3
"""R224 — relatório de profiling: tempo por etapa + traço de pontuação de match.

Lê ``data/_logs/profile.jsonl`` (escrito por ``_write_profile`` no hot path, para
cada folha processada/reprocessada/regenerada) e produz um resumo:
  - tempos por etapa (OCR Pass-1/2, refs, scoring, store) — p50/p95/máx;
  - folhas mais lentas;
  - tokens gerados pelo OCR (mostra o efeito do /no_think) por modelo;
  - distribuição do `pool_size` do cross (expõe o pool inchado de ~12k);
  - traço de match por folha (vencedor, candidatos pontuados, decisões).

Uso:
    uv run python scripts/diag/profile_report.py --last 20
    uv run python scripts/diag/profile_report.py --sheet 1932 --detail
    uv run python scripts/diag/profile_report.py --last 50 --md reports/profiling.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOG = _REPO / "data" / "_logs" / "profile.jsonl"

# Ordem de apresentação das etapas de timing.
_STAGE_ORDER = [
    "pass1_ms", "side_detect_ms", "pass2_ms", "build_ms",
    "refs_ms", "rebuild_ms", "scoring1_ms", "overwrites_ms", "scoring2_ms",
    "store_ms", "cross_total_ms",
]


def _read_records(log: Path) -> list[dict]:
    recs: list[dict] = []
    # rotacionado (.jsonl.1) primeiro = mais antigo, depois o atual.
    for p in (log.with_suffix(".jsonl.1"), log):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0
    s = sorted(vals)
    k = (len(s) - 1) * q
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f))


def _stat_line(name: str, vals: list[float]) -> str:
    if not vals:
        return f"  {name:16s}  —"
    return (f"  {name:16s}  n={len(vals):4d}  p50={_pct(vals, .5):7.0f}  "
            f"p95={_pct(vals, .95):7.0f}  max={max(vals):7.0f}")


def _total_ms(rec: dict) -> int:
    t = rec.get("timing") or {}
    # tempo de OCR (passagens) + total do cross.
    ocr = sum(v for k, v in t.items()
              if k in ("pass1_ms", "side_detect_ms", "pass2_ms", "build_ms")
              and isinstance(v, (int, float)))
    return int(ocr + (t.get("cross_total_ms") or 0))


def build_report(recs: list[dict], *, detail: bool) -> str:
    out: list[str] = []
    w = out.append

    w(f"# Profiling — {len(recs)} registo(s)")
    by_trigger: dict[str, int] = {}
    for r in recs:
        by_trigger[r.get("trigger", "?")] = by_trigger.get(r.get("trigger", "?"), 0) + 1
    w("Por trigger: " + ", ".join(f"{k}={v}" for k, v in sorted(by_trigger.items())))
    w("")

    # --- Tempos por etapa ---
    w("## Tempos por etapa (ms)")
    for stage in _STAGE_ORDER:
        vals = [r["timing"][stage] for r in recs
                if isinstance(r.get("timing"), dict)
                and isinstance(r["timing"].get(stage), (int, float))]
        if vals:
            w(_stat_line(stage, vals))
    totals = [_total_ms(r) for r in recs]
    w(_stat_line("TOTAL", totals))
    w("")

    # --- Folhas mais lentas ---
    w("## Folhas mais lentas (top 10 por total)")
    slow = sorted(recs, key=_total_ms, reverse=True)[:10]
    w("  sheet  total_ms  trigger        template            n_rows")
    for r in slow:
        w(f"  {str(r.get('sheet_id')):5s}  {_total_ms(r):8d}  "
          f"{str(r.get('trigger','?')):13s}  {str(r.get('template','?')):18s}  "
          f"{r.get('n_rows', 0)}")
    w("")

    # --- OCR (tokens) ---
    w("## OCR — tokens gerados (eval_count_total), por modelo")
    by_model: dict[str, list[int]] = {}
    for r in recs:
        ocr = r.get("ocr") or {}
        ec = ocr.get("eval_count_total")
        if isinstance(ec, (int, float)):
            by_model.setdefault(ocr.get("model") or "?", []).append(int(ec))
    for model, vals in by_model.items():
        w(_stat_line(model, vals))
    w("")

    # --- Cross — pool_size ---
    w("## Cross — pool_size por linha (pool inchado = candidatos a pontuar)")
    pool_sizes: list[int] = []
    fallback_rows = 0
    total_rows = 0
    for r in recs:
        for row in (r.get("scoring") or []):
            total_rows += 1
            ps = row.get("pool_size")
            if isinstance(ps, (int, float)):
                pool_sizes.append(int(ps))
            if row.get("fallback_full_scan"):
                fallback_rows += 1
    w(_stat_line("pool_size", pool_sizes))
    if total_rows:
        w(f"  full-scan fallback: {fallback_rows}/{total_rows} linhas "
          f"({100*fallback_rows/total_rows:.0f}%)")
    w("")

    # --- Detalhe por folha ---
    if detail:
        w("## Traço de match por folha")
        for r in recs:
            w(f"\n### Folha {r.get('sheet_id')} — {r.get('trigger')} — "
              f"{r.get('template')} — {r.get('n_rows')} linhas")
            t = r.get("timing") or {}
            w("  tempos: " + ", ".join(f"{k}={v}" for k, v in t.items()))
            for row in (r.get("scoring") or []):
                w(f"  • linha {row.get('row_index')}: winner={row.get('winner_of')} "
                  f"(mode={row.get('winner_mode')}, combined={row.get('winner_combined')}, "
                  f"pool={row.get('pool_size')}, cand_por_campo={row.get('candidates_by_field')}"
                  f"{', FULLSCAN' if row.get('fallback_full_scan') else ''})")
                cands = row.get("candidates") or []
                for c in cands[:5]:
                    sims = ", ".join(
                        f"{fs.get('field')}={fs.get('sim')}"
                        for fs in (c.get("field_sims") or [])
                        if (fs.get("sim") or 0) > 0
                    )
                    w(f"      - {c.get('of')}: agree={c.get('agree')} exact={c.get('exact')} "
                      f"combined={c.get('combined')} [{sims}]")
                for d in (row.get("decisions") or []):
                    if d.get("ocr_value") != d.get("final_value") or d.get("engine_status") == "very_different":
                        w(f"      ↳ {d.get('field')}: '{d.get('ocr_value')}' → "
                          f"'{d.get('final_value')}' ({d.get('engine_status')}/{d.get('source')})")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Relatório de profiling (R224).")
    ap.add_argument("--log", type=Path, default=_DEFAULT_LOG, help="profile.jsonl")
    ap.add_argument("--last", type=int, default=None, help="só os últimos N registos")
    ap.add_argument("--sheet", type=int, default=None, help="só esta folha")
    ap.add_argument("--detail", action="store_true", help="traço de match por folha")
    ap.add_argument("--md", type=Path, default=None, help="escrever markdown neste ficheiro")
    args = ap.parse_args()

    recs = _read_records(args.log)
    if args.sheet is not None:
        recs = [r for r in recs if r.get("sheet_id") == args.sheet]
        args.detail = True
    if args.last is not None:
        recs = recs[-args.last:]
    if not recs:
        print(f"Sem registos em {args.log} (já processaste alguma folha com profiling ligado?)")
        return 1

    report = build_report(recs, detail=args.detail or args.sheet is not None)
    print(report)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(report, encoding="utf-8")
        print(f"\n[markdown escrito em {args.md}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
