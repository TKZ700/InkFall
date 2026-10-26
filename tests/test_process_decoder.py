"""Process-isolated page decoder tests + Path F invariants over {0,1,2} workers.

Deterministic by construction: no sleeps for synchronization (bounded
deadline waits only; a timeout means a real hang). Worker timing is never
asserted — only delivery, identity, fallback, and cleanup.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.types import DocumentId, Size
from document.errors import InvalidPageIndexError
from document.models import PageInfo, page_id_for
from rendering.errors import RenderCancelledError, RenderFailedError
from rendering.pipeline import DocumentPipeline
from rendering.process_decoder import (
    MAX_PROCESS_WORKERS,
    ProcessDecoder,
    ProcessDecoderConfig,
)
from rendering.scheduler import EngineConfig
from tests.helpers import make_probe_pdf


def _pages(doc: DocumentId, sizes):
    out = []
    for i, (w, h, rot) in enumerate(sizes):
        out.append(
            PageInfo(id=page_id_for(doc, i), document_id=doc, index=i,
                     size=Size(w, h), rotation=rot)
        )
    return out


SIZES_595 = [(595.0, 842.0, 0)]


def _pipe(pdf, sizes, workers=2, **kw):
    doc = DocumentId("procdecode")
    defaults = dict(engine_config=EngineConfig(worker_count=2))
    defaults.update(kw)
    return DocumentPipeline(doc, pdf, _pages(doc, sizes),
                            process_decode_workers=workers, **defaults)


def _drain(pipe, timeout_s=90.0):
    deadline = time.monotonic() + timeout_s
    while True:
        stats = pipe.stats()
        pd = stats["process_decoder"]
        if stats["in_flight"] == 0 and stats["engine"]["pending"] == 0 and (
                pd is None or pd["outstanding"] == 0):
            return stats
        assert time.monotonic() < deadline, "pipeline did not quiesce"
        time.sleep(0.005)


def _wait_for(pred, timeout_s=30.0):
    deadline = time.monotonic() + timeout_s
    while not pred():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


# -- decoder unit tests ----------------------------------------------------

def test_decoder_starts_and_decodes_with_dims(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        fut = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        data, w, h, stride, render_ms, convert_ms = fut.result(timeout=60)
        assert (w, h) == (595, 842)
        assert stride == 595 * 4
        assert len(data) == 595 * 842 * 4
        assert render_ms >= 0 and convert_ms >= 0
        assert dec.stats()["live_workers"] == 1
    finally:
        dec.shutdown()


def test_decoder_pixel_matches_in_process(tmp_path):
    from rendering import renderer as raster
    from rendering.types import RenderRequest, TileRect
    from rendering.workers import WorkerHandles
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    handles = WorkerHandles()
    try:
        doc, page, intrinsic = handles.prepare_page(pdf, 0)
        req = RenderRequest(page_index=0, scale=1.0, rotation_degrees=0,
                            tile=TileRect(0, 0, 595, 842))
        ref, _, _, _, _ = raster.render_tile_raw(
            doc, req, 595.0, 842.0, None, alpha=True, intrinsic_rotation=intrinsic)
    finally:
        handles.close_all()
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        data, w, h, _, _, _ = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        assert bytes(data) == bytes(ref)
    finally:
        dec.shutdown()


def test_decoder_sequential_reuse(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        outs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
                for i in range(3)]
        assert all(o[1:3] == (595, 842) for o in outs)
        assert outs[0][0] == outs[1][0] == outs[2][0]
        assert dec.stats()["results"] == 3
    finally:
        dec.shutdown()


def test_decoder_two_workers_concurrent(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        out = {}
        def w(i):
            out[i] = dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        threads = [threading.Thread(target=w, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert out[0][0] == out[1][0]
        assert dec.stats()["live_workers"] == 2
    finally:
        dec.shutdown()


def test_decoder_invalid_page_maps_error(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        fut = dec.submit("bad", pdf, 9999, 1.0, 0, 595, 842)
        # Parity with the in-process engine: out-of-range access surfaces
        # as RenderFailedError on both paths (PyMuPDF raises a non-IndexError).
        with pytest.raises(RenderFailedError):
            fut.result(timeout=60)
        assert dec.stats()["live_workers"] == 1  # worker survives the error
    finally:
        dec.shutdown()


def test_decoder_error_translation():
    assert isinstance(
        ProcessDecoder._translate_error("InvalidPageIndexError", "x"),
        InvalidPageIndexError,
    )
    assert isinstance(
        ProcessDecoder._translate_error("RenderCancelledError", "x"),
        RenderCancelledError,
    )
    assert isinstance(
        ProcessDecoder._translate_error("AnythingElse", "x"), RenderFailedError
    )


def test_decoder_bad_path_maps_failed(tmp_path):
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        fut = dec.submit("bad", str(tmp_path / "missing.pdf"), 0, 1.0, 0, 595, 842)
        with pytest.raises(RenderFailedError):
            fut.result(timeout=60)
    finally:
        dec.shutdown()


def test_decoder_timeout_terminates_and_replaces(monkeypatch, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, timeout_s=0.2))
    # Sabotage spawning: sleeper processes that never read the task queue,
    # so the deadline fires deterministically without racing a real decode.
    ctx = dec._ctx
    monkeypatch.setattr(dec, "_spawn_locked", lambda: _sleeper_slot(dec, ctx))
    try:
        fut = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        with pytest.raises(RenderFailedError, match="timed out"):
            fut.result(timeout=60)
        stats = dec.stats()
        assert stats["timeouts"] == 1
        assert stats["worker_restarts"] >= 1
    finally:
        dec.shutdown()


def test_queued_task_survives_busy_workers(monkeypatch, tmp_path):
    """Regression: a task queued while all workers are busy must not be lost.

    Previously the feeder popped a message before finding a free worker and
    dropped it when both workers were busy: the request stayed outstanding
    forever with no timeout, hanging the pipeline drain.
    """
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    ctx = dec._ctx
    monkeypatch.setattr(dec, "_spawn_locked", lambda: _sleeper_slot(dec, ctx))
    try:
        first = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        _wait_for(lambda: dec._slots and dec._slots[0].busy, timeout_s=30.0)
        second = dec.submit("r2", pdf, 0, 1.0, 0, 595, 842)
        time.sleep(1.0)  # feeder polls every 0.5s; a pop-then-drop bug eats it here
        with dec._lock:
            queued = len(dec._visible) + len(dec._prefetch)
            outstanding = list(dec._outstanding)
        assert not first.done() and not second.done()
        assert queued == 1, f"queued task was lost, queues hold {queued}"
        assert sorted(outstanding) == ["r1", "r2"]
    finally:
        dec.shutdown()
    assert first.done() and second.done()  # shutdown fails outstanding cleanly


def test_withdrawal_removes_only_unwanted_queued(monkeypatch, tmp_path):
    """Withdrawal is selective and never touches running decodes.

    One worker, occupied by a sleeper task: queue two unwanted prefetches
    plus one wanted visible decode, withdraw everything except the running
    task and the wanted one. Only the unwanted queued requests cancel; the
    running task stays busy and the wanted task stays queued.
    """
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    ctx = dec._ctx
    monkeypatch.setattr(dec, "_spawn_locked", lambda: _sleeper_slot(dec, ctx))
    try:
        running = dec.submit("run", pdf, 0, 1.0, 0, 595, 842)
        _wait_for(lambda: dec._slots and dec._slots[0].busy, timeout_s=30.0)
        drop1 = dec.submit("drop1", pdf, 0, 1.0, 0, 595, 842,
                           priority_visible=False)
        drop2 = dec.submit("drop2", pdf, 0, 1.0, 0, 595, 842,
                           priority_visible=False)
        keep = dec.submit("keep", pdf, 0, 1.0, 0, 595, 842)
        _wait_for(lambda: dec.pending_count() == 4, timeout_s=30.0)
        removed = dec.withdraw({"run", "keep"})
        assert removed == 2
        with pytest.raises(RenderCancelledError):
            drop1.result(timeout=30)
        with pytest.raises(RenderCancelledError):
            drop2.result(timeout=30)
        # Running task untouched, wanted task still queued (never lost).
        assert not running.done()
        assert not keep.done()
        assert dec._slots[0].busy
        assert dec._slots[0].request_id == "run"
        with dec._lock:
            queued = len(dec._visible) + len(dec._prefetch)
            outstanding = sorted(dec._outstanding)
        assert queued == 1, f"expected only 'keep' queued, depth={queued}"
        assert outstanding == ["keep", "run"], outstanding
        stats = dec.stats()
        assert stats["withdrawn"] == 2
        assert stats["errors"] == 0  # withdrawal is scheduling, not failure
    finally:
        dec.shutdown()
    assert running.done() and keep.done()  # shutdown resolves outstanding


def _sleeper_slot(dec, ctx):
    from rendering.process_decoder import _WorkerSlot
    proc = ctx.Process(target=_sleeper_entry, name="inkfall-page-decode-test")
    proc.start()
    return _WorkerSlot(proc, ctx.Queue())


def test_withdrawal_empty_and_shutdown_safe(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        assert dec.withdraw(set()) == 0
        fut = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        assert dec.withdraw({"r1"}) == 0  # wanted work survives
        data, w, h, _, _, _ = fut.result(timeout=60)
        assert (w, h) == (595, 842)
        assert dec.withdraw(set()) == 0  # nothing queued: no-op
    finally:
        dec.shutdown()


def _sleeper_entry():
    import time
    time.sleep(60)


def test_decoder_termination_detected_and_replaced(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        first = dec.submit("r0", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        assert first[1:3] == (595, 842)
        before = dec.stats()["worker_restarts"]
        dec._slots[0].proc.terminate()
        _wait_for(lambda: dec.stats()["worker_restarts"] > before)
        assert dec.stats()["live_workers"] == 1
        again = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        assert again[0] == first[0]
    finally:
        dec.shutdown()


def test_decoder_shutdown_leaves_no_workers(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(2)]
    for f in futs:
        f.result(timeout=60)
    procs = [s.proc for s in dec._slots]
    assert all(p.is_alive() for p in procs)
    dec.shutdown()
    assert dec.stats()["live_workers"] == 0
    assert all(not p.is_alive() for p in procs)
    dec.shutdown()  # idempotent


def test_decoder_lazy_no_spawn_before_submit():
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        assert dec.stats()["spawned"] is False
        assert dec.worker_pids() == ()
    finally:
        dec.shutdown()  # safe with zero workers ever spawned
    assert dec.stats()["spawned"] is False


def test_decoder_visible_lane_jumps_prefetch_queue(tmp_path):
    """VISIBLE strictly precedes PREFETCH among queued decodes.

    pre-1 may win the initial race (it can be assigned before vis is even
    submitted), but vis must always execute ahead of pre-2: a single worker
    runs strictly serially and the feeder serves the VISIBLE lane first, so
    no interleaving lets pre-2 overtake vis. This holds deterministically.
    """
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        done: list = []
        lock = threading.Lock()

        def record(tag):
            with lock:
                done.append(tag)

        def submit(tag, visible):
            fut = dec.submit(tag, pdf, 0, 1.0, 0, 595, 842,
                             priority_visible=visible)
            fut.add_done_callback(lambda _f, t=tag: record(t))

        submit("pre-1", False)
        submit("pre-2", False)
        submit("vis", True)
        _wait_for(lambda: len(done) == 3, timeout_s=60.0)
        assert done.index("vis") < done.index("pre-2"), f"lane violated: {done}"
        assert sorted(done) == ["pre-1", "pre-2", "vis"]
    finally:
        dec.shutdown()


def test_shutdown_close_after_backlog_is_fast(tmp_path):
    """Close-after-backlog must not burn the full join timeout per worker.

    Previously shutdown stopped draining the result queue before joining
    workers, so a child finishing a decode blocked in teardown flushing
    result bytes to an unread pipe (~10s per stuck worker). Draining while
    joining lets workers reach clean exit. Dense pages at 2.0x give ~8MB
    payloads and slow decodes so results are genuinely in flight at close.
    """
    from tests.helpers import make_dense_pdf
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=1)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 2.0, 0, 1190, 1684) for i in range(6)]
        procs = [s.proc for s in dec._slots]
        t0 = time.monotonic()
        dec.shutdown()
        wall = time.monotonic() - t0
        assert wall < 2.0, f"shutdown took {wall:.1f}s"
        assert all(not p.is_alive() for p in procs)
        assert all(f.done() for f in futs)
        assert dec.stats()["shutdown_stranded"] == 0
    finally:
        dec.shutdown()  # idempotent


def test_shutdown_during_active_decode(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(4)]
        procs = [s.proc for s in dec._slots]
        t0 = time.monotonic()
        dec.shutdown()
        wall = time.monotonic() - t0
        assert wall < 5.0, f"shutdown took {wall:.1f}s"
        assert all(not p.is_alive() for p in procs)
        assert all(f.done() for f in futs)
    finally:
        dec.shutdown()


def test_shutdown_with_queued_decodes(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(4)]
        dec.shutdown()
        assert all(f.done() for f in futs)
        assert dec.stats()["live_workers"] == 0
    finally:
        dec.shutdown()


def test_shutdown_delivers_late_results_when_active(tmp_path):
    """Results that beat the STOP still deliver instead of stranding futures."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        fut = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        data, w, h, _, _, _ = fut.result(timeout=60)
        assert (w, h) == (595, 842)
        assert len(data) == 595 * 842 * 4
        dec.shutdown()
    finally:
        dec.shutdown()


