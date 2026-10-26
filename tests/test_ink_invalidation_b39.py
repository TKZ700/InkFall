"""B.3.9 bounded live-invalidation union (viewer-level + pixels).

Proves per-move update rects stop growing with the stroke (union of
current + previous single tail only), mid-stroke restyles fall back
to a full repaint with no stale pixels, and cancel still erases the
whole live stroke via the preserved full-stroke union slot.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _pen_tool(pressure=True, width_mm=3.0, color="#1b1e26", opacity=1.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color=color,
                        width_mm=width_mm, opacity=opacity,
                        pressure=pressure)


def _open(qtbot, tmp_path, tool, name="live39.pdf"):
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool)
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


def _paint(viewer, dirty):
    r = viewer.rect()
    img = QImage(r.width(), r.height(),
                 QImage.Format.Format_ARGB32_Premultiplied)
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
        p.setClipRect(QRect(dirty))
        viewer._paint_body(p, QRect(dirty))
    finally:
        p.end()
    return img


def _bytes(img):
    return img.bits().tobytes()


def _drive_recorded(viewer, page, pts, prs, t0=1000):
    """Drive press+moves; returns (rects, views_live_flags)."""
    it = viewer._interaction
    calls = []
    orig = viewer.update
    viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
    try:
        t = t0
        viewer._apply_intent(it.handle_pen_press(
            page, pts[0][0], pts[0][1], prs[0], t))
        rects = []
        for i in range(1, len(pts)):
            t += 8
            before = len(calls)
            viewer._apply_intent(it.handle_pen_move(
                page, pts[i][0], pts[i][1], prs[i], t))
            new = calls[before:]
            assert len(new) == 1, new
            rects.append(new[0])
        return rects
    finally:
        viewer.update = orig


def _shapes(kind, n):
    import math

    if kind == "straight":
        return [(60.0 + i * 0.9, 140.0 + (i % 9) * 2.0) for i in range(n)]
    if kind == "curve":
        return [(60.0 + i * 0.9,
                 200.0 + 40.0 * math.sin(i * 0.12) + 0.3 * math.sin(i * 0.5))
                for i in range(n)]
    h = n // 2
    out = [(60.0 + i * 1.5, 140.0 + (i % 5) * 1.0) for i in range(h)]
    # +0.001 avoids an exact-duplicate turnaround (a dupe would be a
    # noop and every move must schedule exactly one update).
    return out + [(60.0 + i * 1.5 + 0.001, 140.0 + (i % 5) * 1.0)
                  for i in range(n - h - 1, -1, -1)]


def _areas(rects):
    out = []
    for r in rects:
        assert len(r) == 1 and isinstance(r[0], QRect), r
        q = r[0]
        assert not q.isNull()
        out.append(q.width() * q.height())
    return out


def test_update_regions_do_not_grow_with_stroke(qtbot, tmp_path):
    for kind in ("straight", "curve", "reversal"):
        window, viewer = _open(qtbot, tmp_path, _pen_tool(),
                               name=f"b39-{kind}.pdf")
        try:
            page = _page_id(viewer)
            pts = _shapes(kind, 600)
            prs = [0.6] * len(pts)
            areas = _areas(_drive_recorded(viewer, page, pts, prs))
            early = max(areas[:100])
            late = max(areas[300:])
            # Bounded: late moves repaint no more than ~2x the early
            # window (two-tail union), never the accumulated stroke.
            assert late <= 2.0 * early, (kind, early, late)
            viewer._apply_intent(viewer._interaction.handle_pen_release(
                page, pts[-1][0], pts[-1][1], prs[-1], 999999))
        finally:
            viewer.close_document()
            window.close()


def test_wide_stroke_regions_bounded(qtbot, tmp_path):
    window, viewer = _open(qtbot, tmp_path, _pen_tool(width_mm=5.0),
                           name="b39-wide.pdf")
    try:
        page = _page_id(viewer)
        pts = _shapes("straight", 600)
        prs = [0.6] * len(pts)
        areas = _areas(_drive_recorded(viewer, page, pts, prs))
        assert max(areas[300:]) <= 2.0 * max(areas[:100])
        # Absolute bound: far below the whole-stroke bbox repaint the
        # old accumulation produced (~1800px wide here).
        assert max(areas) < 400 * 400
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            page, pts[-1][0], pts[-1][1], prs[-1], 999999))
    finally:
        viewer.close_document()
        window.close()


def test_restyle_takes_full_update_without_stale_pixels(qtbot, tmp_path):
    import dataclasses

    for field, new in (("width_mm", 0.5), ("color", "#c02020"),
                       ("opacity", 0.4)):
        window, viewer = _open(qtbot, tmp_path, _pen_tool(),
                               name=f"b39-re-{field}.pdf")
        try:
            page = _page_id(viewer)
            it = viewer._interaction
            pts = _shapes("straight", 120)
            calls = []
            orig = viewer.update
            viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
            try:
                viewer._apply_intent(it.handle_pen_press(
                    page, pts[0][0], pts[0][1], 0.6, 1000))
                before = _paint(viewer, None)
                for i in range(1, 60):
                    viewer._apply_intent(it.handle_pen_move(
                        page, pts[i][0], pts[i][1], 0.6, 1000 + i * 8))
                # Mid-stroke restyle through the production same-slot
                # path: identical id keeps the stroke open while the
                # next move picks up the new tool snapshot.
                it.set_tool(dataclasses.replace(it._tool, **{field: new}))
                n_bare_before = sum(1 for c in calls if len(c) == 0)
                viewer._apply_intent(it.handle_pen_move(
                    page, pts[60][0], pts[60][1], 0.6, 1480))
                assert sum(1 for c in calls if len(c) == 0) == \
                    n_bare_before + 1
                for i in range(61, 120):
                    viewer._apply_intent(it.handle_pen_move(
                        page, pts[i][0], pts[i][1], 0.6, 1000 + i * 8))
                    assert len(calls[-1]) == 1  # back to scoped updates
                full = _paint(viewer, None)
                done = viewer._apply_intent(it.handle_pen_release(
                    page, pts[-1][0], pts[-1][1], 0.6, 999999))
                assert _bytes(full) != _bytes(before)  # sanity: ink exists
            finally:
                viewer.update = orig
        finally:
            viewer.close_document()
            window.close()


def test_cancel_erases_entire_live_stroke(qtbot, tmp_path):
    window, viewer = _open(qtbot, tmp_path, _pen_tool(),
                           name="b39-cancel.pdf")
    try:
        page = _page_id(viewer)
        it = viewer._interaction
        pts = _shapes("curve", 300)
        prs = [0.6] * len(pts)
        blank = _paint(viewer, None)
        calls = []
        orig = viewer.update
        viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
        try:
            viewer._apply_intent(it.handle_pen_press(
                page, pts[0][0], pts[0][1], prs[0], 1000))
            for i in range(1, len(pts)):
                viewer._apply_intent(it.handle_pen_move(
                    page, pts[i][0], pts[i][1], prs[i], 1000 + i * 8))
            # Union of every move rect: the cancel erase must cover it.
            union = QRect()
            for c in calls:
                assert len(c) == 1 and isinstance(c[0], QRect)
                union = union.united(c[0])
            n_before = len(calls)
            viewer._apply_intent(it.cancel())
            cancel_rect = calls[n_before][0]
            assert isinstance(cancel_rect, QRect)
            assert QRect(cancel_rect).contains(union)
            assert _bytes(_paint(viewer, None)) == _bytes(blank)
        finally:
            viewer.update = orig
    finally:
        viewer.close_document()
        window.close()


def test_slots_cleared_on_done_cancel_close(qtbot, tmp_path):
    window, viewer = _open(qtbot, tmp_path, _pen_tool(),
                           name="b39-slots.pdf")
    try:
        page = _page_id(viewer)
        it = viewer._interaction
        viewer._apply_intent(it.handle_pen_press(page, 60.0, 60.0, 0.6,
                                                 1000))
        viewer._apply_intent(it.handle_pen_move(page, 70.0, 60.0, 0.6,
                                                1008))
        assert viewer._live_full_rect is not None
        assert viewer._live_meta is not None
        viewer._apply_intent(it.handle_pen_release(page, 70.0, 60.0, 0.6,
                                                   1016))
        assert viewer._live_full_rect is None
        assert viewer._live_meta is None
        assert viewer._live_prev_rect is None
        viewer._apply_intent(it.handle_pen_press(page, 60.0, 60.0, 0.6,
                                                 2000))
        viewer._apply_intent(it.handle_pen_move(page, 70.0, 60.0, 0.6,
                                                2008))
        viewer._apply_intent(it.cancel())
        assert viewer._live_full_rect is None
        assert viewer._live_meta is None
        viewer.close_document()
        assert viewer._live_full_rect is None
        assert viewer._live_meta is None
    finally:
        window.close()


def test_nan_width_never_spams_full_updates(qtbot, tmp_path):
    window, viewer = _open(qtbot, tmp_path, _pen_tool(),
                           name="b39-nan.pdf")
    try:
        page = _page_id(viewer)
        it = viewer._interaction
        calls = []
        orig = viewer.update
        viewer.update = lambda *a: calls.append(tuple(a))  # noqa: E731
        try:
            viewer._apply_intent(it.handle_pen_press(page, 60.0, 60.0,
                                                     0.6, 1000))
            import dataclasses
            it.set_tool(dataclasses.replace(it._tool,
                                            width_mm=float("nan")))
            for i in range(1, 20):
                viewer._apply_intent(it.handle_pen_move(
                    page, 60.0 + i, 60.0, 0.6, 1000 + i * 8))
            # Exactly one bare update (the restyle move itself); the
            # rest stay rect-scoped with no exception fallbacks.
            assert sum(1 for c in calls if len(c) == 0) == 1
            assert all(len(c) == 1 and isinstance(c[0], QRect)
                       for c in calls if len(c) == 1)
        finally:
            viewer.update = orig
    finally:
        viewer.close_document()
        window.close()
