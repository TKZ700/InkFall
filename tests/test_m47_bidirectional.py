"""M4.7 bidirectional scroll regression tests.

Deterministic by construction: direction/planning/order/gate behavior is
asserted on synchronous state (no timing sleeps for correctness);
background quiescence uses the same generous-deadline drain convention as
test_pipeline.py (a timeout means a real hang, never a flaky threshold).
"""

from __future__ import annotations

import heapq
import threading
import time
from concurrent.futures import Future

import pytest

from core.types import DocumentId
from document.models import page_id_for
from rendering.cache import cache_identity
from rendering.engine import RendererEngine
from rendering.metrics import RenderMetrics
from rendering.page_decode import decode_key
from rendering.pipeline import DocumentPipeline, _group_key_of
from rendering.scheduler import EngineConfig, Scheduler
from rendering.types import RenderPriority, RenderRequest, RenderResult, TileKey, TileRect
from tests.helpers import make_probe_pdf, make_rich_pdf
from tests.test_pipeline import _drain, _pages


def _pipe(pdf: str, sizes, **kw) -> DocumentPipeline:
    doc = DocumentId("pipe47")
    defaults: dict = dict(engine_config=EngineConfig(worker_count=2),
                          process_decode_workers=0)
    defaults.update(kw)
    return DocumentPipeline(doc, pdf, _pages(doc, sizes), **defaults)


SIZES_6 = [(600.0, 800.0, 0)] * 6


def _sched() -> Scheduler:
    return Scheduler(EngineConfig(worker_count=1, max_pending=64), RenderMetrics())


def _sreq(pdf: str, page: int, priority=RenderPriority.VISIBLE, **kw) -> RenderRequest:
    from core.types import PageId

    args = dict(document_id=DocumentId("s"), page_id=PageId(f"s:p{page}"),
                page_index=page, tile=TileRect(0, 0, 256, 256),
                source_path=pdf, priority=priority)
    args.update(kw)
    return RenderRequest(**args)


def _heap_order(sched: Scheduler) -> list:
    heap = list(sched._heap)
    out = []
    while heap:
        _rank, _seq, entry = heapq.heappop(heap)
        out.append(entry.request.key())
    return out


# -- A. direction ---------------------------------------------------------------

