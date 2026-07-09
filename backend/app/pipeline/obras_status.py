"""R115 — agregação de status de obras por cliente e por OV.

Junta a verdade do plano (refs do ref_watcher, mined de
``plan_colunas_cpis.xlsx``) com a verdade do chão (kanbans validadas em
``production_rows`` + ``sheets``). Devolve, num único agregado cacheado,
o ponto-de-situação de cada cliente e de cada OV:

* quantas peças foram planeadas, produzidas e quantas faltam
* em que fase cada peça está (bf/c/q/s/r/a/exp — somatórios por OV)
* qual o último kanban que registou movimento na OV
  (sheet_id + captured_at + operador + setor)

A página /obras consome isto. Cache TTL 60s — recalcular em cada hit
seria caro (~10k entries no plan, ~600 sheets validated). Invalidação
manual via :func:`invalidate_cache` chamada após sheet validate ou
refs reload.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from app.cross_check.ref_watcher import get_watcher
from app.pipeline.of_consumption import (
    _to_num,
    annotate_remaining,
    get_consumption,
    remaining,
)
from app.web import db


_CACHE_TTL_S = 60.0
_cache: dict[str, Any] | None = None
_cache_at: float = 0.0
_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def _phase_keys(entries: list[dict]) -> list[str]:
    """Union de todas as chaves de fases vistas nas entries, em ordem
    estável (a ordem em que aparecem na primeira entry que cada chave
    aparece).

    Em produção real é sempre ``bf, c, q, s, r, a, exp`` — mas o
    ``_phase_columns`` do ref_watcher é dinâmico (deriva de headers
    entre ``quanttrp`` e ``esp``), por isso espelhamos o mesmo
    comportamento aqui em vez de hardcoded. Algumas entries podem ter
    sub-conjuntos diferentes — unimos todas.
    """
    seen: dict[str, None] = OrderedDict()
    for e in entries:
        for k in (e.get("fases") or {}).keys():
            if k not in seen:
                seen[k] = None
    return list(seen.keys())


def _produced_from_remaining(quanttrp: float, rem: float) -> float:
    if rem == float("inf") or quanttrp <= 0:
        return 0.0
    return min(quanttrp, max(quanttrp - rem, 0.0))


def _entry_done(entry: dict, consumption: dict | None = None) -> bool:
    if str(entry.get("fechado") or "0") in ("1", "True", "true"):
        return True
    return remaining(entry, consumption) <= 0


# ---------------------------------------------------------------- indexes

def build_ov_index(refs: dict) -> dict[str, list[dict]]:
    """``ov_str -> [entries…]`` derivado de ``refs.of_to_entries``.

    O ref_watcher já indexa por cliente e por OF mas não por OV. Cada
    entry mantém o ``_of`` para o cálculo de ``remaining`` funcionar.
    """
    out: dict[str, list[dict]] = {}
    for of_str, entries in (refs.get("of_to_entries") or {}).items():
        for e in entries:
            ov = str(e.get("ov") or "").strip()
            if not ov:
                continue
            out.setdefault(ov, []).append({**e, "_of": of_str})
    return out


def _kanbans_by_ov() -> dict[str, list[dict]]:
    """``ov_str -> [{sheet_id, captured_at, operador, setor_maquina, qtd,
    modelo, of}…]`` ordenados por ``captured_at DESC``.

    JOIN com ``sheets`` para extrair ``setor_maquina`` do JSON
    (``production_rows`` não desnormaliza esse campo).
    """
    out: dict[str, list[dict]] = {}
    try:
        with db.conn() as c:
            rows = c.execute(
                """SELECT pr.ov, pr.of, pr.modelo, pr.qtd,
                          pr.operador, pr.captured_at, pr.sheet_iso_date,
                          pr.sheet_id,
                          json_extract(s.sheet_data, '$.header.setor_maquina') AS setor_maquina
                     FROM production_rows pr
                     JOIN sheets s ON s.id = pr.sheet_id
                    WHERE s.status = 'validated'
                      AND pr.ov IS NOT NULL AND TRIM(pr.ov) <> ''
                    ORDER BY pr.captured_at DESC"""
            ).fetchall()
        for r in rows:
            ov = str(r["ov"]).strip()
            if not ov:
                continue
            out.setdefault(ov, []).append({
                "sheet_id": r["sheet_id"],
                "of": r["of"],
                "modelo": r["modelo"],
                "qtd": r["qtd"],
                "operador": r["operador"],
                "captured_at": r["captured_at"],
                "sheet_iso_date": r["sheet_iso_date"],
                "setor_maquina": r["setor_maquina"],
            })
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- aggregates

def aggregate_ov(
    ov: str,
    entries: list[dict],
    kanbans: list[dict],
    consumption: dict | None = None,
    slim: bool = False,
) -> dict:
    """Agrega uma OV: soma planeadas/produzidas, fases acumuladas,
    último kanban, lista de peças.

    ``kanbans`` é a lista já ordenada por ``captured_at DESC`` (output de
    :func:`_kanbans_by_ov`).

    ``slim=True`` omite ``entries``, ``recent_kanbans`` e ``fase_totals``
    para reduzir payload na vista de cliente (drill-down faz fetch ao
    detalhe individual via :func:`get_ov_detail`).
    """
    consumption = consumption if consumption is not None else get_consumption()

    cliente = ""
    ofs: set[str] = set()
    modelos: set[str] = set()
    planned = 0.0
    produced = 0.0
    fase_keys = _phase_keys(entries)
    fase_totals: OrderedDict[str, float] = OrderedDict((k, 0.0) for k in fase_keys)

    entry_rows: list[dict] = []
    open_entries = 0
    closed_entries = 0

    # Waterfall entre entries IRMÃS da mesma (of, designação) — o agregado
    # de kanbans deixou de ser subtraído por inteiro a cada irmã (smear que
    # fechava todas; ver of_consumption.annotate_remaining). O grupo de
    # irmãs está completo no âmbito da OV (partilham OF→OV).
    rems = annotate_remaining(entries, consumption)

    for e, rem in zip(entries, rems):
        cliente = cliente or (e.get("cliente") or "")
        if e.get("_of"):
            ofs.add(str(e["_of"]))
        elif e.get("of"):
            ofs.add(str(e["of"]))
        if e.get("designacao"):
            modelos.add(str(e["designacao"]))

        q = _to_num(e.get("quanttrp")) or 0.0
        # Use the same downstream/phase-aware remaining semantics as
        # of_consumption. This avoids marking Acabamento as complete just
        # because an upstream phase over-produced.
        produced_entry = _produced_from_remaining(q, rem)
        planned += q
        produced += produced_entry
        for k in fase_keys:
            fase_totals[k] += _to_num((e.get("fases") or {}).get(k)) or 0.0

        rem_out = None if rem == float("inf") else rem
        # done pelo rem do waterfall (annotate_remaining já devolve 0.0
        # para entries com flag `fechado` do plano).
        done = rem <= 0
        if done:
            closed_entries += 1
        else:
            open_entries += 1

        entry_rows.append({
            "of": e.get("_of") or e.get("of"),
            "designacao": e.get("designacao"),
            "comp": e.get("comp"),
            "lbase": e.get("lbase"),
            "ltopo": e.get("ltopo"),
            "esp": e.get("esp"),
            "material": e.get("material"),
            "quanttrp": q if q > 0 else None,
            "produced": produced_entry,
            "remaining": rem_out,
            "done": done,
            "fases": dict(e.get("fases") or {}),
            "fechado": str(e.get("fechado") or "0") in ("1", "True", "true"),
        })

    # Peças a faltar (open) primeiro, ordenadas por faltam menos.
    def _sort_key(row: dict) -> tuple[int, float, float]:
        # group 0 = open, 1 = done (vão para o fim)
        group = 1 if row["done"] else 0
        rem = row["remaining"]
        rem_val = float("inf") if rem is None else float(rem)
        q = float(row["quanttrp"] or float("inf"))
        return (group, rem_val, q)

    entry_rows.sort(key=_sort_key)

    last_kb = kanbans[0] if kanbans else None
    pct = (produced / planned * 100.0) if planned > 0 else 0.0

    out = {
        "ov": ov,
        "cliente": cliente,
        "ofs": sorted(ofs),
        "modelos": sorted(modelos),
        "planned": planned,
        "produced": produced,
        "remaining": max(planned - produced, 0.0),
        "pct_complete": round(pct, 1),
        "n_entries": len(entries),
        "n_open": open_entries,
        "n_closed": closed_entries,
        "all_closed": open_entries == 0 and closed_entries > 0,
        "phase_keys": fase_keys,
        "last_kanban": last_kb,
        "n_kanbans": len(kanbans),
    }
    if not slim:
        out["fase_totals"] = dict(fase_totals)
        out["recent_kanbans"] = kanbans[:5]
        out["entries"] = entry_rows
    return out


def aggregate_cliente(cliente: str, ovs: list[dict]) -> dict:
    """Resumo de um cliente a partir das OVs já agregadas."""
    n_ovs = len(ovs)
    n_open = sum(1 for o in ovs if not o["all_closed"])
    n_closed = n_ovs - n_open
    planned = sum(o["planned"] for o in ovs)
    produced = sum(o["produced"] for o in ovs)
    remaining_total = max(planned - produced, 0.0)
    pct = (produced / planned * 100.0) if planned > 0 else 0.0
    # Último kanban do cliente = o mais recente das OVs.
    last_kb = None
    for o in ovs:
        kb = o.get("last_kanban")
        if kb and (last_kb is None or (kb.get("captured_at") or "")
                   > (last_kb.get("captured_at") or "")):
            last_kb = kb
    return {
        "cliente": cliente,
        "n_ovs": n_ovs,
        "n_open": n_open,
        "n_closed": n_closed,
        "planned": planned,
        "produced": produced,
        "remaining": remaining_total,
        "pct_complete": round(pct, 1),
        "last_kanban": last_kb,
    }


# ---------------------------------------------------------------- top-level

def _build_status(include_closed: bool) -> dict:
    refs = get_watcher().get_refs() or {}
    consumption = get_consumption()
    ov_index = build_ov_index(refs)
    kb_index = _kanbans_by_ov()

    # Agrega cada OV em modo slim — vista de cliente não precisa de
    # entries detalhadas. Detalhe completo via /api/obras/ov/{ov}.
    ovs_full: list[dict] = []
    for ov, entries in ov_index.items():
        agg = aggregate_ov(ov, entries, kb_index.get(ov, []),
                           consumption, slim=True)
        if not include_closed and agg["all_closed"]:
            continue
        ovs_full.append(agg)

    # Agrupa por cliente
    by_cliente: dict[str, list[dict]] = {}
    for o in ovs_full:
        by_cliente.setdefault(o["cliente"] or "(sem cliente)", []).append(o)

    clientes_out: list[dict] = []
    for cli, ovs in by_cliente.items():
        # Dentro de cada cliente, ordena OVs por remaining DESC (quem
        # mais peças tem a fazer aparece primeiro).
        ovs.sort(key=lambda o: (-o["remaining"], o["ov"]))
        cli_agg = aggregate_cliente(cli, ovs)
        cli_agg["ovs"] = ovs
        clientes_out.append(cli_agg)

    # Ordena clientes por remaining DESC (quem tem mais por fazer primeiro)
    clientes_out.sort(key=lambda c: (-c["remaining"], c["cliente"]))

    return {
        "clientes": clientes_out,
        "n_clientes": len(clientes_out),
        "n_ovs": sum(c["n_ovs"] for c in clientes_out),
        "n_open_ovs": sum(c["n_open"] for c in clientes_out),
        "generated_at": time.time(),
        "include_closed": include_closed,
    }


def compute_obras_status(include_closed: bool = False) -> dict:
    """Devolve agregado cacheado (TTL 60s)."""
    global _cache, _cache_at
    key = ("closed" if include_closed else "open")
    with _lock:
        now = time.time()
        if (
            _cache is not None
            and _cache.get("_key") == key
            and (now - _cache_at) <= _CACHE_TTL_S
        ):
            return _cache
        data = _build_status(include_closed)
        data["_key"] = key
        _cache = data
        _cache_at = now
        return _cache


def get_ov_detail(ov: str, include_closed: bool = True) -> dict | None:
    """Detalhe single-OV — para drill-down sem recarregar todo o
    agregado. Não usa cache (chamada pontual)."""
    refs = get_watcher().get_refs() or {}
    ov_index = build_ov_index(refs)
    entries = ov_index.get(str(ov).strip())
    if not entries:
        return None
    kbs = _kanbans_by_ov().get(str(ov).strip(), [])
    return aggregate_ov(str(ov).strip(), entries, kbs)


def invalidate_cache() -> None:
    """Forçar refresh no próximo :func:`compute_obras_status`."""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


__all__ = [
    "build_ov_index",
    "aggregate_ov",
    "aggregate_cliente",
    "compute_obras_status",
    "get_ov_detail",
    "invalidate_cache",
]
