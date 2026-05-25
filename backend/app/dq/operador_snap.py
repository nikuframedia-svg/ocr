"""Round 70 — Operator snap against ListaColaboradores (SAP employee export).

Resolves OCR-captured operator name + n_operador against the canonical
SAP employee list using a 3-level confidence strategy. Auto-substitutes
the operator name whenever there's strong identity signal (matching cod
+ ≥1 token in common between OCR name and canonical sname).

Three confidence levels:

  HIGH — auto-snap silently (green / no UI flag):
    (A) cod exact + name exact (no-op confirm)
    (B) cod exact + ≥1 shared token between OCR and canonical sname
    (C) cod NOT in list, but unique Lev-1 candidate in list shares ≥1
        token → snap both cod and name

  MEDIUM — keep OCR + flag yellow (cell ``cc-na-suspended``):
    (D) cod exact + 0 shared tokens — probably wrong cod or wrong name
    (E) cod NOT in list, Lev-1 yields ≥2 candidates with token overlap
        OR 0 candidates

  NO_REF — current behaviour (gray, no flag):
    (F) cod empty/non-numeric

Public API:
    snap_operador(raw_name, raw_cod, colaboradores) -> SnapResult
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapResult:
    """Result of an operator snap attempt.

    Attributes:
        raw_name: OCR's operator name string (as captured)
        raw_cod: OCR's n_operador string (as captured)
        snapped_name: canonical sname or raw_name if not snapped
        snapped_cod: canonical cod string or raw_cod if not snapped
        pernr: full 8-digit pernr ("10000XXX") or "" if no match
        rule: classifier of the decision (e.g. "operador.cod_token_match")
        applied: True if snapped_name != raw_name OR snapped_cod != raw_cod
        suspended: True if cell should render with yellow flag (MEDIUM)
    """
    raw_name: str
    raw_cod: str
    snapped_name: str
    snapped_cod: str
    pernr: str
    rule: str
    applied: bool
    suspended: bool


def _strip_accents(s: str) -> str:
    """NFD-decompose + drop combining marks. 'JÚLIO' → 'JULIO'."""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in nfd if not unicodedata.combining(ch))


def _normalize_name(raw: str) -> str:
    """Uppercase + strip accents + collapse whitespace."""
    if not raw:
        return ""
    return " ".join(_strip_accents(raw).upper().split())


def _tokens(name: str) -> set[str]:
    """Tokenize normalized name; drop tokens with len < 3 (partículas DA/DE)."""
    if not name:
        return set()
    return {t for t in name.split() if len(t) >= 3}


def _parse_cod(raw: str | int | None) -> int | None:
    """Strip leading zeros, parse as int. Returns None if empty/invalid.

    Empty strings and whitespace-only strings return None (treated as
    'no cod captured') rather than 0.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    stripped = s.lstrip("0") or "0"
    try:
        return int(stripped)
    except (ValueError, TypeError):
        return None


# R91 — letter→digit confusion map for OCR recovery of operator codes.
# When the OCR captures a string with letters (e.g. "J-81" instead of
# "1481"), we generate candidate integer codes by trying each letter as
# its plausible digit equivalent. Multi-option lists let us cover
# letters that look like multiple digits (L → "1" or "4").
_LETTER_DIGIT_CANDIDATES: dict[str, list[str]] = {
    "J": ["1"], "I": ["1"], "L": ["1", "4"],
    "T": ["7"], "Z": ["2"], "S": ["5"],
    "B": ["8"], "O": ["0"], "G": ["6", "9"],
    "Q": ["0", "9"], "Y": ["4"], "C": ["0"],
    # Separators / punctuation: skip OR (for "-") try as caligraphic "4"
    # whose right stem is missing.
    "-": ["", "4"], ".": [""], "/": [""], " ": [""], "º": [""],
    "º".upper(): [""], "N": [""],  # "Nº" prefix
}


