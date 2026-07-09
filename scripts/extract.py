"""Extract Kanban data from one or many images.

Outputs the legacy 3-block CSV (compatible with ``ocr6.py``) for every
sheet, plus an optional HTML view with the original image inline next
to the extracted table — that's the primary review surface ("scroll
through 19 Kanbans, eyeball the differences against the photo").

Usage:
    python scripts/extract.py inputs/originais/AugustoMonteiro_2026.04.16.JPG --html
    python scripts/extract.py inputs/originais/ --html
    python scripts/extract.py inputs/originais/ --html --ground-truth ground_truth/

When ``--ground-truth`` is provided AND a JSON with the matching stem
exists in that folder, the HTML highlights diffs and an aggregate
``_metrics.md`` is written alongside the per-sheet outputs.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path

# Make `backend/` importable without an editable install.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

import httpx
from app.config import Settings, get_settings
from app.evaluation.metrics import (
    FieldComparison,
    character_error_rate,
    compare_extractions,
    compute_metrics,
    worst_errors,
)
from app.observability.logger import configure_logging, get_logger
from app.pipeline.inference.csv_writer import write_csv
from app.pipeline.inference.schemas import (
    ROW_FIELDS,
    KanbanExtraction,
)
from app.pipeline.inference.vllm_client import (
    ExtractionParseError,
    ExtractionResult,
    extract,
    extract_with_crops,
)
from pydantic import ValidationError
from rich.console import Console

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


# ---------------------------------------------------------------------------
# Health check (mirror of benchmark.py — abort early if endpoint dead).
# ---------------------------------------------------------------------------


def _check_endpoint_or_die(settings: Settings, console: Console) -> None:
    log = get_logger(__name__)
    base = str(settings.vllm_url).rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{base}/models")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]inference endpoint unreachable at {base}: {exc}[/red]")
        log.error("extract.endpoint_unreachable", error=str(exc))
        raise SystemExit(2) from exc

    available = [item["id"] for item in response.json().get("data", [])]
    if settings.vllm_model not in available:
        console.print(
            f"[red]model {settings.vllm_model!r} not loaded — available: {available}[/red]"
        )
        log.error(
            "extract.model_missing",
            expected=settings.vllm_model,
            available=available,
        )
        raise SystemExit(3)


# ---------------------------------------------------------------------------
# Per-sheet HTML generation: image + extraction table side by side.
# ---------------------------------------------------------------------------


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; margin: 0; padding: 16px;
       background: #f5f5f5; color: #222; }
h1 { font-size: 16px; margin: 0 0 12px; }
.meta { font-size: 11px; color: #666; margin-bottom: 12px; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
         align-items: start; max-height: 95vh; }
.image, .table { background: white; padding: 12px; border-radius: 4px;
                 overflow: auto; max-height: 95vh; }
.image img { max-width: 100%; height: auto; display: block; }
table { border-collapse: collapse; width: 100%; font-size: 11px;
        font-family: ui-monospace, monospace; }
th { background: #eee; padding: 4px; border: 1px solid #aaa;
     text-align: left; position: sticky; top: 0; }
td { padding: 3px 4px; border: 1px solid #ddd; }
td.empty { color: #ccc; }
td.critical { background: #fff8e0; }
td.mismatch { background: #ffd6d6; }
td.hallucination { background: #ffe6cc; }
.legend { font-size: 10px; color: #888; margin-top: 8px; }
.swatch { display: inline-block; width: 12px; height: 12px;
          vertical-align: middle; margin-right: 4px; }
section { margin-bottom: 12px; }
section h2 { font-size: 12px; margin: 0 0 4px; color: #555; }
"""

_CRITICAL_ROW_KEYS = {"of", "modelo", "qtd"}


def _row_cell(value: str, *, key: str, expected: str | None) -> str:
    """One <td> with appropriate CSS class."""
    classes: list[str] = []
    if key in _CRITICAL_ROW_KEYS:
        classes.append("critical")
    if not value:
        classes.append("empty")
    if expected is not None and expected != value:
        classes.append("hallucination" if (not expected and value) else "mismatch")
    cls = f' class="{" ".join(classes)}"' if classes else ""
    return f"<td{cls}>{escape(value) if value else '&nbsp;'}</td>"


