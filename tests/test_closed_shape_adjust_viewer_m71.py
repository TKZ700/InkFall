"""M7.1 Phase B Part 2: closed-shape live adjustment through the viewer.

Covers all four recognized closed pen shapes -- rectangle, circle,
triangle and polygon -- end to end: the morph arms, the first real
movement switches it into live adjustment, the frozen handle follows the
stylus, invalid frames freeze the last valid preview, release commits the
preview geometry, and one grouped undo/redo step is preserved (including
an ink-chain merge).

Recognition is triggered by calling the dwell handler directly, exactly
as in the Phase A suite: the 320 ms timer contract is unchanged and
timing it here would only add wall-clock flake. Everything after
recognition runs through the real pointer and commit paths.

The fixture calls ``viewer.shutdown()`` explicitly: without it the page
navigator's process decoder keeps spawned children alive past the test
session in this headless environment.
"""

from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QInputDevice, QPointingDevice, QTabletEvent

from annotations.models import InkStroke, Shape, ShapeKind
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
    pdf = make_probe_pdf(str(tmp_path / "closed-adjust.pdf"),
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


def _page_to_widget(viewer, x, y, page_index=0):
    entry = viewer._pipeline.layout.entry(page_index)
    mapped = viewer._page_origin_for(entry.page_id.value)
    assert mapped is not None
    ox, oy, factor = mapped
    return (ox + float(x) * factor, oy + float(y) * factor)


def _select_pen(viewer):
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="pen",
                                      color="#112233", width_mm=0.7)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(slot)
    return slot


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


def _stroke_stylus(viewer, points, pressure=0.6):
    """Draw and release one short gesture (commits a freehand InkStroke)."""
    _draw_stylus(viewer, points, pressure)
    _release_stylus(viewer, points[-1])


def _release_stylus(viewer, point):
    wx, wy = _page_to_widget(viewer, *point)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, wx, wy, 0.0))
    return (wx, wy)


def _arm(viewer):
    viewer._on_shape_dwell()
    state = viewer._shape_morph
    assert state is not None, "recognition did not arm a morph"
    return state


def _activate(viewer, moves, label="closed shape"):
    state = viewer._shape_morph
    assert state is not None
    for point in moves:
        _move_stylus(viewer, point)
        if state.get("adjust") is not None:
            return state, state["adjust"]["adjustment"]
    raise AssertionError(f"{label} adjustment did not activate")


def _history(viewer):
    return viewer._history_for(viewer._document, create=False)


def _annotations(viewer):
    return list(viewer._document.annotations.page(0))


# -- geometry fixtures (page points) ---------------------------------------

def _polyline(points, per=12):
    """One closed stroke along a vertex ring, ending back at its first."""
    out = []
    for index in range(len(points)):
        start = points[index]
        end = points[(index + 1) % len(points)]
        for step in range(per):
            t = step / per
            out.append((start[0] + (end[0] - start[0]) * t,
                        start[1] + (end[1] - start[1]) * t))
    out.append(points[0])
    return out


def _square_stroke(start=(150.0, 150.0), width=300.0, height=250.0):
    x0, y0 = start
    return _polyline(((x0, y0), (x0 + width, y0),
                      (x0 + width, y0 + height), (x0, y0 + height)))


def _circle_stroke(cx=300.0, cy=300.0, radius=120.0, steps=48):
    return [(cx + radius * math.cos(2.0 * math.pi * i / steps),
             cy + radius * math.sin(2.0 * math.pi * i / steps))
            for i in range(steps + 1)]


def _triangle_stroke():
    return _polyline(((150.0, 150.0), (450.0, 150.0), (300.0, 390.0)))


def _pentagon_stroke():
    return _polyline(((300.0, 140.0), (440.0, 240.0), (390.0, 410.0),
                      (210.0, 410.0), (160.0, 240.0)))


def _side(start, end, steps=12):
    return [(start[0] + (end[0] - start[0]) * i / steps,
             start[1] + (end[1] - start[1]) * i / steps)
            for i in range(steps + 1)]


# -- committed geometry checks ---------------------------------------------

def _assert_rectangle_commit(candidate, shape):
    cx, cy, half_a, half_b, deg = candidate.meta
    assert shape.kind is ShapeKind.RECTANGLE
    assert shape.rotation_deg == pytest.approx(deg)
    assert shape.rect.origin.x == pytest.approx(cx - half_a)
    assert shape.rect.origin.y == pytest.approx(cy - half_b)
    assert shape.rect.width == pytest.approx(2.0 * half_a)
    assert shape.rect.height == pytest.approx(2.0 * half_b)