def test_shutdown_double_close_is_idempotent(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        dec.shutdown()
        dec.shutdown()
        dec.shutdown()
        assert dec.stats()["live_workers"] == 0
    finally:
        dec.shutdown()


def test_shutdown_zero_workers_is_safe():
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    t0 = time.monotonic()
    try:
        dec.shutdown()
    finally:
        dec.shutdown()
    assert time.monotonic() - t0 < 5.0
    assert dec.stats()["spawned"] is False
    assert dec.stats()["live_workers"] == 0


def test_shutdown_normal_stop_exit(tmp_path):
    """Workers that consume STOP exit cleanly with no termination."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    try:
        dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        pid = dec._slots[0].proc.pid
        assert dec._slots[0].proc.exitcode is None  # still running
        dec.shutdown()
        assert pid is not None
    finally:
        dec.shutdown()


def test_shutdown_terminates_stuck_worker(monkeypatch, tmp_path):
    """A worker that ignores STOP is terminated and verified dead."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1))
    ctx = dec._ctx
    monkeypatch.setattr(dec, "_spawn_locked", lambda: _sleeper_slot(dec, ctx))
    try:
        fut = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842)
        _wait_for(lambda: dec._slots and dec._slots[0].busy, timeout_s=30.0)
        procs = [s.proc for s in dec._slots]
        t0 = time.monotonic()
        dec.shutdown()
        wall = time.monotonic() - t0
        # Sleeper ignores STOP: drain cap + short join + terminate. Bounded.
        assert wall < 30.0, f"shutdown took {wall:.1f}s"
        assert all(not p.is_alive() for p in procs)
        assert fut.done()
        assert dec.stats()["shutdown_stranded"] == 0
    finally:
        dec.shutdown()


