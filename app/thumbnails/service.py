"""Library thumbnail service (M4.6.1). Explicit deterministic states.

Lifecycle per document identity::

    NOT_REQUESTED -> QUEUED -> LOADING -> READY
    LOADING -> FAILED (retryable) -> QUEUED ...
    * -> MISSING (fast path, never retried)

Rules:

- The job carries its request key + generation ``(mtime_ns, size)``.
  Results are stored under the **request key**; a stat-drifted result is
  discarded as stale (bounded re-request, never an infinite loop).
- ``cancel_outside`` drops only QUEUED work; visible items are always
  re-requestable, so cancellation can never poison an item.
- Failed items stay retryable: the page re-requests visible failures
  (bounded) and offers manual retry. No infinite tight loops.
- Corrupt disk entries are deleted before regeneration.
- Every worker path emits exactly one terminal signal (``ready``,
  ``failed`` or ``stale``). No GUI event processing on worker threads.
- Threading: one lock guards every shared structure (queue, in-flight
  set, caches, states, counters). Worker completions convert images
  and write the disk cache BEFORE taking the lock, and terminal
  signals are emitted only AFTER releasing it, so a GUI-thread slot
  receiving a signal can re-enter ``request()`` without deadlock.
- ``shutdown()`` stops intake, drops the queue and joins the pool;
  the shell calls it on application exit.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QImage

from app.previews import flatten_on_white
from document.backend import render_cover_preview

__all__ = [
    "ThumbnailService",
    "ThumbState",
    "MAX_MEMORY_ENTRIES",
    "MAX_MEMORY_BYTES",
    "MAX_QUEUED",
    "MAX_DRIFT_RETRIES",
]

MAX_MEMORY_ENTRIES = 128
MAX_MEMORY_BYTES = 32 * 1024 * 1024
MAX_QUEUED = 200
#: Re-requests after stat drift before parking as retryable FAILED.
MAX_DRIFT_RETRIES = 3
PREVIEW_SIDE_PX = 256
#: Worker threads for background raster jobs. The pump admits at most
#: this many in-flight renders; both sides read this one constant.
POOL_THREADS = 2


class ThumbState:
    """Explicit per-item thumbnail states."""

    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    MISSING = "missing"


def cache_key(normalized_path: str, modified_ns: int, size_bytes: int) -> str:
    """Stable disk/memory key for one file revision (integer ns, no float drift)."""
    raw = f"{normalized_path}|{int(modified_ns)}|{int(size_bytes)}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()


def _disk_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"{key}.png")


def _stat_generation(path: str) -> tuple[int, int] | None:
    """Current ``(mtime_ns, size)`` or None when unreadable."""
    try:
        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


@dataclass(slots=True)
class _Request:
    item_id: str
    path: str
    key: str
    generation: tuple[int, int]


class _RenderJob(QRunnable):
    """Background raster job (never touches the GUI thread)."""

    def __init__(self, service: ThumbnailService, request: _Request) -> None:
        super().__init__()
        self._service = service
        self._request = request
        self.setAutoDelete(True)

    def run(self) -> None:
        result = render_cover_preview(self._request.path, PREVIEW_SIDE_PX)
        self._service._on_job_finished(self._request, result)


class ThumbnailService(QObject):
    """Facade owned by the library page (one per application)."""

    ready = Signal(str, QImage)  # item_id, image (current result accepted)
    failed = Signal(str)  # item_id (retryable failure)
    stale = Signal(str)  # item_id (discarded drifted result; re-request)

    def __init__(self, cache_dir: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if cache_dir is None:
            cache_dir = os.path.join(
                os.path.expanduser("~"), ".cache", "inkfall", "thumbs"
            )
        self._cache_dir = cache_dir
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
        except OSError:
            pass
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(POOL_THREADS)
        # Guards every shared structure below. Never held across
        # rendering, image conversion, disk I/O, waits, or signal
        # emission (GUI-thread slots re-enter the public API).
        self._lock = threading.Lock()
        self._memory: OrderedDict[str, QImage] = OrderedDict()
        self._memory_bytes = 0
        self._pending: dict[str, str] = {}  # item_id -> path (QUEUED)
        self._meta: dict[str, _Request] = {}  # item_id -> request (QUEUED/LOADING)
        self._inflight: set[str] = set()  # item_id (LOADING)
        self._states: dict[str, str] = {}  # item_id -> ThumbState
        self._drift: dict[str, int] = {}  # item_id -> consecutive drift discards
        self._accepting = True
        # Instrumentation for the performance gate.
        self.renders_started = 0
        self.stale_discarded = 0

    def shutdown(self) -> None:
        """Stop intake, drop queued work and join the pool (exit path).

        The pool join never happens under ``self._lock``: a worker
        completing during the wait takes the lock in its completion
        bookkeeping.
        """
        with self._lock:
            self._accepting = False
            self._pending.clear()
            self._meta.clear()
        try:
            self._pool.waitForDone(3000)
        except Exception:
            pass
        with self._lock:
            self._inflight.clear()

    # -- introspection (testable) ------------------------------------------

    @property
    def memory_entries(self) -> int:
        """Number of images in the memory cache."""
        with self._lock:
            return len(self._memory)

    @property
    def memory_bytes(self) -> int:
        """Bytes held by the memory cache."""
        with self._lock:
            return self._memory_bytes

    @property
    def pending_count(self) -> int:
        """Queued + in-flight request count."""
        with self._lock:
            return len(self._pending) + len(self._inflight)

    def state_of(self, item_id: str) -> str:
        """Current explicit state (defaults to NOT_REQUESTED)."""
        with self._lock:
            return self._states.get(item_id, ThumbState.NOT_REQUESTED)

    def cached(self, key: str) -> QImage | None:
        """Return the memory-cached image for a key (LRU touch)."""
        with self._lock:
            image = self._memory.get(key)
            if image is not None:
                self._memory.move_to_end(key)
            return image

    # -- public API (GUI thread) -------------------------------------------

    def request(
        self,
        item_id: str,
        normalized_path: str,
        modified_ts: float,
        size_bytes: int,
        missing: bool = False,
    ) -> QImage | None:
        """Request a thumbnail; returns a cached image or None (async).

        Terminal signals (``ready`` / ``failed`` / ``stale``) always
        follow for items that enter QUEUED/LOADING. Missing files resolve
        to MISSING immediately without spawning work.
        """
        if missing or not normalized_path:
            with self._lock:
                self._set_state(item_id, ThumbState.MISSING)
            self.failed.emit(item_id)
            return None
        if not self._accepting:
            return None
        # Prefer a live integer-nanosecond stat (exact); fall back to the
        # caller's float timestamp only when the file is unreadable here.
        live = _stat_generation(normalized_path)
        if live is not None:
            gen_ns, gen_size = live
        else:
            try:
                gen_ns = int(float(modified_ts) * 1_000_000_000)
            except (TypeError, ValueError):
                gen_ns = 0
            gen_size = int(size_bytes)
        key = cache_key(normalized_path, gen_ns, gen_size)
        hit = self.cached(key)
        if hit is not None:
            with self._lock:
                self._set_state(item_id, ThumbState.READY)
            return hit
        disk = _disk_path(self._cache_dir, key)
        try:
            if os.path.isfile(disk):
                image = flatten_on_white(QImage(disk))
                if not image.isNull():
                    with self._lock:
                        self._store_memory(key, image)
                        self._set_state(item_id, ThumbState.READY)
                    return image
                # Corrupt cache entry: invalidate so it regenerates once.
                try:
                    os.remove(disk)
                except OSError:
                    pass
        except Exception:
            pass
        with self._lock:
            if item_id in self._pending or item_id in self._inflight:
                return None  # ALREADY QUEUED/LOADING: terminal signal follows
            if len(self._pending) >= MAX_QUEUED:
                return None  # DROPPED (queue full): caller re-requests later
            request = _Request(item_id, normalized_path, key, (gen_ns, gen_size))
            self._pending[item_id] = normalized_path
            self._meta[item_id] = request
            self._set_state(item_id, ThumbState.QUEUED)
        self._pump()
        return None

    def retry(self, item_id: str, normalized_path: str, modified_ts: float,
              size_bytes: int, missing: bool = False) -> QImage | None:
        """Manual retry: clears drift backoff then requests normally."""
        with self._lock:
            self._drift.pop(item_id, None)
            if self._states.get(item_id) == ThumbState.FAILED:
                self._set_state(item_id, ThumbState.NOT_REQUESTED)
        return self.request(item_id, normalized_path, modified_ts, size_bytes, missing)

    def cancel_outside(self, visible_ids: set[str]) -> int:
        """Drop QUEUED requests for items that left the viewport.

        In-flight work is never cancelled; items becoming visible again
        simply re-request (states return to QUEUED).
        """
        with self._lock:
            doomed = [i for i in self._pending if i not in visible_ids]
            for item_id in doomed:
                self._pending.pop(item_id, None)
                self._meta.pop(item_id, None)
                if self._states.get(item_id) == ThumbState.QUEUED:
                    self._set_state(item_id, ThumbState.NOT_REQUESTED)
        return len(doomed)

    def clear_memory(self) -> None:
        """Empty the memory cache (disk cache survives)."""
        with self._lock:
            self._memory.clear()
            self._memory_bytes = 0

    # -- internals ----------------------------------------------------------

    def _set_state(self, item_id: str, state: str) -> None:
        """Store one item state. Caller must hold ``self._lock``."""
        self._states[item_id] = state

    def _pump(self) -> None:
        """Dispatch queued work while pool slots are free.

        Serialized by ``self._lock`` so a worker completion and a
        GUI-thread re-request can never dispatch the same item twice
        or interleave the queue. ``pool.start`` is non-blocking, so the
        lock is never held across a wait; start failures are collected
        and reported after the lock is released.
        """
        failed_ids: list[str] = []
        with self._lock:
            while self._accepting and self._pending \
                    and len(self._inflight) < POOL_THREADS:
                item_id = next(iter(self._pending))
                self._pending.pop(item_id)
                request = self._meta.get(item_id)
                if request is None:
                    continue
                self._inflight.add(item_id)
                self._set_state(item_id, ThumbState.LOADING)
                self.renders_started += 1
                try:
                    self._pool.start(_RenderJob(self, request))
                except Exception:
                    self._inflight.discard(item_id)
                    self._meta.pop(item_id, None)
                    self._set_state(item_id, ThumbState.FAILED)
                    failed_ids.append(item_id)
        for item_id in failed_ids:
            self.failed.emit(item_id)

    def _store_memory(self, key: str, image: QImage) -> None:
        """Insert into the byte-budgeted LRU. Caller must hold ``self._lock``."""
        size = image.width() * image.height() * 4
        old = self._memory.pop(key, None)
        if old is not None:
            self._memory_bytes -= old.width() * old.height() * 4
        self._memory[key] = image
        self._memory_bytes += size
        while (len(self._memory) > MAX_MEMORY_ENTRIES or self._memory_bytes > MAX_MEMORY_BYTES) and self._memory:
            _k, evicted = self._memory.popitem(last=False)
            self._memory_bytes -= evicted.width() * evicted.height() * 4
            self._memory_bytes = max(0, self._memory_bytes)

    @Slot()
    def _on_job_finished(self, request: _Request, result: tuple | None) -> None:
        """Worker-thread completion for one render job.

        Runs on a pool worker thread. Stat, image conversion and the
        PNG write happen here, before the lock; the locked section is
        pure bookkeeping; the terminal signal is emitted after the lock
        is released so a GUI-thread slot can re-enter ``request()``
        without deadlock. The emitted image is the local conversion
        result — never a shared field, so two concurrent completions
        cannot deliver each other's pixels.
        """
        item_id = request.item_id
        current = None
        drifted = False
        image: QImage | None = None
        if result is not None:
            # Stat-drift guard: the file changed mid-render. Discard the
            # stale bytes; bounded internal re-request, never an infinite
            # loop (terminal FAILED at MAX_DRIFT_RETRIES).
            current = _stat_generation(request.path)
            drifted = current is not None \
                and tuple(current) != tuple(request.generation)
            if not drifted:
                try:
                    data, width, height, stride, _page_count = result
                    candidate = QImage(
                        data, width, height, stride,
                        QImage.Format.Format_RGBA8888,
                    ).copy()
                    if not candidate.isNull():
                        # Preview paper: transparent page pixels become
                        # white before the display copy is cached (memory
                        # and disk), so fresh and cached previews match.
                        # The PDF itself is never modified.
                        image = flatten_on_white(candidate)
                        try:
                            image.save(
                                _disk_path(self._cache_dir, request.key),
                                "PNG")
                        except Exception:
                            pass
                except Exception:
                    image = None
        terminal: str | None = None
        ready_image: QImage | None = None
        with self._lock:
            self._inflight.discard(item_id)
            self._meta.pop(item_id, None)
            if result is None:
                self._set_state(item_id, ThumbState.FAILED)
                terminal = "failed"
            elif drifted:
                self.stale_discarded += 1
                drifts = self._drift.get(item_id, 0) + 1
                self._drift[item_id] = drifts
                if drifts >= MAX_DRIFT_RETRIES:
                    self._set_state(item_id, ThumbState.FAILED)
                    terminal = "failed"
                else:
                    fresh_key = cache_key(request.path, current[0], current[1])
                    fresh = _Request(
                        item_id, request.path, fresh_key,
                        (current[0], current[1]))
                    self._pending[item_id] = request.path
                    self._meta[item_id] = fresh
                    self._set_state(item_id, ThumbState.QUEUED)
                    terminal = "stale"
            elif image is None:
                self._set_state(item_id, ThumbState.FAILED)
                terminal = "failed"
            else:
                self._drift.pop(item_id, None)
                # Store under the REQUEST key (never a recomputed key).
                self._store_memory(request.key, image)
                self._set_state(item_id, ThumbState.READY)
                terminal = "ready"
                ready_image = image
        if terminal == "ready":
            self.ready.emit(item_id, ready_image)
        elif terminal == "failed":
            self.failed.emit(item_id)
        elif terminal == "stale":
            self.stale.emit(item_id)
        self._pump()
