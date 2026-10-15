"""Vision transport for server deployments; Ollama remains the local default.

Keys are read from the process environment, never included in diagnostics.
One call means one HTTP request: the existing OCR pipeline owns retry policy.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


def provider() -> str:
    return os.environ.get("OCR_PROVIDER", "ollama").strip().lower() or "ollama"


def configuration_status() -> dict[str, Any]:
    kind = provider()
    return {
        "provider": kind,
        "model": os.environ.get("OCR_MODEL", "qwen3.5:9b"),
        "key_configured": bool(_api_key()) if kind != "ollama" else None,
    }


def _api_key() -> str:
    return (os.environ.get("OCR_API_KEY")
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()


def _failure(message: str, *, retryable: bool = False, **extra: Any):
    return None, {"error": message, "retryable": retryable, **extra}


def request_image(image_b64: str, prompt: str, *, model: str) -> tuple[str | None, dict]:
    """Send the same OCR prompt/image through the configured vision API.

    Protocols: bedrock (InvokeModel), anthropic (Messages), chat (OpenAI
    Chat Completions, including Grok), responses (OpenAI Responses).
    OCR_API_BASE_URL includes /openai/v1 or /anthropic/v1 when required.
    """
    kind = provider()
    key = _api_key()
    base = os.environ.get("OCR_API_BASE_URL", "").rstrip("/")
    if kind not in {"bedrock", "anthropic", "chat", "responses"}:
        return _failure("Fornecedor OCR remoto desconhecido.")
    if not key or not base:
        return _failure("Falta configurar a chave ou o endereço da API OCR.")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query:
        return _failure("O endereço da API OCR deve usar HTTPS, sem credenciais ou query.")
    try:
        timeout = float(os.environ.get("OCR_API_TIMEOUT_S", "180"))
        limit = int(os.environ.get("OCR_API_MAX_TOKENS", "8192"))
        if timeout <= 0 or limit <= 0:
            raise ValueError
    except ValueError:
        return _failure("Timeout ou limite de tokens OCR inválido.")
    headers = {"Content-Type": "application/json"}
    image = {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg", "data": image_b64,
    }}
    if kind in {"bedrock", "anthropic"}:
        payload = {"max_tokens": limit, "messages": [{
            "role": "user", "content": [image, {"type": "text", "text": prompt}],
        }]}
        if kind == "bedrock":
            url = f"{base}/model/{quote(model, safe='')}/invoke"
            payload["anthropic_version"] = "bedrock-2023-05-31"
            headers["Authorization"] = f"Bearer {key}"
        else:
            url = f"{base}/messages"
            payload["model"] = model
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {key}"
        image_url = f"data:image/jpeg;base64,{image_b64}"
        effort = os.environ.get("OCR_REASONING_EFFORT", "").strip()
        if kind == "chat":
            url = f"{base}/chat/completions"
            payload = {"model": model, "max_tokens": limit, "stream": False,
                       "messages": [{"role": "user", "content": [
                           {"type": "text", "text": prompt},
                           {"type": "image_url", "image_url": {"url": image_url}},
                       ]}]}
            if effort:
                payload["reasoning_effort"] = effort
        else:
            url = f"{base}/responses"
            payload = {"model": model, "max_output_tokens": limit, "store": False,
                       "input": [{"role": "user", "content": [
                           {"type": "input_text", "text": prompt},
                           {"type": "input_image", "image_url": image_url},
                       ]}]}
            if effort:
                payload["reasoning"] = {"effort": effort}
    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(url, headers=headers, json=payload)
        if not response.is_success:
            # Do not store vendor bodies: they may echo credentials or input.
            status = response.status_code
            daily_quota = status == 429 and "tokens per day" in response.text.lower()
            reason = ("Limite diário de tokens da API atingido." if daily_quota else
                      "Chave ou acesso ao modelo recusado." if status in {401, 403} else
                      "A API OCR recusou o pedido.")
            return _failure(f"{reason} HTTP {status}.", status_code=status,
                            retryable=(status == 429 or status >= 500) and not daily_quota)
        data = response.json()
        usage = data.get("usage") or {}
        finish = None
        if kind in {"bedrock", "anthropic"}:
            text = "".join(x.get("text", "") for x in data.get("content", [])
                           if x.get("type") == "text")
            finish = data.get("stop_reason")
        elif kind == "chat":
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            text = content if isinstance(content, str) else "".join(
                x.get("text", "") for x in (content or []) if x.get("type") == "text")
            finish = choice.get("finish_reason")
        else:
            text = "".join(part.get("text", "") for item in data.get("output", [])
                           for part in item.get("content", [])
                           if part.get("type") == "output_text")
            finish = data.get("status")
        if finish in {"length", "max_tokens", "incomplete"}:
            return _failure("A resposta OCR foi cortada pelo limite de tokens.")
        if finish == "refusal":
            return _failure("A API recusou a leitura desta imagem.")
        if not text.strip():
            return _failure("A API OCR não devolveu texto.", retryable=True)
        return text.strip(), {
            "eval_count": usage.get("output_tokens", usage.get("completion_tokens", 0)),
            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            "total_duration_ms": round((time.monotonic() - start) * 1000),
            "eval_duration_ms": 0, "provider": kind, "model": model,
        }
    except httpx.TimeoutException:
        return _failure("Tempo de espera da API OCR excedido.", retryable=True)
    except httpx.RequestError:
        return _failure("Não foi possível contactar a API OCR.", retryable=True)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return _failure("A API OCR devolveu uma resposta inválida.", retryable=True)