def _assert_circle_commit(candidate, shape):
    cx, cy, half_a, _half_b, _deg = candidate.meta
    assert shape.kind is ShapeKind.ELLIPSE
    assert shape.rect.origin.x == pytest.approx(cx - half_a)
    assert shape.rect.origin.y == pytest.approx(cy - half_a)
    assert shape.rect.width == pytest.approx(2.0 * half_a)
    assert shape.rect.height == pytest.approx(2.0 * half_a)


def _assert_polygon_commit(candidate, shape):
    assert shape.kind is ShapeKind.POLYGON
    assert shape.close_path is True
    assert len(shape.points) == len(candidate.contour)
    for point, expected in zip(shape.points, candidate.contour):
        assert (point.x, point.y) == pytest.approx(
            (float(expected[0]), float(expected[1])))


# -- rectangles -------------------------------------------------------------

def test_rectangle_adjusts_commits_preview_and_one_undo_step(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _square_stroke())
    state = _arm(viewer)
    assert state["candidate"].kind == "rectangle"
    assert state["adjust"] is None
    unadjusted = state["candidate"]

    state, adjustment = _activate(viewer, ((120.0, 120.0),))
    assert adjustment.kind == "rectangle"
    assert state["candidate"].kind == "rectangle"
    assert state["candidate"].contour != unadjusted.contour

    _move_stylus(viewer, (90.0, 110.0))
    preview = state["candidate"]
    cx, cy, half_a, half_b, _deg = preview.meta
    assert half_a > 150.0
    assert half_b > 125.0
    assert (preview.contour[2][0], preview.contour[2][1]) == pytest.approx(
        (cx - half_a, cy - half_b))

    # Preview changes never touch the document or its history.
    assert _annotations(viewer) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()

    _release_stylus(viewer, (90.0, 110.0))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    _assert_rectangle_commit(preview, shape)

    history = viewer.document_history
    assert history is not None and history.can_undo() is True
    assert history.can_redo() is False
    history.undo()
    assert _annotations(viewer) == []
    history.redo()
    assert len(_annotations(viewer)) == 1


def test_rectangle_invalid_frame_freezes_last_valid_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _square_stroke())
    state = _arm(viewer)
    state, _adjustment = _activate(viewer, ((120.0, 120.0),))
    _move_stylus(viewer, (100.0, 100.0))
    last_valid = state["candidate"]
    last_contour = tuple(last_valid.contour)

    # Cross the diagonal anchor's local axes: a mirror, never committed.
    _move_stylus(viewer, (560.0, 500.0))
    assert state["candidate"] is last_valid
    assert tuple(state["candidate"].contour) == last_contour

    _release_stylus(viewer, (560.0, 500.0))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    assert isinstance(annotations[0], Shape)
    _assert_rectangle_commit(last_valid, annotations[0])


# -- circles ----------------------------------------------------------------

def test_circle_resizes_radially_and_commits_the_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _circle_stroke())
    state = _arm(viewer)
    assert state["candidate"].kind == "circle"
    original_radius = state["candidate"].meta[2]

    state, adjustment = _activate(viewer, ((440.0, 300.0),))
    assert adjustment.kind == "circle"
    cx, cy = adjustment.center
    assert (cx, cy) == pytest.approx((300.0, 300.0), abs=0.01)
    assert state["candidate"].meta[2] == pytest.approx(
        original_radius + (440.0 - 420.0))

    # Tangential motion leaves the radius alone; radial motion changes it.
    current_radius = state["candidate"].meta[2]
    _move_stylus(viewer, (cx + current_radius * math.cos(0.9),
                          cy + current_radius * math.sin(0.9)))
    assert state["candidate"].meta[2] == pytest.approx(current_radius,
                                                        abs=0.5)

    _move_stylus(viewer, (cx + 70.0, cy))
    preview = state["candidate"]
    # r_new = r0 + (dist(pointer, center) - dist(origin, center)).
    assert preview.meta[2] == pytest.approx(original_radius - 50.0, abs=0.5)
    assert preview.meta[0] == pytest.approx(cx)
    assert preview.meta[1] == pytest.approx(cy)
    assert preview.meta[3] == pytest.approx(preview.meta[2])
    assert preview.meta[4] == 0.0

    _release_stylus(viewer, (cx + 70.0, cy))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    assert isinstance(annotations[0], Shape)
    _assert_circle_commit(preview, annotations[0])


def test_circle_invalid_frame_freezes_last_valid_radius(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _circle_stroke())
    state = _arm(viewer)
    state, adjustment = _activate(viewer, ((430.0, 300.0),))
    last_valid = state["candidate"]
    last_radius = last_valid.meta[2]

    # Inside the activation-grandfathered radius floor.
    _move_stylus(viewer, (adjustment.center[0] + 0.5,
                          adjustment.center[1]))
    assert state["candidate"] is last_valid
    assert state["candidate"].meta[2] == pytest.approx(last_radius)

    _release_stylus(viewer, (adjustment.center[0] + 0.5,
                             adjustment.center[1]))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    _assert_circle_commit(last_valid, annotations[0])


