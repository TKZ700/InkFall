"""M6.7.2 Polygon live preview + Snap-to-15° gated connect/always-close.

Corrective slice on M6.7.1, extended by the M6.7.2 follow-up. While a
Polygon gesture is being constructed the preview shows:

* the confirmed outline edges (never a premature closing edge);
* exactly ONE provisional live segment from the last confirmed vertex to
  the current pointer (so the next edge follows the pointer);
* the configured fill as a separate closed contour (confirmed vertices +
  pointer) only when fill is enabled — a fill-disabled preview leaves the
  page fully visible;
* a constant-screen-size circle at each confirmed vertex only (never the
  moving pointer).

All preview elements are cleared on completion or cancellation, and the
committed polygon carries the confirmed vertices only (one closed shape,
one Add command). Coverage: Qt-free handler intents plus the real viewer
paint path (pixel assertions).
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QTabletEvent

from annotations.interaction import InteractionController, ShapeSnapContext
from annotations.models import Shape, ShapeKind
from core.types import PageId, Point, Rect, Size
from tests.helpers import make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------- helpers

def _shape_tool(kind, **kw):
    base = dict(kind=kind, name="Shape", color="#e01b24", width_mm=2.0,
                opacity=1.0)
    base.update(kw)
    return ToolInstance(**base)


@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        keys = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                "inkfall/m5/toolbar/active",
                "inkfall/m5/toolbar/custom_colors",
                "inkfall/m5/toolbar/presets")
        for key in keys:
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass


def _press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _move(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _dbl(viewer, x, y):
    viewer.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _open_window(qtbot, tmp_path, tool=None):
    from app.main_window import MainWindow

    pdf = make_probe_pdf(str(tmp_path / "m672poly.pdf"),
                         page_width=600.0, page_height=800.0)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    if tool is not None:
        viewer._interaction.set_tool(tool)
    qtbot.wait(120)
    return window, viewer


def _shapes(viewer, page_index=0):
    return [a for a in viewer._document.annotations.page(page_index)
            if isinstance(a, Shape)]


def _widget(viewer, lx, ly, page_index=0):
    from rendering.layout import DocumentLayout

    layout = viewer._pipeline.layout
    entry = layout.entry(page_index)
    scale = viewer._display_scale()
    vw_render, _vh = viewer._render_viewport()
    bucket = float(layout.raster_scale)
    x_off = DocumentLayout.page_x_origin(
        vw_render, float(layout.total_width_px),
        float(entry.render_width_px))
    wx = (x_off + float(lx) * bucket - viewer._scroll_doc_x()) * scale
    wy = (entry.y_offset_px + float(ly) * bucket
          - viewer._scroll_doc()) * scale
    return wx, wy


def _click(viewer, p, page_index=0):
    w = _widget(viewer, *p, page_index)
    _press(viewer, *w)
    _release(viewer, *w)
    return w


def _pixel(viewer, x, y):
    return viewer.grab().toImage().pixelColor(int(x), int(y))


def _is_red(color) -> bool:
    return (color.red() > 140 and color.green() < 150 and color.blue() < 150)


def _is_green(color) -> bool:
    return (color.green() > 140 and color.red() < 150 and color.blue() < 150)


def _is_dark(color) -> bool:
    return color.red() < 90 and color.green() < 90 and color.blue() < 90


def _red_at(viewer, lx, ly) -> bool:
    return _is_red(_pixel(viewer, *_widget(viewer, lx, ly)))


def _is_accent(color) -> bool:
    """Inkfall interaction-accent pixels (#8B93E0), a *hollow* ring.

    Distinguishes the snap ring from the blue Line (#2b6cff, ``b==255``),
    white halo/page (``b>=246`` or grayscale), the red outline, and the
    probe block — so a match can only come from the indicator itself.
    """
    r, g, b = color.red(), color.green(), color.blue()
    return (190 < b < 246 and 95 < r < 210 and abs(r - g) < 28
            and (b - r) > 20)


def _accent_ring_count(viewer, px, py) -> int:
    """Accent pixels in the ring band (5..8 px) around a widget point."""
    image = viewer.grab().toImage()
    count = 0
    for dy in range(-9, 10):
        for dx in range(-9, 10):
            d = (dx * dx + dy * dy) ** 0.5
            if 5.0 <= d <= 8.0 and _is_accent(
                    image.pixelColor(int(px + dx), int(py + dy))):
                count += 1
    return count


def _pts(seq):
    return [(p.x, p.y) for p in seq]


def _hover(viewer, x, y):
    """Button-free move — the real Polygon live-preview path.

    The existing ``_move`` helper holds the left button (a drag); a user
    constructing a Polygon hovers with *no* button pressed, so the live
    snap must be proven through the button-free branch too.
    """
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


_STYLUS = None


def _stylus():
    global _STYLUS
    if _STYLUS is None:
        from PySide6.QtGui import QInputDevice, QPointingDevice

        _STYLUS = QPointingDevice(
            "test-stylus", 1, QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Pen,
            QInputDevice.Capability.Position, 1, 1)
    return _STYLUS


def _tablet(viewer, etype, x, y, pressure=0.0,
            buttons=Qt.MouseButton.NoButton):
    """Deliver a raw stylus event to the viewer.

    ``pressure=0.0`` / ``NoButton`` models a *hovering* pen (tip lifted):
    the tablet stream must drive the Polygon preview exactly like a
    button-free mouse move, without placing a vertex.
    """
    event = QTabletEvent(
        etype, _stylus(), QPointF(x, y), QPointF(x, y), float(pressure),
        0.0, 0.0, 0.0, 0.0, 0.0, Qt.KeyboardModifier.NoModifier,
        Qt.MouseButton.LeftButton, buttons)
    viewer.tabletEvent(event)
    return event


def _tablet_hover(viewer, lx, ly, page_index=0):
    """Tip-lifted stylus move (no press) at a page-local point."""
    wx, wy = _widget(viewer, lx, ly, page_index)
    return _tablet(viewer, QEvent.Type.TabletMove, wx, wy)


def _snap_tol(viewer) -> float:
    """The live Connect-Lines tolerance (screen px -> page pt)."""
    from app import viewer as viewer_module

    layout = viewer._pipeline.layout
    return viewer_module._SHAPE_SNAP_PX / (
        float(layout.raster_scale) * viewer._display_scale())


def _draw_line_with_tool(viewer, p0, p1, page_index=0):
    """Create a committed Line through the real Line-tool gesture.

    Deliberately does *not* seed the store: the snap target must be a
    shape the user could actually have drawn.
    """
    viewer._interaction.set_tool(_shape_tool(ToolType.LINE, color="#2b6cff"))
    a = _widget(viewer, *p0, page_index)
    b = _widget(viewer, *p1, page_index)
    _press(viewer, *a)
    _move(viewer, *b)
    _release(viewer, *b)


# =================================================== handler intents (Qt-free)

def test_move_adds_provisional_segment_from_last_confirmed_vertex():
    """Req 1/2: A,B confirmed -> outline A->B and provisional B->pointer."""
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    c.handle_shape_press(pid, 100.0, 0.0)
    c.handle_shape_release(pid, 100.0, 0.0)
    intent = c.handle_shape_move(pid, 400.0, 300.0)
    outline = intent["shape_update"]
    assert outline.kind is ShapeKind.POLYGON
    assert outline.close_path is False                 # never closed early
    assert _pts(outline.points) == [(0.0, 0.0), (100.0, 0.0)]
    geo = intent["shape_preview"]
    assert _pts(geo.vertices) == [(0.0, 0.0), (100.0, 0.0)]
    # Exactly one provisional edge: last confirmed vertex -> pointer.
    assert _pts(geo.segment) == [(100.0, 0.0), (400.0, 300.0)]
    assert _pts(geo.fill) == [(0.0, 0.0), (100.0, 0.0), (400.0, 300.0)]
    # The outline Shape's rect covers the pointer so bounded invalidation
    # repaints the whole live contour + provisional edge.
    assert geo.rect.x1 == pytest.approx(400.0)
    assert geo.rect.y1 == pytest.approx(300.0)


def test_move_updates_provisional_segment_without_adding_vertex():
    """Req 2: moving the pointer moves the segment, never a vertex."""
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    first = c.handle_shape_move(pid, 50.0, 50.0)["shape_preview"]
    assert _pts(first.vertices) == [(0.0, 0.0)]
    assert _pts(first.segment) == [(0.0, 0.0), (50.0, 50.0)]
    second = c.handle_shape_move(pid, 200.0, 120.0)["shape_preview"]
    assert _pts(second.vertices) == [(0.0, 0.0)]       # no vertex added
    assert _pts(second.segment) == [(0.0, 0.0), (200.0, 120.0)]


def test_provisional_segment_uses_last_confirmed_vertex_only():
    """Req 3/4: A,B,C confirmed + pointer D -> segment C->D, not D->A."""
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    for pt in ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0)):
        c.handle_shape_press(pid, *pt)
        c.handle_shape_release(pid, *pt)
    geo = c.handle_shape_move(pid, 400.0, 400.0)["shape_preview"]
    assert _pts(geo.vertices) == [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    assert _pts(geo.segment) == [(100.0, 100.0), (400.0, 400.0)]
    # Never a closing segment back to the first vertex.
    assert _pts(geo.segment) != [(400.0, 400.0), (0.0, 0.0)]
    assert (400.0, 400.0) not in _pts(geo.vertices)


def test_move_before_second_vertex_has_no_segment_or_fill():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 10.0, 10.0)
    c.handle_shape_release(pid, 10.0, 10.0)
    geo = c.handle_shape_move(pid, 200.0, 200.0)["shape_preview"]
    assert _pts(geo.vertices) == [(10.0, 10.0)]
    assert (200.0, 200.0) not in _pts(geo.vertices)


def test_fill_contour_tracks_pointer():
    """Req 5 (handler): the fill contour includes the moving pointer."""
    c = InteractionController(_shape_tool(ToolType.POLYGON,
                                          fill_enabled=True))
    pid = PageId()
    for pt in ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0)):
        c.handle_shape_press(pid, *pt)
        c.handle_shape_release(pid, *pt)
    geo = c.handle_shape_move(pid, 400.0, 50.0)["shape_preview"]
    assert _pts(geo.fill) == [
        (0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (400.0, 50.0)]


def test_polygon_snap_off_uses_raw_pointer_but_still_closes():
    """Snap-to-15° OFF => no endpoint/intersection Connect Lines snap; a
    press near a target keeps the raw pointer. Polygon still closes
    regardless of any legacy connect_lines/close_path value."""
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 40.0),), tol_pt=10.0)
    c = InteractionController(
        _shape_tool(ToolType.POLYGON, snap_angle=False,
                    connect_lines=False, close_path=False))
    pid = PageId()
    for pt in ((0.0, 0.0), (105.0, 42.0), (100.0, 200.0)):
        c.handle_shape_press(pid, *pt, snap=ctx)
        c.handle_shape_release(pid, *pt)
    done = c.handle_shape_double_click()["shape_done"]
    # No snap: the near-target vertex keeps the raw pointer position.
    assert (done.points[1].x, done.points[1].y) == pytest.approx((105.0, 42.0))
    assert done.close_path is True


def test_polygon_snap_on_connects_to_endpoint_and_closes():
    """Snap-to-15° ON => endpoint Connect Lines snaps before the click;
    Polygon still closes regardless of close_path."""
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 40.0),), tol_pt=10.0)
    c = InteractionController(
        _shape_tool(ToolType.POLYGON, snap_angle=True,
                    connect_lines=False, close_path=False))
    pid = PageId()
    for pt in ((0.0, 0.0), (105.0, 42.0), (100.0, 200.0)):
        c.handle_shape_press(pid, *pt, snap=ctx)
        c.handle_shape_release(pid, *pt)
    done = c.handle_shape_double_click()["shape_done"]
    assert (done.points[1].x, done.points[1].y) == pytest.approx((100.0, 40.0))
    assert done.close_path is True


def test_cancel_reports_cleared_gesture():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    assert c.cancel() == {"shape_cancelled": True}
    assert c.shape_active() is False
    assert c.handle_shape_move(pid, 50.0, 50.0) == {"noop": True}


# ================================= live Connect-Lines snapping (Qt-free)


def test_move_snaps_provisional_edge_to_endpoint():
    """A move near an endpoint resolves the provisional edge and fill to
    that endpoint *before* the vertex is confirmed."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)   # A
    c.handle_shape_release(pid, 100.0, 100.0)
    intents = c.handle_shape_move(pid, 505.0, 503.0, snap=ctx)
    geo = intents["shape_preview"]
    # The provisional edge ends at the resolved endpoint, not the pointer.
    assert _pts(geo.segment) == [(100.0, 100.0), (500.0, 500.0)]
    # The pointer is still not a confirmed vertex.
    assert _pts(geo.vertices) == [(100.0, 100.0)]
    # The fill contour shares the same resolved endpoint.
    assert _pts(geo.fill)[-1] == (500.0, 500.0)
    # The confirmed outline stays confirmed-only and open.
    assert _pts(intents["shape_update"].points) == [(100.0, 100.0)]
    assert intents["shape_update"].close_path is False


def test_move_snaps_provisional_edge_to_intersection():
    """A move near a segment intersection resolves to the intersection."""
    seg = (Point(0.0, 200.0), Point(400.0, 200.0))
    ctx = ShapeSnapContext(segments=(seg,), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)       # anchor A
    c.handle_shape_release(pid, 0.0, 0.0)
    geo = c.handle_shape_move(pid, 100.0, 205.0, snap=ctx)["shape_preview"]
    # Ray (0,0)->(100,205) crosses y=200 at x = 200 / 2.05 ~ 97.561.
    sx, sy = _pts(geo.segment)[-1]
    assert sx == pytest.approx(97.5610, abs=0.02)
    assert sy == pytest.approx(200.0, abs=0.02)
    # The fill contour ends at the same resolved intersection.
    fx, fy = _pts(geo.fill)[-1]
    assert (fx, fy) == pytest.approx((sx, sy))


def test_confirm_matches_last_snapped_preview_without_jump():
    """Confirming stores exactly the coordinates shown in the preview."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    preview = c.handle_shape_move(pid, 505.0, 503.0, snap=ctx)["shape_preview"]
    shown = _pts(preview.segment)[-1]
    stored = _pts(
        c.handle_shape_press(pid, 505.0, 503.0, snap=ctx)[
            "shape_preview"].vertices)[-1]
    assert shown == (500.0, 500.0)
    assert stored == shown


def test_move_away_from_snap_target_returns_to_pointer():
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    near = c.handle_shape_move(pid, 505.0, 503.0, snap=ctx)["shape_preview"]
    assert _pts(near.segment)[-1] == (500.0, 500.0)
    # Moving away immediately drops the snap and follows the pointer.
    away = c.handle_shape_move(pid, 300.0, 300.0, snap=ctx)["shape_preview"]
    assert _pts(away.segment)[-1] == (300.0, 300.0)


def test_snap_target_wins_over_angle_when_both_apply():
    """Policy: for Polygon an eligible exact endpoint/intersection inside
    the tolerance beats the 15° angular constraint — the same order the
    commit uses, so the preview never lies."""
    # Raw pointer (100, 40) sits at ~21.8°; the 15° constraint alone would
    # resolve to ~(104.05, 27.88), far from the target. The endpoint is
    # ~1.4 pt away (inside tol), so it must win.
    ctx = ShapeSnapContext(endpoints=(Point(101.0, 41.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    geo = c.handle_shape_move(pid, 100.0, 40.0, snap=ctx)["shape_preview"]
    assert _pts(geo.segment) == [(0.0, 0.0), (101.0, 41.0)]
    assert _pts(geo.fill)[-1] == (101.0, 41.0)


def test_angle_applies_when_no_target_in_tolerance():
    """Fallback: with no eligible target inside the tolerance, Polygon
    applies the 15° constraint to the raw pointer (when Snap-to-15° is on).
    """
    ctx = ShapeSnapContext(endpoints=(Point(700.0, 700.0),), tol_pt=6.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    geo = c.handle_shape_move(pid, 100.0, 40.0, snap=ctx)["shape_preview"]
    ex, ey = _pts(geo.segment)[-1]
    # Direction from the anchor is quantized to the nearest 15° (15° here).
    assert math.degrees(math.atan2(ey, ex)) == pytest.approx(15.0, abs=0.2)
    # The resolved fill endpoint matches the provisional edge endpoint.
    assert _pts(geo.fill)[-1] == pytest.approx((ex, ey))


def test_snapped_polygon_closes_without_duplicate_or_early_close():
    """Snapped vertices still connect, close once, and never duplicate."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 0.0), Point(500.0, 400.0)),
                           tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON,
                                          snap_angle=True,
                                          connect_lines=False,
                                          close_path=False))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    c.handle_shape_press(pid, 503.0, 3.0, snap=ctx)     # -> (500, 0)
    c.handle_shape_release(pid, 503.0, 3.0)
    c.handle_shape_press(pid, 503.0, 397.0, snap=ctx)   # -> (500, 400)
    c.handle_shape_release(pid, 503.0, 397.0)
    # A press exactly on the last vertex never appends a duplicate.
    assert c.handle_shape_press(pid, 500.0, 400.0, snap=ctx) == {"noop": True}
    done = c.handle_shape_double_click()["shape_done"]
    assert done.close_path is True
    assert _pts(done.points) == [(0.0, 0.0), (500.0, 0.0), (500.0, 400.0)]


