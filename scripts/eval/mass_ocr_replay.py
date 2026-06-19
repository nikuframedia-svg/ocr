"""Mass replay of kanban images through the real OCR path.

This script is intentionally isolated from the production SQLite DB and from
the production cross-check directory. It reads a dataset bundle, calls the
same OCR runner used by the web app, applies cross-check auto-overwrites in
memory, and writes append-only reports under reports/mass_ocr_runs/.

Example:
    python scripts/eval/mass_ocr_replay.py \
        --dataset /Users/martimnicolau/Downloads/kb_71c811b8ce384f1697a3aa091c0e547b \
        --critical-smoke
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = Path(
    "/Users/martimnicolau/Downloads/kb_71c811b8ce384f1697a3aa091c0e547b"
)
DEFAULT_MODEL = "yasserrmd/Nanonets-OCR2-3B:latest"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
CRITICAL_SHEET_IDS = (1838, 1839, 1840, 1843, 1851)
REPORTS_BASE = REPO / "reports" / "mass_ocr_runs"
UTC = timezone.utc  # noqa: UP017 - keep script runnable with the local Python 3.9 CLI

CONCRETE_SOURCES = {"plan", "sap", "ferramenta", "maquinas", "colaboradores", "lexicon"}
CONCRETE_REF_SOURCES = {"plan", "sap", "maquinas", "colaboradores"}
NO_AUTO_SOURCES = {"ocr_raw", "syntax", "obra_concluida"}


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    path.write_text(content, encoding="utf-8")


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str))
        fh.write("\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return out


def load_dotenv(env_path: Path) -> dict[str, str]:
    """Load .env values before importing ocr6/ocr_runner.

    Existing environment variables win, so an explicit shell override still
    works for controlled experiments.
    """
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def _sqlite_ro(db_path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(db_path.resolve()), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def dataset_db_path(dataset: Path) -> Path:
    return dataset / "app.db"


def resolve_image_path(dataset: Path, image_path: str) -> Path:
    normalized = str(image_path or "").replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        return path
    return dataset / path


def git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception:
        return ""
    return proc.stdout.strip()


def dataset_profile(dataset: Path) -> dict:
    db_path = dataset_db_path(dataset)
    images_dir = dataset / "images"
    profile: dict[str, Any] = {
        "dataset": str(dataset),
        "db_path": str(db_path),
        "image_files": 0,
        "image_ext_counts": {},
        "sheets": {},
        "production_rows": 0,
        "baseline_cross": {},
        "templates": {},
    }
    if images_dir.exists():
        ext_counts: Counter[str] = Counter()
        for p in images_dir.iterdir():
            if p.is_file():
                ext_counts[p.suffix.lower().lstrip(".")] += 1
        profile["image_files"] = sum(ext_counts.values())
        profile["image_ext_counts"] = dict(sorted(ext_counts.items()))
    if not db_path.exists():
        return profile
    with _sqlite_ro(db_path) as conn:
        profile["sheets"] = {
            row["status"]: row["n"]
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM sheets GROUP BY status")
        }
        profile["production_rows"] = conn.execute(
            "SELECT COUNT(*) FROM production_rows"
        ).fetchone()[0]
        baseline = conn.execute(
            """
            SELECT COUNT(*) AS sheets_with_cross,
                   SUM(
                       CAST(json_extract(shadow_scoring_json,'$.summary.total') AS INTEGER)
                   ) AS total,
                   SUM(
                       COALESCE(
                           CAST(json_extract(shadow_scoring_json,'$.summary.na') AS INTEGER),
                           0
                       )
                   ) AS na
              FROM sheets
             WHERE shadow_scoring_json IS NOT NULL AND shadow_scoring_json <> ''
            """
        ).fetchone()
        total = int(baseline["total"] or 0)
        na = int(baseline["na"] or 0)
        profile["baseline_cross"] = {
            "sheets_with_cross": int(baseline["sheets_with_cross"] or 0),
            "total_cells": total,
            "na_cells": na,
            "na_pct": round(100.0 * na / total, 2) if total else None,
        }
        profile["templates"] = {
            row["template"]: row["n"]
            for row in conn.execute(
                """
                SELECT COALESCE(json_extract(sheet_data,'$.template_name'),
                                json_extract(raw_extraction,'$.template_name'),
                                '<none>') AS template,
                       COUNT(*) AS n
                  FROM sheets
                 WHERE sheet_data IS NOT NULL AND sheet_data <> ''
                 GROUP BY template
                 ORDER BY n DESC
                """
            )
        }
    return profile


def load_dataset_sheets(
    dataset: Path,
    *,
    sheet_ids: Iterable[int] | None = None,
    sample_per_template: int | None = None,
    limit: int | None = None,
    require_extraction: bool = False,
) -> list[dict]:
    db_path = dataset_db_path(dataset)
    if not db_path.exists():
        raise FileNotFoundError(f"Dataset DB not found: {db_path}")
    with _sqlite_ro(db_path) as conn:
        params: list[Any] = []
        where = "WHERE status != 'deleted'"
        ids = list(dict.fromkeys(sheet_ids or ()))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            where += f" AND id IN ({placeholders})"
            params.extend(ids)
        if require_extraction:
            where += (
                " AND ((raw_extraction IS NOT NULL AND raw_extraction <> '') "
                "OR (sheet_data IS NOT NULL AND sheet_data <> ''))"
            )
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT id, image_path, captured_at, status, operador,
                       raw_extraction, sheet_data, dq_audit, shadow_scoring_json,
                       COALESCE(json_extract(sheet_data,'$.template_name'),
                                json_extract(raw_extraction,'$.template_name'),
                                '<none>') AS historical_template
                  FROM sheets
                  {where}
                 ORDER BY id
                """,
                params,
            )
        ]
    if sample_per_template:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            tpl = str(row.get("historical_template") or "<none>")
            if len(grouped[tpl]) < sample_per_template:
                grouped[tpl].append(row)
        rows = [row for tpl in sorted(grouped) for row in grouped[tpl]]
        rows.sort(key=lambda r: int(r["id"]))
    if limit is not None:
        rows = rows[:limit]
    return rows


