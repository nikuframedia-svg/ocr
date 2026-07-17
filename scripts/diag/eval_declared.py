#!/usr/bin/env python3
"""Avaliação de maturação dos campos de cross DECLARADO (fase B).

Read-only diagnostic — não escreve na BD, nos refs nem nos JSONs do cross.
Padrão R244 (cross_refit): recomputa tudo do zero a partir das
fontes-verdade a cada corrida — zero estado novo, zero custo de runtime.

Varre os JSONs do cross (kanban_refs/03_Cross_Check) à procura de células
com source/ref_source "declared_plan" e cruza-as com as folhas validadas e
as edições humanas da BD. Por campo declarado imprime:

  - n células, taxas confirmed / very_different / NA
  - n edições humanas no campo
  - proxy-m: concordância do valor FINAL validado pelo humano com a
    referência do plano (usa o MESMO comparador do motor: cmp/tol do spec)
  - proxy-u: concordância por acaso (permutação determinística das refs
    entre células do mesmo campo)

Veredito por campo (pisos do refit R244): n >= --min-cells, NA < 30% e
proxy-m >= 2× proxy-u → "candidato a promoção (fase C)". A promoção em si
é sempre processo com dev + gates (backtest + golden set) — ver
docs/PROPOSTA_CROSS_DECLARADO.md.

Fase C-lite ("dv") — as métricas chaveiam por (template, campo) e o
`--write-params` grava o bloco `declared_vote.by_template` em
lexicons/cross_params.json (m medido p/ o peso do voto, com fingerprint
column/cmp/tol; NUNCA automático — exige --yes e só entradas que passem os
pisos). O refit online (chars) não toca no bloco; o motor só o lê no
próximo processo (o lru_cache de _load_cross_params vive por processo).

Notas de leitura com o VOTO ativo (conservadoras por construção):
  - o proxy-u sobe ligeiramente (os refs gravados vêm de winners que o
    próprio voto puxou p/ a concordância) → a régua de promoção fica MAIS
    exigente, nunca menos;
  - a taxa de NA cai "de borla" (o voto favorece entries com a coluna
    preenchida) — não ler como melhoria de OCR.

Uso:
    uv run python scripts/diag/eval_declared.py
    uv run python scripts/diag/eval_declared.py --field pbase --json out.json
    uv run python scripts/diag/eval_declared.py --write-params --yes
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))

from app.config import resolve_kanban_path
from app.pipeline.scoring_engine import (
    _AGREE_THRESHOLD,
    _norm_ascii_upper,
    _num,
    _str_sim,
)
from app.web.template_store import _declared_from_dict

_ROW_PATH_RE = re.compile(r"^rows\[(\d+)\]\.([a-z0-9_]+)$")
_MAX_U_PAIRS = 200


def _default_cross_dir() -> Path:
    return resolve_kanban_path(
        "CROSS_CHECK_DIR",
        r"C:\kanban\nifruka\03_Cross_Check",
        "kanban_refs/03_Cross_Check",
    )


def _agree(final: str, ref: str, cmp: str, tol: float | None) -> bool | None:
    """O MESMO comparador do braço declarado do motor
    (_apply_declared_to_field). None = não comparável (equivale a NA)."""
    final, ref = str(final or "").strip(), str(ref or "").strip()
    if not final or not ref:
        return None
    if cmp == "num":
        got, want = _num(final), _num(ref)
        if want is None:
            return None
        if got is None:
            return False
        return abs(got - want) <= float(tol or 0.0)
    a, b = _norm_ascii_upper(final), _norm_ascii_upper(ref)
    return a == b or (_str_sim(a, b) / 100.0) >= _AGREE_THRESHOLD


def _declared_configs(con) -> dict[tuple[str, str], dict]:
    """declared_cross de TODOS os kanban_templates (qualquer status —
    folhas históricas podem vir de templates entretanto desativados),
    chaveado por (template_name, campo) — o mesmo nome de campo pode
    apontar para colunas/semânticas diferentes em templates distintos."""
    out: dict[tuple[str, str], dict] = {}
    for (name, spec_json) in con.execute(
        "SELECT name, spec_json FROM kanban_templates ORDER BY id"
    ):
        try:
            spec = json.loads(spec_json or "{}")
        except (TypeError, ValueError):
            continue
        for field, ref in _declared_from_dict(spec.get("declared_cross")).items():
            out[(str(name), field)] = {
                "column": ref.column, "cmp": ref.cmp, "tol": ref.tol,
                "vote": ref.vote,
            }
    return out


def _sheet_templates(con) -> dict[int, str]:
    """sheet_id → template_name (para chavear as células por template)."""
    out: dict[int, str] = {}
    for sid, name in con.execute(
        "SELECT id, COALESCE(json_extract(sheet_data, '$.template_name'), "
        "'bobine_formato') FROM sheets"
    ):
        out[int(sid)] = str(name or "bobine_formato")
    return out


def _scan_cross_jsons(cross_dir: Path, only_field: str | None) -> list[dict]:
    """Células declaradas nos JSONs por-folha (ignora agregados _*.json)."""
    cells: list[dict] = []
    for path in sorted(cross_dir.rglob("*sheet*.json")):
        if path.name.startswith("_"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        sheet_id = payload.get("sheet_id")
        if sheet_id is None:
            continue
        for row in payload.get("rows") or []:
            row_index = row.get("row_index", 0)
            for field, cell in (row.get("fields") or {}).items():
                if only_field and field != only_field:
                    continue
                src = cell.get("ref_source") or cell.get("source")
                if src != "declared_plan":
                    continue
                cells.append({
                    "sheet_id": int(sheet_id),
                    "row_index": int(row_index),
                    "field": field,
                    "engine_status": cell.get("engine_status") or "",
                    "value": str(cell.get("value") or ""),
                    "ref": str(cell.get("ref") or ""),
                })
    return cells


def _validated_finals(con) -> dict[int, list[dict]]:
    """sheet_id → rows finais (sheet_data) das folhas validadas."""
    out: dict[int, list[dict]] = {}
    for sid, sheet_json in con.execute(
        "SELECT id, sheet_data FROM sheets WHERE status='validated'"
    ):
        try:
            out[int(sid)] = (json.loads(sheet_json or "{}") or {}).get("rows") or []
        except (TypeError, ValueError):
            continue
    return out


def _human_edit_counts(con) -> Counter:
    """Nº de edições humanas por campo (field_path rows[i].campo)."""
    counts: Counter = Counter()
    for (field_path,) in con.execute(
        "SELECT field_path FROM edits "
        "WHERE source='human' AND old_value <> new_value"
    ):
        m = _ROW_PATH_RE.match(str(field_path or ""))
        if m:
            counts[m.group(2)] += 1
    return counts


def evaluate(cross_dir: Path, db_path: Path, only_field: str | None,
             min_cells: int) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        configs = _declared_configs(con)
        sheet_tpl = _sheet_templates(con)
        finals = _validated_finals(con)
        edit_counts = _human_edit_counts(con)
    finally:
        con.close()

    cells = _scan_cross_jsons(cross_dir, only_field)
    # dv — chavear por (template, campo): o mesmo nome de campo pode ter
    # colunas/semânticas diferentes em templates distintos. Folhas cujo
    # template já não existe caem no bucket "?" (só informativo).
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cells:
        tpl = sheet_tpl.get(c["sheet_id"], "?")
        if (tpl, c["field"]) not in configs:
            tpl = "?"
        by_key[(tpl, c["field"])].append(c)

    report: dict = {"cross_dir": str(cross_dir), "db": str(db_path),
                    "min_cells": min_cells, "fields": {}}
    for (tpl, field) in sorted(by_key):
        fc = by_key[(tpl, field)]
        cfg = configs.get((tpl, field)) or {
            "column": "?", "cmp": "text", "tol": None, "vote": False}
        status_counts = Counter(c["engine_status"] for c in fc)
        n = len(fc)
        na_rate = status_counts.get("NA", 0) / n if n else 0.0

        # proxy-m: valor FINAL humano (folhas validadas) vs ref do plano
        pairs: list[tuple[str, str]] = []   # (final, ref) comparáveis
        for c in fc:
            rows = finals.get(c["sheet_id"])
            if rows is None or c["row_index"] >= len(rows) or not c["ref"]:
                continue
            final = str((rows[c["row_index"]] or {}).get(field) or "").strip()
            if final:
                pairs.append((final, c["ref"]))
        m_hits = [
            _agree(final, ref, cfg["cmp"], cfg["tol"]) for final, ref in pairs]
        m_hits = [h for h in m_hits if h is not None]
        proxy_m = sum(m_hits) / len(m_hits) if m_hits else None

        # proxy-u: permutação determinística (shift circular) das refs
        u_hits = []
        if len(pairs) >= 2:
            for i, (final, _ref) in enumerate(pairs[:_MAX_U_PAIRS]):
                other_ref = pairs[(i + 1) % len(pairs)][1]
                h = _agree(final, other_ref, cfg["cmp"], cfg["tol"])
                if h is not None:
                    u_hits.append(h)
        proxy_u = sum(u_hits) / len(u_hits) if u_hits else None

        promotable = bool(
            n >= min_cells
            and na_rate < 0.30
            and proxy_m is not None
            and proxy_m >= 2 * (proxy_u if proxy_u else 0.0)
            and proxy_m > 0.0
        )
        missing = []
        if n < min_cells:
            missing.append(f"células {n}/{min_cells}")
        if na_rate >= 0.30:
            missing.append(f"NA {na_rate:.0%} (>= 30%)")
        if proxy_m is None:
            missing.append("sem folhas validadas comparáveis")
        elif proxy_u is not None and proxy_m < 2 * proxy_u:
            missing.append(
                f"separação m/u fraca ({proxy_m:.2f} < 2×{proxy_u:.2f})")

        report["fields"][f"{tpl}.{field}"] = {
            "template": tpl,
            "field": field,
            "config": cfg,
            "n_cells": n,
            "rates": {
                "confirmed": status_counts.get("confirmed", 0) / n if n else 0,
                "very_different":
                    status_counts.get("very_different", 0) / n if n else 0,
                "na": na_rate,
            },
            "n_human_edits": edit_counts.get(field, 0),
            "n_validated_pairs": len(m_hits),
            "proxy_m": proxy_m,
            "proxy_u": proxy_u,
            "promotable": promotable,
            "missing": missing,
        }
    return report


def _print_report(report: dict) -> None:
    fields = report["fields"]
    if not fields:
        print("Sem células declaradas nos JSONs do cross "
              f"({report['cross_dir']}). Ou nenhum template declara campos, "
              "ou ainda não foram cruzadas folhas com eles.")
        return
    for key, r in fields.items():
        cfg = r["config"]
        tol = f" ±{cfg['tol']}" if cfg["tol"] is not None else ""
        vote = "  [VOTA]" if cfg.get("vote") else ""
        print(f"\n== {key}  (plano.{cfg['column']}, {cfg['cmp']}{tol}){vote} ==")
        print(f"  células: {r['n_cells']}  "
              f"(confirmed {r['rates']['confirmed']:.0%} / "
              f"very_different {r['rates']['very_different']:.0%} / "
              f"NA {r['rates']['na']:.0%})")
        print(f"  edições humanas no campo: {r['n_human_edits']}")
        pm = "—" if r["proxy_m"] is None else f"{r['proxy_m']:.2f}"
        pu = "—" if r["proxy_u"] is None else f"{r['proxy_u']:.2f}"
        print(f"  proxy-m (concordância c/ valor validado): {pm}  "
              f"[{r['n_validated_pairs']} pares]")
        print(f"  proxy-u (concordância por acaso):          {pu}")
        if r["promotable"]:
            print("  → CANDIDATO a promoção (fase C — processo com dev + "
                  "backtest + golden set)")
        else:
            print(f"  → ainda não: {'; '.join(r['missing'])}")


def write_declared_vote_params(report: dict, params_path: Path,
                               assume_yes: bool) -> int:
    """dv — grava o bloco declared_vote em cross_params.json (m medido).

    Só entradas PROMOTABLE (pisos do veredito) entram; fingerprint
    column/cmp/tol gravada para o motor recusar m de declarações mudadas.
    Backup do bloco anterior em declared_vote_previous (molde
    calibration_previous); escrita atómica tmp+replace (molde cross_refit
    R244). NUNCA automático — exige --yes depois de mostrar o diff."""
    entries: dict[str, dict[str, dict]] = {}
    for r in report["fields"].values():
        if not r["promotable"] or r["template"] == "?":
            continue
        cfg = r["config"]
        entries.setdefault(r["template"], {})[r["field"]] = {
            "column": cfg["column"], "cmp": cfg["cmp"], "tol": cfg["tol"],
            "m": round(float(r["proxy_m"]), 4),
            "u_fitted": (round(float(r["proxy_u"]), 4)
                         if r["proxy_u"] is not None else None),
            "n_pairs": r["n_validated_pairs"],
        }
    if not entries:
        print("--write-params: nenhuma entrada passa os pisos — nada a "
              "gravar (o bloco nunca contém entradas não-promovíveis).")
        return 0

    try:
        params = json.loads(params_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        params = {}
    old_block = params.get("declared_vote")
    new_block = {
        "fitted_at": datetime.now(timezone.utc).isoformat(),
        "fitted_by": "scripts/diag/eval_declared.py --write-params",
        "m_default": (old_block or {}).get("m_default", 0.5),
        "cap_bits": (old_block or {}).get("cap_bits", 2.0),
        "by_template": entries,
    }
    print("\n--write-params — bloco declared_vote a gravar em "
          f"{params_path}:")
    print(json.dumps(new_block, indent=2, ensure_ascii=False))
    if old_block:
        print("\n(bloco anterior fica em declared_vote_previous)")
    if not assume_yes:
        print("\nABORTADO: confirma com --yes (a escrita muda o peso do "
              "voto no motor — gate humano).", file=sys.stderr)
        return 3
    if old_block:
        params["declared_vote_previous"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "values": old_block,
        }
    params["declared_vote"] = new_block
    tmp = params_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(params, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(params_path)
    print(f"\ngravado: {params_path} (o motor lê no próximo processo — "
          "restart do uvicorn na fábrica)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cross-dir", type=Path, default=None,
                    help="pasta 03_Cross_Check (default: resolução R118)")
    ap.add_argument("--db", type=Path, default=_REPO / "data" / "app.db")
    ap.add_argument("--field", default=None,
                    help="avaliar só este campo declarado")
    ap.add_argument("--min-cells", type=int, default=500,
                    help="piso de células validadas (eco de MIN_M_CELLS)")
    ap.add_argument("--json", type=Path, default=None,
                    help="escrever o relatório JSON neste ficheiro")
    ap.add_argument("--write-params", action="store_true",
                    help="gravar o bloco declared_vote (m medido) em "
                         "lexicons/cross_params.json — exige --yes")
    ap.add_argument("--yes", action="store_true",
                    help="confirma a escrita do --write-params")
    ap.add_argument("--params-path", type=Path,
                    default=_REPO / "lexicons" / "cross_params.json")
    args = ap.parse_args()

    cross_dir = args.cross_dir or _default_cross_dir()
    if not cross_dir.exists():
        print(f"pasta do cross não existe: {cross_dir}", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"BD não existe: {args.db}", file=sys.stderr)
        return 2

    report = evaluate(cross_dir, args.db, args.field, args.min_cells)
    _print_report(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"\nrelatório JSON: {args.json}")
    if args.write_params:
        return write_declared_vote_params(report, args.params_path, args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