def _render_table(
    extraction: KanbanExtraction,
    expected: KanbanExtraction | None,
) -> str:
    h = extraction.header
    eh = expected.header if expected else None

    def cell(actual_val: str, expected_val: str | None, key: str) -> str:
        return _row_cell(actual_val, key=key, expected=expected_val)

    header_html = (
        f"<section><h2>HEADER</h2><table>"
        f"<tr><th>operador</th><th>nº</th><th>setor/máquina</th><th>data</th></tr>"
        f"<tr>{cell(h.operador, eh.operador if eh else None, 'operador')}"
        f"{cell(h.n_operador, eh.n_operador if eh else None, 'n_operador')}"
        f"{cell(h.setor_maquina, eh.setor_maquina if eh else None, 'setor_maquina')}"
        f"{cell(h.data, eh.data if eh else None, 'data')}</tr></table></section>"
    )

    expected_rows = expected.rows if expected else []
    rows_html_parts = [
        "<section><h2>ROWS</h2><table>"
        "<tr><th>#</th>"
        + "".join(f"<th>{k}</th>" for k in ROW_FIELDS)
        + "</tr>"
    ]
    for idx, row in enumerate(extraction.rows):
        e_row = expected_rows[idx] if idx < len(expected_rows) else None
        cells = "".join(
            cell(getattr(row, k), getattr(e_row, k) if e_row else None, k)
            for k in ROW_FIELDS
        )
        rows_html_parts.append(f"<tr><td>{idx}</td>{cells}</tr>")
    # Show extra ground-truth rows the model missed
    for idx in range(len(extraction.rows), len(expected_rows)):
        e_row = expected_rows[idx]
        cells = "".join(
            f'<td class="mismatch">{escape(getattr(e_row, k))}</td>' for k in ROW_FIELDS
        )
        rows_html_parts.append(f'<tr style="opacity:0.5"><td>{idx}*</td>{cells}</tr>')
    rows_html_parts.append("</table></section>")
    rows_html = "".join(rows_html_parts)

    f = extraction.footer
    ef = expected.footer if expected else None
    fc_actual = f.colunas_produzidas
    fc_expected = ef.colunas_produzidas if ef else None
    fh_actual = f.horas_trabalhadas
    fh_expected = ef.horas_trabalhadas if ef else None
    footer_html = (
        "<section><h2>FOOTER</h2><table>"
        "<tr><th>colunas_produzidas</th><th>horas_trabalhadas</th></tr>"
        f"<tr>{cell(fc_actual, fc_expected, 'colunas_produzidas')}"
        f"{cell(fh_actual, fh_expected, 'horas_trabalhadas')}"
        "</tr></table></section>"
    )

    return header_html + rows_html + footer_html


def _build_html(
    image_path: Path,
    result: ExtractionResult,
    expected: KanbanExtraction | None,
) -> str:
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    suffix = image_path.suffix.lower().lstrip(".")
    mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
    table = _render_table(result.extraction, expected)
    legend = (
        '<div class="legend">'
        '<span class="swatch" style="background:#fff8e0"></span>critical (OF/MODELO/QTD) &nbsp; '
        '<span class="swatch" style="background:#ffd6d6"></span>mismatch vs ground truth &nbsp; '
        '<span class="swatch" style="background:#ffe6cc"></span>hallucination &nbsp; '
        '<span style="color:#ccc">empty</span>'
        "</div>"
    )
    meta = (
        f'<div class="meta">model: <code>{escape(result.model)}</code> &middot; '
        f"latency: {result.latency_ms:.0f} ms &middot; "
        f"tokens: {result.completion_tokens} &middot; "
        f"prompt: <code>{result.prompt_version[:8]}</code> &middot; "
        f"grammar: <code>{result.grammar_version}</code></div>"
    )
    return f"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><title>{escape(image_path.name)}</title>