def preflight_ollama(timeout_sec: float = 10.0) -> dict:
    url = os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")
    model = os.environ.get("OCR_MODEL", DEFAULT_MODEL)
    req = urllib.request.Request(f"{url}/api/tags", headers={"Accept": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama unreachable at {url}: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Ollama timeout at {url}") from exc
    models = payload.get("models") or []
    names = sorted(
        str(m.get("name") or m.get("model") or "")
        for m in models
        if isinstance(m, dict)
    )
    if model not in names:
        raise RuntimeError(
            f"OCR model not available on {url}: expected {model!r}; available={names}"
        )
    return {
        "ok": True,
        "url": url,
        "model": model,
        "available_models": names,
        "duration_sec": round(time.perf_counter() - started, 3),
    }


def load_runtime(*, include_ocr: bool = True) -> SimpleNamespace:
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / "backend"))

    from app.cross_check import get_watcher  # noqa: PLC0415
    from app.cross_check.ref_watcher import refs_snapshot  # noqa: PLC0415
    from app.dq.machines import resolve_machine_from_setor  # noqa: PLC0415
    from app.dq.operador_snap import snap_operador  # noqa: PLC0415
    from app.pipeline.scoring_engine import ENGINE_VERSION, cross_check_sheet  # noqa: PLC0415
    if include_ocr:
        from app.web import ocr_runner  # noqa: PLC0415
    else:
        ocr_runner = None

    watcher = get_watcher()
    refs = watcher.get_refs()
    if not refs.get("available"):
        raise RuntimeError("Cross-check refs are not available; cannot run real coverage test")
    plan_path = getattr(watcher, "plan_path", None)
    return SimpleNamespace(
        ocr_runner=ocr_runner,
        cross_check_sheet=cross_check_sheet,
        engine_version=ENGINE_VERSION,
        watcher=watcher,
        refs=refs,
        refs_snapshot=refs_snapshot(refs, plan_path),
        snap_operador=snap_operador,
        resolve_machine_from_setor=resolve_machine_from_setor,
    )


def _parse_field_path(path: str) -> list[str | int]:
    if path.startswith("rows["):
        idx_s, field = path[5:].split("].", 1)
        return ["rows", int(idx_s), field]
    section, field = path.split(".", 1)
    return [section, field]


def _get_path(data: dict, path: str) -> Any:
    cur: Any = data
    for part in _parse_field_path(path):
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                return ""
            cur = cur[part]
        else:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(part, "")
    return cur


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = _parse_field_path(path)
    cur: Any = data
    for part in parts[:-1]:
        if isinstance(part, int):
            cur = cur[part]
        else:
            cur = cur.setdefault(part, [] if part == "rows" else {})
    cur[parts[-1]] = value


def _canonical_for_auto_apply(cell: dict) -> str:
    canonical = ""
    if cell.get("source") == "obra_concluida":
        return canonical
    engine_status = cell.get("engine_status")
    source = cell.get("source")
    ref_source = cell.get("ref_source") or source
    if engine_status == "snapped":
        canonical = str(cell.get("value") or "").strip()
    elif engine_status == "very_different" and source not in NO_AUTO_SOURCES:
        if source in CONCRETE_SOURCES:
            canonical = str(
                cell.get("value") or cell.get("ref") or cell.get("proposed") or ""
            ).strip()
        elif ref_source in CONCRETE_REF_SOURCES:
            canonical = str(cell.get("ref") or cell.get("proposed") or "").strip()
    return canonical


