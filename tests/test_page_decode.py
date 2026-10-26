"""Path F prototype tests: page-decode-once invariants + fallback + bounds.

Deterministic by construction: submission counts asserted directly,
convergence asserted post-drain (a timeout means a real hang).
"""

from __future__ import annotations

import time

import pytest

from core.types import DocumentId, Size
from document.models import PageInfo, page_id_for
from rendering.cache import cache_identity
from rendering.page_decode import (
    PAGE_DECODE_BUDGET_BYTES,
    DecodedPage,
    PageDecodeCache,
    decode_key,
    slice_tile,
)
from rendering.pipeline import DocumentPipeline
from rendering.scheduler import EngineConfig
from rendering.types import TileKey
from tests.helpers import (
    make_dense_pdf,
    make_probe_pdf,
    make_rich_pdf,
    rasterize_page,
)


def _pages(doc: DocumentId, sizes):
    out = []
    for i, (w, h, rot) in enumerate(sizes):
        out.append(
            PageInfo(id=page_id_for(doc, i), document_id=doc, index=i,
                     size=Size(w, h), rotation=rot)
        )
    return out


SIZES_595 = [(595.0, 842.0, 0)]


def _pipe(pdf, sizes, **kw):
    doc = DocumentId("pagedecode")
    defaults = dict(engine_config=EngineConfig(worker_count=2))
    defaults.update(kw)
    return DocumentPipeline(doc, pdf, _pages(doc, sizes), **defaults)


