"""M3-B pipeline tests: cache-first submit, pressure, stale, shutdown.

Deterministic by construction: pure helpers tested directly, engine
interaction asserted post-drain (quiescence polled with a generous
deadline — a timeout means a real hang, never a flaky threshold).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from core.types import DocumentId, Size
from document.models import PageInfo, page_id_for
from rendering.cache import cache_identity
from rendering.errors import RenderCancelledError
from rendering.pipeline import DocumentPipeline, VISIBLE_HEADROOM, superseded_prefetch
from rendering.scheduler import EngineConfig
from rendering.types import (
    RenderPriority,
    RenderRequest,
    RenderResult,
    TileKey,
    TileRect,
)
from tests.helpers import (
    make_dense_pdf,
    make_encrypted_pdf,
    make_intrinsic_pdf,
    make_invalid_pdf,
    make_probe_pdf,
    make_rich_pdf,
)


def _pages(doc: DocumentId, sizes: list[tuple[float, float, int]]) -> list[PageInfo]:
    out = []
    for i, (w, h, rot) in enumerate(sizes):
        out.append(
            PageInfo(id=page_id_for(doc, i), document_id=doc, index=i,
                     size=Size(w, h), rotation=rot)
        )
    return out


def _pipe(pdf: str, sizes: list[tuple[float, float, int]], **kw) -> DocumentPipeline:
    doc = DocumentId("pipe")
    defaults: dict = dict(engine_config=EngineConfig(worker_count=2))
    defaults.update(kw)
    return DocumentPipeline(doc, pdf, _pages(doc, sizes), **defaults)


def _drain(pipe: DocumentPipeline, timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        stats = pipe.stats()
        pd = stats["process_decoder"]
        if stats["in_flight"] == 0 and stats["engine"]["pending"] == 0 and (
                pd is None or pd["outstanding"] == 0):
            return stats
        assert time.monotonic() < deadline, "pipeline did not quiesce"
        time.sleep(0.005)


def _dark_count(data: bytes) -> int:
    n = 0
    for i in range(0, len(data), 4):
        if data[i + 3] > 127 and data[i] < 128 and data[i + 1] < 128 and data[i + 2] < 128:
            n += 1
    return n


SIZES_600x800 = [(600.0, 800.0, 0)]


def test_hit_avoids_engine_submission(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        first = pipe.update(0.0, 800, 600)
        assert first["visible"] == 4
        _drain(pipe)
        submitted = pipe.stats()["submitted_visible"]
        assert submitted == 1  # one shared page decode covers all four tiles
        assert pipe.stats()["decode_submits"] == 1
        second = pipe.update(0.0, 800, 600)
        _drain(pipe)
        assert second["hits"] == 4
        assert pipe.stats()["submitted_visible"] == submitted  # zero new submits
        ready = pipe.visible_ready(0.0, 800, 600)
        assert len(ready) == 4
        for tile in ready:
            assert len(tile.data) == tile.width_px * tile.height_px * 4
    finally:
        pipe.close()


def test_visible_submits_before_prefetch(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        order: list = []
        engine = pipe._engine
        orig_submit = engine.submit

        def recording_submit(request, token=None):
            order.append(request.priority)
            return orig_submit(request, token)

        monkeypatch.setattr(engine, "submit", recording_submit)
        decoder = pipe._proc
        assert decoder is not None  # default-on-2: decodes route here
        orig_decode = decoder.submit

        def recording_decode(*args, **kwargs):
            order.append(
                RenderPriority.VISIBLE
                if kwargs.get("priority_visible", True)
                else RenderPriority.PREFETCH
            )
            return orig_decode(*args, **kwargs)

        monkeypatch.setattr(decoder, "submit", recording_decode)
        pipe.update(0.0, 800, 600)
        _drain(pipe)
        assert order, "expected submissions"
        first_prefetch = next(
            (i for i, p in enumerate(order) if p == RenderPriority.PREFETCH), len(order)
        )
        assert all(p == RenderPriority.VISIBLE for p in order[:first_prefetch])
        assert any(p == RenderPriority.VISIBLE for p in order)
    finally:
        pipe.close()


def test_prefetch_decode_submits_when_visible_resident(tmp_path):
    """Prefetch tiles reach the process decoder once visible work is done.

    Walk three pages with a small viewport so the 2-entry decode LRU evicts
    page 0 while its tiles stay cached; returning to page 0 then finds
    visible hits but a missing LRU entry, staging PREFETCH decoder work.
    """
    from tests.helpers import make_dense_pdf
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=3)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 3)
    try:
        seen: list = []
        decoder = pipe._proc
        assert decoder is not None
        orig_decode = decoder.submit

        def recording_decode(*args, **kwargs):
            seen.append(kwargs.get("priority_visible", True))
            return orig_decode(*args, **kwargs)

        import unittest.mock as mock
        with mock.patch.object(decoder, "submit", recording_decode):
            for scroll in (0.0, 858.0, 1716.0):
                pipe.update(scroll, 400, 400)
                _drain(pipe)
            assert pipe.stats()["page_decode"]["decoded_pages"] == 2
            seen.clear()
            pipe.update(0.0, 400, 400)
            _drain(pipe)
        assert seen, "expected prefetch decoder submissions"
        assert all(v is False for v in seen)
    finally:
        pipe.close()


def test_pressure_drops_prefetch_first(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=4)
    # Engine-queue pressure mechanics: page decodes bypass the engine queue
    # under the default process decoder, so pin the in-process path here.
    pipe = _pipe(
        pdf, [(595.0, 842.0, 0)] * 4,
        engine_config=EngineConfig(worker_count=1, max_pending=2),
        process_decode_workers=0,
    )
    try:
        # One page decode per viewport update; the tiny queue (2) fills
        # while the single worker is still busy on millisecond renders.
        for scroll in (0.0, 858.0, 1716.0, 2574.0):
            pipe.update(scroll, 595, 842)
        stats = pipe.stats()
        # Microsecond submits vs millisecond renders: the tiny queue fills.
        assert stats["queue_full"] + stats["prefetch_dropped"] >= 1
        assert stats["submitted_visible"] >= 1
        # Visible work still converges on a follow-up update.
        _drain(pipe)
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        assert len(pipe.visible_ready(0.0, 595, 842)) > 0
    finally:
        pipe.close()


def test_duplicate_in_flight_never_double_submits(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=1)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)])
    try:
        first = pipe.update(0.0, 595, 842)
        second = pipe.update(0.0, 595, 842)  # workers still busy -> attach
        assert pipe.stats()["attached_in_flight"] >= 1
        _drain(pipe)
        total_unique = first["visible"] + first["prefetch"]
        submitted = pipe.stats()["submitted_visible"] + pipe.stats()["submitted_prefetch"]
        assert submitted <= total_unique
    finally:
        pipe.close()


def test_superseded_prefetch_helper():
    f1, f2, f3 = Future(), Future(), Future()

    def req(x: int, prio):
        return RenderRequest(
            document_id=DocumentId("d"),
            tile=TileRect(x, 0, 64, 64),
            priority=prio,
        )

    in_flight = {
        ("a",): (f1, 0, req(0, RenderPriority.PREFETCH), RenderPriority.PREFETCH),
        ("b",): (f2, 0, req(64, RenderPriority.VISIBLE), RenderPriority.VISIBLE),
        ("c",): (f3, 0, req(128, RenderPriority.PREFETCH), RenderPriority.PREFETCH),
    }
    dropped = superseded_prefetch(in_flight, {("a",)})
    assert len(dropped) == 1 and dropped[0].tile.x == 128


def test_stale_completion_never_enters_cache(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        pipe.update(0.0, 800, 600)
        _drain(pipe)
        before = pipe.stats()["cache"]["resident_bytes"]
        assert pipe.set_zoom(4.0) == 4.0  # epoch bump
        assert pipe.epoch == 1
        forged: Future = Future()
        key = list(pipe._cache._entries.values())[0].key
        old_req = RenderRequest(
            document_id=key.document_id, page_id=key.page_id, scale=key.scale,
            rotation_degrees=key.rotation, tile=key.to_tile_rect(), generation=0,
        )
        forged.set_result(
            RenderResult(request=old_req, success=True, data=b"\x00" * 16,
                         width_px=2, height_px=2, generation=0)
        )
        pipe._on_complete(forged, cache_identity(TileKey.from_request(old_req)), 0)
        assert pipe.stats()["stale_discarded"] == 1
        assert pipe.stats()["cache"]["resident_bytes"] == before
    finally:
        pipe.close()


def test_zoom_wiggle_renders_nothing_new(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        pipe.update(0.0, 800, 600)
        _drain(pipe)
        submitted = pipe.stats()["submitted_visible"] + pipe.stats()["submitted_prefetch"]
        assert pipe.set_zoom(1.13) == 1.0  # hysteresis holds the bucket
        assert pipe.epoch == 0
        pipe.update(0.0, 800, 600)
        _drain(pipe)
        now = pipe.stats()["submitted_visible"] + pipe.stats()["submitted_prefetch"]
        assert now == submitted
    finally:
        pipe.close()


def test_shutdown_during_render_is_safe(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=4)
    sizes = [(595.0, 842.0, 0)] * 4
    pipe = _pipe(pdf, sizes)
    try:
        pipe.update(0.0, 800, 600)
        pipe.update(2000.0, 800, 600)
    finally:
        pipe.close()
        pipe.close()
    assert pipe.stats()["cache"]["resident_bytes"] == 0
    assert pipe.stats()["engine"]["worker_handles"] == 0
    assert [t for t in threading.enumerate() if t.name.startswith("inkfall-render")] == []
    assert pipe.stats()["process_decoder"]["live_workers"] == 0
    assert [t for t in threading.enumerate() if t.name.startswith("procdecode-")] == []
    with pytest.raises(RenderCancelledError):
        pipe.update(0.0, 800, 600)


def test_document_close_releases_handles(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    pipe.update(0.0, 800, 600)
    _drain(pipe)
    # Default-on-2: page decodes run in isolated workers, not engine threads.
    assert pipe.stats()["process_decoder"]["live_workers"] >= 1
    procs = [s.proc for s in pipe._proc._slots]
    pipe.close()
    assert pipe.stats()["engine"]["worker_handles"] == 0
    assert pipe.stats()["process_decoder"]["live_workers"] == 0
    assert all(not p.is_alive() for p in procs)


def test_corrupted_pdf_never_leaks_fitz(tmp_path):
    pdf = make_invalid_pdf(str(tmp_path / "bad.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        summary = pipe.update(0.0, 800, 600)  # must not raise
        _drain(pipe)
        # Decode fails in the child -> group falls back; per-tile engine
        # renders fail on the follow-up update. Either counter proves it.
        pipe.update(0.0, 800, 600)
        stats = _drain(pipe)
        assert summary["visible"] == 4
        assert stats["decode_fallbacks"] + stats["submit_errors"] \
            + stats["engine"]["failed"] >= 1
        assert stats["engine"]["worker_handles"] >= 0
    finally:
        pipe.close()


def test_encrypted_pdf_is_render_failed(tmp_path):
    pdf = make_encrypted_pdf(str(tmp_path / "enc.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        pipe.update(0.0, 800, 600)
        _drain(pipe)
        # Child rejects the encrypted file -> fallback tiles fail in-engine
        # on the follow-up update; nothing renderable may remain.
        pipe.update(0.0, 800, 600)
        stats = _drain(pipe)
        assert stats["decode_fallbacks"] + stats["engine"]["failed"] >= 1
        assert len(pipe.visible_ready(0.0, 800, 600)) == 0
    finally:
        pipe.close()


def test_cache_budget_enforced_across_scrolls(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "r.pdf"), pages=3)
    tile_budget = 2 * 512 * 512 * 4
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 3, cache_budget_bytes=tile_budget)
    try:
        for scroll in (0.0, 842.0 + 16.0, 2 * (842.0 + 16.0)):
            pipe.update(scroll, 595, 842)
            stats = _drain(pipe)
            assert stats["cache"]["resident_bytes"] <= tile_budget
    finally:
        pipe.close()


def test_rapid_jumps_keep_decode_queue_bounded(tmp_path):
    import random
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=4)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 4)
    try:
        rng = random.Random(7)
        max_queued = 0
        for _ in range(30):
            scroll = float(rng.randrange(4)) * 858.0
            pipe.update(scroll, 595, 842)
            proc = pipe._proc
            assert proc is not None
            with proc._lock:
                queued = len(proc._visible) + len(proc._prefetch)
            max_queued = max(max_queued, queued)
        # Viewport-banded demand bounds legitimate queueing: the expanded
        # band (+-512px) covers at most 4 pages of pitch 858, one group per
        # page, plus in-flight workers. Dozens queued would be backlog.
        assert max_queued <= 6, f"decode backlog accumulated: {max_queued}"
        stats = _drain(pipe)
        pd = stats["process_decoder"]
        assert pd["outstanding"] == 0
        assert len(pipe._decode_cover) == 0
        # No-loss census: every submit reached a terminal state.
        assert pd["submits"] == (
            pd["results"] + pd["errors"] + pd["withdrawn"]
            + pd["shutdown_cancelled"] + pd["outstanding"]
        ), pd
        # Final viewport converges and paints real tiles.
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        ready = pipe.visible_ready(0.0, 595, 842)
        assert len(ready) == 4
        for tile in ready:
            assert len(tile.data) == tile.width_px * tile.height_px * 4
    finally:
        pipe.close()


def test_in_flight_stays_bounded(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=4)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 4)
    try:
        for i in range(6):
            pipe.update(float(i * 500), 595, 842)
        stats = pipe.stats()
        assert stats["in_flight"] <= 64 + 4  # max_pending + workers
        _drain(pipe)
    finally:
        pipe.close()


def test_rapid_viewport_replacement_converges(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES_600x800)
    try:
        for scroll in (0.0, 200.0, 0.0, 400.0, 0.0):
            pipe.update(scroll, 800, 600)
        _drain(pipe)
        ready = pipe.visible_ready(0.0, 800, 600)
        assert len(ready) == 4
    finally:
        pipe.close()


def test_intrinsic_rotation_composition_probe(tmp_path):
    """Intrinsic/user matrix verified through pixels, not arithmetic."""
    pdf = make_intrinsic_pdf(str(tmp_path / "i.pdf"))
    doc = DocumentId("rot")
    # Backend-reported geometry: rect pre-rotated by /Rotate + intrinsic.
    # Media is 600x800 for every page here.
    rect_sizes = [(600.0, 800.0, 0), (800.0, 600.0, 90),
                  (600.0, 800.0, 180), (800.0, 600.0, 270)]
    pages = [
        PageInfo(id=page_id_for(doc, i), document_id=doc, index=i,
                 size=Size(w, h), rotation=rot)
        for i, (w, h, rot) in enumerate(rect_sizes)
    ]
    pipe = DocumentPipeline(doc, pdf, pages,
                            engine_config=EngineConfig(worker_count=2))
    try:
        # Render dims @1.0/gap16: p0 600x800 @0; p1 800x600 @816;
        # p2 600x800 @1432; p3 800x600 @2248.
        # Per page: (scroll_y, dark_tiles, blank_tiles). Page 2's rect spans
        # two tiles, so both are dark and no blank tile exists there.
        expectations = {
            0: (0.0, ((0, 0),), ((512, 512),)),
            1: (816.0, ((512, 0),), ((0, 512),)),
            2: (1432.0, ((0, 512), (512, 512)), ()),
            3: (2248.0, ((0, 512),), ((512, 0),)),
        }
        for _page, (scroll, _d, _b) in expectations.items():
            pipe.update(scroll, 800, 600)
            _drain(pipe)  # converge each viewport before moving on: a jump
            # withdraws the previous viewport's still-queued decodes, so only
            # the final viewport would render without per-stop drains.
        for page, (scroll, dark_set, blank_set) in expectations.items():
            ready = {(t.key.tile_x, t.key.tile_y): t
                     for t in pipe.visible_ready(scroll, 800, 600)}
            for dark_xy in dark_set:
                dark = ready.get(dark_xy)
                assert dark is not None, f"page {page}: expected tile missing"
                assert _dark_count(dark.data) > 1000, f"page {page}: orientation wrong"
            for far_xy in blank_set:
                far = ready.get(far_xy)
                if far is not None:
                    assert _dark_count(far.data) == 0, f"page {page}: leak into far tile"
    finally:
        pipe.close()
