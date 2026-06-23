"""Application settings loaded from environment / .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the OCR pipeline.

    Values are loaded from environment variables (and a local ``.env`` file
    when present). All fields that talk to external services live here so
    the pipeline modules never read ``os.environ`` directly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    vllm_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:11434/v1"),
        description=(
            "OpenAI-compatible base URL. Defaults to Ollama (:11434/v1) "
            "because vLLM 0.20.x in WSL2+Blackwell still hits a silent "
            "30-50 s self-SIGINT we couldn't trace. Switch to :8000/v1 "
            "when vLLM is stabilised; the rest of the client is identical."
        ),
    )
    vllm_model: str = Field(
        default="qwen3.5:9b",
        description=(
            "Model name as known to the inference engine. Defaults to the "
            "same Ollama vision model used by the OCR path. For a true vLLM "
            "deployment, override VLLM_MODEL with the server's model id."
        ),
    )
    vllm_timeout_s: float = Field(
        default=600.0,
        gt=0,
        description="HTTP timeout for a single vLLM request, in seconds.",
    )
    guided_decoding_enabled: bool = Field(
        default=False,
        description=(
            "Send `extra_body.guided_json` to constrain output via "
            "llguidance. Only vLLM supports this; Ollama errors. "
            "Default off to match the default Ollama endpoint."
        ),
    )
    json_mode_enabled: bool = Field(
        default=False,
        description=(
            "Send `extra_body.format=\"json\"` (Ollama-native JSON mode). "
            "Lighter than llguidance, but in 2026-04 testing on Ollama "
            "0.20 + qwen2.5vl:3b it crashed the runner with 500 just "
            "like `response_format`. Default off until Ollama ships a "
            "newer Qwen2.5-VL build with stable constrained sampling."
        ),
    )
    vllm_max_retries: int = Field(
        default=3,
        ge=0,
        description="Number of retry attempts on 5xx / timeout before giving up.",
    )
    hf_token: str | None = Field(
        default=None,
        description="Hugging Face token. Only required for gated models.",
    )

    debug: bool = Field(
        default=False,
        description="When true, structlog renders pretty console output.",
    )
    log_level: str = Field(
        default="INFO",
        description="Log level threshold (DEBUG, INFO, WARNING, ERROR).",
    )

    ollama_url: str = Field(
        default="http://localhost:11434",
        description=(
            "Base URL of the Ollama server (no /v1 suffix). Used by the "
            "/learnings LLM analyst via the native /api/chat endpoint. "
            "Reads the OLLAMA_URL env var — the same server as the OCR."
        ),
    )
    ollama_text_model: str = Field(
        default="qwen3.5:9b",
        description=(
            "Text model for the /learnings LLM analyst (chat, attractor "
            "analysis, reports). Defaults to qwen3.5:9b and is intentionally "
            "separate from the OCR vision model. Override via OLLAMA_TEXT_MODEL "
            "to point at a dedicated text model."
        ),
    )

    preprocess_enabled: bool = Field(
        default=False,
        description=(
            "Apply Phase 0.5 preprocessing (deskew + CLAHE) before encoding "
            "the image for the inference engine. Default OFF: an A/B over "
            "19 sheets in 2026-04 showed -2.6 pp field accuracy and -4 pp "
            "critical with preprocessing on (Qwen2.5-VL prefers natural "
            "images). Re-evaluate per image type in Phase 0.5 follow-ups."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def _dotenv_value(repo_root: Path, env_var: str) -> str | None:
    env_path = repo_root / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != env_var:
            continue
        value = value.strip().strip('"').strip("'")
        return value or None
    return None


def resolve_kanban_path(env_var: str, prod_default: str, repo_subpath: str) -> Path:
    """R118 — resolve a kanban-data path com fallback inteligente.

    Ordem:
      1. Se env var set → usa esse path (interpretado relativo ao repo se não absoluto).
      2. Senão, se `.env` define a env var → usa esse path.
      3. Senão, se o path produção ``prod_default`` existe e é absoluto no
         sistema atual → usa-o (compat com PC Metalogalva).
      4. Senão → usa ``<repo>/{repo_subpath}`` (dev laptop).

    Args:
        env_var: nome da env var (ex: ``KANBAN_DOC_DIR``).
        prod_default: path Windows-style absoluto do PC produção
            (ex: ``r"C:\\kanban\\nifruka\\04_Documentacao"``).
        repo_subpath: sub-path relativo ao repo root, usado como fallback
            de dev (ex: ``"kanban_refs/04_Documentacao"``).

    O caller pode setar a env var com path absoluto OU relativo. Relativos
    são resolvidos contra o repo root (não contra CWD).
    """
    repo_root = Path(__file__).resolve().parents[2]
    env_val = os.environ.get(env_var) or _dotenv_value(repo_root, env_var)
    if env_val:
        p = Path(env_val)
        return p if p.is_absolute() else repo_root / p
    prod = Path(prod_default)
    # On POSIX, Windows strings like "C:\\kanban\\..." are relative paths.
    # Never treat those as production defaults, even if a previous run
    # accidentally created such a directory in the repo.
    if prod.is_absolute() and prod.exists():
        return prod
    return repo_root / repo_subpath
