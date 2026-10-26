"""M2 engine tests: dedup, cancel, generation, queue, priority, lifecycle.

Deterministic by construction: scheduler-level tests need no threads;
engine tests use pre-cancelled tokens, barriers, and Future identity —
never timing sleeps for correctness.
"""

from __future__ import annotations

import threading
from concurrent import futures
from concurrent.futures import CancelledError, ThreadPoolExecutor

import pytest

from core.types import DocumentId, PageId
from document.errors import InvalidPageIndexError
from rendering.engine import RendererEngine
from rendering.errors import QueueFullError, RenderCancelledError
from rendering.metrics import RenderMetrics
from rendering.scheduler import EngineConfig, Scheduler
from rendering.types import CancellationToken, RenderPriority, RenderRequest, TileRect, is_stale
from rendering.workers import WorkerHandles
from tests.helpers import make_probe_pdf, make_rich_pdf


def _req(pdf: str, page_index: int = 0, **kw) -> RenderRequest:
    args: dict = dict(
        document_id=DocumentId("e"),
        page_id=PageId(f"e:p{page_index}"),
        page_index=page_index,
        tile=TileRect(0, 0, 256, 256),
        source_path=pdf,
    )
    args.update(kw)
    return RenderRequest(**args)


# -- dedup -----------------------------------------------------------------

def test_identical_requests_share_one_future(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "d.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=2))
    try:
        r = _req(pdf)
        f1 = engine.submit(r)
        f2 = engine.submit(r)
        assert f1 is f2
        assert f1.result(timeout=60).success is True
        assert engine.metrics()["deduplicated"] >= 1
    finally:
        engine.shutdown()


def test_different_generations_do_not_dedup(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "g.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        f1 = engine.submit(_req(pdf, generation=10))
        f2 = engine.submit(_req(pdf, generation=11))
        assert f1 is not f2
        assert f1.result(timeout=60).generation == 10
        assert f2.result(timeout=60).generation == 11
    finally:
        engine.shutdown()


# -- cancellation ----------------------------------------------------------

def test_cancel_before_execution_produces_no_success(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "c.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        token = CancellationToken()
        token.cancel()
        fut = engine.submit(_req(pdf), token=token)
        with pytest.raises(CancelledError):
            fut.result(timeout=60)
        assert engine.metrics()["cancelled"] >= 1
    finally:
        engine.shutdown()


def test_cancel_queued_request_scheduler_level(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "q.pdf"))
    sched = Scheduler(EngineConfig(max_pending=8), RenderMetrics())
    fut, _tok, _ = sched.submit(_req(pdf))
    assert sched.cancel(_req(pdf)) is True
    assert fut.cancelled()
    assert sched.cancel(_req(pdf)) is False  # already done


# -- generation ------------------------------------------------------------

def test_is_stale_helper():
    from rendering.types import RenderResult

    old = RenderResult(generation=10)
    assert is_stale(old, 11) is True
    assert is_stale(old, 10) is False


def test_invalidate_page_cancels_pending_older_gen():
    sched = Scheduler(EngineConfig(max_pending=8), RenderMetrics())
    r10 = RenderRequest(document_id=DocumentId("d"), page_id=PageId("d:p0"),
                       page_index=0, generation=10, source_path="x.pdf")
    r11 = RenderRequest(document_id=DocumentId("d"), page_id=PageId("d:p0"),
                       page_index=0, generation=11, source_path="x.pdf")
    f10, _, _ = sched.submit(r10)
    n = sched.invalidate_page("d", 0, 11)
    assert n == 1
    assert f10.cancelled()
    f11, _, _ = sched.submit(r11)  # current gen accepted afterwards
    assert not f11.done()


# -- queue bounds + priority (scheduler level, deterministic) ---------------

def test_visible_refused_when_full(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "f.pdf"))
    sched = Scheduler(EngineConfig(max_pending=2), RenderMetrics())
    sched.submit(_req(pdf, tile=TileRect(0, 0, 64, 64)))
    sched.submit(_req(pdf, tile=TileRect(64, 0, 64, 64)))
    with pytest.raises(QueueFullError):
        sched.submit(_req(pdf, tile=TileRect(128, 0, 64, 64)))
    assert sched.pending_count() == 2


def test_prefetch_drops_oldest_and_counts(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    metrics = RenderMetrics()
    sched = Scheduler(EngineConfig(max_pending=2), metrics)
    a = _req(pdf, tile=TileRect(0, 0, 64, 64), priority=RenderPriority.PREFETCH)
    b = _req(pdf, tile=TileRect(64, 0, 64, 64), priority=RenderPriority.PREFETCH)
    c = _req(pdf, tile=TileRect(128, 0, 64, 64), priority=RenderPriority.PREFETCH)
    fa, _, _ = sched.submit(a)
    sched.submit(b)
    sched.submit(c)  # drops a
    assert fa.cancelled()
    assert metrics.snapshot()["dropped_prefetch"] == 1
    assert sched.pending_count() == 2


def test_visible_served_before_prefetch(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "o.pdf"))
    sched = Scheduler(EngineConfig(max_pending=8), RenderMetrics())
    sched.submit(_req(pdf, priority=RenderPriority.PREFETCH))
    sched.submit(_req(pdf, tile=TileRect(64, 0, 64, 64), priority=RenderPriority.VISIBLE))
    first = sched.next_entry()
    assert first is not None and first.request.priority == RenderPriority.VISIBLE


# -- worker isolation ------------------------------------------------------

def test_workers_do_not_share_handles(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "iso.pdf"))
    handles = WorkerHandles()
    barrier = threading.Barrier(3)  # 2 workers + main
    ids: dict[int, int] = {}

    def probe(slot: int) -> None:
        doc = handles.acquire(pdf)
        ids[slot] = id(doc)
        barrier.wait(timeout=30)
        # Still the same handle after the rendezvous (thread-confined).
        assert handles.acquire(pdf) is doc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(probe, 1)
            f2 = pool.submit(probe, 2)
            barrier.wait(timeout=30)
            f1.result(timeout=30)
            f2.result(timeout=30)
        assert ids[1] != ids[2]
    finally:
        handles.close_all()