<style>{_CSS}</style></head><body>
<h1>{escape(image_path.name)}</h1>
{meta}
<div class="split">
  <div class="image"><img src="data:{mime};base64,{img_b64}" alt="{escape(image_path.name)}"></div>
  <div class="table">{table}{legend}</div>
</div>
</body></html>"""


def _build_index(
    sheets: list[tuple[Path, ExtractionResult, KanbanExtraction | None]],
    metrics_summary: str | None,
) -> str:
    rows = "".join(
        f'<tr><td><a href="{p.stem}.html">{escape(p.name)}</a></td>'
        f"<td>{len(r.extraction.rows)}</td>"
        f"<td>{r.latency_ms:.0f}</td>"
        f"<td>{r.completion_tokens}</td>"
        f'<td>{"yes" if exp else "no"}</td></tr>'
        for p, r, exp in sheets
    )
    metrics_block = (
        f'<section><h2>METRICS</h2><pre>{escape(metrics_summary)}</pre></section>'
        if metrics_summary
        else ""
    )
    return f"""<!doctype html>
<html lang="pt"><head><meta charset="utf-8"><title>Extractions index</title>
<style>{_CSS}</style></head><body>
<h1>Extractions ({len(sheets)} sheets)</h1>
{metrics_block}
<table><tr><th>image</th><th>rows</th><th>latency (ms)</th>
<th>tokens</th><th>has gt</th></tr>{rows}</table>
</body></html>"""


# ---------------------------------------------------------------------------
# Metrics report (Markdown)
# ---------------------------------------------------------------------------


def _render_metrics_md(
    metrics_summary_text: str,
    metrics_obj_dict: dict,
    extraction_errors: list[tuple[str, str]],
    worst: list[FieldComparison],
) -> str:
    lines = [
        "# Extraction metrics",
        "",
        f"Generated: {datetime.now(tz=UTC).isoformat()}",
        "",
        "## Summary",
        "",
        f"```\n{metrics_summary_text}\n```",
        "",
        "## Accuracy by field",
        "",
        "| field | accuracy | CER (lex) |",
        "|---|---|---|",
    ]
    acc = metrics_obj_dict["accuracy_by_field"]
    cer = metrics_obj_dict["cer_by_field"]
    for field in sorted(acc.keys()):
        a_pct = f"{acc[field] * 100:.1f}%"
        c_str = f"{cer[field]:.3f}" if field in cer else "—"
        lines.append(f"| `{field}` | {a_pct} | {c_str} |")
    lines.extend(["", "## Top errors to eyeball", ""])
    for err in worst:
        flags = []
        if err.is_critical:
            flags.append("critical")
        if err.is_hallucination:
            flags.append("hallucination")
        flag_str = f" *({', '.join(flags)})*" if flags else ""
        lines.append(f"- **{err.field_path}** in `{err.sheet}`{flag_str}")
        lines.append(f"  - expected: `{err.expected or '<empty>'}`")
        lines.append(f"  - actual:   `{err.actual or '<empty>'}`")
    if extraction_errors:
        lines.extend(["", "## Extraction failures", ""])
        for sheet, msg in extraction_errors:
            lines.append(f"- `{sheet}`: {msg}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Kanban data from one or many images, output CSV + optional HTML."
        )
    )
    parser.add_argument("source", type=Path, help="image file or folder of images")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/extractions"),
        help="output directory (default: reports/extractions/)",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="also write HTML preview with image+table side-by-side",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=None,
        help=(
            "folder of ground-truth JSON files; when set, emit metrics "
            "and highlight diffs in the HTML"
        ),
    )
    parser.add_argument(
        "--crops",
        action="store_true",
        help=(
            "use the 3-pass crops pipeline (header/table/footer separately). "
            "~3x latency but typically cleaner structure on long sheets."
        ),
    )
    args = parser.parse_args()

    configure_logging()
    log = get_logger(__name__)
    console = Console()
    settings = get_settings()

    if args.source.is_file():
        images = [args.source]
    elif args.source.is_dir():
        images = sorted(
            p for p in args.source.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
        )
    else:
        console.print(f"[red]not found: {args.source}[/red]")
        return 2
    if not images:
        console.print("[red]no images found[/red]")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    _check_endpoint_or_die(settings, console)

    console.print(f"[bold]{len(images)} sheet(s) to extract[/bold]")
    sheets: list[tuple[Path, ExtractionResult, KanbanExtraction | None]] = []
    extraction_errors: list[tuple[str, str]] = []
    comparisons_by_sheet: dict[str, list[FieldComparison]] = {}

    extract_fn = extract_with_crops if args.crops else extract
    for idx, image_path in enumerate(images, start=1):
        console.print(f"  [{idx}/{len(images)}] {image_path.name}")
        try:
            result = extract_fn(image_path, settings=settings)
        except ExtractionParseError as exc:
            log.error("extract.parse_failed", file=image_path.name, error=str(exc))
            extraction_errors.append((image_path.name, f"parse_error: {exc.preview}"))
            continue
        except (httpx.HTTPError, ValidationError) as exc:
            log.error("extract.failed", file=image_path.name, error=str(exc))
            extraction_errors.append((image_path.name, f"failed: {exc}"))
            continue

        # CSV always.
        csv_path = args.out / f"{image_path.stem}.csv"
        write_csv(csv_path, [(image_path.name, result.extraction)])

        # Ground truth match (optional).
        expected: KanbanExtraction | None = None
        if args.ground_truth and args.ground_truth.is_dir():
            gt_path = args.ground_truth / f"{image_path.stem}.json"
            if gt_path.is_file():
                try:
                    expected = KanbanExtraction.model_validate(
                        json.loads(gt_path.read_text(encoding="utf-8"))
                    )
                except (json.JSONDecodeError, ValidationError) as exc:
                    log.warning(
                        "extract.gt_invalid", file=gt_path.name, error=str(exc)
                    )
                    expected = None

        if args.html:
            html_path = args.out / f"{image_path.stem}.html"
            html_path.write_text(_build_html(image_path, result, expected), encoding="utf-8")

        if expected is not None:
            comparisons_by_sheet[image_path.name] = compare_extractions(
                image_path.name, expected, result.extraction
            )

        sheets.append((image_path, result, expected))

    # Aggregate metrics if any ground-truth match.
    metrics_summary_text: str | None = None
    if comparisons_by_sheet:
        metrics = compute_metrics(comparisons_by_sheet)
        worst = worst_errors(comparisons_by_sheet, n=5)
        metrics_summary_text = (
            f"sheets evaluated:    {metrics.total_sheets}\n"
            f"field accuracy:      {metrics.field_accuracy * 100:.1f}%\n"
            f"perfect sheets:      {metrics.perfect_sheet_rate * 100:.1f}%\n"
            f"critical accuracy:   {metrics.critical_field_accuracy * 100:.1f}%\n"
            f"hallucination rate:  {metrics.hallucination_rate * 100:.1f}%"
        )
        # Also write a JSONL of all diffs for downstream tooling.
        diffs_path = args.out / "diffs.jsonl"
        with diffs_path.open("w", encoding="utf-8") as fh:
            for comps in comparisons_by_sheet.values():
                for c in comps:
                    if not c.is_match:
                        fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        # Markdown report.
        md = _render_metrics_md(
            metrics_summary_text, asdict(metrics), extraction_errors, worst
        )
        (args.out / "_metrics.md").write_text(md, encoding="utf-8")
        console.print(f"\n[green]metrics: {args.out / '_metrics.md'}[/green]")
        console.print(metrics_summary_text)

    if args.html and len(images) > 1:
        index_path = args.out / "index.html"
        index_path.write_text(
            _build_index(sheets, metrics_summary_text), encoding="utf-8"
        )
        console.print(f"[green]index: {index_path}[/green]")

    failures = len(extraction_errors)
    success = len(sheets)
    console.print(f"\n[bold]{success}/{len(images)} extracted, {failures} failure(s)[/bold]")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


# Used to silence unused-import lint when the type only appears in
# a CER-based dataclass field. Direct import keeps the function
# accessible from this script's namespace for ad-hoc REPL use.
_ = character_error_rate
