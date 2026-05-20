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
from app.cross_check import storage as cc_storage
from app.cross_check.ref_watcher import get_watcher
from app.learning import metrics, store
from app.web import attractors, db

logger = logging.getLogger(__name__)

_CHAT_TIMEOUT_S = 120.0
_MAX_HISTORY = 12


# A bare run of 4-7 digits — candidate OF or OV. Membership against the
# real plan decides which (or neither). Lotes: NNB... with optional prefix.
_NUM_TOKEN_RE = re.compile(r"\b\d{4,7}\b")
_LOTE_TOKEN_RE = re.compile(r"\b[A-Za-z]?\d{2}[Bb]\d{1,6}\b")


def _get_refs() -> dict:
    """Live SAP+plan refs, or {} on any failure (Excel parse, missing
    file). Never raises into the chat path."""
    try:
        return get_watcher().get_refs() or {}
    except Exception:  # ref watcher / openpyxl — optional context
        logger.warning("ref watcher unavailable for LLM dossier", exc_info=True)
        return {}


def _plan_db_stats(refs: dict) -> dict:
    """Top-level shape of the SAP+plan database — so the LLM knows the
    data exists and how big it is, even before any specific lookup."""
    stats = refs.get("stats") or {}
    clientes = sorted(refs.get("clientes_plan") or [])
    return {
        "n_ofs": stats.get("n_ofs", 0),
        "n_ovs": stats.get("n_ovs", 0),
        "n_clientes": stats.get("n_clientes", 0),
        "n_lotes_stocksap": stats.get("n_lotes", 0),
        "n_linhas_plan": stats.get("n_plan_rows", 0),
        "n_modelos": stats.get("n_modelos_fts", 0),
        "clientes_amostra": clientes[:30],
        "carregado_em": refs.get("loaded_at"),
    }


def _lookup_refs(user_message: str, refs: dict) -> dict:
    """Resolve OF / OV / lote / cliente identifiers mentioned in the
    user's message against the real plan_colunas + StockSAP data.

    The local model can't tool-call, so instead of giving it the whole
    10k-OF database we inject only the records it actually asked about.
    Returns {} when nothing matches."""
    msg = user_message or ""
    of_to_entries: dict = refs.get("of_to_entries") or {}
    of_to_ovs: dict = refs.get("of_to_ovs") or {}
    ovs_plan = refs.get("ovs_plan") or frozenset()
    lotes_full: dict = refs.get("lotes_sap_full") or {}
    plan_by_cliente: dict = refs.get("plan_by_cliente") or {}

    ofs_out: list = []
    ovs_out: list = []
    lotes_out: list = []
    clientes_out: list = []

    for tok in sorted(set(_NUM_TOKEN_RE.findall(msg))):
        if tok in of_to_entries and len(ofs_out) < 6:
            entries = of_to_entries[tok]
            ofs_out.append({
                "of": tok,
                "ovs": sorted(of_to_ovs.get(tok, [])),
                "n_linhas_plan": len(entries),
                "linhas": entries[:8],
            })
        if tok in ovs_plan and len(ovs_out) < 4:
            ofs_for = sorted(
                of for of, ovset in of_to_ovs.items() if tok in ovset)
            ovs_out.append({"ov": tok, "ofs": ofs_for[:25]})

    for tok in sorted(set(_LOTE_TOKEN_RE.findall(msg))):
        key = tok.upper()
        rec = lotes_full.get(key) or lotes_full.get(key.lstrip("M"))
        if rec and len(lotes_out) < 6:
            lotes_out.append({"lote": key, **rec})

    msg_up = msg.upper()
    for cli in sorted(plan_by_cliente):
        if len(cli) >= 4 and cli in msg_up and len(clientes_out) < 3:
            entries = plan_by_cliente[cli]
            ofs_cli = sorted({e.get("_of", "") for e in entries if e.get("_of")})
            clientes_out.append({
                "cliente": cli,
                "n_linhas_plan": len(entries),
                "ofs_amostra": ofs_cli[:25],
            })

    out = {
        "ofs": ofs_out, "ovs": ovs_out,
        "lotes": lotes_out, "clientes": clientes_out,
    }
    return {k: v for k, v in out.items() if v}


