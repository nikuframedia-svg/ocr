"""L2 — lexicon-aware auto-fix with confusion-weighted distance.

Per-field policy (deliberate, opinionated):

============  ===================================================
Field         Snap policy
============  ===================================================
cliente       rapidfuzz token_sort_ratio >= 85 against 42 known
modelo        prefix-only snap against modelo_prefix list (60),
              confusion-weighted Levenshtein <= 1
lote          NO SNAP — identifier; snap masks real OCR errors
of            NO SNAP — masks digit-misread errors
ov            NO SNAP — same reason
horas         normalize "8,00" -> "8:00", trim trailing "h"
others        no L2 (handled by L1 + L3)
============  ===================================================

Each snap returns ``SnapResult`` with provenance: ``raw``,
``snapped``, ``rule``, ``distance``, ``applied: bool``. Audit trail
flows through L3 → final ``CellAudit``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz


@dataclass(frozen=True)
class SnapResult:
    raw: str
    snapped: str
    rule: str            # e.g. "cliente.token_sort"
    distance: float      # 0 = exact, higher = looser
    applied: bool        # snapped != raw
    candidates: tuple[str, ...] = ()  # other near-matches considered


def _load_lexicon(repo: Path) -> dict:
    p = repo / "backend" / "app" / "pipeline" / "inference" / "lexicons" / "observed.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_learned(repo: Path) -> dict:
    p = repo / "lexicons" / "learned.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# Lexicons cached at module load. Re-import the module to refresh
# after re-running mining.
_REPO = Path(__file__).resolve().parents[3]
_LEX = _load_lexicon(_REPO)
_LEARNED = _load_learned(_REPO)
_CLIENTES: tuple[str, ...] = tuple(_LEX.get("cliente", []))
_MODELO_PREFIXES: tuple[str, ...] = tuple(_LEX.get("modelo_prefix", []))
_CONFUSION: dict[str, dict[str, float]] = _LEARNED.get("confusion_matrix", {})
# Full modelo lexicon mined from ground_truth (Round 23). Used by
# snap_modelo for full-string Levenshtein-1 lookup before falling back
# to prefix-only snap. Format: list of {"value": str, "count": int}.
_MODELOS_FULL: tuple[str, ...] = tuple(
    entry["value"] for entry in _LEARNED.get("modelos_full", [])
)


def _load_sap_plan(repo: Path) -> dict:
    """Round 27: real refs mined from StockSAP + plan_colunas_cpis.

    Used for snap_lote (SAP lookup) and snap_of (plan lookup +
    Lev-1 with OV disambiguation). Empty dict if file missing.
    """
    p = repo / "lexicons" / "sap_plan_mined.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


_REFS = _load_sap_plan(_REPO)
_LOTES_SAP: frozenset[str] = frozenset(_REFS.get("lotes_sap", []))
# OFs in plan are integers; we keep both string and int sets for fast lookup
_OFS_PLAN_STR: frozenset[str] = frozenset(str(of) for of in _REFS.get("ofs_plan", []))
# OF (str) -> set of OVs found for that OF in the plan. Used to disambiguate
# Lev-1 OF candidates: when our row has OV "X" and only one Lev-1 candidate
# OF has X in its plan entries, snap to it.
_OF_TO_OVS: dict[str, frozenset[str]] = {
    of_str: frozenset(e["ov"] for e in entries if e.get("ov"))
    for of_str, entries in _REFS.get("of_to_entries", {}).items()
}
# OF (str) -> tuple of plan designacao strings (uppercase). Used by
# snap_modelo for OF-scoped Lev-1 against the actual production-order
# modelos, dramatically reducing search space vs full lexicon scan.
_OF_TO_DESIGNACOES: dict[str, tuple[str, ...]] = {
    of_str: tuple(e["designacao"].upper().strip() for e in entries if e.get("designacao"))
    for of_str, entries in _REFS.get("of_to_entries", {}).items()
}
# OF (str) -> tuple of full plan entries (dicts with ov/cliente/comp/esp/lbase/...)
# Used by snap_of multi-field tie-break (Round 28): when Lev-1 has multiple
# candidates and OV-disambig fails, score each by modelo-substring + comp ±50mm.
_OF_TO_ENTRIES_FULL: dict[str, tuple[dict, ...]] = {
    of_str: tuple(entries)
    for of_str, entries in _REFS.get("of_to_entries", {}).items()
}

# Round 37 — closed-list cliente aliases (operator shorthand → canonical).
# User-managed via lexicons/cliente_aliases.json. Hot-loaded at module
# import. snap_cliente Stage 0 uses this for direct mapping.
def _load_cliente_aliases(repo: Path) -> dict[str, str]:
    p = repo / "lexicons" / "cliente_aliases.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {k.upper(): v.upper() for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, str)}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}

_CLIENTE_ALIASES: dict[str, str] = _load_cliente_aliases(_REPO)

# Round 37 — OF → set of distinct clientes seen in plan_colunas. 99.8 %
# of OFs have a single cliente, used for unambiguous OF→cliente snap.
_OF_TO_CLIENTES: dict[str, frozenset[str]] = {
    of_str: frozenset(
        (e.get("cliente") or "").strip().upper()
        for e in entries
        if (e.get("cliente") or "").strip()
    )
    for of_str, entries in _REFS.get("of_to_entries", {}).items()
}

# Round 37 — OV → OF reverse map (only when OV is unambiguous, i.e. it
# appears in exactly one OF in plan). Used by snap_of Stage 0b-prime to
# recover garbled OF when operator wrote a valid OV.
def _build_ov_to_of(refs: dict) -> dict[str, str]:
    counts: dict[str, set[str]] = {}
    for of_str, entries in refs.get("of_to_entries", {}).items():
        for e in entries:
            ov = str(e.get("ov") or "").strip()
            if not ov:
                continue
            counts.setdefault(ov, set()).add(of_str)
    return {ov: next(iter(of_set)) for ov, of_set in counts.items() if len(of_set) == 1}

_OV_TO_OF: dict[str, str] = _build_ov_to_of(_REFS)

# Round 39 — OV → all OFs (NOT filtered to unambiguous). Used by
# snap_of Stage 0d to narrow search space when OV exists but maps to
# multiple OFs; multi-field scoring then picks best candidate.
def _build_ov_to_ofs_all(refs: dict) -> dict[str, tuple[str, ...]]:
    out: dict[str, set[str]] = {}
    for of_str, entries in refs.get("of_to_entries", {}).items():
        for e in entries:
            ov = str(e.get("ov") or "").strip()
            if not ov:
                continue
            out.setdefault(ov, set()).add(of_str)
    return {ov: tuple(of_set) for ov, of_set in out.items()}

_OV_TO_OFS_ALL: dict[str, tuple[str, ...]] = _build_ov_to_ofs_all(_REFS)


# Round 53 — field-agnostic candidate indexes for snap_of pool.
# Used when OF/OV/cliente are ALL wrong but modelo/dims are correct.

def _build_modelo_token_index(refs: dict) -> dict[str, frozenset[str]]:
    """Map each alphanumeric token (≥5 chars) in plan designacao to
    the set of OFs that contain it. Lets snap_of find candidates by
    operator's modelo even when all identifiers are OCR garbage.
    """
    import re as _re
    out: dict[str, set[str]] = {}
    for of_str, entries in refs.get("of_to_entries", {}).items():
        for e in entries:
            des = (e.get("designacao") or "").upper()
            for tok in _re.findall(r"[A-Z0-9]{5,}", des):
                out.setdefault(tok, set()).add(of_str)
    return {k: frozenset(v) for k, v in out.items()}


_MODELO_TOKEN_TO_OFS: dict[str, frozenset[str]] = _build_modelo_token_index(_REFS)


def _build_geometry_index(refs: dict) -> dict[tuple[int, int], frozenset[str]]:
    """Bucket plan entries by (lbase//30, ltopo//30) — 30mm buckets.
    Used to find candidates by geometric similarity when identifiers
    are all wrong but lbase/ltopo are roughly correct.
    """
    out: dict[tuple[int, int], set[str]] = {}
    for of_str, entries in refs.get("of_to_entries", {}).items():
        for e in entries:
            lb = e.get("lbase")
            lt = e.get("ltopo")
            if lb is None or lt is None:
                continue
            try:
                key = (int(float(lb)) // 30, int(float(lt)) // 30)
            except (ValueError, TypeError):
                continue
            out.setdefault(key, set()).add(of_str)
    return {k: frozenset(v) for k, v in out.items()}


_GEOMETRY_INDEX: dict[tuple[int, int], frozenset[str]] = _build_geometry_index(_REFS)


def _to_num_local(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _multifield_score(plan_of: str, plan_entry: dict, row: dict) -> tuple[int, list[str]]:
    """Round 39 — score how well a plan entry matches a kanban row.

    Higher score = stronger match. Used by snap_of stages 0d/0e when
    direct lookup fails. Weights tuned empirically on 21 sheets.
    """
    import re as _re
    score = 0
    reasons: list[str] = []

    cli = (row.get("cliente") or "").strip().upper()
    plan_cli = (plan_entry.get("cliente") or "").strip().upper()
    if cli and plan_cli == cli:
        score += 3; reasons.append("cli_exact")
    elif cli and plan_cli and cli in plan_cli:
        score += 1; reasons.append("cli_partial")

    ov = str(row.get("ov") or "").strip()
    plan_ov = str(plan_entry.get("ov") or "").strip()
    if ov and plan_ov == ov:
        score += 4; reasons.append("ov_exact")
    elif ov and plan_ov:
        # OV Lev-1: same length 1 char diff (substitution)
        if len(ov) == len(plan_ov):
            diffs = sum(1 for a, b in zip(ov, plan_ov) if a != b)
            if diffs == 1:
                score += 3; reasons.append("ov_lev1")
        # OV Lev-1: length diff 1 (insertion/deletion). e.g.
        # OCR '260054' (6) vs plan '2600054' (7) — operator/OCR
        # missed a leading 0. Check by trying every position.
        elif abs(len(ov) - len(plan_ov)) == 1:
            short, long_ = (ov, plan_ov) if len(ov) < len(plan_ov) else (plan_ov, ov)
            for i in range(len(long_)):
                if short == long_[:i] + long_[i+1:]:
                    score += 3; reasons.append("ov_lev1_indel")
                    break

    mod = (row.get("modelo") or "").strip().upper()
    plan_des = (plan_entry.get("designacao") or "").upper()
    if mod and len(mod) >= 4 and mod in plan_des:
        score += 3; reasons.append("mod_substr")

    comp = _to_num_local(row.get("comp_mm"))
    plan_comp = _to_num_local(plan_entry.get("comp"))
    if comp and plan_comp and abs(comp - plan_comp) <= 50:
        score += 2; reasons.append("comp")

    for k in ("lbase", "ltopo"):
        v = _to_num_local(row.get(k))
        pv = _to_num_local(plan_entry.get(k))
        if v and pv and abs(v - pv) <= 5:
            score += 2; reasons.append(k)

    of_ocr = str(row.get("of") or "").strip()
    # Compare RAW OCR to plan OF (catches 1-char misreads like `d↔0`,
    # `T↔7`, `A↔1` that aren't pure digit confusions).
    if of_ocr and len(of_ocr) == len(plan_of):
        diffs = sum(1 for a, b in zip(of_ocr, plan_of) if a != b)
        if diffs == 1:
            score += 5; reasons.append("of_lev1")
    # Also check cleaned (digit-only) version for cases like `2.6122A`
    of_clean = _re.sub(r"\D", "", of_ocr)
    if of_clean and len(of_clean) == len(plan_of) and of_clean != of_ocr:
        diffs = sum(1 for a, b in zip(of_clean, plan_of) if a != b)
        if diffs == 1:
            score += 5; reasons.append("of_lev1_clean")

    return score, reasons


def _ofs_for_ov(ov: str) -> tuple[str, ...]:
    """Round 48 Phase A — return all OF strs in plan that share this OV.

    Used by snap_of joint-score override to find candidates for OF
    re-evaluation. Returns empty tuple if OV not in plan.
    """
    if not ov:
        return ()
    out: list[str] = []
    for of_str, entries in _OF_TO_ENTRIES_FULL.items():
        for e in entries:
            if str(e.get("ov", "")) == ov:
                out.append(of_str)
                break
    return tuple(out)


def _score_of_for_row(of: str, row: dict) -> tuple[int, list[str], dict]:
    """Round 48 Phase A — joint score for OF re-evaluation.

    Two-tier output:
    - PRIMARY score (int 0-5): coarse match across modelo/comp/lbase/ltopo/esp.
      Used for override threshold (winner needs gap >= 2).
    - TIE-BREAKERS dict: secondary signals to pick among equal-scored OFs.

    Round 48b extension — when many OFs share OV with similar dims,
    primary score ties. Tie-breakers (in priority order):
      a) modelo_exact_match: full OCR matches designacao (including suffix)
      b) of_char_distance:   how close OCR.of digits are to candidate OF
      c) active_flag:        fechado='0' preferred over fechado='1'
      d) comp_delta:         smaller |OCR - plan.comp| wins
      e) lbase_delta:        smaller |OCR - plan.lbase|

    Returns (primary_score, primary_reasons, tiebreakers_dict).
    """
    entries = _OF_TO_ENTRIES_FULL.get(of, ())
    if not entries:
        return (0, [], {})
    best_score = 0
    best_reasons: list[str] = []
    best_tb: dict = {}
    for e in entries:
        score = 0
        reasons: list[str] = []
        tb: dict = {}
        # modelo (primary)
        mod = (row.get("modelo", "") or "").upper().strip()
        des = str(e.get("designacao", "")).upper()
        ft = des.split(" - ", 1)[0].strip()
        if mod and len(mod) >= 4:
            if mod in des:
                score += 1
                reasons.append("modelo_substring")
                # tie-breaker: full-string match vs first-token-only
                if mod == des.strip():
                    tb["modelo_exact"] = 1.0
                elif mod == ft:
                    tb["modelo_exact"] = 0.5  # exact first-token, no suffix
                else:
                    tb["modelo_exact"] = 0.7  # OCR contains more than ft
            elif ft and len(ft) == len(mod):
                diffs = sum(1 for a, b in zip(ft, mod) if a != b)
                if diffs <= 1:
                    score += 1
                    reasons.append("modelo_lev1")
                    tb["modelo_exact"] = 0.3
        # comp (primary)
        try:
            cn = float(str(row.get("comp_mm", "") or "").replace(",", "."))
            ec = float(e.get("comp") or 0)
            if cn and ec:
                comp_delta = abs(cn - ec)
                tb["comp_delta"] = comp_delta
                if comp_delta <= 50:
                    score += 1
                    reasons.append("comp_50mm")
        except (ValueError, TypeError):
            pass
        # lbase (primary)
        try:
            lb = float(str(row.get("lbase", "") or "").replace(",", "."))
            elb = float(e.get("lbase") or 0)
            if lb and elb:
                lbase_delta = abs(lb - elb)
                tb["lbase_delta"] = lbase_delta
                if lbase_delta <= 30:
                    score += 1
                    reasons.append("lbase_30mm")
        except (ValueError, TypeError):
            pass
        # ltopo (primary)
        try:
            lt = float(str(row.get("ltopo", "") or "").replace(",", "."))
            elt = float(e.get("ltopo") or 0)
            if lt and elt:
                ltopo_delta = abs(lt - elt)
                tb["ltopo_delta"] = ltopo_delta
                if ltopo_delta <= 30:
                    score += 1
                    reasons.append("ltopo_30mm")
        except (ValueError, TypeError):
            pass
        # ltotal sanity (primary, R49b) — geometric coherence: OCR's
        # lbase + ltopo should sum to plan.ltotal within ±5mm. Detects
        # when one of the dims is OCR-misread but the other compensates.
        try:
            lb_val = float(str(row.get("lbase", "") or "").replace(",", "."))
            lt_val = float(str(row.get("ltopo", "") or "").replace(",", "."))
            plan_ltotal = float(e.get("ltotal") or 0)
            if lb_val and lt_val and plan_ltotal:
                ltotal_delta = abs((lb_val + lt_val) - plan_ltotal)
                tb["ltotal_delta"] = ltotal_delta
                if ltotal_delta <= 5:
                    score += 1
                    reasons.append("ltotal_5mm")
        except (ValueError, TypeError):
            pass
        # esp (primary, vs plan)
        try:
            ep = float(str(row.get("esp", "") or "0").replace(",", "."))
            ee = float(e.get("esp") or 0)
            if ep and ee and abs(ep - ee) <= 0.05:
                score += 1
                reasons.append("esp_match")
        except (ValueError, TypeError):
            pass
        # SAP-via-lote coherence (R49c) — when OCR.lote IS in SAP, the
        # OCR.larg, OCR.esp should match SAP[lote] independently of plan.
        # Adds 0-2 to score:
        #   +1 if SAP[lote].larg ≈ OCR.larg (±20mm)
        #   +1 if SAP[lote].esp == OCR.esp (±0.05)
        # This validates that the operator's lote is geometrically
        # consistent with what they wrote for larg/esp — independent
        # of the plan. Useful when plan has multiple OFs with same
        # lbase/ltopo but the lote-pair differs.
        lote_ocr = str(row.get("lote", "") or "").upper().strip()
        if lote_ocr:
            sap_full = _REFS.get("lotes_sap_full", {})
            sap_e = sap_full.get(lote_ocr)
            if sap_e:
                # SAP-larg coherence
                try:
                    larg_ocr = float(str(row.get("larg_mm", "") or "").replace(",", "."))
                    sap_larg = float(sap_e.get("larg") or 0)
                    if larg_ocr and sap_larg and abs(larg_ocr - sap_larg) <= 20:
                        score += 1
                        reasons.append("sap_larg_match")
                except (ValueError, TypeError):
                    pass
                # SAP-esp coherence
                try:
                    sap_esp = float(sap_e.get("esp") or 0)
                    if ep and sap_esp and abs(ep - sap_esp) <= 0.05:
                        score += 1
                        reasons.append("sap_esp_match")
                except (ValueError, TypeError):
                    pass
                # Material consistency (R49d) — SAP[lote].desc contains
                # material grade (e.g. "S235JR", "S355J0"). Plan[OF]
                # has explicit `material` column. If both populated and
                # MATCH → +1. Catches lote substitutions where operator
                # used wrong stock (different material than plan needs).
                sap_desc = str(sap_e.get("desc", "") or "").strip().upper()
                plan_mat = str(e.get("material", "") or "").strip().upper()
                if sap_desc and plan_mat:
                    # SAP.desc may have trailing space or other suffix;
                    # tokenise on whitespace and check first token equals plan
                    sap_first = sap_desc.split()[0] if sap_desc.split() else ""
                    if sap_first and sap_first == plan_mat:
                        score += 1
                        reasons.append("material_match")
        # tie-breaker: active OF preferred
        tb["active"] = 1 if str(e.get("fechado", "0")) == "0" else 0
        # tie-breaker: OCR.of closeness (computed at OF level, not entry)
        of_ocr = str(row.get("of", "") or "").strip()
        if of_ocr and len(of_ocr) == len(of):
            tb["of_char_diff"] = sum(1 for a, b in zip(of_ocr, of) if a != b)
        else:
            tb["of_char_diff"] = 99
        if score > best_score:
            best_score = score
            best_reasons = reasons
            best_tb = tb
        elif score == best_score and best_tb:
            # If equal, prefer entry with better modelo_exact / smaller deltas
            if tb.get("modelo_exact", 0) > best_tb.get("modelo_exact", 0):
                best_reasons = reasons
                best_tb = tb
    return (best_score, best_reasons, best_tb)


def _num_or_none(v):
    """Round 51 — float or None for safe arithmetic comparisons."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _score_of_holistic(of: str, row: dict) -> int:
    """Round 51 — equal-weight 0-9 score: count of fields where the
    operator's row matches plan/SAP canonical for this OF.

    User principle: "os campos têm o mesmo peso, encontrar o melhor
    match em TODOS os campos". Each verifiable field = +1 point.

    9 fields scored (per-OF discriminating + per-lote informative):
      1. cliente match (op vs plan[OF].cliente, alias-resolved)
      2. ov match (op vs plan[OF].ov, exact)
      3. modelo substring (op upper in plan.designacao upper)
      4. comp ±50mm
      5. lbase ±30mm
      6. ltopo ±30mm
      7. esp ±0.05
      8. larg via SAP[op_lote].larg ±20mm
      9. material consistency (plan[OF].material == SAP[op_lote].desc first token)

    Fields 8/9 are tied to op_lote (same across all OF candidates) so
    they don't discriminate between OFs but contribute to absolute score.
    Use this score to compare candidates AND assess overall row quality.

    Multiple plan entries per OF: returns max across entries.
    Returns 0 if OF not in plan.
    """
    entries = _OF_TO_ENTRIES_FULL.get(of, ())
    if not entries:
        return 0

    op_cli_raw = (row.get("cliente") or "").strip().upper()
    op_cli = _CLIENTE_ALIASES.get(op_cli_raw, op_cli_raw)
    op_ov = str(row.get("ov") or "").strip()
    op_mod = (row.get("modelo") or "").strip().upper()
    op_comp = _num_or_none(row.get("comp_mm"))
    op_lb = _num_or_none(row.get("lbase"))
    op_lt = _num_or_none(row.get("ltopo"))
    op_esp = _num_or_none(row.get("esp"))
    op_larg = _num_or_none(row.get("larg_mm"))
    op_lote = (row.get("lote") or "").strip().upper()

    sap_e = _REFS.get("lotes_sap_full", {}).get(op_lote) if op_lote else None
    sap_larg = _num_or_none(sap_e.get("larg")) if sap_e else None
    sap_desc_first = ""
    if sap_e:
        d = (sap_e.get("desc") or "").strip().upper()
        sap_desc_first = d.split()[0] if d.split() else ""

    best = 0
    for e in entries:
        s = 0
        if op_cli and op_cli == (e.get("cliente") or "").strip().upper():
            s += 1
        if op_ov and op_ov == str(e.get("ov") or "").strip():
            s += 1
        des = (e.get("designacao") or "").upper()
        if op_mod and len(op_mod) >= 4 and op_mod in des:
            s += 1
        plan_comp = _num_or_none(e.get("comp"))
        if op_comp is not None and plan_comp is not None and abs(op_comp - plan_comp) <= 100:
            s += 1  # R52: ±100mm matches engine.py TOL_COMP_MM
        plan_lb = _num_or_none(e.get("lbase"))
        if op_lb is not None and plan_lb is not None and abs(op_lb - plan_lb) <= 30:
            s += 1
        plan_lt = _num_or_none(e.get("ltopo"))
        if op_lt is not None and plan_lt is not None and abs(op_lt - plan_lt) <= 30:
            s += 1
        plan_esp = _num_or_none(e.get("esp"))
        if op_esp is not None and plan_esp is not None and abs(op_esp - plan_esp) <= 0.05:
            s += 1
        if sap_larg is not None and op_larg is not None and abs(op_larg - sap_larg) <= 20:
            s += 1
        plan_mat = (e.get("material") or "").strip().upper()
        if plan_mat and sap_desc_first and plan_mat == sap_desc_first:
            s += 1
        if s > best:
            best = s
    return best


