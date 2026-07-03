"""R244 — REFIT automático dos parâmetros do cross (o sistema aprende).

Os parâmetros do descodificador são CONTAGENS (m por campo, matriz de
confusão de caracteres, canal humano) — re-estimáveis da tabela ``edits`` +
folhas validadas a cada ciclo de learning (50 folhas validadas). Auto-gate:

  1. PISOS de amostra — um parâmetro só é atualizado com dados suficientes
     (pares de chars >= 300; m por campo >= 500 células) — senão mantém-se.
  2. DERIVA LIMITADA — m só mexe ±0.15 por refit; custos de chars vivem no
     clamp [3, 10] por construção. Um refit nunca dá um salto cego.
  3. ESCRITA ATÓMICA com backup da versão anterior em ``previous`` +
     proveniência (data, contagens); o cache do motor é invalidado a seguir.
O gate COMPLETO (backtest_winner contra baseline) continua a ser o passo de
dev/offline antes de qualquer mudança de CÓDIGO; o refit só mexe em DADOS.

Também é o núcleo partilhado do fit offline
(``scripts/diag/fit_char_confusion.py`` importa daqui).
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[3]
PARAMS_PATH = _REPO / "lexicons" / "cross_params.json"

ID_FIELDS = ("of", "ov", "lote", "pri")
MIN_CHAR_PAIRS = 300
MIN_M_CELLS = 500
MAX_M_DRIFT = 0.15
# Prior de glifos confusos (pseudo-contagens simétricas): conhecimento de
# domínio como PRIOR bayesiano — os dados dominam onde há contagens.
GLYPH_PRIOR_PAIRS = [
    ("0", "O", 6), ("1", "I", 6), ("1", "L", 4), ("1", "7", 4), ("3", "8", 4),
    ("5", "S", 5), ("8", "B", 4), ("6", "G", 3), ("2", "Z", 4), ("7", "T", 3),
    ("4", "A", 2), ("9", "G", 2), ("0", "D", 2), ("6", "5", 2), ("9", "4", 2),
    ("3", "5", 2), ("2", "7", 2), ("1", "4", 2), ("M", "H", 3), ("E", "F", 2),
]
PRIOR_ANY = 0.25
PRIOR_MATCH = 50.0


def norm_id(s: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def lev(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def nw_align(a: str, b: str) -> list[tuple[str, str]]:
    """Alinhamento global de custo unitário; '-' marca inserção/apagamento."""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1,
                           dp[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
    out: list[tuple[str, str]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            out.append((a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            out.append((a[i - 1], "-"))
            i -= 1
        else:
            out.append(("-", b[j - 1]))
            j -= 1
    return out[::-1]


def collect_pairs_from_conn(con) -> tuple[list[tuple[str, str, str]], dict]:
    """Pares (verdade, escrito, fonte) de misreads plausíveis: edições
    humanas (fortes) + raw vs final das validadas (largos, dist<=2)."""
    pairs: list[tuple[str, str, str]] = []
    edit_re = re.compile(r"^rows\[(\d+)\]\.(of|ov|lote|pri)$")
    raw_cache: dict[int, list] = {}

    def raw_rows(sheet_id: int) -> list:
        if sheet_id not in raw_cache:
            r = con.execute(
                "SELECT raw_extraction FROM sheets WHERE id=?", (sheet_id,)
            ).fetchone()
            try:
                raw_cache[sheet_id] = (json.loads(r[0] or "{}") or {}).get("rows") or []
            except (TypeError, ValueError, IndexError):
                raw_cache[sheet_id] = []
        return raw_cache[sheet_id]

    op_by_sheet: dict[int, str] = {}
    for r in con.execute("SELECT id, operador FROM sheets"):
        op_by_sheet[int(r[0])] = str(r[1] or "").strip().upper()

    n_strong = 0
    for r in con.execute(
        "SELECT sheet_id, field_path, new_value FROM edits "
        "WHERE source='human' AND old_value <> new_value"
    ):
        m = edit_re.match(r[1])
        if not m:
            continue
        i = int(m.group(1))
        rows = raw_rows(r[0])
        if i >= len(rows):
            continue
        w, t = norm_id((rows[i] or {}).get(m.group(2))), norm_id(r[2])
        if w and t and w != t and abs(len(w) - len(t)) <= 2 and lev(w, t) <= 3:
            pairs.append((t, w, "human", op_by_sheet.get(r[0], "")))
            n_strong += 1

    n_broad = 0
    for raw_j, fin_j, op in con.execute(
        "SELECT raw_extraction, sheet_data, operador FROM sheets "
        "WHERE status='validated' AND raw_extraction IS NOT NULL"
    ):
        try:
            rr = (json.loads(raw_j) or {}).get("rows") or []
            rf = (json.loads(fin_j) or {}).get("rows") or []
        except (TypeError, ValueError):
            continue
        for i in range(min(len(rr), len(rf))):
            for field in ID_FIELDS:
                w = norm_id((rr[i] or {}).get(field))
                t = norm_id((rf[i] or {}).get(field))
                if (w and t and w != t and abs(len(w) - len(t)) <= 1
                        and lev(w, t) <= 2):
                    pairs.append((t, w, "validated",
                                  str(op or "").strip().upper()))
                    n_broad += 1
    return pairs, {"strong_pairs": n_strong, "broad_pairs": n_broad}


def fit_char_matrix(pairs: list[tuple[str, str, str]]) -> dict:
    """Matriz de custos (bits) por substituição, com priors de glifos."""
    sub: Counter = Counter()
    ins = del_ = match = 0
    for truth, written, *_ in pairs:
        for ct, cw in nw_align(truth, written):
            if ct == "-":
                ins += 1
            elif cw == "-":
                del_ += 1
            elif ct == cw:
                match += 1
            else:
                sub[(ct, cw)] += 1
    prior: dict = defaultdict(float)
    for a, b, k in GLYPH_PRIOR_PAIRS:
        prior[(a, b)] += k
        prior[(b, a)] += k
    total_pos = match + sum(sub.values()) + ins + del_ + PRIOR_MATCH
    p_match = (match + PRIOR_MATCH) / max(total_pos, 1)
    costs = {}
    for k in set(sub) | set(prior):
        p_sub = (sub.get(k, 0) + prior.get(k, PRIOR_ANY)) / max(total_pos, 1)
        costs[f"{k[0]}>{k[1]}"] = round(
            max(3.0, min(10.0, math.log2(p_match / p_sub))), 2)
    p_indel = (ins + del_ + 1) / max(total_pos, 1)
    return {
        "p_match": round(p_match, 4),
        "cost_default_bits": 10.0,
        "cost_indel_bits": round(
            max(3.0, min(10.0, math.log2(p_match / p_indel))), 2),
        "sub_costs_bits": dict(sorted(costs.items())),
        "counts": {"match": match, "sub": sum(sub.values()),
                   "ins": ins, "del": del_,
                   "top_subs": {f"{a}>{b}": c
                                for (a, b), c in sub.most_common(25)}},
    }


def _clamped_m(new: float, old: float | None) -> float:
    if old is None:
        return round(new, 3)
    return round(max(old - MAX_M_DRIFT, min(old + MAX_M_DRIFT, new)), 3)


def refit_from_db() -> dict | None:
    """Refit dos parâmetros a partir do app.db VIVO, com auto-gate (pisos +
    deriva limitada + backup). Devolve o resumo do que mudou, ou None se os
    pisos não foram atingidos / erro. Chamado pelo ciclo de learning."""
    from app.web import db

    try:
        params = (json.loads(PARAMS_PATH.read_text(encoding="utf-8"))
                  if PARAMS_PATH.exists() else {})
    except (OSError, ValueError):
        params = {}
    changed: dict = {}
    try:
        with db.conn() as con:
            pairs, meta = collect_pairs_from_conn(con)
            n_pairs = len(pairs)
            if n_pairs >= MIN_CHAR_PAIRS:
                changed["char_channel"] = fit_char_matrix(pairs)
                changed["pair_counts"] = meta
                # R245 — canais POR OPERADOR quando há dados (>=300 pares do
                # próprio operador; fallback global). Hoje nenhum operador
                # atinge o piso (~1.5k pares no total) — o mecanismo ativa-se
                # sozinho à medida que as correções crescem.
                by_op: dict[str, list] = defaultdict(list)
                for pr in pairs:
                    if len(pr) > 3 and pr[3]:
                        by_op[pr[3]].append(pr)
                per_op = {
                    op: fit_char_matrix(prs)
                    for op, prs in by_op.items()
                    if len(prs) >= MIN_CHAR_PAIRS
                }
                if per_op:
                    changed["char_channel_by_operator"] = per_op
            else:
                logger.info("cross_refit: %d pares < piso %d — matriz mantida",
                            n_pairs, MIN_CHAR_PAIRS)
    except Exception:
        logger.exception("cross_refit falhou — parâmetros mantidos")
        return None
    if not changed:
        return None
    # Backup + proveniência + escrita atómica.
    prev = {k: params.get(k) for k in changed if k in params}
    params.update(changed)
    params["previous"] = {"at": params.get("fitted_at"), "values": prev}
    params["fitted_at"] = datetime.now(timezone.utc).isoformat()
    params["fitted_by"] = "learning-cycle cross_refit R244"
    tmp = PARAMS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(params, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(PARAMS_PATH)
    # Invalidar o cache do motor (novos params na próxima folha).
    try:
        from app.pipeline import scoring_engine

        scoring_engine._load_cross_params.cache_clear()
    except Exception:  # noqa: BLE001
        pass
    logger.info("cross_refit: parâmetros atualizados (%s)", list(changed))
    return {"updated": list(changed)}