# ============================= transient snap indicator state (Qt-free)
#
# Req 1-10: the resolver now tags *why* the Polygon pointer resolved where
# it did (an existing endpoint/intersection vs a plain 15° angular snap),
# and the preview exposes that transiently for the paint-only indicator.
# These assert the state; the pixel tests below assert it reaches the
# painter. The tag is never a vertex and never stored.

def _preview(c, pid, x, y, snap):
    return c.handle_shape_move(pid, x, y, snap=snap)["shape_preview"]


def test_snap_indicator_endpoint_state_and_shared_resolved_point():
    """Req 1/3: an endpoint snap exposes kind+target, and the provisional
    edge, the fill contour and the indicator share that same point."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    geo = _preview(c, pid, 505.0, 503.0, ctx)
    assert geo.snap_kind == "endpoint"
    assert geo.snap_target == pytest.approx((500.0, 500.0))
    assert _pts(geo.segment)[-1] == pytest.approx(geo.snap_target)
    assert _pts(geo.fill)[-1] == pytest.approx(geo.snap_target)


def test_snap_indicator_intersection_state():
    """Req 2: an intersection snap exposes kind 'intersection'."""
    seg = (Point(0.0, 200.0), Point(400.0, 200.0))
    ctx = ShapeSnapContext(segments=(seg,), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    geo = _preview(c, pid, 100.0, 205.0, ctx)
    assert geo.snap_kind == "intersection"
    assert _pts(geo.segment)[-1] == pytest.approx(geo.snap_target)
    assert geo.snap_target[1] == pytest.approx(200.0, abs=0.02)


def test_snap_indicator_absent_for_angular_snap_only():
    """Req 7: a plain 15° angular snap moves the resolved point but shows
    no endpoint/intersection indicator."""
    ctx = ShapeSnapContext(endpoints=(Point(700.0, 700.0),), tol_pt=6.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    geo = _preview(c, pid, 100.0, 40.0, ctx)
    ex, ey = _pts(geo.segment)[-1]
    # The constraint moved the point ...
    assert (ex, ey) != pytest.approx((100.0, 40.0))
    assert math.degrees(math.atan2(ey, ex)) == pytest.approx(15.0, abs=0.2)
    # ... but no Connect-Lines indicator is shown.
    assert geo.snap_target == ()
    assert geo.snap_kind == ""


def test_snap_indicator_absent_when_snap_off():
    """Req 6: Snap-to-15° OFF => no endpoint/intersection snap, and so no
    indicator; the provisional edge follows the raw pointer."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=False))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    geo = _preview(c, pid, 505.0, 503.0, ctx)
    assert geo.snap_target == ()
    assert geo.snap_kind == ""
    assert _pts(geo.segment)[-1] == pytest.approx((505.0, 503.0))


