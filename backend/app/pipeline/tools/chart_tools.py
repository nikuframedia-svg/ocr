"""R110.B — Tool propose_chart para o agente Qwen.

Quando o Qwen quer mostrar um gráfico, chama esta tool com um ChartSpec
JSON. Validamos, persistimos em qwen_charts, devolvemos confirmação.

A UI da /learnings já renderiza charts via Chart.js — o formato é
compatível com o que o envelope antigo devolvia.
"""
from __future__ import annotations

from typing import Any

from app.pipeline.chart_renderer import (
    ChartValidationError,
    to_chartjs_envelope,
    validate_chart,
)


# Estado por sessão — preenchido pelo qwen_agent quando começa uma chat turn.
_current_session_charts: list[dict] = []


def reset_session_charts() -> None:
    """Limpa o buffer de charts da sessão actual."""
    _current_session_charts.clear()


def get_session_charts() -> list[dict]:
    """Devolve cópia dos charts produzidos nesta sessão (para o envelope)."""
    return list(_current_session_charts)


def propose_chart(
    type: str,
    title: str,
    labels: list[str],
    datasets: list[dict],
    narrative: str = "",
) -> dict:
    """Propõe um gráfico para mostrar ao utilizador.

    O gráfico é validado: tem de ter pelo menos 2 labels (gráficos com 1
    ponto são inúteis) e todos os datasets têm de ter o mesmo número de
    pontos que labels.

    Tipos válidos: bar, line, scatter, pie, doughnut, heatmap, histogram.

    Args:
        type: Tipo do gráfico.
        title: Título mostrado em cima.
        labels: Eixos / categorias / pontos no x.
        datasets: Lista de dicts {label, data} — uma série por dataset.
        narrative: Texto curto a explicar o que mostra (PT-PT).

    Returns:
        dict {status, chart_id, message} — chart fica disponível no
        envelope final como "charts".
    """
    spec = {
        "type": type,
        "title": title,
        "labels": labels,
        "datasets": datasets,
        "narrative": narrative,
    }
    try:
        validated = validate_chart(spec)
    except ChartValidationError as e:
        return {
            "status": "error",
            "error": str(e),
            "hint": "Corrige o spec e tenta de novo. Garante que labels "
                    "tem >= 2 entradas e que datasets[*].data tem o mesmo "
                    "tamanho que labels.",
        }
    chart_js = to_chartjs_envelope(validated)
    # Anexar ao buffer da sessão. O wrapper qwen_agent pega no final.
    _current_session_charts.append({
        **chart_js,
        "narrative": validated.get("narrative", ""),
    })
    return {
        "status": "ok",
        "message": "Gráfico aceite. Vai aparecer no envelope final.",
        "index": len(_current_session_charts) - 1,
    }


CHART_TOOLS: dict[str, dict[str, Any]] = {
    "propose_chart": {
        "fn": propose_chart,
        "description": "Propõe um gráfico para acompanhar a resposta. "
                       "Validamos: precisa pelo menos 2 labels; cada dataset "
                       "tem de ter o mesmo número de pontos que labels. "
                       "Usa quando os dados ajudam mais visualmente que "
                       "em texto (>= 2 valores comparáveis).",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string",
                         "enum": ["bar", "line", "scatter", "pie", "doughnut",
                                  "heatmap", "histogram"],
                         "description": "Tipo do gráfico."},
                "title": {"type": "string",
                          "description": "Título mostrado em cima do gráfico."},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "Eixos / categorias (>= 2)."},
                "datasets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "data": {"type": "array",
                                     "items": {"type": "number"}},
                        },
                        "required": ["label", "data"],
                    },
                    "description": "Séries do gráfico. Cada uma com label + "
                                   "lista de números (mesmo tamanho que labels).",
                },
                "narrative": {"type": "string",
                              "description": "Texto curto PT-PT a explicar o "
                                             "que mostra o gráfico."},
            },
            "required": ["type", "title", "labels", "datasets"],
        },
    },
}


__all__ = ["CHART_TOOLS", "propose_chart", "reset_session_charts",
           "get_session_charts"]
