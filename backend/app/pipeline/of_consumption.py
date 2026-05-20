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
_lock = threading.Lock()


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
    """
    if not fases:
        return 0.0
    vals = [_to_num(v) for v in fases.values()]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else 0.0


def _kanban_consumption() -> dict[tuple[str, str], float]:
    """SQL: qtd consumida por (of, modelo upper) nas folhas validated.

    Só conta folhas validadas — folhas em extracted (à espera de
    revisão) NÃO contam ainda. Mantém conservador: só produção
    confirmada pelo operador.
    """
    out: dict[tuple[str, str], float] = {}
    try:
        with db.conn() as c:
            rows = c.execute(
                """SELECT pr.of, UPPER(pr.modelo) AS m, SUM(pr.qtd) AS q
                   FROM production_rows pr
                   JOIN sheets s ON s.id = pr.sheet_id
                   WHERE s.status = 'validated'
                     AND pr.of IS NOT NULL AND pr.modelo IS NOT NULL
                     AND pr.qtd IS NOT NULL
                   GROUP BY pr.of, UPPER(pr.modelo)"""
            ).fetchall()
        for r in rows:
            out[(str(r["of"]), str(r["m"]))] = float(r["q"] or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def get_consumption() -> dict[tuple[str, str], float]:
    """Devolve o consumption dict, cacheado 30s."""
    global _cache, _cache_at
    with _lock:
        now = time.time()
        if _cache is None or (now - _cache_at) > _CACHE_TTL_S:
            _cache = _kanban_consumption()
            _cache_at = now
        return _cache


def invalidate_cache() -> None:
    """Forçar refresh no próximo `get_consumption()`. Chamar após
    apply-of-entry ou sheet validate."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


def remaining(entry: dict, consumption: dict | None = None) -> float:
    """Quanto falta produzir desta entry. ≤ 0 = já cumprida; inf = não
    rastreável (sem quanttrp)."""
    if str(entry.get("fechado") or "0") in ("1", "True", "true"):
        return 0.0
    quanttrp = _to_num(entry.get("quanttrp"))
    if quanttrp is None or quanttrp <= 0:
        return float("inf")
    phase_max = _max_phase(entry.get("fases"))
    consumption = consumption if consumption is not None else get_consumption()
    key = (
        str(entry.get("_of") or entry.get("of") or "").strip(),
        str(entry.get("designacao") or "").strip().upper(),
    )
    kanban_qty = consumption.get(key, 0.0)
    return float(quanttrp) - phase_max - kanban_qty


def sort_entries_by_remaining(
    entries: list[dict],
    include_done: bool = False,
) -> list[dict]:
    """Devolve cópias das entries enriquecidas com `_remaining`,
    `_quanttrp`, `_done` + ordenadas ascendente por remaining.

    include_done=False filtra entries com remaining ≤ 0.
    Entries com remaining=inf (sem quanttrp) vão para o fim.
    """
    consumption = get_consumption()
    enriched: list[dict] = []
    for e in entries:
        rem = remaining(e, consumption)
        e2 = dict(e)
        e2["_remaining"] = None if rem == float("inf") else rem
        e2["_quanttrp"] = _to_num(e.get("quanttrp"))
        e2["_done"] = rem <= 0
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
    "sort_entries_by_remaining",
    "get_consumption",
    "invalidate_cache",
]
