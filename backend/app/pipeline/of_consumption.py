"""R113 — Calcula quanto falta produzir por entry do plan.

`remaining(entry) = quanttrp - max(fases) - qtd_consumida_pelas_kanbans`

A ideia: quando o operador (ou o motor v5) precisa de escolher entre N
entries do mesmo OF (várias peças do mesmo modelo, só muda o número),
preferir as que estão mais perto de fechar — operador acaba uma peça,
passa para a próxima.

Cache TTL 30s para evitar SQL repetido em cada cross-check / lookup.
Invalidação manual via `invalidate_cache()` chamada após apply ou
validação de uma folha.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.web import db


_CACHE_TTL_S = 30.0
_cache: dict[tuple[str, str], float] | None = None
_cache_at: float = 0.0
_cache_cutoff: str | None = None
_lock = threading.Lock()


def _plan_cutoff_iso() -> str | None:
    """Data (ISO) do snapshot do plano carregado — o corte temporal do
    consumo de kanbans.

    FIX do double counting da wizard: as fases bf/c/q/s/r/a/exp do plano
    são a produção JÁ registada no ERP até ao snapshot, e este próprio
    sistema alimenta o CPIS — subtrair TODAS as kanbans validadas contava
    a mesma produção 2× e marcava obras abertas como fechadas. Regra:
    "o ERP sabe tudo até ao snapshot; o local sabe o que aconteceu
    depois" → só contam kanbans com sheet_iso_date >= data do plano
    (INCLUSIVO: assume export matinal; errar para 'ainda aberta' é
    benigno, errar para 'fechada' é o bug). Sem plano/mtime → None
    (sem corte, comportamento antigo)."""
    try:
        from app.cross_check.ref_watcher import get_watcher  # lazy — sem ciclo

        pm = float((get_watcher().get_refs() or {}).get("plan_mtime") or 0.0)
        if pm <= 0:
            return None
        import datetime as _dt

        return _dt.date.fromtimestamp(pm).isoformat()
    except Exception:  # noqa: BLE001 — corte é proteção, nunca bloqueia
        return None


def _to_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _max_phase(fases: dict | None) -> float:
    """Máximo das fases — qtd já produzida segundo o ERP.

    As fases representam a mesma linha de produção a passar por estágios
    (corte, soldadura, acabamento, etc.); cada fase reporta quantas
    peças já passaram. Pegamos no max porque a fase mais avançada é a
    que está mais perto da conclusão. NÃO somar.

    R138 — NOTA: para o cálculo de `remaining` (wizard) deixou de se usar
    o MAX. As fases iniciais (bf/corte) sobre-produzem (margem de sucata),
    pelo que o max ≥ quanttrp em ~92% das linhas e marcava quase tudo como
    fechado. Usar `_produced` (fase do setor / expedição). `_max_phase`
    permanece porque `obras_status.py` ainda o usa.
    """
    if not fases:
        return 0.0
    vals = [_to_num(v) for v in fases.values()]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else 0.0


def _produced(fases: dict | None, phase: str | None) -> float:
    """R138 — qtd produzida relevante para "quanto falta".

    As fases do plan (`bf, c, q, s, r, a, exp`) são estágios SEQUENCIAIS;
    as peças fluem da esquerda (corte/formato, que sobre-produz) para a
    direita (expedição). O MAX pega no estágio a montante sobre-produzido
    → marca a linha como concluída cedo demais.

    - `phase` dado (etapa do kanban, via setor→colunaexcel) e presente nas
      fases → usa essa fase: "concluída" só quando o setor do operador
      atingiu a quantidade.
    - Sem `phase` (setor sem mapeamento) → cai na fase mais a JUSANTE
      (último valor de `fases`, tipicamente `exp`/expedição), que é a
      medida conservadora de "já saiu da fábrica".
    - Valores NEGATIVOS (estornos/acertos do ERP — vistos exp=-5 no plano
      real) são clamped a 0 no PONTO DE USO: produção negativa não existe;
      sem o clamp, exp=-5 daria remaining = quanttrp+5. O valor cru
      continua visível em /obras como sinal de dados ERP anómalos.
    """
    if not fases:
        return 0.0
    if phase and phase in fases:
        v = _to_num(fases.get(phase))
        return max(v, 0.0) if v is not None else 0.0
    # Fallback: fase mais a jusante (último valor; `fases` vem em ordem de
    # folha, esquerda→direita, por isso o último é o mais avançado).
    vals = list(fases.values())
    for v in reversed(vals):
        n = _to_num(v)
        if n is not None:
            return max(n, 0.0)
    return 0.0


def _kanban_consumption(cutoff_iso: str | None = None) -> dict[tuple[str, str], float]:
    """SQL: qtd consumida por (of, modelo upper) nas folhas validated.

    Só conta folhas validadas — folhas em extracted (à espera de
    revisão) NÃO contam ainda. Mantém conservador: só produção
    confirmada pelo operador.

    `cutoff_iso` (data do snapshot do plano): só contam linhas com
    sheet_iso_date >= cutoff — as anteriores JÁ estão nas fases do ERP
    (ver _plan_cutoff_iso). Usa a data de PRODUÇÃO (sheet_iso_date, com
    fallback para captura na ingestão) e não validated_at: validar hoje
    uma semana de kanbans atrasadas não pode reintroduzir a dupla
    contagem de produção que o ERP já conhece.
    """
    out: dict[tuple[str, str], float] = {}
    sql = """SELECT pr.of, UPPER(pr.modelo) AS m, SUM(pr.qtd) AS q
             FROM production_rows pr
             JOIN sheets s ON s.id = pr.sheet_id
             WHERE s.status = 'validated'
               AND pr.of IS NOT NULL AND pr.modelo IS NOT NULL
               AND pr.qtd IS NOT NULL"""
    args: tuple = ()
    if cutoff_iso:
        sql += " AND pr.sheet_iso_date >= ?"
        args = (cutoff_iso,)
    sql += " GROUP BY pr.of, UPPER(pr.modelo)"
    try:
        with db.conn() as c:
            rows = c.execute(sql, args).fetchall()
        for r in rows:
            out[(str(r["of"]), str(r["m"]))] = float(r["q"] or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def get_consumption() -> dict[tuple[str, str], float]:
    """Devolve o consumption dict, cacheado 30s (refresh imediato quando
    o snapshot do plano muda — o cutoff faz parte da chave do cache)."""
    global _cache, _cache_at, _cache_cutoff
    cutoff = _plan_cutoff_iso()
    with _lock:
        now = time.time()
        if (
            _cache is None
            or (now - _cache_at) > _CACHE_TTL_S
            or cutoff != _cache_cutoff
        ):
            _cache = _kanban_consumption(cutoff)
            _cache_at = now
            _cache_cutoff = cutoff
        return _cache


def invalidate_cache() -> None:
    """Forçar refresh no próximo `get_consumption()`. Chamar após
    apply-of-entry, sheet validate ou reload das refs."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


