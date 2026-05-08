"""DQ pipeline CLI — run L1+L2+L3 on a folder of v9-style extractions.

Usage:
    python scripts/dq_run.py reports/extractions_v9/ \\
        --out reports/extractions_v9_dq/ \\
        --inputs inputs/originais/

Outputs per sheet:
    <out>/<stem>_dq.json   — full audit trail
    <out>/<stem>_fixed.json — post-L2 fixed extraction (drop-in v9 replacement)
    <out>/<stem>.html       — image + table with cells coloured by confidence

Outputs at folder level:
    <out>/_summary.md       — flag-precision/recall, STP rate, top rules
    <out>/index.html        — links to per-sheet HTMLs
    <out>/cross_sheet.json  — persistent index updated through batch

Exit code: 0 if any output written; 1 on input errors.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from html import escape
from pathlib import Path

# Ensure backend is importable
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "backend"))

from app.dq.cross_sheet_index import CrossSheetIndex  # noqa: E402
from app.dq.pipeline import run_dq, _ROW_FIELDS, _HEADER_FIELDS, _FOOTER_FIELDS  # noqa: E402


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt"><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 0; background: #f4f4f5; }}
.wrap {{ display: flex; gap: 12px; padding: 12px; height: 100vh; box-sizing: border-box; }}
.left {{ flex: 1; overflow: auto; background: #fff; border: 1px solid #ddd; }}
.left img {{ max-width: 100%; display: block; }}
.right {{ flex: 1; overflow: auto; background: #fff; border: 1px solid #ddd; padding: 12px; }}
h1 {{ font-size: 16px; margin: 0 0 8px; }}
.summary {{ background: #f9fafb; padding: 8px; border-left: 3px solid #6366f1; margin-bottom: 12px; font-size: 13px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ border: 1px solid #ccc; padding: 4px 6px; text-align: left; vertical-align: top; }}
th {{ background: #fafafa; position: sticky; top: 0; }}
.cell-high {{ background: #ecfdf5; }}        /* confidence >= 0.9 */
.cell-mid  {{ background: #fef3c7; }}        /* 0.7 <= confidence < 0.9 */
.cell-low  {{ background: #fee2e2; }}        /* confidence < 0.7 */
.cell-fixed {{ font-style: italic; }}
.cell-fixed::after {{ content: " ↻"; color: #6366f1; font-size: 10px; }}
.violations {{ margin-top: 16px; }}
.viol {{ font-size: 12px; padding: 6px 8px; margin-bottom: 4px; border-left: 3px solid; }}
.viol.error {{ border-color: #dc2626; background: #fef2f2; }}
.viol.warning {{ border-color: #d97706; background: #fffbeb; }}
.viol .rule {{ font-weight: 600; }}
.tip {{ cursor: help; }}
.tip:hover::after {{
    content: attr(data-tip); position: absolute; background: #1f2937; color: #fff;
    padding: 4px 8px; border-radius: 3px; font-size: 11px; white-space: pre-wrap;
    max-width: 300px; z-index: 10; margin-left: 8px;
}}
.score {{ font-size: 13px; padding: 4px 8px; border-radius: 3px; margin-left: 8px; }}
.score.stp {{ background: #ecfdf5; color: #059669; }}
.score.review {{ background: #fef3c7; color: #b45309; }}
</style></head><body>
<div class="wrap">
<div class="left">{img_html}</div>
<div class="right">
<h1>{title} <span class="score {score_class}">{score_label}</span></h1>
<div class="summary">{summary}</div>
{table_html}
{violations_html}
</div></div></body></html>
"""


def _img_html(image_path: Path | None) -> str:
    if image_path is None or not image_path.exists():
        return "<em>(no image)</em>"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f'<img src="data:image/jpeg;base64,{data}" alt="{escape(image_path.name)}">'


def _cell_class(conf: float) -> str:
    if conf >= 0.9:
        return "cell-high"
    if conf >= 0.7:
        return "cell-mid"
    return "cell-low"


def _td(cell: dict | None) -> str:
    if cell is None:
        return "<td></td>"
    cls = [_cell_class(cell["confidence"])]
    if cell["fix_chain"]:
        cls.append("cell-fixed")
    parts = []
    if cell["fix_chain"]:
        parts.append(f"raw={cell['raw']!r}")
        parts.extend(cell["fix_chain"])
    if cell["issues"]:
        parts.extend(f"L1: {i['rule']}" for i in cell["issues"])
    if cell["violations"]:
        parts.extend(f"L3: {v['rule']}" for v in cell["violations"])
    parts.append(f"conf={cell['confidence']:.2f}")
    tip = " | ".join(parts)
    val = escape(cell["final"]) or "&nbsp;"
    return f'<td class="{" ".join(cls)} tip" data-tip="{escape(tip)}">{val}</td>'


