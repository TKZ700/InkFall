"""M5-B.2.7 Part A: smoothing is independent of the Pressure switch.

Pressure controls width variation only; centerline smoothing
(``smooth_centerline``) applies equally with Pressure ON and OFF, and
live/completed geometry agree in both modes. Also pins the B.2.7
waste-skip: with Pressure OFF the incremental ``LiveOutline`` builder
is never fed (``live_outline()`` returns None regardless, so feeding
it only burns per-sample CPU).
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.ink_geometry import (
    build_outline,
    pressure_widths,
    smooth_centerline,
)
from annotations.interaction import InteractionController
from core.types import PageId
from tests.helpers import committed_strokes
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _tool(pressure):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=3.0, opacity=1.0, pressure=pressure)


def _jittered_diagonal(n=40):
    # Deterministic jitter around y=x: smoothing must displace samples.
    pts = []
    for i in range(n):
        jog = 0.9 if i % 2 == 0 else -0.9
        pts.append((float(i * 2.0), float(i * 2.0 + jog)))
    return pts


def _drive(controller, page, pts, pressures=None, t0=1000):
    lives = []
    intent = controller.handle_pen_press(page, pts[0][0], pts[0][1],
                                         pressures[0] if pressures else 1.0,
                                         t0)
    lives.append(intent["stroke_update"])
    for i in range(1, len(pts)):
        p = pressures[i] if pressures else 1.0
        intent = controller.handle_pen_move(page, pts[i][0], pts[i][1],
                                            p, t0 + i * 8)
        if "stroke_update" in intent:
            lives.append(intent["stroke_update"])
    last_p = pressures[-1] if pressures else 1.0
    done = controller.handle_pen_release(page, pts[-1][0], pts[-1][1],
                                         last_p, t0 + len(pts) * 8)
    return lives, done["stroke_done"]


def test_off_smoothing_displaces_jitter():
    raw = _jittered_diagonal()
    spts, svals = smooth_centerline(raw, None)
    assert svals is None
    assert len(spts) >= 3
    # Endpoints pinned.
    assert spts[0] == raw[0]
    assert spts[-1] == raw[-1]
    # Interior actually smoothed: max deviation from the raw diagonal
    # shrinks (raw jog is +-0.9).
    raw_dev = max(abs(y - x) for x, y in raw[1:-1])
    sm_dev = max(abs(y - x) for x, y in spts[1:-1])
    assert raw_dev == pytest.approx(0.9)
    assert sm_dev < raw_dev


def test_centerline_identical_with_and_without_widths():
    raw = _jittered_diagonal()
    base_pt = 3.0 * 72.0 / 25.4
    pressures = [0.1 + 0.8 * ((i * 7) % 10) / 9.0 for i in range(len(raw))]
    widths = pressure_widths(pressures, base_pt)
    s_plain, v_plain = smooth_centerline(raw, None)
    s_w, v_w = smooth_centerline(raw, widths)
    assert v_plain is None and v_w is not None
    assert tuple(s_plain) == tuple(s_w)
    assert len(v_w) == len(s_w)


def test_off_live_outline_none_and_flag_latched_off():
    controller = InteractionController(_tool(False))
    page = PageId()
    lives, done = _drive(controller, page, _jittered_diagonal(10))
    assert done.pressure_enabled is False
    assert all(s.pressure_enabled is False for s in lives)
    assert controller.live_outline() is None  # stroke closed: no samples
    # Mid-stroke is also None with pressure off.
    controller.handle_pen_press(page, 0.0, 0.0, 0.5, 5000)
    controller.handle_pen_move(page, 5.0, 5.0, 0.5, 5008)
    assert controller.live_outline() is None
    controller.handle_pen_release(page, 5.0, 5.0, 0.5, 5016)


def test_on_live_outline_present_and_widths_vary():
    controller = InteractionController(_tool(True))
    page = PageId()
    pts = _jittered_diagonal(10)
    prs = [0.1 + 0.8 * i / 9.0 for i in range(10)]
    lives, done = _drive(controller, page, pts, prs)
    assert done.pressure_enabled is True
    base_pt = 3.0 * 72.0 / 25.4
    widths = pressure_widths(tuple(done.pressures), base_pt)
    assert max(widths) > min(widths) * 1.5  # variable-width retained
    # Mid-stroke incremental outline exists with pressure on.
    controller.handle_pen_press(page, pts[0][0], pts[0][1], prs[0], 9000)
    for i in range(1, len(pts)):
        controller.handle_pen_move(page, pts[i][0], pts[i][1], prs[i],
                                   9000 + i * 8)
    outline = controller.live_outline()
    assert outline is not None
    assert outline.polygon or outline.dots or outline.discs
    controller.handle_pen_release(page, pts[-1][0], pts[-1][1], prs[-1],
                                  9999)


def test_live_completed_agree_both_modes():
    for pressure in (False, True):
        controller = InteractionController(_tool(pressure))
        page = PageId()
        pts = _jittered_diagonal(25)
        prs = [0.2 + 0.6 * ((i * 3) % 7) / 6.0 for i in range(25)]
        lives, done = _drive(controller, page, pts, prs)
        # Raw points preserved through live updates to completion.
        assert [(p.x, p.y) for p in done.points] == pts
        assert [(p.x, p.y) for p in lives[-1].points] == pts
        # Same render geometry from live and completed inputs.
        live_raw = [(p.x, p.y) for p in lives[-1].points]
        done_raw = [(p.x, p.y) for p in done.points]
        if pressure:
            base_pt = 3.0 * 72.0 / 25.4
            lw = pressure_widths(tuple(lives[-1].pressures), base_pt)
            dw = pressure_widths(tuple(done.pressures), base_pt)
            s_live, _ = smooth_centerline(live_raw, lw)
            s_done, _ = smooth_centerline(done_raw, dw)
        else:
            s_live, _ = smooth_centerline(live_raw, None)
            s_done, _ = smooth_centerline(done_raw, None)
        assert tuple(s_live) == tuple(s_done)


def test_edge_cases_both_modes():
    straight = [(float(i * 3.0), 0.0) for i in range(10)]
    curve = [(float(i * 2.0), 0.05 * float(i * i)) for i in range(12)]
    reversal = [(0.0, 0.0), (10.0, 0.0), (20.0, 1.0), (10.0, 0.0),
                (0.0, 0.0), (-5.0, 2.0)]
    dupes = [(0.0, 0.0), (0.0, 0.0), (5.0, 5.0), (5.0, 5.0),
             (10.0, 9.0), (15.0, 15.0)]
    for raw in (straight, curve, reversal, dupes):
        for widths in (None, tuple(2.0 for _ in raw)):
            spts, svals = smooth_centerline(raw, widths)
            assert len(spts) >= 1
            if widths is None:
                assert svals is None
            else:
                assert svals is not None and len(svals) == len(spts)
            # Straight stays on its line; everything stays finite.
            for x, y in spts:
                assert math.isfinite(x) and math.isfinite(y)
            outline = build_outline(
                spts, svals if svals is not None else
                tuple(2.0 for _ in spts))
            assert outline.polygon or outline.dots
    # Short strokes bypass smoothing unchanged (documented preconditions).
    for raw in ([(0.0, 0.0)], [(0.0, 0.0), (1.0, 1.0)],
                [(0.0, 0.0), (0.05, 0.0), (0.09, 0.0)]):
        spts, _ = smooth_centerline(raw, None)
        assert tuple(spts) == tuple(raw)


def test_sample_preservation_and_duplicate_collapse():
    for pressure in (False, True):
        controller = InteractionController(_tool(pressure))
        page = PageId()
        pts = [(60.0 + i * 1.5, 60.0 + (i % 9) * 2.0) for i in range(60)]
        lives, done = _drive(controller, page, pts)
        assert len(done.points) == 60
        assert len(done.pressures) == 60
        assert len(done.times_ms) == 60
        assert all(b > a for a, b in
                   zip(done.times_ms, done.times_ms[1:]))
        # Exact duplicate is an explicit noop, not a silent drop.
        controller.handle_pen_press(page, 0.0, 0.0, 0.5, 20000)
        n0 = len(controller._pen_handler()._points)
        intent = controller.handle_pen_move(page, 0.0, 0.0, 0.5, 20008)
        assert intent == {"noop": True}
        assert len(controller._pen_handler()._points) == n0
        controller.handle_pen_release(page, 1.0, 1.0, 0.5, 20016)


def test_off_skips_live_builder():
    off = InteractionController(_tool(False))
    page = PageId()
    pts = _jittered_diagonal(20)
    _drive(off, page, pts)
    # Stroke closed resets; check mid-stroke state instead.
    off.handle_pen_press(page, pts[0][0], pts[0][1], 0.5, 30000)
    for i in range(1, len(pts)):
        off.handle_pen_move(page, pts[i][0], pts[i][1], 0.5, 30000 + i * 8)
    assert len(off._pen_handler()._live) == 0
    assert off.live_outline() is None
    off.handle_pen_release(page, pts[-1][0], pts[-1][1], 0.5, 39999)

    on = InteractionController(_tool(True))
    on.handle_pen_press(page, pts[0][0], pts[0][1], 0.5, 40000)
    for i in range(1, len(pts)):
        on.handle_pen_move(page, pts[i][0], pts[i][1], 0.5, 40000 + i * 8)
    assert len(on._pen_handler()._live) == len(pts)
    assert on.live_outline() is not None
    on.handle_pen_release(page, pts[-1][0], pts[-1][1], 0.5, 49999)


def test_off_pressure_outline_none_on_present_and_pixel_identity(
        qtbot, tmp_path):
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / "off27.pdf"), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    try:
        viewer._interaction.set_tool(_tool(False))
        qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0,
                        timeout=20000)
        vr = viewer.rect()
        vw, vh = vr.width(), vr.height()

        def _full():
            img = QImage(vw, vh, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QColor("#000000"))
            p = QPainter(img)
            try:
                viewer._paint_body(p, None)
            finally:
                p.end()
            return img

        page = viewer._pipeline.layout.entry(0).page_id
        it = viewer._interaction
        calls = []
        viewer.update = lambda *a: calls.append(tuple(a))
        pts = [(80.0 + i * 1.5, 140.0 + (i % 9) * 2.0) for i in range(50)]
        viewer._apply_intent(it.handle_pen_press(page, pts[0][0], pts[0][1],
                                                 0.6, 1000))
        for i in range(1, 50):
            viewer._apply_intent(it.handle_pen_move(page, pts[i][0],
                                                    pts[i][1], 0.6,
                                                    1000 + i * 8))
        # Pressure-OFF completed outline takes the legacy path.
        pending = it._pen_handler()._stroke()
        assert viewer._pressure_outline(pending) is None
        live_full = _full()
        viewer._apply_intent(it.handle_pen_release(page, pts[-1][0],
                                                   pts[-1][1], 0.6, 2000))
        done_rect = calls[-1][0] if calls and calls[-1] else None
        done_full = _full()
        assert live_full.bits().tobytes() == done_full.bits().tobytes()
        assert done_rect is not None  # scoped completion, not full
        img = live_full.copy()
        p = QPainter(img)
        try:
            from PySide6.QtCore import QRect
            p.setClipRect(QRect(done_rect))
            viewer._paint_body(p, QRect(done_rect))
        finally:
            p.end()
        assert img.bits().tobytes() == done_full.bits().tobytes()
        assert len(committed_strokes(viewer)) == 1
        assert committed_strokes(viewer)[0].pressure_enabled is False
    finally:
        viewer.close_document()
        window.close()
