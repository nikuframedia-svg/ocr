"""R110.A — Wrapper Qwen como agente com function calling local.

Conversa com o Qwen 3.5:9b via Ollama /api/chat, invocando tools quando
o modelo decide chamá-las. Devolve sempre o envelope canónico esperado
pela UI da página /learnings (aba LLM):

    {"reply": str, "charts": [...], "proposed_rules": [...]}

Harness (Samchon pattern):
- Tools ≤ 8 por sessão (Qwen confunde-se com mais).
- Schemas Pydantic + JSON schema strict.
- Quando uma tool call falha schema, devolve mensagem de erro estruturada
  ao modelo (ele tenta de novo dentro da mesma sessão).
- Máximo 6 rondas de tool calls antes de forçar resposta final.

Fallback: se o Ollama não suportar tool_calling ou houver erro de
conexão, devolve envelope vazio com erro descritivo.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import get_settings
from app.pipeline.tools import (
    ALL_TOOLS,
    collect_session_outputs,
    reset_session_buffers,
)


logger = logging.getLogger(__name__)

_CHAT_TIMEOUT_S = 180.0
_MAX_TOOL_ROUNDS = 6
_MAX_HISTORY = 16


_SYSTEM_PROMPT = """És o analista interno do sistema de OCR de kanbans da Metalogalva.
Respondes SEMPRE em português de Portugal.

TENS ACESSO A 3 FAMÍLIAS DE FERRAMENTAS:

LEITURA (consulta a base de dados real — não inventes números):
- query_db(sql): SELECT contra sheets, edits, learnings, etc.
- get_sheet(sheet_id): detalhe completo de uma folha.
- list_recent_edits(n_days): edições humanas/sistema recentes.
- list_templates(): schemas dos templates (bobine, quinadora, etc.).
- get_refs_summary(): stats dos refs SAP/plan.
- query_learnings(status, kind): proposals do motor R98.

GRÁFICOS (quando os dados ajudam mais visualmente):
- propose_chart(type, title, labels, datasets, narrative): cria gráfico
  Chart.js. Tipos: bar, line, scatter, pie, doughnut, histogram.
  Precisa pelo menos 2 labels. Usa só quando ajuda mesmo.

PROPOSTAS DE MELHORIA (quando vês um padrão concreto):
- propose_rule(kind, payload, ...): regra de snap (alias, confusion_pair,
  snap_rule). Vai para a quarentena ou auto-aplica se for alias com
  evidência forte.
- propose_template_change(template_name, change_type, field_name, ...):
  adicionar/remover coluna a um template (ex: adicionar OV ao
  quinadora_pav8 se vês 23% das folhas a ter OV preenchido).
- propose_cpis_change(scope, change_type, column_name, ...): mudança
  ao formato CPIS de exportação.

ESTRATÉGIA:
1. Pensa que dados precisas.
2. Invoca tools de leitura para confirmar.
3. Se ajuda visualmente, propose_chart.
4. Se vês padrão concreto, propose_rule/template_change/cpis_change.
5. Responde em PT-PT com reply em markdown.

A RESPOSTA FINAL (sem mais tool calls) deve ser texto markdown PT-PT
ou um objecto JSON {reply, charts, proposed_rules}. Se usaste
propose_chart ou propose_rule, eles já foram registados — não precisas
de os repetir no JSON final, só foca o reply explicativo.

REGRAS DURAS:
- Não inventes OFs, OVs, lotes nem clientes — confirma sempre via query_db.
- confusion_pair é SÓ para 1 carácter mal lido (ex: '0' por 'O').
  Para valor inteiro errado (modelo, cliente) usa alias.