def _cross_check_dossier(user_message: str = "") -> dict:
    """Real cross-check + SAP/plan facts the assistant must reason over:
    how accurate the OCR actually is, the NO_MATCH inbox, the shape of
    the plan database, and the specific OF/OV/lote/cliente records the
    user asked about. Every part degrades to absent on failure (no 500s).
    """
    out: dict = {}
    try:
        summary = cc_storage.load_summary()
        totals = summary.get("totals", {}) or {}
        m = totals.get("match", 0) or 0
        nm = totals.get("no_match", 0) or 0
        denom = m + nm
        out["totals"] = {"match": m, "no_match": nm, "na": totals.get("na", 0)}
        out["match_rate"] = round(m / denom, 4) if denom else None
        out["n_sheets"] = summary.get("n_sheets", 0)
        by_op: dict[str, float] = {}
        for op, t in (summary.get("by_operador") or {}).items():
            d = (t.get("match", 0) or 0) + (t.get("no_match", 0) or 0)
            if d:
                by_op[op] = round((t.get("no_match", 0) or 0) / d, 4)
        out["error_rate_by_operador"] = by_op
    except OSError:
        pass
    try:
        inbox = cc_storage.load_to_analisar(limit=15)
        out["no_match_total"] = inbox.get("total", 0)
        out["no_match_sample"] = [
            {
                "sheet_id": it.get("sheet_id"),
                "field": it.get("field"),
                "ocr_value": it.get("value"),
                "sap_value": it.get("plan_value"),
                "reason": it.get("reason"),
            }
            for it in inbox.get("items", [])
        ]
    except OSError:
        pass
    refs = _get_refs()
    if refs:
        out["plan_db"] = _plan_db_stats(refs)
        consultas = _lookup_refs(user_message, refs)
        if consultas:
            out["consultas"] = consultas
    return out


