"""M2 tile rendering tests: scale, rotation, tiles, determinism, errors."""

from __future__ import annotations

import ast
from concurrent.futures import Future
from pathlib import Path

import pytest

from core.types import DocumentId, PageId
from document.errors import InvalidPageIndexError
from rendering.engine import RendererEngine
from rendering.errors import RenderFailedError
from rendering.renderer import PIXEL_FORMAT
from rendering.scheduler import EngineConfig
from rendering.types import RenderPriority, RenderRequest, TileRect
from tests.helpers import make_probe_pdf, make_rich_pdf


def _req(pdf: str, page_index: int = 0, scale: float = 1.0, rotation: int = 0,
         tile: TileRect | None = None, **kw) -> RenderRequest:
    return RenderRequest(
        document_id=DocumentId("t"),
        page_id=PageId(f"t:p{page_index}"),
        page_index=page_index,
        scale=scale,
        rotation_degrees=rotation,
        tile=tile if tile is not None else TileRect(0, 0, 512, 512),
        source_path=pdf,
        **kw,
    )


def _render(pdf: str, **kw) -> object:
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        return engine.submit(_req(pdf, **kw)).result(timeout=60)
    finally:
        engine.shutdown()


def _dark_count(data: bytes) -> int:
    """Count opaque dark RGBA pixels (alpha preserved by the rasterizer).

    Blank page background renders transparent (0,0,0,0); only pixels with
    alpha > 127 and r,g,b < 128 count (solid black probe geometry).
    """
    assert data is not None
    n = 0
    for i in range(0, len(data), 4):
        if data[i + 3] > 127 and data[i] < 128 and data[i + 1] < 128 and data[i + 2] < 128:
            n += 1
    return n


def test_valid_page_renders_rgba_contract(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "rich.pdf"), pages=2)
    res = _render(pdf)
    assert res.success is True
    assert res.pixel_format == PIXEL_FORMAT == "RGBA8"
    assert (res.width_px, res.height_px) == (512, 512)
    assert res.stride == 512 * 4
    assert res.data is not None and len(res.data) == 512 * 512 * 4
    # Non-trivial content (gradient/text), not a flat page.
    assert len(set(res.data[::997])) > 4


def test_scale_dimensions_deterministic(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "s.pdf"), pages=1, page_width=595.0, page_height=842.0)
    r1 = _render(pdf, scale=1.0, tile=TileRect(0, 0, 595, 842))
    r2 = _render(pdf, scale=2.0, tile=TileRect(0, 0, 1190, 1684))
    assert (r1.width_px, r1.height_px) == (595, 842)
    assert (r2.width_px, r2.height_px) == (1190, 1684)
    assert len(r2.data or b"") == 1190 * 1684 * 4


def test_scale_4x_tile(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "s4.pdf"), pages=1)
    res = _render(pdf, scale=4.0, tile=TileRect(0, 0, 512, 512))
    assert (res.width_px, res.height_px) == (512, 512)


@pytest.mark.parametrize("rotation,expected", [(0, (1000, 1400)), (90, (1400, 1000)),
                                               (180, (1000, 1400)), (270, (1400, 1000))])
def test_rotation_dimensions(tmp_path, rotation, expected):
    pdf = make_rich_pdf(str(tmp_path / f"r{rotation}.pdf"), pages=1,
                        page_width=1000.0, page_height=1400.0)
    full = TileRect(0, 0, expected[0], expected[1])
    res = _render(pdf, rotation=rotation, tile=full)
    assert (res.width_px, res.height_px) == expected


def test_orientation_content_probe(tmp_path):
    """Black rect at page top-left must rotate clockwise with content."""
    pdf = make_probe_pdf(str(tmp_path / "probe.pdf"))
    # r=0: dark in top-left tile, white in bottom-right tile.
    top_left = _render(pdf, rotation=0, tile=TileRect(0, 0, 256, 256))
    bottom_right = _render(pdf, rotation=0, tile=TileRect(344, 544, 256, 256))
    assert _dark_count(top_left.data) > 1000
    assert _dark_count(bottom_right.data) == 0
    # r=90 CW: page top-left -> rotated top-right; x in [600,800], y in [0,200].
    moved = _render(pdf, rotation=90, tile=TileRect(544, 0, 256, 256))
    vacated = _render(pdf, rotation=90, tile=TileRect(0, 0, 256, 256))
    assert _dark_count(moved.data) > 1000
    assert _dark_count(vacated.data) == 0


def test_edge_tile_clipping(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "edge.pdf"))  # 600x800
    res = _render(pdf, tile=TileRect(512, 512, 512, 512))
    assert (res.width_px, res.height_px) == (88, 288)
    assert len(res.data or b"") == 88 * 288 * 4


def test_tile_outside_page_fails(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "out.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        with pytest.raises(RenderFailedError):
            engine.submit(_req(pdf, tile=TileRect(5000, 5000, 512, 512))).result(timeout=60)
    finally:
        engine.shutdown()


def test_deterministic_bytes(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "det.pdf"), pages=1)
    a = _render(pdf, tile=TileRect(0, 0, 512, 512))
    b = _render(pdf, tile=TileRect(0, 0, 512, 512))
    assert a.data == b.data
    assert (a.width_px, a.height_px) == (b.width_px, b.height_px)


def test_no_full_page_rasterization_path():
    """Every get_pixmap call in the renderer must pass clip= (region raster)."""
    source = Path("rendering/renderer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "get_pixmap"]
    assert calls, "renderer must rasterize via get_pixmap"
    for call in calls:
        keywords = {k.arg for k in call.keywords}
        assert "clip" in keywords, "get_pixmap without clip= would render the full page"


def test_invalid_page_index_translated(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "bad.pdf"))
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        with pytest.raises(InvalidPageIndexError):
            engine.submit(_req(pdf, page_index=99)).result(timeout=60)
    finally:
        engine.shutdown()


def test_invalid_document_path_rejected(tmp_path):
    engine = RendererEngine(EngineConfig(worker_count=1))
    try:
        with pytest.raises(RenderFailedError):
            engine.submit(_req(str(tmp_path / "missing.pdf")))
    finally:
        engine.shutdown()


def test_prefetch_priority_flag_accepted(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "pri.pdf"))
    res = _render(pdf, priority=RenderPriority.PREFETCH)
    assert res.success is True
    assert isinstance(res.request.request_id, str) and res.request.request_id
