"""M5-B.2.4 Step 2: completed-stroke geometry cache.

Validates the viewer outline cache: miss-then-hit behavior, exact
pixel-input identity against the uncached renderer path, invalidation
on document close, and bypass rules (live stroke, duck-typed
non-InkStroke items, legacy None results).
"""

from __future__ import annotations

import pytest

from annotations.models import InkStroke
from core.types import PageId, Point

pytestmark = pytest.mark.usefixtures("qapp")


def _make_viewer(qtbot):
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    return viewer


def _stroke(n=60, profile="normal", width_mm=3.0, enabled=True):
    pts = tuple(Point(float(i * 1.5), 3.0 * (i % 5)) for i in range(n))
    prs = tuple(min(1.0, max(0.0, 0.5 + 0.4 * ((-1) ** i) * (i % 3) / 2.0))
                for i in range(n))
    tms = tuple(100 + i for i in range(n))
    return InkStroke(points=pts, color="#1b1e26", width_mm=width_mm,
                     pressures=prs, times_ms=tms,
                     pressure_enabled=enabled, pressure_profile=profile)


def test_miss_then_hit_returns_identical_object(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        stroke = _stroke()
        assert viewer.outline_cache_hits == 0
        assert viewer.outline_cache_misses == 0
        first = viewer._cached_outline(stroke)
        assert first is not None
        assert viewer.outline_cache_misses == 1
        assert viewer.outline_cache_hits == 0
        second = viewer._cached_outline(stroke)
        assert second is first  # same object, no rebuild
        assert viewer.outline_cache_misses == 1
        assert viewer.outline_cache_hits == 1
        assert len(viewer._outline_cache) == 1
    finally:
        viewer.close()


def test_cached_outline_identical_to_uncached(qtbot):
    """Pixel-identity proof: the raster step is a pure function of the
    outline + paint params (unchanged code), so outline equality
    across profiles/sizes implies identical pixels."""
    viewer = _make_viewer(qtbot)
    try:
        for profile in ("soft", "normal", "hard"):
            for width_mm in (0.7, 1.5, 3.0):
                stroke = _stroke(n=45, profile=profile, width_mm=width_mm)
                cached = viewer._cached_outline(stroke)
                uncached = viewer._pressure_outline(stroke)
                assert cached == uncached
                assert cached.polygon == uncached.polygon
                assert cached.dots == uncached.dots
                assert cached.discs == uncached.discs
    finally:
        viewer.close()


def test_close_document_invalidates_cache(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        viewer._cached_outline(_stroke())
        viewer._cached_outline(_stroke(n=30, profile="soft"))
        assert len(viewer._outline_cache) == 2
        viewer.close_document()
        assert viewer._outline_cache == {}
    finally:
        viewer.close()


def test_live_stroke_bypasses_cache(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        stroke = _stroke()
        viewer._live_stroke = stroke
        first = viewer._cached_outline(stroke)
        second = viewer._cached_outline(stroke)
        assert first is not None and second is not None
        # Live strokes are never stored; counters untouched.
        assert viewer._outline_cache == {}
        assert viewer.outline_cache_hits == 0
        assert viewer.outline_cache_misses == 0
    finally:
        viewer.close()


def test_duck_typed_stroke_bypasses_cache(qtbot):
    from types import SimpleNamespace

    viewer = _make_viewer(qtbot)
    try:
        bogus = SimpleNamespace(points=(Point(0.0, 0.0), Point(1.0, 0.0)),
                                pressures=(0.5, 0.5),
                                pressure_enabled=True,
                                pressure_profile="velvet", width_mm=1.0)
        assert viewer._cached_outline(bogus) is None
        assert viewer._outline_cache == {}
    finally:
        viewer.close()


def test_legacy_none_results_never_cached(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        disabled = _stroke(enabled=False)
        assert viewer._cached_outline(disabled) is None
        assert viewer._cached_outline(disabled) is None
        assert viewer._outline_cache == {}
        assert viewer.outline_cache_hits == 0
        assert viewer.outline_cache_misses == 2
    finally:
        viewer.close()


def test_distinct_strokes_never_share_cache_entry(qtbot):
    """Each stroke carries a unique id, so even content-identical
    strokes key separately: no false sharing, no cross-talk."""
    viewer = _make_viewer(qtbot)
    try:
        pts = (Point(0.0, 0.0), Point(2.0, 1.0), Point(4.0, 0.0))
        kwargs = dict(points=pts, pressures=(0.2, 0.6, 0.9),
                      times_ms=(1, 2, 3), pressure_enabled=True,
                      created_at=1234.5)
        first = viewer._cached_outline(InkStroke(**kwargs))
        second = viewer._cached_outline(InkStroke(**kwargs))
        assert first == second  # same geometry ...
        assert first is not second  # ... but separate cache entries
        assert len(viewer._outline_cache) == 2
        assert viewer.outline_cache_misses == 2
    finally:
        viewer.close()