def _conf_weighted_dist(a: str, b: str, max_dist: float = 1.0) -> float:
    """Levenshtein where substitutions present in confusion matrix
    cost 0.5 instead of 1.0. Returns ``inf`` if guaranteed to exceed
    ``max_dist`` (early-exit).

    Substitution direction: when computing ``dist(a, b)`` we treat
    ``a`` as gold and ``b`` as ocr; cost is reduced if ``a -> b`` is a
    common confusion (mined from gold-vs-ocr pairs).
    """
    if a == b:
        return 0.0
    la, lb = len(a), len(b)
    if abs(la - lb) > int(max_dist) + 1:
        return float("inf")

    # Standard Levenshtein DP with weighted substitution.
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        ca = a[i - 1]
        curr = [i] + [0.0] * lb  # type: ignore
        for j in range(1, lb + 1):
            cb = b[j - 1]
            if ca == cb:
                cost = 0.0
            else:
                # Cheaper if (ca -> cb) is in confusion matrix at p > 0.10
                p = _CONFUSION.get(ca, {}).get(cb, 0.0)
                cost = 0.5 if p > 0.10 else 1.0
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr  # type: ignore
    return prev[lb]


def snap_cliente(value: str, *, row_context: dict | None = None) -> SnapResult:
    """Multi-stage cliente snap.

    Stage 0 (R37): closed-list alias lookup. Operator shorthand
        ("SYNDICAT", "SK-T-BELUX", "LICHT NL") doesn't fuzzy-match
        canonical (>15 char diff). Hardcoded mapping in
        lexicons/cliente_aliases.json covers known shortcuts.
    Stage 1: exact match in _CLIENTES.
    Stage 2: whitespace-only diff ("MTGBELUX" / "MTG BELUX").
    Stage 3 (R37): OF→cliente unique. When row's OF in plan has exactly
        one cliente (99.8% of OFs), snap. Catches cases where operator
        wrote a non-canonical name not in the alias list (e.g.
        "TECPOLES" → "TECPOLES GMBH", "ESOM" → "ESOM SAS").
    Stage 4: rapidfuzz token_sort_ratio >= 85 against _CLIENTES.

    Refuses to snap: nothing — cliente is the field where snap is
    most useful and gold is the truth.
    """
    raw = value or ""
    if not raw:
        return SnapResult(raw=raw, snapped=raw, rule="cliente.empty", distance=0.0, applied=False)
    su = raw.upper().strip()

    # Stage 0 (R37): closed-list alias.
    if su in _CLIENTE_ALIASES:
        canonical = _CLIENTE_ALIASES[su]
        return SnapResult(raw=raw, snapped=canonical, rule="cliente.alias",
                          distance=0.0, applied=True)

    # Stage 1 (R37): OF → cliente unique. Plan is the authority — the
    # canonical name from plan_colunas ALWAYS wins over operator
    # shorthand, even when shorthand happens to be in our lexicon
    # (_CLIENTES has older non-canonical entries like "TECPOLES" without
    # the GMBH suffix; plan has the full "TECPOLES GMBH"). 99.8 % of
    # OFs have a single cliente → deterministic snap.
    if row_context:
        of = str(row_context.get("of", "")).strip()
        if of:
            cli_set = _OF_TO_CLIENTES.get(of, frozenset())
            if len(cli_set) == 1:
                canonical = next(iter(cli_set))
                if canonical and canonical != su:
                    return SnapResult(raw=raw, snapped=canonical,
                                      rule="cliente.of_unique",
                                      distance=99.0, applied=True)

    # Stage 1.5 (R41): OV → cliente unanimous. When OF is unresolvable
    # but OV is in plan, derive cliente from the set of OFs sharing
    # that OV. Only fires when ALL OFs agree (zero false positives).
    if row_context:
        ov_ctx = str(row_context.get("ov", "")).strip()
        if ov_ctx and ov_ctx in _OV_TO_OFS_ALL:
            ov_cli_set: set[str] = set()
            for plan_of in _OV_TO_OFS_ALL[ov_ctx]:
                for e in _OF_TO_ENTRIES_FULL.get(plan_of, ()):
                    c = (e.get("cliente") or "").strip().upper()
                    if c:
                        ov_cli_set.add(c)
            if len(ov_cli_set) == 1:
                canonical = next(iter(ov_cli_set))
                if canonical != su:
                    return SnapResult(raw=raw, snapped=canonical,
                                      rule="cliente.ov_unanimous",
                                      distance=99.0, applied=True)

    if su in _CLIENTES:
        return SnapResult(raw=raw, snapped=su, rule="cliente.exact", distance=0.0,
                          applied=raw != su)

    # Stage 2: whitespace-only difference.
    nosp = su.replace(" ", "")
    for c in _CLIENTES:
        if c.replace(" ", "") == nosp and c != su:
            return SnapResult(raw=raw, snapped=c, rule="cliente.no_space_match",
                              distance=0.0, applied=True)

    # Stage 4: fuzzy match. token_sort handles word reorder; ratio
    # handles single-token typos. Take the better of the two.
    scored: list[tuple[str, float]] = []
    for c in _CLIENTES:
        s_token = fuzz.token_sort_ratio(su, c)
        s_ratio = fuzz.ratio(su, c)
        scored.append((c, max(s_token, s_ratio)))
    scored.sort(key=lambda x: -x[1])
    best, best_score = scored[0]
    if best_score >= 85 and best != su:
        candidates = tuple(c for c, s in scored[:5] if s >= 85)
        return SnapResult(raw=raw, snapped=best, rule="cliente.fuzzy",
                          distance=100 - best_score, applied=True, candidates=candidates)
    return SnapResult(raw=raw, snapped=raw, rule="cliente.no_match",
                      distance=100 - best_score if scored else 100.0, applied=False)