def apply_cross_overwrites_in_memory(
    sheet_data: dict,
    cross_result: dict,
    *,
    protected: frozenset[str] = frozenset(),
) -> list[dict]:
    applied: list[dict] = []

    def apply_cell(path: str, cell: dict) -> None:
        if path in protected:
            return
        canonical = _canonical_for_auto_apply(cell)
        if not canonical:
            return
        old = _get_path(sheet_data, path)
        if str(old or "") == canonical:
            return
        _set_path(sheet_data, path, canonical)
        applied.append(
            {
                "field_path": path,
                "old": "" if old is None else str(old),
                "new": canonical,
                "source": cell.get("source"),
                "ref_source": cell.get("ref_source"),
                "engine_status": cell.get("engine_status"),
            }
        )

    for row_r in cross_result.get("rows", []) or []:
        idx = row_r.get("row_index")
        if idx is None:
            continue
        for field, cell in (row_r.get("fields") or {}).items():
            apply_cell(f"rows[{idx}].{field}", cell)
    for section in ("header", "footer"):
        for field, cell in (cross_result.get(section) or {}).items():
            apply_cell(f"{section}.{field}", cell)
    return applied


def apply_header_derivations_in_memory(
    sheet_data: dict,
    runtime: SimpleNamespace,
    *,
    protected: frozenset[str] = frozenset(),
) -> list[dict]:
    refs = runtime.refs
    applied: list[dict] = []
    header = sheet_data.setdefault("header", {})

    colabs = refs.get("colaboradores") or {}
    raw_name = str(header.get("operador") or "").strip()
    raw_cod = str(header.get("n_operador") or "").strip()
    if colabs and (raw_name or raw_cod):
        sr = runtime.snap_operador(
            raw_name,
            raw_cod,
            colabs,
            aliases=refs.get("operador_aliases") or {},
        )
        targets = []
        if getattr(sr, "pernr", ""):
            targets.append(("header.pernr", sr.pernr))
        if getattr(sr, "applied", False) and getattr(sr, "snapped_name", ""):
            targets.append(("header.operador", sr.snapped_name))
        if getattr(sr, "applied", False) and getattr(sr, "snapped_cod", ""):
            targets.append(("header.n_operador", sr.snapped_cod))
        for path, value in targets:
            if path in protected:
                continue
            old = _get_path(sheet_data, path)
            if str(old or "") == str(value or ""):
                continue
            _set_path(sheet_data, path, value)
            applied.append(
                {
                    "field_path": path,
                    "old": str(old or ""),
                    "new": str(value),
                    "source": "colaboradores",
                }
            )

    if "header.cod_maquina" not in protected:
        setor = str(header.get("setor_maquina") or "").strip()
        maq = runtime.resolve_machine_from_setor(setor, refs) if setor else None
        canonical = (maq or {}).get("codmaq")
        if canonical:
            old = _get_path(sheet_data, "header.cod_maquina")
            if str(old or "") != str(canonical):
                _set_path(sheet_data, "header.cod_maquina", canonical)
                applied.append(
                    {
                        "field_path": "header.cod_maquina",
                        "old": str(old or ""),
                        "new": str(canonical),
                        "source": "maquinas",
                    }
                )
    return applied


def iter_cross_cells(cross_result: dict) -> Iterator[tuple[str, int | None, str, dict]]:
    for field, cell in (cross_result.get("header") or {}).items():
        yield "header", None, field, cell
    for row in cross_result.get("rows", []) or []:
        idx = row.get("row_index")
        for field, cell in (row.get("fields") or {}).items():
            yield "rows", idx, field, cell
    for field, cell in (cross_result.get("footer") or {}).items():
        yield "footer", None, field, cell


def cell_category(cell: dict) -> str:
    status = str(cell.get("status") or "NA").upper()
    if status == "MATCH":
        if str(cell.get("match_kind") or "").startswith("MATCH_REGRA"):
            return "MATCH_REGRA"
        return "MATCH"
    if status == "NO_MATCH":
        return "NO_MATCH"
    return "NA"


def cross_counts(cross_result: dict) -> Counter[str]:
    counts: Counter[str] = Counter()
    for _section, _idx, _field, cell in iter_cross_cells(cross_result):
        counts[cell_category(cell)] += 1
    counts["TOTAL"] = sum(counts.values())
    return counts


