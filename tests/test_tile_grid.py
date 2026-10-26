"""M3-A tile-grid tests: TileKey identity + deterministic grid math."""

from __future__ import annotations

import pytest

from core.types import DocumentId, PageId, Size
from rendering.tile_grid import (
    MAX_PREFETCH_TILES,
    page_render_size,
    prefetch_tiles,
    visible_tile_range,
    visible_tiles,
)
from rendering.types import DEFAULT_TILE_SIZE, TileKey, TileRect
from rendering.viewport import ViewportState


def _vp(scroll_x: float = 0.0, scroll_y: float = 0.0, vw: int = 800, vh: int = 600,
        scale: float = 1.0, rotation: int = 0, generation: int = 0) -> ViewportState:
    return ViewportState(
        document_id=DocumentId("d"),
        page_id=PageId("d:p0"),
        page_index=0,
        viewport_width_px=vw,
        viewport_height_px=vh,
        scroll_x=scroll_x,
        scroll_y=scroll_y,
        scale=scale,
        rotation=rotation,
        generation=generation,
    )


def _key(**kw) -> TileKey:
    args: dict = dict(document_id=DocumentId("d"), page_id=PageId("d:p0"))
    args.update(kw)
    return TileKey(**args)


# -- TileKey identity --------------------------------------------------------

def test_tilekey_equality_and_hashing():
    a = _key(tile_x=0, tile_y=0)
    b = _key(tile_x=0, tile_y=0)
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


def test_tilekey_generation_distinction():
    assert _key(generation=10) != _key(generation=11)


def test_tilekey_scale_distinction():
    assert _key(scale=1.0) != _key(scale=2.0)


def test_tilekey_rotation_distinction():
    assert _key(rotation=0) != _key(rotation=90)


def test_tilekey_origin_distinction():
    assert _key(tile_x=0, tile_y=0) != _key(tile_x=512, tile_y=0)
    assert _key(tile_x=0, tile_y=0) != _key(tile_x=0, tile_y=512)


def test_tilekey_clipped_size_distinction():
    assert _key(tile_w=512, tile_h=512) != _key(tile_w=88, tile_h=288)


def test_tilekey_validation():
    with pytest.raises(ValueError):
        _key(generation=-1)
    with pytest.raises(ValueError):
        _key(scale=0.0)
    with pytest.raises(ValueError):
        _key(rotation=45)
    with pytest.raises(ValueError):
        _key(tile_x=-1)
    with pytest.raises(ValueError):
        _key(tile_w=0)


def test_tilekey_round_trip_and_canonical_path():
    from rendering.types import RenderRequest

    key = _key(tile_x=512, tile_y=0, tile_w=512, tile_h=512, generation=7)
    rect = key.to_tile_rect()
    assert isinstance(rect, TileRect)
    assert (rect.x, rect.y, rect.width, rect.height) == (512, 0, 512, 512)
    req = RenderRequest(
        document_id=key.document_id,
        page_id=key.page_id,
        scale=key.scale,
        rotation_degrees=key.rotation,
        tile=rect,
        generation=key.generation,
    )
    assert TileKey.from_request(req) == key
    # Request key embeds the canonical tile tuple.
    assert req.key()[-2:] == ((512, 0, 512, 512), 7)


# -- page dimensions ---------------------------------------------------------

@pytest.mark.parametrize("rotation,expected", [(0, (595, 842)), (90, (842, 595)),
                                               (180, (595, 842)), (270, (842, 595))])
def test_page_render_size_rotations(rotation, expected):
    assert page_render_size(595.0, 842.0, 1.0, rotation) == expected


def test_page_render_size_rotated_case():
    assert page_render_size(1000.0, 1400.0, 1.0, 90) == (1400, 1000)
    assert page_render_size(1000.0, 1400.0, 2.0, 0) == (2000, 2800)


@pytest.mark.parametrize("scale,expected", [(1.0, (595, 842)), (2.0, (1190, 1684)),
                                             (4.0, (2380, 3368))])
def test_page_render_size_scales(scale, expected):
    assert page_render_size(595.0, 842.0, scale, 0) == expected


# -- visible range/grid ------------------------------------------------------

def test_single_tile_page():
    vp = _vp(vw=400, vh=300)
    assert visible_tile_range(vp, 400, 300) == (0, 0, 0, 0)
    keys = visible_tiles(vp, 400, 300)
    assert len(keys) == 1
    assert (keys[0].tile_x, keys[0].tile_y) == (0, 0)