def snap_modelo(value: str, *, row_context: dict | None = None) -> SnapResult:
    """Multi-stage modelo snap.

    Stage P0 (NEW, Round 27b): plan-substring no-op. If OCR modelo IS
        already substring of any plan.designacao for the row's OF,
        confirm — don't change.

    Stage P1 (NEW, Round 27b): trailing-punct strip + substring retry.
        ``CONIPROT.`` → ``CONIPROT`` when the stripped form becomes
        substring of plan.designacao. Cat A.

    Stage P2 (NEW, Round 27b): OF-scoped Lev-1 against plan.designacao
        first-tokens. Snaps OCR digit/letter confusions against the
        actual modelos for the matched OF (e.g. ``CB04E63D`` →
        ``CBO4E63D`` because plan has ``CBO4E63D - FL PL`` for that OF).
        Cat B — biggest single recoverable category (~34 cells).

    Stage 0 (existing): full-string Lev-1 against ``_MODELOS_FULL`` (94 GT)
    Stage 0b (existing): Lev-2 fallback with stricter tie-break
    Stage 1 (existing): prefix-only Lev-1 against ``_MODELO_PREFIXES``

    The new stages need ``row_context["of"]`` to be set; callers without
    it (or with an OF not in plan) fall through to legacy stages.
    """
    raw = value or ""
    if not raw:
        return SnapResult(raw=raw, snapped=raw, rule="modelo.empty", distance=0.0, applied=False)
    su = raw.upper().strip()
    if len(su) < 4:
        return SnapResult(raw=raw, snapped=raw, rule="modelo.too_short", distance=0.0, applied=False)

    of = (row_context or {}).get("of", "") if row_context else ""
    designacoes = _OF_TO_DESIGNACOES.get(str(of).strip(), ()) if of else ()

    # Round 42 — PT special chars + prefix-superset guard.
    # Why: legacy lexicon Lev-2 strips `1ª` -> `49` (e.g. CA08E08B-1ª
    # wrongly snapped to CA08E08B-149). When OCR is a prefix of a known
    # canonical, that's a strong signal — operator wrote a shorter form.
    # How to apply: run BEFORE all other stages. If OCR is exact prefix
    # of any plan designacao first-token OR lexicon entry, snap to the
    # longest such canonical. Otherwise, if OCR contains PT special
    # chars, preserve as-is (legacy Lev-2 cannot be trusted with them).
    _has_non_ascii = any(ord(c) > 127 for c in su)

    # Round 60 — unified longest-canonical-FT (replaces R43 Sol 8 exact
    # + R42 prefix-superset for plan FTs). Collect ALL plan FTs that
    # match OCR exactly OR have OCR as prefix, then snap to the LONGEST
    # such FT (most canonical / specific name).
    #
    # Examples this resolves:
    #   - OCR=`OMEGA606M`, plan has `OMEGA606M` AND `OMEGA606MABI` → snap
    #     to `OMEGA606MABI` (operator wrote shorter form, plan has
    #     longer canonical).
    #   - OCR=`CFC5F45R`, plan has only `CFC5F45R` → exact, no-op
    #     (R43 Sol 8 preserved — don't extend via lexicon when plan is
    #     authoritative).
    canonical_candidates: list[tuple[str, int, str]] = []  # (ft, length, kind)
    for d in designacoes:
        ft = d.split(" - ", 1)[0].strip()
        ft_up = ft.upper()
        if not ft_up:
            continue
        if ft_up == su:
            canonical_candidates.append((ft, len(ft_up), "exact"))
        elif ft_up.startswith(su) and len(ft_up) > len(su):
            canonical_candidates.append((ft, len(ft_up), "prefix"))
    if canonical_candidates:
        # Longest FT wins (most canonical). Exact (==OCR) and prefix
        # (extension of OCR) compete on length; longest is most specific.
        canonical_candidates.sort(key=lambda x: -x[1])
        best_ft, best_len, kind = canonical_candidates[0]
        return SnapResult(raw=raw, snapped=best_ft,
                          rule=f"modelo.plan_canonical_{kind}",
                          distance=float(best_len - len(su)),
                          applied=(raw.strip().upper() != best_ft.upper()),
                          candidates=tuple(ft for ft, _, _ in canonical_candidates[:5]))

    # Lexicon prefix-superset fallback (R42) — only when NO plan FT
    # matched above. Preserved verbatim for cases where OCR is partial
    # form of a known lexicon entry but plan doesn't have it.
    superset_candidates: list[tuple[str, int, str]] = []
    for canonical in _MODELOS_FULL:
        c_up = canonical.upper().strip()
        if c_up.startswith(su) and len(c_up) > len(su):
            superset_candidates.append((canonical, len(c_up), "lex"))
    if superset_candidates:
        superset_candidates.sort(key=lambda x: -x[1])
        best, _best_len, src = superset_candidates[0]
        return SnapResult(raw=raw, snapped=best,
                          rule=f"modelo.prefix_superset_{src}",
                          distance=float(_best_len - len(su)),
                          applied=raw.strip() != best.strip(),
                          candidates=tuple(c for c, _, _ in superset_candidates[:5]))
    if _has_non_ascii:
        return SnapResult(raw=raw, snapped=raw,
                          rule="modelo.pt_special_preserved",
                          distance=0.0, applied=False)

    # ── Stages P0–P2: OF-scoped against plan.designacao ──
    if designacoes:
        # P0 (R60): OCR is substring of some plan designacao (anywhere,
        # not just first-token — that was handled by the canonical block
        # above). If the matched designacao's first-token differs from
        # OCR, snap to the FT (plan canonical name). If OCR already IS
        # the FT, no-op confirm.
        # Example: OCR=`CD03P502` substring of `CD03P10A - CD03P502 +
        # FURACAO...` → snap to `CD03P10A` (canonical primary code).
        matched_designacao = None
        for d in designacoes:
            if su in d.upper():
                matched_designacao = d
                break
        if matched_designacao is not None:
            ft = matched_designacao.split(" - ", 1)[0].strip()
            ft_up = ft.upper()
            if ft_up != su:
                return SnapResult(raw=raw, snapped=ft,
                                  rule="modelo.plan_designacao_to_ft",
                                  distance=float(abs(len(ft) - len(su))),
                                  applied=True,
                                  candidates=(ft,))
            # OCR is already canonical (== FT) — no-op confirm
            return SnapResult(raw=raw, snapped=raw,
                              rule="modelo.plan_substring",
                              distance=0.0, applied=False)
        # P1: trailing punct strip
        su_stripped = su.rstrip(".,;:- ")
        if su_stripped != su and any(su_stripped in d for d in designacoes):
            snapped = raw.rstrip(".,;:- ")
            return SnapResult(raw=raw, snapped=snapped, rule="modelo.strip_trailing_punct",
                              distance=0.0, applied=True)
        # P2: Lev-1 against first-tokens of designacoes
        candidates: list[str] = []
        for d in designacoes:
            first_token = d.split(" - ", 1)[0].strip()
            if not first_token:
                continue
            if len(first_token) == len(su):
                diffs = sum(1 for a, b in zip(first_token, su) if a != b)
                if diffs == 1:
                    candidates.append(first_token)
        unique = list(dict.fromkeys(candidates))  # dedupe preserving order
        if len(unique) == 1:
            return SnapResult(raw=raw, snapped=unique[0],
                              rule="modelo.plan_designacao_lev1",
                              distance=1.0, applied=True,
                              candidates=tuple(unique))
        if len(unique) > 1:
            # Multiple Lev-1 candidates within the same OF — too risky to pick
            return SnapResult(raw=raw, snapped=raw, rule="modelo.plan_lev1_ambiguous",
                              distance=1.0, applied=False, candidates=tuple(unique[:5]))

        # P3 (Round 27d): longest-prefix Lev-≤1 against first-tokens.
        # Catches:
        #   - OCR has trailing suffix not in plan (e.g. ``CA06F18D N1`` →
        #     ``CAO6F18D``: mu's first-8 chars are 1-char off from ft)
        #   - plan token is longer than OCR (e.g. ``CFC5F05RiV`` ↔
        #     ``CFC5F05RI_V``: ft's first-10 chars are 1-char off from mu)
        # Snaps to ft (the canonical plan token), discarding OCR's suffix.
        # Conservative: minimum prefix length 5, exactly 1 candidate at min dist.
        prefix_candidates: list[tuple[str, int]] = []
        for d in designacoes:
            ft = d.split(" - ", 1)[0].strip()
            if len(ft) < 5:
                continue
            L = min(len(su), len(ft))
            if L < 5:
                continue
            diffs = sum(1 for a, b in zip(su[:L], ft[:L]) if a != b)
            if diffs <= 1:
                prefix_candidates.append((ft, diffs))
        if prefix_candidates:
            # Sort by distance asc, then by length desc (longer ft = more specific)
            prefix_candidates.sort(key=lambda x: (x[1], -len(x[0])))
            best_ft, best_d = prefix_candidates[0]
            ties = [ft for ft, dd in prefix_candidates if dd == best_d and ft != best_ft]
            if not ties:
                return SnapResult(raw=raw, snapped=best_ft,
                                  rule=f"modelo.plan_prefix_lev{best_d}",
                                  distance=float(best_d) + 0.5,
                                  applied=True,
                                  candidates=tuple(ft for ft, _ in prefix_candidates[:5]))
            # Multiple ties — risky to pick; bail
            return SnapResult(raw=raw, snapped=raw, rule="modelo.plan_prefix_ambiguous",
                              distance=float(best_d) + 0.5, applied=False,
                              candidates=tuple([best_ft] + ties[:4]))

        # P4 (Round 28): longest-common-substring (LCS) vs full normalized
        # plan.designacao. Catches operator-notation differences where OCR
        # shares a contiguous alphanumeric run with the canonical model
        # somewhere in the designacao. Examples:
        #   "OMEGA60-6M" → "OMEGA606MABI" (shared "OMEGA606M", 9 chars)
        #   "0641-S-515" → plan "36044610 - 0641S515 + ..." → snap to "0641S515"
        # Strategy: normalize alphanum, find LCS ≥ 6 chars, snap to the
        # plan token containing it (first-token if LCS sits in head, else
        # the substring itself as the canonical key).
        # Conservative: min substring length 6, single dominant candidate.
        mu_norm = "".join(ch for ch in su if ch.isalnum())
        if len(mu_norm) >= 6:
            lcs_candidates: list[tuple[str, int, str]] = []  # (snap_target, lcs_len, source_designacao)
            for d in designacoes:
                d_norm = "".join(ch for ch in d if ch.isalnum())
                if not d_norm:
                    continue
                # Compute longest common contiguous substring (DP)
                a, b = mu_norm, d_norm
                la, lb = len(a), len(b)
                best_len = 0
                best_a_end = 0
                dp = [0] * (lb + 1)
                for i in range(1, la + 1):
                    prev = 0
                    for j in range(1, lb + 1):
                        cur = dp[j]
                        dp[j] = prev + 1 if a[i-1] == b[j-1] else 0
                        if dp[j] > best_len:
                            best_len = dp[j]
                            best_a_end = i
                        prev = cur
                if best_len >= 6:
                    lcs_substring = a[best_a_end - best_len:best_a_end]
                    # R60 — prefer first-token (canonical name) over LCS
                    # substring whenever FT is at least as long as the LCS
                    # and contains it (operator wrote partial form of FT).
                    # Resolves OMEGA606M → OMEGA606MABI: LCS=OMEGA606M is
                    # contained in FT=OMEGA606MABI; snap to FT.
                    # Fallback: LCS-in-designacao (was R28 behavior).
                    target = None
                    d_upper = d.upper()
                    ft = d.split(" - ", 1)[0].strip()
                    ft_norm = "".join(ch for ch in ft if ch.isalnum()).upper()
                    if ft_norm and lcs_substring in ft_norm and len(ft_norm) >= len(lcs_substring):
                        target = ft  # canonical first-token (R60)
                    elif lcs_substring in d_upper:
                        target = lcs_substring  # legacy: LCS as-is
                    else:
                            # Try shrinking from both ends to find a substring
                            # that's actually in raw designacao
                            for trim_front in range(0, max(0, best_len - 5)):
                                for trim_back in range(0, max(0, best_len - 5 - trim_front)):
                                    candidate = lcs_substring[trim_front:best_len - trim_back]
                                    if len(candidate) >= 6 and candidate in d_upper:
                                        target = candidate
                                        break
                                if target:
                                    break
                    if target:
                        lcs_candidates.append((target, best_len, d))
            if lcs_candidates:
                # Sort by lcs_len desc, then prefer first-token targets (longer)
                lcs_candidates.sort(key=lambda x: (-x[1], -len(x[0])))
                best_target, best_len, best_src = lcs_candidates[0]
                # Tie check: another candidate with same LCS but DIFFERENT target
                ties = [c for c, ln, _ in lcs_candidates
                        if ln == best_len and c != best_target]
                if not ties:
                    return SnapResult(raw=raw, snapped=best_target,
                                      rule=f"modelo.plan_lcs_{best_len}",
                                      distance=float(len(mu_norm) - best_len),
                                      applied=True,
                                      candidates=tuple(c for c, _, _ in lcs_candidates[:5]))
                return SnapResult(raw=raw, snapped=raw,
                                  rule="modelo.plan_lcs_ambiguous",
                                  distance=float(len(mu_norm) - best_len),
                                  applied=False,
                                  candidates=tuple([best_target] + ties[:4]))

        # P5 (Round 28b): OF-scoped Lev-2 against plan first-tokens with
        # STRICT uniqueness guards. Catches cases where the GT lexicon
        # contains the operator-written-incorrect form (so legacy Stage 0
        # would do a no-op `lexicon_exact`), but the plan canonical differs
        # by 2 chars. Examples:
        #   "CFC5F06R" → "CLC6F06R" (Lev-2: F↔L pos1, 5↔6 pos3)
        #   "CGCHF05D" → "CGC4F09D" (Lev-2: H↔4, 5↔9)
        #   "CECAF08D-V" → "CEC1F08D_V" (Lev-2: A↔1, -↔_)
        # Conservative: same length only, exactly 1 candidate at exactly
        # dist=2 (no ties in 0.5 window), min length 6. Plan-canonical
        # OVERRIDES GT lexicon — when plan and GT disagree, plan wins.
        # Round 43 Sol 8 — multi-prefix preserve guard. When this OF has
        # multiple designacoes with DIFFERENT 4-char prefixes (e.g. CR11..,
        # CLC8.., B713..), Lev-2 cannot disambiguate which family the
        # operator meant. Preserve OCR rather than fuzzy-match a wrong
        # family. Sheet 23 OF 260489 has CR11/B713/B713 prefixes — but
        # operator wrote CLC8F08Ri-V which is none of them.
        distinct_prefixes_4 = {
            d.split(" - ", 1)[0].strip()[:4].upper()
            for d in designacoes
            if len(d.split(" - ", 1)[0].strip()) >= 4
        }
        if len(distinct_prefixes_4) >= 2 and len(su) >= 4:
            su_prefix = su[:4]
            # R55 — skip the multi-prefix preserve guard when operator wrote
            # a multi-token modelo (3+ alphanumeric tokens, e.g.
            # "1045 V120 N:4 1045 V503"). The "first 4 chars" heuristic
            # assumes a contiguous single-token code; multi-token writing
            # is handled by Stage MT below.
            import re as _re_r55
            _op_tok_count = len(_re_r55.findall(r"[A-Z0-9]{4,}", su))
            if su_prefix not in distinct_prefixes_4 and _op_tok_count < 3:
                # OCR doesn't even share family with any plan FT — preserve
                return SnapResult(raw=raw, snapped=raw,
                                  rule="modelo.plan_multi_prefix_preserve",
                                  distance=0.0, applied=False,
                                  candidates=tuple(distinct_prefixes_4)[:5])

        if len(su) >= 6:
            lev2_candidates: list[tuple[str, float]] = []
            for d in designacoes:
                ft = d.split(" - ", 1)[0].strip()
                if len(ft) != len(su):  # same length only
                    continue
                if len(ft) < 6:
                    continue
                d_dist = _conf_weighted_dist(ft, su, max_dist=2.0)
                if 1.0 < d_dist <= 2.0:
                    lev2_candidates.append((ft, d_dist))
            if lev2_candidates:
                lev2_candidates.sort(key=lambda x: x[1])
                best_ft, best_d = lev2_candidates[0]
                # Strict tie check: any other candidate within 0.5 of min?
                close_ties = [ft for ft, d in lev2_candidates
                              if d <= best_d + 0.5 and ft != best_ft]
                if not close_ties:
                    return SnapResult(raw=raw, snapped=best_ft,
                                      rule=f"modelo.plan_lev2_{best_d:.1f}",
                                      distance=best_d, applied=True,
                                      candidates=tuple(ft for ft, _ in lev2_candidates[:5]))
                return SnapResult(raw=raw, snapped=raw,
                                  rule="modelo.plan_lev2_ambiguous",
                                  distance=best_d, applied=False,
                                  candidates=tuple([best_ft] + close_ties[:4]))

    # Stage P6 (R40): plan-canonical fallback. When all OF-scoped smart
    # stages (P0-P5) fail AND the row's OF has a single canonical
    # designacao first-token, snap to it. Plan is authoritative — even
    # if OCR happens to match a legacy lexicon entry (Stage 0 below),
    # the plan's first-token is the correct modelo for THIS OF.
    # Example: "CB04E63D" matches lexicon (legacy GT) but plan for
    # OF 257093 says "CBCBE06DI" → we snap to plan canonical.
    if designacoes:
        first_tokens = []
        for d in designacoes:
            ft = d.split(" - ", 1)[0].strip()
            if len(ft) >= 4:
                first_tokens.append(ft)
        unique_fts = list(dict.fromkeys(first_tokens))
        if len(unique_fts) == 1:
            target = unique_fts[0]
            if target.upper() != su:
                return SnapResult(
                    raw=raw, snapped=target,
                    rule="modelo.plan_canonical",
                    distance=99.0, applied=True,
                    candidates=(target,),
                )

    # Stage P7 (R41): OV-unanimous fallback. When OF is unresolved but
    # OV is in plan, check if ALL OFs sharing that OV have the same
    # designacao first-token → snap modelo to it. Defensive against
    # operator-shorthand modelos we haven't seen before.
    if row_context:
        ov_ctx = str(row_context.get("ov", "")).strip()
        if ov_ctx and ov_ctx in _OV_TO_OFS_ALL:
            ov_ft_set: set[str] = set()
            for plan_of in _OV_TO_OFS_ALL[ov_ctx]:
                for e in _OF_TO_ENTRIES_FULL.get(plan_of, ()):
                    ft = (e.get("designacao") or "").split(" - ", 1)[0].strip()
                    if len(ft) >= 4:
                        ov_ft_set.add(ft.upper())
            if len(ov_ft_set) == 1:
                target = next(iter(ov_ft_set))
                if target != su:
                    return SnapResult(
                        raw=raw, snapped=target,
                        rule="modelo.ov_unanimous",
                        distance=99.0, applied=True,
                        candidates=(target,),
                    )

    # Stage MT (R55): multi-token operator modelo → plan first-token.
    # Catches operator compound modelos like "1045 V120 N:4 1045 V503"
    # that don't match any single plan first-token via Lev/LCS but DO
    # have coherent multi-token overlap with a plan designacao.
    #
    # Scoring (per plan designacao, per operator token ≥4 chars):
    #   +5 if EXACT substring of any plan token (anchor — strong signal)
    #   +2 if Lev-1 against any 4+ char SUBSTRING of a plan token
    #      (sliding window — handles 1045 vs 1015 inside 1015VT20)
    # Requires score >= 7 AND gap ≥ 2 vs runner-up.
    # Conservative — leaves ambiguous rows as raw (cross-check flags them).
    #
    # Why high substring weight: an exact substring like V503 ⊂ 1015V503
    # is a strong anchor uniquely identifying that plan entry, while
    # Lev-1 matches like V503 vs V506 are weak and present across many
    # designacoes (they all start with 1015). Without weighting the
    # anchor, ambiguous rows tie and the snap can't disambiguate.
    if designacoes:
        import re as _re_r55
        op_tokens = _re_r55.findall(r"[A-Z0-9]{4,}", su)
        if len(op_tokens) >= 3:
            scored: list[tuple[int, str, str]] = []
            for d in designacoes:
                plan_tokens = _re_r55.findall(r"[A-Z0-9]{4,}", d.upper())
                if not plan_tokens:
                    continue
                score = 0
                for ot in op_tokens:
                    ot_score = 0
                    for pt in plan_tokens:
                        # Direct substring — strong anchor
                        if ot in pt:
                            ot_score = max(ot_score, 5)
                            continue
                        # Sliding window: |ot|-char window of pt
                        if len(ot) <= len(pt):
                            for i in range(len(pt) - len(ot) + 1):
                                sub = pt[i:i+len(ot)]
                                diffs = sum(1 for a, b in zip(ot, sub) if a != b)
                                if diffs == 0:
                                    ot_score = max(ot_score, 5)
                                    break
                                if diffs == 1:
                                    ot_score = max(ot_score, 2)
                    score += ot_score
                scored.append((score, d.split(" - ", 1)[0].strip(), d))
            if scored:
                scored.sort(reverse=True)
                best_score, best_ft, best_des = scored[0]
                runner_score = scored[1][0] if len(scored) > 1 else 0
                if best_score >= 7 and (best_score - runner_score) >= 2:
                    return SnapResult(
                        raw=raw, snapped=best_ft,
                        rule="modelo.multi_token",
                        distance=float(best_score),
                        applied=True,
                        candidates=tuple(ft for _, ft, _ in scored[:5]),
                    )

    # Stage 0: full-string Levenshtein-1 against GT modelo lexicon.
    # Case-insensitive: lexicon has canonical case; we compare upper(),
    # then return the canonical form if found.
    full_candidates: list[tuple[str, float]] = []
    for canonical in _MODELOS_FULL:
        c_up = canonical.upper().strip()
        if c_up == su:
            # Already canonical — preserve casing of canonical entry.
            return SnapResult(raw=raw, snapped=canonical, rule="modelo.lexicon_exact",
                              distance=0.0, applied=raw != canonical)
        d = _conf_weighted_dist(c_up, su, max_dist=1.0)
        if d <= 1.0:
            full_candidates.append((canonical, d))
    if full_candidates:
        # Sort by distance, then by GT frequency (most-common first).
        # _MODELOS_FULL is already in descending count order, so
        # sort by distance only (stable sort preserves count order).
        full_candidates.sort(key=lambda x: x[1])
        best, best_d = full_candidates[0]
        # Bail out if there's a tie at the same distance — too risky.
        ties = [c for c, d in full_candidates if d == best_d]
        if len(ties) > 1:
            # Ambiguous — leave raw, document candidates for audit.
            return SnapResult(raw=raw, snapped=raw, rule="modelo.lexicon_ambiguous",
                              distance=best_d, applied=False,
                              candidates=tuple(ties[:5]))
        return SnapResult(raw=raw, snapped=best, rule=f"modelo.lexicon_dist_{best_d:.1f}",
                          distance=best_d, applied=True,
                          candidates=tuple(c for c, _ in full_candidates[:5]))

    # Stage 0b (Round 24c): Levenshtein-2 fallback with stricter
    # tie-breaking. Only snap if there's a UNIQUE candidate at distance
    # ≤ 2.0 AND length difference ≤ 1 (avoid snapping short strings
    # to long modelos). Recovers cases like 'CLCCFO9Di-V' (2 chars
    # off from 'CLCCF09Di-V'... wait no, GT had 'CLC6F04Di-V').
    # Actually: many remaining errors have 2+ chars wrong, so Lev-2
    # may help.
    far_candidates: list[tuple[str, float]] = []
    for canonical in _MODELOS_FULL:
        c_up = canonical.upper().strip()
        if abs(len(c_up) - len(su)) > 1:
            continue  # length differs too much, skip
        d = _conf_weighted_dist(c_up, su, max_dist=2.0)
        if 1.0 < d <= 2.0:
            far_candidates.append((canonical, d))
    if far_candidates:
        far_candidates.sort(key=lambda x: x[1])
        best, best_d = far_candidates[0]
        ties = [c for c, d in far_candidates if d == best_d]
        # STRICTER tie-breaking at Lev-2: require uniqueness, not just
        # at min distance but across ALL candidates within 0.5 of min.
        close_ties = [c for c, d in far_candidates if d <= best_d + 0.5]
        if len(close_ties) > 1:
            return SnapResult(raw=raw, snapped=raw, rule="modelo.lexicon_lev2_ambiguous",
                              distance=best_d, applied=False,
                              candidates=tuple(close_ties[:5]))
        return SnapResult(raw=raw, snapped=best, rule=f"modelo.lexicon_lev2_{best_d:.1f}",
                          distance=best_d, applied=True,
                          candidates=tuple(c for c, _ in far_candidates[:5]))

    for plen in (5, 4):
        if len(su) < plen:
            continue
        prefix = su[:plen]
        # Exact prefix match? keep value (preserve original casing in suffix).
        for known in _MODELO_PREFIXES:
            if known[:plen] == prefix:
                return SnapResult(raw=raw, snapped=raw, rule="modelo.prefix_exact",
                                  distance=0.0, applied=False)
        # 1-edit (confusion-weighted) match.
        best = None
        best_dist = 1.0  # max accepted
        candidates: list[str] = []
        for known in _MODELO_PREFIXES:
            if len(known) < plen:
                continue
            kp = known[:plen]
            d = _conf_weighted_dist(kp, prefix, max_dist=1.0)
            if d <= 1.0:
                candidates.append(known)
            if d < best_dist:
                best_dist = d
                best = kp
        if best is not None and best != prefix:
            snapped = best + raw[plen:]  # preserve suffix casing
            return SnapResult(raw=raw, snapped=snapped, rule=f"modelo.prefix_dist_{best_dist:.1f}",
                              distance=best_dist, applied=True,
                              candidates=tuple(c[:plen] for c in candidates[:5]))
    return SnapResult(raw=raw, snapped=raw, rule="modelo.no_prefix_match",
                      distance=float("inf"), applied=False)


