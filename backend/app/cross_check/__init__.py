"""Cross-check package — live SAP/plan reference watcher + auto-validate
+ auto-fill engine + on-disk results storage.

Responsibilities:
- ``ref_watcher``: detect when StockSAP.xlsx or plan_colunas_cpis.xlsx
  in ``C:\\kanban\\nifruka\\04_Documentacao\\`` change on disk; reload
  refs into memory; expose to snap.py + engine.py.
- ``engine``: take a post-DQ sheet, cross-reference each row against
  refs, return per-field status (OK / CORRIGIDO / PREENCHIDO / ANALISAR /
  MISSING) + summary.
- ``storage``: write per-sheet JSON + ``_summary.json`` + ``_to_analisar.json``
  to ``C:\\kanban\\nifruka\\03_Cross_Check\\<date>\\<stem>_sheet<id>.json``.

The whole package runs SYNCHRONOUSLY on /upload + /edit (~50ms typical)
so the UI sees fresh state immediately. No background threads.
"""
from .ref_watcher import RefWatcher, get_watcher
from .engine import cross_check_sheet, CROSS_CHECK_STATUSES
from .storage import store_cross_check, load_summary, load_to_analisar

__all__ = [
    "RefWatcher", "get_watcher",
    "cross_check_sheet", "CROSS_CHECK_STATUSES",
    "store_cross_check", "load_summary", "load_to_analisar",
]