- Charts com 1 ponto são inúteis — escreve o número no texto.
- Numa proposta indica evidence_sheet_ids quando aplicável.
"""


def _build_ollama_tools_spec() -> list[dict]:
    """Converte ALL_TOOLS para o formato esperado pelo Ollama /api/chat."""
    out = []
    for name, info in ALL_TOOLS.items():
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"],
            },
        })
    return out


def _execute_tool(name: str, arguments: dict) -> dict:
    """Invoca uma tool pelo nome. Devolve sempre dict serializável."""
    if name not in ALL_TOOLS:
        return {"status": "error", "error": f"Tool desconhecida: {name}"}
    fn = ALL_TOOLS[name]["fn"]
    try:
        return fn(**(arguments or {}))
    except TypeError as e:
        return {"status": "error",
                "error": f"Argumentos inválidos para {name}: {e}"}
    except Exception as e:
        return {"status": "error",
                "error": f"Falhou ao executar {name}: {e}"}


def _parse_envelope(content: str) -> dict:
    """Tenta extrair envelope {reply, charts, proposed_rules} de uma
    resposta texto-livre do modelo. Fallback gracioso."""
    if not content:
        return {"reply": "", "charts": [], "proposed_rules": []}

    # Pode vir com texto à volta — tentar localizar primeiro `{`
    s = content.strip()
    if s.startswith("```"):
        s = s.lstrip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip().rstrip("`").strip()

    try:
        env = json.loads(s)
        if not isinstance(env, dict):
            raise ValueError("not a dict")
        return {
            "reply": str(env.get("reply", "")),
            "charts": env.get("charts", []) if isinstance(env.get("charts"), list) else [],
            "proposed_rules": env.get("proposed_rules", []) if isinstance(env.get("proposed_rules"), list) else [],
        }
    except (json.JSONDecodeError, ValueError):
        # Fallback: trata tudo como reply em texto
        return {"reply": content.strip(), "charts": [], "proposed_rules": []}


def chat(
    message: str,
    history: list[dict] | None = None,
    model: str | None = None,
) -> dict:
    """Uma volta de conversa com o agente Qwen.

    Args:
        message: pergunta/comando do utilizador em PT.
        history: lista de turns prévios [{role, content}], opcional.
        model: override do OLLAMA_TEXT_MODEL.

    Returns:
        Envelope {reply, charts, proposed_rules, _meta} (já parsed).
        _meta inclui tool_calls_count, duration_ms, status.
    """
    started = time.perf_counter()
    settings = get_settings()
    base = settings.ollama_url.rstrip("/")
    model_name = model or settings.ollama_text_model

    # Reset dos buffers de charts + proposals desta sessão.
    reset_session_buffers()

    # Construir histórico
    msgs: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if history:
        msgs.extend(history[-_MAX_HISTORY:])
    msgs.append({"role": "user", "content": message})

    tools_spec = _build_ollama_tools_spec()
    tool_calls_count = 0
    tools_used: list[str] = []

    try:
        with httpx.Client(timeout=_CHAT_TIMEOUT_S) as client:
            for round_idx in range(_MAX_TOOL_ROUNDS):
                # R116 — "think": False evita reasoning blocks que rebentam o
                # timeout em qwen3.x (mesmo racional de llm_assistant.py:405-408).
                payload = {
                    "model": model_name,
                    "messages": msgs,
                    "tools": tools_spec,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.2,
                    },
                }
                r = client.post(f"{base}/api/chat", json=payload)
                r.raise_for_status()
                data = r.json()
                resp_msg = data.get("message") or {}
                tool_calls = resp_msg.get("tool_calls") or []

                if not tool_calls:
                    # Resposta final — parse envelope
                    content = resp_msg.get("content") or ""
                    env = _parse_envelope(content)
                    # Sobrepor com charts/proposals que o agente colocou via tools
                    session_outputs = collect_session_outputs()
                    if session_outputs.get("charts"):
                        env["charts"] = (env.get("charts") or []) + session_outputs["charts"]
                    if session_outputs.get("proposed_rules"):
                        env["proposed_rules"] = (env.get("proposed_rules") or []) + session_outputs["proposed_rules"]
                    env["_meta"] = {
                        "tool_calls_count": tool_calls_count,
                        "tools_used": tools_used,
                        "rounds": round_idx + 1,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "status": "ok",
                    }
                    return env

                # Adicionar o turno do assistant (com tool calls) ao histórico
                msgs.append({
                    "role": "assistant",
                    "content": resp_msg.get("content", ""),
                    "tool_calls": tool_calls,
                })

                # Executar cada tool call e adicionar resultados
                for call in tool_calls:
                    fn_obj = call.get("function") or {}
                    name = fn_obj.get("name", "")
                    args = fn_obj.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    tools_used.append(name)
                    tool_calls_count += 1
                    result = _execute_tool(name, args)
                    msgs.append({
                        "role": "tool",
                        "content": json.dumps(result, ensure_ascii=False, default=str)[:8000],
                    })

            # Excedeu rounds — força resposta final pedindo ao modelo
            msgs.append({
                "role": "user",
                "content": ("Atingiste o limite de tool calls. Responde agora "
                            "com o envelope JSON final usando os dados que já "
                            "recolheste."),
            })
            payload = {
                "model": model_name,
                "messages": msgs,
                "stream": False,
                "think": False,  # R116
                "options": {"temperature": 0.1},
                "format": "json",
            }
            r = client.post(f"{base}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            content = (data.get("message") or {}).get("content") or ""
            env = _parse_envelope(content)
            session_outputs = collect_session_outputs()
            if session_outputs.get("charts"):
                env["charts"] = (env.get("charts") or []) + session_outputs["charts"]
            if session_outputs.get("proposed_rules"):
                env["proposed_rules"] = (env.get("proposed_rules") or []) + session_outputs["proposed_rules"]
            env["_meta"] = {
                "tool_calls_count": tool_calls_count,
                "tools_used": tools_used,
                "rounds": _MAX_TOOL_ROUNDS,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "max_rounds_reached",
            }
            return env

    except httpx.HTTPError as e:
        logger.warning("Qwen agent HTTP error: %s", e)
        return {
            "reply": f"Erro de comunicação com o Ollama: {e}",
            "charts": [],
            "proposed_rules": [],
            "_meta": {
                "tool_calls_count": tool_calls_count,
                "tools_used": tools_used,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "error",
                "error": str(e),
            },
        }
    except Exception as e:
        logger.exception("Qwen agent unexpected error")
        return {
            "reply": f"Erro inesperado no agente: {e}",
            "charts": [],
            "proposed_rules": [],
            "_meta": {
                "tool_calls_count": tool_calls_count,
                "tools_used": tools_used,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "status": "error",
                "error": str(e),
            },
        }


__all__ = ["chat"]
