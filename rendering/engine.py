"""Async tile rendering engine (M2). No Qt. No fitz leakage.

Public API::

    engine = RendererEngine(config)
    future = engine.submit(request)          # request.source_path required
    result = future.result(timeout=...)      # RenderResult (RGBA8 bytes)
    engine.invalidate_page(doc_value, page, new_gen)
    engine.shutdown()                        # idempotent, joins workers

Thread model (documented choice, evidence-based — see M2 report):
- ``ThreadPoolExecutor`` hosts N long-running ``_worker_loop`` tasks
  (``N = config.resolved_workers()``). Pull-based: loops block in
  ``Scheduler.next_entry()``; VISIBLE strictly precedes PREFETCH.
- Worker PDF handles are thread-confined (see ``workers.py``); the
  application ``PdfBackend`` handle is never shared here.
- Requests/results are immutable; scheduler owns queue state under its
  lock; no global renderer state; no threads start before first submit
  (lazy pool creation); shutdown closes handles after pool join.

Thread-safety summary: engine methods are thread-safe. ``RenderRequest``
and ``RenderResult`` are immutable and freely shareable. Futures follow
``concurrent.futures`` semantics.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from core.types import Size
from document.errors import InvalidPageIndexError
from rendering import renderer as raster
from rendering.errors import RenderCancelledError, RenderFailedError
from rendering.metrics import RenderMetrics
from rendering.renderer import PIXEL_FORMAT
from rendering.scheduler import EngineConfig, Scheduler
from rendering.types import CancellationToken, RenderRequest, RenderResult, is_stale
from rendering.workers import WorkerHandles

__all__ = ["RendererEngine", "is_stale"]


class RendererEngine:
    """Bounded async tile renderer."""

    def __init__(self, config: EngineConfig | None = None) -> None:
        self._config = config if config is not None else EngineConfig()
        self._metrics = RenderMetrics()
        self._scheduler = Scheduler(self._config, self._metrics)
        self._handles = WorkerHandles()
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._loops_started = 0
        self._shutdown = False
        # Gate on SIMULTANEOUS in-process renders (M3 Gate 5 fix). Queued
        # worker loops park here instead of entering MuPDF together: with
        # bound == worker count this semaphore never blocks (historical
        # behavior); with bound == 1 only one raster section holds the GIL
        # at a time so the GUI thread keeps its 5ms switch slice. Never
        # taken on the submitting (GUI) thread; always released in
        # ``finally`` so shutdown can never deadlock on it.
        self._render_gate = threading.Semaphore(
            self._config.resolved_max_concurrent_renders()
        )

    # -- public API ------------------------------------------------------

    @property
    def config(self) -> EngineConfig:
        """Engine configuration (as constructed)."""
        return self._config

    def submit(
        self, request: RenderRequest, token: CancellationToken | None = None
    ) -> Future:
        """Enqueue a tile request. Returns a ``Future[RenderResult]``.

        ``request.source_path`` must point at a readable PDF. Dedup: an
        identical pending/running request returns the SAME Future.
        """
        with self._lock:
            if self._shutdown:
                raise RenderCancelledError("engine is shut down")
            self._ensure_workers_locked()
        if not request.source_path or not os.path.isfile(request.source_path):
            raise RenderFailedError(f"invalid document path: {request.source_path!r}")
        future, _tok, _dedup = self._scheduler.submit(request, token)
        return future

    def cancel(self, request: RenderRequest) -> bool:
        """Cancel a pending request by key."""
        return self._scheduler.cancel(request)

    def reprioritize(self, keys) -> int:
        """Promote currently-visible queued work to the front (M4.7).

        Promote-only: stale-direction work keeps its rank. Returns the
        number of queued entries promoted.
        """
        return self._scheduler.reprioritize(keys)

    def set_generation(self, document_value: str, page_index: int, generation: int) -> None:
        """Record the current generation for a (doc, page) scope."""
        self._scheduler.set_generation(document_value, page_index, generation)

    def invalidate_page(self, document_value: str, page_index: int, new_generation: int) -> int:
        """Bump generation; best-effort cancel pending older-gen work."""
        return self._scheduler.invalidate_page(document_value, page_index, new_generation)

    def metrics(self) -> dict:
        """Programmatic metrics snapshot."""
        snap = self._metrics.snapshot()
        snap["worker_handles"] = self._handles.live_count()
        snap["pending"] = self._scheduler.pending_count()
        return snap

    def pending_count(self) -> int:
        """Queued + running request count (for pipeline admission control)."""
        return self._scheduler.pending_count()

    def shutdown(self) -> None:
        """Deterministic, idempotent shutdown. No worker survives."""
        with self._lock:
            if self._shutdown:
                executor = None
            else:
                self._shutdown = True
                executor = self._executor
                self._executor = None
        self._scheduler.shutdown()
        if executor is not None:
            executor.shutdown(wait=True)
        self._handles.close_all()

    # -- internals -------------------------------------------------------

    def _ensure_workers_locked(self) -> None:
        if self._executor is None:
            # Pool (and loops) start lazily on first submit, never at app start.
            self._executor = ThreadPoolExecutor(
                max_workers=self._config.resolved_workers(),
                thread_name_prefix="inkfall-render",
            )
            for _ in range(self._config.resolved_workers()):
                self._executor.submit(self._worker_loop)
                self._loops_started += 1

    def _worker_loop(self) -> None:
        while True:
            entry = self._scheduler.next_entry()
            if entry is None:
                return  # shutdown drained
            self._execute(entry)

    def _execute(self, entry) -> None:  # type: ignore[no-untyped-def]
        from rendering.types import RenderRequest as _RR

        request: _RR = entry.request
        future: Future = entry.future
        token: CancellationToken = entry.token
        queue_wait_ms = (time.perf_counter() - entry.enqueue_time) * 1000.0
        try:
            if future.cancelled() or token.is_cancelled:
                raise RenderCancelledError("cancelled before execution")
            t0 = time.perf_counter()
            # Single-render gate: concurrent heavyweight MuPDF rasters hold
            # the GIL and starve the GUI thread (M3 Finding F-1). Queues,
            # dedup, cancellation, and completion are unaffected — only the
            # raster section below is serialized when configured.
            self._render_gate.acquire()
            self._metrics.render_started()
            try:
                # Worker-local page, neutralized to unrotated media geometry;
                # intrinsic is cached per (thread, path, page) because
                # neutralization is sticky (re-reading rotation would give 0).
                try:
                    doc, page, intrinsic = self._handles.prepare_page(
                        request.source_path or "", request.page_index
                    )
                    page_w, page_h = float(page.rect.width), float(page.rect.height)
                except IndexError as e:
                    raise InvalidPageIndexError(
                        f"Invalid page index {request.page_index!r}"
                    ) from e
                data, w, h, render_ms, convert_ms = raster.render_tile_raw(
                    doc, request, page_w, page_h, token,
                    alpha=True, intrinsic_rotation=intrinsic,
                )
                assert data is not None
            finally:
                self._metrics.render_finished()
                self._render_gate.release()
            total_ms = (time.perf_counter() - t0) * 1000.0
            if future.cancelled() or token.is_cancelled:
                raise RenderCancelledError("cancelled during execution")
            result = RenderResult(
                request=request,
                size=Size(float(w), float(h)),
                scale=request.scale,
                success=True,
                data=data,
                width_px=w,
                height_px=h,
                pixel_format=PIXEL_FORMAT,
                stride=w * raster.BYTES_PER_PIXEL,
                generation=request.generation,
                queue_wait_ms=queue_wait_ms,
                render_ms=render_ms,
                convert_ms=convert_ms,
                total_ms=total_ms,
            )
            with self._metrics._lock:
                m = self._metrics
                m.completed += 1
                m.queue_wait_ms.append(queue_wait_ms)
                m.render_ms.append(render_ms)
                m.convert_ms.append(convert_ms)
                m.total_ms.append(total_ms)
            if not future.done():
                future.set_result(result)
        except RenderCancelledError as e:
            with self._metrics._lock:
                self._metrics.cancelled += 1
            if not future.done():
                future.cancel()
            _ = e
        except InvalidPageIndexError as e:
            with self._metrics._lock:
                self._metrics.failed += 1
            if not future.done():
                future.set_exception(e)
        except RenderFailedError as e:
            with self._metrics._lock:
                self._metrics.failed += 1
            if not future.done():
                future.set_exception(e)
        except Exception as e:  # never leak raw backend errors
            with self._metrics._lock:
                self._metrics.failed += 1
            if not future.done():
                future.set_exception(RenderFailedError(f"render failed: {e}"))
        finally:
            self._scheduler.finish(entry)

    @staticmethod
    def _page_path(path: str) -> str:
        return str(Path(path).expanduser())