def _build_table_html(dq: dict) -> str:
    cells: dict[str, dict] = dq["cells"]
    by_path = cells

    rows_html: list[str] = ["<table>"]
    # Header row
    rows_html.append("<thead>")
    rows_html.append("<tr><th>#</th>" + "".join(f"<th>{f}</th>" for f in _ROW_FIELDS) + "</tr>")
    rows_html.append("</thead>")
    rows_html.append("<tbody>")

    # Header / footer first
    hdr_cells = [by_path.get(f"header.{f}") for f in _HEADER_FIELDS]
    rows_html.append("<tr><td><strong>HDR</strong></td>"
                     + "".join(f'<td colspan="{len(_ROW_FIELDS)//len(_HEADER_FIELDS)}">'
                               f'{c["field"]}: {escape(c["final"])} '
                               f'<small>({c["confidence"]:.2f})</small></td>' for c in hdr_cells if c)
                     + "</tr>")
    # Row data
    row_indices = sorted({c["row_index"] for c in cells.values() if c.get("row_index") is not None})
    for i in row_indices:
        row_cells = [by_path.get(f"rows[{i}].{f}") for f in _ROW_FIELDS]
        rows_html.append(f'<tr><td>{i+1}</td>' + "".join(_td(c) for c in row_cells) + "</tr>")
    # Footer
    ft_cells = [by_path.get(f"footer.{f}") for f in _FOOTER_FIELDS]
    rows_html.append("<tr><td><strong>FT</strong></td>"
                     + "".join(f'<td colspan="{len(_ROW_FIELDS)//len(_FOOTER_FIELDS)}">'
                               f'{c["field"]}: {escape(c["final"])} '
                               f'<small>({c["confidence"]:.2f})</small></td>' for c in ft_cells if c)
                     + "</tr>")
    rows_html.append("</tbody></table>")
    return "\n".join(rows_html)


def _build_violations_html(violations: list[dict]) -> str:
    if not violations:
        return ""
    blocks = ['<div class="violations"><h3>L3 violations</h3>']
    for v in violations:
        affected = ", ".join(a["path"] for a in v["affected"])
        blocks.append(
            f'<div class="viol {v["severity"]}">'
            f'<div class="rule">{escape(v["rule"])} '
            f'<small style="font-weight:normal">[{v["severity"]}, weight {v["weight"]:.2f}]</small></div>'
            f'<div>expected: {escape(v["expected"])}</div>'
            f'<div>actual: {escape(v["actual"])}</div>'
            f'<div>affected: <code>{escape(affected)}</code></div>'
            + (f'<div><small>{escape(v["detail"])}</small></div>' if v.get("detail") else "")
            + "</div>"
        )
    blocks.append("</div>")
    return "\n".join(blocks)


def _render_html(dq: dict, image_path: Path | None) -> str:
    score = dq["aggregate_score"]
    summary_text = (
        f"Aggregate score: {score:.2f} | "
        f"L1 issues: {dq['summary']['n_l1_issues']} | "
        f"L3 violations: {dq['summary']['n_l3_violations']} ({dq['summary']['n_l3_errors']} errors) | "
        f"L2 fixes: {dq['summary']['n_l2_fixes']} | "
        f"requires review: {dq['summary']['n_requires_review']}"
    )
    if dq["stp_eligible"]:
        score_label, score_class = "STP eligible", "stp"
    else:
        score_label, score_class = f"{dq['summary']['n_requires_review']} cells need review", "review"

    return _HTML_TEMPLATE.format(
        title=escape(dq["sheet"]),
        img_html=_img_html(image_path),
        summary=escape(summary_text),
        table_html=_build_table_html(dq),
        violations_html=_build_violations_html(dq["violations"]),
        score_label=escape(score_label),
        score_class=score_class,
    )


def _build_index_html(out_dir: Path, results: list[dict]) -> str:
    rows_html = ['<!DOCTYPE html><html><head><meta charset="utf-8"><title>DQ Run Index</title>',
                 '<style>body{font-family:system-ui;padding:20px}'
                 'table{border-collapse:collapse}th,td{border:1px solid #ccc;padding:6px 10px}'
                 'tr.stp{background:#ecfdf5}tr.review{background:#fef3c7}</style></head>',
                 '<body><h1>DQ Run — Summary</h1><table>',
                 '<tr><th>Sheet</th><th>Score</th><th>L1</th><th>L3 (err)</th>'
                 '<th>L2 fixes</th><th>Review</th><th>STP</th></tr>']
    for r in results:
        cls = "stp" if r["stp_eligible"] else "review"
        rows_html.append(
            f'<tr class="{cls}">'
            f'<td><a href="{r["sheet"]}.html">{r["sheet"]}</a></td>'
            f'<td>{r["aggregate_score"]:.2f}</td>'
            f'<td>{r["summary"]["n_l1_issues"]}</td>'
            f'<td>{r["summary"]["n_l3_violations"]} ({r["summary"]["n_l3_errors"]})</td>'
            f'<td>{r["summary"]["n_l2_fixes"]}</td>'
            f'<td>{r["summary"]["n_requires_review"]}</td>'
            f'<td>{"YES" if r["stp_eligible"] else "no"}</td></tr>'
        )
    rows_html.append("</table></body></html>")
    return "\n".join(rows_html)


