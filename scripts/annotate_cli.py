"""Interactive CLI for annotating Kanban sheets — Phase 0 ground truth.

Usage:
    python scripts/annotate_cli.py                        # uses inputs/originais/
    python scripts/annotate_cli.py inputs/originais/      # explicit folder
    python scripts/annotate_cli.py inputs/originais/ --force
    python scripts/annotate_cli.py --validate-only

For each image, the script:

1. Opens it in the system default viewer (so the operator can read it
   while typing).
2. Prompts for each header / row / footer field, applying lightweight
   regex validation on critical fields (date, OF, LOTE, MODELO,
   numeric sizes).
3. Suggests recently-seen values for free-form fields (operador,
   cliente, lote) so the operator only has to type each one once.
4. Saves the result to ``ground_truth/<image_stem>.json``.

Already-annotated images are skipped unless ``--force`` is passed.
``--validate-only`` re-validates every existing annotation against the
schema (useful when the schema evolves).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

# Make `backend/` importable without an editable install.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.observability.logger import configure_logging, get_logger  # noqa: E402
from app.pipeline.inference.schemas import (  # noqa: E402
    FOOTER_FIELDS,
    HEADER_FIELDS,
    ROW_FIELDS,
    KanbanExtraction,
)
from pydantic import ValidationError  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.prompt import Confirm, Prompt  # noqa: E402

# Regex tuned against the 19 sample sheets from Metalogalva (Apr 2026).
# MODELO and CLIENTE are intentionally NOT validated by regex: real values
# range from `CGC2E10D` to `OMEGA60-6M`, `BRACOS`, `1383VF01`, `0641-S-515`,
# multi-line names like `CRLS-N°1\nCR1921A4505`, and suffixes such as
# `-V`, `-2ªPRIORIDADE`, `-N°1`. Any pattern tight enough to be useful
# would reject roughly 4 in 5 actual values.
_OF_RE = re.compile(r"^\d{6}$")
_LOTE_RE = re.compile(r"^[HM]\d{2}B\d{4,5}$")  # 4 OR 5 trailing digits
_DATE_RE = re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")  # accepts 1- or 2-digit D/M
_NUMERIC_RE = re.compile(r"^\d+$")
_RECENT_LIMIT = 50

_VALIDATORS: dict[str, tuple[re.Pattern[str], str]] = {
    "data": (_DATE_RE, "D-M-YYYY (e.g. 10-4-2026 or 18-04-2026)"),
    "of": (_OF_RE, "exactly 6 digits"),
    "lote": (_LOTE_RE, "[HM]NNB followed by 4 or 5 digits, e.g. M25B0746 or M24B11531"),
    "qtd": (_NUMERIC_RE, "digits only"),
    "comp_mm": (_NUMERIC_RE, "digits only"),
    "larg_mm": (_NUMERIC_RE, "digits only"),
    "n_operador": (_NUMERIC_RE, "digits only"),
    "colunas_produzidas": (_NUMERIC_RE, "digits only"),
}


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def _open_image(path: Path) -> None:
    """Open an image file in the system default viewer."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        # Viewer launch is best-effort; the operator can still annotate.
        pass


def _normalise_date(value: str) -> str:
    return value.replace("/", "-").replace(".", "-")


def _validate_field(name: str, value: str) -> tuple[bool, str, str | None]:
    """Return ``(is_valid, normalised_value, hint_if_invalid)``.

    Empty values are always valid (the field is optional / not visible).
    """
    if name == "data":
        normalised = _normalise_date(value)
        if normalised and not _DATE_RE.match(normalised):
            return False, value, _VALIDATORS["data"][1]
        return True, normalised, None

    if value == "":
        return True, "", None

    spec = _VALIDATORS.get(name)
    if spec is None:
        return True, value, None

    pattern, hint = spec
    if pattern.match(value):
        return True, value, None
    return False, value, hint


