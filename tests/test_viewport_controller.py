"""M3-C viewport-controller tests: pure geometry, no Qt required."""

from __future__ import annotations

import pytest

from app.viewport_controller import (
    FloatRect,
    apply_dpr,
    display_scale_for,
    page_screen_rect,
    placeholder_rect,
    render_viewport_size,
    scroll_fraction,
    scroll_from_fraction,
    split_current_stale,
    tile_screen_rect,
    to_int_rect,
)


def test_display_scale():
    assert display_scale_for(1.0, 1.0) == 1.0
    assert display_scale_for(1.13, 1.0) == pytest.approx(1.13)
    assert display_scale_for(2.0, 2.0) == 1.0
    with pytest.raises(ValueError):
        display_scale_for(0.0, 1.0)


def test_render_viewport_size():
    assert render_viewport_size(800.0, 600.0, 1.0) == (800.0, 600.0)
    assert render_viewport_size(800.0, 600.0, 2.0) == (400.0, 300.0)


def test_page_mapping_top_middle_bottom():
    # 595x842 page at bucket 1.0, viewport 800x600, centered narrow page.
    r = page_screen_rect(0.0, 595.0, 842.0, 0.0, 800.0, 1.0)
    assert (r.x, r.y, r.width, r.height) == pytest.approx((102.5, 0.0, 595.0, 842.0))
    r = page_screen_rect(0.0, 595.0, 842.0, 100.0, 800.0, 1.0)
    assert r.y == pytest.approx(-100.0)
    # Gap boundary: second page offset includes gap.
    r = page_screen_rect(858.0, 595.0, 842.0, 858.0, 800.0, 1.0)
    assert r.y == pytest.approx(0.0)


def test_mixed_sizes_and_alignment():
    # Narrow page centers; wider-than-viewport page left-aligns at 0.
    narrow = page_screen_rect(0.0, 500.0, 500.0, 0.0, 800.0, 1.0)
    assert narrow.x == pytest.approx(150.0)
    wide = page_screen_rect(0.0, 1000.0, 700.0, 0.0, 800.0, 1.0)
    assert wide.x == pytest.approx(0.0)


def test_zoom_bucket_display_scale():
    assert display_scale_for(1.13, 1.0) == pytest.approx(1.13)
    assert display_scale_for(8.0, 8.0) == pytest.approx(1.0)
    r = page_screen_rect(0.0, 595.0, 842.0, 0.0, 800.0, 0.5)
    assert r.width == pytest.approx(297.5)


def test_tile_and_edge_tile_placement():
    page = FloatRect(102.5, 0.0, 595.0, 842.0)
    full = tile_screen_rect(0, 0, 512, 512, page, 1.0)
    assert (full.x, full.y, full.width, full.height) == pytest.approx((102.5, 0.0, 512.0, 512.0))
    edge = tile_screen_rect(512, 512, 83, 330, page, 1.0)
    assert (edge.x, edge.y) == pytest.approx((614.5, 512.0))
    assert (edge.width, edge.height) == pytest.approx((83.0, 330.0))
    scaled = tile_screen_rect(0, 0, 512, 512, page, 2.0)
    assert (scaled.width, scaled.height) == pytest.approx((1024.0, 1024.0))


@pytest.mark.parametrize("dpr", [1.0, 1.5, 2.0])
def test_dpr_applied_exactly_once(dpr):
    logical = FloatRect(102.5, 0.0, 595.0, 842.0)
    device = apply_dpr(logical, dpr)
    assert (device.x, device.width) == pytest.approx((102.5 * dpr, 595.0 * dpr))
    # Chained application would double-scale; single application is exact.
    assert apply_dpr(apply_dpr(logical, 1.0), dpr) == device
    with pytest.raises(ValueError):
        apply_dpr(logical, 0.0)


def test_placeholder_geometry_matches_tile():
    tile = FloatRect(10.0, 20.0, 512.0, 512.0)
    assert placeholder_rect(tile) == tile


def test_to_int_rect_rounding():
    assert to_int_rect(FloatRect(102.5, -0.4, 595.2, 841.7)).x == 103
    r = to_int_rect(FloatRect(0.0, 0.0, -5.0, 10.0))
    assert (r.width, r.height) == (0, 10)


def test_scroll_fraction_round_trip():
    assert scroll_fraction(0.0, 2000.0) == 0.0
    assert scroll_fraction(500.0, 2000.0) == pytest.approx(0.25)
    assert scroll_from_fraction(0.25, 2000.0) == pytest.approx(500.0)
    assert scroll_fraction(0.0, 0.0) == 0.0


def test_split_current_stale():
    have = {"a": 1, "b": 2, "c": 3}
    keep, drop = split_current_stale({"a", "c", "d"}, have)
    assert keep == {"a": 1, "c": 3}
    assert sorted(drop) == ["b"]
