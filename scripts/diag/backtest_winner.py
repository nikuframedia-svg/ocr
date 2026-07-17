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
  - MODEL_SIB (R247): linhas validadas cuja OF tem >=2 entries IRMÃS no plano
    (designações distintas) e o operador escreveu um modelo — mede se o winner
    escolheu a ENTRY certa (OF certa E designação consistente com a verdade),
    não só a OF. É o ponto cego histórico: o gate por OF não via trocas de
    modelo dentro da mesma OF. Verdade fraca (validação em bloco) — ler
    comparativamente, como ENG.
  - MODEL_STRONG (R247): subconjunto com edit humano em rows[i].modelo —
    verdade forte mas escassa (~9 linhas).

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

# R253.5 — o CANDIDATO emite `_posterior_stats` (estatísticas suficientes por
# temperatura) para o grid 3D do --calibrate recomputar p_of em forma fechada.
# Tem de estar setado ANTES de importar app.pipeline.scoring_engine (a flag é
# lida no import). O baseline é desligado em _load_engine_from_ref.
import os

os.environ.setdefault("CROSS_POSTERIOR_SWEEP", "1")

_ID_EDIT_RE = re.compile(r"^rows\[(\d+)\]\.(of|ov|cliente|modelo)$")
_MODEL_EDIT_RE = re.compile(r"^rows\[(\d+)\]\.modelo$")
# Tokens-código de um modelo: alfanuméricos com dígito E letra, len>=4.
_MODEL_CODE_TOKEN_RE = re.compile(r"[A-Z0-9]{4,}")