def test_viewport_on_tile_boundary():
    # Exactly one 512 tile: edge at 512 must NOT pull in the next tile.
    vp = _vp(vw=512, vh=512)
    assert visible_tile_range(vp, 1024, 1024) == (0, 0, 0, 0)


def test_viewport_crossing_tile_boundary():
    vp = _vp(scroll_x=500.0, scroll_y=0.0, vw=512, vh=512)
    assert visible_tile_range(vp, 1200, 800) == (0, 0, 1, 0)
    keys = visible_tiles(vp, 1200, 800)
    assert [(k.tile_x, k.tile_y) for k in keys] == [(0, 0), (512, 0)]


def test_partial_edge_tiles_clipped():
    vp = _vp(vw=600, vh=800)  # page 600x800, tile 512
    keys = visible_tiles(vp, 600, 800)
    by_origin = {(k.tile_x, k.tile_y): (k.tile_w, k.tile_h) for k in keys}
    assert by_origin[(0, 0)] == (512, 512)
    assert by_origin[(512, 0)] == (88, 512)
    assert by_origin[(0, 512)] == (512, 288)
    assert by_origin[(512, 512)] == (88, 288)


def test_completely_outside_viewport_is_empty():
    vp = _vp(scroll_x=5000.0, scroll_y=0.0)
    assert visible_tile_range(vp, 600, 800) is None
    assert visible_tiles(vp, 600, 800) == ()
    above = _vp(scroll_x=0.0, scroll_y=-700.0)
    assert visible_tile_range(above, 600, 800) is None


def test_negative_overscroll_clamps_without_negatives():
    vp = _vp(scroll_x=-100.0, scroll_y=-50.0, vw=512, vh=512)
    box = visible_tile_range(vp, 1024, 1024)
    assert box == (0, 0, 0, 0)
    for key in visible_tiles(vp, 1024, 1024):
        assert key.tile_x >= 0 and key.tile_y >= 0


def test_no_out_of_bounds_or_duplicates():
    vp = _vp(vw=800, vh=600)
    keys = visible_tiles(vp, 595, 842)
    assert len(keys) == len(set(keys))  # no duplicates
    for key in keys:
        assert key.tile_x >= 0 and key.tile_y >= 0
        assert key.tile_w > 0 and key.tile_h > 0
        assert key.tile_x + key.tile_w <= 595
        assert key.tile_y + key.tile_h <= 842


def test_row_major_deterministic_order():
    vp = _vp(vw=1024, vh=1024)
    first = visible_tiles(vp, 1200, 1200)
    second = visible_tiles(vp, 1200, 1200)
    assert first == second
    origins = [(k.tile_x, k.tile_y) for k in first]
    assert origins == sorted(origins, key=lambda p: (p[1], p[0]))


def test_generation_propagates_to_keys():
    assert visible_tiles(_vp(generation=10), 600, 800)[0].generation == 10
    assert visible_tiles(_vp(generation=11), 600, 800)[0].generation == 11


# -- prefetch ----------------------------------------------------------------

def test_prefetch_ring_and_margin_zero():
    vp = _vp(vw=512, vh=512)  # single visible tile on a 3x3 grid
    vis = visible_tiles(vp, 1536, 1536)
    assert len(vis) == 1
    ring = prefetch_tiles(vp, 1536, 1536, visible=vis)
    assert len(ring) == 3  # right, bottom, bottom-right neighbours
    assert not set(ring) & set(vis)
    assert prefetch_tiles(vp, 1536, 1536, margin_tiles=0, visible=vis) == ()


def test_prefetch_hard_cap():
    vp = _vp(vw=512, vh=512)
    vis = visible_tiles(vp, 4096, 4096)
    ring = prefetch_tiles(vp, 4096, 4096, visible=vis)
    assert len(ring) <= MAX_PREFETCH_TILES
    assert MAX_PREFETCH_TILES == 8


def test_prefetch_excludes_out_of_bounds():
    vp = _vp(scroll_x=3500.0, scroll_y=3500.0, vw=512, vh=512)
    vis = visible_tiles(vp, 4096, 4096)
    ring = prefetch_tiles(vp, 4096, 4096, visible=vis)
    for key in ring:
        assert key.tile_x + key.tile_w <= 4096
        assert key.tile_y + key.tile_h <= 4096
