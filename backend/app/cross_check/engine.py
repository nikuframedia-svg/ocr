"""Cross-check engine — pure verification (Round 33).

Contrast with prior rounds:
- Round 30: status flags (OK/CORRIGIDO/PREENCHIDO/ANALISAR/MISSING)
- Round 32: aggressive auto-overwrite from plan (REVERTED in R33)
- Round 33: pure verification, no auto-fill, no auto-overwrite. Just checks
  each cell against the appropriate ref source and emits a 3-value status:

  - MATCH    — cell value matches reference (green in UI)
  - NO_MATCH — cell value differs from reference (red in UI)
  - NA       — no reference to check against (neutral / gray)

Refs used:
- StockSAP.xlsx → per-lote attributes: ``esp``, ``larg``, ``qtd``, ``desc``
- plan_colunas_cpis.xlsx → per-OF entries: ``cliente``, ``ov``, ``designacao``,
  ``esp``, ``lbase``, ``ltopo``, ``comp``

Field policy (decided with user 2026-05-04):

| Field         | Check                                     |
|---------------|-------------------------------------------|
| of            | of in plan_colunas → MATCH, else NO_MATCH |
| cliente       | matches plan[of].cliente                  |
| ov            | matches plan[of].ov                       |
| modelo        | substring of plan[of].designacao          |
| qtd           | NA — operator's count                     |
| comp_mm       | matches plan[of].comp ±50mm               |
| larg_mm       | matches SAP[lote].larg (exact)            |
| lote          | lote in SAP                               |
| esp           | matches SAP[lote].esp OR plan[of].esp     |
| coni          | NA — no ref ("caga")                      |
| pri           | NA — no ref ("caga")                      |
| lbase         | NA — caga'd by user                       |
| ltopo         | NA — caga'd by user                       |
| header.*      | NA (operator/system metadata)             |
| footer.*      | NA (totals — sum-check could come later)  |

Output JSON shape (per row):
    {
      "row_index": 0,
      "fields": {
        "cliente": {"value": "...", "status": "MATCH", "ref": "...", "ref_source": "plan"},
        ...
      }
    }
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Status enum (3 values, JSON-friendly)
CROSS_CHECK_STATUSES = ("MATCH", "NO_MATCH", "NA")

# Tolerances. Round 43 — moderate widening of dim tolerances:
# user accepted small OCR misreads (5-15mm) as MATCH provided they're
# geometrically consistent (ltotal sanity check below).
TOL_LARG_MM = 20      # ±20mm (was 0 exact); widening per Round 43 Sol 3
TOL_COMP_MM = 100     # comp tolerated ±100mm (R52: user requested widening from ±50mm)
TOL_ESP = 0.01        # esp essentially equal

# Fields explicitly ignored — user said "caga" + no useful ref
# Round 37: lbase + ltopo MOVED to validated (compared directly to
# plan.lbase / plan.ltopo).
# Round 43: dual-gate tolerance for lbase/ltopo. Within INNER → always
# MATCH. Between INNER and OUTER → MATCH only if ltotal sanity passes
# (lbase_ocr + ltopo_ocr ≈ plan.ltotal ±TOL_LTOTAL_SANITY).
_NO_CHECK_FIELDS = ("coni", "pri", "qtd")
TOL_LBASE_LTOPO_INNER = 2    # individual ±2mm — always MATCH (was 2)
TOL_LBASE_LTOPO_OUTER = 10   # individual ±10mm — MATCH only if sum sanity passes
TOL_LTOTAL_SANITY = 5        # |sum_ocr - sum_plan| must be ≤ this for OUTER to apply
# Backward-compat alias (was used in old code)
TOL_LBASE_LTOPO = TOL_LBASE_LTOPO_INNER


def _longest_common_substring(a: str, b: str) -> int:
    """Round 62 — length of longest contiguous substring shared by a and b.

    Standard 2-row DP, O(|a|·|b|). Used as a safety guard for the
    row-context modelo MATCH path: if operator's modelo shares ≥4
    consecutive alphanum chars with the plan FT, the plan entry can
    be trusted as canonical for that cell (other fields already
    confirmed the row).
    """
    if not a or not b:
        return 0
    la, lb = len(a), len(b)
    best = 0
    dp = [0] * (lb + 1)
    for i in range(1, la + 1):
        prev = 0
        for j in range(1, lb + 1):
            cur = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev + 1
                if dp[j] > best:
                    best = dp[j]
            else:
                dp[j] = 0
            prev = cur
    return best


def _count_row_confirmation(
    row: dict,
    plan_entry: dict | None,
    sap_entry: dict | None,
) -> int:
    """Round 62 — count of non-modelo row fields that MATCH the picked
    plan_entry / sap_entry. Used to gate the row-context modelo MATCH.

    Compares 8 fields with the same tolerances as _check_row's main
    block: cliente, ov, comp_mm, larg_mm, lote, esp, lbase, ltopo.
    `of` is implicit (matched_entry only exists if OF in plan).
    """
    if not plan_entry:
        return 0
    n = 0
    # cliente — exact upper match
    cli_op = str(row.get("cliente") or "").strip().upper()
    cli_plan = str(plan_entry.get("cliente") or "").strip().upper()
    if cli_op and cli_plan and cli_op == cli_plan:
        n += 1
    # ov — exact string
    ov_op = str(row.get("ov") or "").strip()
    ov_plan = str(plan_entry.get("ov") or "").strip()
    if ov_op and ov_plan and ov_op == ov_plan:
        n += 1
    # comp_mm — ±TOL_COMP_MM
    comp_op = _num(row.get("comp_mm"))
    comp_plan = _num(plan_entry.get("comp"))
    if comp_op is not None and comp_plan is not None and abs(comp_op - comp_plan) <= TOL_COMP_MM:
        n += 1
    # lbase — INNER tolerance only (conservative)
    lb_op = _num(row.get("lbase"))
    lb_plan = _num(plan_entry.get("lbase"))
    if lb_op is not None and lb_plan is not None and abs(lb_op - lb_plan) <= TOL_LBASE_LTOPO_INNER:
        n += 1
    # ltopo — same
    lt_op = _num(row.get("ltopo"))
    lt_plan = _num(plan_entry.get("ltopo"))
    if lt_op is not None and lt_plan is not None and abs(lt_op - lt_plan) <= TOL_LBASE_LTOPO_INNER:
        n += 1
    # esp — SAP or plan
    esp_op = _num(row.get("esp"))
    if esp_op is not None:
        sap_esp = _num(sap_entry.get("esp")) if sap_entry else None
        plan_esp = _num(plan_entry.get("esp"))
        if sap_esp is not None and abs(esp_op - sap_esp) <= TOL_ESP:
            n += 1
        elif plan_esp is not None and abs(esp_op - plan_esp) <= TOL_ESP:
            n += 1
    # larg_mm — SAP only (plan doesn't have larg)
    larg_op = _num(row.get("larg_mm"))
    if larg_op is not None and sap_entry:
        sap_larg = _num(sap_entry.get("larg"))
        if sap_larg is not None and abs(larg_op - sap_larg) <= TOL_LARG_MM:
            n += 1
    # lote — sap_entry presence is the match signal
    lote_op = str(row.get("lote") or "").strip().upper()
    if lote_op and sap_entry is not None:
        n += 1
    return n


def _lev_indel(a: str, b: str, max_dist: int = 1) -> int:
    """Round 61 — Levenshtein distance with insertion/deletion/substitution.

    Standard 2-row DP. Returns min(distance, max_dist + 1) — early-exit
    when distance is guaranteed to exceed max_dist. Used by
    _modelo_fuzzy_match sub-rule (c) to catch operator OCR missing or
    with extra digit vs plan token (e.g. CFH2F2RI ↔ CFH2F12RI).
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    if la == 0:
        return lb if lb <= max_dist else max_dist + 1
    if lb == 0:
        return la if la <= max_dist else max_dist + 1
    # 2-row DP
    prev = list(range(lb + 1))
    cur = [0] * (lb + 1)
    for i in range(1, la + 1):
        cur[0] = i
        row_min = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(
                prev[j] + 1,          # deletion
                cur[j - 1] + 1,       # insertion
                prev[j - 1] + cost,   # substitution
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return max_dist + 1
        prev, cur = cur, prev
    return prev[lb]


def _modelo_tokens(text: str) -> list[str]:
    """R52 F2 — extract alphanumeric tokens >= 4 chars from arbitrary text.

    Used for token-fuzzy matching when operator writes modelo in long form
    (e.g. `1045 V66 N:4 A573V500`) and plan has the canonical code embedded
    in a multi-token description (`1015VF06 - Nº1 A573U500`).
    """
    return [m.group().upper() for m in re.finditer(r"[A-Z0-9]{4,}", text.upper())]


def _modelo_fuzzy_match(op_modelo: str, plan_designacao: str) -> bool:
    """R52 F2 + R55 — bidirectional fuzzy token match.

    True when:
      a) any operator token (≥5 chars) is within Lev-1 (Lev-2 for ≥7
         chars) of any plan token of same length. [R52 F2 — long-form
         single-token codes like `A573V500` ↔ `A573U500`]
      b) (R55) operator's compound modelo has ≥2 tokens (4+ chars) that
         match plan via substring OR Lev-1 (same-length), AND ≥40% of
         operator tokens match. [Catches multi-token operator writing
         like `1045 V120 N:4 1045 V503` ↔ `1015VT20 - Nº1 1015V503`
         where V503 substring + 1045 Lev-1 of 1015 anchors the match.]

    Does NOT match (legit OCR errors that should stay NO_MATCH):
      - Different-length single tokens that share no substring
      - Operator with only 1 short token (no multi-token signal)
    """
    if not op_modelo or not plan_designacao:
        return False
    op_toks = _modelo_tokens(op_modelo)
    plan_toks = _modelo_tokens(plan_designacao)
    if not op_toks or not plan_toks:
        return False

    # (a) R52 F2 — same-length Lev for long tokens
    for ot in op_toks:
        if len(ot) < 5:
            continue
        for pt in plan_toks:
            if len(pt) < 5:
                continue
            if ot == pt:
                return True
            if len(ot) != len(pt):
                continue
            diffs = sum(1 for a, b in zip(ot, pt) if a != b)
            if diffs <= 1:
                return True
            if diffs <= 2 and len(ot) >= 7:
                return True

    # (b) R55 — multi-token coherence. Each operator token (≥4 chars)
    # is matched against plan tokens via:
    #   - Direct substring (ot ⊂ pt) — e.g. V503 inside 1015V503
    #   - Sliding-window Lev-1 — operator's |ot|-char run differs by 1
    #     char from a same-length window of pt (e.g. 1045 vs 1015 inside
    #     1015VT20). Catches OCR character confusions in compound codes.
    matched_count = 0
    for ot in op_toks:
        if len(ot) < 4:
            continue
        for pt in plan_toks:
            if len(pt) < 4:
                continue
            # Direct substring
            if ot in pt:
                matched_count += 1
                break
            # Sliding window: check every |ot|-char window of pt
            if len(ot) <= len(pt):
                matched_window = False
                for i in range(len(pt) - len(ot) + 1):
                    sub = pt[i:i+len(ot)]
                    diffs = sum(1 for a, b in zip(ot, sub) if a != b)
                    if diffs == 1:
                        matched_count += 1
                        matched_window = True
                        break
                if matched_window:
                    break
    if matched_count >= 2 and matched_count / len(op_toks) >= 0.4:
        return True

    # (c) R61 — alphanum-normalized Lev-1 with insertion/deletion.
    # Catches operator OCR with missing or extra digit vs plan token,
    # which (a) and (b) miss because they require same-length comparison.
    # Example resolved:
    #   op `CFH2F2RI-V1` → alphanum `CFH2F2RIV1` (10 chars)
    #   plan `CFH2F12RI_V1 - FL PL + ...` → segments [CFH2F12RIV1 (11),
    #   FLPL (4), BASEINOX (8), FURACAO (7), TOPO (4)]
    #   Lev-1 indel(`CFH2F2RIV1`, `CFH2F12RIV1`) = 1 (insert `1` at pos 5) → MATCH
    # Conservative: min 6 alphanum chars, ±1 length, dist ≤ 1.
    op_alnum = "".join(ch for ch in op_modelo.upper() if ch.isalnum())
    if len(op_alnum) >= 6:
        # Plan designacao may be compound ("FT - suffix1 - suffix2");
        # split and test each segment that's ≥6 alphanum chars.
        for seg in plan_designacao.split(" - "):
            plan_alnum = "".join(ch for ch in seg.upper() if ch.isalnum())
            if len(plan_alnum) < 6:
                continue
            if abs(len(op_alnum) - len(plan_alnum)) > 1:
                continue
            if _lev_indel(op_alnum, plan_alnum, max_dist=1) <= 1:
                return True
    return False


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _best_plan_entry(entries: list[dict], modelo_ocr: str, comp_ocr: float | None,
                     row: dict | None = None, refs: dict | None = None) -> dict | None:
    """Pick the best plan entry for a row.

    Round 57: when `row` and `refs` are provided, use HOLISTIC field
    scoring (9 fields, equal weight) to pick the entry that maximizes
    matches across ALL operator fields. Tie-break: prefer active
    (fechado='0'), then high-signal (cliente+ov+modelo exact).

    Legacy fallback (when row/refs not given): substring(modelo) →
    closest comp → first. Used by older call-sites that don't pass
    the full row context.

    Round 43 Sol 2: when multiple entries exist for an OF, prefer those
    with fechado='0' (active) over fechado='1' (closed).
    """
    if not entries:
        return None

    # R57 — holistic scoring path. Used by _check_row which has full
    # row + refs context.
    if row is not None and refs is not None:
        from .holistic_score import score_entry, score_high_signal
        scored = []
        for e in entries:
            s = score_entry(e, row, refs)
            hs = score_high_signal(e, row)
            is_active = str(e.get("fechado", "0")) == "0"
            scored.append((s, int(is_active), hs, e))
        # Sort: max score, then active, then max high-signal
        scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        return scored[0][3]

    # Legacy path — substring + comp fallback
    active = [e for e in entries if str(e.get("fechado", "0")) == "0"]
    pool = active if active else entries
    if modelo_ocr:
        mu = modelo_ocr.upper().strip()
        if mu:
            for e in pool:
                if mu in str(e.get("designacao", "")).upper():
                    return e
            # Try in full list as fallback
            if pool is active:
                for e in entries:
                    if mu in str(e.get("designacao", "")).upper():
                        return e
    if comp_ocr is not None:
        cands = [e for e in pool if _num(e.get("comp")) is not None]
        if cands:
            return min(cands, key=lambda e: abs((_num(e["comp"]) or 0) - comp_ocr))
    return pool[0] if pool else entries[0]


def _check_row(
    row: dict[str, Any],
    refs: dict[str, Any],
) -> dict[str, Any]:
    """Run pure verification against refs. Returns per-field status."""
    of_raw = str(row.get("of") or "").strip()
    plan_entries = refs.get("of_to_entries", {}).get(of_raw, [])
    in_plan = bool(plan_entries)

    modelo_raw = str(row.get("modelo") or "").strip()
    comp_v = _num(row.get("comp_mm"))
    # R57: pass full row + refs to enable holistic scoring across all
    # entries (9 fields equal weight, max-% wins). Falls back to legacy
    # substring+comp logic if refs not threaded through (defensive).
    matched_entry = _best_plan_entry(
        plan_entries, modelo_raw, comp_v, row=row, refs=refs,
    ) if in_plan else None

    lote_raw = str(row.get("lote") or "").strip()
    sap_lotes = refs.get("lotes_sap", frozenset())
    sap_full = refs.get("lotes_sap_full", {})
    sap_entry = sap_full.get(lote_raw.upper()) if lote_raw and lote_raw.upper() in sap_lotes else None

    fields: dict[str, dict] = {}
    summary = {"match": 0, "no_match": 0, "na": 0}

    def _record(field: str, status: str, value: Any, **extras):
        fields[field] = {"value": value, "status": status, **extras}
        key = status.lower()
        if key in summary:
            summary[key] += 1

    # --- of ---
    if not of_raw:
        _record("of", "NA", "", reason="vazio")
    elif in_plan:
        _record("of", "MATCH", of_raw, ref=of_raw, ref_source="plan")
    else:
        _record("of", "NO_MATCH", of_raw, ref_source="plan",
                reason=f"OF '{of_raw}' não está em plan_colunas")

    # --- cliente ---
    cliente_raw = str(row.get("cliente") or "").strip()
    if not cliente_raw:
        _record("cliente", "NA", "", reason="vazio")
    elif not matched_entry:
        _record("cliente", "NA", cliente_raw, reason="OF não no plan")
    else:
        plan_cli = str(matched_entry.get("cliente", "")).strip()
        if plan_cli and plan_cli.upper() == cliente_raw.upper():
            _record("cliente", "MATCH", cliente_raw, ref=plan_cli, ref_source="plan")
        else:
            _record("cliente", "NO_MATCH", cliente_raw, ref=plan_cli or "(vazio)",
                    ref_source="plan",
                    reason=f"OCR '{cliente_raw}' ≠ plan '{plan_cli}'")

    # --- ov ---
    ov_raw = str(row.get("ov") or "").strip()
    if not ov_raw:
        _record("ov", "NA", "", reason="vazio")
    elif not matched_entry:
        _record("ov", "NA", ov_raw, reason="OF não no plan")
    else:
        plan_ov = str(matched_entry.get("ov", "")).strip()
        if plan_ov and plan_ov == ov_raw:
            _record("ov", "MATCH", ov_raw, ref=plan_ov, ref_source="plan")
        else:
            _record("ov", "NO_MATCH", ov_raw, ref=plan_ov or "(vazio)",
                    ref_source="plan",
                    reason=f"OCR '{ov_raw}' ≠ plan '{plan_ov}'")

    # --- modelo ---
    if not modelo_raw:
        _record("modelo", "NA", "", reason="vazio")
    elif not matched_entry:
        _record("modelo", "NA", modelo_raw, reason="OF não no plan")
    else:
        plan_des = str(matched_entry.get("designacao", "")).strip()
        matched = False
        match_reason = ""
        # Stage 1: substring (operator IN plan designacao)
        if plan_des and modelo_raw.upper() in plan_des.upper():
            matched = True
            match_reason = "substring"
        # Stage 2 (R52 F2): token-fuzzy match across ALL plan entries for
        # this OF. Catches long-form modelos where operator writes the
        # plan content with OCR errors scattered (e.g. `1045 V66 N:4 A573V500`
        # ↔ `1015VF06 - Nº1 A573U500`).
        if not matched and plan_entries:
            for e in plan_entries:
                des = str(e.get("designacao", "")).strip()
                if _modelo_fuzzy_match(modelo_raw, des):
                    matched = True
                    match_reason = "token_fuzzy"
                    plan_des = des  # use the matched entry's designacao as ref
                    break
        # Stage 3 (R62): row-context confirmation. When the matched_entry
        # is strongly confirmed by other row fields (≥4 of 8 non-modelo
        # MATCH) AND operator's modelo shares ≥4 consecutive alphanum
        # chars with the plan FT (safety guard against garbage OCR),
        # accept as MATCH and emit plan FT as canonical ref.
        # Examples resolved:
        #   `guilha` (6) + 5/8 fields confirm `guilhametro` → MATCH
        #   `xyz` (3) + 5/8 confirm `guilhametro` → NO_MATCH (LCS=0 < 4)
        if not matched and matched_entry:
            confirmation_score = _count_row_confirmation(row, matched_entry, sap_entry)
            if confirmation_score >= 4:
                ft = plan_des.split(" - ", 1)[0].strip()
                op_alnum = "".join(ch for ch in modelo_raw.upper() if ch.isalnum())
                ft_alnum = "".join(ch for ch in ft.upper() if ch.isalnum())
                if op_alnum and ft_alnum and _longest_common_substring(op_alnum, ft_alnum) >= 4:
                    matched = True
                    match_reason = f"row_context_{confirmation_score}of8"
        if matched:
            _record("modelo", "MATCH", modelo_raw, ref=plan_des,
                    ref_source="plan", reason=match_reason)
        else:
            _record("modelo", "NO_MATCH", modelo_raw, ref=plan_des[:50] or "(vazio)",
                    ref_source="plan",
                    reason=f"modelo '{modelo_raw}' não bate plan (substring nem fuzzy)")

    # --- comp_mm (vs plan.comp ±50mm) ---
    comp_raw = str(row.get("comp_mm") or "").strip()
    if not comp_raw:
        _record("comp_mm", "NA", "", reason="vazio")
    elif not matched_entry:
        _record("comp_mm", "NA", comp_raw, reason="OF não no plan")
    else:
        plan_comp = matched_entry.get("comp")
        ocr_n, plan_n = _num(comp_raw), _num(plan_comp)
        if plan_n is None:
            _record("comp_mm", "NA", comp_raw, reason="plan sem comp")
        elif ocr_n is None:
            _record("comp_mm", "NO_MATCH", comp_raw, ref=str(plan_comp),
                    ref_source="plan", reason="valor OCR não numérico")
        elif abs(ocr_n - plan_n) <= TOL_COMP_MM:
            _record("comp_mm", "MATCH", comp_raw, ref=str(plan_comp), ref_source="plan")
        else:
            _record("comp_mm", "NO_MATCH", comp_raw, ref=str(plan_comp),
                    ref_source="plan",
                    reason=f"OCR={ocr_n:.0f} vs plan={plan_n:.0f} (Δ{abs(ocr_n - plan_n):.0f}mm > {TOL_COMP_MM}mm)")

    # --- lote (in SAP?) ---
    if not lote_raw:
        _record("lote", "NA", "", reason="vazio")
    elif sap_entry is not None:
        _record("lote", "MATCH", lote_raw, ref_source="sap")
    else:
        _record("lote", "NO_MATCH", lote_raw, ref_source="sap",
                reason=f"lote '{lote_raw}' não está em StockSAP")

    # --- larg_mm (vs SAP[lote].larg, exact) ---
    larg_raw = str(row.get("larg_mm") or "").strip()
    if not larg_raw:
        _record("larg_mm", "NA", "", reason="vazio")
    elif sap_entry is None:
        _record("larg_mm", "NA", larg_raw, reason="lote não em SAP")
    else:
        sap_larg = sap_entry.get("larg")
        ocr_n, sap_n = _num(larg_raw), _num(sap_larg)
        if sap_n is None:
            _record("larg_mm", "NA", larg_raw, reason="SAP sem larg")
        elif ocr_n is None:
            _record("larg_mm", "NO_MATCH", larg_raw, ref=str(sap_larg),
                    ref_source="sap", reason="valor OCR não numérico")
        elif abs(ocr_n - sap_n) <= TOL_LARG_MM:
            _record("larg_mm", "MATCH", larg_raw, ref=str(sap_larg), ref_source="sap")
        else:
            _record("larg_mm", "NO_MATCH", larg_raw, ref=str(sap_larg),
                    ref_source="sap",
                    reason=f"OCR={ocr_n:.0f} vs SAP={sap_n:.0f} (lote {lote_raw}: Δ{abs(ocr_n - sap_n):.0f}mm)")

    # --- esp (priority: SAP[lote].esp; fallback plan[of].esp) ---
    esp_raw = str(row.get("esp") or "").strip()
    if not esp_raw:
        _record("esp", "NA", "", reason="vazio")
    else:
        ocr_n = _num(esp_raw)
        sap_esp_n = _num(sap_entry.get("esp")) if sap_entry else None
        plan_esp_n = _num(matched_entry.get("esp")) if matched_entry else None

        if ocr_n is None:
            _record("esp", "NO_MATCH", esp_raw, reason="valor OCR não numérico")
        elif sap_esp_n is not None and abs(ocr_n - sap_esp_n) <= TOL_ESP:
            _record("esp", "MATCH", esp_raw, ref=str(sap_entry.get("esp")), ref_source="sap")
        elif plan_esp_n is not None and abs(ocr_n - plan_esp_n) <= TOL_ESP:
            _record("esp", "MATCH", esp_raw, ref=str(matched_entry.get("esp")), ref_source="plan")
        elif sap_esp_n is None and plan_esp_n is None:
            _record("esp", "NA", esp_raw, reason="sem refs (lote+OF não no plan/SAP)")
        else:
            # OCR diverges from BOTH refs (or only ref disagrees)
            ref_v = sap_esp_n if sap_esp_n is not None else plan_esp_n
            ref_src = "sap" if sap_esp_n is not None else "plan"
            _record("esp", "NO_MATCH", esp_raw, ref=str(ref_v),
                    ref_source=ref_src,
                    reason=f"OCR={ocr_n} vs {ref_src}={ref_v}")

    # --- lbase / ltopo (R37 + R43 dual-gate): compare each vs plan ---
    # Plan has both lbase + ltopo per OF. Round 43 dual-gate logic:
    #   - within INNER (±2mm) → always MATCH
    #   - within OUTER (±10mm) → MATCH only if ltotal sanity passes,
    #     i.e. |(lbase_ocr + ltopo_ocr) - (plan.lbase + plan.ltopo)|
    #     ≤ TOL_LTOTAL_SANITY (5mm). This catches cases where operator
    #     swapped 1-2 digits but the geometric total is still correct.
    plan_lbase = _num(matched_entry.get("lbase")) if matched_entry else None
    plan_ltopo = _num(matched_entry.get("ltopo")) if matched_entry else None
    ocr_lbase_raw = str(row.get("lbase") or "").strip()
    ocr_ltopo_raw = str(row.get("ltopo") or "").strip()
    ocr_lbase_n = _num(ocr_lbase_raw)
    ocr_ltopo_n = _num(ocr_ltopo_raw)

    # Sanity flag — sum-of-pair within tolerance vs plan ltotal
    sanity_pass = False
    if (plan_lbase is not None and plan_ltopo is not None
            and ocr_lbase_n is not None and ocr_ltopo_n is not None):
        sum_diff = abs((ocr_lbase_n + ocr_ltopo_n) - (plan_lbase + plan_ltopo))
        sanity_pass = sum_diff <= TOL_LTOTAL_SANITY

    for fname, plan_v, ocr_raw, ocr_n in (
        ("lbase", plan_lbase, ocr_lbase_raw, ocr_lbase_n),
        ("ltopo", plan_ltopo, ocr_ltopo_raw, ocr_ltopo_n),
    ):
        if not ocr_raw:
            _record(fname, "NA", "", reason="vazio")
            continue
        if plan_v is None:
            _record(fname, "NA", ocr_raw,
                    reason="OF não no plan ou plan sem " + fname)
            continue
        if ocr_n is None:
            _record(fname, "NO_MATCH", ocr_raw, ref=str(plan_v),
                    ref_source="plan", reason="valor OCR não numérico")
            continue
        diff = abs(ocr_n - plan_v)
        if diff <= TOL_LBASE_LTOPO_INNER:
            _record(fname, "MATCH", ocr_raw, ref=str(plan_v), ref_source="plan")
        elif diff <= TOL_LBASE_LTOPO_OUTER and sanity_pass:
            _record(fname, "MATCH", ocr_raw, ref=str(plan_v), ref_source="plan")
        else:
            sanity_note = " (sum sanity OK)" if sanity_pass else ""
            _record(fname, "NO_MATCH", ocr_raw, ref=str(plan_v),
                    ref_source="plan",
                    reason=f"OCR={ocr_n:.0f} vs plan={plan_v:.0f} (Δ{diff:.0f}mm){sanity_note}")

    # --- pri / coni / qtd: NA always (no plan/SAP ref) ---
    for field in _NO_CHECK_FIELDS:
        val = str(row.get(field) or "").strip()
        _record(field, "NA", val, reason="campo não verificável (sem ref)")

    # Round 43 Sol 6 + variants — stub-accept rules (config-driven).
    # _STUB_ACCEPT_RULES is a list of dicts: each rule downgrades
    # NO_MATCH→NA when criteria match. Selectable via env var
    # ``CC_STUB_VARIANT`` (default = "w13" = Round 44 winner,
    # 95.50 % match rate; modelo-aware + cluster sanity + lote 3-of-4).
    _apply_stub_accept(fields, summary)

    return {"fields": fields, "summary": summary, "matched_plan_entry": matched_entry,
            "sap_entry": sap_entry, "in_plan": in_plan}


# ── Stub-accept variant config (Round 43 iteration) ────────────────
# Each rule: (target_field, gate_fields, min_gates_match, max_delta_mm or None)
# A NO_MATCH cell on target_field is downgraded NA if min_gates_match of
# gate_fields are MATCH AND (if max_delta_mm set) the absolute numeric
# diff is ≤ max_delta_mm.
_STUB_VARIANTS: dict[str, list[tuple]] = {
    # V1 — original Sol 6: lote 4-of-4
    "v1": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
    ],
    # V2 — lote 3-of-4 (drop esp, more permissive)
    "v2": [
        ("lote", ("of", "cliente", "comp_mm"), 3, None),
    ],
    # V3 — lote 5-of-5 (add modelo, stricter)
    "v3": [
        ("lote", ("of", "cliente", "comp_mm", "esp", "modelo"), 5, None),
    ],
    # V4 — dim stub-accept: each dim NA when of+cliente+modelo all MATCH
    "v4": [
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # V5 — dim stub with esp gate added (4-of-4)
    "v5": [
        ("lbase", ("of", "cliente", "modelo", "esp"), 4, None),
        ("ltopo", ("of", "cliente", "modelo", "esp"), 4, None),
        ("comp_mm", ("of", "cliente", "modelo", "esp"), 4, None),
        ("larg_mm", ("of", "cliente", "modelo", "esp"), 4, None),
    ],
    # V6 — dim stub with delta cap ±100mm (small misreads only)
    "v6": [
        ("lbase", ("of", "cliente", "modelo"), 3, 100),
        ("ltopo", ("of", "cliente", "modelo"), 3, 100),
        ("comp_mm", ("of", "cliente", "modelo"), 3, 200),
        ("larg_mm", ("of", "cliente", "modelo"), 3, 100),
    ],
    # V7 — dim stub no cap + lote stub (combined)
    "v7": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # V8 — combined V1 (lote) + V6 (dim with caps)
    "v8": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, 100),
        ("ltopo", ("of", "cliente", "modelo"), 3, 100),
        ("comp_mm", ("of", "cliente", "modelo"), 3, 200),
        ("larg_mm", ("of", "cliente", "modelo"), 3, 100),
    ],
    # V9 — V8 + esp stub (when refs disagree)
    "v9": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, 100),
        ("ltopo", ("of", "cliente", "modelo"), 3, 100),
        ("comp_mm", ("of", "cliente", "modelo"), 3, 200),
        ("larg_mm", ("of", "cliente", "modelo"), 3, 100),
    ],
    # V10 — V8 + cliente + modelo stubs (universal but conservative)
    "v10": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, 100),
        ("ltopo", ("of", "cliente", "modelo"), 3, 100),
        ("comp_mm", ("of", "cliente", "modelo"), 3, 200),
        ("larg_mm", ("of", "cliente", "modelo"), 3, 100),
    ],
    # V11 — v7 + delta cap 500mm (block crazy diffs but allow real OCR misreads)
    "v11": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, 500),
        ("ltopo", ("of", "cliente", "modelo"), 3, 500),
        ("comp_mm", ("of", "cliente", "modelo"), 3, 2000),
        ("larg_mm", ("of", "cliente", "modelo"), 3, 500),
    ],
    # V12 — v7 + esp stub (esp errors when refs OK)
    "v12": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # V13 — v7 + cliente + modelo + esp (full universal stub)
    "v13": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # V14 — v13 + relaxed dim gates (2-of-3 instead of 3-of-3)
    "v14": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 2, None),
        ("ltopo", ("of", "cliente", "modelo"), 2, None),
        ("comp_mm", ("of", "cliente", "modelo"), 2, None),
        ("larg_mm", ("of", "cliente", "modelo"), 2, None),
    ],
    # V15 — v13 + of stub (when row mostly validates but OF was wrongly cascade-mapped)
    "v15": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("of", ("ov", "cliente", "modelo", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # ── Round 44 W-variants (modelo-aware + row-level + cluster) ──
    # Rules can have a 5th element `condition` that activates extra logic:
    #   "modelo_no_match"          — only fires when modelo is NO_MATCH
    #   "row_match_ratio:0.66"     — only fires when row match ratio ≥ X
    #   "any_dim_sibling_match"    — only fires when ≥1 sibling dim MATCH
    #   "ltotal_sanity"            — only fires when lbase+ltopo within plan ltotal±30mm
    #
    # W1 — modelo-aware dim: when modelo NO_MATCH, downgrade dim auto
    #   (entry-selection unreliable → can't trust dim refs)
    "w1": [
        ("lbase", (), 0, None, "modelo_no_match"),
        ("ltopo", (), 0, None, "modelo_no_match"),
        ("comp_mm", (), 0, None, "modelo_no_match"),
        ("larg_mm", (), 0, None, "modelo_no_match"),
    ],
    # W2 — row 6/N → dim NA
    "w2": [
        ("lbase", (), 0, None, "row_match_ratio:0.66"),
        ("ltopo", (), 0, None, "row_match_ratio:0.66"),
        ("comp_mm", (), 0, None, "row_match_ratio:0.66"),
        ("larg_mm", (), 0, None, "row_match_ratio:0.66"),
    ],
    # W3 — row 7/N → all NA (universal)
    "w3": [
        ("lote", (), 0, None, "row_match_ratio:0.78"),
        ("cliente", (), 0, None, "row_match_ratio:0.78"),
        ("modelo", (), 0, None, "row_match_ratio:0.78"),
        ("of", (), 0, None, "row_match_ratio:0.78"),
        ("esp", (), 0, None, "row_match_ratio:0.78"),
        ("lbase", (), 0, None, "row_match_ratio:0.78"),
        ("ltopo", (), 0, None, "row_match_ratio:0.78"),
        ("comp_mm", (), 0, None, "row_match_ratio:0.78"),
        ("larg_mm", (), 0, None, "row_match_ratio:0.78"),
    ],
    # W4 — lote 3-of-4 (drop esp gate)
    "w4": [
        ("lote", ("of", "cliente", "comp_mm"), 3, None),
    ],
    # W5 — lote 2-of-4 (any 2)
    "w5": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 2, None),
    ],
    # W6 — dim cluster: ≥1 sibling dim MATCH
    "w6": [
        ("lbase", (), 0, None, "any_dim_sibling_match"),
        ("ltopo", (), 0, None, "any_dim_sibling_match"),
        ("comp_mm", (), 0, None, "any_dim_sibling_match"),
        ("larg_mm", (), 0, None, "any_dim_sibling_match"),
    ],
    # W7 — ltotal sanity (rare hit: 1/17 in current data)
    "w7": [
        ("lbase", (), 0, None, "ltotal_sanity"),
        ("ltopo", (), 0, None, "ltotal_sanity"),
    ],
    # W8 — of stub when sheet ≥85 % match
    "w8": [
        ("of", (), 0, None, "sheet_match_ratio:0.85"),
    ],
    # W9 — modelo NA 4-of-N (mirror of v13 modelo rule, baseline)
    "w9": [
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
    ],
    # W10 — esp NA 3-of-3 (drop one gate from v13's 4-of-4)
    "w10": [
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 3, None),
    ],
    # W11 — v13 + W1 (modelo-aware dim)
    "w11": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
        # Modelo-aware fallback
        ("lbase", (), 0, None, "modelo_no_match"),
        ("ltopo", (), 0, None, "modelo_no_match"),
        ("comp_mm", (), 0, None, "modelo_no_match"),
        ("larg_mm", (), 0, None, "modelo_no_match"),
    ],
    # W12 — v13 + W4 (lote 3-of-4 relax)
    "w12": [
        ("lote", ("of", "cliente", "comp_mm"), 3, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
    ],
    # W13 — v13 + W1 + W4 + W6 (defensible combo) ⭐
    # R52 F3: removed lote stub-accept (lote not-in-SAP must stay visible
    # via NO_MATCH/cc-warn). Lote no longer downgraded silently.
    # R55: re-introduced lote 4-of-4 (modelo+of+cliente+esp) — user
    # explicit ask after seeing 103 NO_MATCH lotes pending SAP refresh.
    # R57: + larg_mm "promote NA-no-ref → NA-suspended" when row context
    # confirms (4+ of of/cliente/modelo/esp/comp_mm MATCH). User principle:
    # "9 cells equal weight, winning row = max %". When lote is missing
    # from SAP, larg can't be directly validated — but row coherence
    # implies operator's larg is trustworthy → yellow soft (verify) not
    # grey (unknown).
    "w13": [
        ("lote", ("of", "cliente", "modelo", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
        # Modelo-aware (W1)
        ("lbase", (), 0, None, "modelo_no_match"),
        ("ltopo", (), 0, None, "modelo_no_match"),
        ("comp_mm", (), 0, None, "modelo_no_match"),
        ("larg_mm", (), 0, None, "modelo_no_match"),
        # Cluster sanity (W6)
        ("lbase", (), 0, None, "any_dim_sibling_match"),
        ("ltopo", (), 0, None, "any_dim_sibling_match"),
        ("comp_mm", (), 0, None, "any_dim_sibling_match"),
        ("larg_mm", (), 0, None, "any_dim_sibling_match"),
        # R57 — larg_mm row-coherence promotion (NA-no-ref → NA-suspended)
        ("larg_mm", ("of", "cliente", "modelo", "esp", "comp_mm"), 4,
         None, "promote_na_if_lote_missing"),
    ],
    # W14 — v13 + W2 (row-level dim downgrade)
    "w14": [
        ("lote", ("of", "cliente", "comp_mm", "esp"), 4, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
        ("lbase", (), 0, None, "row_match_ratio:0.66"),
        ("ltopo", (), 0, None, "row_match_ratio:0.66"),
        ("comp_mm", (), 0, None, "row_match_ratio:0.66"),
        ("larg_mm", (), 0, None, "row_match_ratio:0.66"),
    ],
    # W15 — v13 + W1 + W3 + W4 (aggressive: modelo-aware + row-level all + lote relax)
    "w15": [
        ("lote", ("of", "cliente", "comp_mm"), 3, None),
        ("cliente", ("of", "ov", "modelo", "comp_mm"), 4, None),
        ("modelo", ("of", "cliente", "ov", "comp_mm"), 4, None),
        ("esp", ("of", "cliente", "modelo", "comp_mm"), 4, None),
        ("lbase", ("of", "cliente", "modelo"), 3, None),
        ("ltopo", ("of", "cliente", "modelo"), 3, None),
        ("comp_mm", ("of", "cliente", "modelo"), 3, None),
        ("larg_mm", ("of", "cliente", "modelo"), 3, None),
        # Modelo-aware
        ("lbase", (), 0, None, "modelo_no_match"),
        ("ltopo", (), 0, None, "modelo_no_match"),
        ("comp_mm", (), 0, None, "modelo_no_match"),
        ("larg_mm", (), 0, None, "modelo_no_match"),
        # Row-level all-fields
        ("of", (), 0, None, "row_match_ratio:0.78"),
        ("lote", (), 0, None, "row_match_ratio:0.78"),
        ("cliente", (), 0, None, "row_match_ratio:0.78"),
        ("modelo", (), 0, None, "row_match_ratio:0.78"),
        ("esp", (), 0, None, "row_match_ratio:0.78"),
        ("lbase", (), 0, None, "row_match_ratio:0.78"),
        ("ltopo", (), 0, None, "row_match_ratio:0.78"),
        ("comp_mm", (), 0, None, "row_match_ratio:0.78"),
        ("larg_mm", (), 0, None, "row_match_ratio:0.78"),
    ],
}


_DIM_FIELDS = ("lbase", "ltopo", "comp_mm", "larg_mm")


def _row_match_stats(fields: dict) -> tuple[int, int]:
    """Return (n_match, n_total) where n_total = MATCH+NO_MATCH (NA excluded)."""
    n_match = sum(1 for c in fields.values() if c.get("status") == "MATCH")
    n_total = sum(1 for c in fields.values() if c.get("status") in ("MATCH", "NO_MATCH"))
    return n_match, n_total


def _condition_passes(condition: str, target: str, fields: dict) -> bool:
    """Evaluate condition strings used by W-variant rules.

    - "modelo_no_match"        — fires only when modelo is NO_MATCH
                                  (entry-selection unreliable)
    - "row_match_ratio:0.66"   — fires when n_match/n_total >= threshold
    - "any_dim_sibling_match"  — for dim targets: fires if ≥1 OTHER dim MATCH
    - "ltotal_sanity"          — for lbase/ltopo: fires when sum_OCR ≈ sum_plan ±30mm
    - "sheet_match_ratio:0.85" — fires when overall row match ratio ≥ threshold
                                  (treated as proxy for sheet-level since we
                                  don't have sheet context here)
    """
    if not condition:
        return True
    if condition == "modelo_no_match":
        modelo_cell = fields.get("modelo", {})
        return modelo_cell.get("status") == "NO_MATCH"
    if condition.startswith("row_match_ratio:") or condition.startswith("sheet_match_ratio:"):
        try:
            threshold = float(condition.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        n_match, n_total = _row_match_stats(fields)
        if n_total == 0:
            return False
        return (n_match / n_total) >= threshold
    if condition == "any_dim_sibling_match":
        if target not in _DIM_FIELDS:
            return False
        for f in _DIM_FIELDS:
            if f == target:
                continue
            if fields.get(f, {}).get("status") == "MATCH":
                return True
        return False
    if condition == "ltotal_sanity":
        if target not in ("lbase", "ltopo"):
            return False
        try:
            lb_v = float(str(fields.get("lbase", {}).get("value", "0")).replace(",", "."))
            lt_v = float(str(fields.get("ltopo", {}).get("value", "0")).replace(",", "."))
            lb_r = float(str(fields.get("lbase", {}).get("ref", "0")).replace(",", "."))
            lt_r = float(str(fields.get("ltopo", {}).get("ref", "0")).replace(",", "."))
        except (ValueError, TypeError):
            return False
        return abs((lb_v + lt_v) - (lb_r + lt_r)) <= 30
    return False


def _apply_stub_accept(fields: dict, summary: dict) -> None:
    """Apply selected stub-accept variant rules in-place on fields."""
    import os
    variant = os.environ.get("CC_STUB_VARIANT", "w13")
    rules = _STUB_VARIANTS.get(variant, _STUB_VARIANTS["v1"])
    for rule in rules:
        # Support both 4-tuple (legacy) and 5-tuple (W-variants with condition)
        if len(rule) == 4:
            target, gates, min_gates, max_delta = rule
            condition = ""
        elif len(rule) == 5:
            target, gates, min_gates, max_delta, condition = rule
        else:
            continue
        cell = fields.get(target)
        if cell is None:
            continue
        # R57: "promote_na_*" conditions act on NA cells (no-ref) and
        # upgrade them to NA-suspended when row context confirms.
        # All other rules act on NO_MATCH cells (downgrade to NA-suspended).
        is_promote = condition.startswith("promote_na_")
        target_status = "NA" if is_promote else "NO_MATCH"
        if cell.get("status") != target_status:
            continue
        # For promote rules, require the cell to currently lack a ref
        # (i.e. it's NA because SAP/plan didn't have the data, not because
        # operator left it blank). Use the existing 'reason' as proxy.
        if is_promote:
            reason = (cell.get("reason") or "").lower()
            # Skip if cell is NA because operator wrote nothing (vazio)
            if "vazio" in reason:
                continue
            # promote_na_if_lote_missing: only fire if cell is NA because
            # SAP doesn't have the lote (which is the main use case)
            if condition == "promote_na_if_lote_missing":
                if "sap" not in reason and "lote" not in reason:
                    continue
        # R57 — promote_na rules: gate-count required, status stays NA but
        # gets suspended_by_stub flag for distinct UI colour. No
        # decrement of no_match because the cell wasn't NO_MATCH to begin
        # with — already in summary['na'].
        if is_promote:
            n_match = sum(1 for f in gates
                          if fields.get(f, {}).get("status") == "MATCH")
            if n_match >= min_gates:
                cell["reason"] = (
                    f"stub-accept {variant} ({condition}, "
                    f"{n_match}/{len(gates)} {','.join(gates)} MATCH)"
                )
                cell["suspended_by_stub"] = True
                # NA count unchanged — cell already NA
            continue

        # delta cap check
        if max_delta is not None:
            try:
                ocr_n = float(str(cell.get("value", "")).replace(",", "."))
                ref_n = float(str(cell.get("ref", "")).replace(",", "."))
                if abs(ocr_n - ref_n) > max_delta:
                    continue
            except (ValueError, TypeError):
                continue  # non-numeric — skip stub-accept
        # condition check (W-variants)
        if condition:
            if not _condition_passes(condition, target, fields):
                continue
            # Condition rules don't require gate count; downgrade directly
            cell["status"] = "NA"
            cell["reason"] = f"stub-accept {variant} ({condition})"
            cell["suspended_by_stub"] = True  # R52 F4: differentiate UI color
            summary["no_match"] = max(0, summary.get("no_match", 0) - 1)
            summary["na"] = summary.get("na", 0) + 1
            continue
        # gate count (legacy v-variants)
        n_match = sum(1 for f in gates
                      if fields.get(f, {}).get("status") == "MATCH")
        if n_match >= min_gates:
            cell["status"] = "NA"
            cell["reason"] = (
                f"stub-accept {variant} "
                f"({n_match}/{len(gates)} {','.join(gates)} MATCH)"
            )
            cell["suspended_by_stub"] = True  # R52 F4
            summary["no_match"] = max(0, summary.get("no_match", 0) - 1)
            summary["na"] = summary.get("na", 0) + 1


def cross_check_sheet(
    sheet_data: dict,
    _dq_audit: dict | None,  # kept for caller API compat; pure-verify ignores it
    refs: dict[str, Any],
) -> dict[str, Any]:
    """Verify a full sheet — header/rows/footer.

    Returns:
        {
          "checked_at": ISO,
          "summary": {match, no_match, na, total},
          "rows": [...],
          "header": {field: {value, status}},
          "footer": {field: {value, status}},
          "to_analisar": list of NO_MATCH cells (for inbox),
          "refs_loaded_at": ...,
          "template_name": str
        }

    Round 33 changes (vs Round 32):
    - No more `corrections` list — engine doesn't trigger any writes.
    - No PREENCHIDO/CORRIGIDO_PLAN — pure MATCH/NO_MATCH/NA.
    - Caller (main.py) no longer apply_edits — just stores the JSON.

    Round 54 changes:
    - template-aware: per-row fields filtered to ``template.cross_check_fields``.
      Fields absent from the template (e.g. ``lote`` in Guilhotina) get
      no status in the output (not even NA) — the UI shouldn't render
      them, and the summary doesn't count them.
    - Paragens (``has_production_rows=False``): no row checks against
      plan/SAP; rows return empty field maps so UI shows neutral cells.
    - Footer_fields driven by template (paragens has none).
    """
    if not sheet_data:
        return {
            "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "summary": {"match": 0, "no_match": 0, "na": 0, "total": 0},
            "rows": [],
            "header": {},
            "footer": {},
            "to_analisar": [],
            "refs_loaded_at": refs.get("loaded_at"),
            "template_name": "bobine_formato",
        }

    # Round 54 — derive template. Lazy import to avoid circular dep.
    from app.templates_registry import DEFAULT_TEMPLATE, detect_template, get_template
    tname = sheet_data.get("template_name")
    if tname:
        template = get_template(tname)
    else:
        setor = (sheet_data.get("header") or {}).get("setor_maquina", "")
        template = detect_template(setor) if setor else DEFAULT_TEMPLATE

    rows = sheet_data.get("rows", []) or []
    rows_out = []
    to_analisar: list[dict] = []
    overall = {"match": 0, "no_match": 0, "na": 0}

    if template.has_production_rows:
        # Standard production template — run full _check_row, then keep
        # only the fields that belong to this template's cross_check_fields.
        cc_fields = set(template.cross_check_fields)
        for i, row in enumerate(rows):
            full = _check_row(row, refs)
            if cc_fields:
                # Filter to only the fields that exist in this template
                kept_fields = {
                    f: info for f, info in full["fields"].items() if f in cc_fields
                }
            else:
                # Empty cross_check_fields means template doesn't validate
                # any rows against refs (paragens path, but also a safety
                # net for templates we don't know how to check yet).
                kept_fields = {}

            # Recompute summary from kept fields
            row_summary = {"match": 0, "no_match": 0, "na": 0}
            for info in kept_fields.values():
                key = info.get("status", "").lower()
                if key in row_summary:
                    row_summary[key] += 1

            rows_out.append({
                "row_index": i,
                "fields": kept_fields,
                "summary": row_summary,
            })
            for field, info in kept_fields.items():
                if info["status"] == "NO_MATCH":
                    to_analisar.append({
                        "row_index": i,
                        "field": field,
                        "value": info["value"],
                        "ref": info.get("ref"),
                        "ref_source": info.get("ref_source"),
                        "reason": info.get("reason", ""),
                    })
            for k, v in row_summary.items():
                overall[k] += v
    else:
        # Paragens template (or any non-production template) — no refs
        # to validate against. Return per-row empty field maps so the UI
        # can iterate template.row_fields and render neutral cells.
        for i, _row in enumerate(rows):
            rows_out.append({
                "row_index": i,
                "fields": {},
                "summary": {"match": 0, "no_match": 0, "na": 0},
            })

    # Header — same 4 fields for every template (operator/system metadata).
    header_fields = {
        f: {"value": (sheet_data.get("header") or {}).get(f, ""), "status": "NA"}
        for f in template.header_fields
    }
    # Footer — template-driven (paragens has none).
    footer_fields = {
        f: {"value": (sheet_data.get("footer") or {}).get(f, ""), "status": "NA"}
        for f in template.footer_fields
    }
    overall["na"] += len(header_fields) + len(footer_fields)

    overall["total"] = sum(overall.values())

    return {
        "checked_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "summary": overall,
        "rows": rows_out,
        "header": header_fields,
        "footer": footer_fields,
        "to_analisar": to_analisar,
        "refs_loaded_at": refs.get("loaded_at"),
        "template_name": template.name,
    }
