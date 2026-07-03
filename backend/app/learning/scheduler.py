"""Trigger + orchestration of the learning engine.

The cycle runs after every 50 validated sheets. The trigger is a cheap
count check called from the sheet-validation route; the heavy work runs
in a daemon thread guarded by a module lock so only one cycle runs at a
time. The counter is absolute and persisted (``learning_runs``), so the
cadence survives server restarts.
"""
from __future__ import annotations

import logging
import threading

from app.learning import materialize, metrics, mining, risk, store
from app.web import db

logger = logging.getLogger(__name__)

LEARN_EVERY_N_SHEETS = 50
_LOCK = threading.Lock()


def _validated_count() -> int:
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) n FROM sheets WHERE status = 'validated'"
        ).fetchone()["n"]


def maybe_trigger_learning() -> bool:
    """Called right after a sheet is validated. Dispatches a learning
    cycle in the background when 50 new validated sheets have piled up
    since the last run. Returns True if a cycle was dispatched."""
    try:
        count = _validated_count()
        last = store.last_run()
        last_count = (last or {}).get("validated_count_at_run") or 0
        if count - last_count < LEARN_EVERY_N_SHEETS:
            return False
        if _LOCK.locked():
            return False
        threading.Thread(
            target=run_learning_cycle, daemon=True, name="learning-cycle"
        ).start()
        return True
    except Exception:
        logger.exception("maybe_trigger_learning failed")
        return False


def run_learning_cycle() -> int | None:
    """Full cycle: mine → classify risk → upsert → materialise → metric →
    reload pipeline. Returns the ``learning_runs`` id, or None if another
    cycle holds the lock."""
    if not _LOCK.acquire(blocking=False):
        logger.info("learning cycle skipped — another run in progress")
        return None
    run_id: int | None = None
    try:
        run_id = store.record_run(_validated_count())
        edits = mining.mine_edits()
        sheets = mining.load_validated_sheets()
        proposals = (
            mining.derive_aliases(edits)
            + mining.derive_confusions(edits)
            + mining.derive_observed_values(sheets)
            + mining.derive_fewshot_candidates(sheets)
        )
        new_n = auto_n = quar_n = 0
        for p in proposals:
            res = store.upsert_proposal(p, risk.classify_risk(p))
            if res["created"]:
                new_n += 1
                if res["status"] == "approved":
                    auto_n += 1
                else:
                    quar_n += 1
        materialize.materialize_overlay()
        # R244 — refit dos parâmetros do cross (matriz de chars, contagens)
        # a partir das edits/validadas novas, com auto-gate (pisos de amostra
        # + deriva limitada + backup). Falha nunca bloqueia o ciclo.
        try:
            from app.learning import cross_refit

            cross_refit.refit_from_db()
        except Exception:
            logger.exception("cross_refit falhou — ciclo continua")
        store.finish_run(
            run_id,
            status="done",
            edits_scanned=len(edits),
            proposals_new=new_n,
            proposals_auto_applied=auto_n,
            proposals_quarantined=quar_n,
            corrections_per_sheet=metrics.corrections_per_sheet(),
        )
        reload_pipeline()
        logger.info(
            "learning cycle done: %d proposals (%d new, %d auto, %d quarantine)",
            len(proposals), new_n, auto_n, quar_n,
        )
        # R117 — emit kernel event para a UI / cron reagirem ao fim do ciclo
        try:
            from app import kernel
            kernel.emit_event("r98_cycle_completed", {
                "run_id": run_id,
                "proposals_new": new_n,
                "proposals_auto_applied": auto_n,
                "proposals_quarantined": quar_n,
            })
        except Exception:
            pass
        return run_id
    except Exception as e:
        logger.exception("learning cycle failed")
        if run_id is not None:
            try:
                store.finish_run(run_id, status="error", error_message=str(e))
            except Exception:
                pass
        return run_id
    finally:
        _LOCK.release()


def reload_pipeline() -> None:
    """Re-load lexicons so the running OCR pipeline picks up new approvals
    without a server restart."""
    try:
        from app.dq import snap

        snap.reload_lexicons()
    except Exception:
        logger.exception("snap.reload_lexicons failed")
