"""Task C E4 — fila OCR com jobs tipados ("sheet" | "discovery")."""
from __future__ import annotations

import queue
import threading

import pytest

from app.web import ocr_queue as q


@pytest.fixture()
def fresh_queue(monkeypatch):
    monkeypatch.setattr(q, "_ocr_queue", queue.Queue())
    monkeypatch.setattr(q, "_worker_thread", None)
    monkeypatch.setattr(q, "_processor_fn", None)
    monkeypatch.setattr(q, "_discovery_fn", None)
    return q


class TestTypedJobs:
    def test_enqueue_puts_typed_tuple(self, fresh_queue):
        q.enqueue(5)
        assert q._ocr_queue.get_nowait() == ("sheet", 5)

    def test_enqueue_discovery(self, fresh_queue):
        q.enqueue_discovery(9)
        assert q._ocr_queue.get_nowait() == ("discovery", 9)

    def test_positions_shared_fifo(self, fresh_queue):
        assert q.enqueue(1) == 1
        assert q.enqueue_discovery(2) == 2
        assert q.enqueue(3) == 3
        assert q.queue_size() == 3


class TestWorkerDispatch:
    def test_dispatch_by_kind(self, fresh_queue):
        processed: list[int] = []
        discovered: list[int] = []
        done = threading.Event()

        def _disc(i: int) -> None:
            discovered.append(i)
            done.set()

        q.start_worker(processed.append, _disc)
        q.enqueue(1)
        # item legado (int puro) tem de continuar a ser tratado como sheet
        q._ocr_queue.put(2)
        q.enqueue_discovery(7)
        assert done.wait(3.0)
        q._ocr_queue.join()
        assert processed == [1, 2]
        assert discovered == [7]

    def test_discovery_without_fn_is_dropped(self, fresh_queue):
        processed: list[int] = []
        done = threading.Event()
        q.start_worker(lambda i: (processed.append(i), done.set()))
        q.enqueue_discovery(9)  # sem discovery_fn — não pode crashar
        q.enqueue(1)
        assert done.wait(3.0)
        assert processed == [1]

    def test_callback_exception_swallowed(self, fresh_queue):
        done = threading.Event()

        def _boom(i: int) -> None:
            if i == 1:
                raise RuntimeError("boom")
            done.set()

        q.start_worker(_boom)
        q.enqueue(1)
        q.enqueue(2)
        assert done.wait(3.0)
        assert q.worker_alive()
