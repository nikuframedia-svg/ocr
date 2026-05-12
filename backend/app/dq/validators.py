"""L1 — lazy field-level validation.

Reuses regex patterns from :mod:`schemas_strict` but never raises:
each cell that fails validation produces a :class:`ValidationIssue`
with a severity tag, leaving the value untouched. Downstream layers
(L2 snap, L3 constraints) decide whether to auto-fix or escalate.

MODELO is special — the strict pattern is too permissive (matches any
alphanumeric run). We use a disjunctive list of MODELO families
mined from the 19 sample sheets, which lets us flag truly weird
shapes (e.g. ``"1ª PRIORIDADE"`` extracted as a whole MODELO).

Public API:
    validate_extraction(extraction: dict) -> list[ValidationIssue]
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

# Reuse already-calibrated patterns where they're tight enough.
from app.pipeline.inference.schemas_strict import (
    _CONI,
    _DATE,
    _ESP,
    _HORAS,
    _NUMERIC_DIM,
    _NUMERIC_LARGE,
    _NUMERIC_SHORT,
    _OF,
    _OPERADOR,
    _OV,
    _PRI,
    _SETOR,
)

Severity = Literal["error", "warning", "info"]


# MODELO family patterns — disjunctive match. Drawn from observed.json
# modelo_prefix list, walkthrough corrections, and v9 false-positive
# review. Each pattern covers a recognisable family. Extend as new
# models surface in production.
#
# Design philosophy: prefer to over-accept (false-negative on real
# garbage) over false-flagging valid-but-unusual MODELOs. L2 snap and
# L3 constraints catch errors L1 misses.
_MODELO_FAMILIES: tuple[re.Pattern[str], ...] = (
    # Core C-family: C + 2 letter/digit + (optional C) + digit/letter
    # + family + digits + tail. Permissive on inner chars because we
    # observed real values like CGCAE04Di, CLC5E4SD, CLCCF07R-V.
    # e.g. CGC2E10D, CFC5F45RiV, CLC8F07Ri-V, CCS2E052i, CGCAE04Di,
    #      CLC5E4SD (S in digit position), CLCCF07R-V.
    re.compile(
        r"^C[A-Z][A-Z0-9]C?[A-Z0-9][A-Z][A-Z0-9]\d{0,2}"
        r"[A-Z]?[a-zA-Z]?[iIvV]?(?:-?[VD])?$"
    ),
    # C-family with priority suffix (with or without space before suffix)
    re.compile(
        r"^C[A-Z][A-Z0-9]C?[A-Z0-9][A-Z][A-Z0-9]\d{0,2}"
        r"[A-Z]?[a-zA-Z]?[iIvV]?(?:-\s?[12]ªP(?:RIORIDADE)?)?$"
    ),
    # OMEGA family
    re.compile(r"^OMEGA\d{2}-\d[A-Z]?$"),
    # Numeric-prefix families
    re.compile(r"^\d{4}VF\d{2}$"),                 # 1383VF01
    re.compile(r"^\d{4,5}[A-Z]\d+$"),              # 8615F00, 87790J01
    re.compile(r"^PTJ\d{6}$"),                     # PTJ157578
    re.compile(r"^BSBCA\d{4}$"),                   # BSBCA0066
    re.compile(r"^LMF\d{4}T$"),                    # LMF1882T
    re.compile(r"^LNF\d{4}T$"),                    # LNF1882T (variant)
    re.compile(r"^\d{4}-[A-Z]-\d{3}$"),             # 0641-S-515
    re.compile(r"^\d{4}V\d{3}$"),                   # 2315V500
    # Special / freeform-ish
    re.compile(r"^BRA[ÇC]OS$"),                    # BRAÇOS
    re.compile(r"^CONIPROT\.?$"),                  # CONIPROT, CONIPROT.
    re.compile(r"^N[°.:]?\s?\d.*$"),               # N°2.1/2-..., N:2-1/2-...
    re.compile(r"^CASS\s.+$"),                     # CASS N2 1/2
    re.compile(r"^CD\d{2}\s?P\d{3}$"),             # CD03 P502, CD03P503
    re.compile(r"^CA\d{2}F\d{2}D[\s-]N[°º]?\d.*$"),  # CA06F18D N1, CA06F18D-N°9
    re.compile(r"^CRLS-N[°º]\d.*$"),                # CRLS-N°1 CR1921A4505
    re.compile(r"^CB\d{2}E?\d+[A-Z]?$"),           # CB04E63D, CB06E08D
    # Composite: two valid models joined by hyphen (e.g. CD02P11B-CD02P503)
    re.compile(r"^[A-Z]{2}\d{2}[A-Z]\d{2}[A-Z]?-[A-Z]{2}\d{2}[A-Z]\d{3}$"),
)


@dataclass(frozen=True)
class ValidationIssue:
    """One per cell failing L1 validation. Multiple issues per cell allowed."""
    field_path: str       # "header.data" or "rows[3].modelo"
    severity: Severity
    rule: str             # short rule id, e.g. "of.6digits"
    raw: str
    expected: str         # human-readable expected pattern
    detail: str = ""      # optional extra info


# Map field name -> regex pattern. None means use disjunctive _MODELO_FAMILIES.
_FIELD_PATTERNS: dict[str, str | None] = {
    "operador": _OPERADOR,
    "n_operador": _NUMERIC_SHORT,
    "setor_maquina": _SETOR,
    # cod_maquina (M0xx) — empty allowed for legacy / unprinted kanbans
    "cod_maquina": r"^M\d{3}$|^$",
    "data": _DATE,
    "pri": _PRI,
    "ov": _OV,
    "of": _OF,
    "modelo": None,  # disjunctive families
    "qtd": _NUMERIC_SHORT,
    "comp_mm": _NUMERIC_DIM,
    "larg_mm": _NUMERIC_DIM,
    "lote": r"^[HM]\d{2}\s?B\d{4,5}$|^$",
    "coni": _CONI,
    "esp": _ESP,
    "lbase": _NUMERIC_LARGE,
    "ltopo": _NUMERIC_LARGE,
    "colunas_produzidas": _NUMERIC_LARGE,
    "horas_trabalhadas": _HORAS,
    # CLIENTE intentionally has no L1 pattern — it's free-form upper-case
    # text that L2 token-sort against the lexicon is the right tool.
}


def _validate_modelo(value: str) -> bool:
    """Disjunctive: matches if at least one family pattern fires."""
    if not value:
        return True  # empty MODELO is allowed — empty cells are common
    return any(p.fullmatch(value) for p in _MODELO_FAMILIES)


def _validate_field(name: str, value: str, path: str) -> Iterable[ValidationIssue]:
    """Yield 0 or 1 ValidationIssue for a single cell."""
    pattern = _FIELD_PATTERNS.get(name)
    if pattern is None and name == "modelo":
        if not _validate_modelo(value):
            yield ValidationIssue(
                field_path=path,
                severity="warning",
                rule="modelo.unknown_family",
                raw=value,
                expected="one of: C-family / OMEGA / digit-prefix / "
                         "PTJ / BSBCA / LMF / BRAÇOS / N°x / CASS / CDxxP / etc.",
                detail="value does not match any observed MODELO family — "
                       "could be a new variant or a misread",
            )
        return
    if pattern is None:
        return  # field intentionally unvalidated by L1 (e.g. cliente)

    if not re.fullmatch(pattern, value or ""):
        # Severity: 'error' for hard-format fields (OF, LOTE, DATA), 'warning' otherwise
        severity: Severity = (
            "error" if name in ("of", "lote", "data", "n_operador") else "warning"
        )
        yield ValidationIssue(
            field_path=path,
            severity=severity,
            rule=f"{name}.format",
            raw=value or "",
            expected=pattern,
            detail=_format_hint(name),
        )


def _format_hint(name: str) -> str:
    return {
        "of": "OF must be exactly 6 digits",
        "ov": "OV must be 6-8 digits, no symbols",
        "lote": "LOTE: [H|M] + 2 digits + B + 4 or 5 digits (e.g. M25B0746)",
        "data": "DATA: DD-MM-YYYY",
        "n_operador": "N_OPERADOR: 1-4 digits",
        "qtd": "QTD: 1-4 digits",
        "comp_mm": "COMP_MM: 2-5 digits",
        "larg_mm": "LARG_MM: 2-5 digits",
        "lbase": "LBASE: 1-5 digits",
        "ltopo": "LTOPO: 1-5 digits",
        "esp": "ESP: number with optional comma decimal (e.g. 2,6)",
        "coni": "CONI: 10|12|14|T|OCT|OCT.|TORRES",
        "pri": "PRI: number, C-code, P-code, or REP. Cn",
        "horas_trabalhadas": "HORAS: H:MM, H, or H[,. ]MM",
        "operador": "OPERADOR: uppercase name (with diacritics)",
        "setor_maquina": "SETOR/MAQUINA: uppercase, hyphenated",
        "cod_maquina": "COD_MAQUINA: M + 3 digits (e.g. M032) or empty",
        "colunas_produzidas": "COLUNAS_PRODUZIDAS: integer count",
    }.get(name, "")


def validate_extraction(extraction: dict) -> list[ValidationIssue]:
    """Run L1 validation over the full extraction structure.

    Args:
        extraction: dict matching the ocr6.py output schema:
            {"header": {...}, "rows": [{...}, ...], "footer": {...}}

    Returns:
        list[ValidationIssue]. Empty list = all cells pass L1.
    """
    issues: list[ValidationIssue] = []
    header = extraction.get("header", {}) or {}
    footer = extraction.get("footer", {}) or {}
    rows = extraction.get("rows", []) or []

    for fname, fval in header.items():
        issues.extend(_validate_field(fname, str(fval or ""), f"header.{fname}"))
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for fname, fval in row.items():
            issues.extend(_validate_field(fname, str(fval or ""), f"rows[{i}].{fname}"))
    for fname, fval in footer.items():
        issues.extend(_validate_field(fname, str(fval or ""), f"footer.{fname}"))

    return issues


def summarize_issues(issues: list[ValidationIssue]) -> dict[str, int]:
    """Quick aggregate counts for logging / triage."""
    by_severity: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    by_rule: dict[str, int] = {}
    for issue in issues:
        by_severity[issue.severity] += 1
        by_rule[issue.rule] = by_rule.get(issue.rule, 0) + 1
    return {"total": len(issues), "by_severity": by_severity, "by_rule": by_rule}