def test_snap_indicator_clears_when_pointer_leaves_tolerance():
    """Req 5: leaving the tolerance drops the transient indicator."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    near = _preview(c, pid, 505.0, 503.0, ctx)
    assert near.snap_target != ()
    away = _preview(c, pid, 300.0, 300.0, ctx)
    assert away.snap_target == ()
    assert away.snap_kind == ""


def test_snap_indicator_follows_pointer_between_targets():
    """Req 4: the indicator tracks whichever eligible target is nearest."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),
                                      Point(500.0, 100.0)), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    a = _preview(c, pid, 505.0, 495.0, ctx)
    assert a.snap_target == pytest.approx((500.0, 500.0))
    b = _preview(c, pid, 505.0, 105.0, ctx)
    assert b.snap_target == pytest.approx((500.0, 100.0))


def test_snap_indicator_cleared_on_confirm_with_single_vertex():
    """Req 8: confirming removes the transient indicator, leaves no
    provisional edge, and creates exactly one vertex at the previewed
    coordinate."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    shown = _preview(c, pid, 505.0, 503.0, ctx)
    assert shown.snap_target == pytest.approx((500.0, 500.0))
    after = c.handle_shape_press(pid, 505.0, 503.0, snap=ctx)["shape_preview"]
    assert after.snap_target == ()
    assert after.snap_kind == ""
    assert after.segment == ()
    assert _pts(after.vertices) == pytest.approx(
        [(100.0, 100.0), (500.0, 500.0)])


def test_snap_indicator_cleared_on_cancel():
    """Req 9: cancellation drops the transient indicator and the gesture."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 500.0),), tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0, snap=ctx)
    c.handle_shape_release(pid, 100.0, 100.0)
    assert _preview(c, pid, 505.0, 503.0, ctx).snap_target != ()
    assert c.cancel() == {"shape_cancelled": True}
    assert c.shape_active() is False
    assert c.handle_shape_move(pid, 505.0, 503.0, snap=ctx) == {"noop": True}


def test_snap_indicator_never_enters_committed_geometry():
    """Req 10: the transient indicator is never a vertex and never stored —
    the committed Polygon carries only the real confirmed vertices."""
    ctx = ShapeSnapContext(endpoints=(Point(500.0, 0.0), Point(500.0, 400.0)),
                           tol_pt=12.0)
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    c.handle_shape_release(pid, 0.0, 0.0)
    assert _preview(c, pid, 503.0, 3.0, ctx).snap_target == \
        pytest.approx((500.0, 0.0))
    c.handle_shape_press(pid, 503.0, 3.0, snap=ctx)
    c.handle_shape_release(pid, 503.0, 3.0)
    c.handle_shape_press(pid, 503.0, 397.0, snap=ctx)
    c.handle_shape_release(pid, 503.0, 397.0)
    done = c.handle_shape_double_click()["shape_done"]
    assert _pts(done.points) == pytest.approx(
        [(0.0, 0.0), (500.0, 0.0), (500.0, 400.0)])


# ==================================================== viewer paint (offscreen)

def test_pixels_confirmed_edges_plus_one_provisional_no_closing(
        qtbot, tmp_path, cleared_settings):
    """Req 1/3/4: A->B, B->C confirmed, C->D provisional, no closing."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24"))
    try:
        _click(viewer, (150.0, 150.0))          # A
        _click(viewer, (350.0, 150.0))          # B
        _click(viewer, (350.0, 350.0))          # C
        _move(viewer, *_widget(viewer, 150.0, 450.0))   # D
        viewer.repaint()
        assert _red_at(viewer, 250.0, 150.0) is True    # confirmed A->B
        assert _red_at(viewer, 350.0, 250.0) is True    # confirmed B->C
        assert _red_at(viewer, 250.0, 400.0) is True    # provisional C->D
        # No closing edge D->A and no premature closing C->A.
        assert _red_at(viewer, 150.0, 300.0) is False   # D -> A
        assert _red_at(viewer, 250.0, 250.0) is False   # C -> A
        # A point between B and D is not on any drawn edge.
        assert _red_at(viewer, 250.0, 300.0) is False
    finally:
        viewer.close_document()


def test_provisional_segment_follows_pointer_then_clears(
        qtbot, tmp_path, cleared_settings):
    """Req 2: the live segment tracks the pointer and the old one clears."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24"))
    try:
        _click(viewer, (150.0, 150.0))          # A
        _click(viewer, (350.0, 150.0))          # B
        _move(viewer, *_widget(viewer, 350.0, 350.0))   # pointer -> P1
        viewer.repaint()
        assert _red_at(viewer, 350.0, 250.0) is True    # provisional B->P1
        before = _pts(viewer._live_shape.points)
        _move(viewer, *_widget(viewer, 500.0, 150.0))   # pointer -> P2
        viewer.repaint()
        assert _red_at(viewer, 425.0, 150.0) is True    # provisional B->P2
        assert _red_at(viewer, 350.0, 250.0) is False   # old B->P1 cleared
        assert _pts(viewer._live_shape.points) == before
        history = viewer._history_for(viewer._document, create=False)
        assert history is None or history.stack.count() == 0
    finally:
        viewer.close_document()