def _prompt_field(
    name: str,
    *,
    recent: list[str],
    console: Console,
    default: str = "",
) -> str:
    """Prompt for one field, validating until input is acceptable.

    When ``default`` is non-empty it is shown to the user; pressing
    Enter accepts it. Used by ``--from-draft`` to pre-populate fields
    so the operator only has to verify or correct.
    """
    hint = ""
    if recent:
        hint = f" [dim](recent: {', '.join(recent[-3:])})[/dim]"
    while True:
        raw = Prompt.ask(
            f"  {name}{hint}", default=default, show_default=bool(default), console=console
        )
        ok, value, why = _validate_field(name, raw.strip())
        if ok:
            return value
        console.print(f"    [red]invalid[/red] — expected: {why}")


def _draft_for(stem: str, draft_dir: Path | None) -> dict | None:
    """Load a draft JSON if available; return its dict (raw) or None."""
    if draft_dir is None:
        return None
    path = draft_dir / f"{stem}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _annotate_image(
    image_path: Path,
    recent: dict[str, list[str]],
    console: Console,
    draft_dir: Path | None = None,
) -> KanbanExtraction:
    """Run the interactive annotation loop for one image.

    When ``draft_dir`` points to a folder with ``<stem>.json`` for
    this image, every prompt is pre-filled with the draft value.
    """
    _open_image(image_path)
    console.print(Panel.fit(f"[cyan bold]{image_path.name}[/cyan bold]", border_style="cyan"))

    draft = _draft_for(image_path.stem, draft_dir)
    if draft:
        console.print(
            f"[dim]using draft from {draft_dir}/{image_path.stem}.json — Enter to accept[/dim]"
        )

    console.print("[bold]Header[/bold]")
    draft_header = (draft or {}).get("header") or {}
    header = {
        field: _prompt_field(
            field,
            recent=recent.get(field, []),
            console=console,
            default=str(draft_header.get(field, "") or ""),
        )
        for field in HEADER_FIELDS
    }

    console.print(
        "[bold]Rows[/bold] — Enter to accept draft value; answer No to stop adding rows."
    )
    rows: list[dict[str, str]] = []
    draft_rows = (draft or {}).get("rows") or []
    if draft and draft_rows:
        console.print(
            f"[dim]draft has {len(draft_rows)} row(s); each is pre-loaded — accept or edit[/dim]"
        )
    while True:
        idx = len(rows)
        draft_row = draft_rows[idx] if idx < len(draft_rows) else {}
        # Default the "add another row?" prompt to True while there
        # are draft rows left, False once we've consumed them all.
        default_next = idx < len(draft_rows)
        if not Confirm.ask(
            f"  add row {idx} (draft has {'value' if draft_row else 'no value'})?",
            default=default_next,
            console=console,
        ):
            break
        rows.append(
            {
                field: _prompt_field(
                    field,
                    recent=recent.get(field, []),
                    console=console,
                    default=str(draft_row.get(field, "") or ""),
                )
                for field in ROW_FIELDS
            }
        )

    console.print("[bold]Footer[/bold]")
    draft_footer = (draft or {}).get("footer") or {}
    footer = {
        field: _prompt_field(
            field,
            recent=recent.get(field, []),
            console=console,
            default=str(draft_footer.get(field, "") or ""),
        )
        for field in FOOTER_FIELDS
    }

    payload = {"header": header, "rows": rows, "footer": footer}
    return KanbanExtraction.model_validate(payload)


def _update_recent(recent: dict[str, list[str]], extraction: KanbanExtraction) -> None:
    """Record non-empty values seen in this extraction for future hinting."""
    def push(field: str, value: str) -> None:
        if not value:
            return
        bucket = recent.setdefault(field, [])
        if value in bucket:
            bucket.remove(value)
        bucket.append(value)
        if len(bucket) > _RECENT_LIMIT:
            del bucket[: len(bucket) - _RECENT_LIMIT]

    for field in HEADER_FIELDS:
        push(field, getattr(extraction.header, field))
    for row in extraction.rows:
        for field in ROW_FIELDS:
            push(field, getattr(row, field))
    for field in FOOTER_FIELDS:
        push(field, getattr(extraction.footer, field))