def _lev1_unique(target: str, pool) -> str | None:
    """Return the single Lev-1 candidate from ``pool`` if exactly one
    exists, else None. Cheap (linear scan with length pre-filter).
    """
    matches: list[str] = []
    tlen = len(target)
    for c in pool:
        clen = len(c)
        if abs(clen - tlen) > 1:
            continue
        if clen == tlen:
            diffs = sum(1 for a, b in zip(c, target) if a != b)
            if diffs == 1:
                matches.append(c)
                if len(matches) > 1:
                    return None
        elif clen == tlen - 1:
            for i in range(tlen):
                if target[:i] + target[i+1:] == c:
                    matches.append(c)
                    if len(matches) > 1:
                        return None
                    break
        elif clen == tlen + 1:
            for i in range(clen):
                if c[:i] + c[i+1:] == target:
                    matches.append(c)
                    if len(matches) > 1:
                        return None
                    break
    return matches[0] if len(matches) == 1 else None


def _lev1_candidates(target: str, pool) -> list[str]:
    """Return all Lev-1 candidates from ``pool``. Used when disambiguation
    by row context (e.g. OV) is needed.
    """
    out: list[str] = []
    tlen = len(target)
    for c in pool:
        clen = len(c)
        if abs(clen - tlen) > 1:
            continue
        if clen == tlen:
            if sum(1 for a, b in zip(c, target) if a != b) <= 1:
                out.append(c)
        elif clen == tlen - 1:
            for i in range(tlen):
                if target[:i] + target[i+1:] == c:
                    out.append(c)
                    break
        elif clen == tlen + 1:
            for i in range(clen):
                if c[:i] + c[i+1:] == target:
                    out.append(c)
                    break
    return out