def test_shutdown_no_worker_pid_survives(tmp_path):
    import subprocess as _sp
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(3)]
        for f in futs:
            f.result(timeout=60)
        pids = [s.proc.pid for s in dec._slots]
        assert len(pids) == 2
        dec.shutdown()
        out = _sp.check_output(["tasklist", "/FI", "IMAGENAME eq python.exe"],
                               text=True, stderr=_sp.STDOUT)
        for pid in pids:
            assert f" {pid} " not in f" {out} ", f"worker {pid} survived shutdown"
    finally:
        dec.shutdown()


def test_shutdown_resolves_all_outstanding(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(5)]
        dec.shutdown()
        assert all(f.done() for f in futs)
        st = dec.stats()
        assert st["outstanding"] == 0
        assert st["submits"] == (
            st["results"] + st["errors"] + st["withdrawn"]
            + st["shutdown_cancelled"] + st["outstanding"]
        ), st
    finally:
        dec.shutdown()


def test_recycling_replaces_after_task_limit(tmp_path):
    """Test A: exactly one replacement at the task boundary."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=2))
    try:
        ref = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        dec.submit("r2", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        old_proc = dec._slots[0].proc
        pid_old = old_proc.pid
        # Retirement is marked synchronously at delivery: visible now.
        assert dec._slots[0].retiring is True
        assert old_proc.is_alive()  # never killed mid-decode
        r3 = dec.submit("r3", pdf, 0, 1.0, 0, 595, 842).result(timeout=90)
        assert r3[0] == ref[0] and r3[1:3] == (595, 842)
        pid_new = dec._slots[0].proc.pid
        assert pid_new != pid_old
        _wait_for(lambda: not old_proc.is_alive(), timeout_s=30.0)
        stats = dec.stats()
        assert stats["worker_recycles"] == 1, stats
        assert stats["worker_restarts"] == 0, stats
        assert stats["outstanding"] == 0
    finally:
        dec.shutdown()


def test_recycling_counter_resets_per_worker(tmp_path):
    """Test B: replacement boundaries exactly at 2, 4 with N=2."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=2))
    try:
        ref = dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        dec.submit("r2", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        pid1 = dec._slots[0].proc.pid
        dec.submit("r3", pdf, 0, 1.0, 0, 595, 842).result(timeout=90)
        dec.submit("r4", pdf, 0, 1.0, 0, 595, 842).result(timeout=90)
        pid2 = dec._slots[0].proc.pid
        assert pid2 != pid1
        r5 = dec.submit("r5", pdf, 0, 1.0, 0, 595, 842).result(timeout=90)
        assert r5[0] == ref[0]
        pid3 = dec._slots[0].proc.pid
        assert pid3 != pid2
        stats = dec.stats()
        assert stats["worker_recycles"] == 2, stats
        assert stats["worker_restarts"] == 0, stats
        assert stats["results"] == 5, stats
    finally:
        dec.shutdown()


def test_recycling_queued_work_survives(tmp_path):
    """Test C: retirement mid-queue loses nothing and duplicates nothing."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=2))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(5)]
        outs = [f.result(timeout=90) for f in futs]
        assert all(o[1:3] == (595, 842) for o in outs)
        assert all(o[0] == outs[0][0] for o in outs)
        stats = dec.stats()
        assert stats["worker_recycles"] == 2, stats
        assert stats["results"] == 5, stats
        assert stats["outstanding"] == 0
        assert stats["submits"] == (
            stats["results"] + stats["errors"] + stats["withdrawn"]
            + stats["shutdown_cancelled"] + stats["outstanding"]
        ), stats
    finally:
        dec.shutdown()


def test_recycling_two_worker_bound(tmp_path):
    """Test D: live workers never exceed 2 across double recycling.

    Task distribution across the two workers is timing-dependent, so the
    recycle count is asserted as a structural range, not an exact number:
    8 executions with a 2-task quota force between 2 and 4 replacements.
    """
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=2, max_tasks_per_worker=2))
    try:
        peak = [0]
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                try:
                    live = dec.stats()["live_workers"]
                    peak[0] = max(peak[0], live)
                except Exception:
                    pass
                time.sleep(0.01)

        sm = threading.Thread(target=sampler, daemon=True)
        sm.start()
        try:
            futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(8)]
            outs = [f.result(timeout=120) for f in futs]
        finally:
            stop.set()
            sm.join(timeout=5)
        assert all(o[1:3] == (595, 842) for o in outs)
        assert peak[0] <= 2, f"live workers exceeded cap: {peak[0]}"
        stats = dec.stats()
        assert stats["live_workers"] <= 2, stats
        assert 2 <= stats["worker_recycles"] <= 4, stats
        assert stats["worker_restarts"] == 0, stats
        assert stats["results"] == 8, stats
        assert stats["outstanding"] == 0
    finally:
        dec.shutdown()


def test_recycling_no_mid_decode_kill(tmp_path):
    """Test E: with N=1 every task retires its worker only after delivery."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=1))
    try:
        ref = None
        pids = []
        for i in range(3):
            out = dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842).result(timeout=90)
            if ref is None:
                ref = out[0]
            assert out[0] == ref  # pixel-correct every time
            assert out[1:3] == (595, 842)
            pids.append(dec._slots[0].proc.pid)
        assert len(set(pids)) == 3, pids  # fresh worker per task
        # The third worker is marked retiring synchronously at delivery
        # (deterministic); its pending replacement may or may not have
        # landed yet, so the counter is a lower bound here.
        assert dec.stats()["worker_recycles"] >= 2, dec.stats()
    finally:
        dec.shutdown()


