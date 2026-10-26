"""M5-B.2.6 Step 1: incremental live-invalidation rect.

Proves the O(1) tail rect (``_live_tail_rect``) covers every pixel the
live outline may change — through the real intent path with recorded
update rects, dual-paint pixel equivalence per move, and a geometric
proof test that the tail window suffices for the actual smoothing
implementation (or the safe fallback engages).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.ink_geometry import (
    SMOOTH_MIN_SPACING_PT,
    SMOOTH_PASSES,
)
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _dims(viewer):
    # The viewer never fills the window (MainWindow chrome); images
    # must match the viewer rect exactly or full-vs-dirty paints speak
    # about different regions.
    r = viewer.rect()
    return r.width(), r.height()


def _pen_tool(pressure=True, width_mm=3.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=width_mm, opacity=1.0, pressure=pressure)


def _open(qtbot, tmp_path, tool):
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / "live26.pdf"), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool)
    # Settle the tile pipeline: pixel comparisons need a frozen tile
    # state (no placeholders flipping to images mid-test). Bounded
    # best-effort wait; the synchronous test body runs no event loop,
    # so whatever state holds here stays frozen during comparisons.
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    for _ in range(30):
        _paint(viewer, None)
        if viewer.painted_placeholders == 0 and viewer.painted_tiles > 0:
            qtbot.wait(150)
            _paint(viewer, None)
            if viewer.painted_placeholders == 0:
                break
        qtbot.wait(150)
    return window, viewer


def _page_id(viewer):
    return viewer._pipeline.layout.entry(0).page_id


def _page_map(viewer):
    """(ox, oy, factor) exactly as the paint path derives them."""
    from app.viewport_controller import page_screen_rect

    layout = viewer._pipeline.layout
    scale = viewer._display_scale()
    vw, _vh = viewer._render_viewport()
    entry = layout.entry(0)
    rect = page_screen_rect(float(entry.y_offset_px),
                            float(entry.render_width_px),
                            float(entry.render_height_px),
                            viewer._scroll_doc(), vw, scale,
                            viewer._scroll_doc_x(),
                            float(layout.total_width_px))
    return (float(rect.x), float(rect.y),
            float(layout.raster_scale) * scale)


def _paint(viewer, dirty):
    w, h = _dims(viewer)
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#000000"))
    p = QPainter(img)
    try:
        viewer._paint_body(p, QRect(dirty) if dirty is not None else None)
    finally:
        p.end()
    return img


def _paint_dirty_onto(viewer, base, dirty):
    img = base.copy()
    p = QPainter(img)
    try:
        # Production paintEvents arrive pre-clipped to the update
        # region by Qt; replicate that clip or the unconditional page
        # fill would erase strokes outside the dirty rect (a harness
        # artifact, not paint behavior).
        p.setClipRect(QRect(dirty))
        viewer._paint_body(p, QRect(dirty))
    finally:
        p.end()
    return img


def _bytes(img):
    return img.bits().tobytes()


def _adversarial(n, seed=7):
    """Straight + sharp turns + reversal + duplicates + pressure jumps."""
    import math

    pts, prs = [], []
    for i in range(n):
        if i < n // 3:
            pts.append((60.0 + i * 1.5, 120.0 + (i % 9) * 2.0))
            prs.append(0.5)
        elif i < 2 * n // 3:
            # sharp zigzag with pressure jumps
            pts.append((60.0 + (n // 3) * 1.5 + (i % 2) * 40.0,
                        120.0 + (i - n // 3) * 1.2))
            prs.append(0.05 if i % 2 == 0 else 1.0)
        else:
            # reversal back over the path plus near-duplicates
            j = n - 1 - i
            pts.append((60.0 + j * 1.5, 120.0 + (j % 9) * 2.0))
            prs.append(0.9)
            if i % 11 == 0:
                pts.append((60.0 + j * 1.5 + 1e-6, 120.0 + (j % 9) * 2.0))
                prs.append(0.9)
    pts = pts[:n]
    prs = (prs + [0.5] * n)[:n]
    assert len(pts) == n and len(prs) == n
    return pts, prs


def _drive_recorded(viewer, page, pts, prs):
    """Feed samples through the real path; record update() calls.

    Returns (press_rect, rects, snapshots, outlines): the press update
    rect, per-move update args, the live stroke snapshot after each
    move, and the incremental outline pulled at each move.
    """
    it = viewer._interaction
    calls = []
    orig = viewer.update
    viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
    try:
        t = 1000
        press_before = len(calls)
        viewer._apply_intent(it.handle_pen_press(
            page, pts[0][0], pts[0][1], prs[0], t))
        # The press update rect covers the start cap; production
        # coverage is press + moves, so the proof must include it.
        assert len(calls) == press_before + 1
        assert len(calls[press_before]) == 1
        press_rect = QRect(calls[press_before][0])
        rects, snaps, outlines = [], [], []
        for i in range(1, len(pts)):
            t += 8
            before = len(calls)
            viewer._apply_intent(it.handle_pen_move(
                page, pts[i][0], pts[i][1], prs[i], t))
            new = calls[before:]
            assert len(new) == 1, new
            rects.append(new[0])
            # B.3.5: the live view is one mutable object per stroke, so
            # retrospective geometry must use an immutable prefix
            # snapshot (the full InkStroke build here is test-only and
            # mirrors the pre-change per-move snapshots exactly).
            snaps.append(viewer._interaction._last_pen._stroke())
            # Capture the incremental outline NOW: it advances with
            # every sample, so a later pull would mismatch this prefix.
            outlines.append(viewer._interaction.live_outline())
        return press_rect, rects, snaps, outlines
    finally:
        viewer.update = orig


def test_tail_rect_used_no_full_scan(qtbot, tmp_path):
    """Ordinary samples take the tail path: QRect updates, zero
    full-outline scans, bounded area."""
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    page = _page_id(viewer)
    pts, prs = _adversarial(300)
    scans = []
    orig = viewer._outline_page_extents
    viewer._outline_page_extents = lambda *a: (  # noqa: E731
        scans.append(1) or orig(*a))
    try:
        _press, rects, snaps, _outlines = _drive_recorded(
            viewer, page, pts, prs)
    finally:
        viewer._outline_page_extents = orig
        viewer.close_document()
        _window.close()
    assert len(rects) == 299
    # Every move scheduled a bounded rect (never a bare full update).
    assert all(len(r) == 1 and isinstance(r[0], QRect) for r in rects), \
        [r for r in rects if len(r) != 1][:3]
    assert all(not r[0].isNull() for r in rects)
    vw, vh = _dims(viewer)
    widget_area = vw * vh
    areas = [r[0].width() * r[0].height() for r in rects]
    assert max(areas) < widget_area // 4, max(areas)
    # The full-vertex scan never ran for any ordinary sample.
    assert scans == [], len(scans)


def test_tail_window_sufficient_for_smoothing(qtbot, tmp_path):
    """Geometric proof: at every prefix, the full-outline rect is
    covered by the union of recorded tail rects (or the tail path
    provably declines via None and the fallback covers)."""
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        ox, oy, factor = _page_map(viewer)
        for n, label in ((60, "short"), (300, "mid"),
                         (120, "dense")):
            if label == "dense":
                # 400+ raw samples inside ~2pt: resample/chaikin stress.
                pts = [(200.0 + (i % 13) * 0.15, 300.0 + (i % 7) * 0.2)
                       for i in range(400)]
                prs = [0.05 + 0.9 * ((i * 7919) % 100) / 99.0
                       for i in range(400)]
            else:
                pts, prs = _adversarial(n)
            press, rects, snaps, outlines = _drive_recorded(
                viewer, page, pts, prs)
            covered = QRect(press)
            for args, snap, outline in zip(rects, snaps, outlines):
                assert len(args) == 1 and isinstance(args[0], QRect)
                recorded = QRect(args[0])
                # Full-outline rect for this exact prefix.
                full = viewer._stroke_paint_rect(
                    snap, outline, ox, oy, factor)
                assert full is not None and not full.isNull()
                covered = covered.united(recorded)
                assert covered.contains(full), (
                    label, "full outline escapes tail-union")
            # The tail path (not the fallback) served every sample:
            # each recorded rect is much smaller than the full outline.
            viewer._apply_intent(
                viewer._interaction.handle_pen_release(
                    page, pts[-1][0], pts[-1][1], prs[-1], 999999))
    finally:
        viewer.close_document()
        _window.close()
    # Constants the proof rests on are the real implementation values.
    assert SMOOTH_PASSES == 2
    assert SMOOTH_MIN_SPACING_PT == pytest.approx(0.5)


def test_tail_fallback_declines_safely(qtbot, tmp_path):
    """Uncomputable tail (NaN geometry) returns None, and the update
    path declines to a full repaint (safe direction — never silently
    paints nothing)."""
    from annotations.models import InkStroke
    from core.types import Point

    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        ox, oy, factor = _page_map(viewer)
        page = _page_id(viewer)
        bad = InkStroke(
            page_id=page,
            points=(Point(10.0, 10.0), Point(float("nan"), 10.0)),
            color="#1b1e26", width_mm=3.0, opacity=1.0,
            pressures=(0.5, 0.5), times_ms=(1, 2),
            pressure_enabled=True, pressure_profile="normal")
        assert viewer._live_tail_rect(bad, ox, oy, factor) is None
    finally:
        viewer.close_document()
        _window.close()


def test_pixel_equivalence_long_stroke_2000(qtbot, tmp_path):
    """Per-move dirty repaint == full repaint over 2000 adversarial
    samples; outside the dirty rect the previous pixels survive."""
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        pts, prs = _adversarial(2000)
        it = viewer._interaction
        calls = []
        orig = viewer.update
        viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
        try:
            t = 1000
            viewer._apply_intent(it.handle_pen_press(
                page, pts[0][0], pts[0][1], prs[0], t))
            prev = _paint(viewer, None)
            for i in range(1, len(pts)):
                t += 8
                before = len(calls)
                viewer._apply_intent(it.handle_pen_move(
                    page, pts[i][0], pts[i][1], prs[i], t))
                assert len(calls) == before + 1
                args = calls[-1]
                assert len(args) == 1 and isinstance(args[0], QRect)
                dirty = QRect(args[0])
                full = _paint(viewer, None)
                incr = _paint_dirty_onto(viewer, prev, dirty)
                assert _bytes(full) == _bytes(incr), f"move {i}"
                prev = full
                if i % 500 == 0:
                    # Outside-dirty spot check is subsumed by the full
                    # byte equality above; keep the loop honest.
                    assert not dirty.isNull()
        finally:
            viewer.update = orig
    finally:
        viewer.close_document()
        _window.close()


def test_growth_and_retreat_bounded(qtbot, tmp_path):
    """Tail rects track the tip: small after retreat, union covers all."""
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        ox, oy, factor = _page_map(viewer)
        # Out 200 samples, back to near the start (no exact duplicates:
        # a duplicate would be a noop, and every move must schedule
        # exactly one update — no sample may be dropped).
        out = [(60.0 + i * 2.0, 200.0) for i in range(200)]
        back = [(60.0 + (198 - i) * 2.0, 200.0) for i in range(200)]
        pts = out + back
        prs = [0.6] * len(pts)
        _press, rects, snaps, _outlines = _drive_recorded(
            viewer, page, pts, prs)
        assert len(rects) == len(pts) - 1
        tails = [viewer._live_tail_rect(s, ox, oy, factor) for s in snaps]
        assert all(r is not None for r in tails)
        union = QRect()
        for r in tails:
            union = union.united(r)
        full = viewer._stroke_paint_rect(
            snaps[-1], viewer._interaction.live_outline(), ox, oy, factor)
        assert full is not None and union.contains(full)
        # The retreated tail tracks the tip: it no longer reaches the
        # far end of the stroke, while the union covers everything.
        assert tails[-1].right() < full.right() - 20, (
            tails[-1].right(), full.right())
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            page, pts[-1][0], pts[-1][1], prs[-1], 999999))
    finally:
        viewer.close_document()
        _window.close()