def snap_lote(value: str, *, row_context: dict | None = None) -> SnapResult:
    """Lote = `[HM]\\d{2}B\\d{4,5}` (heat-treatment batch identifier).

    Stages (Round 27 adds 0a/0b against real StockSAP refs):

    Stage 0a: H→M canonical prefix when rest of format matches
        (empirical: ~52 H-prefix lotes from Qwen, all should be M).

    Stage 0b: SAP lookup. If lote in StockSAP exactly → confirm; if not,
        Lev-1 search against 750 SAP lotes — snap only when a SINGLE
        candidate matches (avoid masking real OCR errors when ambiguous).

    No-snap fallback for unknown lotes (stock may have rotated; preserve
    original so it's flagged in validation rather than silently changed).

    The ``row_context`` arg is reserved for future cross-field rules
    (currently unused by lote — kept for API uniformity with snap_of).
    """
    import re
    raw = value or ""
    sap_full = _REFS.get("lotes_sap_full", {})

    # Stage 0a: H→M (existing rule, applies before SAP lookup)
    pre_snapped = raw
    h_applied = False
    if re.match(r"^H\d{2}B\d{4,5}$", raw):
        pre_snapped = "M" + raw[1:]
        h_applied = True

    raw_up = pre_snapped.upper().strip()

    # Stage 0b: SAP exact lookup
    if raw_up in _LOTES_SAP:
        return SnapResult(
            raw=raw, snapped=pre_snapped,
            rule="lote.h_to_m_consensus" if h_applied else "lote.sap_exact",
            distance=1.0 if h_applied else 0.0,
            applied=(pre_snapped != raw),
        )

    # Stage 0c (R41b): Lev-1 search in SAP WITH cross-field disambig.
    # R27 disabled this because Lev-1 alone gives false positives when
    # operator wrote canonical lote correctly but it rotated out of SAP.
    # Now we re-enable it with a SAFETY GUARD: candidate must match the
    # row's ESP (±0.05) AND larg (±50mm). This ensures we only snap to
    # lotes whose physical specs match what the operator measured.
    if row_context and len(raw_up) >= 7:
        def _to_n(v):
            if v is None or v == "": return None
            try: return float(str(v).replace(",", ".").strip())
            except: return None
        ocr_esp = _to_n(row_context.get("esp"))
        ocr_larg = _to_n(row_context.get("larg_mm"))
        # Cross-field disambig requires at least one of esp/larg known
        if ocr_esp is not None or ocr_larg is not None:
            cands: list[str] = []
            for k, v in sap_full.items():
                if len(k) != len(raw_up):
                    continue
                if sum(1 for a, b in zip(k, raw_up) if a != b) != 1:
                    continue
                k_esp = _to_n(v.get("esp"))
                k_larg = _to_n(v.get("larg"))
                esp_ok = (ocr_esp is None) or (
                    k_esp is not None and abs(k_esp - ocr_esp) <= 0.05
                )
                larg_ok = (ocr_larg is None) or (
                    k_larg is not None and abs(k_larg - ocr_larg) <= 50
                )
                if esp_ok and larg_ok:
                    cands.append(k)
            if len(cands) == 1:
                return SnapResult(
                    raw=raw, snapped=cands[0],
                    rule="lote.sap_lev1_disambig",
                    distance=1.0 + (1.0 if h_applied else 0.0),
                    applied=True,
                )

    # H→M still applied even when SAP doesn't know the lote (stock rotation).
    if h_applied:
        return SnapResult(
            raw=raw, snapped=pre_snapped, rule="lote.h_to_m_consensus",
            distance=1.0, applied=True,
        )
    return SnapResult(raw=raw, snapped=raw, rule="lote.no_snap", distance=0.0, applied=False)


