"""M3 Gate 5 stage 2: 4.0x isolated decode + slice-and-drop refusal.

4.0x page rasters (~32 MiB for T.pdf-class pages) exceed the 16 MiB
PageDecodeCache budget, so ``put`` refuses them. Slice-and-drop fans out
the covered tiles from the transient payload instead of marking the
group failed (which would reroute to the GIL-stalling per-tile path).
8.0x stays decode-ineligible and in-process.

Deterministic by construction: refusal, byte equality, counters, and
shutdown state are asserted directly — never timing sleeps.
"""

from __future__ import annotations

import pytest

from rendering.cache import cache_identity
from rendering.process_decoder import ProcessDecoder, ProcessDecoderConfig
from rendering.types import TileRect
from tests.helpers import make_dense_pdf, make_pdf
from tests.test_pipeline import _drain, _pipe
from tests.test_render_engine import _req


def _big_pdf(tmp_path, name: str = "big.pdf") -> str:
    """1000x1000pt text pages: 4.0x raster is 4000x4000x4 = 64 MiB."""
    return make_pdf(str(tmp_path / name), pages=2,
                    page_width=1000.0, page_height=1000.0)


def _sizes_big() -> list[tuple[float, float, int]]:
    return [(1000.0, 1000.0, 0), (1000.0, 1000.0, 0)]


def _sizes_dense() -> list[tuple[float, float, int]]:
    return [(595.0, 842.0, 0), (595.0, 842.0, 0)]


# -- 1. eligibility --------------------------------------------------------

def test_4x_routes_to_isolated_decode(tmp_path):
    pdf = _big_pdf(tmp_path)
    pipe = _pipe(pdf, _sizes_big(), ui_zoom=4.0, process_decode_workers=2)
    try:
        assert pipe.raster_scale == 4.0
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        assert stats["decode_submits"] >= 1
        assert stats["engine"]["completed"] == 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert len(ready) == len(planned) > 0
    finally:
        pipe.close()


def test_8x_remains_decode_ineligible_in_process(tmp_path):
    pdf = _big_pdf(tmp_path, "big8.pdf")
    pipe = _pipe(pdf, _sizes_big(), ui_zoom=8.0, process_decode_workers=2)
    try:
        assert pipe.raster_scale == 8.0
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        assert stats["process_decoder"]["submits"] == 0
        assert stats["engine"]["completed"] > 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert len(ready) == len(planned) > 0
    finally:
        pipe.close()


# -- 2. refusal fans out without retain or fallback -------------------------

def test_refusal_slice_and_drop(tmp_path):
    pdf = _big_pdf(tmp_path, "ref.pdf")
    pipe = _pipe(pdf, _sizes_big(), ui_zoom=4.0, process_decode_workers=2)
    try:
        before = pipe.stats()
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        pd = stats["page_decode"]
        assert pd["rejected"] >= 1  # 64 MiB single exceeds the 16 MiB budget
        assert stats["sliced_tiles"] > 0
        assert stats["decode_fallbacks"] == before["decode_fallbacks"]
        assert pd["decoded_pages"] == 0  # transient: never retained
        assert pd["decoded_bytes"] == 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert {cache_identity(t.key) for t in ready} == {
            cache_identity(k) for k in planned
        }
    finally:
        pipe.close()


# -- 3. byte equality vs in-process baseline --------------------------------

def test_4x_bytes_match_legacy_engine(tmp_path):
    pdf = _big_pdf(tmp_path, "px.pdf")
    gated = _pipe(pdf, _sizes_big(), ui_zoom=4.0, process_decode_workers=2)
    legacy = _pipe(pdf, _sizes_big(), ui_zoom=4.0, process_decode_workers=0)
    try:
        gated.update(0.0, 600, 900)
        _drain(gated)
        legacy.update(0.0, 600, 900)
        _drain(legacy)
        g = {cache_identity(t.key): bytes(t.data)
             for t in gated.visible_ready(0.0, 600, 900)}
        n = {cache_identity(t.key): bytes(t.data)
             for t in legacy.visible_ready(0.0, 600, 900)}
        assert g.keys() == n.keys() and g
        for key in g:
            assert g[key] == n[key]
    finally:
        gated.close()
        legacy.close()


