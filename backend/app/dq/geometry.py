"""Round 38 — Geometric calculations: weight + waste for trapezoidal columns.

Mirrors the legacy Metalogalva VBA functions (npecas + desp). All inputs
in mm; weights in kg. Steel density assumed S235JR / S355 (7.85 g/cm³).

Formulas:
    Trapezoidal column area = (lbase + ltopo) / 2 × comp        [mm²]
    Column volume           = area × esp                         [mm³]
    Column weight           = volume × 7.85e-6                   [kg]
    Bobine slice area (1)   = larg × comp_a_cortar               [mm²]
    Pieces per slice        = npecas (from larg/lbase/ltopo formula)
    Slices needed           = ceil(qtd / npecas)
    Total consumed area     = slices × bobine slice area
    Waste                   = consumed − produced (kg or mm²)
    Waste %                 = waste ÷ consumed
"""
from __future__ import annotations

from math import ceil

DENSITY_KG_PER_MM3 = 7.85e-6  # steel S235JR / S355


def calc_npecas(
    larg: float | None,
    lbase: float | None,
    ltopo: float | None,
    comp: float | None,
    comp_teorico: float | None,
) -> int:
    """Number of pieces from one bobine slice — VBA `npecas()` literal port.

    Returns 0 when bobine narrower than lbase (production not viable).
    """
    if not larg or not lbase or not ltopo:
        return 0
    if larg <= 0:
        return 0
    if larg > 3 * lbase + 3 * ltopo:
        return 6
    if larg > 3 * lbase + 2 * ltopo:
        return 5
    if larg > 2 * lbase + 2 * ltopo:
        return 4
    if larg > 2 * lbase + 1 * ltopo:
        return 3
    if larg > 1 * lbase + 1 * ltopo:
        return 2
    if larg > lbase and comp is not None and comp_teorico is not None:
        if comp - comp_teorico > 100:
            return 2
        return 1
    if larg > lbase:
        return 1
    return 0


def trapezoid_area_mm2(lbase: float, ltopo: float, comp: float) -> float:
    """Cross-section × length of one trapezoidal column."""
    return (lbase + ltopo) / 2 * comp


def column_weight_kg(lbase: float, ltopo: float, comp: float, esp: float) -> float:
    """Weight of one finished column."""
    return trapezoid_area_mm2(lbase, ltopo, comp) * esp * DENSITY_KG_PER_MM3


def row_weight_kg(
    qtd: float,
    lbase: float | None,
    ltopo: float | None,
    comp_teorico: float | None,
    esp: float | None,
) -> float:
    """Total weight of N produced columns. Returns 0.0 if any input missing
    or implausible (sanity-guarded against OCR garbage)."""
    if not all([qtd, lbase, ltopo, comp_teorico, esp]):
        return 0.0
    if qtd <= 0 or lbase <= 0 or ltopo <= 0 or comp_teorico <= 0 or esp <= 0:
        return 0.0
    # Sanity guards — reject implausible OCR values
    if esp > 30 or esp < 0.5:
        return 0.0
    if comp_teorico > 20000:
        return 0.0
    if lbase > 2000 or ltopo > 2000:
        return 0.0
    return qtd * column_weight_kg(lbase, ltopo, comp_teorico, esp)


def row_waste(
    qtd: float,
    larg: float | None,
    comp_a_cortar: float | None,
    lbase: float | None,
    ltopo: float | None,
    comp_teorico: float | None,
    esp: float | None,
    *,
    npecas_override: int | None = None,
) -> dict:
    """Waste for a kanban row. Returns dict with computed quantities or
    {"valid": False, "reason": ...} when any input is missing or invalid.

    Output dict keys (when valid):
        valid, npecas_used, ciclos,
        area_teorica_mm2, area_real_total_mm2,
        peso_produzido_kg, peso_consumido_kg, peso_desperdicio_kg,
        desperdicio_pct.
    """
    inputs = (qtd, larg, comp_a_cortar, lbase, ltopo, comp_teorico, esp)
    if any(v is None for v in inputs):
        return {"valid": False, "reason": "missing_input"}
    if any(v <= 0 for v in inputs):
        return {"valid": False, "reason": "non_positive"}
    # Sanity guards (Round 38) — reject obvious OCR garbage so it doesn't
    # inflate dashboard aggregates. Real steel bobines:
    #   larg: 800-2500 mm   (rolling mill width limits)
    #   comp: 1000-15000 mm (typical column lengths)
    #   esp:  1-15 mm       (sheet metal thickness range)
    #   lbase/ltopo: 100-1500 mm
    if larg > 3000 or larg < 200:
        return {"valid": False, "reason": "larg_implausible"}
    if comp_a_cortar > 20000 or comp_teorico > 20000:
        return {"valid": False, "reason": "comp_implausible"}
    if esp > 30 or esp < 0.5:
        return {"valid": False, "reason": "esp_implausible"}
    if lbase > 2000 or ltopo > 2000:
        return {"valid": False, "reason": "lbase_ltopo_implausible"}

    np = npecas_override if npecas_override else calc_npecas(
        larg, lbase, ltopo, comp_a_cortar, comp_teorico
    )
    if np <= 0:
        return {"valid": False, "reason": "npecas_zero"}

    ciclos = ceil(qtd / np)
    area_teorica = trapezoid_area_mm2(lbase, ltopo, comp_teorico)
    area_real_per_cycle = larg * comp_a_cortar
    area_real_total = ciclos * area_real_per_cycle
    area_teorica_total = qtd * area_teorica
    peso_produzido = area_teorica_total * esp * DENSITY_KG_PER_MM3
    peso_consumido = area_real_total * esp * DENSITY_KG_PER_MM3
    peso_waste = max(0.0, peso_consumido - peso_produzido)
    desp_pct = (peso_waste / peso_consumido * 100) if peso_consumido > 0 else 0.0

    return {
        "valid": True,
        "npecas_used": np,
        "ciclos": ciclos,
        "area_teorica_mm2": area_teorica,
        "area_real_total_mm2": area_real_total,
        "peso_produzido_kg": peso_produzido,
        "peso_consumido_kg": peso_consumido,
        "peso_desperdicio_kg": peso_waste,
        "desperdicio_pct": desp_pct,
    }
