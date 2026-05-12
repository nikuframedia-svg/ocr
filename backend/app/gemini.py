"""Round 54 Fase 7 — Gemini (TPL102) helpers and queries.

The 3 corte térmico templates (GASPARINI / HPE32 / HD36) share a TPL102
schema: pf / cliente / of / modelo / cf / m2 / qtd / nesting /
inicio / fim / np. Domain is chapa nesting (sheet metal cutting), not
column production.

This module mirrors `app.downtime` for paragens: provides helpers
to query Gemini sheets out of the JSON `sheet_data` column and
compute aggregate stats for a dedicated dashboard.

Public API:
    parse_m2(s)               — float square-metres
    parse_minutes_between(i, f) — re-uses downtime helper for duration
    list_gemini_sheets(db_path) — pull all Gemini sheets + rows
    aggregate_gemini(sheets)  — per operador / machine / day totals
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.downtime import compute_duration_minutes


_M2_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(?:m[2²]?)?\s*$", re.IGNORECASE)


def parse_m2(s: Any) -> float | None:
    """Parse an m² string ("12.5", "12,5 m²", "8.34") to float.

    Returns None if unparseable. Caps at 1000 m² (huge sheets exist
    but that's a safety guard against OCR misreads).
    """
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    m = _M2_RE.match(raw)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if v < 0 or v > 1000:
        return None
    return v


def _to_iso(dd_mm_yyyy: str) -> str | None:
    if not dd_mm_yyyy:
        return None
    parts = dd_mm_yyyy.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        d, m, y = parts
        if len(y) == 4 and len(m) <= 2 and len(d) <= 2:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return None


# Gemini template names from registry — kept inline to avoid a circular
# import (gemini.py is itself imported via main.py).
_GEMINI_TEMPLATES = ("gasparini", "hpe32", "hd36")
_GEMINI_SETOR_ALIASES = ("GASPARINI", "HPE32", "HPE 32", "HD36", "HD 36")


def list_gemini_sheets(db_path: Path) -> list[dict]:
    """Return all Gemini sheets (3 machines combined) from DB.

    Filters by template_name in (gasparini/hpe32/hd36) OR by setor alias
    for legacy sheets. Each entry has machine_name (operator-facing
    label), aggregated row info with parsed m² + duration.
    """
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row

    # Build the WHERE clause: template_name match OR legacy setor match
    tname_placeholders = ",".join("?" * len(_GEMINI_TEMPLATES))
    setor_placeholders = ",".join("?" * len(_GEMINI_SETOR_ALIASES))
    sql = f"""
        SELECT id, captured_at, status, operador, validated_at, sheet_data
          FROM sheets
         WHERE sheet_data IS NOT NULL
           AND (
                 json_extract(sheet_data, '$.template_name') IN ({tname_placeholders})
              OR (
                   json_extract(sheet_data, '$.template_name') IS NULL
                   AND UPPER(TRIM(json_extract(sheet_data, '$.header.setor_maquina')))
                       IN ({setor_placeholders})
                 )
           )
         ORDER BY captured_at DESC
    """
    params = (*_GEMINI_TEMPLATES, *_GEMINI_SETOR_ALIASES)
    raw_rows = c.execute(sql, params).fetchall()
    c.close()

    out: list[dict] = []
    for r in raw_rows:
        try:
            sd = json.loads(r["sheet_data"]) if r["sheet_data"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        header = sd.get("header") or {}
        sheet_date = (header.get("data") or "").strip()
        sheet_iso = _to_iso(sheet_date)
        setor = (header.get("setor_maquina") or "").strip().upper()
        template_name = sd.get("template_name") or _machine_from_setor(setor)

        rows = []
        for i, row in enumerate(sd.get("rows", []) or []):
            if not isinstance(row, dict):
                continue
            pf = (row.get("pf") or "").strip()
            cliente = (row.get("cliente") or "").strip()
            of = (row.get("of") or "").strip()
            modelo = (row.get("modelo") or "").strip()
            if not (pf or cliente or of or modelo):
                continue
            m2_val = parse_m2(row.get("m2"))
            inicio = (row.get("inicio") or "").strip()
            fim = (row.get("fim") or "").strip()
            duration_min = compute_duration_minutes(inicio, fim)
            qtd_raw = (row.get("qtd") or "").strip()
            try:
                qtd = int(re.sub(r"\D", "", qtd_raw)) if qtd_raw else None
            except ValueError:
                qtd = None
            rows.append({
                "row_index": i,
                "pf": pf,
                "cliente": cliente,
                "of": of,
                "modelo": modelo,
                "cf": (row.get("cf") or "").strip(),
                "m2": (row.get("m2") or "").strip(),
                "m2_val": m2_val,
                "qtd": qtd,
                "nesting": (row.get("nesting") or "").strip(),
                "inicio": inicio,
                "fim": fim,
                "duration_min": duration_min,
                "np": (row.get("np") or "").strip(),
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
            "template_name": template_name,
            "machine": _machine_display_name(template_name),
            "rows": rows,
        })
    return out


def _machine_from_setor(setor_upper: str) -> str:
    """Infer template_name from raw setor (legacy sheets only)."""
    if not setor_upper:
        return "gasparini"
    s = setor_upper.replace(" ", "")
    if "GASPARINI" in s:
        return "gasparini"
    if "HPE32" in s:
        return "hpe32"
    if "HD36" in s:
        return "hd36"
    return "gasparini"


def _machine_display_name(template_name: str) -> str:
    return {
        "gasparini": "Gasparini",
        "hpe32": "HPE32",
        "hd36": "HD36",
    }.get(template_name, template_name)


def aggregate_gemini(sheets: list[dict]) -> dict:
    """Compute Gemini stats: m² per machine, per operador, per day.

    Returns dict with: total_m2, total_qty, total_minutes, n_sheets,
    n_entries, by_machine, by_operador, by_date, by_cliente.
    """
    total_m2 = 0.0
    total_qty = 0
    total_min = 0
    n_entries = 0
    by_machine: dict[str, dict] = defaultdict(
        lambda: {"m2": 0.0, "qtd": 0, "entries": 0, "sheets": 0}
    )
    by_op: dict[str, dict] = defaultdict(
        lambda: {"m2": 0.0, "qtd": 0, "entries": 0}
    )
    by_date: dict[str, float] = defaultdict(float)
    by_cliente: dict[str, dict] = defaultdict(
        lambda: {"m2": 0.0, "qtd": 0}
    )

    for s in sheets:
        machine = s.get("machine") or s.get("template_name") or "—"
        by_machine[machine]["sheets"] += 1
        operador = s.get("operador") or "—"
        sheet_iso = s.get("sheet_iso_date")
        for r in s.get("rows", []):
            n_entries += 1
            m2 = r.get("m2_val") or 0.0
            qtd = r.get("qtd") or 0
            mins = r.get("duration_min") or 0
            total_m2 += m2
            total_qty += qtd
            total_min += mins
            by_machine[machine]["m2"] += m2
            by_machine[machine]["qtd"] += qtd
            by_machine[machine]["entries"] += 1
            by_op[operador]["m2"] += m2
            by_op[operador]["qtd"] += qtd
            by_op[operador]["entries"] += 1
            if sheet_iso:
                by_date[sheet_iso] += m2
            cli = (r.get("cliente") or "—").strip() or "—"
            by_cliente[cli]["m2"] += m2
            by_cliente[cli]["qtd"] += qtd

    return {
        "total_m2": round(total_m2, 2),
        "total_qty": total_qty,
        "total_minutes": total_min,
        "total_hours": round(total_min / 60, 1) if total_min else 0,
        "n_sheets": len(sheets),
        "n_entries": n_entries,
        "by_machine": [
            {
                "name": k,
                "m2": round(v["m2"], 2),
                "qtd": v["qtd"],
                "entries": v["entries"],
                "sheets": v["sheets"],
            }
            for k, v in sorted(
                by_machine.items(), key=lambda kv: -kv[1]["m2"]
            )
        ],
        "by_operador": [
            {
                "operador": k,
                "m2": round(v["m2"], 2),
                "qtd": v["qtd"],
                "entries": v["entries"],
            }
            for k, v in sorted(by_op.items(), key=lambda kv: -kv[1]["m2"])
        ],
        "by_date": sorted(
            [(k, round(v, 2)) for k, v in by_date.items()],
            reverse=True,
        ),
        "by_cliente": [
            {
                "cliente": k,
                "m2": round(v["m2"], 2),
                "qtd": v["qtd"],
            }
            for k, v in sorted(by_cliente.items(), key=lambda kv: -kv[1]["m2"])
        ][:10],
    }


__all__ = [
    "parse_m2",
    "list_gemini_sheets",
    "aggregate_gemini",
]