def test_live_preview_snaps_to_committed_endpoint_on_move(
        qtbot, tmp_path, cleared_settings):
    """The viewer rebuilds the snap context on every move, so a live
    Polygon move snaps its provisional edge to a committed endpoint."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24",
                         snap_angle=True))
    try:
        # Seed a committed line whose endpoint (400, 400) is a snap target.
        line = Shape(
            kind=ShapeKind.LINE,
            points=(Point(200.0, 400.0), Point(400.0, 400.0)),
            rect=Rect(Point(200.0, 400.0), Size(200.0, 0.0)),
            width_mm=2.0, color="#2b6cff")
        viewer._document.annotations.add(line, 0)
        _click(viewer, (150.0, 150.0))          # A
        _move(viewer, *_widget(viewer, 402.0, 401.0))
        viewer.repaint()
        preview = viewer._live_shape_preview
        assert preview is not None
        end = preview.segment[-1]
        assert (end.x, end.y) == pytest.approx((400.0, 400.0))
        # The pointer is not yet a confirmed vertex.
        assert _pts(viewer._live_shape.points) == [(150.0, 150.0)]
    finally:
        viewer.close_document()


def test_vertex_markers_only_at_confirmed_vertices(
        qtbot, tmp_path, cleared_settings):
    """Req 6: constant-size markers at confirmed vertices only."""
    # Thin outline so a marker disc is clearly distinct from the stroke.
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=0.25))
    try:
        _click(viewer, (150.0, 150.0))          # A
        _click(viewer, (150.0, 350.0))          # B
        _move(viewer, *_widget(viewer, 150.0, 550.0))   # pointer D (B->D)
        viewer.repaint()
        ax, ay = _widget(viewer, 150.0, 150.0)
        bx, by = _widget(viewer, 150.0, 350.0)
        px, py = _widget(viewer, 150.0, 550.0)
        # Confirmed A and B carry a marker a couple of px off their edge.
        assert _is_red(_pixel(viewer, ax + 2, ay)) is True
        assert _is_red(_pixel(viewer, ax - 2, ay)) is True
        assert _is_red(_pixel(viewer, bx + 2, by)) is True
        # The pointer has no marker (only the thin provisional stroke cap).
        assert _is_red(_pixel(viewer, px + 2, py)) is False
        assert _is_red(_pixel(viewer, px - 2, py)) is False
    finally:
        viewer.close_document()


def test_vertex_marker_uses_larger_constant_radius(
        qtbot, tmp_path, cleared_settings):
    """M6.7.2 follow-up: confirmed markers are ~9 px across (r=4.5).

    The marker is a red disc (fill radius ``r``) inside a white ring (pen
    width 1.5 centred on ``r``). Measured over the dark probe block, an
    ``r=4.5`` disc paints pure red out to ~3.5 px, while the previous
    ``r=3.5`` disc reached only ~2.5 px, so a >=3.0 px red radius proves
    the enlarged footprint.
    """
    from app import viewer as viewer_module

    assert viewer_module._POLYGON_VERTEX_RADIUS_PX == 4.5
    # Thin outline so the marker disc is measured, not the stroke.
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=0.25))
    try:
        # Place the confirmed vertex near the centre of the probe PDF's
        # solid black block so the marker's pure-red fill and white ring
        # are both distinguishable from the page; the vertical provisional
        # edge runs straight down, so the scan isolates the marker from
        # the stroke.
        _click(viewer, (100.0, 100.0))          # A (over the probe block)
        _move(viewer, *_widget(viewer, 100.0, 300.0))
        viewer.repaint()
        ax, ay = _widget(viewer, 100.0, 100.0)
        pure_red = 0.0
        ring_seen = False
        for dy in range(-9, 10):
            for dx in range(-9, 10):
                color = _pixel(viewer, ax + dx, ay + dy)
                dist = (dx * dx + dy * dy) ** 0.5
                if (color.red() > 200 and color.green() < 60
                        and color.blue() < 60):
                    pure_red = max(pure_red, dist)
                if (color.red() > 180 and color.green() > 180
                        and color.blue() > 180):
                    ring_seen = True
        # The red disc is clearly larger than the old r=3.5 fill (~2.5 px).
        assert pure_red >= 3.0, pure_red
        assert pure_red <= 4.6, pure_red
        # Its white ring is still painted around the disc.
        assert ring_seen is True
        # The moving pointer still gets no marker (only the stroke cap).
        px, py = _widget(viewer, 100.0, 300.0)
        assert _is_red(_pixel(viewer, px + 4, py)) is False
        assert _is_red(_pixel(viewer, px - 4, py)) is False
    finally:
        viewer.close_document()


def test_fill_preview_updates_with_pointer_and_is_fill_only(
        qtbot, tmp_path, cleared_settings):
    """Req 5: fill follows the pointer; it is a fill, not extra edges."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24",
                         fill_enabled=True, fill_color="#00cc44",
                         fill_opacity=1.0))
    try:
        # Vertices inside the probe PDF's solid black block (0,0)-(200,200).
        _click(viewer, (20.0, 20.0))
        _click(viewer, (180.0, 20.0))
        _click(viewer, (180.0, 180.0))
        _move(viewer, *_widget(viewer, 20.0, 180.0))
        viewer.repaint()
        assert _is_green(_pixel(viewer, *_widget(viewer, 100.0, 100.0))) is True
        # Move the pointer up: the fill contour follows, revealing page.
        _move(viewer, *_widget(viewer, 20.0, 60.0))
        viewer.repaint()
        assert _is_green(_pixel(viewer, *_widget(viewer, 100.0, 80.0))) is True
        assert _is_green(_pixel(viewer, *_widget(viewer, 100.0, 150.0))) is False
        # The revealed area is the black page, not an opaque backdrop.
        assert _is_dark(_pixel(viewer, *_widget(viewer, 100.0, 150.0))) is True
    finally:
        viewer.close_document()


def test_fill_disabled_preview_has_no_backdrop(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, fill_enabled=False))
    try:
        _click(viewer, (20.0, 20.0))
        _click(viewer, (180.0, 20.0))
        _click(viewer, (180.0, 180.0))
        _move(viewer, *_widget(viewer, 20.0, 180.0))
        viewer.repaint()
        assert _is_dark(_pixel(viewer, *_widget(viewer, 100.0, 100.0))) is True
    finally:
        viewer.close_document()


def test_completion_removes_provisional_and_adds_closing_once(
        qtbot, tmp_path, cleared_settings):
    """Req 7: on completion the provisional edge is gone and the closing
    edge appears exactly once (one shape, one history step)."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24"))
    try:
        _click(viewer, (150.0, 150.0))          # A
        _click(viewer, (350.0, 150.0))          # B
        _click(viewer, (350.0, 350.0))          # C
        _move(viewer, *_widget(viewer, 150.0, 450.0))   # D
        viewer.repaint()
        assert _red_at(viewer, 250.0, 400.0) is True    # provisional C->D
        _dbl(viewer, *_widget(viewer, 350.0, 350.0))
        assert viewer._live_shape is None
        assert viewer._live_shape_preview is None
        viewer.repaint()
        assert _red_at(viewer, 250.0, 400.0) is False   # provisional removed
        assert _red_at(viewer, 250.0, 250.0) is True    # closing edge C->A
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        assert shapes[0].close_path is True
        assert _pts(shapes[0].points) == [
            (150.0, 150.0), (350.0, 150.0), (350.0, 350.0)]
        history = viewer._history_for(viewer._document, create=False)
        assert history.stack.count() == 1
    finally:
        viewer.close_document()


def test_preview_cleared_on_cancellation(qtbot, tmp_path, cleared_settings):
    """Req 8: cancellation removes all preview elements, no history."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, fill_enabled=True,
                         fill_color="#00cc44", fill_opacity=1.0))
    try:
        _click(viewer, (150.0, 150.0))
        _click(viewer, (350.0, 150.0))
        _move(viewer, *_widget(viewer, 350.0, 350.0))
        viewer.repaint()
        assert viewer._live_shape is not None
        assert viewer._live_shape_preview is not None
        assert _red_at(viewer, 350.0, 250.0) is True    # provisional edge
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._live_shape is None
        assert viewer._live_shape_preview is None
        viewer.repaint()
        assert _red_at(viewer, 350.0, 250.0) is False   # provisional gone
        assert _red_at(viewer, 250.0, 150.0) is False   # confirmed edge gone
        assert _shapes(viewer) == []
        history = viewer._history_for(viewer._document, create=False)
        assert history is None or history.stack.count() == 0
    finally:
        viewer.close_document()


def test_undo_redo_preserves_geometry_and_appearance(
        qtbot, tmp_path, cleared_settings):
    """Req 9: undo/redo preserves the committed polygon exactly."""
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24",
                         fill_enabled=True, fill_color="#00cc44",
                         fill_opacity=1.0))
    try:
        _click(viewer, (250.0, 250.0))
        _click(viewer, (450.0, 250.0))
        _click(viewer, (450.0, 450.0))
        _dbl(viewer, *_widget(viewer, 450.0, 450.0))
        original = _shapes(viewer)[0]
        geo = (_pts(original.points), original.close_path, original.color,
               original.fill_enabled, original.fill_color)
        viewer.repaint()
        assert _is_green(_pixel(viewer, *_widget(viewer, 400.0, 300.0))) is True
        assert _is_red(_pixel(viewer, *_widget(viewer, 350.0, 250.0))) is True
        viewer._on_undo_requested()
        viewer.repaint()
        assert _shapes(viewer) == []
        assert _is_green(_pixel(viewer, *_widget(viewer, 400.0, 300.0))) is False
        assert _is_red(_pixel(viewer, *_widget(viewer, 350.0, 250.0))) is False
        viewer._on_redo_requested()
        viewer.repaint()
        redone = _shapes(viewer)
        assert len(redone) == 1
        assert (_pts(redone[0].points), redone[0].close_path, redone[0].color,
                redone[0].fill_enabled, redone[0].fill_color) == geo
        assert _is_green(_pixel(viewer, *_widget(viewer, 400.0, 300.0))) is True
        assert _is_red(_pixel(viewer, *_widget(viewer, 350.0, 250.0))) is True
    finally:
        viewer.close_document()


# ================================= real viewer event path (tool + hover)
#
# The tests above either seed the store directly or hold the left button
# during the move. A user does neither: the snap target is drawn with the
# Line tool and the Polygon edge is previewed with a *button-free* hover.
# These tests reproduce that exact runtime path.

def _near(viewer, target, frac=0.8):
    """A page-pt pointer ``frac`` of the live tolerance off ``target``."""
    return (target[0] + frac * _snap_tol(viewer), target[1])


