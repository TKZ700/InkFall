"""M5-B.2.10: exact incremental Pressure-OFF smoothing.

Proves ``LiveCenterline`` reproduces ``smooth_centerline(raw, None)``
exactly at every prefix (the real gate — exact equality, never
tolerances), plus feed-count guards, fallback behavior, lifecycle
(cancel/tool-switch/completion/close), ON/OFF centerline position
identity, and viewer cache behavior.
"""

from __future__ import annotations

import math
import random

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.ink_geometry import (
    LiveCenterline,
    smooth_centerline,
)
from annotations.interaction import InteractionController, PenHandler
from core.types import PageId
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _oracle(raw):
    spts, svals = smooth_centerline(raw, None)
    assert svals is None
    return tuple(spts)


def _jitter(n, dx=3.0, seed=2654435761):
    return [(i * dx, 0.4 * (((i * seed) % 1024) / 1024.0 * 2 - 1))
            for i in range(n)]


def _check_prefixes(raw):
    live = LiveCenterline()
    for k in range(1, len(raw) + 1):
        live.append(*raw[k - 1])
        assert live.smoothed() == _oracle(raw[:k]), f"k={k}"
    return live


def test_prefix_exact_jitter_300():
    live = _check_prefixes(_jitter(300))
    assert live.fallbacks == 0


def test_prefix_exact_jitter_1000():
    live = _check_prefixes(_jitter(1000))
    assert live.fallbacks == 0


def test_prefix_exact_jitter_2000():
    live = _check_prefixes(_jitter(2000))
    assert live.fallbacks == 0


def test_prefix_exact_dense_sub_spacing():
    live = _check_prefixes(_jitter(120, dx=0.2, seed=2654435761 + 77))
    assert live.fallbacks == 0


def test_prefix_exact_straight_curve_sparse():
    straight = [(float(i), 0.0) for i in range(50)]
    assert _check_prefixes(straight).fallbacks == 0
    curve = [(10.0 * math.cos(t), 10.0 * math.sin(t))
             for t in (i * 0.15 for i in range(60))]
    assert _check_prefixes(curve).fallbacks == 0
    sparse = [(0.0, 0.0), (0.0, 0.0), (0.05, 0.0), (5.0, 0.0),
              (5.2, 0.1), (30.0, 0.0)]
    _check_prefixes(sparse)


