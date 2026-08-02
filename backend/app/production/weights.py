"""Central weight calculations for CPIS exports and production KPIs.

The important split is:
- Bobine/Formato produced weight represents cut pieces and therefore prefers
  geometry, falling back to the plan unit weight only when geometry is absent;
- later production phases prefer the canonical plan unit weight;
- consumed weight and waste are only material-cutting metrics, currently
  limited to Bobine/Formato rows.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from math import ceil
from typing import Any

from app.dq.geometry import (
    DENSITY_KG_PER_MM3,
    calc_npecas,
    row_weight_kg,
)
from app.templates_registry import detect_template

DIRECT_CONSUMPTION_PHASES = {"Bobine Formato"}


@dataclass(frozen=True)
class RowWeightMetrics:
    """Resolved weight metrics for one ``production_rows`` row.

    All weights are stored in kg. Export layers decide the presentation unit
    and rounding.
    """

    n_chapas: int | None = None
    peso_consumido_kg: float | None = None
    peso_produzido_kg: float | None = None
    desperdicio_kg: float | None = None
    desperdicio_pct: float | None = None
    comp_mm: float | None = None
    larg_mm: float | None = None
    esp_mm: float | None = None
    lbase: float | None = None
    ltopo: float | None = None
    direct_consumption: bool = False
    produced_source: str | None = None
    consumption_source: str | None = None


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    f = _to_float(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError, OverflowError):
        return None


def _first_positive(*values: Any) -> float | None:
    for v in values:
        n = _to_float(v)
        if n is not None and n > 0:
            return n
    return None


def _norm_text(v: Any) -> str:
    raw = str(v or "").strip().upper()
    if not raw:
        return ""
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    return "".join(ch for ch in raw if ch.isalnum())


def normalize_of(value: Any) -> str:
    """Return the canonical 6-digit OF string when the value is numeric."""
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s.zfill(6) if len(s) < 6 else s
    try:
        n = float(s.replace(",", "."))
    except ValueError:
        return s
    if n.is_integer():
        out = str(int(n))
        return out.zfill(6) if len(out) < 6 else out
    return s


def find_plan_entry(row: dict, refs: dict | None) -> dict | None:
    """Best matching plan row for a production row.

    OF is the primary key. When an OF has multiple plan entries, prefer the
    entry whose designation contains the OCR/reference model; then OV and
    cliente are used as tie-breakers. If there is no OF match, fall back to
    a unique ``OV + modelo`` match. Falls back to the first OF entry when no
    stronger signal exists.
    """
    if not refs:
        return None
    row_model = _norm_text(row.get("modelo"))
    row_ov = str(row.get("ov") or "").strip()
    row_cliente = _norm_text(row.get("cliente"))
    row_comp = _to_float(row.get("comp_mm"))
    row_lbase = _to_float(row.get("lbase"))
    row_ltopo = _to_float(row.get("ltopo"))

    def model_matches(entry: dict) -> bool:
        des = _norm_text(entry.get("designacao"))
        return bool(row_model and des and (row_model in des or des in row_model))

    def score(entry: dict) -> tuple[int, float]:
        s = 0
        if model_matches(entry):
            s += 8
        if row_ov and row_ov == str(entry.get("ov") or "").strip():
            s += 4
        if row_cliente and row_cliente == _norm_text(entry.get("cliente")):
            s += 2
        diffs = []
        for row_v, key, tol in (
            (row_comp, "comp", 50.0),
            (row_lbase, "lbase", 30.0),
            (row_ltopo, "ltopo", 30.0),
        ):
            plan_v = _to_float(entry.get(key))
            if row_v is not None and plan_v is not None:
                diff = abs(row_v - plan_v)
                diffs.append(diff)
                if diff <= tol:
                    s += 1
        return s, -sum(diffs)

    def unique_ov_model_match() -> dict | None:
        if not row_ov or not row_model:
            return None
        ov_entries = (refs.get("plan_by_ov") or {}).get(row_ov) or []
        matches = [entry for entry in ov_entries if model_matches(entry)]
        by_identity: dict[tuple[str, str], dict] = {}
        for entry in matches:
            key = (
                str(entry.get("_of") or entry.get("of") or "").strip(),
                _norm_text(entry.get("designacao")),
            )
            by_identity.setdefault(key, entry)
        if len(by_identity) == 1:
            return next(iter(by_identity.values()))
        return None

    of_key = normalize_of(row.get("of"))
    entries = (refs.get("of_to_entries") or {}).get(of_key) or []
    if entries:
        best = entries[0] if len(entries) == 1 else max(entries, key=score)
        best_ov = str(best.get("ov") or "").strip()
        if row_ov and row_model and best_ov != row_ov and not model_matches(best):
            return unique_ov_model_match()
        return best

    return unique_ov_model_match()


def is_direct_consumption_row(row: dict) -> bool:
    """True when consumed/waste metrics should be calculated for the row."""
    template_name = str(row.get("template_name") or "").strip()
    if template_name == "bobine_formato":
        return True
    setor = str(row.get("setor_maquina") or row.get("setor_maquina_desc") or "").strip()
    if not setor:
        return False
    tpl = detect_template(setor)
    if tpl.phase not in DIRECT_CONSUMPTION_PHASES:
        return False
    # Avoid treating unknown labels that merely fell back to bobine_formato as
    # direct material consumption.
    setor_norm = _norm_text(setor)
    return "BOB" in setor_norm or "FORMATO" in setor_norm


def _valid_consumption_input(
    qtd: float | None,
    larg: float | None,
    comp: float | None,
    esp: float | None,
    npecas: int | None,
) -> bool:
    if any(v is None for v in (qtd, larg, comp, esp, npecas)):
        return False
    if qtd <= 0 or larg <= 0 or comp <= 0 or esp <= 0 or npecas <= 0:
        return False
    if larg > 3000 or larg < 200:
        return False
    if comp > 20000:
        return False
    return not (esp > 30 or esp < 0.5)


def _resolve_npecas(
    *,
    plan_entry: dict | None,
    larg: float | None,
    lbase: float | None,
    ltopo: float | None,
    comp: float | None,
) -> int | None:
    if plan_entry is not None and "npecas" in plan_entry:
        n = _to_int(plan_entry.get("npecas"))
        return n if n and n > 0 else None
    n = calc_npecas(larg, lbase, ltopo, comp, comp)
    return n if n > 0 else None


def calculate_row_weights(row: dict, refs: dict | None = None) -> RowWeightMetrics:
    """Calculate produced/consumed/waste weights for one production row."""
    qtd = _to_float(row.get("qtd"))
    plan_entry = find_plan_entry(row, refs)
    direct = is_direct_consumption_row(row)
    lote = str(row.get("lote") or "").strip().upper()
    sap_entry = ((refs or {}).get("lotes_sap_full") or {}).get(lote) if lote else None

    comp = _first_positive(
        plan_entry.get("comp") if plan_entry else None,
        row.get("comp_mm"),
    )
    lbase = _first_positive(
        plan_entry.get("lbase") if plan_entry else None,
        row.get("lbase"),
    )
    ltopo = _first_positive(
        plan_entry.get("ltopo") if plan_entry else None,
        row.get("ltopo"),
    )
    esp = _first_positive(
        sap_entry.get("esp") if sap_entry else None,
        plan_entry.get("esp") if plan_entry else None,
        row.get("esp"),
    )
    larg = _first_positive(
        sap_entry.get("larg") if sap_entry else None,
        row.get("larg_mm"),
    )

    if qtd is None or qtd <= 0:
        return RowWeightMetrics(
            comp_mm=comp,
            larg_mm=larg,
            esp_mm=esp,
            lbase=lbase,
            ltopo=ltopo,
            direct_consumption=direct,
        )

    peso_produzido_kg: float | None = None
    produced_source: str | None = None
    pesounit = _to_float(plan_entry.get("pesounit")) if plan_entry else None
    geom_weight = row_weight_kg(qtd, lbase, ltopo, comp, esp)

    # Quantities in Bobine/Formato count cut pieces, so their produced weight
    # must be comparable with the rectangular stock consumed in this phase.
    # Later phases count finished units and keep the official plan weight.
    if direct and geom_weight > 0:
        peso_produzido_kg = geom_weight
        produced_source = "geometry"
    elif pesounit is not None and pesounit > 0:
        peso_produzido_kg = qtd * pesounit
        produced_source = "plan_pesounit"
    elif geom_weight > 0:
        peso_produzido_kg = geom_weight
        produced_source = "geometry"

    if not direct:
        return RowWeightMetrics(
            peso_produzido_kg=peso_produzido_kg,
            comp_mm=comp,
            larg_mm=larg,
            esp_mm=esp,
            lbase=lbase,
            ltopo=ltopo,
            direct_consumption=False,
            produced_source=produced_source,
        )

    npecas = _resolve_npecas(
        plan_entry=plan_entry,
        larg=larg,
        lbase=lbase,
        ltopo=ltopo,
        comp=comp,
    )
    if not _valid_consumption_input(qtd, larg, comp, esp, npecas):
        return RowWeightMetrics(
            peso_produzido_kg=peso_produzido_kg,
            comp_mm=comp,
            larg_mm=larg,
            esp_mm=esp,
            lbase=lbase,
            ltopo=ltopo,
            direct_consumption=True,
            produced_source=produced_source,
        )

    n_chapas = ceil(qtd / npecas)
    peso_consumido_kg = n_chapas * larg * comp * esp * DENSITY_KG_PER_MM3
    desperdicio_kg = None
    desperdicio_pct = None
    if peso_produzido_kg is not None:
        desperdicio_kg = max(0.0, peso_consumido_kg - peso_produzido_kg)
        desperdicio_pct = (
            desperdicio_kg / peso_consumido_kg * 100
            if peso_consumido_kg > 0 else None
        )

    return RowWeightMetrics(
        n_chapas=n_chapas,
        peso_consumido_kg=peso_consumido_kg,
        peso_produzido_kg=peso_produzido_kg,
        desperdicio_kg=desperdicio_kg,
        desperdicio_pct=desperdicio_pct,
        comp_mm=comp,
        larg_mm=larg,
        esp_mm=esp,
        lbase=lbase,
        ltopo=ltopo,
        direct_consumption=True,
        produced_source=produced_source,
        consumption_source="plan_sap" if plan_entry or sap_entry else "ocr",
    )


__all__ = [
    "DIRECT_CONSUMPTION_PHASES",
    "RowWeightMetrics",
    "calculate_row_weights",
    "find_plan_entry",
    "is_direct_consumption_row",
    "normalize_of",
]