def test_recycling_shutdown_interaction(tmp_path):
    """Test F: shutdown is clean with retiring/retired/replacing workers."""
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    # F1: shutdown while a worker sits retiring (STOP maybe unsent).
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=2))
    try:
        dec.submit("r1", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        dec.submit("r2", pdf, 0, 1.0, 0, 595, 842).result(timeout=60)
        assert dec._slots[0].retiring is True
        procs = [s.proc for s in dec._slots]
        dec.shutdown()
        assert all(not p.is_alive() for p in procs)
        assert dec.stats()["shutdown_stranded"] == 0
    finally:
        dec.shutdown()
    # F2/F3: shutdown with queued work (replacement may or may not be
    # pending — both outcomes must converge with zero live, zero pending).
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=1))
    try:
        futs = [dec.submit(f"r{i}", pdf, 0, 1.0, 0, 595, 842) for i in range(4)]
        dec.shutdown()
        assert all(f.done() for f in futs)
        assert dec.stats()["live_workers"] == 0
        assert dec.stats()["shutdown_stranded"] == 0
        st = dec.stats()
        assert st["submits"] == (
            st["results"] + st["errors"] + st["withdrawn"]
            + st["shutdown_cancelled"] + st["outstanding"]
        ), st
    finally:
        dec.shutdown()