def snap_of(value: str, *, row_context: dict | None = None) -> SnapResult:
    """OF (Ordem de Fabrico) — 6-digit production order id.

    Stages (Round 27):

    Stage 0a: terminal A→1 (existing — ~6 sheets/19 had this).

    Stage 0b: plan exact lookup (9657 plan OFs). Confirms OFs that
        appear in the production plan. No transformation when found.

    Stage 0c: plan Lev-1 with OV disambiguation. Common OCR misread
        is `1↔4` (e.g. `264124` → real `261124`). When 1 Lev-1
        candidate exists OR multiple candidates exist but only one has
        the row's OV in its plan entries, snap.

    No-snap when ambiguous or no candidates (real production OFs that
    aren't yet in the plan get flagged downstream rather than mangled).
    """
    import re
    raw = value or ""
    _lev1_audit_candidates: list[str] | None = None  # for final fallback audit

    # R40b: pre-snap OV in row_context. When OCR.ov is wrong (Lev-1
    # against any plan OV) but operator wrote intent close to plan,
    # we resolve OV before scoring so multifield bonuses fire correctly.
    # Updates a LOCAL copy of row_context — doesn't mutate caller's dict.
    if row_context:
        row_context = dict(row_context)
        ov_orig = str(row_context.get("ov", "") or "").strip()
        if ov_orig and ov_orig not in _OV_TO_OFS_ALL:
            # First try cleaned digit-only version (handles `2.601423` →
            # `2601423` typed in plan).
            ov_clean = re.sub(r"\D", "", ov_orig) if ov_orig else ""
            if ov_clean and ov_clean != ov_orig and ov_clean in _OV_TO_OFS_ALL:
                row_context["ov"] = ov_clean
            else:
                # Try Lev-1 same-length search across all plan OVs
                target = ov_clean if ov_clean else ov_orig
                cands = [pov for pov in _OV_TO_OFS_ALL.keys()
                         if len(pov) == len(target) and
                            sum(1 for a, b in zip(pov, target) if a != b) == 1]
                # Try indel (length diff 1)
                if not cands:
                    for pov in _OV_TO_OFS_ALL.keys():
                        if abs(len(pov) - len(target)) != 1:
                            continue
                        short, long_ = (target, pov) if len(target) < len(pov) else (pov, target)
                        for i in range(len(long_)):
                            if short == long_[:i] + long_[i+1:]:
                                cands.append(pov)
                                break
                if len(cands) == 1:
                    row_context["ov"] = cands[0]  # use snapped OV for downstream

    # ════════════════════════════════════════════════════════════════════
    # Round 51 — Holistic best-match (campos com mesmo peso).
    #
    # User principle: "os campos têm o mesmo peso, encontrar o melhor
    # match em TODOS os campos". Single pipeline replaces all early-
    # return cascades (of.from_ov, of.plan_lev1_unique, etc.).
    #
    # Pipeline:
    #   1. Pre-clean OCR (terminal-A, format-clean digits)
    #   2. Build candidate pool (union of: OCR.of, OV→OF, OV cluster,
    #      Lev-1 of OCR.of, top-K cliente-scoped)
    #   3. Score each candidate via _score_of_holistic (0-9)
    #   4. Pick winner; OCR.of preserved on tie at top
    # ════════════════════════════════════════════════════════════════════

    # 1. Pre-clean OCR
    pre_snapped = raw
    a_applied = False
    if re.match(r"^\d{5}A$", raw):
        pre_snapped = raw[:-1] + "1"
        a_applied = True
    raw_up = pre_snapped.strip()
    cleaned = re.sub(r"\D", "", raw) if re.search(r"[^\d]", raw) else None

    # 2. Build candidate pool
    candidates: set[str] = set()
    # 2a. OCR.of (raw + cleaned variants)
    if raw_up in _OFS_PLAN_STR:
        candidates.add(raw_up)
    if cleaned and cleaned in _OFS_PLAN_STR:
        candidates.add(cleaned)
    if row_context:
        # 2b. OFs sharing operator's OV
        ov_ctx = str(row_context.get("ov", "") or "").strip()
        if ov_ctx:
            candidates.update(_ofs_for_ov(ov_ctx))
            # OV→OF reverse (when OV maps to single OF in plan)
            if ov_ctx in _OV_TO_OF:
                candidates.add(_OV_TO_OF[ov_ctx])
        # 2c. Lev-1 candidates of OCR.of
        if re.match(r"^\d{5,7}$", raw_up):
            candidates.update(_lev1_candidates(raw_up, _OFS_PLAN_STR))
        if cleaned and re.match(r"^\d{5,7}$", cleaned):
            candidates.update(_lev1_candidates(cleaned, _OFS_PLAN_STR))
        # 2d. Cliente-scoped top-K (filter score ≥ 4 to bound search)
        cli_raw = str(row_context.get("cliente", "") or "").strip().upper()
        cli = _CLIENTE_ALIASES.get(cli_raw, cli_raw)
        if cli:
            cli_scored: list[tuple[int, str]] = []
            for cof, clis in _OF_TO_CLIENTES.items():
                if cli in clis:
                    s = _score_of_holistic(cof, row_context)
                    if s >= 4:
                        cli_scored.append((s, cof))
            cli_scored.sort(reverse=True)
            candidates.update(c for _, c in cli_scored[:20])

        # 2e (R53): modelo-token index — find OFs whose plan designacao
        # contains any token from operator's modelo. Catches cases where
        # OF/OV/cliente are ALL OCR garbage but operator wrote a valid
        # modelo. Skips tokens that appear in too many OFs (>50) — those
        # are too common to discriminate.
        op_mod = str(row_context.get("modelo", "") or "").upper().strip()
        if op_mod:
            for tok in re.findall(r"[A-Z0-9]{5,}", op_mod):
                hits = _MODELO_TOKEN_TO_OFS.get(tok, frozenset())
                if 0 < len(hits) <= 50:
                    candidates.update(hits)

        # 2f (R53): geometric index — find OFs with lbase/ltopo close to
        # operator's. Only runs if pool still small (<10) to avoid bloat
        # in common-dim cases (e.g. lbase~400mm is very frequent).
        if len(candidates) < 10:
            op_lb = _num_or_none(row_context.get("lbase"))
            op_lt = _num_or_none(row_context.get("ltopo"))
            if op_lb is not None and op_lt is not None:
                base_b = int(op_lb) // 30
                topo_b = int(op_lt) // 30
                geom_hits: set[str] = set()
                for db in (-1, 0, 1):
                    for dt in (-1, 0, 1):
                        geom_hits.update(
                            _GEOMETRY_INDEX.get((base_b + db, topo_b + dt), frozenset())
                        )
                # Geom can return 100s of OFs; pre-filter by score
                if len(geom_hits) > 30:
                    geom_scored = sorted(
                        ((_score_of_holistic(o, row_context), o) for o in geom_hits),
                        reverse=True,
                    )
                    candidates.update(
                        o for s, o in geom_scored[:30] if s >= 4
                    )
                else:
                    candidates.update(geom_hits)

    # 3. If pool empty, preserve OCR (no signal to act on)
    if not candidates:
        if a_applied:
            return SnapResult(
                raw=raw, snapped=pre_snapped, rule="of.terminal_A_to_1",
                distance=1.0, applied=True,
            )
        return SnapResult(
            raw=raw, snapped=raw, rule="of.no_candidates",
            distance=0.0, applied=False,
        )

    # 4. Score every candidate
    if not row_context:
        # Without row context, fall back to: prefer OCR.of if in pool,
        # else any candidate (lev-1 single).
        if raw_up in candidates:
            return SnapResult(
                raw=raw, snapped=raw_up,
                rule="of.terminal_A_to_1" if a_applied else "of.plan_exact",
                distance=1.0 if a_applied else 0.0,
                applied=(pre_snapped != raw),
            )
        if len(candidates) == 1:
            cand = next(iter(candidates))
            return SnapResult(
                raw=raw, snapped=cand, rule="of.no_context_lev1",
                distance=1.0, applied=True,
            )
        return SnapResult(
            raw=raw, snapped=raw, rule="of.no_context_ambiguous",
            distance=float("inf"), applied=False,
            candidates=tuple(sorted(candidates)[:8]),
        )

    # Tie-breakers when multiple candidates share top score:
    #   1. Primary score DESC
    #   2. OF Lev distance to OCR.of ASC (closer wins — operator's
    #      digits matter when products are equal)
    #   3. OF lexicographic ASC (deterministic last resort)
    def _of_distance_to_ocr(of_str: str) -> int:
        target = cleaned if cleaned else raw_up
        if not target:
            return 99
        if len(of_str) == len(target):
            return sum(1 for a, b in zip(of_str, target) if a != b)
        return abs(len(of_str) - len(target)) + 5

    scored = sorted(
        ((_score_of_holistic(c, row_context), c) for c in candidates),
        key=lambda x: (-x[0], _of_distance_to_ocr(x[1]), x[1]),
    )
    winner_score, winner_of = scored[0]
    cur_score = (
        _score_of_holistic(raw_up, row_context)
        if raw_up in _OFS_PLAN_STR else 0
    )

    # 5. Decision
    # OCR.of in top-tied set → normally preserve operator's input
    # (operator is valid when alternatives are equally explained).
    # R57: when tied, break ties by HIGH-SIGNAL score (cliente exact +
    # ov exact + modelo substring). A challenger with strictly more
    # high-signal matches than OCR.of wins. These 3 fields uniquely
    # identify a plan row better than dim tolerances.
    top_set = {c for s, c in scored if s == winner_score}
    if raw_up in top_set:
        # R57 tie-break — compute high-signal for OCR.of and challengers
        from app.cross_check.holistic_score import score_high_signal
        cur_hi = max(
            (score_high_signal(e, row_context, _CLIENTE_ALIASES)
             for e in _OF_TO_ENTRIES_FULL.get(raw_up, ())),
            default=0,
        )
        challengers = top_set - {raw_up}
        best_challenger = None
        best_challenger_hi = -1
        for c in challengers:
            c_hi = max(
                (score_high_signal(e, row_context, _CLIENTE_ALIASES)
                 for e in _OF_TO_ENTRIES_FULL.get(c, ())),
                default=0,
            )
            if c_hi > best_challenger_hi:
                best_challenger_hi = c_hi
                best_challenger = c
        if best_challenger is not None and best_challenger_hi > cur_hi:
            return SnapResult(
                raw=raw, snapped=best_challenger,
                rule=f"of.holistic_tiebreak_signal_{best_challenger_hi}vs{cur_hi}",
                distance=99.0, applied=True,
                candidates=tuple(c for _, c in scored[:5]),
            )
        return SnapResult(
            raw=raw, snapped=pre_snapped,
            rule=(
                "of.terminal_A_to_1" if a_applied
                else f"of.holistic_top_{winner_score}"
            ),
            distance=1.0 if a_applied else 0.0,
            applied=(pre_snapped != raw),
            candidates=tuple(c for _, c in scored[:5]),
        )
    # Otherwise, override only if winner strictly beats current
    if winner_score > cur_score:
        return SnapResult(
            raw=raw, snapped=winner_of,
            rule=f"of.holistic_best_{winner_score}vs{cur_score}",
            distance=99.0, applied=True,
            candidates=tuple(c for _, c in scored[:5]),
        )
    # Equal score, OCR.of NOT in pool (e.g. not in plan): pick winner
    if cur_score == 0 and winner_score > 0:
        return SnapResult(
            raw=raw, snapped=winner_of,
            rule=f"of.holistic_best_outsider_{winner_score}",
            distance=99.0, applied=True,
            candidates=tuple(c for _, c in scored[:5]),
        )

    # Truly no signal: preserve OCR with audit candidates
    return SnapResult(
        raw=raw, snapped=raw if not a_applied else pre_snapped,
        rule="of.holistic_no_winner",
        distance=float("inf"), applied=a_applied,
        candidates=tuple(c for _, c in scored[:8]),
    )