def test_direction_tracked_from_viewport_movement(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "d.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        pipe.update(0.0, 800, 600)
        assert pipe.stats()["scroll_direction"] == 0
        pipe.update(0.0, 800, 600)  # identical poll: sticky
        assert pipe.stats()["scroll_direction"] == 0
        pipe.update(400.0, 800, 600)
        assert pipe.stats()["scroll_direction"] == 1
        pipe.update(402.0, 800, 600)  # tiny same-direction tick: stays
        assert pipe.stats()["scroll_direction"] == 1
        assert pipe.stats()["direction_flips"] == 0
        pipe.update(100.0, 800, 600)  # reversal, even a small one
        assert pipe.stats()["scroll_direction"] == -1
        assert pipe.stats()["direction_flips"] == 1
        pipe.update(700.0, 800, 600)
        assert pipe.stats()["direction_flips"] == 2
    finally:
        pipe.close()


def test_plan_order_follows_direction(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        with pipe._lock:
            down, _ = pipe._plan_locked(50.0, 800, 600, 0, 1)
            up, _ = pipe._plan_locked(50.0, 800, 600, 0, -1)
            flat, _ = pipe._plan_locked(50.0, 800, 600, 0, 0)
        pages_down = [r.page_index for r in down]
        pages_up = [r.page_index for r in up]
        # H2 leading-first: DOWN leads with high pages (descending),
        # UP leads with low pages (ascending). Same page set either way.
        assert pages_down == sorted(pages_down, reverse=True)
        assert pages_up == sorted(pages_up)
        assert {r.page_index for r in down} == {r.page_index for r in flat}
    finally:
        pipe.close()


def test_prefetch_truncation_keeps_leading_head(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "h.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        with pipe._lock:
            _, pre_down = pipe._plan_locked(50.0, 300, 300, 0, 1)
            _, pre_up = pipe._plan_locked(50.0, 300, 300, 0, -1)
        assert pre_down and pre_up
        # H2 leading-first: UP prefetch head is the low (leading) side,
        # DOWN head is the high (leading) side.
        assert pre_up[0].page_index <= pre_down[0].page_index
    finally:
        pipe.close()


# -- B. scheduler promote-only ----------------------------------------------------

def test_dedup_promotes_prefetch_to_visible(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "q.pdf"))
    sched = _sched()
    stale = _sreq(pdf, 0, RenderPriority.VISIBLE)
    sched.submit(stale)
    pre = _sreq(pdf, 1, RenderPriority.PREFETCH)
    sched.submit(pre)
    vis = _sreq(pdf, 1, RenderPriority.VISIBLE)
    _f, _t, dedup = sched.submit(vis)
    assert dedup is True
    order = _heap_order(sched)
    # Promoted prefetch now leads; stale visible keeps its place behind it.
    assert order[0] == pre.key()
    assert order.index(stale.key()) == 1
    assert sched.pending_count() == 2  # same size, only order changed


def test_reprioritize_promote_only(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "r.pdf"))
    sched = _sched()
    stale_vis = _sreq(pdf, 0, RenderPriority.VISIBLE)
    sched.submit(stale_vis)
    stale_pre = _sreq(pdf, 1, RenderPriority.PREFETCH)
    sched.submit(stale_pre)
    fresh_pre = _sreq(pdf, 2, RenderPriority.PREFETCH)
    sched.submit(fresh_pre)
    n = sched.reprioritize({fresh_pre.key()})
    assert n == 1
    order = _heap_order(sched)
    assert order[0] == fresh_pre.key()  # promoted to the very front
    # Stale work untouched: same relative order, same ranks.
    ranks = {entry.request.key(): rank
             for rank, _s, entry in sched._heap}
    assert ranks[stale_vis.key()] == 0
    assert ranks[stale_pre.key()] == 1
    assert order.index(stale_vis.key()) < order.index(stale_pre.key())
    assert sched.pending_count() == 3


def test_reprioritize_noop_and_shutdown(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "n.pdf"))
    sched = _sched()
    assert sched.reprioritize(set()) == 0
    assert sched.reprioritize({_sreq(pdf, 9).key()}) == 0
    sched.shutdown()
    assert sched.reprioritize({_sreq(pdf, 9).key()}) == 0