def test_real_line_target_feeds_context_and_live_hover_snaps(
        qtbot, tmp_path, cleared_settings):
    """Req 1/2/3/7 via the real path: a Line drawn with the Line tool is
    an eligible target, and a button-free Polygon hover resolves the
    provisional edge *and* the fill to it before any confirmation.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        lines = [s for s in _shapes(viewer) if s.kind is ShapeKind.LINE]
        assert len(lines) == 1
        assert _pts(lines[0].points) == [(500.0, 200.0), (500.0, 400.0)]

        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        pid = viewer._pipeline.layout.entry(0).page_id

        # Req 1: the drawn Line endpoint is a real snap target.
        ctx = viewer._shape_snap_context(pid)
        assert (500.0, 400.0) in _pts(ctx.endpoints)
        assert ctx.tol_pt > 0.0

        _click(viewer, (500.0, 100.0))              # anchor A
        off = _near(viewer, (500.0, 400.0))
        assert off[0] - 500.0 >= 4.0                # measurable separation
        _hover(viewer, *_widget(viewer, *off))
        viewer.repaint()

        preview = viewer._live_shape_preview
        assert preview is not None
        # Req 2: the provisional edge snapped on hover (before confirming).
        assert _pts(preview.segment) == [(500.0, 100.0), (500.0, 400.0)]
        # Req 3: the fill contour shares the identical resolved point.
        assert _pts(preview.fill)[-1] == (500.0, 400.0)
        # Req 7: the pointer is not a vertex; the edge never closes back.
        assert _pts(viewer._live_shape.points) == [(500.0, 100.0)]
        assert viewer._live_shape.close_path is False
        assert _pts(preview.segment)[0] == (500.0, 100.0)

        # Rendered proof: the live edge sits at the snapped x, not the raw x.
        assert _red_at(viewer, 500.0, 380.0) is True
        raw_x = 500.0 + (off[0] - 500.0) * (380.0 - 100.0) / (400.0 - 100.0)
        assert _red_at(viewer, raw_x, 380.0) is False
    finally:
        viewer.close_document()


def test_real_confirm_commits_last_previewed_snapped_point(
        qtbot, tmp_path, cleared_settings):
    """Req 4/7: confirming after a snapped hover commits exactly the
    previewed coordinate (no jump), then closes once with no duplicate.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", snap_angle=True))
        qtbot.wait(20)

        _click(viewer, (500.0, 100.0))              # A
        off = _near(viewer, (500.0, 400.0))
        _hover(viewer, *_widget(viewer, *off))
        shown = _pts(viewer._live_shape_preview.segment)[-1]
        assert shown == (500.0, 400.0)

        _click(viewer, off)                         # confirm B (snaps)
        committed = _pts(viewer._live_shape.points)
        assert committed[-1] == shown               # no positional jump
        assert committed == [(500.0, 100.0), (500.0, 400.0)]
        assert viewer._live_shape.close_path is False

        _click(viewer, (200.0, 400.0))              # C, far from targets
        _dbl(viewer, *_widget(viewer, 200.0, 400.0))
        polys = [s for s in _shapes(viewer) if s.kind is ShapeKind.POLYGON]
        assert len(polys) == 1
        assert polys[0].close_path is True
        got = _pts(polys[0].points)
        expected = [(500.0, 100.0), (500.0, 400.0), (200.0, 400.0)]
        assert len(got) == len(expected)
        for point, want in zip(got, expected):
            assert point == pytest.approx(want, abs=1e-6)
    finally:
        viewer.close_document()


def test_real_snap_off_hover_follows_raw_pointer(
        qtbot, tmp_path, cleared_settings):
    """Req 5: with Snap-to-15° off, a hover near the target follows the
    raw pointer — no endpoint/intersection Connect-Lines snap at all.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", snap_angle=False))
        qtbot.wait(20)

        _click(viewer, (500.0, 100.0))              # A
        off = _near(viewer, (500.0, 400.0))
        _hover(viewer, *_widget(viewer, *off))
        end = _pts(viewer._live_shape_preview.segment)[-1]
        assert end == pytest.approx(off, abs=0.5)   # raw pointer, unsnapped
        assert end != pytest.approx((500.0, 400.0), abs=1.0)
    finally:
        viewer.close_document()


def test_real_hover_away_then_back_updates_preview(
        qtbot, tmp_path, cleared_settings):
    """Req 6: moving away from the target updates the preview; coming
    back re-snaps — the context is refreshed on every move.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", snap_angle=True))
        qtbot.wait(20)

        _click(viewer, (500.0, 100.0))              # A
        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0))))
        assert _pts(viewer._live_shape_preview.segment)[-1] == (500.0, 400.0)

        _hover(viewer, *_widget(viewer, 300.0, 600.0))     # away
        away = _pts(viewer._live_shape_preview.segment)[-1]
        assert away != (500.0, 400.0)
        assert away[0] < 450.0

        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0),
                                               frac=0.4)))
        assert _pts(viewer._live_shape_preview.segment)[-1] == (500.0, 400.0)
    finally:
        viewer.close_document()


# =========================== visible snap indicator (real path, offscreen)
#
# The acceptance criterion: a user can visibly tell when Polygon Connect
# Lines has engaged before confirming the vertex. These paint through the
# real hover path and assert accent-ring pixels on the resolved target.

def test_real_snap_indicator_shown_for_endpoint_then_cleared_on_confirm(
        qtbot, tmp_path, cleared_settings):
    """Req 1/3/5/8/10: a real hover snap paints a visible accent ring on
    the resolved endpoint before any click; confirming removes it and
    stores exactly one vertex at the previewed coordinate."""
    from app import viewer as viewer_module

    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (500.0, 100.0))                      # anchor A
        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0))))
        viewer.repaint()

        preview = viewer._live_shape_preview
        assert preview is not None
        assert preview.snap_kind == "endpoint"
        assert preview.snap_target == pytest.approx((500.0, 400.0))
        # Req 3: edge, fill and indicator share the resolved point.
        assert _pts(preview.segment)[-1] == pytest.approx(preview.snap_target)
        assert _pts(preview.fill)[-1] == pytest.approx(preview.snap_target)
        # The indicator is visibly painted (Req 1).
        px, py = _widget(viewer, 500.0, 400.0)
        assert _accent_ring_count(viewer, px, py) >= 6

        # Bounded invalidation: the stored prev-rect is the shape rect
        # inflated by the ring radius, so the ring repaints/clears exactly.
        base = viewer._shape_widget_rect(viewer._live_shape)
        m = int(math.ceil(viewer_module._SNAP_RING_PX)) + 3
        assert viewer._live_shape_prev_rect == base.adjusted(-m, -m, m, m)

        # Req 8/10: confirm on the target. The indicator and the
        # provisional edge vanish, and exactly one vertex is created at
        # the previewed coordinate; nothing enters the store yet.
        _click(viewer, (500.0, 400.0))
        after = viewer._live_shape_preview
        assert after.snap_target == ()
        assert after.snap_kind == ""
        assert after.segment == ()
        assert _pts(viewer._live_shape.points) == pytest.approx(
            [(500.0, 100.0), (500.0, 400.0)])
        viewer.repaint()
        assert _accent_ring_count(viewer, px, py) <= 1
        assert [s for s in _shapes(viewer)
                if s.kind is ShapeKind.POLYGON] == []
    finally:
        viewer.close_document()


def test_real_snap_indicator_shown_for_intersection(
        qtbot, tmp_path, cleared_settings):
    """Req 2/3: an intersection snap also paints the ring on its target."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (200.0, 400.0), (600.0, 400.0))
        _draw_line_with_tool(viewer, (400.0, 200.0), (400.0, 600.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (400.0, 100.0))                      # anchor A
        _hover(viewer, *_widget(viewer, 403.0, 401.0))      # near the crossing
        viewer.repaint()

        preview = viewer._live_shape_preview
        assert preview is not None
        assert preview.snap_kind == "intersection"
        assert preview.snap_target == pytest.approx((400.0, 400.0), abs=3.0)
        assert _pts(preview.segment)[-1] == pytest.approx(preview.snap_target)
        px, py = _widget(viewer, *preview.snap_target)
        assert _accent_ring_count(viewer, px, py) >= 4
    finally:
        viewer.close_document()


def test_real_snap_indicator_follows_target_and_clears_outside_tolerance(
        qtbot, tmp_path, cleared_settings):
    """Req 4/5/7: the ring tracks the acquired target, clears when the
    pointer leaves the tolerance, and never appears for angular-only
    snapping (the resolved point moves but there is no target)."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        _draw_line_with_tool(viewer, (300.0, 200.0), (300.0, 250.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (500.0, 100.0))                      # anchor A

        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0))))
        assert viewer._live_shape_preview.snap_target == \
            pytest.approx((500.0, 400.0))
        px, py = _widget(viewer, 500.0, 400.0)
        assert _accent_ring_count(viewer, px, py) >= 6

        # Away from every target: the ring clears (the angle still moves
        # the resolved point, which must NOT show the indicator).
        _hover(viewer, *_widget(viewer, 420.0, 700.0))
        viewer.repaint()
        away = viewer._live_shape_preview
        assert away.snap_target == ()
        assert away.snap_kind == ""
        assert _accent_ring_count(viewer, px, py) <= 1

        # Re-acquire the same target: the ring returns.
        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0),
                                               frac=0.4)))
        assert viewer._live_shape_preview.snap_target == \
            pytest.approx((500.0, 400.0))
    finally:
        viewer.close_document()


def test_real_no_snap_indicator_when_snap_off(
        qtbot, tmp_path, cleared_settings):
    """Req 6/7: Snap-to-15° OFF never paints an endpoint/intersection ring,
    even with the pointer right on an eligible endpoint."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=False))
        qtbot.wait(20)
        _click(viewer, (500.0, 100.0))
        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0))))
        viewer.repaint()
        assert viewer._live_shape_preview.snap_target == ()
        px, py = _widget(viewer, 500.0, 400.0)
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


def test_real_snap_indicator_cleared_on_cancel_tool_switch_path(
        qtbot, tmp_path, cleared_settings):
    """Cleanup: the cancel path that tool switching and document closure
    invoke discards the transient indicator along with the live preview."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (500.0, 100.0))
        _hover(viewer, *_widget(viewer, *_near(viewer, (500.0, 400.0))))
        viewer.repaint()
        px, py = _widget(viewer, 500.0, 400.0)
        assert _accent_ring_count(viewer, px, py) >= 6

        # Cancelling (the tool-switch / document-close cleanup path) drops
        # both the live preview and the transient indicator.
        viewer._apply_intent(viewer._interaction.cancel())
        viewer.repaint()
        assert viewer._live_shape is None
        assert viewer._live_shape_preview is None
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


