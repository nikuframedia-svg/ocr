"""Integration tests requiring a live vLLM service.

These are gated by the ``vllm`` pytest marker. To run them:

    uv run pytest -m vllm

The fixture skips automatically when vLLM is not reachable, so the
default ``uv run pytest`` invocation stays green even without the
service running.
"""
from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.vllm


@pytest.fixture(scope="module")
def settings():
    from app.config import get_settings

    return get_settings()


@pytest.fixture(scope="module")
def vllm_available(settings) -> bool:
    base = str(settings.vllm_url).rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base}/models")
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    available = [item["id"] for item in response.json().get("data", [])]
    return settings.vllm_model in available


@pytest.fixture(scope="module")
def synthetic_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a tiny blank white PNG so the test has *some* image to send."""
    pillow = pytest.importorskip("PIL.Image")
    img = pillow.new("RGB", (256, 256), color=(255, 255, 255))
    out = tmp_path_factory.mktemp("vllm-fixture") / "blank.png"
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out.write_bytes(buf.getvalue())
    return out


def test_vllm_endpoint_lists_expected_model(settings, vllm_available) -> None:
    if not vllm_available:
        pytest.skip("vLLM not reachable or expected model not loaded")
    base = str(settings.vllm_url).rstrip("/")
    with httpx.Client(timeout=5.0) as client:
        response = client.get(f"{base}/models")
    response.raise_for_status()
    models = [item["id"] for item in response.json().get("data", [])]
    assert settings.vllm_model in models


def test_extract_returns_result_or_acceptable_error(
    settings,
    vllm_available,
    synthetic_image: Path,
) -> None:
    """Smoke test: the request reaches vLLM and the client either returns
    a result or fails in a way the benchmark already handles.
    """
    if not vllm_available:
        pytest.skip("vLLM not reachable or expected model not loaded")

    from app.pipeline.inference.vllm_client import (
        ExtractionParseError,
        ExtractionResult,
        extract,
    )
    from pydantic import ValidationError

    try:
        result = extract(synthetic_image, settings=settings)
    except (ExtractionParseError, ValidationError):
        # The model can legitimately fail to produce schema-valid JSON for
        # a blank image; we only care that the network plumbing works.
        return
    assert isinstance(result, ExtractionResult)
    assert result.model == settings.vllm_model
    assert result.latency_ms > 0
