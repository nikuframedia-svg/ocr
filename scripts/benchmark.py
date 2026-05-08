"""Run the OCR pipeline against ground-truth annotations and emit baseline metrics.

Usage:
    python scripts/benchmark.py
    python scripts/benchmark.py --images custom/images --ground-truth custom/gt

For each (image, ground_truth.json) pair, the script:

1. Loads the ground truth and validates it against the schema.
2. Calls the vLLM client to extract from the image.
3. Compares field by field, recording every mismatch as a JSONL diff.
4. Aggregates the five baseline metrics and renders ``baseline_v0.md``.

Per-run artefacts go under ``reports/runs/<timestamp>/``; the curated
report stays at ``reports/baseline_v0.md`` (overwritten each run).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Make `backend/` importable without an editable install.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

import httpx  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.evaluation.metrics import (  # noqa: E402
    BaselineMetrics,
    FieldComparison,
    compare_extractions,
    compute_metrics,
    worst_errors,
)
from app.observability.logger import configure_logging, get_logger  # noqa: E402
from app.pipeline.inference.schemas import KanbanExtraction  # noqa: E402
from app.pipeline.inference.vllm_client import (  # noqa: E402
    ExtractionParseError,
    extract,
)
from jinja2 import Template  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from rich.console import Console  # noqa: E402

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "baseline.md.j2"


def _check_vllm_or_die(settings: Settings, console: Console) -> None:
    log = get_logger(__name__)
    base = str(settings.vllm_url).rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{base}/models")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]vLLM unreachable at {base}: {exc}[/red]")
        log.error("benchmark.vllm_unreachable", error=str(exc))
        raise SystemExit(2) from exc

    available = [item["id"] for item in response.json().get("data", [])]
    if settings.vllm_model not in available:
        console.print(
            f"[red]model {settings.vllm_model!r} not loaded — available: {available}[/red]"
        )
        log.error(
            "benchmark.model_missing",
            expected=settings.vllm_model,
            available=available,
        )
        raise SystemExit(3)
    log.info("benchmark.vllm_ok", model=settings.vllm_model)


def _pair_images_with_ground_truth(
    images_dir: Path,
    gt_dir: Path,
) -> list[tuple[Path, Path]]:
    """Return ``[(image_path, gt_path)]`` for every ground-truth file with a matching image."""
    pairs: list[tuple[Path, Path]] = []
    log = get_logger(__name__)
    images_by_stem = {
        p.stem: p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    }
    for gt_path in sorted(gt_dir.glob("*.json")):
        image_path = images_by_stem.get(gt_path.stem)
        if image_path is None:
            log.warning("benchmark.no_image_for_gt", stem=gt_path.stem)
            continue
        pairs.append((image_path, gt_path))
    return pairs


def _load_ground_truth(path: Path) -> KanbanExtraction:
    data = json.loads(path.read_text(encoding="utf-8"))
    return KanbanExtraction.model_validate(data)


def _render_report(
    *,
    timestamp: str,
    run_dir: Path,
    model: str,
    prompt_versions: set[str],
    metrics: BaselineMetrics,
    total_sheets: int,
    successful_sheets: int,
    extraction_errors: list[tuple[str, str]],
    worst: list[FieldComparison],
    average_latency_ms: float,
) -> str:
    template = Template(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.render(
        timestamp=timestamp,
        run_dir=str(run_dir),
        model=model,
        prompt_versions=sorted(prompt_versions),
        metrics=metrics,
        total_sheets=total_sheets,
        successful_sheets=successful_sheets,
        extraction_errors=extraction_errors,
        worst_errors=worst,
        average_latency_ms=average_latency_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the OCR pipeline against ground-truth annotations and emit baseline metrics."
        )
    )
    parser.add_argument("--images", type=Path, default=Path("inputs/originais"))
    parser.add_argument("--ground-truth", type=Path, default=Path("ground_truth"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args()

    configure_logging()
    log = get_logger(__name__)
    console = Console()
    settings = get_settings()

    if not args.images.is_dir():
        console.print(f"[red]images dir not found: {args.images}[/red]")
        return 2
    if not args.ground_truth.is_dir():
        console.print(f"[red]ground truth dir not found: {args.ground_truth}[/red]")
        return 2

    pairs = _pair_images_with_ground_truth(args.images, args.ground_truth)
    if not pairs:
        console.print("[red]no (image, ground_truth) pairs found[/red]")
        return 2

    console.print(f"[bold]{len(pairs)} sheets to benchmark[/bold]")
    _check_vllm_or_die(settings, console)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    run_dir = args.reports_dir / "runs" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    diffs_path = run_dir / "diffs.jsonl"

    comparisons_by_sheet: dict[str, list[FieldComparison]] = {}
    extraction_errors: list[tuple[str, str]] = []
    prompt_versions: set[str] = set()
    total_latency_ms = 0.0

    with diffs_path.open("w", encoding="utf-8") as diffs_file:
        for idx, (image_path, gt_path) in enumerate(pairs, start=1):
            sheet_name = image_path.name
            console.print(f"  [{idx}/{len(pairs)}] {sheet_name}")

            try:
                expected = _load_ground_truth(gt_path)
            except (json.JSONDecodeError, ValidationError) as exc:
                log.error("benchmark.gt_invalid", file=sheet_name, error=str(exc))
                extraction_errors.append((sheet_name, f"ground_truth_invalid: {exc}"))
                continue

            try:
                result = extract(image_path, settings=settings)
            except ExtractionParseError as exc:
                log.error("benchmark.parse_failed", file=sheet_name, error=str(exc))
                extraction_errors.append((sheet_name, f"parse_error: {exc.preview}"))
                continue
            except (httpx.HTTPError, ValidationError) as exc:
                log.error("benchmark.extraction_failed", file=sheet_name, error=str(exc))
                extraction_errors.append((sheet_name, f"extraction_failed: {exc}"))
                continue

            prompt_versions.add(result.prompt_version)
            total_latency_ms += result.latency_ms

            comps = compare_extractions(sheet_name, expected, result.extraction)
            comparisons_by_sheet[sheet_name] = comps

            for comp in comps:
                if comp.is_match:
                    continue
                diffs_file.write(
                    json.dumps(asdict(comp), ensure_ascii=False) + "\n"
                )

    if not comparisons_by_sheet:
        console.print("[red]no successful extractions — see logs and reports/runs/[/red]")
        return 1

    metrics = compute_metrics(comparisons_by_sheet)
    worst = worst_errors(comparisons_by_sheet, n=5)
    average_latency_ms = total_latency_ms / len(comparisons_by_sheet)

    md = _render_report(
        timestamp=timestamp,
        run_dir=run_dir,
        model=settings.vllm_model,
        prompt_versions=prompt_versions,
        metrics=metrics,
        total_sheets=len(pairs),
        successful_sheets=len(comparisons_by_sheet),
        extraction_errors=extraction_errors,
        worst=worst,
        average_latency_ms=average_latency_ms,
    )

    output = args.reports_dir / "baseline_v0.md"
    output.write_text(md, encoding="utf-8")

    console.print(f"\n[green]baseline written to {output}[/green]")
    console.print(f"  field accuracy:    {metrics.field_accuracy * 100:.1f}%")
    console.print(f"  perfect sheets:    {metrics.perfect_sheet_rate * 100:.1f}%")
    console.print(f"  critical accuracy: {metrics.critical_field_accuracy * 100:.1f}%")
    console.print(f"  hallucination:     {metrics.hallucination_rate * 100:.1f}%")
    console.print(f"  failures:          {len(extraction_errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
