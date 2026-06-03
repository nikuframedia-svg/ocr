"""Machine lookup helpers backed by ``maquinas.xlsx`` refs."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_PAVILION_ALIASES = (
    (re.compile(r"\bPAV\s*([48])\b"), r"P\1"),
    (re.compile(r"\bPAV([48])\b"), r"P\1"),
)


def normalize_machine_label(value: Any) -> str:
    """Uppercase/ascii machine label with punctuation collapsed to spaces."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return ""
    no_accents = unicodedata.normalize("NFKD", raw)
    no_accents = "".join(ch for ch in no_accents if not unicodedata.combining(ch))
    cleaned = "".join(ch.upper() if ch.isalnum() else " " for ch in no_accents)
    return " ".join(cleaned.split())


def machine_label_variants(value: Any) -> tuple[str, ...]:
    """Return comparable variants, including PAV.8/PAV8 -> P8 aliases."""
    base = normalize_machine_label(value)
    if not base:
        return ()
    variants = {base}
    for pattern, repl in _PAVILION_ALIASES:
        variants.add(pattern.sub(repl, base))
    return tuple(v for v in variants if v)


def _iter_machine_candidates(refs: dict | None) -> Iterable[tuple[str, dict]]:
    seen: set[tuple[str, str]] = set()
    by_kanban = (refs or {}).get("maquinas_by_kanban") or {}
    for label, rec in by_kanban.items():
        if not isinstance(rec, dict):
            continue
        code = str(rec.get("codmaq") or "").strip().upper()
        if not code:
            continue
        key = (str(label), code)
        if key in seen:
            continue
        seen.add(key)
        yield str(label), rec


def _unique_by_code(records: list[dict]) -> dict | None:
    by_code: dict[str, dict] = {}
    for rec in records:
        code = str(rec.get("codmaq") or "").strip().upper()
        if code:
            by_code.setdefault(code, rec)
    if len(by_code) == 1:
        return next(iter(by_code.values()))
    return None


def resolve_machine_from_setor(setor: Any, refs: dict | None) -> dict | None:
    """Resolve ``header.setor_maquina`` to a maquina record.

    Priority:
    1. exact canonical key from ``maquinas_by_kanban``;
    2. exact key after normalising accents/punctuation and PAV aliases;
    3. safe substring match when all candidates point to one codmaq.
    """
    if not setor:
        return None
    by_kanban = (refs or {}).get("maquinas_by_kanban") or {}
    raw = str(setor).strip()
    exact = by_kanban.get(raw) or by_kanban.get(raw.upper())
    if isinstance(exact, dict) and exact.get("codmaq"):
        return exact

    target_variants = set(machine_label_variants(raw))
    if not target_variants:
        return None

    normalised_index: dict[str, list[dict]] = {}
    for label, rec in _iter_machine_candidates(refs):
        for variant in machine_label_variants(label):
            normalised_index.setdefault(variant, []).append(rec)

    for target in target_variants:
        rec = _unique_by_code(normalised_index.get(target, []))
        if rec:
            return rec

    matches: list[dict] = []
    for candidate, records in normalised_index.items():
        if any(target in candidate or candidate in target for target in target_variants):
            matches.extend(records)
    return _unique_by_code(matches)


def machine_phase_from_setor(setor: Any, refs: dict | None) -> str | None:
    rec = resolve_machine_from_setor(setor, refs)
    if not rec:
        return None
    phase = str(rec.get("colunaexcel") or "").strip().lower()
    return phase or None


__all__ = [
    "machine_label_variants",
    "machine_phase_from_setor",
    "normalize_machine_label",
    "resolve_machine_from_setor",
]
