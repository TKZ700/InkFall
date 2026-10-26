"""Bounded priority scheduler (M2). Owns all queue state (lock-guarded).

Design:
- Two FIFO lanes: VISIBLE (rank 0) strictly before PREFETCH (rank 1).
- Bounded: ``pending >= max_pending`` on submit. VISIBLE -> QueueFullError
  (never silently dropped). PREFETCH -> oldest queued PREFETCH is dropped
  (its future is cancelled) to make room.
- Dedup: identical key already pending/running returns the same Future.
- Generation: entries carry their generation; ``invalidate_page`` cancels
  pending older-generation entries for a (doc, page) scope. Completed
  results always echo generation for consumer-side ``is_stale`` checks.
- No threads here; the engine's worker loops pull via :meth:`next_entry`.

Thread-safety: every mutation/read of queue state holds ``self._lock``.
``threading.Condition`` wakes sleeping workers.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from rendering.errors import QueueFullError, RenderCancelledError
from rendering.metrics import RenderMetrics
from rendering.types import CancellationToken, RenderPriority, RenderRequest

__all__ = ["EngineConfig", "Scheduler", "ScheduledEntry"]


@dataclass
class EngineConfig:
    """Renderer configuration (all configurable, none hardcoded downstream)."""

    worker_count: int = 0  # 0 -> min(4, cpu_count)
    max_pending: int = 64
    tile_size: int = 512
    max_concurrent_renders: int = 0  # 0 -> no gate (one render per worker)

    def resolved_workers(self) -> int:
        """Effective worker count."""
        import os

        if self.worker_count > 0:
            return self.worker_count
        try:
            cpu = os.cpu_count() or 4
        except Exception:
            cpu = 4
        return max(1, min(4, cpu))

    def resolved_max_concurrent_renders(self) -> int:
        """Effective cap on simultaneous in-process renders.

        ``0`` preserves the historical behavior (every worker loop may
        render at once). A positive value serializes the GIL-holding
        raster section behind a semaphore without touching submission,
        queueing, cancellation, or completion semantics.
        """
        if self.max_concurrent_renders > 0:
            return self.max_concurrent_renders
        return self.resolved_workers()


@dataclass
class ScheduledEntry:
    """One queued unit of work."""

    request: RenderRequest
    future: Future = field(default_factory=Future)
    token: CancellationToken = field(default_factory=CancellationToken)
    enqueue_time: float = field(default_factory=time.perf_counter)
    seq: int = 0


class Scheduler:
    """Lock-guarded bounded priority queue with dedup + invalidation."""

    def __init__(self, config: EngineConfig, metrics: RenderMetrics) -> None:
        self._config = config
        self._metrics = metrics
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._heap: list[tuple[int, int, ScheduledEntry]] = []
        self._pending: dict[tuple, ScheduledEntry] = {}
        self._seq = itertools.count()
        self._shutdown = False
        self._current_generation: dict[tuple[str, int], int] = {}

    # -- submission ------------------------------------------------------

    def submit(
        self, request: RenderRequest, token: CancellationToken | None = None
    ) -> tuple[Future, CancellationToken, bool]:
        """Enqueue a request. Returns (future, token, deduplicated)."""
        with self._lock:
            if self._shutdown:
                raise RenderCancelledError("scheduler is shut down")
            key = request.key()
            existing = self._pending.get(key)
            if existing is not None and not existing.future.done():
                with self._metrics._lock:
                    self._metrics.deduplicated += 1
                # M4.7 dedup promotion: a PREFETCH-queued tile re-requested
                # as VISIBLE jumps to the front (same Future, same size —
                # only its heap position changes).
                if (request.priority == RenderPriority.VISIBLE
                        and existing.request.priority != RenderPriority.VISIBLE):
                    existing.request = request
                    self._promote_front_locked({key})
                return existing.future, existing.token, True
            if len(self._pending) >= self._config.max_pending:
                if request.priority == RenderPriority.VISIBLE:
                    with self._metrics._lock:
                        self._metrics.queue_full += 1
                    raise QueueFullError(
                        f"render queue full ({self._config.max_pending}); VISIBLE refused"
                    )
                # PREFETCH: drop oldest queued PREFETCH to make room.
                self._drop_oldest_prefetch_locked()
            entry = ScheduledEntry(
                request=request,
                token=token if token is not None else CancellationToken(),
                seq=next(self._seq),
            )
            rank = 0 if request.priority == RenderPriority.VISIBLE else 1
            heapq.heappush(self._heap, (rank, entry.seq, entry))
            self._pending[key] = entry
            with self._metrics._lock:
                self._metrics.submitted += 1
            self._cond.notify()
            return entry.future, entry.token, False

    def _promote_front_locked(self, keys: set[tuple]) -> int:
        """Move queued entries for ``keys`` ahead of all other queued work.

        M4.7 reversal support (promote-only): matched heap tuples are
        rewritten to rank 0 with sequence numbers below the current
        minimum (previous relative order preserved), then the heap is
        restored. No insert/delete: bounds, drop-oldest-prefetch,
        dedup, shutdown, and metrics semantics are untouched. Stale work
        keeps its rank — it is never demoted. Returns promoted count.
        """
        if not keys:
            return 0
        matches: list[int] = [
            i for i, (_rank, _seq, entry) in enumerate(self._heap)
            if entry.request.key() in keys and not entry.future.done()
        ]
        if not matches:
            return 0
        order = sorted(
            matches, key=lambda i: (self._heap[i][0], self._heap[i][1]))
        floor = min(seq for _rank, seq, _entry in self._heap)
        seq = floor - len(order) - 1
        for i in order:
            _rank, _old_seq, entry = self._heap[i]
            self._heap[i] = (0, seq, entry)
            seq += 1
        heapq.heapify(self._heap)
        self._cond.notify_all()
        return len(order)

    def reprioritize(self, keys) -> int:
        """Promote currently-visible queued work to the front (M4.7).

        Called on scroll-direction reversal with the current visible
        request keys. Promote-only: stale-direction work is never
        demoted or dropped by this call. Thread-safe; returns count.
        """
        with self._lock:
            if self._shutdown:
                return 0
            return self._promote_front_locked(set(keys))

    def _drop_oldest_prefetch_locked(self) -> None:
        oldest_idx: int | None = None
        for i, (rank, _seq, entry) in enumerate(self._heap):
            if rank == 1 and not entry.future.done():
                oldest_idx = i
                break
        if oldest_idx is None:
            # No droppable prefetch (all visible): refuse like VISIBLE.
            with self._metrics._lock:
                self._metrics.queue_full += 1
            raise QueueFullError("render queue full of VISIBLE work; PREFETCH refused")
        _rank, _seq, victim = self._heap.pop(oldest_idx)
        heapq.heapify(self._heap)
        self._pending.pop(victim.request.key(), None)
        victim.token.cancel()
        if not victim.future.done():
            victim.future.cancel()
        with self._metrics._lock:
            self._metrics.dropped_prefetch += 1

    # -- consumption -----------------------------------------------------

    def next_entry(self) -> ScheduledEntry | None:
        """Blocking pop for worker loops. None only after shutdown drain."""
        with self._cond:
            while True:
                while self._heap:
                    _rank, _seq, entry = heapq.heappop(self._heap)
                    if entry.future.done():
                        # Already cancelled/dropped; retire it so _pending
                        # cannot leak entries that will never execute.
                        if self._pending.get(entry.request.key()) is entry:
                            self._pending.pop(entry.request.key(), None)
                        continue
                    return entry
                if self._shutdown:
                    return None
                self._cond.wait()

    def finish(self, entry: ScheduledEntry) -> None:
        """Remove a completed entry from the pending map."""
        with self._lock:
            if self._pending.get(entry.request.key()) is entry:
                self._pending.pop(entry.request.key(), None)

    # -- cancellation / generation ---------------------------------------

    def cancel(self, request: RenderRequest) -> bool:
        """Cancel a pending request by key. Returns True if found."""
        with self._lock:
            entry = self._pending.get(request.key())
            if entry is None or entry.future.done():
                return False
            entry.token.cancel()
            entry.future.cancel()
            return True

    def set_generation(self, document_value: str, page_index: int, generation: int) -> None:
        """Record the current generation for a (doc, page) scope."""
        with self._lock:
            self._current_generation[(document_value, page_index)] = generation

    def invalidate_page(self, document_value: str, page_index: int, new_generation: int) -> int:
        """Bump generation; cancel pending older-gen entries. Returns count."""
        cancelled = 0
        with self._lock:
            self._current_generation[(document_value, page_index)] = new_generation
            for entry in list(self._pending.values()):
                r = entry.request
                if (
                    r.document_id.value == document_value
                    and r.page_index == page_index
                    and r.generation < new_generation
                    and not entry.future.done()
                ):
                    entry.token.cancel()
                    entry.future.cancel()
                    cancelled += 1
        return cancelled

    def pending_count(self) -> int:
        """Current pending (queued + running) count."""
        with self._lock:
            return len(self._pending)

    def shutdown(self) -> None:
        """Stop accepting work, cancel pending, wake all workers."""
        with self._lock:
            self._shutdown = True
            for entry in list(self._pending.values()):
                if not entry.future.done():
                    entry.token.cancel()
                    entry.future.cancel()
            self._cond.notify_all()