def snap_ov(value: str, *, row_context: dict | None = None) -> SnapResult:
    """OV (Ordem de Venda) — sales order id (typically 6-7 digits).

    Stages (Round 27c):

    Stage 0a: plan exact lookup. If OCR.ov matches any of the OVs in
        plan.entries[row.of], it's already correct → no-op confirm.

    Stage 0b: OF-scoped Lev-1 against plan OVs. When OCR misread a digit
        (e.g. `1↔4`, `1↔9` in handwriting), find unique Lev-1 candidate
        among the plan OVs FOR THE ROW'S OF. Empirically 7/8 snaps in
        Round 27 testing matched GT exactly — high precision.

    Falls back to no-snap when ambiguous, no candidates, or OF not in plan.
    """
    raw = value or ""
    if not raw or not row_context:
        return SnapResult(raw=raw, snapped=raw, rule="ov.no_snap", distance=0.0, applied=False)

    of = str(row_context.get("of", "")).strip()
    if not of:
        return SnapResult(raw=raw, snapped=raw, rule="ov.no_snap", distance=0.0, applied=False)

    plan_ovs = _OF_TO_OVS.get(of, frozenset())
    if not plan_ovs:
        return SnapResult(raw=raw, snapped=raw, rule="ov.no_snap", distance=0.0, applied=False)

    raw_strip = raw.strip()
    if raw_strip in plan_ovs:
        return SnapResult(raw=raw, snapped=raw, rule="ov.plan_exact",
                          distance=0.0, applied=False)

    # Stage 0c (R37): unique-OV-per-OF. When plan has exactly 1 OV for
    # this OF and operator wrote a different one, snap regardless of
    # Lev distance — only one valid answer exists. 16 fixes in DB.
    if len(plan_ovs) == 1:
        only_ov = next(iter(plan_ovs))
        # Compute char-level diff for audit; not used for safety check.
        diff = sum(1 for a, b in zip(raw_strip, only_ov) if a != b)
        diff += abs(len(raw_strip) - len(only_ov))
        return SnapResult(raw=raw, snapped=only_ov,
                          rule="ov.plan_unique",
                          distance=float(diff), applied=True)

    unique = _lev1_unique(raw_strip, plan_ovs)
    if unique:
        return SnapResult(raw=raw, snapped=unique, rule="ov.plan_lev1_unique",
                          distance=1.0, applied=True)

    candidates = _lev1_candidates(raw_strip, plan_ovs)
    if len(candidates) > 1:
        return SnapResult(raw=raw, snapped=raw, rule="ov.plan_lev1_ambiguous",
                          distance=1.0, applied=False, candidates=tuple(candidates[:8]))

    return SnapResult(raw=raw, snapped=raw, rule="ov.no_snap", distance=0.0, applied=False)