def nonempty_row_na_violations(sheet_data: dict, cross_result: dict) -> list[dict]:
    rows = sheet_data.get("rows") or []
    violations: list[dict] = []
    for cross_row in cross_result.get("rows", []) or []:
        idx = cross_row.get("row_index")
        if idx is None or idx >= len(rows):
            continue
        row = rows[idx] or {}
        if not any(str(v or "").strip() for v in row.values()):
            continue
        na_fields = [
            field
            for field, cell in (cross_row.get("fields") or {}).items()
            if cell_category(cell) == "NA"
        ]
        if na_fields:
            violations.append({"row_index": idx, "na_fields": na_fields})
    return violations


def run_sheet(row: dict, dataset: Path, runtime: SimpleNamespace) -> dict:
    image = resolve_image_path(dataset, row["image_path"])
    if not image.exists():
        raise FileNotFoundError(f"Image not found: {image}")

    historical_raw = _json_loads(row.get("raw_extraction"), {})
    historical_sheet = _json_loads(row.get("sheet_data"), {})
    historical_cross = _json_loads(row.get("shadow_scoring_json"), {})

    started = time.perf_counter()
    result = runtime.ocr_runner.run_pipeline(image)
    raw_new = result["raw"]
    dq_new = result["dq"]
    sheet_data = _json_clone(result["current"])

    first_cross = runtime.cross_check_sheet(sheet_data, dq_new, runtime.refs)
    applied = apply_cross_overwrites_in_memory(sheet_data, first_cross)
    applied.extend(apply_header_derivations_in_memory(sheet_data, runtime))
    if applied:
        final_cross = runtime.cross_check_sheet(sheet_data, dq_new, runtime.refs)
    else:
        final_cross = first_cross

    counts = cross_counts(final_cross)
    return {
        "sheet_id": row["id"],
        "ok": True,
        "image_path": row["image_path"],
        "image_abs": str(image),
        "captured_at": row.get("captured_at"),
        "historical_status": row.get("status"),
        "historical_template": row.get("historical_template"),
        "template_new": raw_new.get("template_name") or result.get("template_name"),
        "duration_sec": round(time.perf_counter() - started, 3),
        "rows_old": len(historical_sheet.get("rows") or historical_raw.get("rows") or []),
        "rows_new": len(raw_new.get("rows") or []),
        "auto_overwrites": applied,
        "auto_overwrite_count": len(applied),
        "counts": dict(counts),
        "nonempty_rows_with_na": nonempty_row_na_violations(sheet_data, final_cross),
        "historical": {
            "raw_extraction": historical_raw,
            "sheet_data": historical_sheet,
            "cross": historical_cross,
            "cross_summary": historical_cross.get("summary") or {},
        },
        "raw_new": raw_new,
        "dq_new": dq_new,
        "sheet_data_final": sheet_data,
        "cross_final": final_cross,
    }


def run_sheet_cross_only(row: dict, dataset: Path, runtime: SimpleNamespace) -> dict:
    image = resolve_image_path(dataset, row["image_path"])
    if not image.exists():
        raise FileNotFoundError(f"Image not found: {image}")

    historical_raw = _json_loads(row.get("raw_extraction"), {})
    historical_sheet = _json_loads(row.get("sheet_data"), {})
    historical_cross = _json_loads(row.get("shadow_scoring_json"), {})
    if not historical_raw and not historical_sheet:
        raise ValueError("Sheet has no raw_extraction or sheet_data to cross-check")

    started = time.perf_counter()
    raw_input = historical_raw or historical_sheet
    sheet_data = _json_clone(raw_input)
    if "template_name" not in sheet_data and historical_sheet.get("template_name"):
        sheet_data["template_name"] = historical_sheet["template_name"]
    dq_input = _json_loads(row.get("dq_audit"), {})

    first_cross = runtime.cross_check_sheet(sheet_data, dq_input, runtime.refs)
    applied = apply_cross_overwrites_in_memory(sheet_data, first_cross)
    applied.extend(apply_header_derivations_in_memory(sheet_data, runtime))
    if applied:
        final_cross = runtime.cross_check_sheet(sheet_data, dq_input, runtime.refs)
    else:
        final_cross = first_cross

    counts = cross_counts(final_cross)
    return {
        "sheet_id": row["id"],
        "ok": True,
        "mode": "cross_only",
        "image_path": row["image_path"],
        "image_abs": str(image),
        "captured_at": row.get("captured_at"),
        "historical_status": row.get("status"),
        "historical_template": row.get("historical_template"),
        "template_new": sheet_data.get("template_name") or row.get("historical_template"),
        "duration_sec": round(time.perf_counter() - started, 3),
        "rows_old": len(historical_sheet.get("rows") or historical_raw.get("rows") or []),
        "rows_new": len(sheet_data.get("rows") or []),
        "auto_overwrites": applied,
        "auto_overwrite_count": len(applied),
        "counts": dict(counts),
        "nonempty_rows_with_na": nonempty_row_na_violations(sheet_data, final_cross),
        "historical": {
            "raw_extraction": historical_raw,
            "sheet_data": historical_sheet,
            "cross": historical_cross,
            "cross_summary": historical_cross.get("summary") or {},
        },
        "raw_new": raw_input,
        "dq_new": dq_input,
        "sheet_data_final": sheet_data,
        "cross_final": final_cross,
    }