def _generate_cod_candidates(raw: str | int | None) -> list[int]:
    """R91 — Generate plausible integer cod candidates from an OCR-corrupted
    input by trying each character as itself (if digit) or as one of the
    letter→digit confusion mappings.

    Examples:
      "J-81"  → [181, 1481]   (J→1; -→{empty, 4})
      "1391"  → [1391]        (already numeric)
      "Z48"   → [248]         (Z→2)
      "abc"   → []            (no digit interpretation possible)

    Bounded to ≤6-digit candidates to avoid combinatorial blowup. Returns
    empty list when ANY character is unknown — better to fail than guess
    wildly.
    """
    if raw is None:
        return []
    s = str(raw).strip().upper()
    if not s:
        return []
    char_options: list[list[str]] = []
    for ch in s:
        if ch.isdigit():
            char_options.append([ch])
        elif ch in _LETTER_DIGIT_CANDIDATES:
            char_options.append(_LETTER_DIGIT_CANDIDATES[ch])
        else:
            return []  # unknown char — bail to avoid junk candidates
    from itertools import product
    seen: set[int] = set()
    out: list[int] = []
    for combo in product(*char_options):
        digits = "".join(combo)
        if not digits or len(digits) > 6:
            continue
        try:
            n = int(digits.lstrip("0") or "0")
        except ValueError:
            continue
        if n > 0 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _derive_pernr_from_cod(cod: int) -> str:
    """R79 — structural derivation of pernr from cod.

    SAP pernr is 8 digits. For cods up to 4 digits, prepend "1000" and
    zero-pad to make 8 chars total. For cods already ≥8 digits (rare —
    sometimes operator writes full pernr), return as-is.

    Examples:
        cod=95   → "10000095"
        cod=1391 → "10001391"
        cod=4397 → "10004397"
        cod=10003261 → "10003261" (already 8-digit)

    Used as fallback when cod isn't in ListaColaboradores — pernr format
    is deterministic, doesn't need list confirmation.
    """
    if cod < 10000:
        return f"1000{cod:04d}"
    return str(cod)


def _lev1_digit_candidates(cod: int) -> list[int]:
    """Generate same-length single-digit-substitution Lev-1 candidates.

    Example: 1391 → 0391, 2391, ..., 1291, 1491, ..., 1390, 1392.
    Excludes the original. Returns ints.
    """
    s = str(cod)
    out: list[int] = []
    for i in range(len(s)):
        for d in "0123456789":
            if d == s[i]:
                continue
            cand = s[:i] + d + s[i + 1:]
            # Drop leading-zero artefacts that aren't meaningful int change
            if cand[0] == "0" and len(s) > 1:
                # leading-zero cand collapses to shorter int — still valid lookup
                pass
            try:
                v = int(cand)
            except ValueError:
                continue
            if v != cod:
                out.append(v)
    return out