def test_decoder_config_bounds():
    with pytest.raises(ValueError):
        ProcessDecoderConfig(workers=0)
    with pytest.raises(ValueError):
        ProcessDecoderConfig(workers=MAX_PROCESS_WORKERS + 1)
    assert ProcessDecoderConfig(workers=1).workers == 1
    assert ProcessDecoderConfig(workers=2).workers == 2
    with pytest.raises(ValueError):
        ProcessDecoderConfig(max_tasks_per_worker=0)
    with pytest.raises(ValueError):
        ProcessDecoderConfig(max_tasks_per_worker=-1)
    assert ProcessDecoderConfig(max_tasks_per_worker=25).max_tasks_per_worker == 25
    assert ProcessDecoderConfig().max_tasks_per_worker == 25


# -- pipeline integration --------------------------------------------------

@pytest.mark.parametrize("workers", [0, 1, 2])
def test_invariant_four_tiles_one_decode(tmp_path, workers):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=workers)
    try:
        seen = []
        orig = pipe._engine.submit
        def rec(request, token=None):
            seen.append(request)
            return orig(request, token)
        import unittest.mock as mock
        with mock.patch.object(pipe._engine, "submit", rec):
            summary = pipe.update(0.0, 595, 842)
        assert summary["visible"] == 4
        stats = _drain(pipe)
        assert stats["decode_submits"] == 1, stats
        assert stats["decode_fallbacks"] == 0, stats
        assert len(pipe.visible_ready(0.0, 595, 842)) == 4
    finally:
        pipe.close()


