#!/usr/bin/env python3
"""Compare stored old cross-check JSON with the current live engine.

Purpose:
  - run after an update, before opening many sheets in the browser;
  - keep the DB and cross-check JSON untouched;
  - measure if the new cross engine changes winners, refs, statuses and
    proposal metadata versus the stale JSON already stored on disk.

Examples on Windows:
    .venv\\Scripts\\python.exe scripts\\diag\\cross_diff.py --last 400 --only-changed
    .venv\\Scripts\\python.exe scripts\\diag\\cross_diff.py --sheet 2136 --detail
    .venv\\Scripts\\python.exe scripts\\diag\\cross_diff.py --last 400 --csv reports\\cross_diff_400.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))


_CELL_KEYS = (
    "status",
    "engine_status",
    "value",
    "ref",
    "source",
    "ref_source",
    "proposal_source",
    "hypothesis_level",
    "alteration_rule",
)
_RESULT_KEYS = {"status", "engine_status", "value", "ref", "source", "ref_source"}
_META_KEYS = {"proposal_source", "hypothesis_level", "alteration_rule"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _short(value: Any, limit: int = 46) -> str:
    text = _text(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _summary(cc: dict[str, Any] | None) -> dict[str, int]:
    data = (cc or {}).get("summary") or {}
    return {
        "match": _int(data.get("match")),
        "no_match": _int(data.get("no_match")),
        "na": _int(data.get("na")),
        "total": _int(data.get("total")),
    }


def _row_index(row: dict[str, Any], pos: int) -> int:
    try:
        return int(row.get("row_index", pos) or 0)
    except (TypeError, ValueError):
        return pos


def _iter_rows(cc: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for pos, row in enumerate((cc or {}).get("rows") or []):
        if isinstance(row, dict):
            rows[_row_index(row, pos)] = row
    return rows


def _iter_cells(cc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(cc, dict):
        return out

    for section in ("header", "footer"):
        block = cc.get(section) or {}
        if not isinstance(block, dict):
            continue
        for field, cell in block.items():
            if isinstance(cell, dict):
                out[f"{section}.{field}"] = cell

    for pos, row in enumerate(cc.get("rows") or []):
        if not isinstance(row, dict):
            continue
        row_i = _row_index(row, pos)
        fields = row.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        for field, cell in fields.items():
            if isinstance(cell, dict):
                out[f"rows[{row_i}].{field}"] = cell
    return out


def _cell_sig(cell: dict[str, Any] | None) -> dict[str, str]:
    cell = cell or {}
    return {key: _text(cell.get(key)) for key in _CELL_KEYS}


def _human_path(path: str) -> str:
    if path.startswith("rows["):
        try:
            left, field = path.split("].", 1)
            row_i = int(left.removeprefix("rows["))
            return f"L{row_i + 1}.{field}"
        except (ValueError, IndexError):
            return path
    return path


def _winner_sig(row: dict[str, Any] | None) -> tuple[str, str, str]:
    row = row or {}
    strategy = row.get("proposal_strategy") or {}
    return (
        _text(row.get("winner_of")),
        _text(row.get("winner_mode")),
        _text(strategy.get("hypothesis_level") or row.get("hypothesis_level")),
    )


def _proposal_source(cell: dict[str, Any] | None) -> str:
    cell = cell or {}
    return _text(cell.get("proposal_source") or cell.get("source") or cell.get("ref_source"))


def _is_structural(cell: dict[str, Any] | None) -> bool:
    cell = cell or {}
    joined = " ".join(
        _text(cell.get(key)).lower()
        for key in ("proposal_source", "alteration_rule", "source", "ref_source")
    )
    return "structural" in joined or "realign" in joined


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_sum = _summary(old)
    new_sum = _summary(new)
    old_cells = _iter_cells(old)
    new_cells = _iter_cells(new)
    old_rows = _iter_rows(old)
    new_rows = _iter_rows(new)

    cell_changes: list[dict[str, Any]] = []
    counts = Counter()
    for path in sorted(set(old_cells) | set(new_cells)):
        left = _cell_sig(old_cells.get(path))
        right = _cell_sig(new_cells.get(path))
        changed_keys = [key for key in _CELL_KEYS if left.get(key) != right.get(key)]
        if not changed_keys:
            continue
        for key in changed_keys:
            counts[f"{key}_changes"] += 1
        counts["cell_changes"] += 1
        if any(key in _RESULT_KEYS for key in changed_keys):
            counts["result_cell_changes"] += 1
        if any(key in _META_KEYS for key in changed_keys):
            counts["metadata_cell_changes"] += 1
        cell_changes.append({
            "path": path,
            "changed_keys": changed_keys,
            "old": left,
            "new": right,
        })

    winner_changes: list[dict[str, Any]] = []
    for row_i in sorted(set(old_rows) | set(new_rows)):
        left = _winner_sig(old_rows.get(row_i))
        right = _winner_sig(new_rows.get(row_i))
        if left != right:
            counts["winner_changes"] += 1
            winner_changes.append({
                "row": row_i,
                "old": left,
                "new": right,
            })

    new_weak_cells = 0
    new_structural_cells = 0
    new_sources = Counter()
    for cell in new_cells.values():
        hyp = _text(cell.get("hypothesis_level"))
        if hyp == "weak_hypothesis":
            new_weak_cells += 1
        if _is_structural(cell):
            new_structural_cells += 1
        source = _proposal_source(cell) or "-"
        new_sources[source] += 1

    result_changed = bool(counts["result_cell_changes"] or winner_changes or old_sum != new_sum)
    metadata_changed = bool(counts["metadata_cell_changes"])

    return {
        "old_summary": old_sum,
        "new_summary": new_sum,
        "delta_no_match": new_sum["no_match"] - old_sum["no_match"],
        "delta_match": new_sum["match"] - old_sum["match"],
        "delta_na": new_sum["na"] - old_sum["na"],
        "cell_changes": cell_changes,
        "winner_changes": winner_changes,
        "counts": counts,
        "new_weak_cells": new_weak_cells,
        "new_structural_cells": new_structural_cells,
        "new_sources": new_sources,
        "result_changed": result_changed,
        "metadata_changed": metadata_changed,
    }


def _sheet_ids(args: argparse.Namespace) -> list[int]:
    from app.web import db

    if args.sheet:
        return args.sheet

    limit_sql = "" if args.all else " LIMIT ?"
    params: tuple[Any, ...] = () if args.all else (args.last,)
    with db.conn() as c:
        rows = c.execute(
            """
            SELECT id
              FROM sheets
             WHERE (raw_extraction IS NOT NULL AND raw_extraction <> '')
                OR (sheet_data IS NOT NULL AND sheet_data <> '')
             ORDER BY captured_at DESC, id DESC
            """ + limit_sql,
            params,
        ).fetchall()
    return [int(r["id"]) for r in rows]


def _load_raw(sheet_id: int) -> tuple[dict[str, Any] | None, str]:
    from app.web import db

    sheet = db.get_sheet(sheet_id)
    if not sheet:
        return None, "folha nao encontrada"
    raw = sheet.get("raw_extraction") or sheet.get("sheet_data")
    if not isinstance(raw, dict) or not raw.get("rows"):
        return None, "sem raw_extraction/sheet_data com linhas"
    return raw, ""


def _compare_sheet(sheet_id: int, refs: dict[str, Any]) -> dict[str, Any]:
    from app.cross_check.storage import load_sheet_cross_check
    from app.pipeline import scoring_engine as se

    started = time.perf_counter()
    old = load_sheet_cross_check(sheet_id, include_stale=True)
    if not old:
        raw, raw_error = _load_raw(sheet_id)
        return {
            "sheet_id": sheet_id,
            "status": "sem_cross_antigo",
            "error": raw_error,
            "template": (raw or {}).get("template_name") if raw else "",
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    raw, raw_error = _load_raw(sheet_id)
    if raw is None:
        return {
            "sheet_id": sheet_id,
            "status": "sem_ocr",
            "error": raw_error,
            "old_engine": old.get("engine_version", ""),
            "template": old.get("template_name", ""),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    try:
        new = se.cross_check_sheet(raw, None, refs)
    except Exception as exc:  # noqa: BLE001 - diagnostic must continue.
        return {
            "sheet_id": sheet_id,
            "status": "erro_motor_novo",
            "error": repr(exc),
            "old_engine": old.get("engine_version", ""),
            "template": old.get("template_name") or raw.get("template_name", ""),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }

    d = _diff(old, new)
    old_sum = d["old_summary"]
    new_sum = d["new_summary"]
    counts = d["counts"]
    if d["result_changed"]:
        status = "mudou"
    elif d["metadata_changed"]:
        status = "meta"
    else:
        status = "igual"
    return {
        "sheet_id": sheet_id,
        "status": status,
        "template": new.get("template_name") or old.get("template_name") or raw.get("template_name", ""),
        "old_engine": old.get("engine_version", ""),
        "new_engine": new.get("engine_version", ""),
        "old_match": old_sum["match"],
        "new_match": new_sum["match"],
        "old_no_match": old_sum["no_match"],
        "new_no_match": new_sum["no_match"],
        "old_na": old_sum["na"],
        "new_na": new_sum["na"],
        "delta_no_match": d["delta_no_match"],
        "delta_match": d["delta_match"],
        "delta_na": d["delta_na"],
        "winner_changes": len(d["winner_changes"]),
        "cell_changes": int(counts["cell_changes"]),
        "result_cell_changes": int(counts["result_cell_changes"]),
        "metadata_cell_changes": int(counts["metadata_cell_changes"]),
        "status_changes": int(counts["status_changes"] + counts["engine_status_changes"]),
        "ref_changes": int(counts["ref_changes"]),
        "value_changes": int(counts["value_changes"]),
        "source_changes": int(
            counts["source_changes"]
            + counts["ref_source_changes"]
            + counts["proposal_source_changes"]
        ),
        "strategy_changes": int(
            counts["hypothesis_level_changes"] + counts["alteration_rule_changes"]
        ),
        "new_weak_cells": d["new_weak_cells"],
        "new_structural_cells": d["new_structural_cells"],
        "new_sources": d["new_sources"],
        "cell_detail": d["cell_changes"],
        "winner_detail": d["winner_changes"],
        "error": "",
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }


def _print_table(rows: list[dict[str, Any]], only_changed: bool) -> None:
    visible = [
        r for r in rows
        if not only_changed or r.get("status") not in {"igual"}
    ]
    if not visible:
        print("Sem diferencas para mostrar com estes filtros.")
        return

    print("")
    print(
        "FOLHA  TEMPLATE             OLD -> NEW              XX old->new  "
        "WIN  CELLS  META  REF  SRC  HYP  STRUCT  STATUS"
    )
    print("-" * 112)
    for r in visible:
        if r.get("status") not in {"mudou", "meta", "igual"}:
            print(
                str(r.get("sheet_id", "")).ljust(6)
                + _short(r.get("template", ""), 20).ljust(21)
                + "-".ljust(24)
                + "-".ljust(12)
                + "-".rjust(4)
                + "-".rjust(7)
                + "-".rjust(6)
                + "-".rjust(5)
                + "-".rjust(5)
                + "-".rjust(5)
                + "-".rjust(8)
                + "  "
                + str(r.get("status"))
                + (" (" + r.get("error", "") + ")" if r.get("error") else "")
            )
            continue
        engine = (
            _short(r.get("old_engine", ""), 10)
            + " -> "
            + _short(r.get("new_engine", ""), 10)
        )
        xx = f"{r.get('old_no_match', 0)}->{r.get('new_no_match', 0)}"
        print(
            str(r.get("sheet_id", "")).ljust(6)
            + _short(r.get("template", ""), 20).ljust(21)
            + engine.ljust(24)
            + xx.ljust(12)
            + str(r.get("winner_changes", 0)).rjust(4)
            + str(r.get("result_cell_changes", 0)).rjust(7)
            + str(r.get("metadata_cell_changes", 0)).rjust(6)
            + str(r.get("ref_changes", 0)).rjust(5)
            + str(r.get("source_changes", 0)).rjust(5)
            + str(r.get("new_weak_cells", 0)).rjust(5)
            + str(r.get("new_structural_cells", 0)).rjust(8)
            + "  "
            + str(r.get("status"))
        )


def _print_detail(rows: list[dict[str, Any]], max_details: int) -> None:
    for r in rows:
        if r.get("status") not in {"mudou", "meta"}:
            continue
        print("")
        print(
            "=== Folha "
            + str(r["sheet_id"])
            + " | "
            + str(r.get("template") or "?")
            + " | "
            + str(r.get("old_engine"))
            + " -> "
            + str(r.get("new_engine"))
            + " ==="
        )
        for item in r.get("winner_detail") or []:
            old_of, old_mode, old_hyp = item["old"]
            new_of, new_mode, new_hyp = item["new"]
            print(
                "  L"
                + str(int(item["row"]) + 1)
                + " winner: "
                + f"{old_of or '-'} / {old_mode or '-'} / {old_hyp or '-'}"
                + "  ->  "
                + f"{new_of or '-'} / {new_mode or '-'} / {new_hyp or '-'}"
            )

        shown = 0
        for item in r.get("cell_detail") or []:
            if shown >= max_details:
                rest = len(r.get("cell_detail") or []) - shown
                if rest > 0:
                    print("  ... +" + str(rest) + " mudancas de celula")
                break
            old = item["old"]
            new = item["new"]
            pieces = []
            for key in ("status", "engine_status", "ref", "source", "proposal_source",
                        "hypothesis_level", "alteration_rule", "value"):
                if old.get(key) != new.get(key):
                    pieces.append(
                        key
                        + ": "
                        + repr(_short(old.get(key), 26))
                        + " -> "
                        + repr(_short(new.get(key), 26))
                    )
            print("  " + _human_path(item["path"]) + " | " + "; ".join(pieces))
            shown += 1


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sheet_id", "status", "template", "old_engine", "new_engine",
        "old_match", "new_match", "old_no_match", "new_no_match",
        "old_na", "new_na", "delta_no_match", "delta_match", "delta_na",
        "winner_changes", "cell_changes", "status_changes", "ref_changes",
        "result_cell_changes", "metadata_cell_changes", "value_changes",
        "source_changes", "strategy_changes",
        "new_weak_cells", "new_structural_cells", "duration_ms", "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="A/B antigo-vs-novo do cross-check, sem gravar folhas.",
    )
    ap.add_argument("--sheet", type=int, action="append", help="folha especifica; pode repetir")
    ap.add_argument("--last", type=int, default=400, help="ultimas N folhas (default: 400)")
    ap.add_argument("--all", action="store_true", help="todas as folhas com OCR")
    ap.add_argument("--only-changed", action="store_true", help="mostrar so folhas que mudaram")
    ap.add_argument("--detail", action="store_true", help="mostrar winners/celulas alteradas")
    ap.add_argument("--max-details", type=int, default=25, help="maximo de celulas por folha no detalhe")
    ap.add_argument("--csv", type=Path, default=None, help="guardar resumo CSV")
    args = ap.parse_args()

    from app.cross_check.ref_watcher import get_watcher
    from app.pipeline import scoring_engine as se

    ids = _sheet_ids(args)
    if not ids:
        print("Sem folhas com OCR para comparar.")
        return 1

    print("=== CROSS DIFF: antigo guardado vs motor novo ao vivo ===")
    print("Motor novo: " + str(se.ENGINE_VERSION))
    print("Folhas a comparar: " + str(len(ids)))
    print("Aviso: correr antes de abrir muitas folhas, porque a UI pode regenerar o JSON antigo.")
    refs = get_watcher().get_refs()
    print(
        "Refs: "
        + ("OK" if refs.get("available") else "VAZIAS/INDISPONIVEIS")
        + " | loaded_at="
        + str(refs.get("loaded_at") or "-")
    )

    rows: list[dict[str, Any]] = []
    totals = Counter()
    source_totals = Counter()
    for idx, sheet_id in enumerate(ids, start=1):
        row = _compare_sheet(sheet_id, refs)
        rows.append(row)
        totals[row.get("status") or "?"] += 1
        if idx % 25 == 0:
            print("... " + str(idx) + "/" + str(len(ids)) + " folhas")
        source_totals.update(row.get("new_sources") or {})

    changed = sum(1 for r in rows if r.get("status") == "mudou")
    meta_only = sum(1 for r in rows if r.get("status") == "meta")
    comparable = sum(1 for r in rows if r.get("status") in {"mudou", "meta", "igual"})
    old_no = sum(_int(r.get("old_no_match")) for r in rows)
    new_no = sum(_int(r.get("new_no_match")) for r in rows)
    old_match = sum(_int(r.get("old_match")) for r in rows)
    new_match = sum(_int(r.get("new_match")) for r in rows)

    print("")
    print("=== RESUMO ===")
    print("Comparaveis: " + str(comparable) + " | resultado mudou: " + str(changed) + " | so meta: " + str(meta_only))
    print("Sem cross antigo: " + str(totals["sem_cross_antigo"]) + " | sem OCR: " + str(totals["sem_ocr"]) + " | erros: " + str(totals["erro_motor_novo"]))
    print("MATCH total: " + str(old_match) + " -> " + str(new_match) + " (delta " + str(new_match - old_match) + ")")
    print("NO_MATCH total: " + str(old_no) + " -> " + str(new_no) + " (delta " + str(new_no - old_no) + ")")
    print("Winner changes: " + str(sum(_int(r.get("winner_changes")) for r in rows)))
    print("Result cell changes: " + str(sum(_int(r.get("result_cell_changes")) for r in rows)))
    print("Metadata cell changes: " + str(sum(_int(r.get("metadata_cell_changes")) for r in rows)))
    print("New weak_hypothesis cells: " + str(sum(_int(r.get("new_weak_cells")) for r in rows)))
    print("New structural_realign cells: " + str(sum(_int(r.get("new_structural_cells")) for r in rows)))

    if source_totals:
        print("")
        print("Top proposal/source no motor novo:")
        for source, count in source_totals.most_common(12):
            print("  " + str(source).ljust(24) + str(count))

    _print_table(rows, args.only_changed)
    if args.detail:
        _print_detail(rows, args.max_details)
    if args.csv:
        _write_csv(args.csv, rows)
        print("")
        print("CSV escrito em: " + str(args.csv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
