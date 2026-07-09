#!/usr/bin/env python3
"""Audit bobine_formato sheets that now look like Acabamento.

Read-only by default: this script never mutates the database. It scans sheets
whose persisted extraction says ``template_name == "bobine_formato"`` and
re-evaluates the OCR header/row structure with the current detector.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))

from app.templates_registry import (
    DEFAULT_TEMPLATE,
    detect_template_with_reason,
)
from app.web.ocr_runner import (
    _acabamento_structure_analysis,
    _infer_template_from_default_pass1,
)

FLIP_STATUSES = (
    "flip_acabamento_setor",
    "flip_acabamento_codmaq",
    "flip_acabamento_structure",
)


@dataclass(frozen=True)
class AuditRecord:
    sheet_id: int
    status: str
    sheet_status: str
    image_path: str
    raw_setor: str
    cod_maquina: str
    current_template: str
    proposed_template: str
    reason: str
    structural_score: int
    acabamento_like_rows: int
    bobine_like_rows: int


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _template_name(data: dict[str, Any]) -> str:
    return str(data.get("template_name") or "").strip()


def _is_persisted_bobine(raw: dict[str, Any], sheet_data: dict[str, Any]) -> bool:
    return DEFAULT_TEMPLATE.name in {_template_name(raw), _template_name(sheet_data)}


def _preferred_extraction(raw: dict[str, Any], sheet_data: dict[str, Any]) -> dict[str, Any]:
    if raw.get("header") or raw.get("rows"):
        return raw
    return sheet_data


def _header_value(data: dict[str, Any], key: str) -> str:
    header = data.get("header") or {}
    return str(header.get(key) or "").strip()


def _weak_acabamento_structure(analysis: dict[str, Any]) -> bool:
    return (
        int(analysis.get("score") or 0) >= 4
        or int(analysis.get("acabamento_like_rows") or 0) > 0
    )


def audit_sheet_row(row: sqlite3.Row | dict[str, Any]) -> AuditRecord | None:
    raw = _load_json(row["raw_extraction"])
    sheet_data = _load_json(row["sheet_data"])
    if not _is_persisted_bobine(raw, sheet_data):
        return None

    source = _preferred_extraction(raw, sheet_data)
    raw_setor = _header_value(source, "setor_maquina")
    cod_maquina = _header_value(source, "cod_maquina")
    persisted_template = _template_name(sheet_data) or _template_name(raw) or DEFAULT_TEMPLATE.name

    detected, detection_reason = detect_template_with_reason(
        raw_setor, cod_maquina=cod_maquina,
    )
    cod_only, _cod_reason = detect_template_with_reason("", cod_maquina=cod_maquina)
    analysis = _acabamento_structure_analysis(source)
    inferred = _infer_template_from_default_pass1(source)
    structure_says_acabamento = bool(inferred and inferred.name == "acabamento")

    proposed = ""
    if detected.name == "acabamento":
        proposed = "acabamento"
        if detection_reason == "cod_maquina":
            status = "flip_acabamento_codmaq"
        else:
            status = "flip_acabamento_setor"
        reason = f"detector:{detection_reason}"
    elif structure_says_acabamento:
        status = "flip_acabamento_structure"
        proposed = "acabamento"
        reason = "row_structure:" + ",".join(analysis.get("reasons") or [])
    elif cod_only.name == "acabamento":
        status = "ambiguous"
        proposed = "acabamento"
        reason = "cod_maquina_conflicts_with_explicit_setor"
    elif _weak_acabamento_structure(analysis):
        status = "ambiguous"
        proposed = "acabamento"
        reason = "weak_row_structure:" + ",".join(analysis.get("reasons") or [])
    else:
        status = "no_flip"
        reason = f"detector:{detection_reason}"

    return AuditRecord(
        sheet_id=int(row["id"]),
        status=status,
        sheet_status=str(row["sheet_status"] or ""),
        image_path=str(row["image_path"] or ""),
        raw_setor=raw_setor,
        cod_maquina=cod_maquina,
        current_template=persisted_template,
        proposed_template=proposed,
        reason=reason,
        structural_score=int(analysis.get("score") or 0),
        acabamento_like_rows=int(analysis.get("acabamento_like_rows") or 0),
        bobine_like_rows=int(analysis.get("bobine_like_rows") or 0),
    )


def audit_db(db_path: Path, *, limit: int | None = None) -> list[AuditRecord]:
    query = """
        SELECT id, status AS sheet_status, image_path, raw_extraction, sheet_data
        FROM sheets
        WHERE raw_extraction IS NOT NULL OR sheet_data IS NOT NULL
        ORDER BY id DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    if not db_path.exists():
        return []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []

    records = [rec for row in rows if (rec := audit_sheet_row(row)) is not None]
    return sorted(records, key=lambda rec: rec.sheet_id)


def write_csv(records: list[AuditRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(AuditRecord.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(rec) for rec in records)


def write_json(records: list[AuditRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(rec) for rec in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_summary(records: list[AuditRecord]) -> None:
    status_counts = Counter(rec.status for rec in records)
    sheet_status_counts = Counter(rec.sheet_status for rec in records)
    flips = sum(status_counts[s] for s in FLIP_STATUSES)
    print(f"Folhas bobine_formato auditadas: {len(records)}")
    print(f"Flip Acabamento proposto: {flips}")
    for name in (*FLIP_STATUSES, "ambiguous", "no_flip"):
        print(f"  {name}: {status_counts[name]}")
    if sheet_status_counts:
        print("Estados na BD:")
        for name, count in sorted(sheet_status_counts.items()):
            print(f"  {name or '(sem estado)'}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audita folhas persistidas como bobine_formato que parecem Acabamento.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_REPO / "data" / "app.db",
        help="Caminho da BD SQLite (default: data/app.db).",
    )
    parser.add_argument("--csv", type=Path, help="Escreve resultado em CSV.")
    parser.add_argument("--json", type=Path, help="Escreve resultado em JSON.")
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Numero maximo de folhas recentes a ler antes do filtro (default: 200).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audita todas as folhas da BD.",
    )
    args = parser.parse_args()

    limit = None if args.all else max(1, args.limit)
    records = audit_db(args.db, limit=limit)
    print_summary(records)
    if args.csv:
        write_csv(records, args.csv)
        print(f"CSV escrito: {args.csv}")
    if args.json:
        write_json(records, args.json)
        print(f"JSON escrito: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