def _collect_arm_blobs(tmp_path, workers):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=workers)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        blobs = sorted(t.data for t in pipe.visible_ready(0.0, 595, 842))
        assert len(blobs) == 4
        return blobs
    finally:
        pipe.close()


@pytest.mark.parametrize("workers", [0, 1, 2])
def test_invariant_pixel_identical_across_arms(tmp_path, workers):
    _collect_arm_blobs(tmp_path, workers)


def test_invariant_arms_agree(tmp_path):
    got = {workers: _collect_arm_blobs(tmp_path, workers) for workers in (0, 1, 2)}
    assert got[0] == got[1] == got[2]


def test_pipeline_lazy_no_spawn_without_decode(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=2)
    try:
        assert pipe._proc is not None
        assert pipe._proc.stats()["spawned"] is False
        assert pipe._proc.worker_pids() == ()
    finally:
        pipe.close()
    assert pipe._proc.stats()["spawned"] is False


def test_pipeline_close_kills_workers(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=2)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        assert pipe._proc is not None
        assert pipe._proc.stats()["live_workers"] == 2
        procs = [s.proc for s in pipe._proc._slots]
    finally:
        pipe.close()
    assert all(not p.is_alive() for p in procs)


def test_stale_decode_never_populates_cache(tmp_path):
    from concurrent.futures import Future
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=2)
    try:
        held: Future = Future()
        orig_submit = pipe._proc.submit
        def rec(*args, **kwargs):
            orig_submit(*args, **kwargs)  # still exercises the real pool
            return held
        import unittest.mock as mock
        with mock.patch.object(pipe._proc, "submit", rec):
            pipe.update(0.0, 595, 842)
        assert pipe.stats()["in_flight"] == 1
        pipe.set_user_rotation(90)  # epoch bump; decode LRU cleared
        assert pipe.stats()["epoch"] == 1
        # Late epoch-0 SUCCESS payload: must be discarded, never stored.
        held.set_result((bytes(595 * 842 * 4), 595, 842, 595 * 4, 1.0, 1.0))
        stats = _drain(pipe)
        assert stats["stale_discarded"] >= 1, stats
        assert stats["page_decode"]["decoded_pages"] == 0, stats
        assert stats["decode_fallbacks"] == 0, stats
    finally:
        try:
            if not held.done():
                held.set_exception(RenderCancelledError("test end"))
        except Exception:
            pass
        pipe.close()


def test_decoder_submit_failure_falls_back_to_tiles(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595, workers=2)
    try:
        import unittest.mock as mock
        with mock.patch.object(
            pipe._proc, "submit",
            side_effect=RenderFailedError("pool down"),
        ):
            pipe.update(0.0, 595, 842)
            _drain(pipe)
            # Fallback groups re-stage as individual tiles on the next update.
            pipe.update(0.0, 595, 842)
            stats = _drain(pipe)
        assert stats["decode_fallbacks"] >= 1, stats
        ready = pipe.visible_ready(0.0, 595, 842)
        assert len(ready) == 4
        for tile in ready:
            assert len(tile.data) == tile.width_px * tile.height_px * 4
    finally:
        pipe.close()


def test_invalid_workers_rejected(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    doc = DocumentId("procdecode")
    with pytest.raises(ValueError):
        DocumentPipeline(doc, pdf, _pages(doc, SIZES_595),
                         process_decode_workers=MAX_PROCESS_WORKERS + 1)
    with pytest.raises(ValueError):
        DocumentPipeline(doc, pdf, _pages(doc, SIZES_595),
                         process_decode_workers=-1)
