"""Cross-check package — wraps the unified scoring engine (R109).

The scoring engine lives in ``app.pipeline.scoring_engine``. This package
keeps the legacy public API (``cross_check_sheet``, ``CROSS_CHECK_STATUSES``,
plus the on-disk storage + ref watcher) and re-exports them so callers
don't change.

Responsibilities:
- ``ref_watcher``: detect StockSAP.xlsx / plan_colunas_cpis.xlsx changes
  and load refs into memory.
- ``refs_uploads``: web UI for uploading new ref files (R106).
- ``storage``: persist cross-check JSON to disk for downstream tools.
- ``cross_check_sheet``: re-exported from scoring_engine (R109 motor).
"""
from app.pipeline.scoring_engine import (
    CROSS_CHECK_STATUSES,
    cross_check_sheet,
)

from .ref_watcher import RefWatcher, get_watcher
from .storage import store_cross_check, load_summary, load_to_analisar

__all__ = [
    "RefWatcher", "get_watcher",
    "cross_check_sheet", "CROSS_CHECK_STATUSES",
    "store_cross_check", "load_summary", "load_to_analisar",
]
