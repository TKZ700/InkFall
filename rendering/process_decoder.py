"""Process-isolated full-page decoder (Path F prototype). No Qt, no PyMuPDF text.

Moves ONLY the expensive full-page JPX decode off the GUI process: a bounded
pool of persistent ``spawn`` workers, each owning its own PyMuPDF document
handle, renders the same full-page request Path F already builds and returns
raw bytes over an ordinary pipe/queue. The parent converts/slices with the
existing numpy Path F logic; TileCache identities never change.

Boundary discipline (see ``tests/test_import_boundaries.py``): this module
must never textually import ``pymupdf``/``fitz`` — not even lazily inside a
function (the check is AST-based). The child reuses the already-approved
``rendering.renderer`` + ``rendering.workers`` modules, so pixel output is
structurally identical to the in-process path.

What crosses the process boundary (both directions plain data only):
    parent -> child: (request_id, source_path, page_index, scale,
                      rotation_degrees, full_w_px, full_h_px)
    child -> parent: (request_id, "ok", data, w, h, stride,
                      render_ms, convert_ms)
                  or (request_id, "error", exc_type_name, message)

Never: documents, pages, pixmaps, Qt objects, numpy arrays, Futures.

Lifecycle: workers spawn lazily on first submit (never at construction, so
opening a document or running unrelated tests spawns nothing). Shutdown is
safe with zero workers ever spawned. A dead/timed-out worker is terminated
and eagerly replaced; its in-flight decode fails and the caller falls back
to the existing per-tile renderer.
"""

from __future__ import annotations

import multiprocessing as mp
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass

from document.errors import InvalidPageIndexError
from rendering.errors import RenderCancelledError, RenderFailedError
from rendering.metrics import SAMPLE_LIMIT

__all__ = [
    "PROCESS_DECODE_TIMEOUT_S",
    "MAX_PROCESS_WORKERS",
    "DEFAULT_MAX_TASKS_PER_WORKER",
    "ProcessDecoderConfig",
    "ProcessDecoder",
]

#: Hang-protection timeout, NOT a latency target. Measured cold JPX decodes
#: run ~300-550ms (warm ~3ms); 10s is ~18x the worst observed decode, so it
#: can only fire on a genuinely stuck/dead child. On expiry the worker is
#: terminated and replaced and the decode fails over to tile rendering.
PROCESS_DECODE_TIMEOUT_S = 10.0

#: Cap on the shutdown result-drain phase. Workers normally exit in well
#: under a second once their result pipe is drained; the cap only bounds
#: pathological cases (e.g. a worker stuck inside a decode at close).
SHUTDOWN_DRAIN_TIMEOUT_S = 15.0

#: Per-worker join timeout AFTER the result channel has been drained. Short
#: on purpose: with pipes draining, teardown is unblocked and exit is fast.
#: This is a reduction from the previous blind 10s join, not an inflation —
#: draining (above) is the mechanism, not waiting longer.
SHUTDOWN_JOIN_TIMEOUT_S = 3.0

#: Explicit bound on decoder processes. Benchmarked memory: parent
#: ~110-126MB, each child ~184-242MB peak, so 2 workers cost ~400-500MB
#: transient. Never derive this from cpu_count; benchmark 1 vs 2 explicitly.
MAX_PROCESS_WORKERS = 2

#: Default worker lifetime in successfully completed decode tasks. Forensic
#: evidence: a 536-page scanned JPX PDF grew individual worker RSS roughly
#: 279MB -> 455MB over ~70+ cumulative decodes. 25 tasks x ~300-550ms worst
#: case decode keeps each worker far below that regime, while ~300ms spawn
#: cost amortizes to ~12ms/task. Counted on successful delivery only.
DEFAULT_MAX_TASKS_PER_WORKER = 25

_STOP = "STOP"