def _model_truth_consistent(truth: object, designacao: object) -> bool:
    """R247 — verdade humana de modelo vs designação escolhida, por
    CONSISTÊNCIA de código (o humano por vezes valida o texto do operador —
    "CD300506", "A4.504" — em vez da designação completa do plano).

    Deliberadamente INDEPENDENTE dos helpers do motor: o fix R247 mexe
    exatamente nessas funções, e medir com a mesma régua que decide
    esconderia um bug em ambos (circularidade)."""
    t = re.sub(r"[^A-Z0-9]+", " ", str(truth or "").upper()).strip()
    d = re.sub(r"[^A-Z0-9]+", " ", str(designacao or "").upper()).strip()
    if not t or not d:
        return False
    tc, dc = t.replace(" ", ""), d.replace(" ", "")

    def _o0(s: str) -> set[str]:
        return {s, s.replace("O", "0"), s.replace("0", "O")}

    if _o0(tc) & _o0(dc):
        return True
    # Containment do compacto inteiro (len>=5 — "A4504" ⊂ "…CA4217A4504…").
    if len(tc) >= 5 and any(
        tv in dv or dv in tv for tv in _o0(tc) for dv in _o0(dc)
    ):
        return True
    # Tokens-código da verdade contidos no compacto da designação.
    toks = [
        x for x in _MODEL_CODE_TOKEN_RE.findall(t)
        if any(c.isdigit() for c in x) and any(c.isalpha() for c in x)
    ]
    return any(v in dv for x in toks for v in _o0(x) for dv in _o0(dc))


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
    # R253/F0 — o baseline NUNCA herda CROSS_SCORING_VARIANT do processo: um
    # ref pós-R250 leria a env no import e, com =next no ambiente (o modo
    # normal de medir a variante), a comparação vira next-vs-next disfarçada.
    if hasattr(mod, "set_scoring_variant"):
        mod.set_scoring_variant("v30")
    # R253/F0b — 2ª assimetria (achada pelo controlo delta-0 pós-fix): o
    # motor resolve cross_params.json por Path(__file__).parents[3], mas este
    # ficheiro vive em reports/<out>/ → parents[3] cai FORA do repo → o
    # baseline corria com params VAZIOS (matriz default, calibração T=3.0)
    # contra um candidato fitted (+1 ENG/+1 MODEL_SIB medidos HEAD-vs-HEAD).
    if hasattr(mod, "_load_cross_params"):
        params_path = _REPO / "lexicons" / "cross_params.json"

        def _params_from_repo(_p=params_path):
            try:
                return json.loads(_p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

        _params_from_repo.cache_clear = lambda: None  # interface lru_cache
        mod._load_cross_params = _params_from_repo
    # R253.5 — o sweep é só para o candidato (o baseline não precisa e paga
    # 16×|pool| exponenciais por linha).
    if hasattr(mod, "_POSTERIOR_SWEEP"):
        mod._POSTERIOR_SWEEP = False
    return mod


def _neutralize_of_consumption() -> None:
    import app.pipeline.of_consumption as ofc

    ofc.remaining = lambda entry, phase=None: float("inf")


def _winner_of(engine, row: dict, refs: dict, idx: dict, tpl: str | None,
               extra_bias: dict | None = None,
               declared_vote: dict | None = None):
    """Winner de UMA linha pelo caminho do próprio motor. Devolve
    (of_normalizada, margem_bits|None, bits|None, winner_dict|None) —
    R247: o dict é preciso para pontuar MODEL_* (designação escolhida).
    dv: `declared_vote` só é passado quando não-None (modo --dv-mode
    candidate) — o fallback TypeError NUNCA pode apanhar a chamada com
    extra_bias de um motor R242+ (perderia a simetria do prior, R253/F0)."""
    select = getattr(engine, "select_winner", None)
    if callable(select):  # R236+ — caminho encapsulado do candidato
        try:
            if declared_vote is not None:
                winner = select(row, refs, template_name=tpl,
                                extra_bias=extra_bias,
                                declared_vote=declared_vote)
            else:
                winner = select(row, refs, template_name=tpl,
                                extra_bias=extra_bias)
        except TypeError:  # motor sem R242 (sem extra_bias)
            winner = select(row, refs, template_name=tpl)
        of = engine.normalize_of(
            (winner or {}).get("_of") or (winner or {}).get("of") or "")
        margin = (winner or {}).get("_margin_bits")
        return of, margin, (winner or {}).get("_bits"), winner
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
    return of, (winner or {}).get("_margin_bits"), (winner or {}).get("_bits"), winner


_SHIFT_TOKEN_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")


def _labeled_sets(db: Path, engine, plan_of_keys: set[str],
                  good_cap: int, eng_cap: int,
                  of_to_entries: dict | None = None,
                  model_cap: int = 150) -> dict[str, list]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    id_edit_rows: dict[int, set[int]] = defaultdict(set)
    model_edit_rows: dict[int, set[int]] = defaultdict(set)
    for r in con.execute(
        "SELECT sheet_id, field_path FROM edits "
        "WHERE source='human' AND old_value <> new_value"
    ):
        m = _ID_EDIT_RE.match(r["field_path"])
        if m:
            id_edit_rows[r["sheet_id"]].add(int(m.group(1)))
        m2 = _MODEL_EDIT_RE.match(r["field_path"])
        if m2:
            model_edit_rows[r["sheet_id"]].add(int(m2.group(1)))
    # R247 — OFs com >=2 designações IRMÃS (superfície do ponto cego).
    of_to_entries = of_to_entries or {}
    multi_sib_ofs = {
        of for of, entries in of_to_entries.items()
        if len({str((e or {}).get("designacao") or "").strip().upper()
                for e in entries or []
                if str((e or {}).get("designacao") or "").strip()}) >= 2
    }
    sets: dict[str, list] = {"CORR": [], "GOOD": [], "ENG": [], "SHIFT": [],
                             "OOD": [], "MODEL_SIB": [], "MODEL_STRONG": []}
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
                # R243 — conjunto OOD: a verdade está FORA do plano de hoje
                # (quant7: 10.7% mesmo em folhas frescas). A resposta certa é
                # ABSTER; usado para calibrar s_ood e medir taxa de abstenção.
                if t_of and len(sets["OOD"]) < 80:
                    sets["OOD"].append((s["id"], i, tpl, dict(rr[i]), t_of))
                continue
            r_of = engine.normalize_of((rr[i] or {}).get("of"))
            # R253.6 — a linha FINAL validada segue no rec (índice 5) para a
            # verdade POR CAMPO (ov/cliente/modelo, não só OF). Os sets
            # SHIFT/MODEL_* têm formatos próprios no índice 5 — o consumo é
            # sempre filtrado por nome do set.
            rec = (s["id"], i, tpl, dict(rr[i]), t_of, dict(rf[i] or {}))
            # R247 — conjuntos MODEL: modelo escrito + verdade DECIDÍVEL (a
            # verdade final é consistente com >=1 entry da OF; senão a linha
            # não distingue motores e só diluiria a métrica).
            t_mod = str((rf[i] or {}).get("modelo") or "").strip()
            r_mod = str((rr[i] or {}).get("modelo") or "").strip()
            if t_mod and r_mod:
                entries = of_to_entries.get(t_of) or []
                decidable = any(
                    _model_truth_consistent(t_mod, e.get("designacao"))
                    for e in entries
                )
                strong = i in model_edit_rows.get(s["id"], set())
                mrec = (s["id"], i, tpl, dict(rr[i]), t_of, t_mod)
                if decidable and strong:
                    sets["MODEL_STRONG"].append(mrec)
                if (decidable and t_of in multi_sib_ofs
                        and len(sets["MODEL_SIB"]) < model_cap):
                    sets["MODEL_SIB"].append(mrec)
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
    ap.add_argument("--model-cap", type=int, default=150,
                    help="cap do conjunto MODEL_SIB (R247)")
    ap.add_argument("--out-dir", type=Path,
                    default=Path("reports/backtest_winner"))
    ap.add_argument("--calibrate", action="store_true",
                    help="fitar T/s_ood e gravar em lexicons/cross_params.json")
    # dv — voto declarado (fase C-lite): spec JSON {"fields": {campo:
    # {"column","cmp","tol","vote","m"}}} instala um template-sonda no
    # registry ANTES do mine (o watcher mina os `extra` pelo caminho de
    # produção); "candidate" liga o termo SÓ no candidato (medição do delta
    # DO termo, com verdade humana + gate GOOD inviolável).
    ap.add_argument("--declared-spec", type=Path, default=None)
    ap.add_argument("--dv-mode", choices=("off", "candidate"), default="off")
    ap.add_argument("--dv-synthetic", choices=("truth", "rival"), default=None,
                    help="injeta o valor declarado nos rows: da entry-VERDADE"
                         " (teto de utilidade) ou de uma entry RIVAL"
                         " (adversarial — GOOD tem de se manter)")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    _neutralize_of_consumption()
    import app.pipeline.scoring_engine as cand
    base = _load_engine_from_ref(args.baseline_ref, args.out_dir)
    from app.cross_check.ref_watcher import _mine_from_excel

    # dv — template-sonda no registry runtime (via caminho de produção).
    dv_fields_cfg: dict[str, dict] = {}
    if args.declared_spec:
        from app.templates_registry import (
            DeclaredRef, TemplateSpec, set_runtime_templates,
        )
        raw_spec = json.loads(
            args.declared_spec.expanduser().read_text(encoding="utf-8"))
        dv_fields_cfg = dict(raw_spec.get("fields") or {})
        probe = TemplateSpec(
            name="bt_declared_probe", tpl_code="TPL103", phase="Produção",
            setor_aliases=("BT DECLARED PROBE",),
            row_fields=tuple(["of"] + sorted(dv_fields_cfg)),
            declared_cross={
                f: DeclaredRef(
                    column=str(cfg.get("column") or f),
                    cmp=str(cfg.get("cmp") or "text"),
                    tol=(float(cfg["tol"]) if cfg.get("tol") else None),
                    vote=cfg.get("vote") is True,
                )
                for f, cfg in dv_fields_cfg.items()
            },
            source="db",
        )
        set_runtime_templates([probe])

    refs = _mine_from_excel(args.sap.expanduser(), args.plan.expanduser())
    refs.setdefault("loaded_at", "backtest")
    plan_of_keys = set((refs.get("of_to_entries") or {}).keys())
    idx_c = cand._get_indices(refs)
    idx_b = base._get_indices(refs)
    print(f"plano: {sum(len(v) for v in refs['of_to_entries'].values())} "
          f"entries, {len(plan_of_keys)} OFs | baseline={args.baseline_ref} "
          f"({base.ENGINE_VERSION}) | candidato={cand.ENGINE_VERSION}")

    sets = _labeled_sets(args.db, cand, plan_of_keys,
                         args.good_cap, args.eng_cap,
                         of_to_entries=refs.get("of_to_entries") or {},
                         model_cap=args.model_cap)
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

    # dv — termo do candidato (o harness é o gate; ignora o env de propósito)
    dv_arg = None
    if args.dv_mode == "candidate" and dv_fields_cfg:
        dv_specs = tuple(
            (f, str(cfg.get("column") or f), str(cfg.get("cmp") or "text"),
             float(cfg.get("tol") or 0.0),
             min(max(float(cfg.get("m") or 0.5), 0.05), 0.95))
            for f, cfg in dv_fields_cfg.items() if cfg.get("vote") is True
        )
        if dv_specs:
            dv_arg = {"specs": dv_specs, "cap": 2.0}
    _rival_cache: dict[tuple[str, str], str | None] = {}

    def _dv_inject(row: dict, t_of: str) -> dict:
        """dv-synthetic (sensitivity analysis, não verdade real): injeta o
        valor da coluna declarada no row — da entry-VERDADE (teto de
        utilidade) ou de uma entry RIVAL determinística (adversarial)."""
        entries = refs.get("of_to_entries") or {}
        out = dict(row)
        for f, cfg in dv_fields_cfg.items():
            col = str(cfg.get("column") or f)
            val: str | None = None
            if args.dv_synthetic == "truth":
                for e in entries.get(t_of) or []:
                    s = cand._declared_raw_str((e.get("extra") or {}).get(col))
                    if s:
                        val = s
                        break
            else:  # rival — 1ª OF != verdade com a coluna preenchida
                key = (t_of, col)
                if key not in _rival_cache:
                    found: str | None = None
                    for k in sorted(entries):
                        if k == t_of:
                            continue
                        for e in entries[k]:
                            s = cand._declared_raw_str(
                                (e.get("extra") or {}).get(col))
                            if s:
                                found = s
                                break
                        if found:
                            break
                    _rival_cache[key] = found
                val = _rival_cache[key]
            if val:
                out[f] = val
        return out

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
    cal_samples: list[tuple[float, float, float]] = []
    model_rel_samples: list[tuple[float, float]] = []  # R247 — (p_top, acerto)
    post_samples: list[tuple[float, float]] = []  # R252 — (p_of posterior, acerto)
    # R253.5 — (stats suficientes, acerto, é_OOD) p/ o grid do posterior.
    post_stats_samples: list[tuple[dict, float, bool]] = []
    # R253.6 — (p_field, acerto) POR CAMPO, verdade da linha final.
    field_rel_samples: dict[str, list[tuple[float, float]]] = {
        "of": [], "ov": [], "cliente": [], "modelo": []}
    ood_stats = [0, 0, 0]  # [n, abstencoes, abstencoes_posterior (p_h0>p_of)]
    flips_path = args.out_dir / "flips.csv"
    model_flips_path = args.out_dir / "model_flips.csv"
    mf_fh = model_flips_path.open("w", newline="", encoding="utf-8")
    mw = csv.writer(mf_fh)
    mw.writerow(["set", "sheet_id", "row", "template", "truth_of",
                 "truth_modelo", "baseline_designacao", "candidate_designacao",
                 "cand_p_top", "cand_margin_bits", "cand_sibling_margin_bits",
                 "ocr_modelo"])
    with flips_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["set", "sheet_id", "row", "template", "truth_of",
                    "baseline_of", "candidate_of", "candidate_margin_bits",
                    "ocr_of", "ocr_ov", "ocr_modelo", "ocr_cliente"])
        t0 = time.time()
        for name, S in sets.items():
            is_model = name.startswith("MODEL")
            b_ok = c_ok = b_only = c_only = neither = errs = 0
            margins_ok: list[float] = []
            margins_bad: list[float] = []
            shift_detail: list[tuple[str, bool, bool]] = []
            dv_ws_bad = 0  # dv — winner ERRADO promovido weak→strong pelo termo
            for rec in S:
                sid, i, tpl, row, t_of = rec[:5]  # SHIFT traz a coluna-fonte em rec[5]
                if args.dv_synthetic and dv_fields_cfg:
                    row = _dv_inject(row, t_of)
                try:
                    # R253/F0 — o MESMO extra_bias nos dois lados: dar o prior
                    # de produção só ao candidato inflava-o ~+1.1pp TOTAL /
                    # +4.3pp SHIFT (provado HEAD-vs-HEAD em
                    # reports/backtest_winner_control_headvshead). Baselines
                    # pré-R242 continuam protegidos pelo except TypeError de
                    # _winner_of (caem na chamada sem extra_bias).
                    prod_bias = _prod_bias_for(sheet_dates.get(sid, ""))
                    b_of, _bm, _bb, b_w = _winner_of(
                        base, row, refs, idx_b, tpl, extra_bias=prod_bias)
                    c_of, mg, cb, c_w = _winner_of(
                        cand, row, refs, idx_c, tpl, extra_bias=prod_bias,
                        declared_vote=dv_arg)
                except Exception as exc:
                    errs += 1
                    print(f"  ERRO s{sid} r{i}: {exc}", file=sys.stderr)
                    continue
                if is_model:
                    # R247 — acerto ao nível da ENTRY: OF certa E designação
                    # consistente com a verdade humana do modelo.
                    t_mod = str(rec[5])
                    b_des = str((b_w or {}).get("designacao") or "")
                    c_des = str((c_w or {}).get("designacao") or "")
                    B = b_of == t_of and _model_truth_consistent(t_mod, b_des)
                    C = c_of == t_of and _model_truth_consistent(t_mod, c_des)
                    # R253/F0 — a reliability do MODELO mede a confiança da
                    # CÉLULA modelo (_p_field["modelo"] — é o que o gate de
                    # gravação consome na variante next), não o _p_top, que na
                    # next é a marginal da OF: o gap 0.993/0.776 do bucket
                    # 0.9-1.0 media a grandeza errada. Fallback _p_top para
                    # motores sem _p_field (pré-R252).
                    if name == "MODEL_SIB":
                        pf_mod = ((c_w or {}).get("_p_field") or {}).get("modelo")
                        if pf_mod is None:
                            pf_mod = (c_w or {}).get("_p_top")
                        if pf_mod is not None:
                            model_rel_samples.append(
                                (float(pf_mod), 1.0 if C else 0.0))
                    if B != C:
                        mw.writerow([
                            name, sid, i, tpl, t_of, t_mod[:40],
                            b_des[:48] or "-", c_des[:48] or "-",
                            (c_w or {}).get("_p_top"),
                            "" if mg is None else round(float(mg), 2),
                            (c_w or {}).get("_sibling_margin_bits"),
                            str(row.get("modelo"))[:32]])
                else:
                    B, C = b_of == t_of, c_of == t_of
                    if c_of and cb is not None:
                        cal_samples.append(
                            (float(cb), float(mg if mg is not None else 99.0),
                             1.0 if C else 0.0))
                    # R252 — reliability do POSTERIOR (marginal da OF), em
                    # paralelo com a logística (telemetria de transição).
                    _p_of_v2 = (c_w or {}).get("_p_of")
                    if c_of and _p_of_v2 is not None:
                        post_samples.append((float(_p_of_v2), 1.0 if C else 0.0))
                    # R253.5 — estatísticas suficientes p/ o grid 3D (OOD
                    # incluído: y=0 por construção — c_of nunca == t_of; a
                    # flag OOD alimenta a âncora do b_H0 e a abstenção).
                    _pst = (c_w or {}).get("_posterior_stats")
                    if _pst is not None:
                        post_stats_samples.append(
                            (_pst, 1.0 if C else 0.0, name == "OOD"))
                    # R253.6 — verdade POR CAMPO da linha final validada.
                    fin_row = (rec[5] if name in ("CORR", "GOOD", "ENG")
                               and len(rec) > 5 else None)
                    pf_all = (c_w or {}).get("_p_field") or {}
                    if fin_row and pf_all:
                        c_des = (c_w or {}).get("designacao")
                        for f in ("of", "ov", "cliente", "modelo"):
                            tv = str(fin_row.get(f) or "").strip()
                            pv = pf_all.get(f)
                            if not tv or pv is None:
                                continue
                            if f == "of":
                                yk = 1.0 if C else 0.0
                            elif f == "ov":
                                yk = 1.0 if cand._identifier_compact(
                                    (c_w or {}).get("ov")
                                ) == cand._identifier_compact(tv) else 0.0
                            elif f == "cliente":
                                yk = 1.0 if cand._cliente_compact(
                                    (c_w or {}).get("cliente")
                                ) == cand._cliente_compact(tv) else 0.0
                            else:
                                yk = 1.0 if (
                                    C and _model_truth_consistent(tv, c_des)
                                ) else 0.0
                            field_rel_samples[f].append((float(pv), yk))
                if name == "OOD":
                    ood_stats[0] += 1
                    ood_stats[1] += 0 if c_of else 1  # absteve = certo
                    # R252 — abstenção PROBABILÍSTICA: P(H0) > P(OF winner).
                    _p_h0 = (c_w or {}).get("_p_h0")
                    _p_of_v2 = (c_w or {}).get("_p_of")
                    if _p_h0 is not None and _p_of_v2 is not None:
                        ood_stats[2] += _p_h0 > _p_of_v2
                if name == "SHIFT" and len(rec) > 5:
                    shift_detail.append((str(rec[5]), B, C))
                b_ok += B
                c_ok += C
                b_only += B and not C
                c_only += C and not B
                neither += not B and not C
                # dv — métrica do quadrante perigoso (§5 do desenho): winner
                # ERRADO cuja margem passou de <4 (baseline) a >=4 com o
                # termo — strong indevido comprado pelo declarado.
                if (dv_arg is not None and not C and mg is not None
                        and float(mg) >= 4.0
                        and _bm is not None and float(_bm) < 4.0):
                    dv_ws_bad += 1
                if mg is not None:
                    (margins_ok if C else margins_bad).append(float(mg))
                if not is_model and B != C:
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
            if dv_arg is not None:
                summary["sets"][name]["dv_weak_to_strong_bad"] = dv_ws_bad
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
    mf_fh.close()

    # Total HISTÓRICO comparável (CORR+GOOD+ENG — a referência 89.2% do R236
    # foi medida sem SHIFT; linhas SHIFT podem duplicar as de CORR/ENG).
    core = [s for k, s in summary["sets"].items() if k in ("CORR", "GOOD", "ENG")]
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
    # R247 — gates ADITIVOS de modelo (não entram em `passed`: mantém a
    # comparabilidade com runs históricos; ler à parte, como ENG).
    msib = summary["sets"].get("MODEL_SIB") or {}
    mstr = summary["sets"].get("MODEL_STRONG") or {}
    gate_model = (
        True if not msib.get("n")
        else int(msib.get("candidate_ok") or 0) >= int(msib.get("baseline_ok") or 0)
    )
    gate_model_strong = (
        True if not mstr.get("n")
        else int(mstr.get("candidate_ok") or 0) >= int(mstr.get("baseline_ok") or 0)
    )
    summary["gate"] = {
        "good_100pct": good.get("candidate_ok") == good.get("n"),
        "total_not_worse": tot_c >= tot_b,
        "shift_not_worse": gate_shift,
        "model_not_worse": gate_model,
        "model_strong_not_worse": gate_model_strong,
        "passed": (
            good.get("candidate_ok") == good.get("n")
            and tot_c >= tot_b
            and gate_shift
        ),
    }
    # R247 — reliability da confiança do winner nas linhas MODEL_SIB: expõe
    # o sintoma "irmão errado verde com p_top 0.9+" e mede se o fix o fecha.
    if model_rel_samples:
        mbuckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for pv, y in model_rel_samples:
            mbuckets[min(9, int(pv * 10))].append((pv, y))
        model_rel = []
        for k in sorted(mbuckets):
            v = mbuckets[k]
            model_rel.append({
                "bucket": f"{k/10:.1f}-{(k+1)/10:.1f}", "n": len(v),
                "conf": round(sum(p for p, _ in v) / len(v), 3),
                "acc": round(sum(y for _, y in v) / len(v), 3),
            })
        summary["model_reliability"] = model_rel
        print("reliability MODEL_SIB (p_top do winner vs acerto de entry):")
        for r in model_rel:
            print(f"  {r['bucket']}: n={r['n']:4d} conf={r['conf']:.2f} acc={r['acc']:.2f}")
    # R243 — calibração (T, s_ood) por grid-search a minimizar o Brier e
    # relatório de reliability (|confiança − acerto| por bucket).
    if cal_samples:

        def _p(bits, margin, t, s_ood):
            eff = min(margin, bits - s_ood)
            return 1.0 / (1.0 + 2.0 ** (-eff / t))

        best_fit = None
        for t10 in range(10, 81, 5):
            for s_ood in range(0, 17):
                t = t10 / 10.0
                brier = sum(
                    (_p(b, m, t, float(s_ood)) - y) ** 2
                    for b, m, y in cal_samples
                ) / len(cal_samples)
                if best_fit is None or brier < best_fit[0]:
                    best_fit = (brier, t, float(s_ood))
        brier, t_fit, s_fit = best_fit
        buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for b, m, y in cal_samples:
            pv = _p(b, m, t_fit, s_fit)
            buckets[min(9, int(pv * 10))].append((pv, y))
        reliability = []
        max_gap = 0.0
        for k in sorted(buckets):
            v = buckets[k]
            conf = sum(p for p, _ in v) / len(v)
            acc = sum(y for _, y in v) / len(v)
            gap = abs(conf - acc)
            if len(v) >= 10:
                max_gap = max(max_gap, gap)
            reliability.append({"bucket": f"{k/10:.1f}-{(k+1)/10:.1f}",
                                "n": len(v), "conf": round(conf, 3),
                                "acc": round(acc, 3), "gap": round(gap, 3)})
        summary["calibration"] = {
            "n_samples": len(cal_samples), "brier": round(brier, 4),
            "temperature_bits": t_fit, "s_ood_bits": s_fit,
            "reliability": reliability,
            "max_gap_buckets_n>=10": round(max_gap, 3),
        }
        if ood_stats[0]:
            summary["ood"] = {
                "n": ood_stats[0], "abstencoes": ood_stats[1],
                "abstain_pct": round(100 * ood_stats[1] / ood_stats[0], 1),
                # R252 — abstenção probabilística do posterior (P(H0)>P(OF)).
                "abstencoes_posterior": ood_stats[2],
                "abstain_posterior_pct": round(
                    100 * ood_stats[2] / ood_stats[0], 1),
            }
        print(f"calibração: T={t_fit} s_ood={s_fit} brier={brier:.4f} "
              f"max_gap={max_gap:.3f} (n={len(cal_samples)})")
        for r in reliability:
            print(f"  {r['bucket']}: n={r['n']:4d} conf={r['conf']:.2f} acc={r['acc']:.2f} gap={r['gap']:.2f}")
        if ood_stats[0]:
            print(f"OOD: n={ood_stats[0]} abstenções={ood_stats[1]} ({summary['ood']['abstain_pct']}%) "
                  f"| posterior P(H0)>P(OF): {ood_stats[2]} ({summary['ood']['abstain_posterior_pct']}%)")
    # R252 — reliability do posterior softmax (marginal da OF do candidato),
    # medida em paralelo com a logística — é o número que decide o flip R252b.
    if post_samples:
        pbuckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for pv, y in post_samples:
            pbuckets[min(9, int(pv * 10))].append((pv, y))
        post_rel = []
        pmax_gap = 0.0
        for k in sorted(pbuckets):
            v = pbuckets[k]
            conf = sum(p for p, _ in v) / len(v)
            acc = sum(y for _, y in v) / len(v)
            gap = abs(conf - acc)
            if len(v) >= 10:
                pmax_gap = max(pmax_gap, gap)
            post_rel.append({"bucket": f"{k/10:.1f}-{(k+1)/10:.1f}",
                             "n": len(v), "conf": round(conf, 3),
                             "acc": round(acc, 3), "gap": round(gap, 3)})
        brier_post = sum((p - y) ** 2 for p, y in post_samples) / len(post_samples)
        # R252 — pós-calibração de PLATT (a,b sobre o logit do p_of) por
        # grid de log-loss: é o fit do flip — o motor consome via
        # calibration.posterior_platt_{a,b} quando gravado com --calibrate.
        import math as _m

        def _logit(p: float) -> float:
            p = min(max(p, 1e-6), 1 - 1e-6)
            return _m.log(p / (1 - p))

        def _sig(x: float) -> float:
            return 1.0 / (1.0 + _m.exp(-x))

        best_platt = None
        for a10 in range(2, 31, 2):
            for b10 in range(-40, 41, 4):
                a, b = a10 / 10.0, b10 / 10.0
                ll = 0.0
                for p, y in post_samples:
                    q = min(max(_sig(a * _logit(p) + b), 1e-6), 1 - 1e-6)
                    ll -= y * _m.log(q) + (1 - y) * _m.log(1 - q)
                if best_platt is None or ll < best_platt[0]:
                    best_platt = (ll, a, b)
        _ll, pa, pb = best_platt
        brier_cal = sum(
            (_sig(pa * _logit(p) + pb) - y) ** 2 for p, y in post_samples
        ) / len(post_samples)
        summary["posterior_reliability"] = {
            "n_samples": len(post_samples), "brier": round(brier_post, 4),
            "reliability": post_rel,
            "max_gap_buckets_n>=10": round(pmax_gap, 3),
            "platt": {"a": pa, "b": pb, "brier_calibrado": round(brier_cal, 4)},
        }
        print(f"reliability POSTERIOR (p_of): brier={brier_post:.4f} max_gap={pmax_gap:.3f} "
              f"| Platt(a={pa}, b={pb}) brier={brier_cal:.4f}")
        for r in post_rel:
            print(f"  {r['bucket']}: n={r['n']:4d} conf={r['conf']:.2f} acc={r['acc']:.2f} gap={r['gap']:.2f}")

        # R253.5 — fit do posterior sobre as estatísticas suficientes do
        # sweep, com a MESMA fórmula do motor. O b_H0 é ANCORADO na
        # evidência OOD real (quantil do b_max das linhas cuja verdade está
        # fora do plano, por bucket de campos escritos) — um grid livre de
        # Brier empurrava b_h0 para a borda (−16) e matava a abstenção (o
        # Brier de p_of não vê P(H0)). Seleção em DOIS estágios: candidatos
        # com Brier <= mínimo+0.002 (ruído) → o de MAIOR abstenção OOD.
        posterior_fit: dict | None = None
        if post_stats_samples:
            t_grid = sorted(
                float(k) for k in post_stats_samples[0][0]["by_t"])
            n_plan_fit = max(
                int(post_stats_samples[0][0].get("n_plan") or 0), 1)

            def _bucket_of(st: dict) -> str:
                nw = int(st.get("n_written") or 0)
                return ("0-2" if nw <= 2 else "3-4" if nw <= 4
                        else "5-6" if nw <= 6 else "7+")

            def _pq(vals: list[float], q: float) -> float:
                v = sorted(vals)
                return v[min(int(q * len(v)), len(v) - 1)]

            ood_bmax_by_bucket: dict[str, list[float]] = defaultdict(list)
            for st, _y, is_ood in post_stats_samples:
                if is_ood:
                    ood_bmax_by_bucket[_bucket_of(st)].append(
                        float(st["b_max"]))
            ood_bmax_all = [v for vs in ood_bmax_by_bucket.values()
                            for v in vs]

            def _p_h0_pair(st: dict, t: float, eff_by_bucket: dict,
                           eff_flat: float, a_floor: float
                           ) -> tuple[float, float]:
                """(p_of, p_h0) com b_h0_eff por bucket (âncora OOD)."""
                zp, wof = st["by_t"][f"{t:g}"]
                eff = eff_by_bucket.get(_bucket_of(st), eff_flat)
                w_h0 = st["odds_h0"] * 2.0 ** ((eff - st["b_max"]) / t)
                fb = st["floor_base"]
                b_fl = (fb + a_floor) if fb is not None else -10.0
                w_rest = st["n_rest"] * 2.0 ** ((b_fl - st["b_max"]) / t)
                zz = zp + w_h0 + w_rest
                if zz <= 0:
                    return 0.0, 0.0
                return wof / zz, w_h0 / zz

            candidates: list[tuple[float, float, dict]] = []
            for t3 in t_grid:
                for q in (0.5, 0.75, 0.9):
                    for dlt in (0.0, 2.0, 4.0):
                        eff_by_bucket = {
                            bk: _pq(vs, q) + dlt
                            for bk, vs in ood_bmax_by_bucket.items()
                            if len(vs) >= 15
                        }
                        eff_flat = (_pq(ood_bmax_all, q) + dlt
                                    if ood_bmax_all else 10.0)
                        for afl in range(-5, 6):
                            se_ = 0.0
                            abst = 0
                            n_ood = 0
                            for st, y, is_ood in post_stats_samples:
                                p_of3, p_h03 = _p_h0_pair(
                                    st, t3, eff_by_bucket, eff_flat,
                                    float(afl))
                                se_ += (p_of3 - y) ** 2
                                if is_ood:
                                    n_ood += 1
                                    abst += p_h03 > p_of3
                            candidates.append((
                                se_ / len(post_stats_samples),
                                (abst / n_ood) if n_ood else 0.0,
                                {"t": t3, "q": q, "delta": dlt,
                                 "a_floor": float(afl),
                                 "eff_by_bucket": eff_by_bucket,
                                 "eff_flat": eff_flat},
                            ))
            brier_min = min(c[0] for c in candidates)
            viable = [c for c in candidates if c[0] <= brier_min + 0.002]
            viable.sort(key=lambda c: (-c[1], c[0], abs(c[2]["a_floor"])))
            brier3, abst_rate, cfg = viable[0]
            t_post = cfg["t"]
            afl_post = cfg["a_floor"]
            # persistência na forma que o MOTOR consome: raw = eff −
            # T·log2(N_fit) — em runtime eff cresce ~log2(N) com o plano
            # (extremo de N amostras), que é o comportamento desejado.
            lgn = t_post * _m.log2(n_plan_fit)
            braw_post = round(cfg["eff_flat"] - lgn, 2)
            braw_by_bucket = {
                bk: round(eff - lgn, 2)
                for bk, eff in cfg["eff_by_bucket"].items()
            }

            def _p_fitted(st: dict) -> float:
                return _p_h0_pair(st, t_post, cfg["eff_by_bucket"],
                                  cfg["eff_flat"], afl_post)[0]

            fitted_pairs = [(_p_fitted(st), y)
                            for st, y, _o in post_stats_samples]
            brier_fit = sum(
                (p - y) ** 2 for p, y in fitted_pairs) / len(fitted_pairs)
            best_platt3 = None
            for a10 in range(2, 31, 2):
                for b10 in range(-40, 41, 4):
                    a3, b3 = a10 / 10.0, b10 / 10.0
                    ll3 = 0.0
                    for p, y in fitted_pairs:
                        q3 = min(max(_sig(a3 * _logit(p) + b3), 1e-6),
                                 1 - 1e-6)
                        ll3 -= y * _m.log(q3) + (1 - y) * _m.log(1 - q3)
                    if best_platt3 is None or ll3 < best_platt3[0]:
                        best_platt3 = (ll3, a3, b3)
            _ll3, pa3, pb3 = best_platt3
            brier_fit_cal = sum(
                (_sig(pa3 * _logit(p) + pb3) - y) ** 2 for p, y in fitted_pairs
            ) / len(fitted_pairs)
            posterior_fit = {
                "n_samples": len(post_stats_samples),
                "posterior_temperature_bits": t_post,
                "b_h0_raw_bits": braw_post,
                "b_h0_raw_by_n_fields": braw_by_bucket,
                "posterior_floor_a_bits": afl_post,
                "posterior_brier": round(brier_fit, 4),
                "posterior_ood_abstain_rate": round(abst_rate, 3),
                "posterior_fit_objective": (
                    f"brier<=min+0.002 → max abstenção OOD (q={cfg['q']}, "
                    f"delta={cfg['delta']}, n_plan_fit={n_plan_fit})"),
            }
            # Platt só persiste se NÃO piorar o Brier (o grid otimiza
            # log-loss; nas caudas pode degradar a métrica de decisão).
            if brier_fit_cal <= brier_fit + 1e-4:
                posterior_fit["posterior_platt_a"] = pa3
                posterior_fit["posterior_platt_b"] = pb3
                posterior_fit["posterior_brier_calibrado"] = round(
                    brier_fit_cal, 4)
            summary["posterior_fit"] = posterior_fit
            print(f"fit POSTERIOR (âncora OOD): T={t_post} "
                  f"b_h0_raw={braw_post} a_floor={afl_post} "
                  f"brier={brier_fit:.4f} abstenção OOD={abst_rate:.1%} "
                  f"(Platt a={pa3} b={pb3} → {brier_fit_cal:.4f}"
                  f"{'' if brier_fit_cal <= brier_fit + 1e-4 else ' — descartado'}) "
                  f"buckets={braw_by_bucket}")

        # R253.6 — reliability + Platt POR CAMPO (verdade da linha final).
        field_platt: dict[str, list[float]] = {}
        field_rel_summary: dict[str, dict] = {}
        for f, samp in field_rel_samples.items():
            if not samp:
                continue
            br_f = sum((p - y) ** 2 for p, y in samp) / len(samp)
            entry: dict = {"n": len(samp), "brier": round(br_f, 4)}
            if len(samp) >= 100:
                best_pf = None
                for a10 in range(2, 31, 2):
                    for b10 in range(-40, 41, 4):
                        a4, b4 = a10 / 10.0, b10 / 10.0
                        ll4 = 0.0
                        for p, y in samp:
                            q = min(max(_sig(a4 * _logit(p) + b4), 1e-6),
                                    1 - 1e-6)
                            ll4 -= y * _m.log(q) + (1 - y) * _m.log(1 - q)
                        if best_pf is None or ll4 < best_pf[0]:
                            best_pf = (ll4, a4, b4)
                _llf, paf, pbf = best_pf
                br_f_cal = sum(
                    (_sig(paf * _logit(p) + pbf) - y) ** 2 for p, y in samp
                ) / len(samp)
                # só persiste se MELHORAR o Brier do campo (o grid otimiza
                # log-loss; 'ov' p.ex. piorava ligeiramente).
                if br_f_cal < br_f - 1e-4:
                    field_platt[f] = [paf, pbf]
                    entry.update({"platt_a": paf, "platt_b": pbf,
                                  "brier_calibrado": round(br_f_cal, 4)})
            field_rel_summary[f] = entry
        if field_rel_summary:
            summary["field_reliability"] = field_rel_summary
            print("reliability POR CAMPO (p_field vs verdade final): "
                  + " | ".join(
                      f"{f}: n={v['n']} brier={v['brier']}"
                      + (f"→{v['brier_calibrado']}" if "platt_a" in v else "")
                      for f, v in field_rel_summary.items()))

        if getattr(args, "calibrate", False):
            import json as _json
            cp = _REPO / "lexicons" / "cross_params.json"
            params = _json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
            cal_prev = dict(params.get("calibration") or {})
            new_cal = dict(cal_prev)  # UPDATE, nunca substituir (R253.5)
            new_cal["fitted_at_run"] = str(args.out_dir)
            # A calibração LOGÍSTICA do R243 foi escolhida À MÃO na região
            # de decisão (chosen_by) CONTRA o Brier-mínimo — o fit novo não
            # a sobrescreve; fica como sugestão *_brier_min para leitura.
            if cal_prev.get("chosen_by"):
                new_cal.update({
                    "temperature_bits_brier_min": t_fit,
                    "s_ood_bits_brier_min": s_fit,
                    "brier_brier_min": round(brier, 4),
                })
            else:
                new_cal.update({
                    "temperature_bits": t_fit, "s_ood_bits": s_fit,
                    "brier": round(brier, 4),
                    "n_samples": len(cal_samples),
                })
            # Governança R244: piso de amostra p/ o posterior; backup do
            # dict anterior; proveniência do run.
            if posterior_fit and posterior_fit["n_samples"] >= 300:
                new_cal.update({k: v for k, v in posterior_fit.items()
                                if k != "n_samples"})
                new_cal["posterior_n_samples"] = posterior_fit["n_samples"]
            elif posterior_fit:
                print(f"AVISO: posterior NÃO gravado — n="
                      f"{posterior_fit['n_samples']} < 300 (piso R244)")
            if field_platt:
                new_cal["posterior_platt_by_field"] = field_platt
            params["calibration_previous"] = cal_prev
            params["calibration"] = new_cal
            cp.write_text(_json.dumps(params, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
            print(f"calibração gravada -> {cp}")

    (args.out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"TOTAL baseline {summary['total']['baseline_pct']}% | "
          f"candidato {summary['total']['candidate_pct']}% | "
          f"gate passed={summary['gate']['passed']}")
    print(f"outputs: {args.out_dir}/summary.json, {flips_path}")


if __name__ == "__main__":
    main()