# -- 4. stale / withdrawal under supersede ----------------------------------

def test_superseded_4x_preserves_semantics(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "s.pdf"), pages=2)
    pipe = _pipe(pdf, _sizes_dense(), ui_zoom=4.0, process_decode_workers=2)
    try:
        pipe.update(0.0, 600, 900)  # slow 32 MiB decode in flight
        assert pipe.set_zoom(1.0) == 1.0  # epoch bump supersedes it
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        pd = stats["process_decoder"]
        assert stats["stale_discarded"] + pd["withdrawn"] >= 1
        lost = (pd["submits"] - pd["results"] - pd["errors"]
                - pd["withdrawn"] - pd["shutdown_cancelled"]
                - pd["outstanding"])
        assert lost == 0
        assert pd["outstanding"] == 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert len(ready) == len(planned) > 0
    finally:
        pipe.close()


def test_rapid_4x_jumps_account_exactly(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "j.pdf"), pages=4)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 4,
                 ui_zoom=4.0, process_decode_workers=2)
    try:
        for i in range(12):
            pipe.update(float((i % 4) * 3368), 600, 900)
        stats = _drain(pipe)
        pd = stats["process_decoder"]
        lost = (pd["submits"] - pd["results"] - pd["errors"]
                - pd["withdrawn"] - pd["shutdown_cancelled"]
                - pd["outstanding"])
        assert lost == 0
        assert pd["outstanding"] == 0
        assert stats["in_flight"] == 0
    finally:
        pipe.close()


# -- 5. recycling with 4x payloads -------------------------------------------

def test_recycling_with_4x_payloads(tmp_path):
    from rendering import renderer as raster  # noqa: E402
    from rendering.types import RenderRequest  # noqa: E402
    from rendering.workers import WorkerHandles  # noqa: E402
    from rendering.coordinates import rotated_render_size  # noqa: E402
    from core.types import Size  # noqa: E402

    pdf = _big_pdf(tmp_path, "cyc.pdf")
    fw, fh = rotated_render_size(Size(1000.0, 1000.0), 4.0, 0)
    assert fw * fh * 4 > 16 * 1024 * 1024  # genuinely oversized
    dec = ProcessDecoder(ProcessDecoderConfig(workers=1, max_tasks_per_worker=2))
    try:
        futs = [dec.submit(f"cyc-{i}", pdf, 0, 4.0, 0, fw, fh) for i in range(3)]
        out = [f.result(timeout=180) for f in futs]
        assert dec.stats()["worker_recycles"] == 1
        assert dec.stats()["worker_restarts"] == 0
        handles = WorkerHandles()
        doc, page, intrinsic = handles.prepare_page(pdf, 0)
        req = RenderRequest(page_index=0, scale=4.0, rotation_degrees=0,
                            tile=TileRect(0, 0, fw, fh))
        base, w, h, _r, _c = raster.render_tile_raw(
            doc, req, 1000.0, 1000.0, None, alpha=True, intrinsic_rotation=intrinsic)
        for data, dw, dh, _s, _rm, _cm in out:
            assert (dw, dh) == (w, h) == (fw, fh)
            assert bytes(data) == bytes(base)
    finally:
        dec.shutdown()


# -- 6. shutdown during active 4x decode --------------------------------------

def test_shutdown_during_4x_decode(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "sd.pdf"), pages=2)
    pipe = _pipe(pdf, _sizes_dense(), ui_zoom=4.0, process_decode_workers=2)
    try:
        pipe.update(0.0, 600, 900)
        pipe.update(0.0, 600, 900)
    finally:
        pipe.close()
        pipe.close()
    assert pipe.stats()["engine"]["worker_handles"] == 0
    assert pipe.stats()["process_decoder"]["live_workers"] == 0
    assert pipe.stats()["process_decoder"]["outstanding"] == 0
    assert all(not s.proc.is_alive() for s in pipe._proc._slots)
    with pytest.raises(Exception):
        pipe.update(0.0, 600, 900)