def _build_summary_md(results: list[dict]) -> str:
    n = len(results)
    n_stp = sum(1 for r in results if r["stp_eligible"])
    n_l3 = sum(r["summary"]["n_l3_violations"] for r in results)
    n_l3_err = sum(r["summary"]["n_l3_errors"] for r in results)
    n_l1 = sum(r["summary"]["n_l1_issues"] for r in results)
    n_l2 = sum(r["summary"]["n_l2_fixes"] for r in results)
    n_review = sum(r["summary"]["n_requires_review"] for r in results)

    rule_counts: dict[str, int] = {}
    for r in results:
        for v in r["violations"]:
            rule_counts[v["rule"]] = rule_counts.get(v["rule"], 0) + 1

    lines = [
        "# DQ Run Summary",
        "",
        f"**Sheets:** {n}",
        f"**STP eligible:** {n_stp}/{n} ({n_stp/n*100:.0f}%)",
        f"**Mean aggregate score:** {sum(r['aggregate_score'] for r in results)/n:.2f}",
        "",
        "## Aggregates",
        "",
        f"- L1 issues total: {n_l1}",
        f"- L2 fixes applied: {n_l2}",
        f"- L3 violations total: {n_l3} ({n_l3_err} errors)",
        f"- Cells requiring review: {n_review}",
        "",
        "## Violations by rule",
        "",
        "| rule | count |",
        "|---|---|",
    ]
    for rule, count in sorted(rule_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| `{rule}` | {count} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="DQ pipeline runner")
    parser.add_argument("input_dir", type=Path,
                        help="Folder with raw extractions (*.json from ocr6.py)")
    parser.add_argument("--out", type=Path, default=Path("reports/extractions_dq"),
                        help="Output folder (default: reports/extractions_dq)")
    parser.add_argument("--inputs", type=Path, default=Path("inputs/originais"),
                        help="Folder with original images for HTML side-by-side")
    parser.add_argument("--learned", type=Path, default=Path("lexicons/learned.json"),
                        help="Path to mining output")
    parser.add_argument("--index", type=Path, default=Path("reports/dq_index/cross_sheet.json"),
                        help="Persistent cross-sheet index path")
    args = parser.parse_args()

    if not args.input_dir.exists():
        print(f"Input dir not found: {args.input_dir}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    learned = json.loads(args.learned.read_text(encoding="utf-8")) if args.learned.exists() else {}
    index = CrossSheetIndex.load(args.index)
    if not index.ov_to_cliente:
        index.seed_from_learned(args.learned)

    results: list[dict] = []
    json_files = sorted(p for p in args.input_dir.glob("*.json") if not p.name.startswith("_metrics"))
    for jp in json_files:
        extraction = json.loads(jp.read_text(encoding="utf-8"))
        dq, fixed = run_dq(extraction, jp.stem, learned, index)
        dq_dict = dq.to_dict()
        results.append(dq_dict)

        # Per-sheet outputs
        (args.out / f"{jp.stem}_dq.json").write_text(
            json.dumps(dq_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (args.out / f"{jp.stem}_fixed.json").write_text(
            json.dumps(fixed, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # HTML
        img = None
        if args.inputs.exists():
            for ext in (".jpeg", ".jpg", ".JPG", ".png"):
                candidate = args.inputs / f"{jp.stem}{ext}"
                if candidate.exists():
                    img = candidate
                    break
        (args.out / f"{jp.stem}.html").write_text(
            _render_html(dq_dict, img), encoding="utf-8"
        )

    # Folder-level outputs
    (args.out / "_summary.md").write_text(_build_summary_md(results), encoding="utf-8")
    (args.out / "index.html").write_text(_build_index_html(args.out, results), encoding="utf-8")
    index.save(args.index)

    n_stp = sum(1 for r in results if r["stp_eligible"])
    print(f"Wrote {len(results)} sheets to {args.out}")
    print(f"STP eligible: {n_stp}/{len(results)} ({n_stp/len(results)*100:.0f}%)")
    print(f"Index updated: {args.index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
