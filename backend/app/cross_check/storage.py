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
- ``load_sheet_cross_check(sheet_id)`` → current-engine sheet JSON, or None
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# R118 — fallback inteligente igual ao de ref_watcher. Sem este fix, em
# laptop dev sem `.env`, o JSON de cross-check ia parar a C:\kanban\... e
# o `load_sheet_cross_check` para folhas locais devolvia None.
from app.config import resolve_kanban_path

_lock = threading.Lock()


def _current_engine_version() -> str:
    try:
        from app.pipeline.scoring_engine import ENGINE_VERSION
    except Exception:
        return ""
    return ENGINE_VERSION


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

    engine_version = cross_check_result.get("engine_version") or _current_engine_version()
    # dv — estado do motor por folha (auditoria EN1090): o env pode ligar/
    # desligar o voto declarado SEM mudar engine_version; cada resultado
    # gravado carrega o modo que o produziu. Leitura tolerante a jusante
    # (chave ausente em JSONs antigos = off histórico).
    try:
        from app.config import get_settings
        declared_vote_mode = str(get_settings().cross_declared_vote or "off")
    except Exception:
        declared_vote_mode = "off"
    payload = {
        "sheet_id": sheet_id,
        "image_path": image_path,
        "operador": operador,
        "date": date_iso,
        "sheet_status": sheet_status,
        "checked_at": cross_check_result.get("checked_at"),
        "refs_loaded_at": cross_check_result.get("refs_loaded_at"),
        "refs_snapshot": cross_check_result.get("refs_snapshot") or {},
        # R123 — versão do motor; o viewer regenera JSONs de versões antigas.
        "engine_version": engine_version,
        "declared_vote_mode": declared_vote_mode,
        "summary": summary,
        "rows": cross_check_result.get("rows", []),
        # R123 (B9) — header/footer validados passam a ser persistidos
        # para o viewer os poder colorir (_build_cc_maps lê-os daqui).
        "header": cross_check_result.get("header", {}),
        "footer": cross_check_result.get("footer", {}),
        "to_analisar": to_analisar,
    }

    with _lock:
        _atomic_write(file_path, json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        _update_index(
            sheet_id, operador, date_iso, sheet_status, summary, rel_key,
            engine_version=engine_version,
        )
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


def _summary_count(summary: dict, key: str) -> float:
    try:
        return float(summary.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _ok_rate(summary: dict) -> float:
    """MATCH rate over comparable cells only; NA is not an error."""
    ok = _summary_count(summary, "match")
    comparable = ok + _summary_count(summary, "no_match")
    return round(ok / comparable, 3) if comparable else 0


def _normalise_index_sheets(idx: dict[str, dict]) -> dict[str, dict]:
    sheets: dict[str, dict] = {}
    for sid, entry in idx.items():
        if not isinstance(entry, dict):
            continue
        cleaned = dict(entry)
        summary = cleaned.get("summary", {}) or {}
        if isinstance(summary, dict):
            cleaned["summary"] = _normalise_summary_counts(summary)
        cleaned["ok_rate"] = _ok_rate(cleaned.get("summary", {}) or {})
        if "engine_version" not in cleaned and "engine" in cleaned:
            cleaned["engine_version"] = cleaned.get("engine")
        sheets[str(sid)] = cleaned
    return sheets


def _normalise_summary_counts(summary: dict | None) -> dict:
    src = summary if isinstance(summary, dict) else {}
    return {
        **src,
        "match": int(_summary_count(src, "match")),
        "no_match": int(_summary_count(src, "no_match")),
        "na": int(_summary_count(src, "na")),
        "total": int(_summary_count(src, "total")),
    }


def _normalise_to_analisar_item(item: dict) -> dict:
    section = item.get("section") or "rows"
    row_index = item.get("row_index")
    field = item.get("field")
    field_path = item.get("field_path")
    if not field_path and field:
        if section == "rows":
            field_path = f"rows[{row_index}].{field}"
        else:
            field_path = f"{section}.{field}"
    ref_value = item.get("ref")
    if ref_value in (None, ""):
        ref_value = item.get("plan_value")
    return {
        **item,
        "section": section,
        "row_index": row_index,
        "field": field,
        "field_path": field_path,
        "ref_value": ref_value,
        # Compatibilidade: consumidores antigos ainda leem `plan_value`.
        # Para fontes como maquinas/colaboradores/syntax, `ref_value` é o
        # nome semanticamente correto.
        "plan_value": ref_value,
        "ref_source": item.get("ref_source") or item.get("source"),
    }


def _read_index() -> dict[str, dict]:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(raw, dict) and "sheets" in raw:
        raw = raw.get("sheets", {})
    if not isinstance(raw, dict):
        return {}
    return _normalise_index_sheets(raw)


def _write_index(idx: dict[str, dict]) -> None:
    sheets = _normalise_index_sheets(idx)
    _atomic_write(_index_path(), json.dumps({
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "sheets": sheets,
    }, indent=2, ensure_ascii=False))


def _update_index(
    sheet_id: int,
    operador: str,
    date_iso: str,
    sheet_status: str,
    summary: dict,
    rel_key: str,
    *,
    engine_version: str | None = None,
) -> None:
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    sheets[str(sheet_id)] = {
        "sheet_id": sheet_id,
        "operador": operador,
        "date": date_iso,
        "sheet_status": sheet_status,
        "summary": summary,
        "engine_version": engine_version or _current_engine_version(),
        # R143 — ok_rate mede apenas células comparáveis. `NA` significa sem
        # referência e não deve penalizar a taxa.
        "ok_rate": _ok_rate(summary),
        "file": rel_key,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }
    _write_index(sheets)


# --- Aggregate _summary.json ---

def _summary_path() -> Path:
    return _base_dir() / "_summary.json"


def _current_engine_entries(sheets: dict[str, dict]) -> tuple[dict[str, dict], int]:
    current = _current_engine_version()
    if not current:
        return sheets, 0
    active = {
        sid: entry for sid, entry in sheets.items()
        if entry.get("engine_version") == current
    }
    return active, max(0, len(sheets) - len(active))


def _load_sheet_payload(entry: dict, *, include_stale: bool = False) -> dict | None:
    current = _current_engine_version()
    if not include_stale and current and entry.get("engine_version") != current:
        return None
    rel_file = entry.get("file")
    if not rel_file:
        return None
    sheet_file = _base_dir() / rel_file
    if not sheet_file.exists():
        return None
    try:
        payload = json.loads(sheet_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    if not include_stale and current and payload.get("engine_version") != current:
        return None
    expected_sheet_id = entry.get("sheet_id")
    payload_sheet_id = payload.get("sheet_id")
    if (
        not include_stale
        and expected_sheet_id is not None
        and payload_sheet_id is not None
        and str(payload_sheet_id) != str(expected_sheet_id)
    ):
        return None
    return payload


def _build_summary_from_index(sheets: dict[str, dict]) -> dict:
    # Round 33: simplified statuses (MATCH/NO_MATCH/NA)
    totals = {"match": 0, "no_match": 0, "na": 0, "total": 0}
    by_day: dict[str, dict] = defaultdict(lambda: dict(totals))
    by_op: dict[str, dict] = defaultdict(lambda: dict(totals))
    active, stale = _current_engine_entries(sheets)
    n_valid = 0

    for entry in active.values():
        payload = _load_sheet_payload(entry)
        if payload is None:
            stale += 1
            continue
        s = payload.get("summary") or entry.get("summary", {})
        n_valid += 1
        for k in totals:
            totals[k] += int(_summary_count(s, k))
        d = entry.get("date") or "no-date"
        for k in totals:
            by_day[d][k] += int(_summary_count(s, k))
        op = entry.get("operador") or "_"
        for k in totals:
            by_op[op][k] += int(_summary_count(s, k))

    summary = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "engine_version": _current_engine_version(),
        "totals": totals,
        "by_day": {d: dict(v) for d, v in sorted(by_day.items())},
        "by_operador": {o: dict(v) for o, v in sorted(by_op.items())},
        "n_sheets": n_valid,
        "stale_sheets": stale,
    }
    return summary


def _update_summary() -> None:
    """Recompute totals + per-day + per-operador from current-engine index entries."""
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    summary = _build_summary_from_index(sheets)
    _atomic_write(_summary_path(), json.dumps(summary, indent=2, ensure_ascii=False))


# --- _to_analisar.json (flat inbox) ---

def _to_analisar_path() -> Path:
    return _base_dir() / "_to_analisar.json"


def _update_to_analisar() -> None:
    """Aggregate all NO_MATCH cells across all sheets into a flat list."""
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    out = _build_to_analisar_from_index(sheets)
    _atomic_write(_to_analisar_path(), json.dumps(out, indent=2, ensure_ascii=False))


def _build_to_analisar_from_index(sheets: dict[str, dict]) -> dict:
    active, stale = _current_engine_entries(sheets)
    all_items: list[dict] = []
    for entry in active.values():
        data = _load_sheet_payload(entry)
        if data is None:
            stale += 1
            continue
        for item in data.get("to_analisar", []):
            norm_item = _normalise_to_analisar_item(item)
            all_items.append({
                "sheet_id": entry.get("sheet_id") or data.get("sheet_id"),
                "operador": entry.get("operador") or data.get("operador") or "_",
                "date": entry.get("date") or data.get("date") or "no-date",
                "engine_version": entry.get("engine_version"),
                "section": norm_item.get("section"),
                "row_index": norm_item.get("row_index"),
                "field": norm_item.get("field"),
                "field_path": norm_item.get("field_path"),
                "value": norm_item.get("value"),
                "ref_value": norm_item.get("ref_value"),
                # R123 (G2) — o item antigo do to_analisar usa a chave `ref`,
                # não `plan_value` (que vinha sempre None). Mantemos
                # `plan_value` como alias compatível, mas `ref_value` é o
                # nome correto para refs de plan/SAP/máquinas/colaboradores.
                "plan_value": norm_item.get("plan_value"),
                "ref_source": norm_item.get("ref_source"),
                "reason": norm_item.get("reason"),
            })
    out = {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "engine_version": _current_engine_version(),
        "total": len(all_items),
        "stale_sheets": stale,
        "items": all_items,
    }
    return out


# --- Read API ---

def _empty_summary(*, stale_sheets: int = 0) -> dict:
    return {
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "engine_version": _current_engine_version(),
        "totals": {"match": 0, "no_match": 0, "na": 0, "total": 0},
        "by_day": {},
        "by_operador": {},
        "n_sheets": 0,
        "stale_sheets": int(stale_sheets or 0),
    }


def load_summary() -> dict:
    raw_idx = _read_index()
    sheets = raw_idx.get("sheets", raw_idx) if isinstance(raw_idx, dict) and "sheets" in raw_idx else raw_idx
    if sheets:
        return _build_summary_from_index(sheets)
    p = _summary_path()
    if not p.exists():
        return _empty_summary()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_summary()
    if not isinstance(data, dict):
        return _empty_summary()
    current = _current_engine_version()
    if current and data.get("engine_version") not in (None, current):
        return _empty_summary(
            stale_sheets=int(_summary_count({"n_sheets": data.get("n_sheets")}, "n_sheets"))
        )
    data["totals"] = _normalise_summary_counts(data.get("totals"))
    data["by_day"] = {
        day: _normalise_summary_counts(counts)
        for day, counts in (data.get("by_day") or {}).items()
        if isinstance(counts, dict)
    }
    data["by_operador"] = {
        op: _normalise_summary_counts(counts)
        for op, counts in (data.get("by_operador") or {}).items()
        if isinstance(counts, dict)
    }
    try:
        data["n_sheets"] = int(data.get("n_sheets", 0) or 0)
    except (TypeError, ValueError):
        data["n_sheets"] = 0
    try:
        data["stale_sheets"] = int(data.get("stale_sheets", 0) or 0)
    except (TypeError, ValueError):
        data["stale_sheets"] = 0
    data.setdefault("engine_version", _current_engine_version())
    return data


def load_to_analisar(limit: int | None = None) -> dict:
    raw_idx = _read_index()
    sheets = raw_idx.get("sheets", raw_idx) if isinstance(raw_idx, dict) and "sheets" in raw_idx else raw_idx
    if sheets:
        data = _build_to_analisar_from_index(sheets)
        if limit is not None and len(data.get("items", [])) > limit:
            data["items"] = data["items"][:limit]
        return data
    p = _to_analisar_path()
    if not p.exists():
        return {"total": 0, "items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"total": 0, "items": []}
        current = _current_engine_version()
        if current and data.get("engine_version") not in (None, current):
            return {
                "engine_version": current,
                "total": 0,
                "stale_sheets": int(_summary_count({"total": data.get("total")}, "total")),
                "items": [],
            }
        items = [
            _normalise_to_analisar_item(item)
            for item in (data.get("items", []) or [])
            if isinstance(item, dict)
        ]
        raw_total = data.get("total", len(items))
        if limit is not None and len(items) > limit:
            items = items[:limit]
        data["items"] = items
        data["total"] = int(raw_total or 0)
        return data
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"total": 0, "items": []}


def iter_sheet_cross_checks(*, include_stale: bool = False) -> list[dict]:
    """Read every per-sheet cross-check JSON listed in the index.

    Returns the full per-sheet payloads (sheet_id, operador, date, summary,
    rows, header, footer, to_analisar, ...). Used by the attractors module
    to aggregate real MATCH/NO_MATCH counts per field/template/operador.
    Missing or corrupt files are skipped silently.

    By default this only returns files generated by the current scoring
    engine. Pass ``include_stale=True`` for historical diagnostics.
    """
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    if not include_stale:
        sheets, _stale = _current_engine_entries(sheets)
    out: list[dict] = []
    for entry in sheets.values():
        payload = _load_sheet_payload(entry, include_stale=include_stale)
        if payload is not None:
            out.append(payload)
    return out


def load_sheet_cross_check(sheet_id: int, *, include_stale: bool = False) -> dict | None:
    """Read one per-sheet cross-check JSON.

    By default this only returns data generated by the current scoring engine.
    Pass ``include_stale=True`` for historical diagnostics.
    """
    raw = _read_index()
    sheets = raw.get("sheets", raw) if isinstance(raw, dict) and "sheets" in raw else raw
    entry = sheets.get(str(sheet_id))
    if not entry:
        return None
    return _load_sheet_payload(entry, include_stale=include_stale)