_WORKER_DATASET: Path | None = None
_WORKER_RUNTIME: SimpleNamespace | None = None


def _init_cross_worker(dataset: str) -> None:
    global _WORKER_DATASET, _WORKER_RUNTIME
    _WORKER_DATASET = Path(dataset)
    _WORKER_RUNTIME = load_runtime(include_ocr=False)


def _run_sheet_cross_only_worker(row: dict) -> dict:
    if _WORKER_DATASET is None or _WORKER_RUNTIME is None:
        raise RuntimeError("Cross-only worker was not initialized")
    started = time.perf_counter()
    try:
        return {
            "kind": "result",
            "record": run_sheet_cross_only(row, _WORKER_DATASET, _WORKER_RUNTIME),
        }
    except Exception as exc:  # pragma: no cover - exercised by CLI integration
        return {
            "kind": "error",
            "record": {
                "sheet_id": int(row["id"]),
                "ok": False,
                "image_path": row.get("image_path"),
                "historical_status": row.get("status"),
                "historical_template": row.get("historical_template"),
                "duration_sec": round(time.perf_counter() - started, 3),
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        }


def _summary_value(summary: dict, *keys: str) -> int:
    total = 0
    for key in keys:
        try:
            total += int(summary.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def build_reports(output_dir: Path) -> dict:
    results = _read_jsonl(output_dir / "results.jsonl")
    errors = _read_jsonl(output_dir / "errors.jsonl")
    field_counts: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    template_counts: dict[str, Counter[str]] = defaultdict(Counter)

    for rec in results:
        tpl = str(rec.get("template_new") or rec.get("historical_template") or "<none>")
        template_counts[tpl]["sheets"] += 1
        for key, val in (rec.get("counts") or {}).items():
            template_counts[tpl][key] += int(val or 0)
        for section, _idx, field, cell in iter_cross_cells(rec.get("cross_final") or {}):
            field_counts[(section, field, cell_category(cell))]["n"] += 1

    write_summary_csv(output_dir / "summary.csv", results, errors)
    write_field_coverage_csv(output_dir / "field_coverage.csv", field_counts)
    write_template_coverage_csv(output_dir / "template_coverage.csv", template_counts)
    write_review_pack(output_dir / "review_pack.html", results, errors)

    total_counts: Counter[str] = Counter()
    for rec in results:
        for key, val in (rec.get("counts") or {}).items():
            total_counts[key] += int(val or 0)
    summary = {
        "ok": len(results),
        "errors": len(errors),
        "counts": dict(total_counts),
        "na_pct": round(100.0 * total_counts["NA"] / total_counts["TOTAL"], 2)
        if total_counts["TOTAL"]
        else None,
        "outputs": {
            "summary_csv": str(output_dir / "summary.csv"),
            "field_coverage_csv": str(output_dir / "field_coverage.csv"),
            "template_coverage_csv": str(output_dir / "template_coverage.csv"),
            "review_pack_html": str(output_dir / "review_pack.html"),
        },
    }
    _write_json(output_dir / "run_summary.json", summary)
    return summary


def write_summary_csv(path: Path, results: list[dict], errors: list[dict]) -> None:
    fields = [
        "sheet_id",
        "ok",
        "historical_status",
        "historical_template",
        "template_new",
        "duration_sec",
        "rows_old",
        "rows_new",
        "old_match_like",
        "old_no_match",
        "old_na",
        "old_total",
        "new_match",
        "new_match_regra",
        "new_no_match",
        "new_na",
        "new_total",
        "auto_overwrite_count",
        "nonempty_rows_with_na",
        "error",
        "image_path",
    ]
    error_by_id = {int(e["sheet_id"]): e for e in errors if "sheet_id" in e}
    rows: list[dict] = []
    for rec in results:
        old = (rec.get("historical") or {}).get("cross_summary") or {}
        counts = Counter(rec.get("counts") or {})
        rows.append(
            {
                "sheet_id": rec.get("sheet_id"),
                "ok": True,
                "historical_status": rec.get("historical_status"),
                "historical_template": rec.get("historical_template"),
                "template_new": rec.get("template_new"),
                "duration_sec": rec.get("duration_sec"),
                "rows_old": rec.get("rows_old"),
                "rows_new": rec.get("rows_new"),
                "old_match_like": _summary_value(old, "match", "confirmed", "snapped"),
                "old_no_match": _summary_value(old, "no_match", "very_different"),
                "old_na": _summary_value(old, "na"),
                "old_total": _summary_value(old, "total"),
                "new_match": counts["MATCH"],
                "new_match_regra": counts["MATCH_REGRA"],
                "new_no_match": counts["NO_MATCH"],
                "new_na": counts["NA"],
                "new_total": counts["TOTAL"],
                "auto_overwrite_count": rec.get("auto_overwrite_count"),
                "nonempty_rows_with_na": len(rec.get("nonempty_rows_with_na") or []),
                "error": "",
                "image_path": rec.get("image_path"),
            }
        )
    for sid, err in sorted(error_by_id.items()):
        rows.append(
            {
                "sheet_id": sid,
                "ok": False,
                "historical_status": err.get("historical_status"),
                "historical_template": err.get("historical_template"),
                "template_new": "",
                "duration_sec": err.get("duration_sec"),
                "rows_old": "",
                "rows_new": "",
                "old_match_like": "",
                "old_no_match": "",
                "old_na": "",
                "old_total": "",
                "new_match": "",
                "new_match_regra": "",
                "new_no_match": "",
                "new_na": "",
                "new_total": "",
                "auto_overwrite_count": "",
                "nonempty_rows_with_na": "",
                "error": err.get("error"),
                "image_path": err.get("image_path"),
            }
        )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: int(r["sheet_id"] or 0)))