def test_prefix_exact_duplicates_corners_reversals():
    _check_prefixes([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
    _check_prefixes([(0.0, 0.0), (10.0, 0.0), (1.0, 0.5)])
    retrace = ([(float(i), 0.0) for i in range(10)]
               + [(float(9 - i), 0.1) for i in range(10)])
    _check_prefixes(retrace)


def test_prefix_exact_random_walk():
    rng = random.Random(7)
    pts = []
    x = y = 0.0
    for _ in range(150):
        x += rng.uniform(-4.0, 6.0)
        y += rng.uniform(-4.0, 4.0)
        pts.append((x, y))
    live = _check_prefixes(pts)
    assert live.fallbacks == 0


def test_bypass_and_endpoints():
    live = LiveCenterline()
    assert live.smoothed() == ()
    live.append(3.0, 4.0)
    assert live.smoothed() == ((3.0, 4.0),)
    assert len(live) == 1
    live.append(3.1, 4.0)
    assert live.smoothed() == ((3.0, 4.0), (3.1, 4.0))
    # Sub-spacing total length bypasses smoothing unchanged.
    tiny = LiveCenterline()
    for p in ((0.0, 0.0), (0.2, 0.0), (0.4, 0.0)):
        tiny.append(*p)
    assert tiny.smoothed() == ((0.0, 0.0), (0.2, 0.0), (0.4, 0.0))
    # Pinned endpoints + final-sample retention on long strokes.
    raw = _jitter(300)
    live = _check_prefixes(raw)
    out = live.smoothed()
    assert out[0] == pytest.approx(raw[0])
    assert out[-1] == pytest.approx(raw[-1])


def test_repeated_retrieval_shares_object():
    live = LiveCenterline()
    for p in _jitter(60):
        live.append(*p)
    first = live.smoothed()
    assert live.smoothed() is first


def test_bad_input_raises_and_resets():
    live = LiveCenterline()
    with pytest.raises(ValueError):
        live.append(float("nan"), 0.0)
    with pytest.raises(ValueError):
        live.append("x", 0.0)  # type: ignore[arg-type]


def test_splice_miss_falls_back_exact():
    raw = _jitter(120)
    live = LiveCenterline()
    for p in raw[:100]:
        live.append(*p)
    # Corrupt the splice state past the 64-sample counting floor: the
    # next append must miss and take the exact full derive — output
    # still == oracle.
    live._S = [(999.0, 999.0)] * len(live._S)
    n_fallbacks = live.fallbacks
    for p in raw[100:]:
        live.append(*p)
        assert live.smoothed() == _oracle(raw[:len(live)])
    assert live.fallbacks > n_fallbacks


def test_handler_feeds_every_prefix_off_and_on():
    for pressure in (False, True):
        tool = ToolInstance(kind=ToolType.PEN, name="P", color="#1b1e26",
                            width_mm=3.0, opacity=1.0, pressure=pressure)
        pen = PenHandler(ToolType.PEN)
        pen.configure(tool)
        page = PageId()
        raw = _jitter(120)
        pen.down(page, raw[0][0], raw[0][1], 0.5, 0)
        assert pen.live_centerline() == _oracle(raw[:1])
        for i in range(1, 120):
            pen.move(page, raw[i][0], raw[i][1], 0.5, i * 8)
            assert pen.live_centerline() == _oracle(raw[:i + 1]), (
                f"pressure={pressure} i={i}")
        assert pen._center.fallbacks == 0
        done = pen.up(page, raw[-1][0] + 3.0, raw[-1][1], 0.5, 99999)
        assert pen.live_centerline() is None
        assert "stroke_done" in done


def test_handler_cancel_and_restart():
    pen = PenHandler(ToolType.PEN)
    pen.configure(ToolInstance(kind=ToolType.PEN, name="P",
                               color="#1b1e26", width_mm=3.0, opacity=1.0,
                               pressure=False))
    page = PageId()
    raw = _jitter(40)
    pen.down(page, raw[0][0], raw[0][1], 0.5, 0)
    for i in range(1, 40):
        pen.move(page, raw[i][0], raw[i][1], 0.5, i * 8)
    assert pen.live_centerline() is not None
    pen.cancel()
    assert pen.live_centerline() is None
    assert len(pen._center) == 0
    pen.down(page, 500.0, 500.0, 0.5, 9999)
    assert pen.live_centerline() == _oracle([(500.0, 500.0)])


def test_controller_tool_switch_cancels_centerline():
    it = InteractionController()
    tool_a = ToolInstance(kind=ToolType.PEN, name="A", color="#1b1e26",
                          width_mm=3.0, opacity=1.0, pressure=False)
    tool_b = ToolInstance(kind=ToolType.PEN, name="B", color="#ff0000",
                          width_mm=3.0, opacity=1.0, pressure=False)
    it.set_tool(tool_a)
    page = PageId()
    it.handle_pen_press(page, 10.0, 10.0, 0.5, 0)
    it.handle_pen_move(page, 13.0, 10.5, 0.5, 8)
    assert it.live_centerline() is not None
    it.set_tool(tool_b)
    assert it.live_centerline() is None


def test_on_off_centerline_positions_identical():
    raw = _jitter(80)
    widths = [1.0 + 0.05 * (i % 7) for i in range(80)]
    s_w, _ = smooth_centerline(raw, widths)
    s_plain, _ = smooth_centerline(raw, None)
    assert tuple(s_w) == tuple(s_plain)


def _pen_tool(pressure):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=3.0, opacity=1.0, pressure=pressure)


def _open(qtbot, tmp_path, tool, name="c210.pdf"):
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
    vw, vh = viewer.rect().width(), viewer.rect().height()

    def _paint(dirty):
        img = QImage(vw, vh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#000000"))
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


def test_viewer_off_live_uses_holder_and_completed_cache(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False))
    try:
        page = viewer._pipeline.layout.entry(0).page_id
        it = viewer._interaction
        raw = [(60.0 + i * 0.9, 140.0 + (i % 9) * 2.0) for i in range(120)]
        viewer._apply_intent(it.handle_pen_press(page, raw[0][0], raw[0][1],
                                                 0.6, 1000))
        for i in range(1, 120):
            viewer._apply_intent(it.handle_pen_move(page, raw[i][0],
                                                    raw[i][1], 0.6,
                                                    1000 + i * 8))
            # Holder serves the live stroke exactly, every version.
            assert it.live_centerline() == _oracle(raw[:i + 1]), f"i={i}"
        assert it._last_pen._center.fallbacks == 0
        live_full = _paint(None)
        assert viewer._live_smooth_memo is not None
        viewer._apply_intent(it.handle_pen_release(page, raw[-1][0],
                                                   raw[-1][1], 0.6, 99999))
        # Completed repaint populates the per-stroke cache; the next
        # repaint hits it and matches the live pixels.
        done_first = _paint(None)
        assert viewer.off_smooth_cache_misses == 1
        done_second = _paint(None)
        assert viewer.off_smooth_cache_hits == 1
        assert (done_first.bits().tobytes()
                == done_second.bits().tobytes())
        assert (live_full.bits().tobytes()
                == done_first.bits().tobytes())
        assert viewer._live_smooth_memo is None
        # Close purges the completed cache (no dead-stroke pinning).
        viewer.close_document()
        assert viewer._off_smooth_cache == {}
    finally:
        try:
            viewer.close_document()
        except Exception:
            pass
        window.close()


def test_viewer_off_completed_cache_survives_tool_switch(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False),
                                   name="c210b.pdf")
    try:
        page = viewer._pipeline.layout.entry(0).page_id
        it = viewer._interaction
        viewer._apply_intent(it.handle_pen_press(page, 60.0, 140.0, 0.6,
                                                 1000))
        viewer._apply_intent(it.handle_pen_move(page, 63.0, 142.0, 0.6,
                                                1008))
        viewer._apply_intent(it.handle_pen_release(page, 63.0, 142.0, 0.6,
                                                   1016))
        before = _paint(None)
        assert viewer.off_smooth_cache_misses == 1
        # Switching tools cancels nothing completed; the frozen stroke
        # keeps its exact cached centerline.
        it.set_tool(_pen_tool(True))
        after = _paint(None)
        assert viewer.off_smooth_cache_hits >= 1
        assert before.bits().tobytes() == after.bits().tobytes()
    finally:
        try:
            viewer.close_document()
        except Exception:
            pass
        window.close()
