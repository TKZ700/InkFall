"""M3-A viewport tests: ViewportState validation + pure request planning."""

from __future__ import annotations

import pytest

from core.types import DocumentId, PageId, Size
from rendering.tile_grid import plan_requests
from rendering.types import RenderPriority
from rendering.viewport import ViewportState


def _vp(**kw) -> ViewportState:
    args: dict = dict(
        document_id=DocumentId("v"),
        page_id=PageId("v:p2"),
        page_index=2,
        viewport_width_px=800,
        viewport_height_px=600,
    )
    args.update(kw)
    return ViewportState(**args)


PAGE = Size(595.0, 842.0)


def test_viewport_defaults_and_validation():
    vp = _vp()
    assert (vp.scroll_x, vp.scroll_y) == (0.0, 0.0)
    assert (vp.scale, vp.rotation, vp.generation) == (1.0, 0, 0)
    with pytest.raises(ValueError):
        _vp(viewport_width_px=0)
    with pytest.raises(ValueError):
        _vp(viewport_height_px=-5)
    with pytest.raises(ValueError):
        _vp(scale=0.0)
    with pytest.raises(ValueError):
        _vp(rotation=45)
    with pytest.raises(ValueError):
        _vp(generation=-1)
    with pytest.raises(ValueError):
        _vp(page_index=-1)


def test_plan_returns_visible_and_prefetch_with_priorities():
    visible, prefetch = plan_requests(_vp(), PAGE)
    assert len(visible) > 0
    assert all(r.priority == RenderPriority.VISIBLE for r in visible)
    assert all(r.priority == RenderPriority.PREFETCH for r in prefetch)
    assert len(prefetch) <= 8  # hard cap


def test_plan_propagates_all_request_fields():
    vp = _vp(scale=2.0, rotation=90, generation=9)
    visible, _ = plan_requests(vp, PAGE, source_path="doc.pdf")
    assert visible, "expected visible tiles"
    for req in visible:
        assert req.document_id == vp.document_id
        assert req.page_id == vp.page_id
        assert req.page_index == 2
        assert req.scale == 2.0
        assert req.rotation_degrees == 90
        assert req.generation == 9
        assert req.source_path == "doc.pdf"
        assert req.tile.x >= 0 and req.tile.y >= 0
        assert req.tile.width > 0 and req.tile.height > 0


def test_plan_generation_change_produces_different_keys():
    vis10, _ = plan_requests(_vp(generation=10), PAGE)
    vis11, _ = plan_requests(_vp(generation=11), PAGE)
    assert [r.key() for r in vis10] != [r.key() for r in vis11]
    assert [r.tile.key() for r in vis10] == [r.tile.key() for r in vis11]


def test_plan_deterministic_repeated():
    vp = _vp(scroll_x=123.5, scroll_y=77.25, scale=2.0)
    first = plan_requests(vp, PAGE)
    second = plan_requests(vp, PAGE)
    assert [r.key() for r in first[0]] == [r.key() for r in second[0]]
    assert [r.key() for r in first[1]] == [r.key() for r in second[1]]


def test_plan_empty_viewport_plans_nothing():
    visible, prefetch = plan_requests(_vp(scroll_x=9000.0), PAGE)
    assert visible == () and prefetch == ()


def test_plan_no_duplicate_keys():
    visible, prefetch = plan_requests(_vp(), PAGE)
    keys = [r.key() for r in (*visible, *prefetch)]
    assert len(keys) == len(set(keys))
    assert not set(map(lambda r: r.key(), visible)) & set(map(lambda r: r.key(), prefetch))


def test_plan_rotated_page_uses_rotated_dimensions():
    # 1000x1400 @ rot 90 -> 1400x1000 render px; an 800x600 viewport at
    # origin must see tile (512,0) but never exceed x=1400.
    vp = _vp(viewport_width_px=800, viewport_height_px=600, rotation=90)
    visible, _ = plan_requests(vp, Size(1000.0, 1400.0))
    assert visible
    for req in visible:
        assert req.tile.x + req.tile.width <= 1400
        assert req.tile.y + req.tile.height <= 1000


def test_plan_prefetch_margin_zero():
    visible, prefetch = plan_requests(_vp(), PAGE, prefetch_margin=0)
    assert len(visible) > 0
    assert prefetch == ()


def test_plan_source_path_defaults_none():
    visible, _ = plan_requests(_vp(), PAGE)
    assert all(r.source_path is None for r in visible)