def _load_existing_recent(ground_truth_dir: Path) -> dict[str, list[str]]:
    """Bootstrap the recent-values cache from previously-saved ground truth."""
    recent: dict[str, list[str]] = {}
    if not ground_truth_dir.exists():
        return recent
    for path in sorted(ground_truth_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            extraction = KanbanExtraction.model_validate(data)
        except (json.JSONDecodeError, ValidationError):
            continue
        _update_recent(recent, extraction)
    return recent


def _validate_only(ground_truth_dir: Path, console: Console) -> int:
    """Re-validate every saved annotation against the current schema."""
    if not ground_truth_dir.exists():
        console.print(f"[yellow]no annotations yet at {ground_truth_dir}[/yellow]")
        return 0
    failures = 0
    for path in sorted(ground_truth_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            KanbanExtraction.model_validate(data)
        except json.JSONDecodeError as exc:
            console.print(f"  [red][FAIL][/red] {path.name} — invalid JSON: {exc}")
            failures += 1
        except ValidationError as exc:
            console.print(f"  [red][FAIL][/red] {path.name} — schema mismatch:\n{exc}")
            failures += 1
        else:
            console.print(f"  [green][OK][/green] {path.name}")
    if failures:
        console.print(f"\n[red]{failures} annotation(s) failed[/red]")
        return 1
    console.print("\n[green]all annotations valid[/green]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Annotate Kanban sheets for the Phase 0 baseline.")
    parser.add_argument(
        "images",
        nargs="?",
        type=Path,
        default=Path("inputs/originais"),
        help="folder of images to annotate (default: inputs/originais)",
    )
    parser.add_argument(
        "--ground-truth-dir",
        type=Path,
        default=Path("ground_truth"),
        help="where to read/write annotation JSONs (default: ground_truth/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-annotate images that already have a ground truth file",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="re-validate every saved annotation against the current schema and exit",
    )
    parser.add_argument(
        "--from-draft",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "folder with draft JSONs (e.g. ground_truth_draft/); each "
            "field's prompt is pre-populated with the draft value, "
            "Enter accepts. Cuts annotation time roughly in half."
        ),
    )
    args = parser.parse_args()

    configure_logging()
    log = get_logger(__name__)
    console = Console()

    ground_truth_dir: Path = args.ground_truth_dir
    ground_truth_dir.mkdir(parents=True, exist_ok=True)

    if args.validate_only:
        return _validate_only(ground_truth_dir, console)

    if not args.images:
        parser.error("the images folder is required (or pass --validate-only)")

    images_dir: Path = args.images
    if not images_dir.is_dir():
        parser.error(f"{images_dir} is not a directory")

    images = sorted(p for p in images_dir.iterdir() if p.suffix.lower() in _IMAGE_EXTS)
    if not images:
        console.print(f"[yellow]no images found in {images_dir}[/yellow]")
        return 0

    recent = _load_existing_recent(ground_truth_dir)

    total = len(images)
    for index, image_path in enumerate(images, start=1):
        out_path = ground_truth_dir / f"{image_path.stem}.json"
        if out_path.exists() and not args.force:
            console.print(
                f"[dim][{index}/{total}][/dim] [yellow]skip[/yellow] "
                f"{image_path.name} — already annotated"
            )
            continue

        console.rule(f"[bold]{index}/{total}[/bold]  {image_path.name}")
        try:
            extraction = _annotate_image(
                image_path, recent, console, draft_dir=args.from_draft
            )
        except KeyboardInterrupt:
            console.print("\n[yellow]annotation interrupted — partial work not saved[/yellow]")
            return 130

        out_path.write_text(
            extraction.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("annotation.saved", file=image_path.name, rows=len(extraction.rows))
        _update_recent(recent, extraction)
        console.print(f"  [green]saved[/green] → {out_path}")

    console.print("\n[bold green]done[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
