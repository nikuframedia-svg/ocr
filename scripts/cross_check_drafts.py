"""Cross-check the ground-truth drafts for transcription inconsistencies.

For each OF that appears in more than one sheet (or more than one row),
compare ``cliente``, ``modelo``, ``comp_mm``, ``larg_mm``. When an OF
has divergent values across rows, at least one of them is wrong —
either in the source sheet or in my draft transcription.

Use this BEFORE moving drafts into ``ground_truth/``. Mismatches surface
the rows most worth eyeballing first.

    python scripts/cross_check_drafts.py
    python scripts/cross_check_drafts.py \\
        --drafts ground_truth_draft \\
        --output reports/draft_cross_check.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.observability.logger import configure_logging
from app.pipeline.inference.schemas import KanbanExtraction

# Fields we consider "should be the same for the same OF".
# (PRI / QTD / LOTE / CONI / ESP / LBASE / LTOPO can legitimately vary
# across days for the same OF, so we don't flag those.)
_INVARIANT_FIELDS = ("cliente", "modelo", "comp_mm", "larg_mm")


def _load_drafts(drafts_dir: Path) -> dict[str, KanbanExtraction]:
    out: dict[str, KanbanExtraction] = {}
    for path in sorted(drafts_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[path.stem] = KanbanExtraction.model_validate(data)
    return out


def _index_rows_by_of(
    extractions: dict[str, KanbanExtraction],
) -> dict[str, list[tuple[str, int, dict[str, str]]]]:
    """Return ``{of: [(sheet_stem, row_index, row_as_dict), ...]}`` for OFs with rows."""
    by_of: dict[str, list[tuple[str, int, dict[str, str]]]] = defaultdict(list)
    for sheet_stem, extraction in extractions.items():
        for idx, row in enumerate(extraction.rows):
            of_value = row.of.strip()
            if not of_value:
                continue
            row_dict = {f: getattr(row, f) for f in _INVARIANT_FIELDS}
            by_of[of_value].append((sheet_stem, idx, row_dict))
    return by_of


def _find_inconsistencies(
    by_of: dict[str, list[tuple[str, int, dict[str, str]]]],
) -> list[tuple[str, dict[str, set[str]], list[tuple[str, int]]]]:
    """For each OF with > 1 row, return divergent invariant fields."""
    inconsistencies: list[tuple[str, dict[str, set[str]], list[tuple[str, int]]]] = []
    for of_value, occurrences in by_of.items():
        if len(occurrences) < 2:
            continue
        per_field: dict[str, set[str]] = {f: set() for f in _INVARIANT_FIELDS}
        for _, _, row in occurrences:
            for f in _INVARIANT_FIELDS:
                per_field[f].add(row[f])
        divergent = {f: vals for f, vals in per_field.items() if len(vals) > 1}
        if divergent:
            inconsistencies.append(
                (of_value, divergent, [(stem, idx) for stem, idx, _ in occurrences])
            )
    return inconsistencies


def _render_report(
    inconsistencies: list[tuple[str, dict[str, set[str]], list[tuple[str, int]]]],
    by_of: dict[str, list[tuple[str, int, dict[str, str]]]],
) -> str:
    total_ofs = len(by_of)
    repeated_ofs = sum(1 for occ in by_of.values() if len(occ) > 1)
    suspect_ofs = len(inconsistencies)

    lines: list[str] = []
    lines.append("# Cross-check report — ground_truth_draft/")
    lines.append("")
    lines.append(f"- distinct OFs: **{total_ofs}**")
    lines.append(f"- OFs that appear more than once: **{repeated_ofs}**")
    lines.append(f"- OFs with divergent invariant fields: **{suspect_ofs}**")
    lines.append("")
    if not inconsistencies:
        lines.append("No invariant divergence detected. Either every repeated OF is consistent")
        lines.append("(good), or my drafts mirror each other's mistakes (less good).")
        return "\n".join(lines) + "\n"

    lines.append("## Suspect OFs")
    lines.append("")
    lines.append(
        "An OF with divergent `cliente` / `modelo` / `comp_mm` / `larg_mm` "
        "across rows almost certainly has at least one wrong transcription. "
        "Open the listed sheets and compare."
    )
    lines.append("")

    for of_value, divergent, locations in sorted(inconsistencies):
        lines.append(f"### OF `{of_value}`")
        lines.append("")
        lines.append("Appears in:")
        for stem, idx in locations:
            row = next(r for s, i, r in by_of[of_value] if s == stem and i == idx)
            row_repr = ", ".join(f"{f}=`{row[f]!s}`" for f in _INVARIANT_FIELDS)
            lines.append(f"- `{stem}.json` row {idx}: {row_repr}")
        lines.append("")
        lines.append("Divergent fields:")
        for field, values in divergent.items():
            value_list = ", ".join(f"`{v}`" for v in sorted(values))
            lines.append(f"- **{field}**: {value_list}")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, default=Path("ground_truth_draft"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/draft_cross_check.md"),
    )
    args = parser.parse_args()

    configure_logging()

    if not args.drafts.is_dir():
        print(f"[red]drafts dir not found: {args.drafts}[/red]")
        return 2

    extractions = _load_drafts(args.drafts)
    by_of = _index_rows_by_of(extractions)
    inconsistencies = _find_inconsistencies(by_of)

    report = _render_report(inconsistencies, by_of)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")

    print(f"wrote {args.output}")
    print(f"  distinct OFs: {len(by_of)}")
    print(f"  suspect OFs: {len(inconsistencies)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
