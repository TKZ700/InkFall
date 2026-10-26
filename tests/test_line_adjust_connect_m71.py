"""M7.1 Phase B: Connect-Lines snapping for live line endpoint adjustment.

Decision D1=A (always-on during live adjustment) and D2 (endpoint +
intersection, Polygon-style). The tests prove that the existing
Connect-Lines machinery is reused end to end: the same
``ShapeSnapContext`` candidates, the same shared ``snap_hit`` target rule
the shape tools use, the same screen-pixel tolerance (zoom-correct), the
same circular indicator renderer and page-local coordinate mapping, and
the same single-commit/undo pipeline.

Coverage: endpoint snapping, intersection snapping, exact target
alignment, indicator appearance/following/clearing, no-drift when
leaving the tolerance, zoom and page-rotation handling, cancel/release
cleanup, pure shared-resolver/geometry helpers, and the untouched
Phase A behavior when no target resolves.
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QInputDevice, QMouseEvent, QPointingDevice
from PySide6.QtGui import QTabletEvent

from annotations.interaction import ShapeSnapContext, snap_hit
from annotations.models import Shape, ShapeKind
from annotations.shape_adjust import begin_line_adjustment, snapped_line
from annotations.shape_recognize import ShapeCandidate
from core.types import Point
from tests.helpers import make_intrinsic_pdf, make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_PAGE_WIDTH = 600.0
_PAGE_HEIGHT = 800.0

_pen_device = None


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
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


@pytest.fixture()
def make_viewer(qtbot, tmp_path, cleared_settings):
    """Factory: open one PDF in its own viewer with clean shutdown."""
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    created = []

    def _make(pdf_path):
        instance = PdfViewer(DocumentManager())
        instance.resize(1100, 750)
        instance.show()
        qtbot.addWidget(instance)
        qtbot.wait(80)
        assert instance.open_document(str(pdf_path)) is True
        qtbot.wait(120)
        created.append(instance)
        return instance

    yield _make
    for instance in created:
        try:
            instance.shutdown()
        except Exception:
            pass
        try:
            instance.close()
        except Exception:
            pass


@pytest.fixture()
def viewer(make_viewer, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "connect.pdf"),
                         page_width=_PAGE_WIDTH, page_height=_PAGE_HEIGHT)
    return make_viewer(pdf)


# ------------------------------------------------------------ input helpers

def _tablet_device():
    global _pen_device
    if _pen_device is None:
        _pen_device = QPointingDevice(
            "test-pen", 1, QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Pen,
            QInputDevice.Capability.Position
            | QInputDevice.Capability.Pressure
            | QInputDevice.Capability.MouseEmulation,
            1, 1)
    return _pen_device


def _tablet(etype, x, y, pressure=0.5):
    button = (Qt.MouseButton.LeftButton
              if etype == QEvent.Type.TabletPress
              else Qt.MouseButton.NoButton)
    buttons = (Qt.MouseButton.NoButton
               if etype == QEvent.Type.TabletRelease
               else Qt.MouseButton.LeftButton)
    return QTabletEvent(etype, _tablet_device(), QPointF(x, y),
                        QPointF(x, y), pressure, 0.0, 0.0, 0.0, 0.0, 0,
                        Qt.KeyboardModifier.NoModifier, button, buttons)


def _mouse_press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_move(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


# ------------------------------------------------------ page <-> widget map

def _page_to_widget(viewer, x, y, page_index=0):
    entry = viewer._pipeline.layout.entry(page_index)
    mapped = viewer._page_origin_for(entry.page_id.value)
    assert mapped is not None
    ox, oy, factor = mapped
    return (ox + float(x) * factor, oy + float(y) * factor)


def _page_point_of_widget(viewer, wx, wy):
    mapped = viewer._event_to_page_pt(wx, wy)
    assert mapped is not None
    return (float(mapped[1]), float(mapped[2]))


# ------------------------------------------------------------- interaction

def _pen_tool():
    return ToolInstance(kind=ToolType.PEN, name="Pen", color="#112233",
                        width_mm=0.7, opacity=1.0)


def _line_tool():
    return ToolInstance(kind=ToolType.LINE, name="Line", color="#2b6cff",
                        width_mm=1.5, opacity=1.0)


def _straight(start=(100.0, 200.0), end=(400.0, 200.0), steps=12):
    return [(start[0] + (end[0] - start[0]) * i / steps,
             start[1] + (end[1] - start[1]) * i / steps)
            for i in range(steps + 1)]


def _commit_line_with_tool(viewer, p0, p1):
    """Create a committed Line through the real Line-tool gesture."""
    viewer._interaction.set_tool(_line_tool())
    a = _page_to_widget(viewer, *p0)
    b = _page_to_widget(viewer, *p1)
    _mouse_press(viewer, a[0], a[1])
    _mouse_move(viewer, b[0], b[1])
    _mouse_release(viewer, b[0], b[1])
    shapes = [a for a in viewer._document.annotations.page(0)
              if isinstance(a, Shape)]
    assert len(shapes) == 1, "the snap target line did not commit"
    return shapes[0]


def _draw_stylus(viewer, points, pressure=0.6):
    first = _page_to_widget(viewer, *points[0])
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, first[0], first[1],
                               0.5))
    for point in points[1:]:
        wx, wy = _page_to_widget(viewer, *point)
        viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, wx, wy, pressure))


def _move_stylus(viewer, point, pressure=0.6):
    wx, wy = _page_to_widget(viewer, *point)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, wx, wy, pressure))
    return (wx, wy)


def _release_stylus(viewer, point):
    wx, wy = _page_to_widget(viewer, *point)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, wx, wy, 0.0))
    return (wx, wy)


def _arm(viewer):
    viewer._on_shape_dwell()
    state = viewer._shape_morph
    assert state is not None, "recognition did not arm a morph"
    return state


def _activate(viewer, pointer, previous_moves=()):
    state = viewer._shape_morph
    assert state is not None
    for point in tuple(previous_moves) + (pointer,):
        _move_stylus(viewer, point)
        if state.get("adjust") is not None:
            return state, state["adjust"]["adjustment"]
    raise AssertionError("line adjustment did not activate")


def _moving(state):
    adjustment = state["adjust"]["adjustment"]
    point = state["candidate"].contour[adjustment.moving_index]
    return (float(point[0]), float(point[1]))


def _anchor(state):
    adjustment = state["adjust"]["adjustment"]
    point = state["candidate"].contour[1 - adjustment.moving_index]
    return (float(point[0]), float(point[1]))


def _pairs_close(actual, expected):
    return (tuple(actual[0]) == pytest.approx(tuple(expected[0]))
            and tuple(actual[1]) == pytest.approx(tuple(expected[1])))


def _shapes(viewer):
    return [a for a in viewer._document.annotations.page(0)
            if isinstance(a, Shape)]


# ------------------------------------------------------- indicator pixels

def _is_accent(color, accent) -> bool:
    """Near the theme accent, excluding page white and the blue target line."""
    return (abs(color.red() - accent.red()) <= 26
            and abs(color.green() - accent.green()) <= 26
            and abs(color.blue() - accent.blue()) <= 26
            and not (color.red() > 240 and color.green() > 240
                     and color.blue() > 240))


def _accent_ring_count(viewer, px, py) -> int:
    """Accent pixels in the ring band (5..8 px) around a widget point."""
    image = viewer.grab().toImage()
    try:
        accent = QColor(viewer._tool_tokens().accent)
    except Exception:
        accent = QColor("#8B93E0")
    count = 0
    for dy in range(-9, 10):
        for dx in range(-9, 10):
            distance = (dx * dx + dy * dy) ** 0.5
            if 5.0 <= distance <= 8.0:
                color = image.pixelColor(int(px + dx), int(py + dy))
                if _is_accent(color, accent):
                    count += 1
    return count


# ------------------------------------------------- pure shared machinery

def test_shared_snap_resolver_endpoint_tolerance_and_priority():
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 50.0),),
                           segments=((Point(150.0, 0.0),
                                      Point(150.0, 200.0)),), tol_pt=10.0)
    hit = snap_hit(ctx, 104.0, 52.0, anchor=(0.0, 100.0),
                   endpoints=True, intersections=True)
    assert hit == ("endpoint", (100.0, 50.0))
    # Endpoint snapping wins even when an intersection also resolves.
    assert snap_hit(ctx, 104.0, 52.0, anchor=(0.0, 100.0),
                    endpoints=True, intersections=True)[0] == "endpoint"
    # Outside the tolerance: nothing.
    assert snap_hit(ctx, 130.0, 52.0, anchor=(0.0, 100.0),
                    endpoints=True, intersections=True) is None
    # Endpoints disabled: the endpoint is not eligible.
    assert snap_hit(ctx, 104.0, 52.0, anchor=(0.0, 100.0),
                    endpoints=False, intersections=True) is None


def test_shared_snap_resolver_intersection_needs_anchor():
    segment = (Point(150.0, 0.0), Point(150.0, 200.0))
    ctx = ShapeSnapContext(segments=(segment,), tol_pt=10.0)
    hit = snap_hit(ctx, 152.0, 100.0, anchor=(0.0, 100.0),
                   endpoints=False, intersections=True)
    assert hit is not None
    kind, target = hit
    assert kind == "intersection"
    assert target[0] == pytest.approx(150.0)
    assert target[1] == pytest.approx(100.0)
    # Without an anchor there is no segment to intersect.
    assert snap_hit(ctx, 152.0, 100.0, anchor=None,
                    endpoints=False, intersections=True) is None
    # Intersections disabled: no hit.
    assert snap_hit(ctx, 152.0, 100.0, anchor=(0.0, 100.0),
                    endpoints=False, intersections=False) is None


def test_shared_snap_resolver_pending_and_disabled_context():
    ctx = ShapeSnapContext(tol_pt=10.0,
                           pending_endpoints=(Point(300.0, 300.0),))
    assert snap_hit(ctx, 302.0, 301.0, endpoints=True) == (
        "endpoint", (300.0, 300.0))
    assert snap_hit(ShapeSnapContext(tol_pt=0.0), 0.0, 0.0,
                    endpoints=True) is None
    assert snap_hit(None, 0.0, 0.0, endpoints=True) is None


def test_snapped_line_places_endpoint_exactly_on_target():
    candidate = ShapeCandidate(kind="line",
                               contour=((10.0, 20.0), (210.0, 20.0)),
                               closed=False, confidence=0.7)
    adjustment = begin_line_adjustment(candidate, (210.0, 20.0))
    assert adjustment is not None
    snapped = snapped_line(adjustment, (150.0, 80.0))
    assert snapped is not None
    assert snapped.contour == ((10.0, 20.0), (150.0, 80.0))
    assert snapped.confidence == pytest.approx(0.7)
    # Same minimum-length rule as ordinary adjustment.
    assert snapped_line(adjustment, (12.0, 20.0)) is None
    assert snapped_line(adjustment, None) is None


# ---------------------------------------------------- endpoint integration

def test_endpoint_snap_aligns_moving_endpoint_and_commits(viewer):
    target_line = _commit_line_with_tool(viewer, (450.0, 150.0),
                                         (450.0, 350.0))
    target = (target_line.points[0].x, target_line.points[0].y)
    assert target == pytest.approx((450.0, 150.0))

    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)
    assert state["candidate"].kind == "line"

    _move_stylus(viewer, (445.0, 155.0))
    assert state.get("adjust") is not None
    snap = state["snap"]
    assert snap is not None
    assert snap[2] == "endpoint"
    assert (snap[0], snap[1]) == pytest.approx((450.0, 150.0), abs=0.01)
    # The moving endpoint coincides exactly with the resolved target.
    assert _moving(state) == pytest.approx((snap[0], snap[1]), abs=1e-9)
    # The fixed anchor is untouched, and the store is never written.
    adjustment = state["adjust"]["adjustment"]
    assert _anchor(state) == pytest.approx(adjustment.anchor, abs=1e-9)
    assert len(_shapes(viewer)) == 1

    # Staying inside the tolerance keeps the endpoint exactly on target.
    _move_stylus(viewer, (451.0, 152.0))
    assert state["snap"] is not None
    assert _moving(state) == pytest.approx((snap[0], snap[1]), abs=1e-9)

    preview = tuple(state["candidate"].contour)
    _release_stylus(viewer, (451.0, 152.0))
    shapes = _shapes(viewer)
    assert len(shapes) == 2
    committed = shapes[-1]
    assert committed.kind is ShapeKind.LINE
    assert _pairs_close(
        ((committed.points[0].x, committed.points[0].y),
         (committed.points[1].x, committed.points[1].y)), preview)
    # Exactly one undoable step for the adjusted line.
    history = viewer.document_history
    assert history is not None and history.can_undo()
    history.undo()
    assert len(_shapes(viewer)) == 1
    history.redo()
    assert len(_shapes(viewer)) == 2


def test_intersection_snap_aligns_moving_endpoint(viewer):
    _commit_line_with_tool(viewer, (450.0, 100.0), (450.0, 400.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight(start=(100.0, 300.0),
                                   end=(400.0, 300.0)))
    state = _arm(viewer)
    _move_stylus(viewer, (455.0, 305.0))
    assert state.get("adjust") is not None

    snap = state["snap"]
    assert snap is not None
    assert snap[2] == "intersection"
    # The target is the crossing with the committed vertical segment.
    assert snap[0] == pytest.approx(450.0, abs=0.02)
    pointer = _page_point_of_widget(viewer, *_page_to_widget(viewer,
                                                             455.0, 305.0))
    anchor = state["adjust"]["adjustment"].anchor
    expected_y = anchor[1] + (pointer[1] - anchor[1]) * (
        (450.0 - anchor[0]) / (pointer[0] - anchor[0]))
    assert snap[1] == pytest.approx(expected_y, abs=0.05)
    assert _moving(state) == pytest.approx((snap[0], snap[1]), abs=1e-9)

    preview = tuple(state["candidate"].contour)
    _release_stylus(viewer, (455.0, 305.0))
    shapes = _shapes(viewer)
    assert len(shapes) == 2
    committed = shapes[-1]
    assert _pairs_close(
        ((committed.points[0].x, committed.points[0].y),
         (committed.points[1].x, committed.points[1].y)), preview)


# --------------------------------------------------- indicator appearance

def test_snap_ring_appears_follows_and_clears(viewer):
    _commit_line_with_tool(viewer, (450.0, 150.0), (450.0, 350.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)

    _move_stylus(viewer, (445.0, 155.0))
    snap = state["snap"]
    assert snap is not None and snap[2] == "endpoint"
    target_widget = _page_to_widget(viewer, snap[0], snap[1])
    assert _accent_ring_count(viewer, *target_widget) > 3

    # Leaving the tolerance clears the indicator and restores the ordinary
    # absolute pointer mapping (no residual target and no drift).
    regular = (400.0, 260.0)
    _move_stylus(viewer, regular)
    assert state["snap"] is None
    assert _accent_ring_count(viewer, *target_widget) == 0
    adjustment = state["adjust"]["adjustment"]
    wx, wy = _page_to_widget(viewer, *regular)
    px, py = _page_point_of_widget(viewer, wx, wy)
    expected = (adjustment.moving[0] + px - adjustment.origin[0],
                adjustment.moving[1] + py - adjustment.origin[1])
    assert _moving(state) == pytest.approx(expected, abs=1e-9)

    first = tuple(state["candidate"].contour)
    _move_stylus(viewer, (350.0, 340.0))
    _move_stylus(viewer, regular)
    assert _pairs_close(state["candidate"].contour, first)

    # Re-entering the tolerance restores the shared ring at the target.
    _move_stylus(viewer, (446.0, 152.0))
    assert state["snap"] is not None
    assert _accent_ring_count(viewer, *target_widget) > 3


def test_snap_indicator_follows_and_identifies_the_nearest_target(viewer):
    _commit_line_with_tool(viewer, (450.0, 150.0), (450.0, 350.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)

    _move_stylus(viewer, (445.0, 155.0))
    first = state["snap"]
    assert first is not None and first[2] == "endpoint"
    assert (first[0], first[1]) == pytest.approx((450.0, 150.0), abs=0.01)
    first_widget = _page_to_widget(viewer, first[0], first[1])
    assert _accent_ring_count(viewer, *first_widget) > 3

    _move_stylus(viewer, (446.0, 346.0))
    second = state["snap"]
    assert second is not None and second[2] == "endpoint"
    assert (second[0], second[1]) == pytest.approx((450.0, 350.0), abs=0.01)
    assert _moving(state) == pytest.approx((second[0], second[1]), abs=1e-9)
    second_widget = _page_to_widget(viewer, second[0], second[1])
    assert _accent_ring_count(viewer, *second_widget) > 3
    # The previous target's ring was erased: only the nearest target is
    # marked at any time.
    assert _accent_ring_count(viewer, *first_widget) <= 1


def test_snap_target_too_short_for_anchor_keeps_last_valid_preview(viewer):
    # An existing endpoint exactly on the recognized line's fixed anchor.
    _commit_line_with_tool(viewer, (100.0, 200.0), (100.0, 320.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)

    _move_stylus(viewer, (450.0, 200.0))     # far: valid, no target
    assert state.get("adjust") is not None
    assert state["snap"] is None
    last_valid = tuple(state["candidate"].contour)

    # Within tolerance of the target that coincides with the anchor: a
    # snapped frame would be a zero-length line, so the last valid preview
    # is kept (minimum-length protection) and nothing degenerate commits.
    _move_stylus(viewer, (106.0, 204.0))
    assert state["snap"] is None
    assert _pairs_close(state["candidate"].contour, last_valid)

    _release_stylus(viewer, (106.0, 204.0))
    shapes = _shapes(viewer)
    assert len(shapes) == 2
    committed = shapes[-1]
    assert _pairs_close(
        ((committed.points[0].x, committed.points[0].y),
         (committed.points[1].x, committed.points[1].y)), last_valid)


def test_cancel_mid_snap_commits_nothing_and_clears_ring(viewer):
    _commit_line_with_tool(viewer, (450.0, 150.0), (450.0, 350.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)
    _move_stylus(viewer, (445.0, 155.0))
    snap = state["snap"]
    assert snap is not None
    target_widget = _page_to_widget(viewer, snap[0], snap[1])
    assert _accent_ring_count(viewer, *target_widget) > 3

    viewer._apply_intent(viewer._interaction.cancel())
    assert viewer._shape_morph is None
    assert viewer._live_stroke is None
    assert len(_shapes(viewer)) == 1       # only the pre-existing target
    assert _accent_ring_count(viewer, *target_widget) == 0
    # Cancellation added no history entry: one undo removes only the
    # pre-existing target line committed before the gesture.
    history = viewer.document_history
    assert history is not None and history.can_undo()
    history.undo()
    assert _shapes(viewer) == []


# ----------------------------------------------------- zoom / rotation

def test_zoom_scales_tolerance_and_keeps_exact_snap(viewer, qtbot):
    _commit_line_with_tool(viewer, (450.0, 150.0), (450.0, 350.0))
    page_id = next(iter(viewer._document.annotations.page(0))).page_id
    tol_1x = viewer._shape_snap_context(page_id).tol_pt
    assert tol_1x > 0.0

    viewer.set_zoom(2.0)
    qtbot.wait(80)
    tol_2x = viewer._shape_snap_context(page_id).tol_pt
    assert tol_2x == pytest.approx(tol_1x / 2.0, rel=0.1)

    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)
    # 3.6 page pt: inside the halved tolerance -> snaps exactly.
    _move_stylus(viewer, (448.0, 152.0))
    assert state["snap"] is not None
    assert state["snap"][2] == "endpoint"
    assert _moving(state) == pytest.approx(
        (state["snap"][0], state["snap"][1]), abs=1e-9)
    # ~13 page pt away: outside the halved tolerance at this zoom.
    _move_stylus(viewer, (458.0, 160.0))
    assert state["snap"] is None


def test_page_rotation_snap_and_ring(make_viewer, tmp_path):
    pdf = make_intrinsic_pdf(str(tmp_path / "rotated.pdf"), rotations=(90,))
    viewer = make_viewer(pdf)
    _commit_line_with_tool(viewer, (450.0, 150.0), (450.0, 350.0))
    viewer._interaction.set_tool(_pen_tool())
    _draw_stylus(viewer, _straight())
    state = _arm(viewer)
    _move_stylus(viewer, (445.0, 155.0))
    snap = state["snap"]
    assert snap is not None and snap[2] == "endpoint"
    assert _moving(state) == pytest.approx((snap[0], snap[1]), abs=1e-9)
    # The indicator is mapped through the same page-local transform, so it
    # must appear at the rotated page's target position.
    target_widget = _page_to_widget(viewer, snap[0], snap[1])
    assert _accent_ring_count(viewer, *target_widget) > 3
