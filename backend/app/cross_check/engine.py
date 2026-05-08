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

from datetime import datetime, timezone
from typing import Any

# Status enum (3 values, JSON-friendly)
CROSS_CHECK_STATUSES = ("MATCH", "NO_MATCH", "NA")

# Tolerances. Round 43 — moderate widening of dim tolerances:
# user accepted small OCR misreads (5-15mm) as MATCH provided they're
# geometrically consistent (ltotal sanity check below).
TOL_LARG_MM = 20      # ±20mm (was 0 exact); widening per Round 43 Sol 3
TOL_COMP_MM = 50      # comp tolerated ±50mm (unchanged)
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


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _best_plan_entry(entries: list[dict], modelo_ocr: str, comp_ocr: float | None) -> dict | None:
    """Mirror of factory validator's _melhor_entrada — pick the plan
    row whose designacao contains the modelo, else closest comp.

    Round 43 Sol 2: when multiple entries exist for an OF, prefer those
    with fechado='0' (active) over fechado='1' (closed). Operators
    typically reference active orders. Falls back to all entries if
    no active ones match the modelo/comp criteria.
    """
    if not entries:
        return None
    # Filter to active first; fall back to all if no active or no match
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
    matched_entry = _best_plan_entry(plan_entries, modelo_raw, comp_v) if in_plan else None

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
        if plan_des and modelo_raw.upper() in plan_des.upper():
            _record("modelo", "MATCH", modelo_raw, ref=plan_des, ref_source="plan")
        else:
            _record("modelo", "NO_MATCH", modelo_raw, ref=plan_des[:50] or "(vazio)",
                    ref_source="plan",
                    reason=f"modelo '{modelo_raw}' não é substring de designacao")

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
    "w13": [
        ("lote", ("of", "cliente", "comp_mm"), 3, None),  # W4 relax
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
        if cell is None or cell.get("status") != "NO_MATCH":
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
          "refs_loaded_at": ...
        }

    Round 33 changes (vs Round 32):
    - No more `corrections` list — engine doesn't trigger any writes.
    - No PREENCHIDO/CORRIGIDO_PLAN — pure MATCH/NO_MATCH/NA.
    - Caller (main.py) no longer apply_edits — just stores the JSON.
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
        }

    rows = sheet_data.get("rows", []) or []
    rows_out = []
    to_analisar: list[dict] = []
    overall = {"match": 0, "no_match": 0, "na": 0}

    for i, row in enumerate(rows):
        result = _check_row(row, refs)
        rows_out.append({"row_index": i, **result})
        for field, info in result["fields"].items():
            if info["status"] == "NO_MATCH":
                to_analisar.append({
                    "row_index": i,
                    "field": field,
                    "value": info["value"],
                    "ref": info.get("ref"),
                    "ref_source": info.get("ref_source"),
                    "reason": info.get("reason", ""),
                })
        for k, v in result["summary"].items():
            overall[k] += v

    # Header / footer: NA for everything (operator/system metadata)
    header_fields = {
        f: {"value": (sheet_data.get("header") or {}).get(f, ""), "status": "NA"}
        for f in ("operador", "n_operador", "setor_maquina", "data")
    }
    footer_fields = {
        f: {"value": (sheet_data.get("footer") or {}).get(f, ""), "status": "NA"}
        for f in ("colunas_produzidas", "horas_trabalhadas")
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
    }
