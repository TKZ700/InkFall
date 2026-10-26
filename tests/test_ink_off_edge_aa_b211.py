"""M5-B.2.12: Pressure-OFF edge antialiasing.

Verifies the OFF terminal ops paint with AA locally (measurable
partial-coverage fringe), live==completed pixel identity is kept,
the ON branch still antialiases, and the hint never leaks into the
caller's painter state. Relative identities only — no hardcoded
pixel baselines.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _fringe_fraction(img):
    """Partial-coverage pixel share of ink pixels.

    Ink #1b1e26 -> fully covered r=27; background r=255; AA fringe
    lands strictly between. Widget background outside the page is
    near-black (r<10) and is excluded, not counted as ink. Zero
    means stair-step coverage (no AA).
    """
    w, h = img.width(), img.height()
    ink = gray = 0
    for y in range(h):
        line = bytes(img.scanLine(y))
        for x in range(w):
            r = line[4 * x + 2]
            if 10 <= r < 250:
                ink += 1
                if r > 40:
                    gray += 1
    assert ink > 0
    return gray / ink


def _stroke_fringe(viewer, img, stroke):
    """Fringe share measured inside the stroke's own paint rect.

    Whole-image measurement is meaningless: the widget background
    fill is near-black and would drown the stroke pixels. The live
    tail rect covers the redrawn region of the stroke.
    """
    rect = viewer._live_update_rect(stroke)
    assert rect is not None
    rect = rect.intersected(img.rect())
    assert not rect.isNull()
    return _fringe_fraction(img.copy(rect))


def _pen_tool(pressure, width_mm=3.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=width_mm, opacity=1.0, pressure=pressure)


def _open(qtbot, tmp_path, tool, name="c212.pdf"):
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

    def _paint(dirty):
        vw, vh = viewer.rect().width(), viewer.rect().height()
        img = QImage(vw, vh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#ffffff"))
        p = QPainter(img)
        try:
            viewer._paint_body(p, QRect(dirty) if dirty is not None else None)
        finally:
            p.end()
        return img

    for _ in range(30):
        _paint(None)
        if viewer.painted_placeholders == 0 and viewer.painted_tiles > 0:
            qtbot.wait(150)
            _paint(None)
            if viewer.painted_placeholders == 0:
                break
        qtbot.wait(150)
    return window, viewer, _paint


def _draw(viewer, pts, t0=1000):
    it = viewer._interaction
    page = viewer._pipeline.layout.entry(0).page_id
    viewer._apply_intent(it.handle_pen_press(page, pts[0][0], pts[0][1],
                                             0.6, t0))
    for i in range(1, len(pts)):
        viewer._apply_intent(it.handle_pen_move(page, pts[i][0], pts[i][1],
                                                0.6, t0 + i * 8))
    return page


def _diag(n=60):
    return [(60.0 + i * 3.0,
             140.0 + i * 1.35 + 0.4 * (((i * 2654435761) % 1024)
                                       / 1024.0 * 2 - 1))
            for i in range(n)]


def _curve(n=60):
    import math
    return [(60.0 + i * 3.0,
             200.0 + 40.0 * math.sin(i * 0.12)
             + 0.3 * (((i * 99991) % 512) / 512.0 * 2 - 1))
            for i in range(n)]


def test_off_diagonal_has_aa_fringe(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False))
    try:
        _draw(viewer, _diag())
        stroke = viewer._live_stroke
        assert stroke is not None
        assert _stroke_fringe(viewer, _paint(None), stroke) > 0.03
    finally:
        viewer.close_document()
        window.close()


def test_off_curve_thin_width_has_aa_fringe(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path,
                                   _pen_tool(False, width_mm=0.7),
                                   name="c212b.pdf")
    try:
        _draw(viewer, _curve())
        stroke = viewer._live_stroke
        assert stroke is not None
        assert _stroke_fringe(viewer, _paint(None), stroke) > 0.10
    finally:
        viewer.close_document()
        window.close()


def test_off_live_completed_identical_with_aa(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False),
                                   name="c212c.pdf")
    try:
        pts = _diag()
        page = _draw(viewer, pts)
        stroke = viewer._live_stroke
        assert stroke is not None
        rect = viewer._live_update_rect(stroke)
        live = _paint(None)
        assert _stroke_fringe(viewer, live, stroke) > 0.03
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            page, pts[-1][0], pts[-1][1], 0.6, 99999))
        done = _paint(None)
        assert live.bits().tobytes() == done.bits().tobytes()
    finally:
        viewer.close_document()
        window.close()


def test_on_still_antialiased_and_stable(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(True),
                                   name="c212d.pdf")
    try:
        pts = _diag()
        page = _draw(viewer, pts)
        stroke = viewer._live_stroke
        assert stroke is not None
        live = _paint(None)
        assert _stroke_fringe(viewer, live, stroke) > 0.03
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            page, pts[-1][0], pts[-1][1], 0.6, 99999))
        assert live.bits().tobytes() == _paint(None).bits().tobytes()
    finally:
        viewer.close_document()
        window.close()


def test_aa_does_not_leak_into_caller_painter(qtbot, tmp_path):
    from PySide6.QtGui import QPainter as _QP
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False),
                                   name="c212e.pdf")
    try:
        _draw(viewer, _diag())
        vw, vh = viewer.rect().width(), viewer.rect().height()
        img = QImage(vw, vh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#ffffff"))
        p = _QP(img)
        try:
            assert not p.testRenderHint(
                _QP.RenderHint.Antialiasing)
            viewer._paint_body(p, None)
            # Balanced save/restore: ambient hint unchanged on return.
            assert not p.testRenderHint(
                _QP.RenderHint.Antialiasing)
        finally:
            p.end()
    finally:
        viewer.close_document()
        window.close()
