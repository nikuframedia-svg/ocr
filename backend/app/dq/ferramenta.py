"""Normalisation for the internal ``coni`` field.

The paper label is now "FERRAMENTA / CONI", but the database/CPIS column
stays ``coni`` for compatibility.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

ALLOWED_FERRAMENTA_TEXT = frozenset({"CONI", "TORRES", "OCT", "CIL", "CIO", "CIB"})
_NUMERIC_RE = re.compile(r"^\d+$")
# R236 — ferramenta decimal (ex.: "13,7"). O `_clean_token` remove pontuação,
# pelo que "13,7" virava "137" (erro de 10× auto-gravado em produção — folhas
# 2366/2367/2368/2517, corrigido à mão pelos operadores). Reconhecer o decimal
# ANTES de limpar e canonicalizar com vírgula (convenção PT do motor, igual ao
# `esp` em `_format_value`).
_DECIMAL_RE = re.compile(r"^(\d{1,3})\s*[.,]\s*(\d{1,2})$")


def _clean_token(value: Any) -> str:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return ""
    no_accents = unicodedata.normalize("NFKD", raw)
    no_accents = "".join(ch for ch in no_accents if not unicodedata.combining(ch))
    return "".join(ch for ch in no_accents.upper() if ch.isalnum())


def normalize_ferramenta(value: Any) -> str | None:
    """Return the canonical ferramenta value, or ``None`` when invalid.

    Empty input is valid as an absent value and returns ``""``. Text aliases
    such as ``OCT.``/``oct`` and ``CONI.``/``Coni`` are canonicalised. Numeric
    values are preserved as their textual number.
    """
    raw = str(value if value is not None else "").strip()
    decimal = _DECIMAL_RE.fullmatch(raw)
    if decimal:
        # R236 — preservar o separador decimal ("13,7" nunca pode virar "137").
        return f"{decimal.group(1)},{decimal.group(2)}"
    token = _clean_token(value)
    if not token:
        return ""
    if _NUMERIC_RE.fullmatch(token):
        return token
    if token in ALLOWED_FERRAMENTA_TEXT:
        return token
    return None


def is_valid_ferramenta(value: Any) -> bool:
    return normalize_ferramenta(value) is not None


def canonical_ferramenta_or_raw(value: Any) -> str:
    """Canonical value for valid OCR, otherwise the original stripped text."""
    raw = str(value if value is not None else "").strip()
    canonical = normalize_ferramenta(raw)
    return canonical if canonical is not None else raw


__all__ = [
    "ALLOWED_FERRAMENTA_TEXT",
    "canonical_ferramenta_or_raw",
    "is_valid_ferramenta",
    "normalize_ferramenta",
]