def normalize_esp(value: str, *, row_context: dict | None = None) -> SnapResult:
    """ESP normalisation + decimal recovery.

    Round 21: `esp` decimal separator `.` → `,` (PT canonical).
    Round 37: decimal recovery — operator's `2,6` sometimes loses comma
    in OCR, becoming `26`. If the row's lote is in SAP and SAP[lote].esp
    equals `int(value)/10`, restore the decimal (e.g. `26` → `2,6`).
    32 fixes in DB analysis.
    """
    import re
    raw = value or ""
    if not raw:
        return SnapResult(raw=raw, snapped=raw, rule="esp.empty",
                          distance=0.0, applied=False)

    # Round 21: `.` → `,`
    if re.match(r"^\d+\.\d+$", raw):
        snapped = raw.replace(".", ",")
        return SnapResult(raw=raw, snapped=snapped, rule="esp.dot_to_comma",
                          distance=0.0, applied=True)

    # Round 37: decimal recovery via SAP lookup. Operator wrote `26`
    # when SAP says lote.esp = 2.6. Auto-insert decimal as `2,6`.
    # Falls back to plan[OF].esp when lote not in SAP (catches lotes
    # that aren't yet in StockSAP but plan knows the spec).
    if raw.isdigit() and int(raw) >= 10 and row_context:
        ocr_div10 = float(raw) / 10.0
        # Stage 0a: SAP lote → esp
        lote = str(row_context.get("lote", "") or "").strip().upper()
        if lote:
            sap_full = _REFS.get("lotes_sap_full", {})
            sap_entry = sap_full.get(lote)
            if sap_entry and sap_entry.get("esp") is not None:
                try:
                    sap_esp = float(sap_entry["esp"])
                    if abs(ocr_div10 - sap_esp) <= 0.05:
                        canonical = str(sap_esp).replace(".", ",")
                        return SnapResult(
                            raw=raw, snapped=canonical,
                            rule="esp.decimal_recovered_sap",
                            distance=1.0, applied=True,
                        )
                except (TypeError, ValueError):
                    pass
        # Stage 0b: plan OF → esp (only if SAP didn't fire, lote may be
        # outside SAP stock but plan still knows the spec).
        of = str(row_context.get("of", "") or "").strip()
        if of:
            of_entries = _REFS.get("of_to_entries", {}).get(of, ())
            if of_entries:
                # Plan can have multiple entries per OF; pick a single esp
                # if all entries agree (avoids ambiguity).
                plan_esps = {
                    float(e["esp"]) for e in of_entries
                    if e.get("esp") is not None
                }
                if len(plan_esps) == 1:
                    plan_esp = next(iter(plan_esps))
                    if abs(ocr_div10 - plan_esp) <= 0.05:
                        canonical = str(plan_esp).replace(".", ",")
                        return SnapResult(
                            raw=raw, snapped=canonical,
                            rule="esp.decimal_recovered_plan",
                            distance=1.0, applied=True,
                        )

    # Round 43 Sol 5 — "consensus override". When OCR is an integer ≥10
    # (almost certainly wrong since esp is single-digit float in mm) AND
    # both SAP[lote].esp and plan[OF].esp AGREE on a small value, force
    # snap to that consensus value. Catches cases like operator wrote
    # `4,8` but OCR read `26` — div10 recovery (2.6) doesn't match, but
    # if both SAP+plan say 4.8, that's the truth.
    if raw.isdigit() and int(raw) >= 10 and row_context:
        of = str(row_context.get("of", "") or "").strip()
        lote = str(row_context.get("lote", "") or "").strip().upper()
        sap_esp_n = None
        plan_esp_n = None
        if lote:
            sap_full = _REFS.get("lotes_sap_full", {})
            sap_entry = sap_full.get(lote)
            if sap_entry and sap_entry.get("esp") is not None:
                try:
                    sap_esp_n = float(sap_entry["esp"])
                except (TypeError, ValueError):
                    pass
        if of:
            of_entries = _REFS.get("of_to_entries", {}).get(of, ())
            plan_esps = set()
            for e in of_entries:
                if e.get("esp") is not None:
                    try:
                        plan_esps.add(float(e["esp"]))
                    except (TypeError, ValueError):
                        pass
            if len(plan_esps) == 1:
                plan_esp_n = next(iter(plan_esps))
        # Both refs present AND agree → force snap
        if (sap_esp_n is not None and plan_esp_n is not None
                and abs(sap_esp_n - plan_esp_n) <= 0.05):
            canonical = str(sap_esp_n).replace(".", ",")
            return SnapResult(raw=raw, snapped=canonical,
                              rule="esp.consensus_override",
                              distance=99.0, applied=True)

    return SnapResult(raw=raw, snapped=raw, rule="esp.no_change",
                      distance=0.0, applied=False)


def normalize_data(value: str) -> SnapResult:
    """Round 21 pattern fix: `header.data` strip zero-pad.

    Empirical: GT sometimes has unpadded month/day (e.g. `09-4-2026`),
    Qwen outputs padded (`09-04-2026`). 3 mismatches in 5. Both forms
    are valid PT date notation. Canonical chosen = padded form (more
    standard, easier to sort lexicographically), so we LEAVE prod
    output as-is. The fix is metric-side (see _normalise_field) not
    snap-side. Snap preserves the model's output.
    """
    raw = value or ""
    return SnapResult(raw=raw, snapped=raw, rule="data.no_snap",
                      distance=0.0, applied=False)


def normalize_horas(value: str) -> SnapResult:
    """Format-only: ``8,00`` -> ``8:00``, strip trailing ``h``/``H``."""
    raw = value or ""
    if not raw:
        return SnapResult(raw=raw, snapped=raw, rule="horas.empty", distance=0.0, applied=False)
    s = raw.strip().rstrip("Hh").strip()
    if "," in s and ":" not in s:
        s = s.replace(",", ":")
    return SnapResult(raw=raw, snapped=s, rule="horas.format_normalize",
                      distance=0.0, applied=raw != s)


def snap_dim(value: str, field: str, *, row_context: dict | None = None) -> SnapResult:
    """Round 47 — numeric Lev-1 snap for dim fields (larg_mm, lbase, ltopo).

    When OCR dim is 1 char different from canonical (plan or SAP), snap.
    - larg_mm → SAP[lote].larg (plan_colunas doesn't have larg/bobine width)
    - lbase, ltopo → plan[OF].lbase / .ltopo

    Conservative guards:
    - Numeric diff ≤ 30mm (catches OCR digit confusion, not real variance)
    - Char-level Lev distance == 1 only (no Lev-2)
    - Same length only (no insertion/deletion confusion)

    comp_mm excluded — production length variance is real (4800 vs 5800
    is different cut, not OCR error).
    """
    raw = value or ""
    if not raw or not row_context:
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.no_context",
                          distance=0.0, applied=False)

    canonical_v: int | None = None
    if field == "larg_mm":
        # SAP-based: lookup lote.larg
        lote = str(row_context.get("lote", "") or "").strip().upper()
        if not lote:
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.no_lote",
                              distance=0.0, applied=False)
        sap_entry = _REFS.get("lotes_sap_full", {}).get(lote)
        if not sap_entry or sap_entry.get("larg") is None:
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.no_sap",
                              distance=0.0, applied=False)
        try:
            canonical_v = int(float(sap_entry["larg"]))
        except (ValueError, TypeError):
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.sap_invalid",
                              distance=0.0, applied=False)
    else:
        # Plan-based (lbase, ltopo)
        of = str(row_context.get("of", "") or "").strip()
        if not of or of not in _OFS_PLAN_STR:
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.no_plan",
                              distance=0.0, applied=False)
        entries = _OF_TO_ENTRIES_FULL.get(of, ())
        if not entries:
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.no_entries",
                              distance=0.0, applied=False)
        plan_vals = set()
        for e in entries:
            v = e.get(field)
            if v is not None:
                try:
                    plan_vals.add(int(float(v)))
                except (ValueError, TypeError):
                    pass
        if len(plan_vals) != 1:
            return SnapResult(raw=raw, snapped=raw, rule=f"{field}.plan_ambiguous",
                              distance=0.0, applied=False)
        canonical_v = next(iter(plan_vals))

    try:
        ocr_n = int(float(str(raw).replace(",", ".")))
    except (ValueError, TypeError):
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.non_numeric",
                          distance=0.0, applied=False)
    if ocr_n == canonical_v:
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.exact",
                          distance=0.0, applied=False)
    diff_mm = abs(ocr_n - canonical_v)
    if diff_mm > 30:
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.diff_too_large",
                          distance=float(diff_mm), applied=False)
    ocr_str = str(ocr_n)
    canonical_str = str(canonical_v)
    if len(ocr_str) != len(canonical_str):
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.length_diff",
                          distance=0.0, applied=False)
    char_diffs = sum(1 for a, b in zip(ocr_str, canonical_str) if a != b)
    if char_diffs > 1:
        return SnapResult(raw=raw, snapped=raw, rule=f"{field}.lev_too_far",
                          distance=float(char_diffs), applied=False)
    src = "sap" if field == "larg_mm" else "plan"
    return SnapResult(raw=raw, snapped=canonical_str,
                      rule=f"{field}.{src}_lev1",
                      distance=1.0, applied=True)


def snap_larg(value: str, *, row_context: dict | None = None) -> SnapResult:
    return snap_dim(value, "larg_mm", row_context=row_context)


def snap_lbase(value: str, *, row_context: dict | None = None) -> SnapResult:
    return snap_dim(value, "lbase", row_context=row_context)


def snap_ltopo(value: str, *, row_context: dict | None = None) -> SnapResult:
    return snap_dim(value, "ltopo", row_context=row_context)


# Dispatcher used by pipeline.py
_DISPATCH = {
    "cliente": snap_cliente,
    "modelo": snap_modelo,
    "lote": snap_lote,
    "of": snap_of,
    "ov": snap_ov,
    "esp": normalize_esp,
    "larg_mm": snap_larg,
    "lbase": snap_lbase,
    "ltopo": snap_ltopo,
    "data": normalize_data,
    "horas_trabalhadas": normalize_horas,
}


def snap_field(
    field: str, value: str, *, row_context: dict | None = None
) -> SnapResult | None:
    """Run L2 snap for a single field. Returns None if no snap policy.

    ``row_context`` lets cross-field snaps (currently only ``of``) use
    other cells in the same row for disambiguation. Most snaps ignore it.
    """
    fn = _DISPATCH.get(field)
    if fn is None:
        return None
    # Round 37: cliente + esp now also accept row_context for OF→cliente
    # auto-snap and SAP-based decimal recovery, respectively.
    # Round 47: dim fields (larg_mm/lbase/ltopo) accept row_context for
    # plan-canonical Lev-1 numeric snap.
    if field in ("of", "lote", "modelo", "ov", "cliente", "esp",
                 "larg_mm", "lbase", "ltopo"):
        return fn(value, row_context=row_context)
    return fn(value)
