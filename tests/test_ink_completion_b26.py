"""M5-B.2.6 Step 2: scoped stroke-completion repaint.

Proves replacing the completion full-widget repaint with
union(completed bounds, last live rect) is pixel-identical: opaque and
translucent strokes, overlap, no-movement release, cancel/restart,
page/viewport boundaries, scroll/zoom after completion, and sequence
equivalence.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter
from tests.helpers import committed_strokes
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _dims(viewer):
    # The viewer never fills the window (MainWindow chrome); images
    # must match the viewer rect exactly or full-vs-dirty paints speak
    # about different regions.
    r = viewer.rect()
    return r.width(), r.height()


def _pen_tool(pressure=True, width_mm=3.0, opacity=1.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=width_mm, opacity=opacity,
                        pressure=pressure)


def _open(qtbot, tmp_path, tool, name="complete26.pdf"):
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
    # Settle the tile pipeline: pixel comparisons need a frozen tile
    # state (no placeholders flipping to images mid-test). Bounded
    # best-effort wait; the synchronous test body runs no event loop,
    # so whatever state holds here stays frozen during comparisons.
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    for _ in range(30):
        _full(viewer)
        if viewer.painted_placeholders == 0 and viewer.painted_tiles > 0:
            qtbot.wait(150)
            _full(viewer)
            if viewer.painted_placeholders == 0:
                break
        qtbot.wait(150)
    return window, viewer


def _page_id(viewer):
    return viewer._pipeline.layout.entry(0).page_id


def _full(viewer):
    w, h = _dims(viewer)
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#000000"))
    p = QPainter(img)
    try:
        viewer._paint_body(p, None)
    finally:
        p.end()
    return img


def _incr(viewer, base, rect):
    img = base.copy()
    p = QPainter(img)
    try:
        # Production paintEvents arrive pre-clipped to the update
        # region by Qt; replicate that clip or the unconditional page
        # fill would erase strokes outside the dirty rect (a harness
        # artifact, not paint behavior).
        p.setClipRect(QRect(rect))
        viewer._paint_body(p, QRect(rect))
    finally:
        p.end()
    return img


def _bytes(img):
    return img.bits().tobytes()


class _Recorder:
    """Record update() calls through one drive (moves + release)."""

    def __init__(self, viewer):
        self.viewer = viewer
        self.calls = []
        self.orig = viewer.update
        viewer.update = lambda *a: self.calls.append(  # noqa: E731
            tuple(a))

    def close(self):
        self.viewer.update = self.orig

    def drive(self, page, pts, prs, t0=1000):
        it = self.viewer._interaction
        t = t0
        self.viewer._apply_intent(it.handle_pen_press(
            page, pts[0][0], pts[0][1], prs[0], t))
        moves = []
        for i in range(1, len(pts)):
            t += 8
            before = len(self.calls)
            self.viewer._apply_intent(it.handle_pen_move(
                page, pts[i][0], pts[i][1], prs[i], t))
            assert len(self.calls) == before + 1
            assert len(self.calls[-1]) == 1
            moves.append(QRect(self.calls[-1][0]))
        return moves

    def release(self, page, x, y, p, t):
        before = len(self.calls)
        self.viewer._apply_intent(
            self.viewer._interaction.handle_pen_release(page, x, y, p, t))
        assert len(self.calls) == before + 1
        assert len(self.calls[-1]) == 1
        return QRect(self.calls[-1][0])

    def cancel(self):
        before = len(self.calls)
        self.viewer._apply_intent(self.viewer._interaction.cancel())
        assert len(self.calls) == before + 1
        return self.calls[-1]


def _samples(x0, y0, n, dx=1.5, wobble=2.0):
    pts = [(x0 + i * dx, y0 + (i % 9) * wobble) for i in range(n)]
    prs = [min(1.0, max(0.05, 0.5 + 0.3 * ((i % 5) - 2) / 2.0))
           for i in range(n)]
    return pts, prs


def test_completion_scoped_not_full(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        pts, prs = _samples(60.0, 120.0, 60)
        rec = _Recorder(viewer)
        try:
            moves = rec.drive(page, pts, prs)
            assert all(not r.isNull() for r in moves)
            done_rect = rec.release(page, pts[-1][0], pts[-1][1],
                                    prs[-1], 9999)
        finally:
            rec.close()
        assert not done_rect.isNull()
        vw, vh = _dims(viewer)
        assert done_rect.width() < vw and done_rect.height() < vh
        assert viewer._live_prev_rect is None  # state reset
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()
        _window.close()


def test_live_to_completed_identity_opaque(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        pts, prs = _samples(60.0, 120.0, 80)
        rec = _Recorder(viewer)
        try:
            rec.drive(page, pts, prs)
            live_full = _full(viewer)
            done_rect = rec.release(page, pts[-1][0], pts[-1][1],
                                    prs[-1], 9999)
        finally:
            rec.close()
        done_full = _full(viewer)
        # The completed stroke is pixel-identical to the live preview.
        assert _bytes(live_full) == _bytes(done_full)
        # The scoped completion repaint reproduces the full repaint.
        assert _bytes(_incr(viewer, live_full, done_rect)) == \
            _bytes(done_full)
    finally:
        viewer.close_document()
        _window.close()


def test_live_to_completed_identity_translucent_overlap(
        qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path,
                            _pen_tool(opacity=0.4), "tr26.pdf")
    try:
        page = _page_id(viewer)
        rec = _Recorder(viewer)
        try:
            # Two pre-existing strokes the new one overlaps.
            for k in range(2):
                pts, prs = _samples(80.0, 140.0 + k * 30.0, 50)
                rec.drive(page, pts, prs)
                rec.release(page, pts[-1][0], pts[-1][1], prs[-1],
                            10000 + k * 1000)
            pts, prs = _samples(60.0, 130.0, 80)
            rec.drive(page, pts, prs)
            live_full = _full(viewer)
            done_rect = rec.release(page, pts[-1][0], pts[-1][1],
                                    prs[-1], 99999)
        finally:
            rec.close()
        done_full = _full(viewer)
        assert _bytes(live_full) == _bytes(done_full)
        assert _bytes(_incr(viewer, live_full, done_rect)) == \
            _bytes(done_full)
        assert len(committed_strokes(viewer)) == 3
    finally:
        viewer.close_document()
        _window.close()


def test_release_no_movement(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        it = viewer._interaction
        rec = _Recorder(viewer)
        try:
            viewer._apply_intent(it.handle_pen_press(page, 200.0, 300.0,
                                                     0.7, 1000))
            live_full = _full(viewer)
            done_rect = rec.release(page, 200.0, 300.0, 0.7, 1016)
        finally:
            rec.close()
        done_full = _full(viewer)
        assert not done_rect.isNull()
        vw, vh = _dims(viewer)
        assert done_rect.width() < vw and done_rect.height() < vh
        assert _bytes(live_full) == _bytes(done_full)
        assert _bytes(_incr(viewer, live_full, done_rect)) == \
            _bytes(done_full)
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()
        _window.close()


def test_cancel_and_restart(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        pts, prs = _samples(60.0, 120.0, 30)
        rec = _Recorder(viewer)
        try:
            rec.drive(page, pts, prs)
            before = _full(viewer)
            cancelled = rec.cancel()
            # Cancel repaints the live region only (or full if none).
            assert len(cancelled) <= 1
            if cancelled:
                assert isinstance(cancelled[0], QRect)
                after = _full(viewer)
                assert _bytes(_incr(viewer, before,
                                    QRect(cancelled[0]))) == \
                    _bytes(after)
            assert viewer._live_stroke is None
            assert committed_strokes(viewer) == []
            # Restart works on clean state.
            pts2, prs2 = _samples(300.0, 400.0, 10)
            rec.drive(page, pts2, prs2)
            rec.release(page, pts2[-1][0], pts2[-1][1], prs2[-1],
                        99999)
        finally:
            rec.close()
        assert len(committed_strokes(viewer)) == 1
        # B.2.6 Step 3: event history is bounded to the live stroke.
        handler = viewer._interaction._pen_handler()
        assert handler.events == ["down"] + ["move"] * 9 + ["up"], \
            handler.events
    finally:
        viewer.close_document()
        _window.close()


def test_completion_near_boundaries(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        rec = _Recorder(viewer)
        try:
            # Page right/bottom edge (page is 600x800pt).
            pts, prs = _samples(560.0, 760.0, 25, dx=1.0, wobble=1.0)
            rec.drive(page, pts, prs)
            live_full = _full(viewer)
            r1 = rec.release(page, pts[-1][0], pts[-1][1], prs[-1],
                             20000)
            done_full = _full(viewer)
            assert _bytes(live_full) == _bytes(done_full)
            assert _bytes(_incr(viewer, live_full, r1)) == \
                _bytes(done_full)
            # Viewport top edge.
            pts2, prs2 = _samples(100.0, 4.0, 25)
            rec.drive(page, pts2, prs2)
            live_full2 = _full(viewer)
            r2 = rec.release(page, pts2[-1][0], pts2[-1][1], prs2[-1],
                             30000)
            done_full2 = _full(viewer)
            assert _bytes(live_full2) == _bytes(done_full2)
            assert _bytes(_incr(viewer, live_full2, r2)) == \
                _bytes(done_full2)
        finally:
            rec.close()
        assert len(committed_strokes(viewer)) == 2
    finally:
        viewer.close_document()
        _window.close()


def test_scroll_zoom_after_completion(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        pts, prs = _samples(60.0, 120.0, 60)
        rec = _Recorder(viewer)
        try:
            rec.drive(page, pts, prs)
            done_rect = rec.release(page, pts[-1][0], pts[-1][1],
                                    prs[-1], 9999)
        finally:
            rec.close()
        assert not done_rect.isNull()
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        qtbot.wait(150)
        assert len(committed_strokes(viewer)) == 1
        a = _bytes(_full(viewer))
        assert a == _bytes(_full(viewer))  # paint stability
        viewer.set_zoom(2.0)
        qtbot.wait(150)
        b = _bytes(_full(viewer))
        assert b == _bytes(_full(viewer))
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()
        _window.close()


def test_sequence_equivalence(qtbot, tmp_path):
    _window, viewer = _open(qtbot, tmp_path, _pen_tool())
    try:
        page = _page_id(viewer)
        rec = _Recorder(viewer)
        try:
            specs = [(60.0, 100.0, 40, 1.0), (200.0, 300.0, 70, 1.0),
                     (120.0, 420.0, 55, 0.35), (350.0, 150.0, 90, 0.6),
                     (80.0, 500.0, 30, 1.0), (300.0, 550.0, 45, 1.0)]
            t = 5000
            for i, (x0, y0, n, op) in enumerate(specs):
                viewer._interaction.set_tool(_pen_tool(opacity=op))
                pts, prs = _samples(x0, y0, n)
                rec.drive(page, pts, prs, t0=t)
                live_full = _full(viewer)
                r = rec.release(page, pts[-1][0], pts[-1][1], prs[-1],
                                t + n * 8 + 8)
                t += 100000
                assert not r.isNull()
                vw, vh = _dims(viewer)
                assert r.width() < vw and r.height() < vh, (i, r)
                done_full = _full(viewer)
                assert _bytes(live_full) == _bytes(done_full), i
                assert _bytes(_incr(viewer, live_full, r)) == \
                    _bytes(done_full), i
        finally:
            rec.close()
        assert len(committed_strokes(viewer)) == 6
        assert len(viewer._outline_cache) == 6
        assert _bytes(_full(viewer)) == _bytes(_full(viewer))
    finally:
        viewer.close_document()
        _window.close()
