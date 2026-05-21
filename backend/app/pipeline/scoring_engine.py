"""R108 — Motor unificado de scoring (experimental, shadow mode).

Filosofia (Luís, R108 v5):

  Top-K por campo → top-1 por linha (melhor score) → substitui sempre
  pela linha vencedora, mesmo que o OCR seja diferente.

  Única condição de aceitação: a linha vencedora tem de ser real
  (existe no plan) e, se o operador escreveu um lote, esse lote tem
  de existir no SAP.

  Sem concordância mínima. Sem proteger OCR. Sem 100%-match-com-OCR.

  4 estados de célula (com legendas para a UI):
    - confirmed:      "Confirmado"          — motor escolheu valor igual ao OCR
    - snapped:        "Substituído"          — motor mudou (ou preencheu) sem ser radical
    - very_different: "Muito diferente"     — motor propõe valor longe do OCR; vermelho
    - NA:             "Sem referência"      — campo sem pool / sem winner
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from itertools import product
from typing import Any


# Inline deps (R109 — motor self-contained) ----------------------------------

def normalize_of(value: object) -> str:
    """6 dígitos canónico. Pure-digit OFs < 6 chars são zero-padded; o
    resto fica intocado para coabitar com snap_of."""
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return s


def _o_zero_variants(s: str) -> list[str]:
    """0/O swap variants (R93). Capped 8 variants, ≤3 swap positions."""
    if not s:
        return [s]
    positions = [i for i, ch in enumerate(s) if ch in ("0", "O", "o")]
    if not positions or len(positions) > 3:
        return [s]
    variants: set[str] = {s}
    chars = list(s)
    for combo in product(("0", "O"), repeat=len(positions)):
        for idx, ch in zip(positions, combo):
            chars[idx] = ch
        variants.add("".join(chars))
    return list(variants)[:8]


def score_entry(
    entry: dict,
    row: dict,
    refs: dict,
    cliente_aliases: dict[str, str] | None = None,
) -> int:
    """Holistic 0-9 match score (R57). 9 features com peso igual."""
    op_cli_raw = (row.get("cliente") or "").strip().upper()
    op_cli = (cliente_aliases or {}).get(op_cli_raw, op_cli_raw) if cliente_aliases else op_cli_raw
    op_ov = str(row.get("ov") or "").strip()
    op_mod = (row.get("modelo") or "").strip().upper()
    op_comp = _num(row.get("comp_mm"))
    op_lb = _num(row.get("lbase"))
    op_lt = _num(row.get("ltopo"))
    op_esp = _num(row.get("esp"))
    op_larg = _num(row.get("larg_mm"))
    op_lote = (row.get("lote") or "").strip().upper()

    sap_full = refs.get("lotes_sap_full", {}) if refs else {}
    sap_e = sap_full.get(op_lote) if op_lote else None
    sap_larg = _num(sap_e.get("larg")) if sap_e else None
    sap_desc_first = ""
    if sap_e:
        d = (sap_e.get("desc") or "").strip().upper()
        sap_desc_first = d.split()[0] if d.split() else ""

    s = 0
    if op_cli and op_cli == (entry.get("cliente") or "").strip().upper():
        s += 1
    if op_ov and op_ov == str(entry.get("ov") or "").strip():
        s += 1
    des = (entry.get("designacao") or "").upper()
    if op_mod and len(op_mod) >= 4 and op_mod in des:
        s += 1
    plan_comp = _num(entry.get("comp"))
    if op_comp is not None and plan_comp is not None and abs(op_comp - plan_comp) <= 100:
        s += 1
    plan_lb = _num(entry.get("lbase"))
    if op_lb is not None and plan_lb is not None and abs(op_lb - plan_lb) <= 30:
        s += 1
    plan_lt = _num(entry.get("ltopo"))
    if op_lt is not None and plan_lt is not None and abs(op_lt - plan_lt) <= 30:
        s += 1
    plan_esp = _num(entry.get("esp"))
    if op_esp is not None and plan_esp is not None and abs(op_esp - plan_esp) <= 0.05:
        s += 1
    if sap_larg is not None and op_larg is not None and abs(op_larg - sap_larg) <= 20:
        s += 1
    plan_mat = (entry.get("material") or "").strip().upper()
    if plan_mat and sap_desc_first and plan_mat == sap_desc_first:
        s += 1
    return s


# Configuração ---------------------------------------------------------------

_NO_REF_FIELDS = frozenset({
    "pri", "coni", "qtd",
    "horas_trabalhadas", "colunas_produzidas",
    "n_operador", "data", "setor_maquina", "cod_maquina", "operador",
})

_ROW_FIELDS = (
    "cliente", "ov", "of", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp",
)

_PLAN_FIELDS = ("of", "ov", "modelo", "comp_mm", "lbase", "ltopo", "esp")

_TOP_K = 10

# Thresholds de "muito diferente" — abaixo destes níveis, vermelho.
_VERY_DIFF_STR_SIM = 50.0          # se sim < 50, é muito diferente
_VERY_DIFF_NUM_REL = 0.20          # 20% de diferença relativa = muito diferente
_VERY_DIFF_NUM_ABS = {             # ou se diferença absoluta > X, é muito diferente
    "comp_mm": 200.0,
    "larg_mm": 50.0,
    "lbase": 30.0,
    "ltopo": 30.0,
    "esp": 0.5,
}

# Legendas para a UI (status → label PT-PT)
_STATUS_LABELS = {
    "confirmed":      "Confirmado",
    "snapped":        "Substituído",
    "very_different": "Muito diferente — rever",
    "NA":             "Sem referência",
}

_ENGINE_VERSION = "shadow_v5_R108"


# Utilidades de distância ----------------------------------------------------

def _lev_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 5:
        return 999
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _str_sim(target: str, candidate: str) -> float:
    if not target or not candidate:
        return 0.0
    t = target.upper()
    c = candidate.upper()
    if t == c:
        return 100.0
    if t in c or c in t:
        return 80.0
    d = _lev_distance(t, c)
    m = max(len(t), len(c))
    if d >= m:
        return 0.0
    return 100.0 * (1 - d / m)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _num_sim(target: float | None, candidate: float | None, max_delta: float) -> float:
    if target is None or candidate is None:
        return 0.0
    d = abs(target - candidate)
    if d >= max_delta:
        return 0.0
    if d <= max_delta / 10:
        return 100.0
    return 100.0 * (1 - d / max_delta)


# Normalização cosmética (única guarda mantida) -----------------------------

def _format_value(field: str, value: Any) -> str:
    """2,6 = 2.6 → '2,6'. 1227.0 = 1227 → '1227'. Strings: strip."""
    if value is None or value == "":
        return ""
    if field == "esp":
        n = _num(value)
        if n is None:
            return str(value).strip()
        return f"{n:g}".replace(".", ",")
    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "qtd"):
        n = _num(value)
        if n is None:
            return str(value).strip()
        return str(int(round(n)))
    return str(value).strip()


# Pre-indexação das refs (cache por refs id) --------------------------------

_INDEX_CACHE: dict[int, dict] = {}


def _get_indices(refs: dict) -> dict:
    key = id(refs)
    cached = _INDEX_CACHE.get(key)
    if cached and cached.get("loaded_at") == refs.get("loaded_at"):
        return cached

    of_to_entries = refs.get("of_to_entries", {}) or {}
    ov_to_entries: dict[str, list[dict]] = {}
    des_to_entries: dict[str, list[dict]] = {}
    dim_indices: dict[str, dict[float, list[dict]]] = {
        "comp": {}, "larg": {}, "lbase": {}, "ltopo": {}, "esp": {},
    }

    for of_key, entries in of_to_entries.items():
        for e in entries:
            stamped = dict(e)
            stamped["_of"] = of_key
            ov_val = str(e.get("ov") or "").strip()
            if ov_val:
                ov_to_entries.setdefault(ov_val, []).append(stamped)
            des = str(e.get("designacao") or "").strip()
            if des:
                des_to_entries.setdefault(des, []).append(stamped)
            for attr in ("comp", "larg", "lbase", "ltopo", "esp"):
                v = _num(e.get(attr))
                if v is not None:
                    dim_indices[attr].setdefault(v, []).append(stamped)

    indices = {
        "loaded_at": refs.get("loaded_at"),
        "of_to_entries": of_to_entries,
        "ov_to_entries": ov_to_entries,
        "des_to_entries": des_to_entries,
        "dim_indices": dim_indices,
        "of_keys": list(of_to_entries.keys()),
        "ov_keys": list(ov_to_entries.keys()),
        "des_keys": list(des_to_entries.keys()),
    }
    _INDEX_CACHE[key] = indices
    return indices


# Geração de candidatos por campo --------------------------------------------

def _topk_keys_by_sim(target: str, pool: list[str], k: int) -> list[tuple[str, float]]:
    if not target or not pool:
        return []
    scored = [(_str_sim(target, key), key) for key in pool]
    scored.sort(reverse=True)
    return [(key, s) for s, key in scored[:k] if s > 0]


def _candidates_for_field(field: str, row: dict, refs: dict, idx: dict) -> list[dict]:
    """Top-K candidatos por campo (puro top-K)."""
    ocr_value = str(row.get(field) or "").strip()
    if not ocr_value or field in _NO_REF_FIELDS:
        return []

    of_to_entries = idx["of_to_entries"]
    ov_to_entries = idx["ov_to_entries"]
    des_to_entries = idx["des_to_entries"]
    dim_indices = idx["dim_indices"]
    lotes_sap = refs.get("lotes_sap_full", {}) or {}
    clientes_plan = refs.get("clientes_plan", frozenset()) or frozenset()

    out: list[dict] = []

    if field == "of":
        normalized = normalize_of(ocr_value)
        seen: set[str] = set()
        for v in _o_zero_variants(normalized):
            if v in of_to_entries and v not in seen:
                seen.add(v)
                out.append({
                    "value": v, "sim": _str_sim(normalized, v),
                    "plan_entries": [dict(e, _of=e.get("_of") or v) for e in of_to_entries[v]],
                })
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(normalized, idx["of_keys"], _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({
                        "value": k, "sim": s,
                        "plan_entries": [dict(e, _of=e.get("_of") or k) for e in of_to_entries[k]],
                    })
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field == "ov":
        seen: set[str] = set()
        for v in _o_zero_variants(ocr_value):
            if v in ov_to_entries and v not in seen:
                seen.add(v)
                out.append({"value": v, "sim": _str_sim(ocr_value, v), "plan_entries": ov_to_entries[v]})
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(ocr_value, idx["ov_keys"], _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({"value": k, "sim": s, "plan_entries": ov_to_entries[k]})
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field == "modelo":
        for k, s in _topk_keys_by_sim(ocr_value, idx["des_keys"], _TOP_K):
            out.append({"value": k, "sim": s, "plan_entries": des_to_entries[k]})
        return out[:_TOP_K]

    if field == "cliente":
        ocr_u = ocr_value.upper()
        pool = set(clientes_plan)
        if "clientes_lexicon" in refs:
            pool |= set(refs.get("clientes_lexicon") or [])
        for k, s in _topk_keys_by_sim(ocr_u, list(pool), _TOP_K):
            out.append({"value": k, "sim": s, "plan_entries": []})
        return out[:_TOP_K]

    if field == "lote":
        if not lotes_sap:
            return []
        seen: set[str] = set()
        for v in _o_zero_variants(ocr_value):
            if v in lotes_sap and v not in seen:
                seen.add(v)
                out.append({"value": v, "sim": _str_sim(ocr_value, v), "plan_entries": [], "sap_entry": lotes_sap[v]})
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(ocr_value, list(lotes_sap.keys()), _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({"value": k, "sim": s, "plan_entries": [], "sap_entry": lotes_sap[k]})
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp"):
        ocr_num = _num(ocr_value)
        if ocr_num is None:
            return []
        plan_attr = {
            "comp_mm": "comp", "larg_mm": "larg",
            "lbase": "lbase", "ltopo": "ltopo", "esp": "esp",
        }[field]
        max_delta = {
            "comp_mm": 100.0, "larg_mm": 50.0,
            "lbase": 30.0, "ltopo": 30.0, "esp": 0.1,
        }[field]
        in_range = [
            (val, entries) for val, entries in dim_indices[plan_attr].items()
            if abs(val - ocr_num) <= max_delta
        ]
        in_range.sort(key=lambda kv: abs(kv[0] - ocr_num))
        for v, entries in in_range[:_TOP_K]:
            out.append({"value": v, "sim": _num_sim(ocr_num, v, max_delta), "plan_entries": entries})
        return out[:_TOP_K]

    return []


# Passe 2: cruzar candidatos e escolher entry vencedora ----------------------

def _entry_key(entry: dict) -> tuple:
    return (
        str(entry.get("_of") or entry.get("of") or "").strip(),
        str(entry.get("ov") or "").strip(),
        str(entry.get("designacao") or "").strip().upper(),
    )


def _entry_is_real(entry: dict, row: dict, refs: dict) -> bool:
    """R108 v5 — a única validação: entry vem do plan (sempre) e, se o
    operador escreveu lote, esse lote existe no SAP. Não compara com OCR."""
    op_lote = (row.get("lote") or "").strip().upper()
    if op_lote:
        sap_full = refs.get("lotes_sap_full", {}) or {}
        if op_lote not in sap_full:
            return False
    return True


def _find_winner_entry(
    candidates_by_field: dict[str, list[dict]],
    row: dict,
    refs: dict,
) -> dict | None:
    """R108 v5 + R113 — entries vencedoras são reais (existem no plan + SAP),
    e quando há vários scores iguais, preferimos a que falta menos para
    fechar (operador acaba uma peça e passa à próxima naturalmente).

    Entries com `remaining ≤ 0` (já totalmente produzidas) são filtradas.
    Empate em score → menor remaining ganha.
    """
    from app.pipeline.of_consumption import remaining as _remaining

    entries_by_key: dict[tuple, dict] = {}
    for field in _PLAN_FIELDS:
        for cand in candidates_by_field.get(field, []):
            for e in cand.get("plan_entries", []):
                k = _entry_key(e)
                entries_by_key.setdefault(k, e)

    if not entries_by_key:
        return None

    # Recolher entries elegíveis (reais + ainda por produzir).
    # Tuple (-score, remaining_sortable, key, entry) — depois sort asc:
    # primeiro o maior score (negativo desempata), depois menor remaining.
    eligible: list[tuple[float, float, tuple, dict]] = []
    for k, e in entries_by_key.items():
        if not _entry_is_real(e, row, refs):
            continue
        if "_of" not in e:
            e = dict(e)
            e["_of"] = k[0]
        score = score_entry(e, row, refs)
        if score < 1:
            continue
        rem = _remaining(e)
        # Entry totalmente produzida — não a recomendar.
        if rem <= 0:
            continue
        rem_sort = 9e9 if rem == float("inf") else rem
        eligible.append((-float(score), rem_sort, k, e))

    if not eligible:
        return None
    eligible.sort()
    best_neg_score, best_rem_sort, best_key, _ = eligible[0]
    winner = dict(entries_by_key[best_key])
    winner["_score"] = int(-best_neg_score)
    if best_rem_sort < 9e9:
        winner["_remaining"] = best_rem_sort
    return winner


# Detecção de "muito diferente" ---------------------------------------------

def _is_very_different(field: str, ocr_value: str, proposed: str) -> bool:
    """True se o proposto é muito diferente do OCR — sinaliza vermelho."""
    if not ocr_value or not proposed:
        return False
    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp"):
        ocr_n = _num(ocr_value)
        prop_n = _num(proposed)
        if ocr_n is None or prop_n is None:
            return False
        abs_max = _VERY_DIFF_NUM_ABS.get(field, 0)
        if abs(ocr_n - prop_n) > abs_max:
            return True
        if max(abs(ocr_n), abs(prop_n)) > 0:
            rel = abs(ocr_n - prop_n) / max(abs(ocr_n), abs(prop_n))
            return rel > _VERY_DIFF_NUM_REL
        return False
    sim = _str_sim(str(ocr_value), str(proposed))
    return sim < _VERY_DIFF_STR_SIM


# Aplicação da entry vencedora à linha ---------------------------------------

def _make_cell(value: str, status: str, source: str, **extra) -> dict:
    cell = {
        "value": value,
        "status": status,
        "label": _STATUS_LABELS.get(status, status),
        "source": source,
    }
    cell.update(extra)
    return cell


def _apply_winner_to_field(
    field: str,
    ocr_value: str,
    winner: dict | None,
    candidates: list[dict],
    refs: dict,
) -> dict:
    if field in _NO_REF_FIELDS:
        return _make_cell(ocr_value, "NA", "ocr_raw")

    if winner is None and not candidates:
        # R120 — se o operador escreveu algo num campo validável (não em
        # _NO_REF_FIELDS) e o motor não achou candidato nem winner no plan,
        # sinaliza vermelho (very_different) em vez de cinza (NA). Antes
        # ficava NA e escondia a divergência. Cells com OCR vazio continuam
        # NA (sem dado para validar).
        if ocr_value:
            return _make_cell(ocr_value, "very_different", "ocr_raw")
        return _make_cell(ocr_value, "NA", "ocr_raw")

    # Extrair valor proposto pela entry vencedora
    proposed: str | None = None
    if winner is not None:
        if field == "of":
            proposed = str(winner.get("_of") or winner.get("of") or "").strip()
        elif field == "ov":
            proposed = str(winner.get("ov") or "").strip()
        elif field == "modelo":
            proposed = str(winner.get("designacao") or "").strip()
        elif field == "cliente":
            proposed = str(winner.get("cliente") or "").strip()
        elif field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp"):
            plan_attr = {
                "comp_mm": "comp", "larg_mm": "larg",
                "lbase": "lbase", "ltopo": "ltopo", "esp": "esp",
            }[field]
            v = winner.get(plan_attr)
            if v is not None and v != "":
                proposed = str(v)

    # Lote: o operador escreveu, e validámos que está no SAP via _entry_is_real
    if field == "lote" and winner is not None and ocr_value:
        proposed = ocr_value  # lote OCR mantido (já validado no SAP)

    if not proposed:
        return _make_cell(ocr_value, "NA", "ocr_raw")

    proposed_fmt = _format_value(field, proposed)
    ocr_fmt = _format_value(field, ocr_value)

    # Status
    if proposed_fmt.upper() == ocr_fmt.upper() if proposed_fmt and ocr_fmt else False:
        status = "confirmed"
    elif not ocr_value:
        status = "snapped"  # autofill
    elif _is_very_different(field, ocr_value, proposed):
        status = "very_different"
    else:
        status = "snapped"

    return _make_cell(
        proposed_fmt,
        status,
        source="plan" if winner else "lexicon",
        score=winner.get("_score") if winner else None,
    )


# Scoring de uma linha completa ----------------------------------------------

def _score_row(row_idx: int, row: dict, refs: dict, idx: dict) -> tuple[dict, int, int, int, int, int]:
    candidates_by_field: dict[str, list[dict]] = {}
    for field in _ROW_FIELDS:
        candidates_by_field[field] = _candidates_for_field(field, row, refs, idx)

    winner = _find_winner_entry(candidates_by_field, row, refs)

    fields_out: dict[str, dict] = {}
    snapped = confirmed = na = very_diff = 0
    for field in _ROW_FIELDS:
        ocr_value = str(row.get(field) or "").strip()
        result = _apply_winner_to_field(
            field, ocr_value, winner, candidates_by_field[field], refs
        )
        fields_out[field] = result
        st = result["status"]
        if st == "snapped":
            snapped += 1
        elif st == "confirmed":
            confirmed += 1
        elif st == "very_different":
            very_diff += 1
        else:
            na += 1

    for k, v in row.items():
        if k in _ROW_FIELDS or k in fields_out:
            continue
        if k in _NO_REF_FIELDS:
            fields_out[k] = _make_cell(str(v) if v is not None else "", "NA", "ocr_raw")
            na += 1

    total = snapped + confirmed + na + very_diff
    row_out = {
        "row_index": row_idx,
        "fields": fields_out,
        "winner_of": (winner or {}).get("_of") if winner else None,
        "winner_score": (winner or {}).get("_score") if winner else None,
    }
    return row_out, snapped, confirmed, na, very_diff, total


# Entry point ----------------------------------------------------------------

def shadow_score(
    sheet_data: dict,
    dq_audit: dict | None,
    refs: dict,
) -> tuple[dict, int, int, int, int, int]:
    """Retorna (scoring, total, snapped, confirmed, na, duration_ms).

    Nota: very_different fica contabilizado dentro do scoring.summary,
    mas não é devolvido como contador top-level (compat com a interface
    db.finish_shadow_run que tem 4 contadores: snapped/confirmed/na/total).
    """
    started = time.perf_counter()
    idx = _get_indices(refs)

    rows = sheet_data.get("rows") or []
    header = sheet_data.get("header") or {}
    footer = sheet_data.get("footer") or {}
    template_name = sheet_data.get("template_name", "bobine_formato")

    out_rows = []
    snapped = confirmed = na = very_diff = 0
    for i, row in enumerate(rows):
        row_out, s, c, n, vd, _t = _score_row(i, row, refs, idx)
        out_rows.append(row_out)
        snapped += s
        confirmed += c
        na += n
        very_diff += vd

    header_out = {
        k: _make_cell(str(v) if v is not None else "", "NA", "ocr_raw")
        for k, v in header.items()
    }
    footer_out = {
        k: _make_cell(str(v) if v is not None else "", "NA", "ocr_raw")
        for k, v in footer.items()
    }
    na += len(header_out) + len(footer_out)

    total = snapped + confirmed + na + very_diff
    duration_ms = int((time.perf_counter() - started) * 1000)

    scoring = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": _ENGINE_VERSION,
        "template_name": template_name,
        "summary": {
            "confirmed": confirmed,
            "snapped": snapped,
            "very_different": very_diff,
            "na": na,
            "total": total,
        },
        "status_labels": _STATUS_LABELS,
        "rows": out_rows,
        "header": header_out,
        "footer": footer_out,
        "duration_ms": duration_ms,
    }
    # Compat com db.finish_shadow_run: snapped agrega snapped + very_different
    return scoring, total, snapped + very_diff, confirmed, na, duration_ms


# R109 — Wrapper compat com UI legacy (MATCH/NO_MATCH/NA) -------------------

CROSS_CHECK_STATUSES = ("MATCH", "NO_MATCH", "NA")

# Mapping interno v5 → legacy
_V5_TO_LEGACY = {
    "confirmed": "MATCH",
    "snapped": "MATCH",            # snap suave + autofill = verde
    "very_different": "NO_MATCH",  # vermelho — operador revê
    "NA": "NA",
}


def _to_legacy_cell(v5_cell: dict, ref_value: str | None = None) -> dict:
    """Converte célula do shadow output para shape esperado pela UI."""
    v5_status = v5_cell.get("status", "NA")
    legacy_status = _V5_TO_LEGACY.get(v5_status, "NA")
    out = {
        "value": v5_cell.get("value", ""),
        "status": legacy_status,
        "label": v5_cell.get("label", ""),
        "snapped": v5_status == "snapped",
        "engine_status": v5_status,
    }
    if ref_value is not None:
        out["ref"] = ref_value
    return out


def cross_check_sheet(
    sheet_data: dict,
    dq_audit: dict | None,
    refs: dict,
) -> dict:
    """R109 — Entry point oficial. Wraps shadow_score, devolve output no
    formato legacy esperado pela UI (status MATCH/NO_MATCH/NA, summary,
    rows, header, footer, to_analisar).
    """
    scoring, _total, _snapped, _confirmed, _na, duration_ms = shadow_score(
        sheet_data, dq_audit, refs,
    )

    # Reconstruir rows no shape legacy
    legacy_rows = []
    summary = {"match": 0, "no_match": 0, "na": 0, "total": 0}
    to_analisar: list[dict] = []

    for r in scoring.get("rows", []):
        legacy_fields: dict[str, dict] = {}
        row_summary = {"match": 0, "no_match": 0, "na": 0}
        for field, cell in r.get("fields", {}).items():
            ref_value = None
            if cell.get("status") in ("snapped", "very_different"):
                # ref é o valor proposto pelo motor (vem do plan)
                ref_value = cell.get("value")
            legacy_cell = _to_legacy_cell(cell, ref_value)
            legacy_fields[field] = legacy_cell
            st = legacy_cell["status"]
            if st == "MATCH":
                row_summary["match"] += 1
                summary["match"] += 1
            elif st == "NO_MATCH":
                row_summary["no_match"] += 1
                summary["no_match"] += 1
                # Adicionar ao to_analisar
                to_analisar.append({
                    "row_index": r.get("row_index", 0),
                    "field": field,
                    "value": sheet_data.get("rows", [{}])[r.get("row_index", 0)].get(field, "")
                            if r.get("row_index", 0) < len(sheet_data.get("rows", [])) else "",
                    "ref": legacy_cell.get("ref", ""),
                    "ref_source": "plan",
                    "reason": "Motor propõe valor muito diferente do OCR",
                })
            else:
                row_summary["na"] += 1
                summary["na"] += 1
            summary["total"] += 1

        legacy_rows.append({
            "row_index": r.get("row_index", 0),
            "fields": legacy_fields,
            "summary": row_summary,
            "winner_of": r.get("winner_of"),
            "winner_score": r.get("winner_score"),
        })

    legacy_header = {k: _to_legacy_cell(v) for k, v in scoring.get("header", {}).items()}
    legacy_footer = {k: _to_legacy_cell(v) for k, v in scoring.get("footer", {}).items()}
    for v in legacy_header.values():
        summary[v["status"].lower() if v["status"] != "NO_MATCH" else "no_match"] += 1
        summary["total"] += 1
    for v in legacy_footer.values():
        summary[v["status"].lower() if v["status"] != "NO_MATCH" else "no_match"] += 1
        summary["total"] += 1

    return {
        "checked_at": scoring.get("checked_at"),
        "engine_version": scoring.get("engine_version"),
        "template_name": scoring.get("template_name"),
        "summary": summary,
        "rows": legacy_rows,
        "header": legacy_header,
        "footer": legacy_footer,
        "to_analisar": to_analisar,
        "refs_loaded_at": refs.get("loaded_at"),
        "duration_ms": duration_ms,
    }


__all__ = ["shadow_score", "cross_check_sheet", "CROSS_CHECK_STATUSES",
           "score_entry", "normalize_of"]