def _run_decode(handles, msg):  # type: ignore[no-untyped-def]
    """Execute one full-page render inside the child. Never raises out."""
    from rendering import renderer as raster
    from rendering.types import RenderRequest, TileRect

    request_id, source_path, page_index, scale, rotation, fw, fh = msg
    try:
        doc, page, intrinsic = handles.prepare_page(source_path, page_index)
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        req = RenderRequest(
            page_index=int(page_index),
            scale=float(scale),
            rotation_degrees=int(rotation),
            tile=TileRect(0, 0, int(fw), int(fh)),
        )
        data, w, h, render_ms, convert_ms = raster.render_tile_raw(
            doc, req, page_w, page_h, None,
            alpha=True, intrinsic_rotation=intrinsic,
        )
        assert data is not None
        return (request_id, "ok", data, int(w), int(h),
                int(w) * raster.BYTES_PER_PIXEL, float(render_ms), float(convert_ms))
    except InvalidPageIndexError as e:
        return (request_id, "error", "InvalidPageIndexError", str(e))
    except RenderCancelledError as e:
        return (request_id, "error", "RenderCancelledError", str(e))
    except RenderFailedError as e:
        return (request_id, "error", "RenderFailedError", str(e))
    except Exception as e:  # never leak raw backend errors, mirroring engine
        return (request_id, "error", "RenderFailedError", f"render failed: {e}")


def _worker_main(task_queue, result_queue):  # type: ignore[no-untyped-def]
    """Child entry point. Module-level so ``spawn`` can pickle it."""
    from core.proc_lifetime import install_child_guard

    # Non-Windows backstop so a crashed parent cannot orphan a worker
    # blocked in task_queue.get() (Windows covers this via the job object).
    install_child_guard()
    from rendering.workers import WorkerHandles

    handles = WorkerHandles()
    try:
        while True:
            msg = task_queue.get()
            if msg == _STOP or (isinstance(msg, str) and msg == _STOP):
                break
            result_queue.put(_run_decode(handles, msg))
    finally:
        try:
            handles.close_all()
        except Exception:
            pass


@dataclass
class ProcessDecoderConfig:
    """Decoder pool configuration (all explicit, none derived)."""

    workers: int = MAX_PROCESS_WORKERS
    timeout_s: float = PROCESS_DECODE_TIMEOUT_S
    max_tasks_per_worker: int = DEFAULT_MAX_TASKS_PER_WORKER

    def __post_init__(self) -> None:
        if int(self.workers) < 1:
            raise ValueError("workers must be >= 1")
        if int(self.workers) > MAX_PROCESS_WORKERS:
            raise ValueError(f"workers must be <= {MAX_PROCESS_WORKERS}")
        if float(self.timeout_s) <= 0:
            raise ValueError("timeout_s must be positive")
        if int(self.max_tasks_per_worker) <= 0:
            raise ValueError("max_tasks_per_worker must be positive")


class _WorkerSlot:
    """One child process plus its busy/outstanding state (parent side)."""

    __slots__ = ("proc", "task_queue", "busy", "request_id", "deadline",
                 "completed", "retiring", "retired_sent", "replacing")

    def __init__(self, proc, task_queue):  # type: ignore[no-untyped-def]
        self.proc = proc
        self.task_queue = task_queue
        self.busy = False
        self.request_id: str | None = None
        self.deadline = 0.0
        # Lifetime accounting: successful deliveries only (never carried
        # across replacement — a fresh slot starts all three at zero).
        self.completed = 0
        self.retiring = False
        self.retired_sent = False
        # Claimed by the supervisor for teardown+replacement (C-02): the
        # feeder never assigns to it, no pass re-claims it, and the
        # blocking terminate/join/spawn for it runs OUTSIDE self._lock.
        self.replacing = False


class _Outstanding:
    """Parent-side record for one submitted decode."""

    __slots__ = ("future", "submit_time", "priority", "slot")

    def __init__(self, future, priority, slot):  # type: ignore[no-untyped-def]
        self.future: Future = future
        self.submit_time = time.perf_counter()
        self.priority = priority
        self.slot = slot