# -- lifecycle / shutdown ---------------------------------------------------

def test_shutdown_is_deterministic_and_idempotent(tmp_path):
    import threading as th

    pdf = make_rich_pdf(str(tmp_path / "s.pdf"), pages=2)
    engine = RendererEngine(EngineConfig(worker_count=2))
    # Distinct tiles/generations so dedup can never collapse submissions here.
    futs = [engine.submit(_req(pdf, page_index=i % 2,
                               tile=TileRect(i * 16, 0, 64, 64),
                               generation=i)) for i in range(6)]
    for f in futs:
        assert f.result(timeout=60).success is True
    assert engine.metrics()["completed"] >= 6
    engine.shutdown()
    engine.shutdown()  # safe repeat
    assert engine.metrics()["worker_handles"] == 0
    render_threads = [t for t in th.enumerate() if t.name.startswith("inkfall-render")]
    assert render_threads == []
    with pytest.raises(RenderCancelledError):
        engine.submit(_req(pdf))


def test_no_threads_start_before_first_submit(tmp_path):
    import threading as th

    engine = RendererEngine(EngineConfig(worker_count=2))
    try:
        assert [t for t in th.enumerate() if t.name.startswith("inkfall-render")] == []
    finally:
        engine.shutdown()


def test_invalid_page_error_translation(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "e.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        with pytest.raises(InvalidPageIndexError):
            engine.submit(_req(pdf, page_index=42)).result(timeout=60)
        snap = engine.metrics()
        assert snap["failed"] >= 1
        # Raw PyMuPDF types never surface.
        try:
            engine.submit(_req(pdf, page_index=43)).result(timeout=60)
        except InvalidPageIndexError as e:
            assert "fitz" not in type(e).__module__ and "pymupdf" not in type(e).__module__
    finally:
        engine.shutdown()


def test_metrics_snapshot_shape(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "m.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        engine.submit(_req(pdf)).result(timeout=60)
        snap = engine.metrics()
        for key in ("submitted", "completed", "failed", "cancelled", "deduplicated",
                    "dropped_prefetch", "queue_full", "pending", "worker_handles"):
            assert key in snap
        assert snap["total_ms"]["count"] >= 1.0
        assert snap["total_ms"]["p50"] > 0.0
    finally:
        engine.shutdown()
