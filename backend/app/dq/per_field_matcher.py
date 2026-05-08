"""Round 42 — Per-field independent matcher.

For each kanban row, search plan_colunas + SAP for the closest match
to each field INDEPENDENTLY of all other fields (no cascade).

Used as a cross-validator AFTER the cascade snap pass: when cascade
applied a snap (raw != snapped) but per-field independent matching
finds the original OCR has a distance-0 hit in the canonical pool,
revert the cascade — the OCR is correct, cascade was over-confident.

Public API:
    per_field_match(row, refs) -> dict[field, MatchResult]
    cross_validate_cascade(cells, row_index) -> list[reverts applied]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .snap import (
    _CLIENTES,
    _CLIENTE_ALIASES,
    _LOTES_SAP,
    _MODELOS_FULL,
    _OF_TO_ENTRIES_FULL,
    _OFS_PLAN_STR,
    _conf_weighted_dist,
)


@dataclass(frozen=True)
class MatchResult:
    raw: str
    best: str
    distance: float
    source: str          # "lexicon" | "plan" | "sap" | "raw"
    confident: bool      # distance ≤ 1 with no ambiguity


# ── Cached pools (populated lazily) ────────────────────────────────

_PLAN_FIRST_TOKENS: tuple[str, ...] | None = None
_PLAN_OVS_ALL: frozenset[str] | None = None
_PLAN_CLIENTES_ALL: frozenset[str] | None = None


def _plan_first_tokens() -> tuple[str, ...]:
    """All distinct plan.designacao first-tokens (uppercase)."""
    global _PLAN_FIRST_TOKENS
    if _PLAN_FIRST_TOKENS is None:
        seen: set[str] = set()
        for entries in _OF_TO_ENTRIES_FULL.values():
            for e in entries:
                d = (e.get("designacao") or "").strip()
                if not d:
                    continue
                ft = d.split(" - ", 1)[0].strip().upper()
                if len(ft) >= 4:
                    seen.add(ft)
        _PLAN_FIRST_TOKENS = tuple(sorted(seen))
    return _PLAN_FIRST_TOKENS


def _plan_ovs_all() -> frozenset[str]:
    global _PLAN_OVS_ALL
    if _PLAN_OVS_ALL is None:
        seen: set[str] = set()
        for entries in _OF_TO_ENTRIES_FULL.values():
            for e in entries:
                ov = (e.get("ov") or "").strip()
                if ov:
                    seen.add(str(ov))
        _PLAN_OVS_ALL = frozenset(seen)
    return _PLAN_OVS_ALL


def _plan_clientes_all() -> frozenset[str]:
    global _PLAN_CLIENTES_ALL
    if _PLAN_CLIENTES_ALL is None:
        seen: set[str] = set()
        for entries in _OF_TO_ENTRIES_FULL.values():
            for e in entries:
                cli = (e.get("cliente") or "").strip().upper()
                if cli:
                    seen.add(cli)
        _PLAN_CLIENTES_ALL = frozenset(seen)
    return _PLAN_CLIENTES_ALL


# ── Field-specific closest matchers ────────────────────────────────


def closest_modelo(ocr: str) -> MatchResult:
    """Search GT lexicon + plan first-tokens for closest match.

    Returns distance 0 only when OCR is an exact uppercase match.
    Confident when single distance-≤1 candidate is found (no ties).
    """
    raw = ocr or ""
    su = raw.upper().strip()
    if not su or len(su) < 4:
        return MatchResult(raw=raw, best=raw, distance=0.0,
                           source="raw", confident=False)
    # Pool: GT lexicon + plan first-tokens (deduped)
    pool: set[str] = set()
    for m in _MODELOS_FULL:
        pool.add(m.upper().strip())
    for ft in _plan_first_tokens():
        pool.add(ft)
    # Stage 1 — exact uppercase hit
    if su in pool:
        # Find the canonical case from lexicon if present
        for m in _MODELOS_FULL:
            if m.upper().strip() == su:
                return MatchResult(raw=raw, best=m, distance=0.0,
                                   source="lexicon", confident=True)
        for ft in _plan_first_tokens():
            if ft == su:
                return MatchResult(raw=raw, best=ft, distance=0.0,
                                   source="plan", confident=True)
    # Stage 2 — Lev-1 candidates (length within ±1)
    candidates: list[tuple[str, float]] = []
    for cand in pool:
        if abs(len(cand) - len(su)) > 1:
            continue
        d = _conf_weighted_dist(cand, su, max_dist=1.0)
        if d <= 1.0:
            candidates.append((cand, d))
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best, best_d = candidates[0]
        ties = [c for c, d in candidates if d == best_d and c != best]
        return MatchResult(raw=raw, best=best, distance=best_d,
                           source="lexicon" if best in {m.upper().strip() for m in _MODELOS_FULL} else "plan",
                           confident=len(ties) == 0)
    return MatchResult(raw=raw, best=raw, distance=float("inf"),
                       source="raw", confident=False)


def closest_cliente(ocr: str) -> MatchResult:
    """Search lexicon + plan + aliases for closest cliente."""
    raw = ocr or ""
    su = raw.upper().strip()
    if not su:
        return MatchResult(raw=raw, best=raw, distance=0.0,
                           source="raw", confident=False)
    # Stage 1 — alias hit
    if su in _CLIENTE_ALIASES:
        return MatchResult(raw=raw, best=_CLIENTE_ALIASES[su],
                           distance=0.0, source="lexicon", confident=True)
    # Pool
    pool: set[str] = set()
    for c in _CLIENTES:
        pool.add(c.upper().strip())
    pool |= _plan_clientes_all()
    if su in pool:
        # Find canonical (lexicon preferred for case)
        for c in _CLIENTES:
            if c.upper().strip() == su:
                return MatchResult(raw=raw, best=c, distance=0.0,
                                   source="lexicon", confident=True)
        return MatchResult(raw=raw, best=su, distance=0.0,
                           source="plan", confident=True)
    # Lev-1
    candidates: list[tuple[str, float]] = []
    for cand in pool:
        if abs(len(cand) - len(su)) > 2:
            continue
        d = _conf_weighted_dist(cand, su, max_dist=1.0)
        if d <= 1.0:
            candidates.append((cand, d))
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best, best_d = candidates[0]
        ties = [c for c, d in candidates if d == best_d and c != best]
        return MatchResult(raw=raw, best=best, distance=best_d,
                           source="plan", confident=len(ties) == 0)
    return MatchResult(raw=raw, best=raw, distance=float("inf"),
                       source="raw", confident=False)


def closest_of(ocr: str) -> MatchResult:
    """Search plan OFs for closest match (pure string similarity).

    No multi-field disambig — that's the cascade's job. This is purely
    informational: if cascade didn't snap and plan has a Lev-1 unique
    OF, surface it.
    """
    raw = ocr or ""
    s = raw.strip()
    if not s:
        return MatchResult(raw=raw, best=raw, distance=0.0,
                           source="raw", confident=False)
    if s in _OFS_PLAN_STR:
        return MatchResult(raw=raw, best=s, distance=0.0,
                           source="plan", confident=True)
    # Lev-1 over digits only
    candidates: list[tuple[str, float]] = []
    for cand in _OFS_PLAN_STR:
        if abs(len(cand) - len(s)) > 1:
            continue
        d = _conf_weighted_dist(cand, s, max_dist=1.0)
        if d <= 1.0:
            candidates.append((cand, d))
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best, best_d = candidates[0]
        ties = [c for c, d in candidates if d == best_d and c != best]
        return MatchResult(raw=raw, best=best, distance=best_d,
                           source="plan", confident=len(ties) == 0)
    return MatchResult(raw=raw, best=raw, distance=float("inf"),
                       source="raw", confident=False)


def closest_ov(ocr: str) -> MatchResult:
    raw = ocr or ""
    s = raw.strip()
    if not s:
        return MatchResult(raw=raw, best=raw, distance=0.0,
                           source="raw", confident=False)
    pool = _plan_ovs_all()
    if s in pool:
        return MatchResult(raw=raw, best=s, distance=0.0,
                           source="plan", confident=True)
    candidates: list[tuple[str, float]] = []
    for cand in pool:
        if abs(len(cand) - len(s)) > 1:
            continue
        d = _conf_weighted_dist(cand, s, max_dist=1.0)
        if d <= 1.0:
            candidates.append((cand, d))
    if candidates:
        candidates.sort(key=lambda x: x[1])
        best, best_d = candidates[0]
        ties = [c for c, d in candidates if d == best_d and c != best]
        return MatchResult(raw=raw, best=best, distance=best_d,
                           source="plan", confident=len(ties) == 0)
    return MatchResult(raw=raw, best=raw, distance=float("inf"),
                       source="raw", confident=False)


def closest_lote(ocr: str) -> MatchResult:
    raw = ocr or ""
    s = raw.upper().strip()
    if not s:
        return MatchResult(raw=raw, best=raw, distance=0.0,
                           source="raw", confident=False)
    if s in _LOTES_SAP:
        return MatchResult(raw=raw, best=s, distance=0.0,
                           source="sap", confident=True)
    return MatchResult(raw=raw, best=raw, distance=float("inf"),
                       source="raw", confident=False)


# ── Bulk row matcher ───────────────────────────────────────────────


def per_field_match(row: dict) -> dict[str, MatchResult]:
    """Run independent per-field closest match for a row dict."""
    return {
        "modelo": closest_modelo(str(row.get("modelo", "") or "")),
        "cliente": closest_cliente(str(row.get("cliente", "") or "")),
        "of": closest_of(str(row.get("of", "") or "")),
        "ov": closest_ov(str(row.get("ov", "") or "")),
        "lote": closest_lote(str(row.get("lote", "") or "")),
    }


# ── Cross-validator hook (used by pipeline) ────────────────────────


def cross_validate_cell(field: str, raw_value: str, snapped_value: str,
                        cascade_rule: str, cascade_distance: float
                        ) -> Optional[MatchResult]:
    """Decide whether to revert a cascade-applied snap.

    Returns a MatchResult if cascade should be REVERTED to raw.
    Returns None if cascade snap stands.

    Decision rules:

    1. Only check modelo and cliente (other fields lack reliable
       independent canonical pools).
    2. Only consider reverting Lev-2 fuzzy snaps (rules with
       ``_lev2`` or distance in (1.0, 2.5]). Plan-canonical (R32)
       and Lev-1 are authoritative — don't second-guess them.
    3. Revert only when raw is an EXACT (distance 0, confident)
       canonical hit AND cascade picked a different canonical.

    This catches the specific R42 over-snap pattern: lexicon Lev-2
    fired (e.g. CLC5E04D -> CLC7E05D) but raw is itself a valid
    known modelo. Plan-overrides (R32) like ``CLCAF66DLV`` -> plan
    canonical are preserved because their cascade rule contains
    ``plan_canonical`` and distance is 99.0.
    """
    if not raw_value or raw_value.strip() == snapped_value.strip():
        return None
    # Skip ANY plan-aware cascade decision (plan_canonical, plan_lev2,
    # plan_prefix, plan_lcs, ov_unanimous, plan_designacao_lev1, etc.).
    # R32 made plan authoritative — even fuzzy matches against the plan
    # are the source of truth for the row's OF. Also skip aliases,
    # exact hits, and prefix_superset (R42 own).
    rule = cascade_rule or ""
    if any(keyword in rule for keyword in (
        "plan_", "ov_unanimous", "alias", "exact", "prefix_superset",
        "from_ov", "format_clean", "multifield",
    )):
        return None
    # Only second-guess legacy lexicon Lev-2 fuzzy fires (distance
    # roughly 1.5-2.5). These are the dangerous ones: no plan/SAP
    # context, just lexicon similarity that can pick wrong canonical.
    if cascade_distance < 1.4 or cascade_distance > 2.6:
        return None
    if field == "modelo":
        result = closest_modelo(raw_value)
    elif field == "cliente":
        result = closest_cliente(raw_value)
    else:
        return None
    # Revert only when raw is exact canonical hit AND cascade picked a
    # different canonical via fuzzy matching (typical Lev-2 over-snap).
    if result.distance == 0.0 and result.confident:
        if result.best.upper().strip() != snapped_value.upper().strip():
            return result
    return None
