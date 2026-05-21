"""Cross-check storage — write per-sheet JSON + aggregates to disk.

Layout:
    C:\\kanban\\nifruka\\03_Cross_Check\\
    ├── _summary.json           # aggregate: totals, per-day, per-operador
    ├── _to_analisar.json       # flat list of cells needing human review
    ├── _index.json             # 1-line per sheet: id, op, date, status, ok_rate
    └── 2026-04-15\\
        ├── JulioLima_2026.04.15_sheet2.json
        └── ...

Atomicity: write to ``.tmp`` then rename. Safe across concurrent /upload
+ /edit calls (rare in practice with 1 user).

Read API:
- ``load_summary()`` → dict (for /admin/refs-status + dashboards)
- ``load_to_analisar(limit=20)`` → list (inbox of cells needing review)
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# R118 — fallback inteligente igual ao de ref_watcher. Sem este fix, em
# laptop dev sem `.env`, o JSON de cross-check ia parar a C:\kanban\... e
# o `load_sheet_cross_check` para folhas locais devolvia None.
from app.config import resolve_kanban_path

_lock = threading.Lock()


def _base_dir() -> Path:
    """Resolve from env or smart default. Created lazily."""
    p = resolve_kanban_path(
        "CROSS_CHECK_DIR",
        r"C:\kanban\nifruka\03_Cross_Check",
        "kanban_refs/03_Cross_Check",
    )
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, content: str) -> None:
    """Write to .tmp + rename. Keeps readers from seeing half-written files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _slug(s: str) -> str:
    """Turn an operator name into a filename-safe slug.
    'JÚLIO LIMA' → 'JulioLima' (also handles spaces, accents).
    """
    if not s:
        return "_"
    import unicodedata
    decomposed = unicodedata.normalize("NFKD", str(s))
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(part.capitalize() for part in no_marks.split() if part).strip() or "_"


def _iso_to_pt(d: str) -> str:
    """YYYY-MM-DD → DD-MM-YYYY (for filenames matching factory convention)."""
    if not d or len(d) != 10:
        return d or "no-date"
    return f"{d[8:10]}-{d[5:7]}-{d[0:4]}"


def _sheet_filename(sheet_id: int, operador: str, date_iso: str) -> tuple[Path, str]:
    """Return (full_path, relative_index_key) for the per-sheet JSON file."""
    base = _base_dir()
    folder = date_iso or "no-date"
    name = f"{_slug(operador)}_{_iso_to_pt(date_iso)}_sheet{sheet_id}.json"
    return base / folder / name, f"{folder}/{name}"