# -- triangles --------------------------------------------------------------

def test_triangle_moves_one_vertex_and_commits_the_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _triangle_stroke())
    state = _arm(viewer)
    assert state["candidate"].kind == "triangle"
    assert len(state["candidate"].contour) == 3

    state, adjustment = _activate(viewer, ((120.0, 120.0),))
    assert adjustment.kind == "triangle"
    index = adjustment.moving_index
    assert 0 <= index < 3
    after_first = tuple(state["candidate"].contour)

    # A further absolute pointer delta moves exactly the frozen vertex.
    _move_stylus(viewer, (95.0, 100.0))
    preview = state["candidate"]
    assert len(preview.contour) == 3
    for other in range(3):
        if other == index:
            continue
        assert preview.contour[other] == pytest.approx(after_first[other])
    assert preview.contour[index] == pytest.approx(
        (after_first[index][0] - 25.0, after_first[index][1] - 20.0))

    _release_stylus(viewer, (95.0, 100.0))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    assert isinstance(annotations[0], Shape)
    _assert_polygon_commit(preview, annotations[0])


# -- polygons ---------------------------------------------------------------

def test_polygon_moves_one_vertex_preserves_ring_and_commits_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _pentagon_stroke())
    state = _arm(viewer)
    assert state["candidate"].kind == "polygon"
    assert len(state["candidate"].contour) == 5

    state, adjustment = _activate(viewer, ((290.0, 120.0),))
    assert adjustment.kind == "polygon"
    index = adjustment.moving_index
    assert 0 <= index < 5
    after_first = tuple(state["candidate"].contour)

    _move_stylus(viewer, (265.0, 100.0))
    preview = state["candidate"]
    assert len(preview.contour) == 5
    for other in range(5):
        if other == index:
            continue
        assert preview.contour[other] == pytest.approx(after_first[other])
    assert preview.contour[index] == pytest.approx(
        (after_first[index][0] - 25.0, after_first[index][1] - 20.0))

    _release_stylus(viewer, (265.0, 100.0))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    assert isinstance(annotations[0], Shape)
    _assert_polygon_commit(preview, annotations[0])


def test_polygon_invalid_frame_keeps_last_valid_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _pentagon_stroke())
    state = _arm(viewer)
    pointer = (290.0, 120.0)
    state, adjustment = _activate(viewer, (pointer,))
    last_valid = state["candidate"]
    last_contour = tuple(last_valid.contour)
    index = adjustment.moving_index
    adjacent = (index + 1) % len(last_contour)

    # Land the moving vertex exactly on its neighbouring vertex: the
    # resulting duplicate/zero-length edge can never be committed.
    bad_pointer = (
        pointer[0] + last_contour[adjacent][0] - last_contour[index][0],
        pointer[1] + last_contour[adjacent][1] - last_contour[index][1],
    )
    _move_stylus(viewer, bad_pointer)
    assert state["candidate"] is last_valid
    assert tuple(state["candidate"].contour) == last_contour

    _release_stylus(viewer, bad_pointer)
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    _assert_polygon_commit(last_valid, annotations[0])


# -- unadjusted recognition and jitter -------------------------------------

@pytest.mark.parametrize("stroke, expected_kind", [
    (_square_stroke, "rectangle"),
    (_circle_stroke, "circle"),
    (_triangle_stroke, "triangle"),
    (_pentagon_stroke, "polygon"),
])
def test_release_without_movement_commits_the_unadjusted_recognition(
        viewer, stroke, expected_kind):
    _select_pen(viewer)
    _draw_stylus(viewer, stroke())
    state = _arm(viewer)
    candidate = state["candidate"]
    assert candidate.kind == expected_kind
    assert state["adjust"] is None

    _release_stylus(viewer, stroke()[-1])
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    if expected_kind == "rectangle":
        _assert_rectangle_commit(candidate, shape)
    elif expected_kind == "circle":
        _assert_circle_commit(candidate, shape)
    else:
        _assert_polygon_commit(candidate, shape)


def test_sub_threshold_movement_keeps_every_closed_preview(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _square_stroke())
    state = _arm(viewer)
    candidate = state["candidate"]
    _move_stylus(viewer, (153.0, 151.0))  # ~3.6 pt: jitter, not adjustment
    assert viewer._shape_morph is state
    assert state["adjust"] is None
    assert state["candidate"] is candidate
    _release_stylus(viewer, (153.0, 151.0))
    annotations = _annotations(viewer)
    assert len(annotations) == 1
    _assert_rectangle_commit(candidate, annotations[0])


# -- cancellation and read-only --------------------------------------------