# R242/D1 — prior de PRODUÇÃO: OFs com atividade validada recente são a
# priori mais prováveis (medido: P(ativa 14d | OF verdadeira)=71.2% vs 2.2%
# para uma OF aleatória do plano — razão 32×; quant_context_priors.py).
_activity_cache: tuple[float, str, frozenset[str]] | None = None
_ACTIVITY_TTL_S = 300.0


def recent_active_ofs(
    as_of: str | None = None, window_days: int = 14
) -> frozenset[str]:
    """OFs (6 dígitos, zero-padded) com produção VALIDADA em
    [as_of−window, as_of), ESTRITAMENTE antes do dia — a produção do próprio
    dia nunca alimenta o prior (anti-circularidade: a escolha de hoje do
    motor não se pode reforçar a si própria hoje). ``as_of`` ISO YYYY-MM-DD;
    default = hoje. Falha → frozenset() (prior desliga-se sozinho)."""
    global _activity_cache
    import datetime as _dt

    day = as_of or _dt.date.today().isoformat()
    with _lock:
        now = time.time()
        if (_activity_cache is not None
                and _activity_cache[1] == day
                and now - _activity_cache[0] <= _ACTIVITY_TTL_S):
            return _activity_cache[2]
    lo = (_dt.date.fromisoformat(day)
          - _dt.timedelta(days=window_days)).isoformat()
    out: set[str] = set()
    try:
        with db.conn() as c:
            rows = c.execute(
                """SELECT DISTINCT pr.of FROM production_rows pr
                   WHERE pr.sheet_status = 'validated'
                     AND pr.of IS NOT NULL
                     AND pr.sheet_iso_date >= ? AND pr.sheet_iso_date < ?""",
                (lo, day),
            ).fetchall()
        for r in rows:
            s = "".join(ch for ch in str(r["of"]) if ch.isdigit())
            if s:
                out.add(s.zfill(6) if len(s) <= 6 else s)
    except Exception:  # noqa: BLE001
        return frozenset()
    result = frozenset(out)
    with _lock:
        _activity_cache = (time.time(), day, result)
    return result