def write_field_coverage_csv(
    path: Path,
    field_counts: dict[tuple[str, str, str], Counter[str]],
) -> None:
    rows = []
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for (section, field, category), counter in field_counts.items():
        grouped[(section, field)][category] += counter["n"]
    for (section, field), counts in sorted(grouped.items()):
        total = sum(counts.values())
        rows.append(
            {
                "section": section,
                "field": field,
                "match": counts["MATCH"],
                "match_regra": counts["MATCH_REGRA"],
                "no_match": counts["NO_MATCH"],
                "na": counts["NA"],
                "total": total,
                "na_pct": round(100.0 * counts["NA"] / total, 2) if total else 0,
            }
        )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "section",
                "field",
                "match",
                "match_regra",
                "no_match",
                "na",
                "total",
                "na_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def write_template_coverage_csv(path: Path, template_counts: dict[str, Counter[str]]) -> None:
    rows = []
    for tpl, counts in sorted(template_counts.items()):
        total = counts["TOTAL"]
        rows.append(
            {
                "template": tpl,
                "sheets": counts["sheets"],
                "match": counts["MATCH"],
                "match_regra": counts["MATCH_REGRA"],
                "no_match": counts["NO_MATCH"],
                "na": counts["NA"],
                "total": total,
                "na_pct": round(100.0 * counts["NA"] / total, 2) if total else 0,
            }
        )
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "template",
                "sheets",
                "match",
                "match_regra",
                "no_match",
                "na",
                "total",
                "na_pct",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _preview_json(value: Any, max_chars: int = 2600) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n..."
    return html.escape(text)


