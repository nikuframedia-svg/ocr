"""Embedded LLM analyst for the /learnings page.

Talks to a local Ollama text model (``OLLAMA_TEXT_MODEL``, e.g. ``qwen3``)
via the native ``/api/chat`` endpoint with ``format='json'``.

No tool-calling — local models are unreliable at it. Instead a pre-
aggregated *data dossier* is injected into the system prompt and the
model must answer in a strict JSON envelope:

    {"reply": str, "charts": [...], "proposed_rules": [...]}

Parsing is defensive: a malformed reply degrades gracefully to plain text.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import get_settings
from app.learning import metrics, store
from app.web import attractors, db

logger = logging.getLogger(__name__)

_CHAT_TIMEOUT_S = 120.0
_MAX_HISTORY = 12


def build_data_dossier() -> dict:
    """Pre-aggregate the system state the LLM is allowed to reason over.

    Only top-N aggregates — never raw rows — so the prompt stays small.
    """
    with db.conn() as c:
        status_counts = {
            r["status"]: r["n"]
            for r in c.execute(
                "SELECT status, COUNT(*) n FROM sheets GROUP BY status"
            ).fetchall()
        }
    quarantine = store.list_proposals(status="quarantine", limit=8)
    # Trim attractors so the local model keeps focus: top 8, and at most 3
    # confusion examples each.
    attractor_rows = []
    for a in attractors.compute_attractors(top_n=8):
        attractor_rows.append({
            "scope": a["scope"],
            "label": a["label"],
            "correction_count": a["correction_count"],
            "error_rate": a["error_rate"],
            "severity": a["severity"],
            "top_confusions": a["top_confusions"][:3],
        })
    return {
        "sheets_by_status": status_counts,
        "corrections_per_sheet_latest": metrics.corrections_per_sheet(),
        "corrections_trend": metrics.corrections_trend()[-12:],
        "attractors": attractor_rows,
        "learnings_by_status": store.count_by_status(),
        "quarantine_sample": [
            {
                "id": p["id"],
                "kind": p["kind"],
                "field": p.get("field"),
                "payload": p["payload"],
                "evidence_count": p["evidence_count"],
            }
            for p in quarantine
        ],
    }


_SYSTEM_TEMPLATE = """És o analista de dados do sistema de OCR de kanbans \
da Metalogalva. Respondes SEMPRE em português de Portugal.

Tens acesso a um dossier com o estado atual do sistema (JSON abaixo). \
Usa-o para responder a perguntas, analisar atratores de erro (campos, \
operadores e templates que acumulam correções humanas) e propor regras \
de melhoria.

DOSSIER:
{dossier}

Respondes SEMPRE e SÓ com um objeto JSON com esta estrutura exata:
{{
  "reply": "texto em português para o utilizador",
  "charts": [
    {{"title": "...", "type": "bar|line|pie|doughnut",
      "labels": ["..."], "datasets": [{{"label": "...", "data": [1,2,3]}}]}}
  ],
  "proposed_rules": [
    {{"kind": "cliente_alias|modelo_alias|confusion_pair|snap_rule",
      "field": "cliente", "payload": {{}}, "reason": "porquê esta regra"}}
  ]
}}

Regras:
- "reply" é obrigatório e é texto simples — SEM HTML, SEM markdown, sem tags.
- "charts" e "proposed_rules" podem ser listas vazias [].
- Inclui "charts" só quando um gráfico ajuda mesmo, e NUNCA com menos de 2
  valores (um gráfico de 1 barra é inútil — mete o número no texto).
- Propõe regras só quando há evidência clara nos atratores.
- confusion_pair é SÓ para UM caractere mal lido: "gold_char" e "ocr_char"
  têm EXATAMENTE 1 caractere. Ex: {{"gold_char": "0", "ocr_char": "O"}}.
- Para corrigir um VALOR INTEIRO (um modelo, cliente ou operador errado) usa
  modelo_alias / cliente_alias / operador_alias com payload
  {{"from": "VALOR ERRADO", "to": "VALOR CERTO"}}. NUNCA metas um valor
  inteiro num confusion_pair.
- Não inventes dados que não estejam no dossier."""


def _envelope_fallback(text: str) -> dict:
    return {"reply": text, "charts": [], "proposed_rules": []}


def _sanitize_rules(rules: list) -> list:
    """Drop malformed proposed rules. A confusion_pair is strictly a
    single-character substitution — the LLM sometimes stuffs a whole
    misread value in there; those belong in an alias, so we drop them."""
    out: list = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        payload = r.get("payload")
        if not isinstance(payload, dict):
            continue
        if r.get("kind") == "confusion_pair":
            gc = str(payload.get("gold_char") or "")
            oc = str(payload.get("ocr_char") or "")
            if len(gc) != 1 or len(oc) != 1:
                continue
        out.append(r)
    return out


def chat(user_message: str, history: list[dict] | None = None) -> dict:
    """Run one chat turn. Always returns an envelope dict with keys
    ``reply``, ``charts`` and ``proposed_rules``."""
    settings = get_settings()
    dossier = json.dumps(build_data_dossier(), ensure_ascii=False, default=str)
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(dossier=dossier)}
    ]
    for m in (history or [])[-_MAX_HISTORY:]:
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})
    messages.append({"role": "user", "content": user_message})

    try:
        raw = _call_ollama(messages, settings)
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.warning("ollama chat failed: %s", e)
        return _envelope_fallback(
            "O assistente está indisponível — não consegui contactar o "
            "Ollama local. Confirma que o serviço está a correr e que "
            f"o modelo '{settings.ollama_text_model}' foi descarregado."
        )
    return _parse_envelope(raw)


def _call_ollama(messages: list[dict], settings) -> str:
    url = str(settings.ollama_url).rstrip("/") + "/api/chat"
    payload = {
        "model": settings.ollama_text_model,
        "messages": messages,
        "stream": False,
        "format": "json",
        # Disable reasoning blocks — same rationale as the OCR's
        # OCR_NO_THINK=1: with thinking on, a qwen3.x chat call blows past
        # the 120s timeout; off, it answers in ~2s with clean JSON.
        "think": False,
        "options": {"temperature": 0.2},
    }
    with httpx.Client(timeout=_CHAT_TIMEOUT_S) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return (data.get("message") or {}).get("content") or ""


def _parse_envelope(raw: str) -> dict:
    """Parse the model's JSON envelope; degrade to plain text on failure."""
    raw = (raw or "").strip()
    if not raw:
        return _envelope_fallback("O assistente devolveu uma resposta vazia.")
    obj: object
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return _envelope_fallback(raw)
        else:
            return _envelope_fallback(raw)
    if not isinstance(obj, dict):
        return _envelope_fallback(str(obj))
    charts = obj.get("charts")
    rules = obj.get("proposed_rules")
    # Strip any HTML tags the model may have slipped into the reply.
    reply = re.sub(r"<[^>]+>", "", str(obj.get("reply") or "")).strip()
    return {
        "reply": reply,
        "charts": charts if isinstance(charts, list) else [],
        "proposed_rules": _sanitize_rules(rules) if isinstance(rules, list) else [],
    }
