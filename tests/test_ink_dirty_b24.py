"""M5-B.2.4 Step 3: dirty-region repaint pixel equivalence.

Paints scenes through the real ``_paint_page_strokes`` on QImages and
proves a dirty-region repaint composes identically to a full repaint:
single strokes, growing-stroke residue, translucent overlap order, and
untouched pixels outside the region. Intent-side rect updates are
verified by recording ``update()`` calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.models import InkStroke
from annotations.store import AnnotationStore
from core.types import PageId, Point

pytestmark = pytest.mark.usefixtures("qapp")

W, H = 500, 600
OX, OY = 50.0, 40.0  # fake page origin (screen px), scale 1.0


def _make_viewer(qtbot):
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    return viewer


def _page_rect():
    return SimpleNamespace(x=OX, y=OY, width=400.0, height=500.0)


def _layout(entry):
    return SimpleNamespace(raster_scale=1.0)


def _stroke(pid, pts, prs, color="#1b1e26", width_mm=3.0, opacity=1.0,
            enabled=True, profile="normal"):
    n = len(pts)
    return InkStroke(page_id=pid, points=tuple(Point(*p) for p in pts),
                     color=color, width_mm=width_mm, opacity=opacity,
                     pressures=tuple(prs), times_ms=tuple(100 + i for i in range(n)),
                     pressure_enabled=enabled, pressure_profile=profile)


def _blank():
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#ffffff"))
    return img


def _paint(img, viewer, strokes, dirty):
    """Model one _paint_body stroke pass: opaque base fill over the
    dirty rect, then the real stroke painter with the same region."""
    p = QPainter(img)
    try:
        p.fillRect(dirty, QColor("#ffffff"))
        viewer._paint_page_strokes(p, _layout(None), _entry, _page_rect(),
                                   1.0, dirty)
    finally:
        p.end()


_entry = SimpleNamespace(page_id=None)  # rebound per test via _bind


def _bind(viewer, strokes, entry_pid):
    """Bind committed strokes to the viewer's document store.

    M6.4: committed pen strokes are read from the owning document's
    ``AnnotationStore`` (no parallel viewer list), so these white-box
    painter tests place the strokes in a real store on a minimal
    document stand-in.
    """
    global _entry
    _entry = SimpleNamespace(page_id=entry_pid, index=0)
    store = AnnotationStore(1)
    for stroke in strokes:
        store.add(stroke, 0)
    viewer._document = SimpleNamespace(annotations=store)
    viewer._live_stroke = None


def _pixels(img):
    return img.bits().tobytes()


def _region_pixels(img, rect):
    clipped = img.copy(rect)
    return clipped.bits().tobytes()


def test_dirty_matches_full_single_stroke(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        pts = [(60.0 + i * 2.0, 60.0 + (i % 7) * 3.0) for i in range(60)]
        prs = [min(1.0, max(0.0, 0.5 + 0.3 * ((i % 5) - 2) / 2.0)) for i in range(60)]
        stroke = _stroke(pid, pts, prs)
        _bind(viewer, [stroke], pid)
        full = _blank()
        _paint(full, viewer, [stroke], QRect(0, 0, W, H))
        dirty = viewer._stroke_paint_rect(
            stroke, viewer._cached_outline(stroke), OX, OY, 1.0)
        assert dirty is not None and not dirty.isNull()
        part = _blank()
        _paint(part, viewer, [stroke], dirty)
        assert _region_pixels(part, dirty) == _region_pixels(full, dirty)
        # Outside the region the canvas is untouched background.
        outside = _blank()
        assert _pixels(part) != _pixels(outside)  # sanity: something painted
        for probe in (QRect(0, 0, W, 10), QRect(0, H - 10, W, 10)):
            assert _region_pixels(part, probe) == _region_pixels(outside, probe)
    finally:
        viewer.close()


def test_incremental_growth_leaves_no_stale_pixels(qtbot):
    """Persistent surface: paint v1 full, then v2 via union dirty rect
    (the production live-update sequence). Must equal a fresh full
    repaint of v2 — no residue from v1, no missing new pixels."""
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        base_pts = [(60.0 + i * 2.0, 80.0 + (i % 5) * 4.0) for i in range(40)]
        base_prs = [0.3 + 0.01 * i for i in range(40)]
        grown_pts = base_pts + [(140.0 + i * 2.0, 60.0 - i * 1.5) for i in range(25)]
        grown_prs = base_prs + [0.7 - 0.01 * i for i in range(25)]
        v1 = _stroke(pid, base_pts, base_prs)
        v2 = _stroke(pid, grown_pts, grown_prs)
        _bind(viewer, [v1], pid)
        screen = _blank()
        _paint(screen, viewer, [v1], QRect(0, 0, W, H))
        # Production sequence: dirty = union(previous, current).
        r1 = viewer._stroke_paint_rect(v1, viewer._cached_outline(v1), OX, OY, 1.0)
        r2 = viewer._stroke_paint_rect(v2, viewer._cached_outline(v2), OX, OY, 1.0)
        assert r1 is not None and r2 is not None
        _bind(viewer, [v2], pid)
        _paint(screen, viewer, [v2], r1.united(r2))
        fresh = _blank()
        _paint(fresh, viewer, [v2], QRect(0, 0, W, H))
        assert _pixels(screen) == _pixels(fresh)
    finally:
        viewer.close()


def test_translucent_overlap_order_preserved(qtbot):
    """Overlapping strokes with translucency: dirty repaint of the
    arrival region (over an opaque base fill, as _paint_body does)
    must equal the full composition — no double-blending."""
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        s1 = _stroke(pid, [(60.0 + i * 3.0, 100.0) for i in range(50)],
                     [0.5] * 50, color="#c00000", width_mm=4.0, opacity=0.5)
        s2 = _stroke(pid, [(80.0, 60.0 + i * 3.0) for i in range(60)],
                     [0.6] * 60, color="#0000c0", width_mm=4.0, opacity=0.6)
        _bind(viewer, [s1, s2], pid)
        full = _blank()
        _paint(full, viewer, [s1, s2], QRect(0, 0, W, H))
        # Production arrival sequence: s1 already on screen, s2 lands.
        screen = _blank()
        _paint(screen, viewer, [s1], QRect(0, 0, W, H))
        r2 = viewer._stroke_paint_rect(s2, viewer._cached_outline(s2), OX, OY, 1.0)
        assert r2 is not None
        _paint(screen, viewer, [s1, s2], r2)
        assert _pixels(screen) == _pixels(full)
    finally:
        viewer.close()


def test_distant_dirty_leaves_canvas_untouched(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        stroke = _stroke(pid, [(60.0 + i * 2.0, 60.0) for i in range(30)],
                         [0.5] * 30)
        _bind(viewer, [stroke], pid)
        before = _blank()
        _paint(before, viewer, [stroke], QRect(0, 0, W, H))
        after = before.copy()
        _paint(after, viewer, [stroke], QRect(400, 500, 90, 90))
        assert _pixels(after) == _pixels(before)
    finally:
        viewer.close()


def test_viewport_change_repaints_fully(qtbot):
    """A full-widget dirty rect paints every stroke (scroll/zoom path
    equivalence: bare update() → full region → nothing skipped)."""
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        strokes = [_stroke(pid, [(60.0 + i * 2.0, 60.0 + k * 40.0) for i in range(30)],
                           [0.4 + 0.01 * k] * 30)
                   for k in range(4)]
        _bind(viewer, strokes, pid)
        full = _blank()
        _paint(full, viewer, strokes, QRect(0, 0, W, H))
        self_rect = QRect(0, 0, W, H)  # what bare update() schedules
        part = _blank()
        _paint(part, viewer, strokes, self_rect)
        assert _pixels(part) == _pixels(full)
    finally:
        viewer.close()


def test_apply_intent_scopes_live_update_and_completes_full(qtbot):
    """_apply_intent schedules a QRect update for live moves and a
    scoped QRect update for stroke completion (B.2.6: union of the
    completed bounds and the last live rect — pixel-identical to the
    former full update via the same paint path)."""
    viewer = _make_viewer(qtbot)
    try:
        from app.viewport_controller import FloatRect

        pid = PageId()
        calls = []
        viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
        viewer._pipeline = SimpleNamespace(
            layout=SimpleNamespace(
                raster_scale=1.0, total_width_px=400.0,
                pages_in=lambda a, b: [0],
                entry=lambda i: SimpleNamespace(
                    page_id=pid, y_offset_px=0.0,
                    render_width_px=400.0, render_height_px=500.0)))
        viewer._display_scale = lambda: 1.0  # noqa: E731
        viewer._scroll_doc = lambda: 0.0  # noqa: E731
        viewer._scroll_doc_x = lambda: 0.0  # noqa: E731
        viewer._render_viewport = lambda: (float(W), float(H))  # noqa: E731
        pts = [(60.0 + i * 2.0, 60.0) for i in range(10)]
        stroke = _stroke(pid, pts, [0.5] * 10)
        viewer._apply_intent({"stroke_update": stroke})
        assert len(calls) == 1 and len(calls[0]) == 1
        rect = calls[0][0]
        assert isinstance(rect, QRect) and not rect.isNull()
        assert rect.width() < W and rect.height() < H  # scoped, not full
        # The rect covers the mapped stroke (page origin x=50 here:
        # page x 60..78 → screen 110..128).
        assert rect.left() <= 110 and rect.right() >= 128
        calls.clear()
        viewer._apply_intent({"stroke_done": stroke})
        # B.2.6 scoped completion: one QRect covering the completed
        # bounds plus the live rect above (110..128 on x).
        assert len(calls) == 1 and len(calls[0]) == 1
        done_rect = calls[0][0]
        assert isinstance(done_rect, QRect) and not done_rect.isNull()
        assert done_rect.width() < W and done_rect.height() < H
        assert done_rect.left() <= 110 and done_rect.right() >= 128
    finally:
        viewer.close()


def test_bbox_memo_shared_and_cleared(qtbot):
    """Completed outlines memoize page extents (one entry per outline,
    shared across paints); close clears the memo with the cache."""
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        strokes = [_stroke(pid, [(60.0 + i * 2.0, 60.0 + k * 40.0) for i in range(30)],
                           [0.5] * 30)
                   for k in range(3)]
        _bind(viewer, strokes, pid)
        img = _blank()
        assert viewer._bbox_memo == {}
        _paint(img, viewer, strokes, QRect(0, 0, W, H))
        assert len(viewer._bbox_memo) == 3
        first_ids = set(viewer._bbox_memo)
        _paint(img, viewer, strokes, QRect(0, 0, W, H))
        assert set(viewer._bbox_memo) == first_ids  # shared, no growth
        # Memoized bounds equal freshly computed ones.
        for stroke in strokes:
            outline = viewer._cached_outline(stroke)
            extents = viewer._bbox_memo[id(outline)][1]
            assert viewer._bbox_memo[id(outline)][0] is outline
            assert extents == viewer._outline_page_extents(outline)
        viewer.close_document()
        assert viewer._bbox_memo == {}
        assert viewer._outline_cache == {}
    finally:
        viewer.close()


def test_live_stroke_never_memoized(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        pid = PageId()
        stroke = _stroke(pid, [(60.0 + i * 2.0, 60.0) for i in range(10)],
                         [0.5] * 10)
        _bind(viewer, [stroke], pid)
        viewer._live_stroke = stroke
        img = _blank()
        _paint(img, viewer, [stroke], QRect(0, 0, W, H))
        _paint(img, viewer, [stroke], QRect(0, 0, W, H))
        assert viewer._bbox_memo == {}
        assert viewer._outline_cache == {}
    finally:
        viewer.close()
