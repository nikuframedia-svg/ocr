"""Round 54 — Downtime (paragens) helpers and queries.

The Quinadora PAV.4 template tracks machine paragens (downtime), not
production. Its rows have fields motivo/inicio/fim/duracao/resolvido
which don't fit the production_rows table (no qtd, no OF, no lote).

This module provides:
  - `compute_duration_minutes(inicio, fim)` — derive duracao if empty
  - `list_downtime_sheets()` — fetch all paragens sheets + rows from DB
  - `aggregate_downtime(rows)` — totals by operador / day / motivo

All downtime data lives in `sheets.sheet_data` JSON. No new tables.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.web import db

# Time string patterns we accept
_HHMM_RE = re.compile(r"^\s*(\d{1,2})[:hH](\d{1,2})\s*$")          # 09:30 or 09H30
_HHMM_DOT_RE = re.compile(r"^\s*(\d{1,2})[.,](\d{1,2})\s*$")        # 09.30 or 09,30


def _paragens_template_names() -> tuple[str, ...]:
    """All registry templates that are paragens (verso) sheets — driven by
    `has_production_rows=False`. rev00 added a generic `paragens` template and
    keeps `maq_fustes_paragens`; this auto-covers both (and any future verso)
    so the downtime dashboard never falls behind the registry again.

    Historical note: the query used to hardcode `quinadora_pav4_paragens`, a
    name removed when PAV.4 became a production template — which silently hid
    every `maq_fustes_paragens` sheet from the dashboard.
    """
    from app.templates_registry import TEMPLATES

    return tuple(sorted(n for n, t in TEMPLATES.items() if not t.has_production_rows))


def _parse_time_to_minutes(s: Any) -> int | None:
    """Parse a clock time string to minutes-since-midnight.

    Accepts HH:MM, HH.MM, HH,MM, HHhMM. Returns None if unparseable.
    """
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    for pat in (_HHMM_RE, _HHMM_DOT_RE):
        m = pat.match(raw)
        if m:
            h = int(m.group(1))
            mm = int(m.group(2))
            if 0 <= h < 24 and 0 <= mm < 60:
                return h * 60 + mm
    return None


def compute_duration_minutes(inicio: Any, fim: Any) -> int | None:
    """Return paragem duration in minutes, or None if either bound missing.

    Handles cross-midnight (rare but possible during night shift): if
    fim < inicio, adds 24h. Caps at 12 hours to avoid silly overflows
    from OCR misreads (e.g. inicio=00:30 fim=23:00 reads as +22h).
    """
    i = _parse_time_to_minutes(inicio)
    f = _parse_time_to_minutes(fim)
    if i is None or f is None:
        return None
    delta = f - i
    if delta < 0:
        delta += 24 * 60          # cross-midnight
    if delta > 12 * 60:
        return None                # likely OCR misread, refuse to guess
    return delta


def parse_duracao_minutes(s: Any) -> int | None:
    """Parse a duracao string ("01:30", "1h30", "90") to minutes."""
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    # Plain integer minutes
    if raw.isdigit():
        return int(raw)
    # HH:MM
    m = _HHMM_RE.match(raw)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # "1h30", "1H30"
    m = re.match(r"^(\d+)\s*[hH]\s*(\d{1,2})$", raw)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _resolvido_to_bool(s: Any) -> bool | None:
    """`sim`/`não`/`S`/`N`/`yes`/`no` → bool. Returns None if unparseable."""
    if s is None:
        return None
    raw = str(s).strip().lower()
    if not raw:
        return None
    if raw in ("sim", "s", "yes", "y", "1", "true", "✓", "v"):
        return True
    if raw in ("nao", "não", "n", "no", "0", "false", "✗", "x"):
        return False
    return None


def list_downtime_sheets(db_path: Path) -> list[dict]:
    """Return all paragens sheets + rows from DB.

    Filters by template_name OR (legacy) by setor matching PAV.4 alias.
    Each entry has: sheet_id, captured_at, operador, sheet_date,
    sheet_iso_date, status, rows[].
    """
    # rev00 — filtro dirigido pelo registry: qualquer template com
    # has_production_rows=False (paragens genérico, maq_fustes_paragens, …).
    paragens_names = _paragens_template_names()
    placeholders = ", ".join("?" for _ in paragens_names) or "NULL"
    sql = f"""
        SELECT id, captured_at, status, operador, validated_at,
               sheet_data
          FROM sheets
         WHERE sheet_data IS NOT NULL
           AND (
                 json_extract(sheet_data, '$.template_name') IN ({placeholders})
              OR (
                   json_extract(sheet_data, '$.template_name') IS NULL
                   AND UPPER(TRIM(json_extract(sheet_data, '$.header.setor_maquina')))
                       IN ('QUINADORA PAV.4', 'QUINADORA PAV 4', 'QUINADORA PAV4')
                 )
           )
         ORDER BY captured_at DESC
    """
    # R256 — conn_at em vez de sqlite3.connect direto: busy_timeout + close
    # garantido em exceção (a conexão antiga vazava se o execute levantasse).
    with db.conn_at(db_path) as c:
        raw_rows = c.execute(sql, paragens_names).fetchall()

    out: list[dict] = []
    for r in raw_rows:
        try:
            sd = json.loads(r["sheet_data"]) if r["sheet_data"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        header = sd.get("header") or {}
        sheet_date = (header.get("data") or "").strip()
        sheet_iso = _to_iso(sheet_date)
        rows = []
        for i, row in enumerate(sd.get("rows", []) or []):
            if not isinstance(row, dict):
                continue
            motivo = (row.get("motivo") or "").strip()
            inicio = (row.get("inicio") or "").strip()
            fim = (row.get("fim") or "").strip()
            if not (motivo or inicio or fim):
                continue            # empty row
            duracao_raw = (row.get("duracao") or "").strip()
            duracao_min = parse_duracao_minutes(duracao_raw)
            if duracao_min is None:
                duracao_min = compute_duration_minutes(inicio, fim)
            rows.append({
                "row_index": i,
                "motivo": motivo,
                "inicio": inicio,
                "fim": fim,
                "duracao": duracao_raw,
                "duracao_min": duracao_min,
                "resolvido": (row.get("resolvido") or "").strip(),
                "resolvido_bool": _resolvido_to_bool(row.get("resolvido")),
            })
        out.append({
            "sheet_id": r["id"],
            "captured_at": r["captured_at"],
            "status": r["status"],
            "operador": (r["operador"] or header.get("operador") or "").strip(),
            "sheet_date": sheet_date,
            "sheet_iso_date": sheet_iso,
            "n_operador": (header.get("n_operador") or "").strip(),
            "setor_maquina": (header.get("setor_maquina") or "").strip(),
            "rows": rows,
        })
    return out


def _to_iso(dd_mm_yyyy: str) -> str | None:
    """Convert ``15-04-2026`` → ``2026-04-15``. Returns None if malformed."""
    if not dd_mm_yyyy:
        return None
    parts = dd_mm_yyyy.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, y = parts
        if len(y) == 4 and len(m) <= 2 and len(d) <= 2:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return None


def aggregate_downtime(sheets: list[dict]) -> dict:
    """Compute totals by operador / motivo / resolvido status.

    Returns a dict with: total_minutes, total_entries, n_sheets,
    by_operador, by_motivo, by_resolved, by_date.
    """
    total_min = 0
    total_entries = 0
    by_op: dict[str, int] = defaultdict(int)         # operador -> total minutes
    by_motivo: dict[str, int] = defaultdict(int)     # motivo -> count
    by_resolved = {"resolved": 0, "open": 0, "unknown": 0}
    by_date: dict[str, int] = defaultdict(int)       # YYYY-MM-DD -> total minutes

    for s in sheets:
        operador = s.get("operador") or "—"
        sheet_iso = s.get("sheet_iso_date")
        for r in s.get("rows", []):
            total_entries += 1
            mins = r.get("duracao_min") or 0
            total_min += mins
            by_op[operador] += mins
            motivo_key = (r.get("motivo") or "—").strip() or "—"
            by_motivo[motivo_key] += 1
            rb = r.get("resolvido_bool")
            if rb is True:
                by_resolved["resolved"] += 1
            elif rb is False:
                by_resolved["open"] += 1
            else:
                by_resolved["unknown"] += 1
            if sheet_iso:
                by_date[sheet_iso] += mins

    return {
        "total_minutes": total_min,
        "total_hours": round(total_min / 60, 1),
        "total_entries": total_entries,
        "n_sheets": len(sheets),
        "by_operador": sorted(by_op.items(), key=lambda kv: -kv[1]),
        "by_motivo": sorted(by_motivo.items(), key=lambda kv: -kv[1]),
        "by_resolved": by_resolved,
        "by_date": sorted(by_date.items(), reverse=True),
    }


__all__ = [
    "compute_duration_minutes",
    "parse_duracao_minutes",
    "list_downtime_sheets",
    "aggregate_downtime",
]
