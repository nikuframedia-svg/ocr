"""Health check for the vLLM service.

Hits the OpenAI-compatible ``/v1/models`` endpoint and verifies the
expected model (from ``settings.vllm_model``) is available. Prints a
single JSON line to stdout for downstream scripts to parse, and returns
a non-zero exit code on failure.

Exit codes:

- ``0`` — vLLM is reachable and the expected model is loaded.
- ``2`` — vLLM is unreachable (network / timeout / 5xx).
- ``3`` — vLLM is up but the expected model is not in the served list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the project's `backend/` importable when running as a script
# (without requiring `uv pip install -e .`).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

import httpx  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.observability.logger import configure_logging, get_logger  # noqa: E402


def main() -> int:
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()

    base = str(settings.vllm_url).rstrip("/")
    url = f"{base}/models"
    log.info("vllm.check.start", url=url, expected_model=settings.vllm_model)

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("vllm.check.unreachable", error=str(exc))
        print(json.dumps({"status": "unreachable", "error": str(exc)}))
        return 2

    data = response.json()
    available = [item["id"] for item in data.get("data", [])]

    if settings.vllm_model in available:
        log.info("vllm.check.ok", model=settings.vllm_model)
        print(json.dumps({"status": "ok", "model": settings.vllm_model, "available": available}))
        return 0

    log.error(
        "vllm.check.model_missing",
        expected=settings.vllm_model,
        available=available,
    )
    print(
        json.dumps(
            {
                "status": "model_missing",
                "expected": settings.vllm_model,
                "available": available,
            }
        )
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