def _drain(pipe, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while True:
        stats = pipe.stats()
        pd = stats["process_decoder"]
        if stats["in_flight"] == 0 and stats["engine"]["pending"] == 0 and (
                pd is None or pd["outstanding"] == 0):
            return stats
        assert time.monotonic() < deadline, "pipeline did not quiesce"
        time.sleep(0.005)


def _recording(pipe, monkeypatch):
    seen = []
    orig = pipe._engine.submit

    def rec(request, token=None):
        seen.append(request)
        return orig(request, token)

    monkeypatch.setattr(pipe._engine, "submit", rec)
    return seen


def _recording_decode(pipe, monkeypatch):
    """Record process-decoder submissions: (request_id, path, page, scale,
    rotation, full_w, full_h). Default-on-2 routes page decodes here."""
    seen = []
    assert pipe._proc is not None
    orig = pipe._proc.submit

    def rec(*args, **kwargs):
        seen.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(pipe._proc, "submit", rec)
    return seen


def test_four_tiles_one_decode(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        seen_engine = _recording(pipe, monkeypatch)
        seen_decode = _recording_decode(pipe, monkeypatch)
        summary = pipe.update(0.0, 595, 842)
        assert summary["visible"] == 4
        assert len(seen_decode) == 1, f"expected exactly 1 decode, got {len(seen_decode)}"
        assert seen_engine == []  # no per-tile engine renders on first pass
        assert pipe.stats()["decode_submits"] == 1
        _drain(pipe)
    finally:
        pipe.close()


def test_decode_uses_full_page_bbox(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        seen = _recording_decode(pipe, monkeypatch)
        pipe.update(0.0, 595, 842)
        assert len(seen) == 1
        _request_id, _path, page, scale, _rot, fw, fh = seen[0]
        assert page == 0
        assert (fw, fh) == (595, 842)
        assert scale == 1.0
        _drain(pipe)
    finally:
        pipe.close()


def test_decode_fans_out_to_normal_tile_entries(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        ready = pipe.visible_ready(0.0, 595, 842)
        assert len(ready) == 4
        assert pipe.stats()["sliced_tiles"] == 4
        for tile in ready:
            assert len(tile.data) == tile.width_px * tile.height_px * 4
            assert tile.key.scale == 1.0
    finally:
        pipe.close()


def test_same_page_reuses_one_decode(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        assert pipe.stats()["decode_submits"] == 1
        # Different viewport, same page: no new decode, tiles served by slicing.
        pipe.update(100.0, 595, 742)
        _drain(pipe)
        assert pipe.stats()["decode_submits"] == 1
        assert len(pipe.visible_ready(100.0, 595, 742)) > 0
    finally:
        pipe.close()


def test_two_pages_two_entries(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=2)
    pipe = _pipe(pdf, SIZES_595 * 2)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        pipe.update(858.0, 595, 842)
        _drain(pipe)
        assert pipe.stats()["decode_submits"] == 2
        assert pipe.stats()["page_decode"]["decoded_pages"] == 2
    finally:
        pipe.close()


def test_lru_bound_two_entries_and_bytes(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=4)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 4)
    try:
        for scroll in (0.0, 858.0, 1716.0, 2574.0):
            pipe.update(scroll, 595, 842)
            _drain(pipe)
        stats = pipe.stats()["page_decode"]
        assert stats["decoded_pages"] <= 2
        assert stats["decoded_bytes"] <= PAGE_DECODE_BUDGET_BYTES
        assert stats["evictions"] >= 1
    finally:
        pipe.close()


def test_budget_rejection_falls_back(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        monkeypatch.setattr(pipe._decodes, "put", lambda page: False)
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        # Decode refused -> slice-and-drop fans out the covered tiles from
        # the transient page WITHOUT recording a decode failure; the
        # viewport still converges on first pass.
        assert pipe.stats()["decode_fallbacks"] == 0
        assert pipe.stats()["sliced_tiles"] > 0
        assert len(pipe.visible_ready(0.0, 595, 842)) == 4
    finally:
        pipe.close()


def test_epoch_change_cannot_serve_stale(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=1)
    pipe = _pipe(pdf, SIZES_595, engine_config=EngineConfig(worker_count=1))
    try:
        pipe.update(0.0, 595, 842)
        pipe.set_zoom(2.0)  # epoch bump clears decoded pages
        assert pipe.stats()["page_decode"]["decoded_pages"] == 0
        _drain(pipe)
        assert pipe.stats()["stale_discarded"] >= 1
        # Recovery at the new bucket still converges.
        pipe.update(0.0, 1190, 1684)
        _drain(pipe)
        assert len(pipe.visible_ready(0.0, 1190, 1684)) > 0
    finally:
        pipe.close()


def test_unsupported_bucket_uses_tile_renderer(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        # 8.0x stays decode-ineligible (4.0x is isolated since stage 2).
        assert pipe.set_zoom(8.0) == 8.0
        seen = _recording(pipe, monkeypatch)
        pipe.update(0.0, 2975, 4210)
        _drain(pipe)
        assert pipe.stats()["decode_submits"] == 0
        assert len(seen) >= 1
        for req in seen:
            assert (req.tile.width, req.tile.height) != (4760, 6736)
        assert len(pipe.visible_ready(0.0, 2975, 4210)) > 0
    finally:
        pipe.close()


def test_pixels_match_tile_renderer(tmp_path):
    import numpy as np

    from tests.helpers import rasterize_page_rgba

    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=1)
    pipe = _pipe(pdf, SIZES_595)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        ready = {(t.key.tile_x, t.key.tile_y): t for t in pipe.visible_ready(0.0, 595, 842)}
        # Sliced tiles must equal the corresponding crop of a direct
        # full-page RGBA render (premultiplied bytes compared directly).
        ref_samples, rw, rh, _ = rasterize_page_rgba(pdf, 0, 1.0)
        ref = np.frombuffer(ref_samples, dtype=np.uint8).reshape(rh, rw, 4)
        for (tx, ty), tile in sorted(ready.items()):
            got = np.frombuffer(tile.data, dtype=np.uint8).reshape(
                tile.height_px, tile.width_px, 4)
            assert np.array_equal(
                got, ref[ty:ty + tile.height_px, tx:tx + tile.width_px, :])
    finally:
        pipe.close()


def test_prefetch_guard_intact(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595 * 4)
    try:
        pipe.update(0.0, 300, 300)
        stats = pipe.stats()
        assert stats["submitted_prefetch"] == 0
        assert stats["prefetch_deferred"] > 0
        _drain(pipe)
        pipe.update(0.0, 595, 842)
        _drain(pipe)
    finally:
        pipe.close()


def test_close_clears_decode_state(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        assert pipe.stats()["page_decode"]["decoded_pages"] >= 1
        pipe.close()
        assert pipe.stats()["page_decode"]["decoded_pages"] == 0
        assert pipe.stats()["page_decode"]["decoded_bytes"] == 0
    finally:
        if pipe._closed is False:
            pipe.close()


def test_decode_cache_unit_semantics():
    cache = PageDecodeCache(max_entries=2, budget_bytes=1024)
    a = DecodedPage(key=decode_key("d", "p0", 1.0, 0), data=b"a" * 512,
                    width_px=16, height_px=8, stride=64, generation=3)
    assert cache.put(a) is True
    assert cache.get(decode_key("d", "p0", 1.0, 0), 3) is a
    assert cache.get(decode_key("d", "p0", 1.0, 0), 4) is None  # stale generation
    big = DecodedPage(key=decode_key("d", "big", 1.0, 0), data=b"b" * 2048,
                       width_px=32, height_px=16, stride=128, generation=3)
    assert cache.put(big) is False  # oversize refused, never stored
    assert cache.stats()["rejected"] == 1
    assert cache.stats()["decoded_bytes"] == 512


def test_slice_tile_rejects_bad_geometry():
    page = DecodedPage(key=decode_key("d", "p", 1.0, 0), data=b"\x00" * 64,
                       width_px=4, height_px=4, stride=16, generation=0)
    assert len(slice_tile(page, 0, 0, 4, 4)) == 64
    with pytest.raises(ValueError):
        slice_tile(page, 0, 0, 5, 5)
    bad = DecodedPage(key=decode_key("d", "q", 1.0, 0), data=b"\x00" * 64,
                      width_px=4, height_px=4, stride=20, generation=0)
    with pytest.raises(ValueError):
        slice_tile(bad, 0, 0, 4, 4)


def test_decode_failure_falls_back_to_tiles(tmp_path, monkeypatch):
    from rendering.errors import RenderFailedError

    pdf = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=595.0, page_height=842.0)
    pipe = _pipe(pdf, SIZES_595)
    try:
        def failing(*args, **kwargs):
            raise RenderFailedError("injected decode failure")

        monkeypatch.setattr(pipe._proc, "submit", failing)
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        # Decode failed -> tiles still converge via individual renders.
        pipe.update(0.0, 595, 842)
        _drain(pipe)
        stats = pipe.stats()
        assert stats["decode_fallbacks"] >= 1 or stats["submit_errors"] >= 1
        assert len(pipe.visible_ready(0.0, 595, 842)) == 4
    finally:
        pipe.close()
