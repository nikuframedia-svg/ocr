"""R246 — Descodificação ATIVA: a "retransmissão" de Shannon, literal.

Quando o posterior de uma célula fica na zona cinzenta (nem confiante nem
claramente errado) e há DUAS hipóteses concretas (ex.: a OF escrita vs a OF
inferida), em vez de escalar logo para um humano o descodificador re-lê o
CROP da região e faz uma pergunta DISCRIMINATIVA ao VLM ("está escrito
262107 ou 262109? responde só o número") — a discriminação binária é muito
mais fiável do que a transcrição livre.

Estado: mecanismo completo, DESLIGADO por flag (``CROSS_ACTIVE_REREAD``).
Antes de ligar é preciso CALIBRAR a fiabilidade do re-read com ~50 casos do
golden set COM imagem (na fábrica — as fotos vivem lá): a resposta entra
como evidência com a log-razão medida, não como verdade absoluta. O
orçamento é limitado (máx. 3 re-reads/folha) para não pesar o hot path.

Reutiliza a infra de crops da raiz (``ocr6_crops.split_kanban`` divide a
folha em regiões; a pergunta indica a linha/coluna).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_REREADS_PER_SHEET = 3
# Zona cinzenta do posterior que justifica um re-read: abaixo do limiar de
# gravação mas acima do "claramente não sei" (aí é revisão humana direta).
GREY_ZONE = (0.50, 0.95)

_PROMPT = (
    "Olha para a linha {row} da tabela nesta folha kanban, coluna {field}. "
    "O valor manuscrito é '{a}' ou '{b}'? Responde APENAS com o valor exato, "
    "sem mais texto."
)


@dataclass
class RereadResult:
    row_index: int
    field: str
    options: tuple[str, str]
    answer: str | None       # opção escolhida pelo VLM, ou None (ilegível)
    raw_response: str
    duration_ms: int


def _normalize_answer(text: str, options: tuple[str, str]) -> str | None:
    """Extrai qual das duas opções o VLM escolheu, tolerando texto à volta
    ("é o 262109." → 262109). Uma opção casa se o seu compacto alfanumérico
    aparece na resposta compacta; havendo empate (ex.: 100 ⊂ 1000) decide a
    igualdade exata. Sem match único → 'não sei' (nunca inventa)."""
    resp = re.sub(r"[^A-Z0-9]+", "", str(text or "").upper())
    if not resp:
        return None
    opt_c = {o: re.sub(r"[^A-Z0-9]+", "", o.upper()) for o in options}
    substr = [o for o, c in opt_c.items() if c and c in resp]
    if len(substr) == 1:
        return substr[0]
    if len(substr) > 1:  # opções encaixadas — a igualdade exata decide
        exact = [o for o in substr if opt_c[o] == resp]
        return exact[0] if len(exact) == 1 else None
    return None


def discriminative_reread(
    image_path: str | Path,
    row_index: int,
    field: str,
    options: tuple[str, str],
) -> RereadResult | None:
    """Re-lê a região das linhas e pergunta A-ou-B. Devolve None quando a
    infra não está disponível (sem imagem, sem Ollama) — o chamador trata
    como 'sem evidência nova' e segue para revisão humana."""
    import time

    path = Path(image_path)
    if not path.exists():
        return None
    started = time.perf_counter()
    try:
        # Reuso da infra de crops da raiz do repo (Fase 3 do roadmap).
        import sys

        root = Path(__file__).resolve().parents[3]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from PIL import Image

        import ocr6_crops as crops

        rgb = Image.open(path).convert("RGB")
        _header, rows_img, _footer = crops.split_kanban(path)
        _ = rgb
        prompt = _PROMPT.format(row=row_index + 1, field=field.upper(),
                                a=options[0], b=options[1])
        b64 = crops.encode_image(rows_img, max_edge=1280)
        text, _meta = crops.ollama_request(prompt, b64)
        answer = _normalize_answer(text or "", options)
        return RereadResult(
            row_index=row_index, field=field, options=options,
            answer=answer, raw_response=str(text or "")[:200],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception:  # noqa: BLE001 — sensing ativo é sempre opcional
        logger.exception("active reread falhou (%s row=%s %s)",
                         path.name, row_index, field)
        return None


def candidates_for_reread(cross_result: dict) -> list[dict]:
    """Seleciona as células que justificam re-read: very_different com
    confiança na zona cinzenta e duas hipóteses concretas (valor OCR original
    vs proposta do winner), ordenadas por prioridade de revisão. Máx. 3."""
    out: list[dict] = []
    for item in (cross_result.get("to_analisar") or []):
        if item.get("section") != "rows" and "rows[" not in str(item.get("field_path")):
            continue
        conf = item.get("decision_confidence")
        if conf is None or not (GREY_ZONE[0] <= float(conf) < GREY_ZONE[1]):
            continue
        written = str(item.get("value") or "").strip()
        proposed = str(item.get("ref") or "").strip()
        if not written or not proposed or written == proposed:
            continue
        out.append({
            "row_index": item.get("row_index"),
            "field": item.get("field"),
            "options": (written, proposed),
            "priority": float(item.get("review_priority") or 0.0),
        })
    out.sort(key=lambda it: -it["priority"])
    return out[:MAX_REREADS_PER_SHEET]