def write_review_pack(
    path: Path,
    results: list[dict],
    errors: list[dict],
    max_items: int = 40,
) -> None:
    by_id = {int(rec["sheet_id"]): rec for rec in results}
    chosen: list[dict] = []
    for sid in CRITICAL_SHEET_IDS:
        if sid in by_id:
            chosen.append(by_id[sid])
    top = sorted(
        results,
        key=lambda rec: (
            Counter(rec.get("counts") or {})["NA"],
            len(rec.get("nonempty_rows_with_na") or []),
        ),
        reverse=True,
    )
    for rec in top:
        if rec not in chosen:
            chosen.append(rec)
        if len(chosen) >= max_items:
            break

    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>Mass OCR Replay Review Pack</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:24px;"
        "background:#faf9f6;color:#191815}"
        "section{border:1px solid #d7d0c3;border-radius:8px;margin:18px 0;"
        "padding:16px;background:white}"
        "img{max-width:360px;max-height:280px;border:1px solid #ddd}"
        "pre{white-space:pre-wrap;background:#f4f1eb;padding:12px;border-radius:6px;"
        "max-height:360px;overflow:auto}"
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px 7px}</style>",
        "<h1>Mass OCR Replay Review Pack</h1>",
        f"<p>OK: {len(results)} | Erros: {len(errors)}</p>",
    ]
    if errors:
        parts.append("<h2>Erros</h2><table><tr><th>Sheet</th><th>Erro</th></tr>")
        for err in errors[:30]:
            parts.append(
                "<tr><td>{}</td><td>{}</td></tr>".format(
                    html.escape(str(err.get("sheet_id"))),
                    html.escape(str(err.get("error"))),
                )
            )
        parts.append("</table>")
    for rec in chosen:
        counts = Counter(rec.get("counts") or {})
        image_uri = Path(rec.get("image_abs") or "").resolve().as_uri()
        old = rec.get("historical") or {}
        parts.append("<section>")
        parts.append(
            f"<h2>Folha #{html.escape(str(rec.get('sheet_id')))} "
            f"({html.escape(str(rec.get('historical_template')))} -> "
            f"{html.escape(str(rec.get('template_new')))})</h2>"
        )
        parts.append(f"<img src='{html.escape(image_uri)}'>")
        parts.append(
            "<p>"
            f"MATCH={counts['MATCH']} | MATCH_REGRA={counts['MATCH_REGRA']} | "
            f"NO_MATCH={counts['NO_MATCH']} | NA={counts['NA']} | "
            f"Auto={html.escape(str(rec.get('auto_overwrite_count')))}"
            "</p>"
        )
        if rec.get("nonempty_rows_with_na"):
            na_preview = _preview_json(rec.get("nonempty_rows_with_na"), 1200)
            parts.append(f"<pre>Linhas com NA: {na_preview}</pre>")
        parts.append("<h3>OCR antigo</h3>")
        parts.append(f"<pre>{_preview_json(old.get('raw_extraction'))}</pre>")
        parts.append("<h3>Entrada testada</h3>")
        parts.append(f"<pre>{_preview_json(rec.get('raw_new'))}</pre>")
        parts.append("<h3>Cross antigo summary</h3>")
        parts.append(f"<pre>{_preview_json(old.get('cross_summary'))}</pre>")
        parts.append("<h3>Cross novo summary</h3>")
        parts.append(f"<pre>{_preview_json((rec.get('cross_final') or {}).get('summary'))}</pre>")
        parts.append("</section>")
    path.write_text("\n".join(parts), encoding="utf-8")


def resolve_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    if args.resume and REPORTS_BASE.exists():
        runs = sorted(p for p in REPORTS_BASE.iterdir() if p.is_dir())
        if runs:
            return runs[-1]
    return REPORTS_BASE / _now_id()


