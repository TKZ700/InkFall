"""B.3.5 live-update without per-move full-stroke copies (Qt-free + pixels).

down()/move() emit a per-stroke mutable LiveStroke view (O(1) per
accepted sample); up() still builds the single immutable InkStroke.
Covers: stable identity, exactly-once pairing, endpoint-once, repeat
idempotence, cancel/switch/close discard, memo invalidation + freshness
+ coalesced sharing, escape (weakref/cache keys), golden completed
values, structural construction counts, mid-stroke width sync.
"""

import weakref

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.interaction import InteractionController, LiveStroke, PenHandler
from annotations.models import InkStroke
from core.types import PageId, Point
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _pen(pressure=False):
    return ToolInstance(kind=ToolType.PEN, name="P", color="#1b1e26",
                        width_mm=0.7, opacity=0.8, pressure=pressure)


def test_view_identity_stable_per_stroke_and_fresh_per_down():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    v0 = pen.down(page, 0.0, 0.0)["stroke_update"]
    assert isinstance(v0, LiveStroke)
    assert not isinstance(v0, InkStroke)
    assert pen.move(page, 1.0, 0.0)["stroke_update"] is v0
    assert pen.move(page, 2.0, 0.0)["stroke_update"] is v0
    assert v0.seq == 3
    pen.up(page, 3.0, 0.0)
    v1 = pen.down(page, 9.0, 9.0)["stroke_update"]
    assert v1 is not v0
    assert v1.seq == 1


def test_view_mirrors_accepted_samples_exactly_once():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    pts = [(0.0, 0.0), (5.0, 1.0), (10.0, 0.0)]
    v = pen.down(page, *pts[0], 0.6, 100)["stroke_update"]
    pen.move(page, *pts[1], 0.7, 108)
    assert pen.move(page, *pts[1], 0.7, 108) == {"noop": True}  # dupe: no touch
    pen.move(page, *pts[2], 0.8, 116)
    assert v.seq == 3
    assert [(p.x, p.y) for p in v.points] == pts
    assert list(v.pressures) == [0.6, 0.7, 0.8]
    assert list(v.times_ms) == [100, 108, 116]


def test_endpoint_appended_exactly_once_on_release():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    v = pen.down(page, 0.0, 0.0, 0.6, 100)["stroke_update"]
    pen.move(page, 10.0, 0.0, 0.6, 108)
    done = pen.up(page, 20.0, 1.0, 0.5, 116)["stroke_done"]
    assert [(p.x, p.y) for p in v.points] == [(0.0, 0.0), (10.0, 0.0),
                                              (20.0, 1.0)]
    assert [(p.x, p.y) for p in done.points] == [(0.0, 0.0), (10.0, 0.0),
                                                 (20.0, 1.0)]
    assert tuple(done.pressures) == (0.6, 0.6, 0.5)


def test_release_without_endpoint_appends_nothing_and_repeats_noop():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    v = pen.down(page, 0.0, 0.0, 0.6, 100)["stroke_update"]
    pen.move(page, 10.0, 0.0, 0.6, 108)
    n = v.seq
    done = pen.up(page, 10.0, 0.0, 0.6, 116)["stroke_done"]
    assert v.seq == n
    assert len(done.points) == 2
    assert pen.up(page, 99.0, 99.0, 0.6, 124) == {"noop": True}
    assert pen._view is None


def test_cancel_and_switch_discard_view():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    v = pen.down(page, 0.0, 0.0, 0.6, 100)["stroke_update"]
    ref = weakref.ref(v)
    pen.move(page, 5.0, 0.0, 0.6, 108)
    assert pen.cancel() == {"stroke_cancelled": True}
    assert pen._view is None
    del v
    import gc
    gc.collect()
    assert ref() is None

    ctrl = InteractionController(_pen())
    cpage = PageId()
    ctrl.handle_pen_press(cpage, 0.0, 0.0, 0.6, 1000)
    ctrl.handle_pen_move(cpage, 4.0, 0.0, 0.6, 1008)
    ctrl.set_tool(ToolInstance(kind=ToolType.PAN, name="Pan"))
    assert ctrl._last_pen._view is None


def test_view_never_reaches_completed_state_or_caches():
    import gc

    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    try:
        pen = PenHandler(ToolType.PEN)
        page = PageId()
        v = pen.down(page, 0.0, 0.0, 0.6, 100)["stroke_update"]
        pen.move(page, 10.0, 0.0, 0.6, 108)
        ref = weakref.ref(v)
        # Live fallbacks route around the caches (isinstance early-out).
        assert viewer._cached_outline(v) is None  # pressure latched off
        assert viewer._off_smooth_cache == {}
        done = pen.up(page, 20.0, 0.0, 0.5, 116)["stroke_done"]
        assert isinstance(done, InkStroke)
        assert done.points is not v.points
        assert list(done.points) == list(v.points)
        viewer._cached_outline(done)  # only completed strokes store
        assert viewer._off_smooth_cache == {}
        del v, pen
        gc.collect()
        assert ref() is None
    finally:
        viewer.close()


