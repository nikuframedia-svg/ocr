#!/usr/bin/env python3
"""Backfill bobine_formato sheets that audit as Acabamento candidates."""
# ruff: noqa: E402,I001
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts" / "diag"))

from app.web import db
from template_audit import AuditRecord, FLIP_STATUSES, audit_db

RerunFunc = Callable[[Path, str], dict[str, Any]]
CrossCheckFunc = Callable[[int], Any]


def _resolve_image(data_dir: Path, image_path: str) -> Path:
    path = Path(image_path)
    if path.is_absolute():
        return path
    return data_dir / path


def _eligible(records: list[AuditRecord]) -> list[AuditRecord]:
    return [
        rec for rec in records
        if rec.status in FLIP_STATUSES and rec.proposed_template == "acabamento"
    ]


def run_reclassification(
    *,
    db_path: Path,
    data_dir: Path,
    apply: bool = False,
    limit: int | None = None,
    rerun_func: RerunFunc | None = None,
    cross_check_func: CrossCheckFunc | None = None,
) -> dict[str, Any]:
    records = audit_db(db_path, limit=None)
    candidates = _eligible(records)
    validated = [rec for rec in candidates if rec.sheet_status == "validated"]
    ambiguous = [rec for rec in records if rec.status == "ambiguous"]
    mutable = [rec for rec in candidates if rec.sheet_status != "validated"]
    selected = mutable[:limit] if limit is not None else mutable

    missing_images: list[int] = []
    processed: list[int] = []
    errors: list[dict[str, str]] = []

    old_db_path = db._DB_PATH
    db._DB_PATH = db_path
    try:
        if apply and rerun_func is None:
            from app.web.ocr_runner import rerun_pipeline_for_template
            rerun_func = rerun_pipeline_for_template
        if apply and cross_check_func is None:
            from app.web.main import _run_and_store_cross_check
            cross_check_func = _run_and_store_cross_check

        for rec in selected:
            image = _resolve_image(data_dir, rec.image_path)
            if not image.exists():
                missing_images.append(rec.sheet_id)
                continue
            if not apply:
                continue
            try:
                assert rerun_func is not None
                assert cross_check_func is not None
                result = rerun_func(image, "acabamento")
                db.update_extraction(
                    rec.sheet_id,
                    raw_extraction=result["raw"],
                    dq_audit=result.get("dq") or {},
                    sheet_data=result["current"],
                )
                cross_check_func(rec.sheet_id)
                processed.append(rec.sheet_id)
            except Exception as exc:
                errors.append({
                    "sheet_id": str(rec.sheet_id),
                    "error": f"{type(exc).__name__}: {exc}",
                })
    finally:
        db._DB_PATH = old_db_path

    return {
        "mode": "apply" if apply else "dry-run",
        "audited": len(records),
        "candidates": len(candidates),
        "validated_skipped": len(validated),
        "ambiguous": len(ambiguous),
        "selected": len(selected),
        "missing_images": missing_images,
        "processed": processed,
        "errors": errors,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print(f"Modo: {summary['mode']}")
    print(f"Folhas auditadas: {summary['audited']}")
    print(f"Candidatas Acabamento: {summary['candidates']}")
    print(f"Validadas saltadas: {summary['validated_skipped']}")
    print(f"Ambiguas sem mutacao: {summary['ambiguous']}")
    print(f"Selecionadas: {summary['selected']}")
    print(f"Imagens em falta: {len(summary['missing_images'])}")
    print(f"Reprocessadas: {len(summary['processed'])}")
    if summary["missing_images"]:
        ids = ", ".join(str(x) for x in summary["missing_images"][:20])
        print(f"  IDs sem imagem: {ids}")
    if summary["processed"]:
        ids = ", ".join(str(x) for x in summary["processed"][:20])
        print(f"  IDs reprocessados: {ids}")
    if summary["errors"]:
        print("Erros:")
        for item in summary["errors"][:20]:
            print(f"  sheet {item['sheet_id']}: {item['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reprocessa candidatos bobine_formato -> acabamento.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_REPO / "data" / "app.db",
        help="Caminho da BD SQLite (default: data/app.db).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_REPO / "data",
        help="Diretorio base das imagens relativas guardadas na BD.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Executa o re-OCR. Sem isto, e sempre dry-run.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Modo explicito de simulacao; e tambem o default.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximo de candidatas mutaveis a selecionar.",
    )
    args = parser.parse_args()

    summary = run_reclassification(
        db_path=args.db,
        data_dir=args.data_dir,
        apply=args.apply,
        limit=args.limit,
    )
    print_summary(summary)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