def _red_terminus(viewer, ax, ay):
    """Farthest red (preview-stroke) pixel from (ax, ay) in the grab.

    The red channel isolates the in-progress Polygon stroke from the blue
    committed target Line, so this measures *rendered* geometry — never
    handler/state values.
    """
    image = viewer.grab().toImage()
    w, h = image.width(), image.height()
    best, best_d = None, -1.0
    for py in range(max(0, int(ay) - 800), min(h, int(ay) + 800)):
        for px in range(max(0, int(ax) - 800), min(w, int(ax) + 800)):
            if _is_red(image.pixelColor(px, py)):
                d = math.hypot(px - ax, py - ay)
                if d > best_d:
                    best_d, best = d, (px, py)
    return best, best_d


def _perp_px(px, py, ax, ay, tx, ty):
    """Perpendicular screen-px distance of (px, py) from the line a->t."""
    vx, vy = tx - ax, ty - ay
    n = math.hypot(vx, vy)
    if n <= 0.0:
        return -1.0
    return abs((px - ax) * vy - (py - ay) * vx) / n


def test_real_painted_edge_terminates_on_target_before_confirmation(
        qtbot, tmp_path, cleared_settings):
    """Rendered (not state) proof of the required live connection.

    Before any confirmation, the *painted* provisional edge must terminate
    at the resolved snap target and not at the raw pointer — the old,
    broken behaviour.  The raw pointer is deliberately displaced
    perpendicular to the anchor->target ray, so the two candidate
    endpoints are unmistakably different pixels: the measured terminus
    lies on the target ray (offset ~= stroke half-width) and far off the
    raw ray.  ``segment[-1]`` / ring pixels alone are not load-bearing.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)

        anchor = (300.0, 500.0)
        target = (500.0, 400.0)                 # committed Line endpoint
        tol = _snap_tol(viewer)
        vx, vy = target[0] - anchor[0], target[1] - anchor[1]
        n = math.hypot(vx, vy)
        ux, uy = -vy / n, vx / n                # perpendicular unit
        raw = (target[0] + ux * 0.85 * tol,     # inside tol, off the ray
               target[1] + uy * 0.85 * tol)

        _click(viewer, anchor)                  # confirm the FIRST vertex
        _hover(viewer, *_widget(viewer, *raw))  # button-free hover
        viewer.repaint()

        # State (necessary, not sufficient): still in progress, snapped.
        preview = viewer._live_shape_preview
        assert preview is not None
        assert viewer._live_shape.close_path is False
        assert _pts(preview.segment) == [anchor, target]
        assert _pts(viewer._live_shape.points) == [anchor]  # pointer uncommitted

        aw = _widget(viewer, *anchor)
        tw = _widget(viewer, *target)
        rw = _widget(viewer, *raw)
        # The raw pointer is a genuinely different, measurable location.
        assert math.hypot(rw[0] - tw[0], rw[1] - tw[1]) >= 5.0

        # Rendered proof: the painted terminus is on the TARGET ray, and
        # far off the RAW ray (which the broken preview would have hit).
        terminus, _dist = _red_terminus(viewer, *aw)
        assert terminus is not None
        perp_target = _perp_px(terminus[0], terminus[1], aw[0], aw[1],
                               tw[0], tw[1])
        perp_raw = _perp_px(terminus[0], terminus[1], aw[0], aw[1],
                            rw[0], rw[1])
        assert perp_target <= 3.0               # stroke half-width only
        assert perp_raw >= 6.0                  # raw ray clearly missed
        # The raw pointer's own terminus is not painted as the live edge.
        assert _red_at(viewer, raw[0], raw[1]) is False

        # Confirming commits exactly the previewed (snapped) point.
        _click(viewer, raw)
        assert _pts(viewer._live_shape.points)[-1] == target
    finally:
        viewer.close_document()


def test_real_open_properties_popup_does_not_swallow_polygon_hover(
        qtbot, tmp_path, cleared_settings):
    """The first Line-vs-Polygon divergence: delivery channel.

    A button-held Line drag always reaches the viewer (Qt's implicit
    grab), so its preview tracks over anything.  A button-free Polygon
    hover is delivered to the widget under the cursor, so an open tool
    Properties popup — a viewer *child* overlay created after
    MainWindow's tracking sweep — used to swallow it and freeze the live
    edge.  This asserts the popup now mirrors button-free moves to the
    viewer, so the provisional edge keeps tracking the snap target while
    the inspector is open (and never stops being rendered).
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication

        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        slot = _shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=2.0,
                           snap_angle=True)
        viewer._interaction.set_tool(slot)
        qtbot.wait(20)
        _click(viewer, (500.0, 100.0))              # anchor A

        # The genuine anchored inspector, parented to the viewer.
        assert viewer._open_properties_popup(slot, viewer._shelf) is True
        qtbot.wait(60)
        popup = viewer._tool_popup
        assert popup is not None and popup.isVisible()
        # The overlay and its subtree are tracked so moves are delivered.
        assert popup.hasMouseTracking() is True

        # Deliver a button-free hover *to the popup* at the target's spot:
        # the real path that was previously dropped before the viewer.
        target = (500.0, 400.0)
        tw = _widget(viewer, *target)
        pos = popup.mapFrom(viewer, QPoint(int(tw[0]), int(tw[1])))
        QApplication.sendEvent(popup, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(pos),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        viewer.repaint()

        preview = viewer._live_shape_preview
        assert preview is not None
        # The preview still resolved to the target despite the overlay.
        assert _pts(preview.segment) == [(500.0, 100.0), target]
        # Rendered: the connected edge is actually on the page.
        assert _red_at(viewer, *target) is True
    finally:
        try:
            viewer._close_tool_popup()
        except Exception:
            pass
        viewer.close_document()


# ===================== M6.7.2 follow-up: Line/Arrow snap indicator
#
# The same hollow accent ring the Polygon preview paints is now shown for a
# Line / Arrow drag that acquires an eligible endpoint / intersection snap.
# These are real viewer event-path tests with rendered-pixel assertions.

def _snap_xy(snap):
    """The (x, y) of a live ``shape_snap`` payload; fails if it is None."""
    assert snap is not None
    return (snap[0], snap[1])


def test_real_line_preview_shows_snap_ring_on_endpoint(
        qtbot, tmp_path, cleared_settings):
    """Req 1/2/3: a Line drag that acquires an endpoint Connect-Lines snap
    paints the shared accent ring on the *visible* endpoint before the
    gesture is confirmed, and the preview's endpoint is that same target."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.LINE, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=False))
        qtbot.wait(20)

        target = (500.0, 400.0)
        start = (300.0, 620.0)
        raw = _near(viewer, target)                 # inside tol, off-target
        _press(viewer, *_widget(viewer, *start))
        _move(viewer, *_widget(viewer, *raw))
        viewer.repaint()

        live = viewer._live_shape
        assert live is not None and live.kind is ShapeKind.LINE
        # The visible endpoint resolves to the target, not the raw cursor.
        assert _pts(live.points)[-1] == pytest.approx(target)
        # The transient snap payload drives the ring, at the resolved point.
        assert _snap_xy(viewer._live_shape_snap) == pytest.approx(target)
        assert viewer._live_shape_snap[2] == "endpoint"
        # Rendered: the accent ring is painted on the visible endpoint.
        px, py = _widget(viewer, *target)
        assert _accent_ring_count(viewer, px, py) >= 6
        # ...and nowhere else well away from it.
        fx, fy = _widget(viewer, 150.0, 700.0)
        assert _accent_ring_count(viewer, fx, fy) <= 1
        # The in-progress drag has committed nothing: only the target Line
        # (drawn earlier, before the gesture) is in the store.
        assert len(_shapes(viewer)) == 1
    finally:
        viewer.close_document()


def test_real_rectangle_preview_has_no_snap_ring(
        qtbot, tmp_path, cleared_settings):
    """Req 1 scope: the indicator is Line/Arrow (and Polygon) only —
    Rectangle's existing drag preview is unchanged (no ring payload)."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.RECTANGLE, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=False))
        qtbot.wait(20)
        target = (500.0, 400.0)
        _press(viewer, *_widget(viewer, 300.0, 200.0))
        _move(viewer, *_widget(viewer, *_near(viewer, target)))
        viewer.repaint()
        live = viewer._live_shape
        assert live is not None and live.kind is ShapeKind.RECTANGLE
        # The rectangle preview is untouched: no snap payload, no ring.
        assert viewer._live_shape_snap is None
        px, py = _widget(viewer, *target)
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


def test_real_arrow_preview_shows_snap_ring_on_endpoint(
        qtbot, tmp_path, cleared_settings):
    """Req 1/2: the Arrow drag shares the exact same snap indicator."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.ARROW, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=False))
        qtbot.wait(20)

        target = (500.0, 400.0)
        raw = _near(viewer, target)
        _press(viewer, *_widget(viewer, 300.0, 620.0))
        _move(viewer, *_widget(viewer, *raw))
        viewer.repaint()

        live = viewer._live_shape
        assert live is not None and live.kind is ShapeKind.ARROW
        assert _pts(live.points)[-1] == pytest.approx(target)
        assert _snap_xy(viewer._live_shape_snap) == pytest.approx(target)
        assert viewer._live_shape_snap[2] == "endpoint"
        px, py = _widget(viewer, *target)
        assert _accent_ring_count(viewer, px, py) >= 6
    finally:
        viewer.close_document()


def test_real_line_preview_ring_follows_target_and_clears(
        qtbot, tmp_path, cleared_settings):
    """Req 3/4: the ring tracks the acquired target as the pointer moves
    between two eligible targets, and clears when the snap is lost."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))   # T1
        _draw_line_with_tool(viewer, (300.0, 200.0), (300.0, 400.0))   # T2
        viewer._interaction.set_tool(_shape_tool(
            ToolType.LINE, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=False))
        qtbot.wait(20)

        t1, t2 = (500.0, 400.0), (300.0, 400.0)
        _press(viewer, *_widget(viewer, 400.0, 650.0))

        _move(viewer, *_widget(viewer, *_near(viewer, t1)))
        viewer.repaint()
        assert _snap_xy(viewer._live_shape_snap) == pytest.approx(t1)
        p1 = _widget(viewer, *t1)
        assert _accent_ring_count(viewer, *p1) >= 6

        _move(viewer, *_widget(viewer, *_near(viewer, t2)))
        viewer.repaint()
        assert _snap_xy(viewer._live_shape_snap) == pytest.approx(t2)
        p2 = _widget(viewer, *t2)
        assert _accent_ring_count(viewer, *p2) >= 6
        # The previous target's ring has cleared.
        assert _accent_ring_count(viewer, *p1) <= 1

        # Away from every target: no snap payload and no ring.
        _move(viewer, *_widget(viewer, 400.0, 120.0))
        viewer.repaint()
        assert viewer._live_shape_snap is None
        assert _accent_ring_count(viewer, *p2) <= 1
    finally:
        viewer.close_document()


def test_real_line_preview_no_ring_for_angular_only(
        qtbot, tmp_path, cleared_settings):
    """Req 1: an angular-only move resolves a different endpoint but is
    never a Connect-Lines target, so no ring is painted."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.LINE, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=True))
        qtbot.wait(20)
        # start -> raw is ~-5.7deg: the 15deg constraint moves the endpoint
        # up to horizontal (~y=500) without connecting to anything.
        _press(viewer, *_widget(viewer, 300.0, 500.0))
        _move(viewer, *_widget(viewer, 500.0, 480.0))
        viewer.repaint()

        live = viewer._live_shape
        assert live is not None
        end = _pts(live.points)[-1]
        assert end[1] == pytest.approx(500.0, abs=0.5)      # angle snapped
        assert end[1] != pytest.approx(480.0, abs=5.0)      # moved off raw
        assert viewer._live_shape_snap is None
        px, py = _widget(viewer, *end)
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


def test_real_line_completion_clears_ring_and_commits_displayed_point(
        qtbot, tmp_path, cleared_settings):
    """Req 5/8: releasing after a snapped drag commits exactly the
    displayed coordinate and discards the transient indicator."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw_line_with_tool(viewer, (500.0, 200.0), (500.0, 400.0))
        viewer._interaction.set_tool(_shape_tool(
            ToolType.LINE, color="#e01b24", width_mm=2.0,
            connect_lines=True, snap_angle=False))
        qtbot.wait(20)

        target = (500.0, 400.0)
        raw = _near(viewer, target)
        start = (300.0, 620.0)
        _press(viewer, *_widget(viewer, *start))
        _move(viewer, *_widget(viewer, *raw))
        viewer.repaint()
        px, py = _widget(viewer, *target)
        assert _accent_ring_count(viewer, px, py) >= 6

        _release(viewer, *_widget(viewer, *raw))
        viewer.repaint()

        assert viewer._live_shape is None
        assert viewer._live_shape_snap is None
        lines = [s for s in _shapes(viewer)
                 if s.kind is ShapeKind.LINE and s is not None]
        assert len(lines) == 2
        live_committed = [s for s in lines
                          if _pts(s.points) == pytest.approx(
                              [start, target])]
        assert len(live_committed) == 1
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


# ============ M6.7.2 follow-up: in-progress Polygon self-connect candidates
#
# The unfinished Polygon's *confirmed* vertices and edges become snap
# candidates for later vertices — without committing it to the store.

def test_pending_candidates_expose_confirmed_geometry_only():
    """Req 2.1/2.2/2.3: the transient candidates are exactly the confirmed
    vertices/edges, never the anchor's own edge, the provisional edge, the
    unconfirmed pointer or the fill contour."""
    c = InteractionController(_shape_tool(ToolType.POLYGON, snap_angle=True))
    pid = PageId()
    # No gesture: nothing offered.
    assert c.shape_pending_snap_candidates() == ((), ())
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    # One confirmed vertex == the live anchor: never a target for itself.
    assert c.shape_pending_snap_candidates() == ((), ())
    c.handle_shape_press(pid, 100.0, 0.0)
    c.handle_shape_release(pid, 100.0, 0.0)
    eps, segs = c.shape_pending_snap_candidates()
    assert [(p.x, p.y) for p in eps] == [(0.0, 0.0)]
    assert segs == ()
    c.handle_shape_press(pid, 100.0, 100.0)
    c.handle_shape_release(pid, 100.0, 100.0)
    eps, segs = c.shape_pending_snap_candidates()
    assert [(p.x, p.y) for p in eps] == [(0.0, 0.0), (100.0, 0.0)]
    assert [((a.x, a.y), (b.x, b.y)) for a, b in segs] == \
        [((0.0, 0.0), (100.0, 0.0))]
    # Move the pointer: the unconfirmed pointer is never a candidate.
    c.handle_shape_move(pid, 400.0, 400.0)
    eps2, segs2 = c.shape_pending_snap_candidates()
    assert [(p.x, p.y) for p in eps2] == [(0.0, 0.0), (100.0, 0.0)]
    assert all((400.0, 400.0) != (p.x, p.y) for p in eps2)
    # Only the confirmed edge is offered: the fill contour's closing edge
    # back to the first vertex is never a candidate either.
    assert [((a.x, a.y), (b.x, b.y)) for a, b in segs2] == \
        [((0.0, 0.0), (100.0, 0.0))]
    # Cancel clears every transient candidate.
    c.cancel()
    assert c.shape_pending_snap_candidates() == ((), ())


def test_real_inprogress_polygon_vertex_snaps_to_confirmed_vertex(
        qtbot, tmp_path, cleared_settings):
    """Req 2.1/2.5: a later Polygon vertex acquires the in-progress
    Polygon's own first confirmed vertex as a target — before it is
    clicked, with nothing committed to the store."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        first = (300.0, 300.0)
        second = (500.0, 300.0)
        _click(viewer, first)
        _click(viewer, second)

        # The unfinished polygon offers its confirmed geometry (transient),
        # and is not in the store.
        eps, _segs = viewer._interaction.shape_pending_snap_candidates()
        assert [(p.x, p.y) for p in eps] == [first]
        assert [s for s in _shapes(viewer)
                if s.kind is ShapeKind.POLYGON] == []

        _hover(viewer, *_widget(viewer, *_near(viewer, first)))
        viewer.repaint()
        preview = viewer._live_shape_preview
        assert preview is not None
        assert preview.snap_kind == "endpoint"
        assert preview.snap_target == pytest.approx(first)
        # Provisional edge + fill follow the same resolved endpoint.
        assert _pts(preview.segment) == [second, first]
        assert _pts(preview.fill)[-1] == pytest.approx(first)
        assert _snap_xy(viewer._live_shape_snap) == pytest.approx(first)
        px, py = _widget(viewer, *first)
        assert _accent_ring_count(viewer, px, py) >= 6
    finally:
        viewer.close_document()


def test_real_inprogress_polygon_vertex_snaps_to_confirmed_edge(
        qtbot, tmp_path, cleared_settings):
    """Req 2.2/2.5: a confirmed *edge* of the same unfinished Polygon is
    eligible for intersection snapping before the vertex is clicked."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (300.0, 300.0))      # v0
        _click(viewer, (500.0, 300.0))      # v1 -> confirmed edge v0-v1
        _click(viewer, (300.0, 100.0))      # v2 (anchor); 225deg from v1
        # Hover just below the confirmed edge: the ray from the anchor
        # crosses it at ~(359, 300), within tolerance.
        _hover(viewer, *_widget(viewer, 360.0, 303.0))
        viewer.repaint()
        preview = viewer._live_shape_preview
        assert preview is not None
        assert preview.snap_kind == "intersection"
        assert preview.snap_target[1] == pytest.approx(300.0, abs=1.0)
        assert 300.0 < preview.snap_target[0] < 500.0
        assert _pts(preview.segment)[-1] == pytest.approx(preview.snap_target)
        px, py = _widget(viewer, *preview.snap_target)
        assert _accent_ring_count(viewer, px, py) >= 4
    finally:
        viewer.close_document()


def test_real_inprogress_polygon_provisional_edge_cannot_self_snap(
        qtbot, tmp_path, cleared_settings):
    """Req 2.3/2.8: the live anchor / provisional edge is never a snap
    target for itself — hovering onto it keeps the raw pointer, paints no
    ring, and a click adds no vertex at the anchor."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (300.0, 300.0))
        _click(viewer, (500.0, 300.0))      # anchor = provisional edge origin
        _hover(viewer, *_widget(viewer, 500.0, 305.0))      # onto the anchor
        viewer.repaint()
        preview = viewer._live_shape_preview
        assert preview is not None
        assert viewer._live_shape_snap is None
        assert preview.snap_target == ()
        assert _pts(preview.segment) == [(500.0, 300.0), (500.0, 305.0)]
        assert _pts(preview.vertices) == [(300.0, 300.0), (500.0, 300.0)]
        px, py = _widget(viewer, 500.0, 300.0)
        assert _accent_ring_count(viewer, px, py) <= 1
        # A click there adds the raw pointer, never a duplicate anchor.
        _click(viewer, (500.0, 305.0))
        assert _pts(viewer._live_shape.points)[-1] == pytest.approx(
            (500.0, 305.0))
    finally:
        viewer.close_document()


def test_real_inprogress_polygon_snap_off_ignores_candidates(
        qtbot, tmp_path, cleared_settings):
    """Req: Snap-to-15° OFF disables Connect Lines entirely, so even the
    in-progress Polygon's own confirmed vertices are not targets."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=False))
        qtbot.wait(20)
        _click(viewer, (300.0, 300.0))
        _click(viewer, (500.0, 300.0))
        off = _near(viewer, (300.0, 300.0))
        _hover(viewer, *_widget(viewer, *off))
        viewer.repaint()
        preview = viewer._live_shape_preview
        assert preview is not None
        assert preview.snap_target == ()
        assert viewer._live_shape_snap is None
        assert _pts(preview.segment)[-1] == pytest.approx(off, abs=0.5)
        px, py = _widget(viewer, 300.0, 300.0)
        assert _accent_ring_count(viewer, px, py) <= 1
    finally:
        viewer.close_document()


def test_real_inprogress_polygon_confirm_matches_last_previewed_snap(
        qtbot, tmp_path, cleared_settings):
    """Req 2.6/2.8: the vertex confirmed from a snapped hover lands exactly
    on the last previewed endpoint — no jump, no early close, no anchor
    duplicate."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (300.0, 300.0))      # v0
        _click(viewer, (400.0, 300.0))      # v1 (candidate, not the anchor)
        _click(viewer, (400.0, 500.0))      # v2 (anchor); 90deg from v1

        off = _near(viewer, (400.0, 300.0))
        _hover(viewer, *_widget(viewer, *off))
        shown = _pts(viewer._live_shape_preview.segment)[-1]
        assert shown == pytest.approx((400.0, 300.0))

        _click(viewer, off)                 # confirm the snapped vertex
        assert _pts(viewer._live_shape.points)[-1] == shown
        assert viewer._live_shape.close_path is False
        assert [s for s in _shapes(viewer)
                if s.kind is ShapeKind.POLYGON] == []
    finally:
        viewer.close_document()


def test_real_inprogress_candidates_cleared_on_completion_and_cancel(
        qtbot, tmp_path, cleared_settings):
    """Req: completion and cancellation both drop the transient candidate
    geometry along with the live preview + indicator."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(
            ToolType.POLYGON, color="#e01b24", width_mm=2.0, snap_angle=True))
        qtbot.wait(20)
        _click(viewer, (300.0, 300.0))
        _click(viewer, (500.0, 300.0))
        assert viewer._interaction.shape_pending_snap_candidates() != ((), ())

        _click(viewer, (500.0, 500.0))
        _dbl(viewer, *_widget(viewer, 500.0, 500.0))
        assert len([s for s in _shapes(viewer)
                    if s.kind is ShapeKind.POLYGON]) == 1
        assert viewer._interaction.shape_pending_snap_candidates() == ((), ())
        assert viewer._live_shape_preview is None
        assert viewer._live_shape_snap is None

        # A fresh gesture, then cancel: candidates are discarded again.
        _click(viewer, (200.0, 200.0))
        _click(viewer, (300.0, 200.0))
        assert viewer._interaction.shape_pending_snap_candidates() != ((), ())
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._interaction.shape_pending_snap_candidates() == ((), ())
        assert viewer._live_shape_snap is None
    finally:
        viewer.close_document()


