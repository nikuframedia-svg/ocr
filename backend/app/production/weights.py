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


def _human_fields(row: dict) -> frozenset[str]:
    """R269 — `production_rows.human_fields`: campos desta linha cuja última
    edição é humana/wizard (CSV escrito por db._sync_production_rows)."""
    raw = row.get("human_fields")
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in str(raw).split(",") if p.strip())


def _resolved_input(
    field: str,
    human: frozenset[str],
    row_value: Any,
    *ref_values: Any,
) -> float | None:
    """R269 — precedência humana: um campo editado pelo operador ganha ao
    plano/StockSAP (as referências existem para ignorar ruído de OCR, não
    decisões humanas). Sem edição humana, mantém-se a ordem histórica
    plano/SAP → valor da folha."""
    if field in human:
        return _first_positive(row_value, *ref_values)
    return _first_positive(*ref_values, row_value)


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
    # R266 — a plan entry always carries the "npecas" key (ref_watcher), but
    # a large share of plan rows have it blank/0; those must still fall back
    # to the legacy geometric formula instead of blanking consumption KPIs.
    if plan_entry is not None:
        n = _to_int(plan_entry.get("npecas"))
        if n and n > 0:
            return n
    n = calc_npecas(larg, lbase, ltopo, comp, comp)
    return n if n > 0 else None


def _sap_entry_for_row(row: dict, refs: dict | None) -> dict | None:
    """StockSAP entry for the row's lote, resolved like the R261 banner.

    R266 — the raw dict lookup previously used here missed lotes written with
    the H↔M OCR confusion, so sibling rows of the same physical bobine could
    export with and without consumption KPIs. Delegates to the banner's
    resolver (exact/alias match or divergence-guarded H→M correction).
    """
    if not refs or not refs.get("lotes_sap_full"):
        return None
    # Local import: keeps this light module importable without pulling the
    # whole cross engine at import time (and safe against future cycles).
    from app.pipeline.scoring_engine import _sap_entry_for_measures

    # R269 — um lote editado pelo operador resolve pelo mesmo caminho: o
    # match exato/alias nunca é vetado por medidas, e o veto H→M (R259, gate
    # Luís) mantém-se — se o humano corrigiu também larg/esp, esses valores
    # ganham à entry de qualquer forma (precedência humana nos inputs).
    _, entry = _sap_entry_for_measures(refs, row)
    return entry


def calculate_row_weights(row: dict, refs: dict | None = None) -> RowWeightMetrics:
    """Calculate produced/consumed/waste weights for one production row."""
    qtd = _to_float(row.get("qtd"))
    plan_entry = find_plan_entry(row, refs)
    direct = is_direct_consumption_row(row)
    sap_entry = _sap_entry_for_row(row, refs)

    # R269 — campos com última edição humana/wizard ganham ao plano/SAP
    # (folha 5226: corrigir comp/larg/esp à mão não mudava o desperdício).
    human = _human_fields(row)
    comp = _resolved_input(
        "comp_mm", human,
        row.get("comp_mm"),
        plan_entry.get("comp") if plan_entry else None,
    )
    lbase = _resolved_input(
        "lbase", human,
        row.get("lbase"),
        plan_entry.get("lbase") if plan_entry else None,
    )
    ltopo = _resolved_input(
        "ltopo", human,
        row.get("ltopo"),
        plan_entry.get("ltopo") if plan_entry else None,
    )
    esp = _resolved_input(
        "esp", human,
        row.get("esp"),
        sap_entry.get("esp") if sap_entry else None,
        plan_entry.get("esp") if plan_entry else None,
    )
    larg = _resolved_input(
        "larg_mm", human,
        row.get("larg_mm"),
        sap_entry.get("larg") if sap_entry else None,
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