class ProcessDecoder:
    """Bounded pool of persistent decode workers (pipe transport only)."""

    def __init__(self, config: ProcessDecoderConfig | None = None) -> None:
        self._config = config if config is not None else ProcessDecoderConfig()
        self._ctx = mp.get_context("spawn")
        self._lock = threading.Lock()
        self._work_ready = threading.Condition(self._lock)
        self._result_queue = self._ctx.Queue()
        self._slots: list[_WorkerSlot] = []
        self._visible: deque[tuple] = deque()
        self._prefetch: deque[tuple] = deque()
        self._outstanding: dict[str, _Outstanding] = {}
        self._started = False
        self._shutdown = False
        self._feeder: threading.Thread | None = None
        self._collector: threading.Thread | None = None
        self.submits = 0
        self.results = 0
        self.errors = 0
        self.timeouts = 0
        self.withdrawn = 0
        self.shutdown_cancelled = 0
        self.worker_restarts = 0
        self.worker_recycles = 0
        self.shutdown_stranded = 0
        # Bounded recent-window latency samples (mirrors RenderMetrics):
        # deque eviction keeps stats() copies flat over long sessions.
        self.ipc_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.child_render_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)

    # -- public API ------------------------------------------------------

    @property
    def config(self) -> ProcessDecoderConfig:
        """Pool configuration (as constructed)."""
        return self._config

    def submit(
        self, request_id: str, source_path: str, page_index: int,
        scale: float, rotation_degrees: int, full_w_px: int, full_h_px: int,
        priority_visible: bool = True,
    ) -> Future:
        """Queue one full-page decode. Returns a ``Future`` of the result tuple.

        The future resolves to ``(data, w, h, stride, render_ms,
        convert_ms)`` or raises the marshaled renderer error. Workers spawn
        lazily here — never before the first submit.
        """
        with self._lock:
            if self._shutdown:
                raise RenderCancelledError("process decoder is shut down")
            self._ensure_started_locked()
            future: Future = Future()
            self._outstanding[request_id] = _Outstanding(
                future, bool(priority_visible), -1
            )
            msg = (request_id, source_path, int(page_index), float(scale),
                   int(rotation_degrees), int(full_w_px), int(full_h_px))
            if priority_visible:
                self._visible.append(msg)
            else:
                self._prefetch.append(msg)
            self.submits += 1
            self._work_ready.notify_all()
            return future

    def withdraw(self, wanted_ids: set[str]) -> int:
        """Withdraw queued (never-assigned) decodes no longer wanted.

        Removes messages with ``request_id`` outside ``wanted_ids`` from both
        lanes. Assigned/running decodes are never touched: feeder assignment
        pops under this same lock, so a message is either still queued here
        or already owned by a worker — never both. Withdrawn futures fail
        with :class:`RenderCancelledError`, which flows through the existing
        pipeline fallback path (per-tile renders if the viewport returns).
        Returns the number of queued requests withdrawn.
        """
        wanted = set(wanted_ids)
        complete_later: list[tuple] = []
        removed = 0
        with self._lock:
            if self._shutdown:
                return 0
            for lane in (self._visible, self._prefetch):
                kept: deque[tuple] = deque()
                while lane:
                    msg = lane.popleft()
                    if msg[0] in wanted:
                        kept.append(msg)
                    else:
                        action = self._fail_locked(
                            msg[0],
                            RenderCancelledError("decode superseded"),
                            count_error=False,
                        )
                        if action is not None:
                            complete_later.append(action)
                        removed += 1
                lane.extend(kept)
            self._work_ready.notify_all()
        # Complete futures lock-free: callbacks execute inline here.
        for action in complete_later:
            self._complete(action)
        return removed

    def pending_count(self) -> int:
        """Queued + in-flight decode count (for admission control)."""
        with self._lock:
            return len(self._visible) + len(self._prefetch) + sum(
                1 for s in self._slots if s.busy
            )

    def worker_pids(self) -> tuple[int, ...]:
        """Live child PIDs (benchmarking/RSS only)."""
        with self._lock:
            return tuple(s.proc.pid for s in self._slots if s.proc.is_alive())

    def prewarm(self) -> None:
        """Spawn workers + pump threads eagerly (never raises).

        Construction spawns nothing by design (documented lifecycle); the
        first decode would otherwise pay the ~300ms ``spawn`` process
        startup on the submitting (GUI) thread. Background pipeline
        preparation calls this so the spawn happens off the GUI thread.
        Idempotent; submits no work. Safe with zero workers ever spawned.
        """
        try:
            with self._lock:
                self._ensure_started_locked()
        except Exception:
            pass

    def stats(self) -> dict:
        """Programmatic snapshot (counters + latency samples)."""
        with self._lock:
            return {
                "workers": self._config.workers,
                "spawned": self._started,
                "live_workers": sum(1 for s in self._slots if s.proc.is_alive()),
                "submits": self.submits,
                "results": self.results,
                "errors": self.errors,
                "timeouts": self.timeouts,
                "withdrawn": self.withdrawn,
                "shutdown_cancelled": self.shutdown_cancelled,
                "shutdown_stranded": self.shutdown_stranded,
                "worker_restarts": self.worker_restarts,
                "worker_recycles": self.worker_recycles,
                "outstanding": len(self._outstanding),
                "ipc_ms": list(self.ipc_ms),
                "child_render_ms": list(self.child_render_ms),
            }

    def shutdown(self) -> None:
        """Deterministic, idempotent shutdown. Safe with zero workers spawned.

        Ordering matters (close-after-backlog evidence): the result queue is
        drained WHILE workers exit. A child that just finished a decode
        cannot complete interpreter teardown until its queued result bytes
        are flushed to a pipe somebody reads; stopping the collector first
        and then joining workers deadlocks that teardown on the full pipe
        and burns the whole join timeout per stuck worker. Draining first
        lets workers reach clean exit, so joins are fast and termination is
        reserved for genuine stragglers (which are counted, never silently
        abandoned).

        Never called with the pipeline lock held (completion callbacks need
        that lock, exactly like the engine join discipline). Futures are
        completed WITHOUT holding the decoder lock (callbacks run inline).
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._work_ready.notify_all()
            feeder, collector = self._feeder, self._collector
        # 1-2. Stop intake: feeder exits on the flag without assigning more.
        if feeder is not None:
            feeder.join(timeout=10)
        # Collector drains until here; join it so the drain loop below is
        # the single reader from this point on.
        if collector is not None:
            collector.join(timeout=10)
        with self._lock:
            slots = list(self._slots)
            self._slots = []
            # Outstanding futures stay registered: results arriving during
            # the drain still deliver through the normal completion path.
        # 3. Ask workers to exit; each finishes its current decode first.
        for slot in slots:
            try:
                slot.task_queue.put(_STOP)
            except Exception:
                pass
        # 4-6. Drain results while workers exit, then join (short: with
        # pipes draining, teardown is unblocked and exit is fast).
        self._drain_until_exit(slots)
        for slot in slots:
            slot.proc.join(timeout=SHUTDOWN_JOIN_TIMEOUT_S)
        # 7. Fail whatever never arrived (exactly once per future).
        with self._lock:
            outstanding = list(self._outstanding.values())
            self._outstanding.clear()
        # Complete futures lock-free: callbacks execute inline here.
        for record in outstanding:
            if not record.future.done():
                record.future.set_exception(
                    RenderCancelledError("process decoder is shut down")
                )
                with self._lock:
                    self.shutdown_cancelled += 1
        # 8-9. Terminate genuine stragglers only, then VERIFY every worker
        # is dead. Survivors are counted in stats (never silently dropped),
        # but shutdown itself stays non-raising so close paths cannot throw.
        for slot in slots:
            if slot.proc.is_alive():
                try:
                    slot.proc.terminate()
                except Exception:
                    pass
                slot.proc.join(timeout=10)
                if slot.proc.is_alive():
                    with self._lock:
                        self.shutdown_stranded += 1
        # 10. Retire the queues LAST, after every owned process is reaped:
        # close() releases the OS handles and cancel_join_thread() makes
        # sure no queue feeder thread can block interpreter exit (or race
        # the spawn finalizers at teardown — the source of the Windows
        # handle-reconstruction Access Denied when teardown happened
        # while handles were still live). Workers are gone at this point,
        # so nobody reads or writes either queue afterwards.
        for slot in slots:
            try:
                slot.task_queue.close()
                slot.task_queue.cancel_join_thread()
            except Exception:
                pass
        try:
            self._result_queue.close()
            self._result_queue.cancel_join_thread()
        except Exception:
            pass

    def _drain_until_exit(self, slots: list) -> None:  # type: ignore[no-untyped-def]
        """Consume the result queue until all workers exited or the cap hits.

        Results for still-active futures deliver normally (so late decodes
        that beat the STOP still land); results with no matching request
        are discarded. No supervision or respawning happens here.
        """
        deadline = time.perf_counter() + SHUTDOWN_DRAIN_TIMEOUT_S
        while time.perf_counter() < deadline:
            if not any(s.proc.is_alive() for s in slots):
                # One last sweep: collect results that arrived with the exit.
                try:
                    while True:
                        res = self._result_queue.get(timeout=0)
                        self._deliver_or_discard(res)
                except Exception:
                    pass
                return
            try:
                res = self._result_queue.get(timeout=0.5)
            except Exception:
                continue
            self._deliver_or_discard(res)

    def _deliver_or_discard(self, res) -> None:  # type: ignore[no-untyped-def]
        """Deliver one drained result (lock-free completion) or drop it."""
        with self._lock:
            action = self._deliver_locked(res)
        if action is not None:
            self._complete(action)

    # -- internals ---------------------------------------------------------

    def _ensure_started_locked(self) -> None:
        if self._started or self._shutdown:
            # A decoder that has begun shutting down must never (re)start:
            # a prewarm thread racing shutdown() would otherwise spawn
            # workers that the shutdown's slot snapshot never sees —
            # orphaned children blocked in task_queue.get() that atexit
            # then joins forever. `_started` stays True either way, so no
            # later call can spawn after cleanup either.
            self._started = True
            return
        self._started = True
        for _ in range(self._config.workers):
            self._slots.append(self._spawn_slot())
        self._feeder = threading.Thread(
            target=self._feeder_loop, name="procdecode-feeder", daemon=True
        )
        self._collector = threading.Thread(
            target=self._collector_loop, name="procdecode-collector", daemon=True
        )
        self._feeder.start()
        self._collector.start()

    def _spawn_slot(self) -> _WorkerSlot:
        """Create one child process + task queue. Needs no lock.

        Named without the ``_locked`` suffix deliberately (C-02): the
        supervisor spawns replacements OUTSIDE ``self._lock`` so the
        ~300ms ``spawn`` never blocks GUI-thread request paths. Startup
        (``_ensure_started_locked``) still calls it under the lock —
        before the collector thread exists, so the two spawn sources
        never overlap.
        """
        task_queue = self._ctx.Queue()
        proc = self._ctx.Process(
            target=_worker_main, args=(task_queue, self._result_queue),
            name="inkfall-page-decode",
        )
        proc.start()
        return _WorkerSlot(proc, task_queue)

    def _feeder_loop(self) -> None:
        """Assign queued decodes to free workers; VISIBLE strictly first."""
        while True:
            # Futures are completed WITHOUT holding the decoder lock:
            # callbacks execute inline and need the pipeline lock.
            complete_later: list[tuple] = []
            with self._lock:
                while True:
                    if self._shutdown:
                        return
                    # Retire before assigning: a free worker at its task
                    # limit gets STOP (once) instead of another decode.
                    # Queued work is untouched and routes to other workers.
                    retiring = self._retiring_slot_locked()
                    if retiring is not None:
                        retiring.retired_sent = True
                        # Grace bound for an orderly exit (reuses the
                        # assignment-deadline field: the slot is idle, so no
                        # task deadline is in force).
                        retiring.deadline = (time.perf_counter()
                                             + self._config.timeout_s)
                        try:
                            retiring.task_queue.put(_STOP)
                        except Exception:
                            pass
                        self._work_ready.notify_all()
                        continue
                    # Peek BEFORE popping: popping a message with no free
                    # worker would silently lose the task (it is outstanding
                    # but never assigned, so no timeout ever fires either).
                    msg = self._peek_msg_locked()
                    slot = self._free_slot_locked()
                    if slot is not None and msg is not None:
                        self._pop_msg_locked(msg)
                        break
                    self._work_ready.wait(timeout=0.5)
                slot.busy = True
                slot.request_id = msg[0]
                slot.deadline = time.perf_counter() + self._config.timeout_s
                record = self._outstanding.get(msg[0])
                if record is not None:
                    record.slot = self._slots.index(slot)
                try:
                    slot.task_queue.put(msg)
                except Exception:
                    slot.busy = False
                    slot.request_id = None
                    action = self._fail_locked(
                        msg[0], RenderFailedError("decode worker unreachable")
                    )
                    if action is not None:
                        complete_later.append(action)
            for action in complete_later:
                self._complete(action)

    def _collector_loop(self) -> None:
        """Route child results to futures; supervise timeouts and deaths."""
        get_timeout = 0.5
        while True:
            # Futures are completed WITHOUT holding the decoder lock:
            # callbacks execute inline and need the pipeline lock.
            complete_later: list[tuple] = []
            replacements: list[tuple] = []
            try:
                res = self._result_queue.get(timeout=get_timeout)
            except Exception:
                res = None
            with self._lock:
                if res is not None:
                    action = self._deliver_locked(res)
                    if action is not None:
                        complete_later.append(action)
                if not self._shutdown:
                    # Supervision phase A (lock held): claim problem slots
                    # and fail their decodes. The blocking teardown and
                    # replacement run in _replace_slots below, OUTSIDE
                    # the lock, so submit/withdraw/pending_count on the
                    # GUI thread never wait on a stuck child (C-02).
                    actions, replacements = self._supervise_locked()
                    complete_later.extend(actions)
            for action in complete_later:
                self._complete(action)
            if replacements:
                self._replace_slots(replacements)
            if self._shutdown:
                # shutdown() fails outstanding futures itself; results
                # still in flight are simply dropped here.
                return

    def _free_slot_locked(self):  # type: ignore[no-untyped-def]
        for slot in self._slots:
            if (not slot.busy and not slot.retiring and not slot.replacing
                    and slot.proc.is_alive()):
                return slot
        return None

    def _retiring_slot_locked(self):  # type: ignore[no-untyped-def]
        """A free worker due for retirement that has not been stopped yet."""
        for slot in self._slots:
            if (not slot.busy and slot.retiring and not slot.retired_sent
                    and not slot.replacing and slot.proc.is_alive()):
                return slot
        return None

    def _peek_msg_locked(self):  # type: ignore[no-untyped-def]
        if self._visible:
            return self._visible[0]
        if self._prefetch:
            return self._prefetch[0]
        return None

    def _pop_msg_locked(self, msg) -> None:  # type: ignore[no-untyped-def]
        """Remove a previously peeked message from its lane."""
        if self._visible and self._visible[0] is msg:
            self._visible.popleft()
            return
        if self._prefetch and self._prefetch[0] is msg:
            self._prefetch.popleft()
            return
        raise AssertionError("feeder message vanished between peek and pop")

    def _deliver_locked(self, res) -> tuple | None:  # type: ignore[no-untyped-def]
        """Record a child result; returns a completion action for lock-free use."""
        request_id = res[0]
        record = self._outstanding.pop(request_id, None)
        if record is None:
            return None  # timed out / cancelled / shut down; nothing to complete
        slot = self._slots[record.slot] if 0 <= record.slot < len(self._slots) else None
        if slot is not None and slot.request_id == request_id:
            slot.busy = False
            slot.request_id = None
        if record.future.done():
            self._work_ready.notify_all()
            return None
        if res[1] == "ok":
            _, _, data, w, h, stride, render_ms, convert_ms = res
            self.results += 1
            total_ms = (time.perf_counter() - record.submit_time) * 1000.0
            self.ipc_ms.append(max(0.0, total_ms - float(render_ms)))
            self.child_render_ms.append(float(render_ms))
            if slot is not None:
                # The decode actually executed in this worker: count it
                # toward its bounded lifetime, then retire it once idle.
                # Delivery just freed the slot, so marking here can never
                # interrupt a running decode. Run-then-stale results count:
                # staleness is decided pipeline-side, after this point.
                # (slot identity was verified against request_id above.)
                slot.completed += 1
                if slot.completed >= self._config.max_tasks_per_worker:
                    slot.retiring = True
            payload = (data, int(w), int(h), int(stride),
                       float(render_ms), float(convert_ms))
            self._work_ready.notify_all()
            return (record.future, "ok", payload)
        _, _, exc_name, message = res
        self.errors += 1
        self._work_ready.notify_all()
        return (record.future, "error", self._translate_error(exc_name, message))

    def _supervise_locked(self) -> tuple[list, list]:
        """Claim problem slots and fail their decodes. LOCK HELD.

        Phase A of the C-02 fix: all bookkeeping that needs ``self._lock``
        happens here — claiming (``replacing=True``), failing the slot's
        in-flight decode, counters, and the non-blocking reap/terminate
        signal — while the potentially blocking work (``join`` up to 10s
        on a genuinely stuck child, the ~300ms ``spawn``) is deferred to
        :meth:`_replace_slots`, which runs on the collector thread
        WITHOUT the lock. The GUI-thread request paths that share this
        lock (``submit``/``withdraw``/``pending_count``) therefore never
        wait on a stuck child.

        Returns ``(complete_actions, claims)``: futures to complete
        lock-free, and ``(index, old_slot, kind)`` claims, where ``kind``
        is ``"dead"`` (process observed dead), ``"retired"`` (ordered out
        but never left) or ``"timeout"`` (busy past its deadline).
        """
        now = time.perf_counter()
        actions: list[tuple] = []
        claims: list[tuple] = []
        for index, slot in enumerate(list(self._slots)):
            if slot.replacing:
                continue  # already claimed; teardown in progress outside
            alive = slot.proc.is_alive()
            if not alive:
                # Reap (non-blocking) and claim; the replacement is
                # spawned outside the lock. An orderly retirement exit
                # counts as a recycle; any other death keeps the existing
                # unexpected-death accounting.
                request_id = slot.request_id
                try:
                    slot.proc.join(timeout=0)
                except Exception:
                    pass
                slot.replacing = True
                if slot.retired_sent:
                    self.worker_recycles += 1
                else:
                    self.worker_restarts += 1
                if request_id is not None:
                    action = self._fail_locked(
                        request_id, RenderFailedError("decode worker died")
                    )
                    if action is not None:
                        actions.append(action)
                claims.append((index, slot, "dead"))
                self._work_ready.notify_all()
                continue
            if slot.retired_sent and not slot.busy and now >= slot.deadline:
                # Ordered out but never left (pathological: a healthy
                # worker exits on STOP within milliseconds). Signal
                # terminate now (non-blocking); the join + replacement
                # happen outside the lock so one stuck slot cannot
                # permanently halve the pool OR freeze request
                # submission. The cap still holds because replacement
                # waits for observed death. The slot is idle by
                # construction: no request to fail.
                slot.replacing = True
                try:
                    slot.proc.terminate()
                except Exception:
                    pass
                claims.append((index, slot, "retired"))
                continue
            if slot.busy and alive and now >= slot.deadline:
                # Past its deadline: terminate (non-blocking signal),
                # fail the in-flight decode so the caller can fall back,
                # and claim. The join + replacement run outside the lock.
                request_id = slot.request_id
                slot.replacing = True
                try:
                    slot.proc.terminate()
                except Exception:
                    pass
                self.timeouts += 1
                self.worker_restarts += 1
                if request_id is not None:
                    action = self._fail_locked(
                        request_id,
                        RenderFailedError(
                            f"process decode timed out after {self._config.timeout_s:.1f}s"
                        ),
                    )
                    if action is not None:
                        actions.append(action)
                claims.append((index, slot, "timeout"))
                self._work_ready.notify_all()
        return actions, claims

    def _replace_slots(self, claims: list) -> None:
        """Blocking teardown + replacement for claimed slots. NO LOCK HELD.

        Phase B/C of the C-02 fix. Invariants preserved:

        - Claimed slots carry ``replacing=True`` (set under the lock in
          :meth:`_supervise_locked`), so the feeder never assigns work to
          them and no later pass re-claims them; their in-flight decodes
          were already failed. The pool is only one worker short during
          the teardown instead of being fully frozen behind the lock.
        - Replacements install under the lock only while the claimed
          slot object still occupies its index (identity check), so slot
          indices stay coherent with ``_outstanding`` bookkeeping.
        - Shutdown interlock: claims observed after ``_shutdown`` are
          dropped without spawning anything; a replacement that already
          spawned is terminated, joined, and its queue retired (shutdown
          step-10 hygiene) — never installed into the emptied slot list,
          never orphaned.
        """
        if not claims:
            return
        with self._lock:
            if self._shutdown:
                return
        spawns: list[tuple] = []
        keeps: list[_WorkerSlot] = []
        for index, old_slot, kind in claims:
            if old_slot.proc.is_alive():
                try:
                    old_slot.proc.join(timeout=10)
                except Exception:
                    pass
            if kind == "retired" and old_slot.proc.is_alive():
                # Survived the terminate: release the claim so the next
                # supervision pass terminates it again (the original
                # retry semantics); no recycle is counted without an
                # observed death.
                keeps.append(old_slot)
                continue
            spawns.append((index, old_slot, self._spawn_slot(), kind))
        if not spawns and not keeps:
            return
        with self._lock:
            for old_slot in keeps:
                old_slot.replacing = False
            for index, old_slot, new_slot, kind in spawns:
                if (not self._shutdown and 0 <= index < len(self._slots)
                        and self._slots[index] is old_slot):
                    self._slots[index] = new_slot
                    if kind == "retired":
                        # Counted only on observed death, exactly like
                        # the original single-phase supervisor.
                        self.worker_recycles += 1
                    self._work_ready.notify_all()
                else:
                    self._discard_fresh_slot(new_slot)
            if keeps:
                self._work_ready.notify_all()

    @staticmethod
    def _discard_fresh_slot(slot: _WorkerSlot) -> None:
        """Tear down a replacement that lost its install race (shutdown).

        Best-effort, mirroring shutdown's step-10 hygiene: terminate,
        short join, then retire the task queue so no queue-feeder thread
        can block interpreter exit or race the spawn finalizers.
        """
        try:
            if slot.proc.is_alive():
                slot.proc.terminate()
                slot.proc.join(timeout=1)
        except Exception:
            pass
        try:
            slot.task_queue.close()
            slot.task_queue.cancel_join_thread()
        except Exception:
            pass

    def _fail_locked(self, request_id: str, exc: Exception,
                     count_error: bool = True) -> tuple | None:
        """Detach a failed decode; returns a completion action for lock-free use.

        Withdrawals (``count_error=False``) are counted separately from
        genuine errors: a superseded decode is routine scheduling, not a
        failure.
        """
        record = self._outstanding.pop(request_id, None)
        if record is None:
            return None
        slot_index = record.slot
        if 0 <= slot_index < len(self._slots):
            slot = self._slots[slot_index]
            if slot.request_id == request_id:
                slot.busy = False
                slot.request_id = None
        if count_error:
            self.errors += 1
        else:
            self.withdrawn += 1
        self._work_ready.notify_all()
        if record.future.done():
            return None
        return (record.future, "error", exc)

    @staticmethod
    def _complete(action: tuple) -> None:
        """Complete one future OUTSIDE the decoder lock (callbacks run inline)."""
        future, kind, payload = action
        if future.done():
            return
        if kind == "ok":
            future.set_result(payload)
        else:
            future.set_exception(payload)

    @staticmethod
    def _translate_error(name: str, message: str) -> Exception:
        if name == "InvalidPageIndexError":
            return InvalidPageIndexError(message)
        if name == "RenderCancelledError":
            return RenderCancelledError(message)
        return RenderFailedError(message)