# ================================= stylus (tablet) polygon hover — M6.8.2


def _tablet_click(viewer, lx, ly, page_index=0, pressure=0.7):
    """A genuine stylus tip contact at a page-local point (press+release).

    The tip press is the only stylus event that may place a vertex; the
    release never does (Polygon commits on press).
    """
    wx, wy = _widget(viewer, lx, ly, page_index)
    _tablet(viewer, QEvent.Type.TabletPress, wx, wy, pressure=pressure,
            buttons=Qt.MouseButton.LeftButton)
    _tablet(viewer, QEvent.Type.TabletRelease, wx, wy, pressure=pressure,
            buttons=Qt.MouseButton.LeftButton)
    return wx, wy


def test_polygon_preview_follows_tablet_hover_without_tip_contact(
        qtbot, tmp_path, cleared_settings):
    """#1: with the tip lifted, a stylus hover updates the live Polygon
    preview (provisional edge + fill) exactly like a mouse hover — no
    contact, no vertex.
    """
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(ToolType.POLYGON))
        qtbot.wait(20)
        _tablet_click(viewer, 100.0, 100.0)         # anchor A (tip press)

        # Tip lifted: TabletMove with zero pressure and no buttons.
        _tablet_hover(viewer, 400.0, 300.0)
        viewer.repaint()

        preview = viewer._live_shape_preview
        assert preview is not None
        assert _pts(preview.segment) == [(100.0, 100.0), (400.0, 300.0)]
        assert _pts(preview.fill)[-1] == (400.0, 300.0)
        # The tip-up hover confirmed no vertex.
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0)]
        assert viewer._live_shape.close_path is False

        # A second tip-up hover keeps tracking continuously.
        _tablet_hover(viewer, 250.0, 500.0)
        updated = viewer._live_shape_preview
        assert _pts(updated.segment) == [(100.0, 100.0), (250.0, 500.0)]
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0)]

        # And between two placed vertices it keeps tracking the tip-up pen.
        _tablet_click(viewer, 250.0, 500.0)         # place B
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0),
                                                   (250.0, 500.0)]
        _tablet_hover(viewer, 420.0, 640.0)
        assert _pts(viewer._live_shape_preview.segment) == [
            (250.0, 500.0), (420.0, 640.0)]
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0),
                                                   (250.0, 500.0)]
    finally:
        viewer.close_document()