def test_pipeline_flip_calls_reprioritize_with_visible_keys(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "f.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    calls: list = []
    orig = pipe._engine.reprioritize
    pipe._engine.reprioritize = lambda keys: calls.append(set(keys)) or 0
    try:
        pipe.update(0.0, 800, 600)
        assert calls == []  # no direction yet: no call
        pipe.update(900.0, 800, 600)
        assert calls == []  # direction established, not flipped
        pipe.update(100.0, 800, 600)
        assert len(calls) == 1  # flip -> exactly one call
        with pipe._lock:
            vis, _ = pipe._plan_locked(100.0, 800, 600, 0, -1)
        assert calls[0] == {r.key() for r in vis}
    finally:
        pipe._engine.reprioritize = orig
        pipe.close()


# -- C. completion admission gate (current-relevance) ------------------------------

def _full_result(req: RenderRequest, w: int, h: int, byte: int) -> RenderResult:
    return RenderResult(request=req, data=bytes([byte]) * (w * h * 4),
                        width_px=w, height_px=h, stride=w * 4, generation=0)


def _tile_req(pipe: DocumentPipeline, page_index: int, tile: TileRect) -> RenderRequest:
    entry = pipe._layout.entry(page_index)
    _ = entry
    return RenderRequest(
        document_id=pipe._document_id,
        page_id=page_id_for(pipe._document_id, page_index),
        page_index=page_index,
        tile=tile,
        scale=float(pipe._current_bucket),
        rotation_degrees=int(pipe._user_rotation),
        generation=0,
        priority=RenderPriority.VISIBLE,
        source_path=pipe._source_path,
    )


def test_gate_discards_stale_tile_completion(tmp_path):
    """A->B: A completion while B is wanted is discarded, caches clean."""
    pdf = make_probe_pdf(str(tmp_path / "g1.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        pipe.update(0.0, 800, 600)  # A (top)
        _drain(pipe)
        pipe.update(5000.0, 800, 600)  # direction +1
        pipe.update(100.0, 800, 600)  # flip to -1
        pipe.update(5000.0, 800, 600)  # flip to +1, snapshot is B
        assert pipe.stats()["direction_flips"] == 2
        # Off-grid tile ident: real planning never produces it, so no
        # background racer can insert it — the assertion is deterministic.
        stale = _tile_req(pipe, 0, TileRect(7, 13, 64, 64))
        ident = cache_identity(TileKey.from_request(stale))
        assert ident not in pipe._last_wanted_idents
        fut: Future = Future()
        fut.set_result(_full_result(stale, 64, 64, 0xAB))
        before = pipe.stats()["superseded_discarded"]
        pipe._on_complete(fut, ident, 0)
        assert pipe.stats()["superseded_discarded"] == before + 1
        assert pipe._cache.get(TileKey.from_request(stale)) is None
    finally:
        pipe.close()


def test_gate_admits_deferred_prefetch_completion(tmp_path):
    """Control: a wanted-but-never-submitted ident admits with marker bytes."""
    pdf = make_probe_pdf(str(tmp_path / "g2.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        out = pipe.update(0.0, 800, 600)
        assert out["visible"] > 0
        wanted = set(pipe._last_wanted_idents)
        in_flight = set(pipe._in_flight.keys())
        cands = sorted(wanted - in_flight)
        assert cands, "need a wanted ident with no racer"
        ident = cands[0]
        # ident = (doc, page_value, scale, rotation, (x, y, w, h)).
        page_index = next(
            i for i in range(6)
            if page_id_for(pipe._document_id, i).value == ident[1])
        x, y, w, h = ident[4]
        req = _tile_req(pipe, page_index, TileRect(x, y, w, h))
        assert cache_identity(TileKey.from_request(req)) == ident
        fut = Future()
        fut.set_result(_full_result(req, w, h, 0xCD))
        pipe._on_complete(fut, ident, 0)
        got = pipe._cache.get(TileKey.from_request(req))
        assert got is not None and got.data == bytes([0xCD]) * len(got.data)
    finally:
        pipe.close()


def _fabricated_cover(pipe: DocumentPipeline, page_index: int):
    entry = pipe._layout.entry(page_index)
    fw, fh = int(entry.render_width_px), int(entry.render_height_px)
    scale = float(pipe._current_bucket)
    rot = int(pipe._user_rotation)
    dreq = RenderRequest(
        document_id=pipe._document_id,
        page_id=page_id_for(pipe._document_id, page_index),
        page_index=page_index,
        tile=TileRect(0, 0, fw, fh),
        scale=scale, rotation_degrees=rot, generation=0,
        priority=RenderPriority.VISIBLE, source_path=pipe._source_path)
    treq = _tile_req(pipe, page_index, TileRect(0, 0, min(64, fw), min(64, fh)))
    group = _group_key_of(dreq)
    did = cache_identity(TileKey.from_request(dreq))
    cover = {"group": group, "request": dreq, "reqs": [treq]}
    return did, cover, dreq, treq, fw, fh


def test_gate_decode_discard_then_readmit(tmp_path):
    """A->B discards A's decode; A->B->A re-admits it (current relevance)."""
    pdf = make_probe_pdf(str(tmp_path / "g3.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        # --- stale case: snapshot = {B group}, with a flip on record so
        # the fabricated pre-flip completion is gate-eligible. No real
        # updates run here, so no background racer touches the cover.
        with pipe._lock:
            pipe.direction_flips = 1
            pipe._last_wanted_groups = frozenset({(5, 1.0, 0, 0)})
            pipe._last_wanted_ids = frozenset({"rid-B"})
            pipe._last_wanted_idents = frozenset({("x",)})
        did, cover, dreq, treq, fw, fh = _fabricated_cover(pipe, 0)
        with pipe._lock:
            pipe._decode_cover[did] = cover
        fut = Future()
        fut.set_result(_full_result(dreq, fw, fh, 0xAB))
        before = pipe.stats()["superseded_discarded"]
        failed_before = len(pipe._decode_failed)
        pipe._on_complete(fut, did, 0)
        assert pipe.stats()["superseded_discarded"] == before + 1
        assert len(pipe._decode_failed) == failed_before  # never marked failed
        assert pipe._decodes.get(
            decode_key(pipe._document_id.value,
                       page_id_for(pipe._document_id, 0).value,
                       float(pipe._current_bucket), 0), 0) is None
        assert pipe._cache.get(TileKey.from_request(treq)) is None
        # --- readmit case: snapshot = {A group} again ---
        with pipe._lock:
            pipe._last_wanted_groups = frozenset({cover["group"]})
            pipe._last_wanted_ids = frozenset({dreq.request_id})
            pipe._last_wanted_idents = frozenset({
                cache_identity(TileKey.from_request(treq)), did})
            pipe._decode_cover[did] = dict(cover)
        fut2 = Future()
        fut2.set_result(_full_result(dreq, fw, fh, 0xAB))
        pipe._on_complete(fut2, did, 0)
        assert pipe.stats()["superseded_discarded"] == before + 1  # no new discard
        got = pipe._cache.get(TileKey.from_request(treq))
        assert got is not None and set(got.data) == {0xAB}
    finally:
        pipe.close()


def test_gate_discards_same_generation_unwanted(tmp_path):
    """Same epoch/generation: relevance — not generation — decides."""
    pdf = make_probe_pdf(str(tmp_path / "g4.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        with pipe._lock:
            pipe.direction_flips = 1  # a flip is on record: gate is armed
            pipe._last_wanted_groups = frozenset({(5, 1.0, 0, 0)})
            pipe._last_wanted_ids = frozenset()
            pipe._last_wanted_idents = frozenset()
        did, cover, dreq, _treq, fw, fh = _fabricated_cover(pipe, 0)
        with pipe._lock:
            pipe._decode_cover[did] = cover
        fut = Future()
        fut.set_result(_full_result(dreq, fw, fh, 0xAB))
        before = pipe.stats()["superseded_discarded"]
        pipe._on_complete(fut, did, 0)  # epoch 0 == current epoch 0
        assert pipe.stats()["superseded_discarded"] == before + 1
    finally:
        pipe.close()


# -- oscillation / bounds / shutdown -------------------------------------------------

def test_oscillation_stays_bounded(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "o.pdf"), pages=12)
    from document.manager import DocumentManager

    mgr = DocumentManager()
    doc = mgr.open(pdf)
    from document.models import PageInfo
    pages = [doc.page_info(i) for i in range(doc.page_count)]
    pipe = DocumentPipeline(doc.id, pdf, pages,
                            engine_config=EngineConfig(worker_count=2),
                            process_decode_workers=0)
    try:
        y0, y1 = 0.0, 4000.0
        pipe.update(y0, 800, 600)
        _drain(pipe)
        first_sub = pipe.stats()["submitted_visible"]
        for _ in range(3):
            pipe.update(y1, 800, 600)
            _drain(pipe)
            pipe.update(y0, 800, 600)
            _drain(pipe)
        st = _drain(pipe)
        assert st["submit_errors"] == 0
        assert st["in_flight"] == 0
        # Revisits are cache-warm: later rounds submit far less than cold load.
        assert pipe.stats()["submitted_visible"] < first_sub * 6
        assert pipe.stats()["direction_flips"] >= 5
    finally:
        pipe.close()


def test_bounded_queues_preserved(tmp_path):
    from rendering.errors import QueueFullError

    pdf = make_probe_pdf(str(tmp_path / "b.pdf"))
    sched = Scheduler(EngineConfig(worker_count=1, max_pending=4), RenderMetrics())
    for i in range(4):
        sched.submit(_sreq(pdf, i, RenderPriority.VISIBLE))
    with pytest.raises(QueueFullError):
        sched.submit(_sreq(pdf, 9, RenderPriority.VISIBLE))
    before = sched.pending_count()
    assert sched.reprioritize({_sreq(pdf, 2).key()}) == 1
    assert sched.pending_count() == before  # order only, never capacity
    with pytest.raises(QueueFullError):
        sched.submit(_sreq(pdf, 10, RenderPriority.VISIBLE))


def test_shutdown_clean_after_reversal(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "s.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    before = {t.name for t in threading.enumerate() if t.name.startswith("inkfall-")}
    pipe.update(0.0, 800, 600)
    pipe.update(3000.0, 800, 600)
    pipe.update(100.0, 800, 600)
    assert pipe.stats()["direction_flips"] >= 1
    pipe.close()
    deadline = time.monotonic() + 30.0
    while True:
        live = {t.name for t in threading.enumerate()
                if t.name.startswith("inkfall-") and t.is_alive()}
        if not live - before:
            break
        assert time.monotonic() < deadline, f"worker leak: {live}"
        time.sleep(0.01)
    assert pipe.stats()["engine"]["pending"] == 0


# -- H1: withdrawn is not failed ---------------------------------------------------

def test_withdrawn_decode_stays_retryable(tmp_path):
    """H1 prime: a withdrawal cancellation must not poison the group.

    Fabricated withdrawal completion (RenderCancelledError, the exact
    error withdraw produces) leaves _decode_failed untouched and counts
    decode_superseded; a later revisit admits the page decode normally.
    """
    from rendering.errors import RenderCancelledError

    pdf = make_probe_pdf(str(tmp_path / "h1.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        with pipe._lock:
            pipe.direction_flips = 1
            pipe._last_wanted_groups = frozenset({(5, 1.0, 0, 0)})
            pipe._last_wanted_ids = frozenset()
            pipe._last_wanted_idents = frozenset()
        did, cover, dreq, treq, fw, fh = _fabricated_cover(pipe, 0)
        with pipe._lock:
            pipe._decode_cover[did] = cover
        fut = Future()
        fut.set_exception(RenderCancelledError("decode superseded"))
        failed_before = len(pipe._decode_failed)
        sup_before = pipe.stats()["decode_superseded"]
        pipe._on_complete(fut, did, 0)
        assert len(pipe._decode_failed) == failed_before
        assert pipe.stats()["decode_superseded"] == sup_before + 1
        assert pipe.stats()["decode_fallbacks"] == 0
        # Revisit: same group wanted again admits a fresh decode.
        with pipe._lock:
            pipe._last_wanted_groups = frozenset({cover["group"]})
            pipe._last_wanted_ids = frozenset({dreq.request_id})
            pipe._last_wanted_idents = frozenset({
                cache_identity(TileKey.from_request(treq)), did})
            pipe._decode_cover[did] = dict(cover)
        fut2 = Future()
        fut2.set_result(_full_result(dreq, fw, fh, 0xAB))
        pipe._on_complete(fut2, did, 0)
        got = pipe._cache.get(TileKey.from_request(treq))
        assert got is not None and set(got.data) == {0xAB}
    finally:
        pipe.close()


def test_genuine_decode_failure_still_poisons(tmp_path):
    """H1 guard: real render failures must still enter _decode_failed."""
    from rendering.errors import RenderFailedError

    pdf = make_probe_pdf(str(tmp_path / "h1b.pdf"))
    pipe = _pipe(pdf, SIZES_6)
    try:
        did, cover, dreq, _treq, _fw, _fh = _fabricated_cover(pipe, 0)
        with pipe._lock:
            pipe._decode_cover[did] = cover
        fut = Future()
        fut.set_exception(RenderFailedError("child render failed"))
        pipe._on_complete(fut, did, 0)
        assert cover["group"] in pipe._decode_failed
        assert pipe.stats()["decode_fallbacks"] >= 1
    finally:
        pipe.close()
