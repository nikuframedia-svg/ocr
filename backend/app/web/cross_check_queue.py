"""One scoring worker, latest pending revision per sheet, shadow last.

The OCR worker also uses SCORING_SLOT: CPU-bound scoring must not fan out
into dozens of Python threads competing with interactive HTTP requests.
"""
from __future__ import annotations

from collections import OrderedDict
from contextvars import Context
from dataclasses import dataclass
import json
import sys
import threading
import time
import traceback
from typing import Callable, Hashable


SCORING_SLOT = threading.RLock()


@dataclass
class _Job:
    version: Hashable
    run: Callable[[], None]
    queued_at: float


class ScoringQueue:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pending: OrderedDict[Hashable, _Job] = OrderedDict()
        self._shadow: OrderedDict[Hashable, _Job] = OrderedDict()
        self._active: tuple[Hashable, _Job] | None = None
        self._worker: threading.Thread | None = None
        self._closed = False

    def submit(self, key: Hashable, version: Hashable,
               run: Callable[[], None], *, shadow: bool = False) -> bool:
        with self._condition:
            if self._closed:
                raise RuntimeError("scoring queue is closed")
            pending = self._shadow if shadow else self._pending
            existing = pending.get(key)
            if existing and existing.version == version:
                return False
            if (existing and isinstance(version, int) and isinstance(existing.version, int)
                    and version < existing.version):
                return False
            if self._active and self._active[0] == key:
                if self._active[1].version == version:
                    return False
            pending[key] = _Job(version, run, time.perf_counter())
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="cross-check-worker", daemon=True)
                self._worker.start()
            self._condition.notify_all()
            return True

    def busy(self, key: Hashable) -> bool:
        with self._condition:
            return (key in self._pending or key in self._shadow
                    or bool(self._active and self._active[0] == key))

    def snapshot(self) -> dict:
        with self._condition:
            return {"pending": len(self._pending), "shadow_pending": len(self._shadow),
                    "active": self._active[0] if self._active else None}

    def wait_idle(self, timeout: float = 10) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not (self._active or self._pending or self._shadow), timeout)

    def close(self) -> None:
        """Drain accepted work; useful for a controlled shutdown and tests."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed or self._pending or self._shadow)
                if not self._pending and not self._shadow:
                    return
                pending = self._pending if self._pending else self._shadow
                key, job = pending.popitem(last=False)
                self._active = (key, job)
            try:
                started = time.perf_counter()
                # Like a new thread, each job starts with default ContextVars.
                # A shadow variant must never leak into the next production run.
                Context().run(job.run)
            except Exception:
                traceback.print_exc()
            finally:
                print(json.dumps({"event": "scoring_job_timing", "key": key,
                                  "wait_ms": round((started - job.queued_at) * 1000, 1),
                                  "duration_ms": round((time.perf_counter() - started) * 1000, 1)}),
                      file=sys.stderr, flush=True)
                with self._condition:
                    self._active = None
                    self._condition.notify_all()


queue = ScoringQueue()