def test_cancel_intent_mid_closed_adjustment_commits_nothing(viewer):
    _select_pen(viewer)
    _draw_stylus(viewer, _pentagon_stroke())
    _arm(viewer)
    _activate(viewer, ((290.0, 120.0),))
    viewer._apply_intent(viewer._interaction.cancel())
    assert viewer._shape_morph is None
    assert viewer._live_stroke is None
    assert _annotations(viewer) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()


def test_read_only_document_never_commits_an_adjusted_closed_shape(
        viewer, monkeypatch):
    _select_pen(viewer)
    _draw_stylus(viewer, _square_stroke())
    _arm(viewer)
    _activate(viewer, ((120.0, 120.0),))
    _move_stylus(viewer, (100.0, 100.0))

    monkeypatch.setattr(viewer, "_document_is_readonly", lambda: True)
    _release_stylus(viewer, (100.0, 100.0))
    assert _annotations(viewer) == []
    history = _history(viewer)
    assert history is None or not history.can_undo()


# -- chain-merged outlines --------------------------------------------------
#
# The chain merge joins separately captured straight sides. The closing gap
# must be too wide to pair as a seam but inside the chain's own closure
# tolerance, so the live side stops part-way along its edge. The layouts
# below also keep the two gap-adjacent sides short enough that their own
# bounding box is smaller than the whole outline -- the window the merge
# was designed around.

def _chain_adjust_and_commit(viewer, sides, live_start, live_end, expected):
    """Draw committed sides plus a live side, adjust, commit, undo, redo."""
    _select_pen(viewer)
    for start, end in sides:
        _stroke_stylus(viewer, _side(start, end))
    committed_sides = len(_annotations(viewer))
    assert committed_sides == len(sides)
    assert all(isinstance(item, InkStroke) for item in _annotations(viewer))

    _draw_stylus(viewer, _side(live_start, live_end))
    state = _arm(viewer)
    assert state["kind"] == "pen"
    assert state["chain_ids"], "the merged chain did not consume any sides"
    assert state["candidate"].kind == expected
    assert state["candidate"].closed is True

    origin = state["origin_pt"]
    nudge = (float(origin[0]) - 12.0, float(origin[1]) - 10.0)
    state, adjustment = _activate(viewer, (nudge,), label=f"chain {expected}")
    assert adjustment.kind == expected

    final = (nudge[0] - 8.0, nudge[1] - 6.0)
    _move_stylus(viewer, final)
    preview = state["candidate"]
    _release_stylus(viewer, final)

    annotations = _annotations(viewer)
    assert len(annotations) == 1
    shape = annotations[0]
    assert isinstance(shape, Shape)
    # A merged outline always commits as one editable polygon.
    assert shape.kind is ShapeKind.POLYGON
    _assert_polygon_commit(preview, shape)

    history = viewer.document_history
    assert history is not None and history.can_undo() is True
    history.undo()
    restored = _annotations(viewer)
    assert len(restored) == committed_sides
    assert all(isinstance(item, InkStroke) for item in restored)
    history.redo()
    redone = _annotations(viewer)
    assert len(redone) == 1
    assert isinstance(redone[0], Shape)
    _assert_polygon_commit(preview, redone[0])
    return committed_sides


def test_chain_merged_rectangle_adjusts_with_one_grouped_undo(viewer):
    start = (150.0, 200.0)
    bottom_right = (450.0, 400.0)
    live_start = (150.0, 400.0)
    live_end = (150.0, 225.0)
    sides = ((start, (150.0, 150.0)),
             ((150.0, 150.0), (450.0, 150.0)),
             ((450.0, 150.0), bottom_right),
             (bottom_right, live_start))
    assert _chain_adjust_and_commit(
        viewer, sides, live_start, live_end, "rectangle") == 4


def test_chain_merged_triangle_adjusts_with_one_grouped_undo(viewer):
    corner = (150.0, 120.0)
    apex = (300.0, 380.0)
    right = (450.0, 120.0)
    start = (200.0, 120.0)
    live_start = (228.0, 120.0)
    sides = ((start, corner), (corner, apex), (apex, right))
    assert _chain_adjust_and_commit(
        viewer, sides, live_start, right, "triangle") == 3


def test_chain_merged_polygon_adjusts_with_one_grouped_undo(viewer):
    top = (300.0, 140.0)
    upper_right = (440.0, 240.0)
    lower_right = (390.0, 410.0)
    lower_left = (210.0, 410.0)
    upper_left = (160.0, 240.0)
    live_start = upper_left
    live_end = (278.03, 155.70)
    sides = ((top, upper_right), (upper_right, lower_right),
             (lower_right, lower_left), (lower_left, upper_left))
    assert _chain_adjust_and_commit(
        viewer, sides, live_start, live_end, "polygon") == 4