def test_completed_goldens_match_pre_change_contract():
    seq = [(0.0, 0.0), (5.0, 1.0), (10.0, 0.0), (15.0, 2.0)]
    ts = [100, 108, 116, 124]
    for pressure, want_p in ((False, (1.0, 0.9, 0.9, 0.9, 0.5)),
                             (True, (0.2, 0.9, 0.9, 0.9, 0.5))):
        pen = PenHandler(ToolType.PEN)
        pen.configure(ToolInstance(kind=ToolType.PEN, name="P",
                                   color="#1b1e26", width_mm=0.7,
                                   opacity=0.8, pressure=pressure))
        page = PageId()
        pen.down(page, *seq[0], want_p[0], ts[0])
        for (x, y), t in zip(seq[1:], ts[1:]):
            pen.move(page, x, y, 0.9, t)
        s = pen.up(page, 16.0, 3.0, 0.5, 132)["stroke_done"]
        assert [(p.x, p.y) for p in s.points] == seq + [(16.0, 3.0)]
        assert tuple(s.pressures) == want_p
        assert tuple(s.times_ms) == (100, 108, 116, 124, 132)
        assert (s.color, s.width_mm, s.opacity,
                s.pressure_profile) == ("#1b1e26", 0.7, 0.8, "normal")
        assert s.pressure_enabled is pressure
    tap = PenHandler(ToolType.PEN)
    pg = PageId()
    tap.down(pg, 3.0, 4.0, 0.6, 50)
    t = tap.up(pg, 3.0, 4.0, 0.6, 58)["stroke_done"]
    assert [(p.x, p.y) for p in t.points] == [(3.0, 4.0)]
    assert tuple(t.pressures) == (0.6,) and tuple(t.times_ms) == (50,)


def test_structural_counts_one_snapshot_per_stroke():
    pen = PenHandler(ToolType.PEN)
    calls = []
    orig = pen._stroke

    def counting():
        calls.append(1)
        return orig()

    pen._stroke = counting
    page = PageId()
    pen.down(page, 0.0, 0.0, 0.6, 1000)
    for i in range(1, 200):
        pen.move(page, float(i), 0.0, 0.6, 1000 + i * 8)
    assert len(calls) == 0  # no full snapshot on any move
    pen.up(page, 200.0, 0.0, 0.6, 3000)
    assert len(calls) == 1  # exactly one, on release


def test_midstroke_width_sync_preserves_latched_fields():
    pen = PenHandler(ToolType.PEN)
    pen.configure(_pen(pressure=True))
    page = PageId()
    v = pen.down(page, 0.0, 0.0, 0.6, 100)["stroke_update"]
    assert v.width_mm == 0.7 and v.pressure_enabled is True
    pen.configure(ToolInstance(kind=ToolType.PEN, name="P", color="#ff0000",
                               width_mm=1.5, opacity=0.5, pressure=False))
    assert (v.color, v.width_mm, v.opacity) == ("#ff0000", 1.5, 0.5)
    assert v.pressure_enabled is True  # latch untouched
    assert v.pressure_profile == "normal"
    done = pen.up(page, 5.0, 0.0, 0.6, 108)["stroke_done"]
    assert done.width_mm == 1.5 and done.pressure_enabled is True


# ------------------------------------------------ viewer memo + pixels

def _make_viewer(qtbot):
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    return viewer


def _paint(viewer, img, entry, dirty):
    from types import SimpleNamespace

    from PySide6.QtGui import QPainter
    p = QPainter(img)
    try:
        p.fillRect(dirty, QColor("#ffffff"))
        viewer._paint_page_strokes(p, SimpleNamespace(raster_scale=1.0),
                                   entry,
                                   SimpleNamespace(x=50.0, y=40.0,
                                                   width=400.0,
                                                   height=500.0),
                                   1.0, dirty)
    finally:
        p.end()


def test_apply_invalidates_smooth_memo_per_update(qtbot):
    from types import SimpleNamespace
    viewer = _make_viewer(qtbot)
    try:
        viewer._interaction.set_tool(_pen())
        page = PageId()
        it = viewer._interaction
        viewer._apply_intent(it.handle_pen_press(page, 60.0, 60.0, 1.0,
                                                 1000))
        viewer._live_smooth_memo = (viewer._live_stroke, ((1.0, 1.0),))
        viewer._apply_intent(it.handle_pen_move(page, 70.0, 60.0, 1.0,
                                                1008))
        assert viewer._live_smooth_memo is None
        assert viewer._live_stroke is it._last_pen._view
    finally:
        viewer.close()


def test_live_paint_fresh_across_updates_shared_within_one(qtbot):
    from types import SimpleNamespace
    viewer = _make_viewer(qtbot)
    try:
        viewer._interaction.set_tool(_pen())
        page = PageId()
        it = viewer._interaction
        entry = SimpleNamespace(page_id=page)
        pulls = []
        orig = it.live_centerline

        def counting():
            r = orig()
            pulls.append(1 if r is None else len(r))
            return r

        it.live_centerline = counting
        W, H = 500, 600
        viewer._apply_intent(it.handle_pen_press(page, 60.0, 60.0, 1.0,
                                                 1000))
        viewer._apply_intent(it.handle_pen_move(page, 90.0, 60.0, 1.0,
                                                1008))
        img1 = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        img1.fill(QColor("#ffffff"))
        full = QRect(0, 0, W, H)
        _paint(viewer, img1, entry, full)
        _paint(viewer, img1, entry, full)  # second paint, same update
        assert len(pulls) == 1  # coalesced: one pull, memo shared
        first = img1.bits().tobytes()
        viewer._apply_intent(it.handle_pen_move(page, 120.0, 60.0, 1.0,
                                                1016))
        img2 = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        img2.fill(QColor("#ffffff"))
        _paint(viewer, img2, entry, full)
        assert len(pulls) == 2  # fresh pull after the new update
        assert img2.bits().tobytes() != first  # new tail actually painted
        tail = QRect(140, 80, 120, 60)
        assert (img1.copy(tail).bits().tobytes()
                != img2.copy(tail).bits().tobytes())
    finally:
        viewer.close()