def remaining(
    entry: dict, consumption: dict | None = None, phase: str | None = None
) -> float:
    """Quanto falta produzir desta entry. ≤ 0 = já cumprida; inf = não
    rastreável (sem quanttrp).

    R138 — `phase` é a etapa do kanban (setor→colunaexcel). Quando dada,
    "produzido" = a fase desse setor; sem ela, cai na fase mais a jusante
    (expedição). Substitui o antigo `_max_phase`, que sobre-contava as
    fases iniciais e marcava ~92% das linhas como fechadas.
    """
    if str(entry.get("fechado") or "0") in ("1", "True", "true"):
        return 0.0
    quanttrp = _to_num(entry.get("quanttrp"))
    if quanttrp is None or quanttrp <= 0:
        return float("inf")
    produced = _produced(entry.get("fases"), phase)
    consumption = consumption if consumption is not None else get_consumption()
    key = (
        str(entry.get("_of") or entry.get("of") or "").strip(),
        str(entry.get("designacao") or "").strip().upper(),
    )
    kanban_qty = consumption.get(key, 0.0)
    return float(quanttrp) - produced - kanban_qty


def _entry_key(entry: dict) -> tuple[str, str]:
    return (
        str(entry.get("_of") or entry.get("of") or "").strip(),
        str(entry.get("designacao") or "").strip().upper(),
    )


def annotate_remaining(
    entries: list[dict],
    consumption: dict | None = None,
    phase: str | None = None,
) -> list[float]:
    """Remaining por entry com repartição WATERFALL do consumo entre
    entries IRMÃS (mesma chave (of, designação)).

    FIX do smear: o consumo agregado por chave era subtraído POR INTEIRO
    a CADA irmã — 3 entries de 10 com 12 produzidas fechavam TODAS
    (faltando 18). Waterfall pela ordem de input (= ordem do plano):
    cada irmã absorve min(o que lhe falta, pool restante); a SOBRA final
    vai à última (preserva a semântica "remaining pode ser negativo" e
    conserva o total). Grupo de 1 ≡ `remaining()`.
    """
    if consumption is None:
        consumption = get_consumption()
    # pool restante por chave (só das chaves presentes)
    pools: dict[tuple[str, str], float] = {}
    counts: dict[tuple[str, str], int] = {}
    for e in entries:
        k = _entry_key(e)
        if k not in pools:
            pools[k] = float(consumption.get(k, 0.0))
        counts[k] = counts.get(k, 0) + 1
    seen: dict[tuple[str, str], int] = {}
    out: list[float] = []
    for e in entries:
        if str(e.get("fechado") or "0") in ("1", "True", "true"):
            out.append(0.0)
            continue
        quanttrp = _to_num(e.get("quanttrp"))
        if quanttrp is None or quanttrp <= 0:
            out.append(float("inf"))
            continue
        k = _entry_key(e)
        seen[k] = seen.get(k, 0) + 1
        need = float(quanttrp) - _produced(e.get("fases"), phase)
        if seen[k] >= counts[k]:
            alloc = pools[k]                       # última irmã leva a sobra
        else:
            alloc = min(max(need, 0.0), pools[k])
        pools[k] -= alloc
        out.append(need - alloc)
    return out


def sort_entries_by_remaining(
    entries: list[dict],
    include_done: bool = False,
    phase: str | None = None,
) -> list[dict]:
    """Devolve cópias das entries enriquecidas com `_remaining`,
    `_quanttrp`, `_done` + ordenadas ascendente por remaining.

    include_done=False filtra entries com remaining ≤ 0.
    Entries com remaining=inf (sem quanttrp) vão para o fim.

    R138 — `phase` (etapa do kanban) torna o "done" consciente do setor:
    uma linha só está concluída quando a fase desse setor atingiu quanttrp.
    Consumo repartido por waterfall entre irmãs (ver annotate_remaining).
    """
    consumption = get_consumption()
    rems = annotate_remaining(entries, consumption, phase)
    enriched: list[dict] = []
    for e, rem in zip(entries, rems):
        e2 = dict(e)
        e2["_remaining"] = None if rem == float("inf") else rem
        e2["_quanttrp"] = _to_num(e.get("quanttrp"))
        e2["_done"] = rem <= 0
        # decomposição para o tooltip da wizard (confiança do operador)
        e2["_produced_erp"] = _produced(e.get("fases"), phase)
        e2["_kanban_qty"] = consumption.get(_entry_key(e), 0.0)
        if not include_done and rem <= 0:
            continue
        enriched.append(e2)
    enriched.sort(key=lambda x: (
        float("inf") if x.get("_remaining") is None else x["_remaining"],
        x.get("_quanttrp") or float("inf"),
    ))
    return enriched


__all__ = [
    "remaining",
    "annotate_remaining",
    "sort_entries_by_remaining",
    "get_consumption",
    "invalidate_cache",
]