def build_data_dossier(user_message: str = "") -> dict:
    """Pre-aggregate the system state the LLM is allowed to reason over.

    Mostly top-N aggregates so the prompt stays small; the SAP/plan part
    is resolved on demand from ``user_message`` (only the OFs/OVs/lotes
    the user named are injected — never the whole 10k-OF database).
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
            "cc_error_rate": a["cc_error_rate"],
            "cc_match": a["cc_match"],
            "cc_no_match": a["cc_no_match"],
            "review_rate": a["review_rate"],
            "severity": a["severity"],
            "top_confusions": a["top_confusions"][:3],
        })
    return {
        "sheets_by_status": status_counts,
        "cross_check": _cross_check_dossier(user_message),
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

DADOS REAIS — como ler o dossier:
- A EXATIDÃO REAL do OCR está em "cross_check": "match_rate" é a taxa de
  acerto (célula a célula contra as refs SAP). A taxa de erro real é
  1 − match_rate (tipicamente <1%). "no_match_sample" traz as células que
  o OCR leu mal — com o valor lido ("ocr_value") e o valor certo do plano
  SAP ("sap_value").
- "cross_check.plan_db" é a BD REAL de fabrico, minerada dos ficheiros
  plan_colunas_cpis.xlsx e StockSAP.xlsx: número de OFs, OVs, clientes,
  lotes e linhas do plano, com uma amostra de clientes.
- "cross_check.consultas" traz os REGISTOS REAIS das OFs/OVs/lotes/clientes
  que o utilizador mencionou na pergunta: "ofs" (linhas do plano dessa OF —
  cliente, ov, designação, esp, comprimentos, material), "ovs" (que OFs
  têm essa OV), "lotes" (qtd/esp/larg do StockSAP), "clientes" (linhas e
  OFs desse cliente). Responde SEMPRE a partir destes dados — NUNCA
  inventes OFs, OVs ou lotes. Se o utilizador perguntar por uma OF/OV/lote
  e ela NÃO estiver em "consultas", diz que não existe no plano/StockSAP.
- Nos "attractors", "cc_error_rate" é a taxa de erro REAL desse campo/
  template/operador (do cross-check). "review_rate" é a fração de folhas
  que precisaram de UM retoque humano — é CARGA DE REVISÃO, NÃO erro.
  NUNCA digas "100% de erro" a partir do "review_rate": um campo pode ter
  review_rate alto e mesmo assim ter o OCR quase sempre certo. Quando
  falares de qualidade/erro usa SEMPRE o cross-check (match_rate /
  cc_error_rate).

Regras:
- "reply" é OBRIGATÓRIO e traz SEMPRE uma explicação clara (2 a 5 frases) —
  mesmo quando devolves um gráfico ou uma regra, explicas o que estás a
  mostrar e porquê.
- "reply" é escrito em **Markdown** (português de Portugal): parágrafos
  curtos, **negrito** nos números e nomes-chave, listas com "- " quando
  enumeras. NÃO uses HTML nem tags. NÃO uses cabeçalhos maiores que "###".
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
    ``reply``, ``charts`` and ``proposed_rules``.

    R110 — entry point oficial: tenta o agente com function calling
    (qwen_agent.chat) e, se falhar (Ollama sem tool support, erro de
    schema persistente), faz fallback para o caminho legacy com data
    dossier pré-agregado.

    Persiste sempre a sessão em qwen_sessions para audit.
    """
    use_tools = True  # Pode passar a env var no futuro
    envelope: dict | None = None
    used_path = "tools"

    if use_tools:
        try:
            from app.pipeline.qwen_agent import chat as qwen_chat
            envelope = qwen_chat(user_message, history=history)
            # Considera falha se o envelope vem com status error e reply vazia
            meta = envelope.get("_meta") or {}
            if meta.get("status") == "error" and not (envelope.get("reply") or "").strip():
                envelope = None
                used_path = "fallback_after_tool_error"
        except Exception:  # noqa: BLE001
            logger.exception("qwen_agent.chat failed; falling back to legacy")
            envelope = None
            used_path = "fallback_after_exception"

    if envelope is None:
        # Legacy path — data dossier pré-agregado, sem tools
        envelope = _chat_legacy(user_message, history)
        envelope["_meta"] = {
            **(envelope.get("_meta") or {}),
            "status": "ok",
            "path": used_path,
        }
    else:
        envelope.setdefault("_meta", {})
        envelope["_meta"]["path"] = used_path
        # Sanitizar proposed_rules com a mesma lógica do path legacy
        envelope["proposed_rules"] = _sanitize_rules(
            envelope.get("proposed_rules") or []
        )

    # Persistir sessão (best effort)
    try:
        db.save_qwen_session(user_message, envelope, trigger="manual")
    except Exception:  # noqa: BLE001
        logger.exception("save_qwen_session failed")

    return envelope


def _chat_legacy(user_message: str, history: list[dict] | None) -> dict:
    """Caminho legacy R98 — data dossier pré-agregado, sem tools.

    Usado como fallback quando o agente com tools falha ou quando a versão
    do Ollama não suporta tool calling.
    """
    settings = get_settings()
    dossier = json.dumps(
        build_data_dossier(user_message), ensure_ascii=False, default=str)
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
    # Strip real HTML tags the model may slip in (it should send Markdown).
    # The pattern matches only tag-like spans so a stray "<" in maths text
    # (e.g. "comp < 500mm") is left untouched.
    reply = re.sub(r"</?[a-zA-Z][^>]*>", "", str(obj.get("reply") or "")).strip()
    return {
        "reply": reply,
        "charts": charts if isinstance(charts, list) else [],
        "proposed_rules": _sanitize_rules(rules) if isinstance(rules, list) else [],
    }