def completed_sheet_ids(output_dir: Path) -> set[int]:
    done = set()
    for rec in _read_jsonl(output_dir / "results.jsonl"):
        try:
            done.add(int(rec["sheet_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return done


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sheet-id", type=int, action="append", default=[])
    parser.add_argument("--critical-smoke", action="store_true")
    parser.add_argument("--sample-per-template", type=int, default=None)
    parser.add_argument("--cross-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--preflight-timeout-sec", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.concurrency < 1:
        print("--concurrency must be >= 1", file=sys.stderr)
        return 2
    if args.concurrency != 1 and not args.cross_only:
        print(
            "Only --concurrency 1 is supported; the OCR runner uses a global prompt lock.",
            file=sys.stderr,
        )
        return 2

    dataset = args.dataset.resolve()
    if not dataset.exists():
        print(f"Dataset not found: {dataset}", file=sys.stderr)
        return 2

    load_dotenv(REPO / ".env")
    os.environ.setdefault("OCR_NO_THINK", "1")

    preflight = {"skipped": True, "reason": "cross_only"} if args.cross_only else None
    if not args.cross_only:
        try:
            preflight = preflight_ollama(timeout_sec=args.preflight_timeout_sec)
        except RuntimeError as exc:
            print(f"PRE-FLIGHT FAILED: {exc}", file=sys.stderr)
            return 2

    try:
        runtime = load_runtime(include_ocr=not args.cross_only)
    except RuntimeError as exc:
        print(f"REFS FAILED: {exc}", file=sys.stderr)
        return 2

    output_dir = resolve_output_dir(args)
    output_dir.mkdir(parents=True, exist_ok=True)

    sheet_ids = list(args.sheet_id)
    if args.critical_smoke:
        sheet_ids.extend(CRITICAL_SHEET_IDS)
    sheets = load_dataset_sheets(
        dataset,
        sheet_ids=sheet_ids or None,
        sample_per_template=args.sample_per_template,
        limit=args.limit,
        require_extraction=args.cross_only,
    )
    done_ids = completed_sheet_ids(output_dir) if args.resume else set()
    sheets_to_run = [s for s in sheets if int(s["id"]) not in done_ids]

    manifest = {
        "run_id": output_dir.name,
        "mode": "cross_only" if args.cross_only else "ocr_replay",
        "started_at": _now_iso(),
        "dataset": str(dataset),
        "output_dir": str(output_dir),
        "git_sha": git_sha(),
        "engine_version": runtime.engine_version,
        "env": {
            "OLLAMA_URL": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
            "OCR_MODEL": os.environ.get("OCR_MODEL", DEFAULT_MODEL),
            "OCR_NO_THINK": os.environ.get("OCR_NO_THINK"),
        },
        "preflight": preflight,
        "refs_snapshot": runtime.refs_snapshot,
        "dataset_profile": dataset_profile(dataset),
        "selection": {
            "requested_sheets": len(sheets),
            "already_done": len(done_ids),
            "to_run": len(sheets_to_run),
            "concurrency": args.concurrency,
            "sheet_ids": sheet_ids,
            "limit": args.limit,
            "sample_per_template": args.sample_per_template,
            "cross_only": args.cross_only,
        },
    }
    _write_json(output_dir / "run_manifest.json", manifest)

    run_label = "cross-only" if args.cross_only else "OCR"
    print(f"Mass {run_label} replay: {len(sheets_to_run)} sheet(s)", flush=True)
    print(f"Mode:    {'cross-only' if args.cross_only else 'ocr-replay'}", flush=True)
    print(f"Dataset: {dataset}", flush=True)
    print(f"Output:  {output_dir}", flush=True)
    if args.cross_only:
        print("OCR:     skipped", flush=True)
    else:
        print(f"OCR:     {preflight['url']} / {preflight['model']}", flush=True)

    def store_payload(payload: dict, pos: int, sid: int) -> None:
        if payload.get("kind") == "result":
            rec = payload["record"]
            _append_jsonl(output_dir / "results.jsonl", rec)
            counts = Counter(rec.get("counts") or {})
            print(
                f"[{pos}/{len(sheets_to_run)}] sheet={sid} "
                f"tpl={rec.get('template_new')} rows={rec.get('rows_new')} "
                f"M={counts['MATCH']} MR={counts['MATCH_REGRA']} "
                f"NM={counts['NO_MATCH']} NA={counts['NA']} "
                f"auto={rec.get('auto_overwrite_count')} "
                f"{rec.get('duration_sec')}s",
                flush=True,
            )
            return

        err = payload["record"]
        _append_jsonl(output_dir / "errors.jsonl", err)
        print(
            f"[{pos}/{len(sheets_to_run)}] sheet={sid} "
            f"FAIL {err.get('error_type')}: {err.get('error')}",
            flush=True,
        )

    if args.cross_only and args.concurrency > 1:
        with ProcessPoolExecutor(
            max_workers=args.concurrency,
            initializer=_init_cross_worker,
            initargs=(str(dataset),),
        ) as executor:
            future_meta = {
                executor.submit(_run_sheet_cross_only_worker, row): (i, int(row["id"]))
                for i, row in enumerate(sheets_to_run, 1)
            }
            completed = 0
            for future in as_completed(future_meta):
                original_pos, sid = future_meta[future]
                completed += 1
                try:
                    payload = future.result()
                except Exception as exc:  # pragma: no cover - worker bootstrap failure
                    payload = {
                        "kind": "error",
                        "record": {
                            "sheet_id": sid,
                            "ok": False,
                            "duration_sec": None,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    }
                store_payload(payload, completed, sid)
                if original_pos != completed:
                    print(
                        f"      input_order={original_pos}/{len(sheets_to_run)}",
                        flush=True,
                    )
    else:
        for pos, row in enumerate(sheets_to_run, 1):
            sid = int(row["id"])
            started = time.perf_counter()
            try:
                if args.cross_only:
                    rec = run_sheet_cross_only(row, dataset, runtime)
                else:
                    rec = run_sheet(row, dataset, runtime)
                payload = {"kind": "result", "record": rec}
            except Exception as exc:
                payload = {
                    "kind": "error",
                    "record": {
                        "sheet_id": sid,
                        "ok": False,
                        "image_path": row.get("image_path"),
                        "historical_status": row.get("status"),
                        "historical_template": row.get("historical_template"),
                        "duration_sec": round(time.perf_counter() - started, 3),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                }
            store_payload(payload, pos, sid)

    summary = build_reports(output_dir)
    manifest["finished_at"] = _now_iso()
    manifest["final_summary"] = summary
    _write_json(output_dir / "run_manifest.json", manifest)

    print("Reports:", flush=True)
    for value in summary["outputs"].values():
        print(f"  {value}", flush=True)
    print(
        f"Final: ok={summary['ok']} errors={summary['errors']} counts={summary['counts']}",
        flush=True,
    )
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