def _find_unique_name_match(
    name_norm: str,
    colaboradores: dict[int, dict[str, str]],
) -> tuple[int, dict[str, str]] | None:
    """R124 — devolve (cod, entry) se o nome normalizado bate exactamente
    com UM único colaborador; None caso contrário (0 ou ≥2 matches).
    """
    if not name_norm:
        return None
    matches = [
        (cod, entry) for cod, entry in colaboradores.items()
        if _normalize_name(entry.get("sname", "")) == name_norm
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def snap_operador(
    raw_name: str | None,
    raw_cod: str | int | None,
    colaboradores: dict[int, dict[str, str]],
    *,
    aliases: dict[str, dict] | None = None,
) -> SnapResult:
    """Resolve operator identity against canonical SAP employee list.

    See module docstring for the 5 condition rules (A-F). Always returns
    a SnapResult — never raises.

    R91 extensions when cod fails ``_parse_cod``:
      G1 — alias lexicon: previously-corrected OCR names map directly to
           the canonical cod/pernr/sname (highest confidence).
      G2 — confusion-map: generate plausible cod candidates via letter→
           digit mapping; snap if exactly one candidate exists in colabs.
    """
    name_raw = str(raw_name or "")
    cod_raw_str = str(raw_cod or "")
    cod_int = _parse_cod(raw_cod)
    name_norm_pre = _normalize_name(name_raw)

    # Condition F — cod empty/non-numeric. Try alias + confusion before
    # falling back to no_ref.
    if cod_int is None:
        # G1 — alias lexicon (memorized manual corrections)
        if aliases and name_norm_pre and name_norm_pre in aliases:
            al = aliases[name_norm_pre]
            try:
                al_cod = int(str(al.get("cod") or ""))
            except (ValueError, TypeError):
                al_cod = 0
            if al_cod > 0:
                return SnapResult(
                    raw_name=name_raw,
                    raw_cod=cod_raw_str,
                    snapped_name=str(al.get("sname") or "").upper(),
                    snapped_cod=str(al_cod),
                    pernr=str(al.get("pernr") or _derive_pernr_from_cod(al_cod)),
                    rule="operador.alias_lexicon",
                    applied=True,
                    suspended=False,
                )

        # G2 — confusion-map candidates filtered against colabs
        candidates = _generate_cod_candidates(cod_raw_str)
        confusion_matches: list[int] = [c for c in candidates if c in colaboradores]
        if len(confusion_matches) == 1:
            cand = confusion_matches[0]
            entry = colaboradores[cand]
            return SnapResult(
                raw_name=name_raw,
                raw_cod=cod_raw_str,
                snapped_name=entry["sname"],
                snapped_cod=str(cand),
                pernr=entry["pernr"],
                rule="operador.confusion_unique",
                applied=True,
                suspended=True,  # yellow flag — operator should verify
            )

        # R124 — name_unique_match: cod inválido, mas o nome OCR normaliza
        # exactamente para UM único colaborador → snap completo. Marca
        # suspended (amarelo) para o operador verificar — o cod ficou só
        # a "esperar" pela confirmação humana.
        match = _find_unique_name_match(name_norm_pre, colaboradores)
        if match is not None:
            cand_cod, entry = match
            return SnapResult(
                raw_name=name_raw,
                raw_cod=cod_raw_str,
                snapped_name=entry["sname"],
                snapped_cod=str(cand_cod),
                pernr=entry["pernr"],
                rule="operador.name_unique_match",
                applied=True,
                suspended=True,
            )

        # Multiple or no candidates → no_ref (cinza, sem flag)
        return SnapResult(
            raw_name=name_raw,
            raw_cod=cod_raw_str,
            snapped_name=name_raw,
            snapped_cod=cod_raw_str,
            pernr="",
            rule="operador.no_ref",
            applied=False,
            suspended=False,
        )

    name_norm = _normalize_name(name_raw)
    name_tokens = _tokens(name_norm)

    # Look up cod in list
    entry = colaboradores.get(cod_int)

    if entry is not None:
        canonical_sname = entry["sname"]   # already UPPER + ASCII
        canonical_pernr = entry["pernr"]
        canonical_tokens = _tokens(canonical_sname)

        # Condition A — exact match (cod + name) — no-op confirm
        if name_norm == canonical_sname:
            return SnapResult(
                raw_name=name_raw,
                raw_cod=cod_raw_str,
                snapped_name=canonical_sname,
                snapped_cod=str(cod_int),
                pernr=canonical_pernr,
                rule="operador.exact_match",
                applied=False,
                suspended=False,
            )

        # Condition B — cod exact + ≥1 shared token → snap name
        shared = name_tokens & canonical_tokens
        if shared:
            return SnapResult(
                raw_name=name_raw,
                raw_cod=cod_raw_str,
                snapped_name=canonical_sname,
                snapped_cod=str(cod_int),
                pernr=canonical_pernr,
                rule="operador.cod_token_match",
                applied=True,
                suspended=False,
            )

        # Condition D — cod exact + 0 shared tokens. R79: TRUST COD.
        # User explicitly: "Ter como o input mais relevante o nº do
        # colaborador". Snap name + pernr to canonical, flag yellow so
        # supervisor can review the name divergence.
        return SnapResult(
            raw_name=name_raw,
            raw_cod=cod_raw_str,
            snapped_name=canonical_sname,
            snapped_cod=str(cod_int),
            pernr=canonical_pernr,
            rule="operador.cod_trust_no_overlap",
            applied=True,
            suspended=True,
        )

    # cod NOT in list — try Lev-1 substitution
    matches: list[tuple[int, dict[str, str], set[str]]] = []  # (cand_cod, entry, shared_tokens)
    for cand in _lev1_digit_candidates(cod_int):
        cand_entry = colaboradores.get(cand)
        if cand_entry is None:
            continue
        cand_tokens = _tokens(cand_entry["sname"])
        overlap = name_tokens & cand_tokens
        if overlap:
            matches.append((cand, cand_entry, overlap))

    # Condition C — exactly 1 Lev-1 candidate with token overlap → snap both
    if len(matches) == 1:
        cand_cod, cand_entry, _ = matches[0]
        return SnapResult(
            raw_name=name_raw,
            raw_cod=cod_raw_str,
            snapped_name=cand_entry["sname"],
            snapped_cod=str(cand_cod),
            pernr=cand_entry["pernr"],
            rule="operador.cod_lev1_token_match",
            applied=True,
            suspended=False,
        )

    # R124 — antes do fallback "preserva OCR name": se o nome normalizado
    # bate exactamente com UM único colaborador, snap pelo nome (mais
    # fiável que cod-Lev-1 ambíguo). Mantém suspended (amarelo).
    match = _find_unique_name_match(name_norm, colaboradores)
    if match is not None:
        cand_cod, entry = match
        return SnapResult(
            raw_name=name_raw,
            raw_cod=cod_raw_str,
            snapped_name=entry["sname"],
            snapped_cod=str(cand_cod),
            pernr=entry["pernr"],
            rule="operador.name_unique_match",
            applied=True,
            suspended=True,
        )

    # Condition E — ambiguous (≥2) or no Lev-1 with overlap.
    # R79: even when cod not in list, DERIVE pernr structurally. The
    # pernr format ("1000" + zfill(4)(cod)) is deterministic — doesn't
    # need list confirmation. Preserves OCR name (no canonical available).
    derived_pernr = _derive_pernr_from_cod(cod_int)
    rule = "operador.cod_lev1_ambiguous" if matches else "operador.cod_not_in_list_pernr_derived"
    return SnapResult(
        raw_name=name_raw,
        raw_cod=cod_raw_str,
        snapped_name=name_raw,
        snapped_cod=str(cod_int),
        pernr=derived_pernr,
        rule=rule,
        applied=True,  # pernr field is being set even if name/cod unchanged
        suspended=True,
    )


__all__ = ["SnapResult", "snap_operador"]
