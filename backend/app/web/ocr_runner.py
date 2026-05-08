"""Wrapper that runs the v9 OCR pipeline + DQ Module on one image.

Imports ``ocr6`` (workspace-root script) and ``app.dq.pipeline`` and
glues them. Sets the prompt to ``prompts/ocr6_v3.txt`` (canonical) on
import.

Public API:
    run_pipeline(image_path: Path) -> dict with keys:
        raw       — direct ocr6 output (header/rows/footer)
        dq        — DQ audit (cells/violations/score/stp_eligible/summary)
        current   — same as raw initially; lives for editing
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]

# ocr6.py is at workspace root
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import ocr6  # type: ignore  # noqa: E402

from app.dq.cross_sheet_index import CrossSheetIndex  # noqa: E402
from app.dq.pipeline import run_dq  # noqa: E402

# Pin canonical prompt v3 (same as production runs of v9)
_PROMPT_PATH = _REPO / "prompts" / "ocr6_v3.txt"
ocr6.PROMPT, ocr6.PROMPT_HASH = ocr6.load_prompt(_PROMPT_PATH)

# Load learned artefacts once at module import
_LEARNED_PATH = _REPO / "lexicons" / "learned.json"
_LEARNED: dict[str, Any] = (
    json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
    if _LEARNED_PATH.exists()
    else {}
)

# Persistent cross-sheet index
_INDEX_PATH = _REPO / "data" / "cross_sheet.json"


def run_pipeline(image_path: Path) -> dict:
    """Run OCR + DQ end-to-end on a single image.

    Returns a dict with the three states downstream needs. Raises on
    OCR failure (caller turns into HTTP 500 / sheet status='error').
    """
    result = ocr6.process_image(image_path, idx=1, total=1)
    if not result.metrics or result.metrics.status != "ok":
        err = result.metrics.error if result.metrics else "unknown"
        raise RuntimeError(f"OCR failed: {err}")

    raw_extraction = {
        "header": result.header,
        "rows": result.rows,
        "footer": result.footer,
    }

    # Lazy-load + persist cross-sheet index
    index = CrossSheetIndex.load(_INDEX_PATH)
    if not index.ov_to_cliente and _LEARNED_PATH.exists():
        index.seed_from_learned(_LEARNED_PATH)

    dq_result, fixed = run_dq(
        extraction=raw_extraction,
        sheet_name=image_path.stem,
        learned=_LEARNED,
        index=index,
    )
    index.save(_INDEX_PATH)

    return {
        "raw": raw_extraction,
        "dq": dq_result.to_dict(),
        "current": fixed,
    }
