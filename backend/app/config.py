"""Application settings loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

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
        default="qwen2.5vl:7b",
        description=(
            "Model name as known to the inference engine. Ollama default "
            "tag is q4_K_M (~5 GB). 3B was the baseline; 7B is the "
            "production choice. For vLLM use `Qwen/Qwen2.5-VL-7B-Instruct-AWQ`."
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
