"""M7.1 Phase A: live line-endpoint adjustment through the viewer.

Covers the stylus and mouse event paths, absolute (drift-free) tracking,
the immutable anchor, degenerate-frame rejection, exactly-one commit and
one undo step, cancellation/tool switching, reduced motion, the
untouched unadjusted recognition path, ordinary freehand drawing, and the
preserved pre-Phase-A behaviour of closed shapes and highlighters.

Recognition is triggered by calling the dwell handler directly: the
320 ms timer's own contract is unchanged and timing it in a test would
only add wall-clock flake. Everything after recognition runs through the
real pointer event paths and the real commit pipeline.

The fixture calls ``viewer.shutdown()`` explicitly: without it the page
navigator's process decoder keeps spawned children alive past the test
session in this headless environment.
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QInputDevice, QMouseEvent, QPointingDevice, QTabletEvent

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from tests.helpers import make_probe_pdf
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_PAGE_WIDTH = 600.0
_PAGE_HEIGHT = 800.0

_pen_device = None


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
def viewer(qtbot, tmp_path, cleared_settings):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    instance = PdfViewer(DocumentManager())
    instance.resize(1100, 750)
    instance.show()
    qtbot.addWidget(instance)
    qtbot.wait(100)
    pdf = make_probe_pdf(str(tmp_path / "line-adjust.pdf"),
                         page_width=_PAGE_WIDTH, page_height=_PAGE_HEIGHT)
    assert instance.open_document(pdf) is True
    qtbot.wait(150)
    yield instance
    try:
        instance.shutdown()
    except Exception:
        pass
    try:
        instance.close()
    except Exception:
        pass


# -- synthetic input --------------------------------------------------------

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


# -- page <-> widget coordinates -------------------------------------------

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


# -- viewer actions ---------------------------------------------------------

def _select_tool(viewer, kind, **properties):
    slot = viewer.tool_store.add_slot(kind, name=str(kind.value),
                                      **properties)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(slot)
    return slot


def _select_pen(viewer):
    return _select_tool(viewer, ToolType.PEN, color="#112233", width_mm=0.7)


def _move_stylus(viewer, point, pressure=0.6):
    wx, wy = _page_to_widget(viewer, *point)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, wx, wy, pressure))
    return (wx, wy)


def _draw_stylus(viewer, points, pressure=0.6):
    first = _page_to_widget(viewer, *points[0])
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, first[0], first[1],
                               0.5))
    for point in points[1:]:
        _move_stylus(viewer, point, pressure)


def _release_stylus(viewer, point):
    wx, wy = _page_to_widget(viewer, *point)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, wx, wy, 0.0))
    return (wx, wy)


def _arm(viewer):
    viewer._on_shape_dwell()
    state = viewer._shape_morph
    assert state is not None, "recognition did not arm a morph"
    return state


def _activate(viewer, moves=((420.0, 150.0), (440.0, 170.0),
                             (470.0, 220.0))):
    state = viewer._shape_morph
    assert state is not None
    for point in moves:
        _move_stylus(viewer, point)
        if state.get("adjust") is not None:
            return state, state["adjust"]["adjustment"]
    raise AssertionError("line adjustment did not activate")


def _moving(candidate, adjustment):
    point = candidate.contour[adjustment.moving_index]
    return (float(point[0]), float(point[1]))


def _anchor(candidate, adjustment):
    point = candidate.contour[1 - adjustment.moving_index]
    return (float(point[0]), float(point[1]))


def _history(viewer):
    return viewer._history_for(viewer._document, create=False)


def _pairs_close(actual, expected):
    """True when two two-point contours match elementwise (approx)."""
    return (tuple(actual[0]) == pytest.approx(tuple(expected[0]))
            and tuple(actual[1]) == pytest.approx(tuple(expected[1])))


# -- geometry fixtures ------------------------------------------------------

def _straight_line(start=(100.0, 150.0), end=(400.0, 150.0), steps=12):
    return [(start[0] + (end[0] - start[0]) * i / steps,
             start[1] + (end[1] - start[1]) * i / steps)
            for i in range(steps + 1)]


def _bowed_line(bow=6.0, steps=16):
    points = []
    for i in range(steps + 1):
        t = i / steps
        points.append((100.0 + 400.0 * t,
                       150.0 + bow * math.sin(math.pi * t)))
    return points


def _square(start=(150.0, 150.0), width=300.0, height=250.0, per_side=12):
    x0, y0 = start
    points = [(x0, y0)]
    for i in range(1, per_side + 1):
        points.append((x0 + width * i / per_side, y0))
    for i in range(1, per_side + 1):
        points.append((x0 + width, y0 + height * i / per_side))
    for i in range(1, per_side + 1):
        points.append((x0 + width - width * i / per_side, y0 + height))
    for i in range(1, per_side + 1):
        points.append((x0, y0 + height - height * i / per_side))
    return points


# -- live adjustment --------------------------------------------------------

def test_stylus_endpoint_tracks_pointer_and_anchor_stays_fixed(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    state = _arm(viewer)
    assert state["candidate"].kind == "line"
    assert state["adjust"] is None

    state, adjustment = _activate(viewer)
    candidate = state["candidate"]
    assert candidate.kind == "line"
    assert candidate.closed is False

    # Preview-only movement never touches the document or its history.
    assert list(viewer._document.annotations.page(0)) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()

    for pointer in ((500.0, 240.0), (520.0, 300.0), (560.0, 160.0)):
        wx, wy = _move_stylus(viewer, pointer)
        px, py = _page_point_of_widget(viewer, wx, wy)
        candidate = state["candidate"]
        expected = (adjustment.moving[0] + px - adjustment.origin[0],
                    adjustment.moving[1] + py - adjustment.origin[1])
        assert _moving(candidate, adjustment) == pytest.approx(expected)
        assert _anchor(candidate, adjustment) == pytest.approx(
            adjustment.anchor)


def test_updates_are_absolute_and_origin_restores_base_geometry(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    _arm(viewer)
    state, adjustment = _activate(viewer)

    first_pointer = (480.0, 220.0)
    _move_stylus(viewer, first_pointer)
    first = tuple(state["candidate"].contour)
    _move_stylus(viewer, (400.0, 380.0))
    _move_stylus(viewer, first_pointer)
    assert _pairs_close(state["candidate"].contour, first)

    # Back at the recognition origin: the rebased base geometry is restored.
    _move_stylus(viewer, adjustment.origin)
    candidate = state["candidate"]
    assert _moving(candidate, adjustment) == pytest.approx(adjustment.moving)
    assert _anchor(candidate, adjustment) == pytest.approx(adjustment.anchor)


def test_degenerate_frame_keeps_last_valid_preview_and_commits_it(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    _arm(viewer)
    state, adjustment = _activate(viewer)

    _move_stylus(viewer, (520.0, 260.0))
    last_valid = tuple(state["candidate"].contour)

    # Two points from the anchor: shorter than the recognized minimum.
    degenerate = (adjustment.anchor[0] + 2.0, adjustment.anchor[1])
    _move_stylus(viewer, degenerate)
    assert _pairs_close(state["candidate"].contour, last_valid)

    _release_stylus(viewer, degenerate)
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    assert shape.kind is ShapeKind.LINE
    assert (shape.points[0].x, shape.points[0].y) == pytest.approx(
        last_valid[0])
    assert (shape.points[1].x, shape.points[1].y) == pytest.approx(
        last_valid[1])


def test_release_commits_one_line_and_one_undo_step(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    _arm(viewer)
    state, adjustment = _activate(viewer)
    _move_stylus(viewer, (540.0, 270.0))
    expected = tuple(state["candidate"].contour)

    _release_stylus(viewer, expected[adjustment.moving_index])
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    assert shape.kind is ShapeKind.LINE
    assert (shape.points[0].x, shape.points[0].y) == pytest.approx(
        expected[0])
    assert (shape.points[1].x, shape.points[1].y) == pytest.approx(
        expected[1])

    history = viewer.document_history
    assert history is not None
    assert history.can_undo() is True
    assert history.can_redo() is False
    history.undo()
    assert list(viewer._document.annotations.page(0)) == []
    history.redo()
    assert len(list(viewer._document.annotations.page(0))) == 1


def test_adjusted_commit_matches_preview_geometry_and_rebase(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _bowed_line())
    state = _arm(viewer)
    original = tuple(state["candidate"].contour)
    offset = state["preview_offset"]
    assert math.hypot(offset[0], offset[1]) > 1.0, (
        "a bowed line must expose the morph preview offset")

    state, adjustment = _activate(viewer)
    # The anchor is the recognized anchor translated into preview space.
    anchor_index = 1 - adjustment.moving_index
    expected_anchor = (original[anchor_index][0] + offset[0],
                       original[anchor_index][1] + offset[1])
    assert adjustment.anchor == pytest.approx(expected_anchor)

    _move_stylus(viewer, (560.0, 300.0))
    preview = tuple(state["candidate"].contour)
    _release_stylus(viewer, (560.0, 300.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    committed = ((shape.points[0].x, shape.points[0].y),
                 (shape.points[1].x, shape.points[1].y))
    assert _pairs_close(committed, preview)


# -- recognition outcomes that must not change ------------------------------

def test_release_without_movement_commits_the_unadjusted_recognition(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    state = _arm(viewer)
    expected = tuple(state["candidate"].contour)
    assert state["adjust"] is None
    _release_stylus(viewer, (400.0, 150.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    assert shape.kind is ShapeKind.LINE
    committed = ((shape.points[0].x, shape.points[0].y),
                 (shape.points[1].x, shape.points[1].y))
    assert _pairs_close(committed, expected)


def test_sub_epsilon_movement_keeps_the_recognized_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    state = _arm(viewer)
    expected = tuple(state["candidate"].contour)
    _move_stylus(viewer, (403.0, 151.0))  # ~3.6 pt: jitter, not drawing
    assert viewer._shape_morph is state
    assert state["adjust"] is None
    _release_stylus(viewer, (403.0, 151.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    committed = ((shape.points[0].x, shape.points[0].y),
                 (shape.points[1].x, shape.points[1].y))
    assert _pairs_close(committed, expected)


def test_cancel_intent_mid_adjustment_commits_nothing(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    _arm(viewer)
    _activate(viewer)
    viewer._apply_intent(viewer._interaction.cancel())
    assert viewer._shape_morph is None
    assert viewer._live_stroke is None
    assert list(viewer._document.annotations.page(0)) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()


def test_tool_switch_mid_adjustment_commits_nothing(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line())
    _arm(viewer)
    _activate(viewer)
    pan_slot = next(slot for slot in viewer.tool_store.slots
                    if slot.kind == ToolType.PAN)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(pan_slot)
    assert viewer._shape_morph is None
    assert viewer._live_stroke is None
    assert list(viewer._document.annotations.page(0)) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()


def test_closed_shape_movement_enters_adjustment_and_commits_a_shape(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _square())
    state = _arm(viewer)
    assert state["candidate"].closed is True
    assert state["candidate"].kind == "rectangle"
    _move_stylus(viewer, (500.0, 500.0))
    assert viewer._shape_morph is state
    assert state["adjust"] is not None
    _release_stylus(viewer, (500.0, 500.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    assert isinstance(annotations[0], Shape)
    assert annotations[0].kind is ShapeKind.RECTANGLE


def test_highlighter_movement_still_cancels_the_morph(viewer):
    _select_tool(viewer, ToolType.HIGHLIGHTER, color="#ffcc00",
                 width_mm=3.5)
    _draw_stylus(viewer, _straight_line(start=(100.0, 500.0),
                                        end=(400.0, 500.0)))
    state = _arm(viewer)
    assert state["kind"] == "highlighter"
    _move_stylus(viewer, (450.0, 480.0))
    assert viewer._shape_morph is None
    _release_stylus(viewer, (450.0, 480.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    assert isinstance(annotations[0], Highlighter)


def test_ordinary_freehand_stroke_commits_an_inkstroke(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line(start=(100.0, 300.0),
                                        end=(300.0, 300.0)))
    assert viewer._shape_morph is None
    _release_stylus(viewer, (300.0, 300.0))
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    assert isinstance(annotations[0], InkStroke)


# -- alternate input paths and motion settings ------------------------------

def test_mouse_pointer_path_adjusts_and_commits_one_line(viewer):
    _select_pen(viewer)
    points = _straight_line(start=(100.0, 350.0), end=(400.0, 350.0))
    first = _page_to_widget(viewer, *points[0])
    _mouse_press(viewer, first[0], first[1])
    for point in points[1:]:
        _mouse_move(viewer, *_page_to_widget(viewer, *point))
    state = _arm(viewer)
    assert state["candidate"].kind == "line"
    for point in ((430.0, 370.0), (470.0, 410.0), (510.0, 440.0)):
        _mouse_move(viewer, *_page_to_widget(viewer, *point))
        if state.get("adjust") is not None:
            break
    assert state.get("adjust") is not None
    expected = tuple(state["candidate"].contour)
    last = _page_to_widget(viewer, *expected[1])
    _mouse_release(viewer, last[0], last[1])
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    assert shape.kind is ShapeKind.LINE
    committed = ((shape.points[0].x, shape.points[0].y),
                 (shape.points[1].x, shape.points[1].y))
    assert _pairs_close(committed, expected)


def test_reduced_motion_recognizes_and_adjusts_immediately(viewer,
                                                           monkeypatch):
    monkeypatch.setattr("app.viewer.reduced_motion_enabled", lambda: True)
    _select_pen(viewer)
    _draw_stylus(viewer, _straight_line(start=(100.0, 250.0),
                                        end=(400.0, 250.0)))
    state = _arm(viewer)
    assert state["duration_ms"] == 0
    assert state["phase"] == 1.0
    state, adjustment = _activate(
        viewer, moves=((430.0, 250.0), (470.0, 290.0)))
    _move_stylus(viewer, (520.0, 330.0))
    expected = tuple(state["candidate"].contour)
    _release_stylus(viewer, expected[adjustment.moving_index])
    annotations = list(viewer._document.annotations.page(0))
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    assert shape.kind is ShapeKind.LINE