def test_tablet_hover_does_not_place_vertex_or_mutate_store_history(
        qtbot, tmp_path, cleared_settings):
    """#2: tip-up hover places no vertex, adds no annotation, no undo entry."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(ToolType.POLYGON))
        qtbot.wait(20)
        _tablet_click(viewer, 100.0, 100.0)         # anchor A
        before_store = tuple(_shapes(viewer))
        before_undo = viewer.document_history.can_undo()
        before_redo = viewer.document_history.can_redo()

        for lx, ly in ((200.0, 200.0), (300.0, 300.0), (400.0, 350.0)):
            _tablet_hover(viewer, lx, ly)

        assert _pts(viewer._live_shape.points) == [(100.0, 100.0)]
        assert tuple(_shapes(viewer)) == before_store
        assert viewer.document_history.can_undo() == before_undo
        assert viewer.document_history.can_redo() == before_redo
    finally:
        viewer.close_document()


def test_tablet_tip_press_places_exactly_one_vertex(
        qtbot, tmp_path, cleared_settings):
    """#3: a genuine tip press places exactly one vertex; the release adds
    none."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(ToolType.POLYGON))
        qtbot.wait(20)
        _tablet_click(viewer, 100.0, 100.0)         # A
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0)]

        _tablet_click(viewer, 300.0, 300.0)         # B (one press = one vertex)
        assert _pts(viewer._live_shape.points) == [(100.0, 100.0),
                                                   (300.0, 300.0)]
        # Still provisional: nothing committed to the store.
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()


def test_tablet_hover_and_synth_mouse_update_preview_once(
        qtbot, tmp_path, cleared_settings):
    """#11: a stylus hover and Qt's synthesized mouse mirror must not
    double-process the live preview (no duplicate update)."""
    _w, viewer = _open_window(qtbot, tmp_path)
    try:
        viewer._interaction.set_tool(_shape_tool(ToolType.POLYGON))
        qtbot.wait(20)
        _tablet_click(viewer, 100.0, 100.0)

        wx, wy = _widget(viewer, 400.0, 300.0)
        moves = []
        orig = viewer._interaction.handle_shape_move

        def counting(pid, x, y, *a, **k):
            moves.append((x, y))
            return orig(pid, x, y, *a, **k)

        viewer._interaction.handle_shape_move = counting

        _tablet(viewer, QEvent.Type.TabletMove, wx, wy)   # stylus hover
        # Qt's synthesized mouse fallback for the same hover position.
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(wx, wy),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))

        assert len(moves) == 1                    # updated exactly once
        assert _pts(viewer._live_shape_preview.segment)[-1] == (400.0, 300.0)
    finally:
        viewer.close_document()