def store_cross_check(
    sheet_id: int,
    image_path: str,
    operador: str,
    date_iso: str,
    sheet_status: str,
    cross_check_result: dict,
) -> dict:
    """Persist the per-sheet cross-check + update aggregate files.
    Returns the relative index key (for storage in DB if needed).

    Idempotent: re-calling overwrites the per-sheet file + recomputes
    aggregates from the index. Safe across concurrent calls (lock).
    """
    file_path, rel_key = _sheet_filename(sheet_id, operador, date_iso)

    summary = cross_check_result.get("summary", {})
    to_analisar = cross_check_result.get("to_analisar", [])

    payload = {
        "sheet_id": sheet_id,
        "image_path": image_path,
        "operador": operador,
        "date": date_iso,
        "sheet_status": sheet_status,
        "checked_at": cross_check_result.get("checked_at"),
        "refs_loaded_at": cross_check_result.get("refs_loaded_at"),
        "summary": summary,
        "rows": cross_check_result.get("rows", []),
        "to_analisar": to_analisar,
    }

    with _lock:
        _atomic_write(file_path, json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        _update_index(sheet_id, operador, date_iso, sheet_status, summary, rel_key)
        _update_summary()
        _update_to_analisar()

    return {"file": str(file_path), "rel_key": rel_key}


def remove_sheet_cross_check(sheet_id: int) -> None:
    """Remove a sheet's cross-check files + index entry. Used when sheet
    is deleted (we don't currently expose this, but useful to keep clean)."""
    with _lock:
        idx = _read_index()
        entry = idx.get(str(sheet_id))
        if entry:
            try:
                (_base_dir() / entry["file"]).unlink(missing_ok=True)
            except OSError:
                pass
            idx.pop(str(sheet_id), None)
            _write_index(idx)
            _update_summary()
            _update_to_analisar()


# --- Index helpers (keyed by sheet_id) ---

def _index_path() -> Path:
    return _base_dir() / "_index.json"


def _read_index() -> dict[str, dict]:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_index(idx: dict[str, dict]) -> None:
    _atomic_write(_index_path(), json.dumps({
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "sheets": idx,
    }, indent=2, ensure_ascii=False))


def _update_index(
    sheet_id: int,
    operador: str,
    date_iso: str,
    sheet_status: str,
    summary: dict,
    rel_key: str,
) -> None:
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    total = summary.get("total", 0) or 1
    ok = summary.get("ok", 0) + summary.get("corrigido", 0) + summary.get("preenchido", 0)
    sheets[str(sheet_id)] = {
        "sheet_id": sheet_id,
        "operador": operador,
        "date": date_iso,
        "sheet_status": sheet_status,
        "summary": summary,
        "ok_rate": round(ok / total, 3) if total else 0,
        "file": rel_key,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    _write_index(sheets)


# --- Aggregate _summary.json ---

def _summary_path() -> Path:
    return _base_dir() / "_summary.json"


def _update_summary() -> None:
    """Recompute totals + per-day + per-operador from the index."""
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw

    # Round 33: simplified statuses (MATCH/NO_MATCH/NA)
    totals = {"match": 0, "no_match": 0, "na": 0, "total": 0}
    by_day: dict[str, dict] = defaultdict(lambda: dict(totals))
    by_op: dict[str, dict] = defaultdict(lambda: dict(totals))

    for entry in sheets.values():
        s = entry.get("summary", {})
        for k in totals:
            totals[k] += s.get(k, 0)
        d = entry.get("date") or "no-date"
        for k in totals:
            by_day[d][k] += s.get(k, 0)
        op = entry.get("operador") or "_"
        for k in totals:
            by_op[op][k] += s.get(k, 0)

    summary = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "totals": totals,
        "by_day": {d: dict(v) for d, v in sorted(by_day.items())},
        "by_operador": {o: dict(v) for o, v in sorted(by_op.items())},
        "n_sheets": len(sheets),
    }
    _atomic_write(_summary_path(), json.dumps(summary, indent=2, ensure_ascii=False))


# --- _to_analisar.json (flat inbox) ---

def _to_analisar_path() -> Path:
    return _base_dir() / "_to_analisar.json"


def _update_to_analisar() -> None:
    """Aggregate all ANALISAR cells across all sheets into a flat list."""
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    all_items: list[dict] = []
    for entry in sheets.values():
        sheet_file = _base_dir() / entry["file"]
        if not sheet_file.exists():
            continue
        try:
            data = json.loads(sheet_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("to_analisar", []):
            all_items.append({
                "sheet_id": entry["sheet_id"],
                "operador": entry["operador"],
                "date": entry["date"],
                "row_index": item.get("row_index"),
                "field": item.get("field"),
                "value": item.get("value"),
                "plan_value": item.get("plan_value"),
                "reason": item.get("reason"),
            })
    out = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "total": len(all_items),
        "items": all_items,
    }
    _atomic_write(_to_analisar_path(), json.dumps(out, indent=2, ensure_ascii=False))


# --- Read API ---

def load_summary() -> dict:
    p = _summary_path()
    if not p.exists():
        return {"totals": {"total": 0}, "by_day": {}, "by_operador": {}, "n_sheets": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"totals": {"total": 0}, "by_day": {}, "by_operador": {}, "n_sheets": 0}


def load_to_analisar(limit: int | None = None) -> dict:
    p = _to_analisar_path()
    if not p.exists():
        return {"total": 0, "items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if limit is not None and len(data.get("items", [])) > limit:
            data["items"] = data["items"][:limit]
        return data
    except json.JSONDecodeError:
        return {"total": 0, "items": []}


def iter_sheet_cross_checks() -> list[dict]:
    """Read every per-sheet cross-check JSON listed in the index.

    Returns the full per-sheet payloads (sheet_id, operador, date, summary,
    rows, header, footer, to_analisar, ...). Used by the attractors module
    to aggregate real MATCH/NO_MATCH counts per field/template/operador.
    Missing or corrupt files are skipped silently.
    """
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    out: list[dict] = []
    for entry in sheets.values():
        sheet_file = _base_dir() / entry["file"]
        if not sheet_file.exists():
            continue
        try:
            out.append(json.loads(sheet_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def load_sheet_cross_check(sheet_id: int) -> dict | None:
    """Read the per-sheet cross-check JSON. Returns None if not found."""
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    entry = sheets.get(str(sheet_id))
    if not entry:
        return None
    sheet_file = _base_dir() / entry["file"]
    if not sheet_file.exists():
        return None
    try:
        return json.loads(sheet_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
