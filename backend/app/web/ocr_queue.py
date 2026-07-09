"""Round 71 — Background OCR queue + single worker thread.

Decouples the /upload endpoint from the blocking ~22s OCR call. Uploads
go onto a FIFO queue and return immediately. A daemon worker thread
drains the queue serially (matching Ollama's single-inference GPU
constraint), running the same OCR → DQ → cross-check → CSV deposit
pipeline that used to run inline.

Task C E4 — jobs tipados: a MESMA fila FIFO passa a transportar
("sheet", sheet_id) e ("discovery", template_id). A descoberta de campos
de um template novo (wizard /admin) partilha o motor de OCR da produção
— sem prioridades: quem chega primeiro é servido primeiro (decisão do
dono; o wizard mostra a posição na fila).

Public API:
    enqueue(sheet_id)                — push a sheet onto the queue
    enqueue_discovery(template_id)   — push a template-discovery job
    queue_size() -> int              — depth (informational)
    worker_alive() -> bool           — health check
    start_worker(processor_fn, discovery_fn=None) — boot once at startup
    recover_pending(seconds, callback) — re-enqueue orphaned 'pending'

The ``processor_fn``/``discovery_fn`` callbacks are injected at startup
so this module stays free of imports from app.web.main (avoiding circular
imports). Signatures: ``fn(item_id: int) -> None``. Exceptions are
swallowed by the worker loop; each callback is responsible for setting
its own error status.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

# Job = (kind, id): kind ∈ {"sheet", "discovery"}.
_ocr_queue: queue.Queue[tuple[str, int]] = queue.Queue()
_worker_thread: threading.Thread | None = None
_processor_fn: Callable[[int], None] | None = None
_discovery_fn: Callable[[int], None] | None = None


def enqueue(sheet_id: int) -> int:
    """Put a sheet_id on the queue. Returns approximate queue position
    (1-based; this sheet's spot, snapshot at insertion time)."""
    _ocr_queue.put(("sheet", sheet_id))
    return _ocr_queue.qsize()


def enqueue_discovery(template_id: int) -> int:
    """Task C E4 — põe uma descoberta de template na MESMA fila FIFO.
    Devolve a posição aproximada (1-based) para o wizard mostrar."""
    _ocr_queue.put(("discovery", template_id))
    return _ocr_queue.qsize()


def queue_size() -> int:
    """Current number of pending items (informational, not authoritative)."""
    return _ocr_queue.qsize()


def worker_alive() -> bool:
    """True if the worker thread is running. Used by /admin/queue-status."""
    return _worker_thread is not None and _worker_thread.is_alive()


def _worker_loop() -> None:
    """Drain the queue forever. NEVER crashes — all exceptions are
    swallowed after delegating to the processor for error-status update."""
    while True:
        job = _ocr_queue.get()
        try:
            # Tolerância a itens legados (int puro) — testes/código antigo.
            kind, item_id = job if isinstance(job, tuple) else ("sheet", job)
            if kind == "discovery":
                if _discovery_fn is not None:
                    _discovery_fn(item_id)
            elif _processor_fn is not None:
                _processor_fn(item_id)
        except Exception:
            # Callbacks are expected to handle errors themselves (set
            # status='error'). This catch is a safety net.
            pass
        finally:
            _ocr_queue.task_done()


def start_worker(
    processor_fn: Callable[[int], None],
    discovery_fn: Callable[[int], None] | None = None,
) -> None:
    """Boot the singleton daemon worker thread. Idempotent — calling
    twice is a no-op (subsequent callback arguments are ignored)."""
    global _worker_thread, _processor_fn, _discovery_fn
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _processor_fn = processor_fn
    _discovery_fn = discovery_fn
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="ocr-worker",
        daemon=True,
    )
    _worker_thread.start()


def recover_pending(
    older_than_seconds: int,
    list_pending_fn: Callable[[int], list[int]],
) -> int:
    """Re-enqueue sheets stuck in status='pending' from before this
    process started. Called once at app startup after start_worker.

    Args:
        older_than_seconds: skip very-recent ones (still in flight)
        list_pending_fn: returns list of sheet_ids — typically
            ``db.list_stuck_pending``

    Returns count re-enqueued.
    """
    stuck = list_pending_fn(older_than_seconds)
    for sheet_id in stuck:
        _ocr_queue.put(("sheet", sheet_id))
    return len(stuck)


def shutdown(timeout: float = 5.0) -> None:
    """Best-effort drain on shutdown. Not strictly necessary — worker
    is daemon and dies with the process — but useful for tests."""
    if _worker_thread is None:
        return
    # Worker doesn't have a stop signal; rely on daemon termination.
    # If empty, return immediately.
    deadline = time.monotonic() + timeout
    while _ocr_queue.qsize() > 0 and time.monotonic() < deadline:
        time.sleep(0.1)


__all__ = [
    "enqueue",
    "enqueue_discovery",
    "queue_size",
    "recover_pending",
    "shutdown",
    "start_worker",
    "worker_alive",
]
