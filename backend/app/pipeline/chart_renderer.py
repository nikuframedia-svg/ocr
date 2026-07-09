"""R110.B — Validação e render de gráficos gerados pelo agente.

O Qwen produz um ChartSpec (JSON estruturado) via tool propose_chart.
Aqui validamos schema com Pydantic e (opcionalmente) renderizamos para
PNG ou Plotly JSON. A UI da /learnings continua a usar o mesmo formato
de chart actualmente (Chart.js) — devolvemos compatibilidade via
to_chartjs_envelope().
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_ALLOWED_TYPES = ("bar", "line", "scatter", "pie", "doughnut", "heatmap", "histogram")


class ChartDataset(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: str = ""
    data: list[float | int | str | None] = Field(default_factory=list)


class ChartSpec(BaseModel):
    """Schema validado de um gráfico produzido pelo agente.

    Formato compatível com Chart.js (já em uso na UI). Pode ser convertido
    para Plotly via to_plotly().
    """
    model_config = ConfigDict(extra="allow")

    type: Literal["bar", "line", "scatter", "pie", "doughnut", "heatmap", "histogram"]
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    datasets: list[ChartDataset] = Field(default_factory=list)
    narrative: str = ""

    @field_validator("datasets")
    @classmethod
    def _at_least_one_dataset(cls, v: list[ChartDataset]) -> list[ChartDataset]:
        if not v:
            raise ValueError("Chart precisa de pelo menos 1 dataset.")
        return v

    @field_validator("labels")
    @classmethod
    def _at_least_two_labels(cls, v: list[str]) -> list[str]:
        if len(v) < 2:
            raise ValueError(
                "Chart precisa de pelo menos 2 labels — um gráfico com 1 "
                "ponto é inútil; escreve o número no texto."
            )
        return v


class ChartValidationError(ValueError):
    """Levantada quando o spec é inválido. Mensagem volta ao Qwen."""


def validate_chart(spec: dict) -> dict:
    """Devolve dict validado e canonicalizado. Levanta ChartValidationError
    com mensagem PT-PT.
    """
    if not isinstance(spec, dict):
        raise ChartValidationError("Chart spec tem de ser um objecto JSON.")
    try:
        model = ChartSpec.model_validate(spec)
    except Exception as e:
        raise ChartValidationError(str(e)) from e

    # Verificar que cada dataset tem nº de pontos consistente com labels
    n_labels = len(model.labels)
    for ds in model.datasets:
        if len(ds.data) != n_labels:
            raise ChartValidationError(
                f"Dataset '{ds.label}' tem {len(ds.data)} pontos mas "
                f"há {n_labels} labels — números têm de bater."
            )
    return model.model_dump()


def to_chartjs_envelope(spec: dict) -> dict:
    """Devolve no formato exacto que a UI da /learnings já espera (Chart.js)."""
    return {
        "title": spec.get("title", ""),
        "type": spec.get("type", "bar"),
        "labels": spec.get("labels", []),
        "datasets": [
            {"label": d.get("label", ""), "data": d.get("data", [])}
            for d in (spec.get("datasets") or [])
        ],
    }


__all__ = [
    "ChartSpec", "ChartDataset", "ChartValidationError",
    "validate_chart", "to_chartjs_envelope",
]
