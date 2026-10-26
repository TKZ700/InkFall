"""M6.8.2 Stage 4: group resize and rotation handles.

Interactive transform handles on the shared selection frame:

* eight resize grips (corners free-scale; edges single-axis) and one
  rotation grip above the frame, hit-tested against the *displayed*
  frame with priority over body movement;
* preview-only interaction computed from immutable gesture-start
  snapshots, then exactly one grouped undo entry;
* cross-page groups transform as one visual group while every member
  keeps its page assignment;
* resize/rotation work identically from Selection, Lasso, and Pan (a
  Pan double-click selection exposes fully functional grips);
* the shared frame is *oriented*: rotating a selection rotates the frame
  itself (not merely its contents), and a follow-up resize uses the
  frame's local axes;
* the rotation grip and its connector (the "rotation centre") are hidden
  for the duration of a resize *and* a body/group move, and restored at
  the frame's current position the moment the gesture ends.

The pure transform mathematics (anchors, clamps, rotated local frames,
cross-page offset formula) is covered Qt-free in
``tests/test_transform_geometry_m682.py``; these tests validate the
viewer wiring, hit-testing, preview lifecycle, and history/undo.
"""

from __future__ import annotations

import contextlib
import dataclasses
import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QHideEvent, QKeyEvent, QMouseEvent

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from annotations.transform import (
    GroupTransform,
    apply_group,
    normalize_deg,
    positive_scale_clamp,
)
from app.viewer import (_MIN_GROUP_SIDE_PX, _ROTATE_HANDLE_OFFSET_PX,
                        _SELECT_FRAME_PAD_PX)
from core.types import Point, Rect, Size
from tests.helpers import make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_MOVE = 40.0  # a drag clearly past the established threshold
_ACCENT = (139, 147, 224)  # _SELECT_ACCENT (#8B93E0)
_SETTINGS_KEYS = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                  "inkfall/m5/toolbar/active",
                  "inkfall/m5/toolbar/custom_colors",
                  "inkfall/m5/toolbar/presets")


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in _SETTINGS_KEYS:
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in _SETTINGS_KEYS:
            QSettings().remove(key)
    except Exception:
        pass


def _selection_tool():
    return ToolInstance(kind=ToolType.SELECTION, name="Select")


def _lasso_tool():
    return ToolInstance(kind=ToolType.LASSO, name="Lasso")


def _pan_tool():
    return ToolInstance(kind=ToolType.PAN, name="Pan")


def _pen_tool():
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#ff0000",
                        width_mm=1.0, opacity=0.9)


def _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0), width_mm=1.0, **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#e01b24",
                width_mm=width_mm, opacity=1.0)
    base.update(kw)
    return InkStroke(**base)


def _box_shape(kind=ShapeKind.RECTANGLE, rect=(100.0, 100.0, 220.0, 180.0),
               rotation_deg=0.0, **kw):
    base = dict(kind=kind,
                rect=Rect(Point(rect[0], rect[1]),
                          Size(rect[2] - rect[0], rect[3] - rect[1])),
                color="#000000", width_mm=1.0,
                rotation_deg=rotation_deg)
    base.update(kw)
    return Shape(**base)


def _highlighter(p0=(120.0, 140.0), p1=(280.0, 140.0), width_mm=4.0, **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#f5d90a",
                width_mm=width_mm, opacity=0.4)
    base.update(kw)
    return Highlighter(**base)


# ------------------------------------------------------- viewer helpers

def _press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _move(viewer, x, y, shift=False):
    mods = (Qt.KeyboardModifier.ShiftModifier if shift
            else Qt.KeyboardModifier.NoModifier)
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, mods))


def _release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


@contextlib.contextmanager
def _chrome_hidden(viewer):
    hidden = []
    for name in ("_shelf", "_undo_hud", "_tool_popup"):
        widget = getattr(viewer, name, None)
        if widget is not None:
            hidden.append(widget)
            widget.hide()
    yield
    for widget in hidden:
        widget.show()


def _open_window(qtbot, tmp_path, tool=None, name="s4.pdf", sizes=None):
    from app.main_window import MainWindow

    if sizes is None:
        pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                             page_height=400.0)
    else:
        pdf = make_sized_pdf(str(tmp_path / name), sizes)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool if tool is not None
                                 else _selection_tool())
    qtbot.wait(100)
    return window, viewer


def _page_id(viewer, page_index=0):
    return viewer._pipeline.layout.entry(page_index).page_id


def _store(viewer):
    return viewer._document.annotations


def _seed(viewer, annotation, page_index=0):
    stored = dataclasses.replace(annotation,
                                 page_id=_page_id(viewer, page_index))
    _store(viewer).add(stored, page_index)
    return stored


def _factor(viewer):
    return (float(viewer._pipeline.layout.raster_scale)
            * viewer._display_scale())


def _offset(viewer, page_index=0):
    entry = viewer._pipeline.layout.entry(page_index)
    pt = viewer._region_page_pt(entry, 0.0, 0.0)
    return (-float(pt[0]), -float(pt[1]))


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


def _history_entries(viewer):
    return viewer.document_history.stack.count()


def _selected_values(viewer):
    return tuple(a.value for a in viewer._selection.selected_ids)


def _grip(viewer, name):
    points = viewer._selection_handle_points()
    assert points is not None
    return points[name]


def _start(viewer, name):
    """Capture the displayed frame, factor, and grip *before* a drag.

    The transform is defined against the frame as displayed at press
    time (the padded frame the grips sit on); the frame is recomputed
    from committed geometry afterwards, so capturing start state first
    is essential.
    """
    factor = _factor(viewer)
    rect = viewer._selection_frame_widget_rect()
    assert rect is not None
    left, top, width, height = rect
    shared = (left / factor, top / factor,
              (left + width) / factor, (top + height) / factor)
    return {"factor": factor, "frame": shared, "grip": _grip(viewer, name)}


def _drag_grip(viewer, name, dx, dy, shift=False, steps=5, start=None):
    if start is None:
        start = _start(viewer, name)
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        for i in range(1, steps + 1):
            frac = i / steps
            _move(viewer, gx + dx * frac, gy + dy * frac, shift=shift)
        _release(viewer, gx + dx, gy + dy)
    return start


def _anchor_for(name, frame):
    fx0, fy0, fx1, fy1 = frame
    cx = (fx0 + fx1) / 2.0
    cy = (fy0 + fy1) / 2.0
    return {
        "se": (fx0, fy0), "nw": (fx1, fy1),
        "ne": (fx0, fy1), "sw": (fx1, fy0),
        "e": (fx0, cy), "w": (fx1, cy),
        "n": (cx, fy1), "s": (cx, fy0),
    }[name]


def _safe_scale(delta, base):
    try:
        delta = float(delta)
        base = float(base)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(delta) or not math.isfinite(base) or base == 0.0:
        return 1.0
    return delta / base


def _resize_transform(start, name, target_widget, shift=False):
    """Mirror the viewer's resize decision using the pure transform API."""
    factor = start["factor"]
    fx0, fy0, fx1, fy1 = start["frame"]
    ax, ay = _anchor_for(name, start["frame"])
    px = float(target_widget[0]) / factor
    py = float(target_widget[1]) / factor
    corners = {"se": (fx1, fy1), "nw": (fx0, fy0),
               "ne": (fx1, fy0), "sw": (fx0, fy1)}
    min_side = _MIN_GROUP_SIDE_PX / factor
    base = (fx1 - fx0, fy1 - fy0)
    if name in corners:
        bx, by = corners[name]
        sx = _safe_scale(px - ax, bx - ax)
        sy = _safe_scale(py - ay, by - ay)
        if shift:
            gx, gy = start["grip"]
            dx = px - float(gx) / factor
            dy = py - float(gy) / factor
            uniform = sx if abs(dx) >= abs(dy) else sy
            cx, cy = positive_scale_clamp(uniform, uniform, base, min_side)
            uniform = max(cx, cy)
            sx = sy = uniform
        else:
            sx, sy = positive_scale_clamp(sx, sy, base, min_side)
    elif name in ("e", "w"):
        bx = fx1 if name == "e" else fx0
        sx = _safe_scale(px - ax, bx - ax)
        sx, _ = positive_scale_clamp(sx, 1.0, base, min_side)
        sy = 1.0
    elif name in ("n", "s"):
        by = fy1 if name == "s" else fy0
        sy = _safe_scale(py - ay, by - ay)
        _, sy = positive_scale_clamp(1.0, sy, base, min_side)
        sx = 1.0
    else:  # pragma: no cover - defensive
        raise AssertionError(name)
    return GroupTransform(anchor=Point(ax, ay), sx=sx, sy=sy)


def _rotate_transform(start, target_widget):
    factor = start["factor"]
    fx0, fy0, fx1, fy1 = start["frame"]
    px = (fx0 + fx1) / 2.0
    py = (fy0 + fy1) / 2.0
    gx, gy = start["grip"]
    begin = math.atan2(float(gy) / factor - py, float(gx) / factor - px)
    cur = math.atan2(float(target_widget[1]) / factor - py,
                     float(target_widget[0]) / factor - px)
    return GroupTransform(pivot=Point(px, py),
                          angle_deg=normalize_deg(math.degrees(cur - begin)))


def _rotate_target(start, delta_deg):
    """Widget point ``delta_deg`` around the pivot from the grip's bearing.

    ``delta_deg`` is relative to where the rotation grip starts (directly
    above the pivot), so ``90`` is always a quarter turn from press.
    """
    factor = start["factor"]
    fx0, fy0, fx1, fy1 = start["frame"]
    px = (fx0 + fx1) / 2.0
    py = (fy0 + fy1) / 2.0
    gx, gy = start["grip"]
    radius = math.hypot(gx / factor - px, gy / factor - py)
    begin = math.atan2(gy / factor - py, gx / factor - px)
    rad = begin + math.radians(delta_deg)
    return ((px + radius * math.cos(rad)) * factor,
            (py + radius * math.sin(rad)) * factor)


def _assert_points(actual, expected, tol=1e-4):
    assert len(actual.points) == len(expected.points)
    for got, want in zip(actual.points, expected.points):
        assert got.x == pytest.approx(want.x, abs=tol)
        assert got.y == pytest.approx(want.y, abs=tol)


# ------------------------------------------------ oriented-frame helpers

def _angle_from_corners(corners) -> float:
    (nx, ny), (ex, ey) = corners[0], corners[1]
    return normalize_deg(math.degrees(math.atan2(ey - ny, ex - nx)))


def _expected_handles(corners):
    """Mirror :meth:`PdfViewer._selection_handle_points` for a quad."""
    nw, ne, se, sw = [(float(c[0]), float(c[1])) for c in corners]

    def mid(a, b):
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    top = mid(nw, ne)
    centre = mid(nw, se)
    vx = top[0] - centre[0]
    vy = top[1] - centre[1]
    norm = math.hypot(vx, vy)
    rot = (top[0] + vx / norm * _ROTATE_HANDLE_OFFSET_PX,
           top[1] + vy / norm * _ROTATE_HANDLE_OFFSET_PX)
    return {"nw": nw, "n": top, "ne": ne, "e": mid(ne, se), "se": se,
            "s": mid(se, sw), "sw": sw, "w": mid(sw, nw), "rotate": rot}


def _start_oriented(viewer, name):
    """Capture the oriented frame corners and grip before a drag."""
    factor = _factor(viewer)
    corners = viewer._selection_frame_corners()
    assert corners is not None
    return {"factor": factor, "corners": tuple(corners),
            "grip": _grip(viewer, name)}


def _resize_transform_oriented(start, name, target_widget, shift=False):
    """Mirror the viewer's *oriented-frame* resize decision, Qt-free."""
    factor = start["factor"]
    nw, ne, se, sw = [(x / factor, y / factor) for x, y in start["corners"]]
    rad = math.radians(_angle_from_corners(start["corners"]))
    c = math.cos(rad)
    s = math.sin(rad)

    def mid(a, b):
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    anchor = {"se": nw, "nw": se, "ne": sw, "sw": ne,
              "e": mid(nw, sw), "w": mid(ne, se),
              "n": mid(sw, se), "s": mid(nw, ne)}[name]
    ax, ay = anchor

    def lu(p):
        return (p[0] - ax) * c + (p[1] - ay) * s

    def lv(p):
        return -(p[0] - ax) * s + (p[1] - ay) * c

    pointer = (target_widget[0] / factor, target_widget[1] / factor)
    grip = (start["grip"][0] / factor, start["grip"][1] / factor)
    base = (math.hypot(ne[0] - nw[0], ne[1] - nw[1]),
            math.hypot(sw[0] - nw[0], sw[1] - nw[1]))
    min_side = _MIN_GROUP_SIDE_PX / factor
    corner = {"nw": nw, "ne": ne, "se": se, "sw": sw}
    if name in corner:
        b = corner[name]
        sx = _safe_scale(lu(pointer), lu(b))
        sy = _safe_scale(lv(pointer), lv(b))
        if shift:
            du = lu(pointer) - lu(grip)
            dv = lv(pointer) - lv(grip)
            uniform = sx if abs(du) >= abs(dv) else sy
            cx, cy = positive_scale_clamp(uniform, uniform, base, min_side)
            sx = sy = max(cx, cy)
        else:
            sx, sy = positive_scale_clamp(sx, sy, base, min_side)
    elif name in ("e", "w"):
        b = mid(ne, se) if name == "e" else mid(nw, sw)
        sx = _safe_scale(lu(pointer), lu(b))
        sx, _ = positive_scale_clamp(sx, 1.0, base, min_side)
        sy = 1.0
    else:
        b = mid(nw, ne) if name == "n" else mid(sw, se)
        sy = _safe_scale(lv(pointer), lv(b))
        _, sy = positive_scale_clamp(1.0, sy, base, min_side)
        sx = 1.0
    return GroupTransform(anchor=Point(ax, ay), sx=sx, sy=sy,
                          frame_deg=_angle_from_corners(start["corners"]))


def _rotate_transform_oriented(start, target_widget):
    factor = start["factor"]
    nw, ne, se, sw = [(x / factor, y / factor) for x, y in start["corners"]]
    px = (nw[0] + se[0]) / 2.0
    py = (nw[1] + se[1]) / 2.0
    gx, gy = start["grip"]
    begin = math.atan2(gy / factor - py, gx / factor - px)
    cur = math.atan2(target_widget[1] / factor - py,
                     target_widget[0] / factor - px)
    return GroupTransform(pivot=Point(px, py),
                          angle_deg=normalize_deg(math.degrees(cur - begin)))


def _rotate_target_oriented(start, delta_deg):
    factor = start["factor"]
    nw, ne, se, sw = [(x / factor, y / factor) for x, y in start["corners"]]
    px = (nw[0] + se[0]) / 2.0
    py = (nw[1] + se[1]) / 2.0
    gx, gy = start["grip"]
    radius = math.hypot(gx / factor - px, gy / factor - py)
    begin = math.atan2(gy / factor - py, gx / factor - px)
    rad = begin + math.radians(delta_deg)
    return ((px + radius * math.cos(rad)) * factor,
            (py + radius * math.sin(rad)) * factor)


def _rotate_selection_90(viewer, direction=1.0):
    """Rotate the current selection a quarter turn; return start capture."""
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 90.0 * direction)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy,
               start={"factor": start["factor"], "grip": start["grip"]})
    return start, target


# ------------------------------------------------------------- pixel helpers

def _accent_like(color):
    return (abs(color.red() - _ACCENT[0]) <= 45
            and abs(color.green() - _ACCENT[1]) <= 45
            and abs(color.blue() - _ACCENT[2]) <= 45)


def _accent_count(image, x0, y0, x1, y1):
    count = 0
    for y in range(max(0, int(y0)), min(image.height(), int(y1))):
        for x in range(max(0, int(x0)), min(image.width(), int(x1))):
            if _accent_like(image.pixelColor(x, y)):
                count += 1
    return count


# ------------------------------------------------------------- Pan helpers

def _dblclick(viewer, wx, wy):
    viewer.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(wx, wy),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _pan_double_click_widget(viewer, wx, wy):
    with _chrome_hidden(viewer):
        _press(viewer, wx, wy)
        _release(viewer, wx, wy)
        _dblclick(viewer, wx, wy)
        _release(viewer, wx, wy)


# ======================================================= resize: geometry

@pytest.mark.parametrize("grip", ["nw", "ne", "sw", "se"])
def test_corner_grip_resizes_about_opposite_anchor(
        qtbot, tmp_path, cleared_settings, grip):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, grip)
    gx, gy = start["grip"]
    dx = 60.0 if grip in ("ne", "se") else -60.0
    dy = 40.0 if grip in ("sw", "se") else -40.0
    target = (gx + dx, gy + dy)
    _drag_grip(viewer, grip, dx, dy, start=start)
    got = _store(viewer).get(a.id)
    assert got is not a
    transform = _resize_transform(start, grip, target)
    expected = apply_group(a, _offset(viewer), transform)
    _assert_points(got, expected)
    assert got.page_id.value == _page_id(viewer).value
    assert _history_entries(viewer) == 1
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    viewer.document_history.redo()
    _assert_points(_store(viewer).get(a.id), expected)


@pytest.mark.parametrize("grip", ["nw", "ne", "sw", "se"])
def test_shift_corner_resize_preserves_aspect_ratio(
        qtbot, tmp_path, cleared_settings, grip):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, grip)
    gx, gy = start["grip"]
    dx = 60.0 if grip in ("ne", "se") else -60.0
    dy = 48.0 if grip in ("sw", "se") else -48.0
    target = (gx + dx, gy + dy)
    _drag_grip(viewer, grip, dx, dy, shift=True, start=start)
    got = _store(viewer).get(a.id)
    transform = _resize_transform(start, grip, target, shift=True)
    _assert_points(got, apply_group(a, _offset(viewer), transform))
    # Uniform scale: both axes share one factor.
    assert transform.sx == pytest.approx(transform.sy, abs=1e-9)


@pytest.mark.parametrize("grip,axis", [("e", "x"), ("w", "x"),
                                       ("n", "y"), ("s", "y")])
def test_edge_grips_scale_one_axis_only(
        qtbot, tmp_path, cleared_settings, grip, axis):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, grip)
    gx, gy = start["grip"]
    dx = 50.0 if grip == "e" else -50.0 if grip == "w" else 0.0
    dy = 40.0 if grip == "s" else -40.0 if grip == "n" else 0.0
    _drag_grip(viewer, grip, dx, dy, start=start)
    got = _store(viewer).get(a.id)
    transform = _resize_transform(start, grip, (gx + dx, gy + dy))
    if axis == "x":
        assert transform.sy == 1.0
        assert transform.sx != 1.0
    else:
        assert transform.sx == 1.0
        assert transform.sy != 1.0
    _assert_points(got, apply_group(a, _offset(viewer), transform))
    assert _history_entries(viewer) == 1


def test_opposite_anchor_stays_fixed_through_preview_and_commit(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    anchor = _anchor_for("se", start["frame"])
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 30.0, gy + 20.0)
        transform = _resize_transform(start, "se", (gx + 30.0, gy + 20.0))
        candidate = viewer._transform_preview[a.id]
        _assert_points(candidate, apply_group(a, _offset(viewer), transform))
        _release(viewer, gx + 40.0, gy + 26.0)
    transform = _resize_transform(start, "se", (gx + 40.0, gy + 26.0))
    # The SE grip anchors at the displayed NW corner: the fixed point.
    assert transform.anchor.x == pytest.approx(anchor[0], abs=1e-9)
    assert transform.anchor.y == pytest.approx(anchor[1], abs=1e-9)
    _assert_points(_store(viewer).get(a.id),
                   apply_group(a, _offset(viewer), transform))


def test_min_side_floor_prevents_flip_through_anchor(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    anchor = _anchor_for("se", start["frame"])
    _drag_grip(viewer, "se", -600.0, -500.0, start=start)
    got = _store(viewer).get(a.id)
    transform = _resize_transform(start, "se",
                                  (start["grip"][0] - 600.0,
                                   start["grip"][1] - 500.0))
    assert transform.sx > 0.0
    assert transform.sy > 0.0
    _assert_points(got, apply_group(a, _offset(viewer), transform))
    # Every candidate point stays on the anchor side (no inversion).
    off = _offset(viewer)
    ax_local = anchor[0] - off[0]
    ay_local = anchor[1] - off[1]
    for point in got.points:
        assert point.x >= ax_local - 1e-6
        assert point.y >= ay_local - 1e-6


def test_rotated_rectangle_and_ellipse_resize_keep_rotation_deg(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    rect = _seed(viewer, _box_shape(ShapeKind.RECTANGLE, rotation_deg=30.0))
    ell = _seed(viewer, _box_shape(ShapeKind.ELLIPSE, rotation_deg=-45.0))
    viewer._selection.set([rect.id, ell.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    _drag_grip(viewer, "se", 60.0, 40.0, start=start)
    transform = _resize_transform(start, "se", (gx + 60.0, gy + 40.0))
    for original in (rect, ell):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, _offset(viewer), transform)
        assert got.rotation_deg == pytest.approx(
            original.rotation_deg, abs=1e-9)  # resize keeps orientation
        assert got.rect.x0 == pytest.approx(expected.rect.x0, abs=1e-4)
        assert got.rect.y0 == pytest.approx(expected.rect.y0, abs=1e-4)
        assert got.rect.x1 == pytest.approx(expected.rect.x1, abs=1e-4)
        assert got.rect.y1 == pytest.approx(expected.rect.y1, abs=1e-4)


def test_mixed_group_resize_scales_all_types_and_clamps_width(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _seed(viewer, _pen_stroke(width_mm=1.0))
    hl = _seed(viewer, _highlighter(width_mm=4.0))
    shape = _seed(viewer, _box_shape())
    viewer._selection.set([stroke.id, hl.id, shape.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    _drag_grip(viewer, "se", 80.0, 60.0, start=start)
    transform = _resize_transform(start, "se", (gx + 80.0, gy + 60.0))
    for original in (stroke, hl, shape):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, _offset(viewer), transform)
        if isinstance(original, (InkStroke, Highlighter)):
            _assert_points(got, expected)
        else:
            assert got.rect.x1 == pytest.approx(expected.rect.x1, abs=1e-4)
        assert got.width_mm == pytest.approx(expected.width_mm, abs=1e-6)
        assert 0.1 - 1e-9 <= got.width_mm <= 50.0 + 1e-9
    # Type and kind are preserved for every member.
    assert type(_store(viewer).get(stroke.id)) is InkStroke
    assert type(_store(viewer).get(hl.id)) is Highlighter
    assert _store(viewer).get(shape.id).kind == ShapeKind.RECTANGLE


def test_resize_preview_is_deterministic_from_originals(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        # Wander back and forth; each sample must equal the single-step
        # transform from the originals (never accumulate drift).
        for x, y in ((gx + 30, gy + 20), (gx - 10, gy - 5),
                     (gx + 70, gy + 45), (gx + 5, gy + 5)):
            _move(viewer, x, y)
            transform = _resize_transform(start, "se", (x, y))
            candidate = viewer._transform_preview[a.id]
            _assert_points(candidate, apply_group(a, _offset(viewer),
                                                  transform))
        # The store is never touched while the preview is live.
        assert _store(viewer).get(a.id) is a
        assert _history_entries(viewer) == 0
        _release(viewer, gx + 5, gy + 5)
    assert _history_entries(viewer) == 1


def test_noop_resize_creates_no_history(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    # Cross the drag threshold, then return to the identity position.
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 30.0, gy)
        _move(viewer, gx, gy)
        _release(viewer, gx, gy)
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_subthreshold_grip_press_keeps_selection_no_history(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    b = _seed(viewer, _pen_stroke(p0=(100.0, 300.0), p1=(300.0, 300.0)))
    viewer._selection.set([a.id, b.id])
    gx, gy = _grip(viewer, "se")
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 1.0, gy + 1.0)
        _release(viewer, gx + 1.0, gy + 1.0)
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is b
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    assert _history_entries(viewer) == 0


# ===================================================== rotation: geometry

@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_rotation_orbits_group_about_shared_pivot(
        qtbot, tmp_path, cleared_settings, sign):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 80.0), p1=(200.0, 80.0)))
    b = _seed(viewer, _pen_stroke(p0=(300.0, 260.0), p1=(380.0, 260.0)))
    viewer._selection.set([a.id, b.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target(start, 90.0 * sign)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    transform = _rotate_transform(start, target)
    assert abs(transform.angle_deg) > 1.0
    for original in (a, b):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, _offset(viewer), transform)
        _assert_points(got, expected)
    assert _history_entries(viewer) == 1
    # One undo restores both pristine members.
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is b
    viewer.document_history.redo()
    for original in (a, b):
        _assert_points(_store(viewer).get(original.id),
                       apply_group(original, _offset(viewer), transform))


def test_rotation_rectangles_accumulate_rotation_deg(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    rect = _seed(viewer, _box_shape(ShapeKind.RECTANGLE, rotation_deg=15.0))
    ell = _seed(viewer, _box_shape(ShapeKind.ELLIPSE, rotation_deg=-20.0))
    viewer._selection.set([rect.id, ell.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    transform = _rotate_transform(start, target)
    for original in (rect, ell):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, _offset(viewer), transform)
        assert got.rotation_deg == pytest.approx(
            expected.rotation_deg, abs=1e-6)
        assert got.rect.x0 == pytest.approx(expected.rect.x0, abs=1e-4)
        assert got.width_mm == pytest.approx(original.width_mm, abs=1e-9)


def test_rotation_preserves_widths_and_moves_points(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _seed(viewer, _pen_stroke(width_mm=2.5))
    hl = _seed(viewer, _highlighter(width_mm=6.0))
    viewer._selection.set([stroke.id, hl.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    transform = _rotate_transform(start, target)
    for original in (stroke, hl):
        got = _store(viewer).get(original.id)
        # Pure rotation preserves stroke width exactly.
        assert got.width_mm == pytest.approx(original.width_mm, abs=1e-9)
        expected = apply_group(original, _offset(viewer), transform)
        _assert_points(got, expected)
        # Points actually moved (a real rotation, not a no-op).
        moved = any(
            abs(p.x - q.x) > 1e-6 or abs(p.y - q.y) > 1e-6
            for p, q in zip(original.points, got.points))
        assert moved


def test_rotation_wraparound_is_continuous(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    # Sweep through the -180/180 boundary; each sample must match the
    # normalized total delta and stay bounded.
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        for deg in (170.0, 181.0, 190.0, 200.0):
            tw = _rotate_target(start, deg)
            _move(viewer, tw[0], tw[1])
            transform = _rotate_transform(start, tw)
            assert -180.0 < transform.angle_deg <= 180.0
            candidate = viewer._transform_preview[a.id]
            _assert_points(candidate, apply_group(a, _offset(viewer),
                                                  transform))
        tw = _rotate_target(start, 200.0)
        _release(viewer, tw[0], tw[1])
    assert _history_entries(viewer) == 1


def test_noop_rotation_creates_no_history(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    # Drag the rotation grip but release back at the starting bearing.
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 20.0, gy + 10.0)
        _release(viewer, gx, gy)
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


# ================================================= interaction / safety

def test_handle_hit_priority_over_body_move(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    gx, gy = _grip(viewer, "se")
    # A grip press arms a resize, never a move.
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        gesture = viewer._move_gesture
        assert gesture is not None
        assert gesture["mode"] == "resize"
        assert gesture["handle"] == "se"
        _release(viewer, gx, gy)
    # A body press still arms a move.
    bx, by = _widget(viewer, 200.0, 150.0)
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        gesture = viewer._move_gesture
        assert gesture is not None
        assert gesture["mode"] == "move"
        _release(viewer, bx, by)


def test_body_move_unchanged_with_handles_present(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    bx, by = _widget(viewer, 200.0, 150.0)
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        _move(viewer, bx + _MOVE, by)
        _release(viewer, bx + _MOVE, by)
    got = _store(viewer).get(a.id)
    pdx = _MOVE / _factor(viewer)
    assert got.points[0].x == pytest.approx(a.points[0].x + pdx, abs=1e-4)
    assert got.points[0].y == pytest.approx(a.points[0].y, abs=1e-9)
    assert _history_entries(viewer) == 1


def test_lasso_tool_resizes_and_rotates(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    _drag_grip(viewer, "se", 50.0, 40.0, start=start)
    assert _store(viewer).get(a.id) is not a
    assert _history_entries(viewer) == 1
    # Rotation from the same Lasso selection.
    viewer._selection.set([a.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    assert _history_entries(viewer) == 2


# ============================================================ Pan editing

def test_pan_double_click_selects_and_shows_handles(qtbot, tmp_path,
                                                    cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    assert _selected_values(viewer) == (a.id.value,)
    points = viewer._selection_handle_points()
    assert points is not None
    # Every grip is hittable and returns the expected name.
    assert viewer._transform_handle_at(*points["se"]) == "se"
    assert viewer._transform_handle_at(*points["rotate"]) == "rotate"


def test_pan_resize_grip_resizes_selection(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    start = _start_oriented(viewer, "se")
    gx, gy = start["grip"]
    target = (gx + 50.0, gy + 40.0)
    _drag_grip(viewer, "se", 50.0, 40.0, start=start)
    got = _store(viewer).get(a.id)
    assert got is not a
    transform = _resize_transform_oriented(start, "se", target)
    _assert_points(got, apply_group(a, _offset(viewer), transform))
    assert _history_entries(viewer) == 1


def test_pan_rotation_grip_rotates_group_and_frame(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    got = _store(viewer).get(a.id)
    transform = _rotate_transform_oriented(start, target)
    _assert_points(got, apply_group(a, _offset(viewer), transform))
    # The frame itself rotated with the content.
    angle = _angle_from_corners(viewer._selection_frame_corners())
    assert angle == pytest.approx(90.0, abs=1.0)
    assert _history_entries(viewer) == 1


def test_pan_selected_body_moves_group(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        _move(viewer, bx + _MOVE, by)
        _release(viewer, bx + _MOVE, by)
    got = _store(viewer).get(a.id)
    pdx = _MOVE / _factor(viewer)
    assert got.points[0].x == pytest.approx(a.points[0].x + pdx, abs=1e-4)


def test_pan_grip_press_never_falls_through(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    gx, gy = _grip(viewer, "se")
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        gesture = viewer._move_gesture
        assert gesture is not None and gesture["mode"] == "resize"
        assert gesture["handle"] == "se"
        assert viewer._region_gesture is None
        _release(viewer, gx, gy)
    # A grip tap writes nothing.
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_pan_empty_canvas_still_pans(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, tool=_pan_tool(),
        sizes=[(600.0, 400.0)] * 4)
    _seed(viewer, _pen_stroke())
    before = viewer._scrollbar.value()
    x0, y0 = _widget(viewer, 300.0, 320.0)
    x1, y1 = _widget(viewer, 300.0, 260.0)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, x1, y1)
        _release(viewer, x1, y1)
    assert viewer._scrollbar.value() > before
    assert viewer._selection.has_selection is False
    assert viewer._move_gesture is None
    assert _history_entries(viewer) == 0


def test_pan_without_selection_has_no_handles(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _pen_stroke())
    assert viewer._selection_frame_corners() is None
    assert viewer._selection_handle_points() is None
    assert viewer._transform_handle_at(200.0, 150.0) is None


def test_pan_handles_hidden_on_tool_switch(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _pen_stroke())
    bx, by = _widget(viewer, 200.0, 150.0)
    _pan_double_click_widget(viewer, bx, by)
    assert viewer._selection_handle_points() is not None
    viewer._on_tool_selected(_pen_tool())
    assert viewer._selection_handle_points() is None


def test_preview_does_not_write_store(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 40.0, gy + 30.0)
        assert viewer._transform_preview is not None
        assert _store(viewer).get(a.id) is a
        assert _history_entries(viewer) == 0
    _release(viewer, gx + 40.0, gy + 30.0)


def test_commit_one_entry_and_exact_undo_redo_order(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    b = _seed(viewer, _pen_stroke(p0=(100.0, 300.0), p1=(300.0, 300.0)))
    c = _seed(viewer, _highlighter(p0=(150.0, 250.0), p1=(250.0, 250.0)))
    order = tuple(item.id.value for item in _store(viewer).page(0))
    viewer._selection.set([a.id, b.id, c.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    _drag_grip(viewer, "se", 60.0, 45.0, start=start)
    assert _history_entries(viewer) == 1
    assert tuple(item.id.value for item in _store(viewer).page(0)) == order
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is b
    assert _store(viewer).get(c.id) is c
    assert tuple(item.id.value for item in _store(viewer).page(0)) == order
    viewer.document_history.redo()
    assert tuple(item.id.value for item in _store(viewer).page(0)) == order
    assert _store(viewer).get(a.id) is not a
    assert viewer._move_gesture is None
    assert viewer._transform_preview is None


def test_escape_cancels_transform_preview(qtbot, tmp_path,
                                          cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        assert viewer._transform_preview is not None
        viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                       Qt.Key.Key_Escape,
                                       Qt.KeyboardModifier.NoModifier))
        assert viewer._transform_preview is None
        assert viewer._move_gesture is None
        _release(viewer, gx + 50.0, gy + 40.0)
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_tool_switch_cancels_transform_preview(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        assert viewer._transform_preview is not None
        viewer._on_tool_selected(_pen_tool())
        assert viewer._transform_preview is None
        assert viewer._move_gesture is None
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_hide_cancels_transform_preview(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        viewer.hideEvent(QHideEvent())
        assert viewer._transform_preview is None
        assert viewer._move_gesture is None
    assert _store(viewer).get(a.id) is a


def test_intervening_edit_aborts_whole_transform(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    b = _seed(viewer, _pen_stroke(p0=(100.0, 300.0), p1=(300.0, 300.0)))
    viewer._selection.set([a.id, b.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        # An intervening edit replaces a still-existing selected member.
        replacement = dataclasses.replace(b, width_mm=3.0)
        _store(viewer).remove(b.id)
        _store(viewer).insert_at(replacement, 0, 1)
        _release(viewer, gx + 50.0, gy + 40.0)
    # Whole operation aborted pre-mutation: nothing moved, no entry.
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is replacement
    assert _history_entries(viewer) == 0


def test_partial_stale_transforms_survivors_only(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    b = _seed(viewer, _pen_stroke(p0=(100.0, 300.0), p1=(300.0, 300.0)))
    viewer._selection.set([a.id, b.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        _store(viewer).remove(b.id)  # b vanishes mid-gesture
        _release(viewer, gx + 50.0, gy + 40.0)
    assert _store(viewer).get(a.id) is not a
    assert _history_entries(viewer) == 1
    assert _store(viewer).get(b.id) is None


def test_all_stale_transforms_nothing(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        _store(viewer).remove(a.id)
        _release(viewer, gx + 50.0, gy + 40.0)
    assert _store(viewer).get(a.id) is None
    assert _history_entries(viewer) == 0


# ===================================================== cross-page group

@pytest.mark.parametrize("mode", ["resize", "rotate"])
def test_cross_page_group_transforms_as_one_and_keeps_pages(
        qtbot, tmp_path, cleared_settings, mode):
    sizes = [(600.0, 200.0), (450.0, 160.0)]
    _window, viewer = _open_window(qtbot, tmp_path, sizes=sizes)
    a = _seed(viewer, _pen_stroke(p0=(80.0, 80.0), p1=(200.0, 80.0)), 0)
    b = _seed(viewer, _pen_stroke(p0=(80.0, 60.0), p1=(200.0, 60.0)), 1)
    off0 = _offset(viewer, 0)
    off1 = _offset(viewer, 1)
    viewer._selection.set([a.id, b.id])
    start = _start(viewer, "se" if mode == "resize" else "rotate")
    gx, gy = start["grip"]
    if mode == "resize":
        target = (gx + 50.0, gy + 40.0)
        _drag_grip(viewer, "se", 50.0, 40.0, start=start)
        transform = _resize_transform(start, "se", target)
    else:
        target = _rotate_target(start, 90.0)
        _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy,
                   start=start)
        transform = _rotate_transform(start, target)
    for original, off in ((a, off0), (b, off1)):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, off, transform)
        _assert_points(got, expected)
        # Page assignment is unchanged by resize/rotation.
        assert got.page_id.value == original.page_id.value
    assert _store(viewer).locate(a.id)[0] == 0
    assert _store(viewer).locate(b.id)[0] == 1
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is b
    viewer.document_history.redo()
    for original, off in ((a, off0), (b, off1)):
        _assert_points(_store(viewer).get(original.id),
                       apply_group(original, off, transform))


def test_rotated_shape_width_preserved_across_pages(qtbot, tmp_path,
                                                    cleared_settings):
    sizes = [(600.0, 200.0), (450.0, 160.0)]
    _window, viewer = _open_window(qtbot, tmp_path, sizes=sizes)
    r0 = _seed(viewer, _box_shape(ShapeKind.RECTANGLE, rotation_deg=25.0,
                                  rect=(60.0, 60.0, 160.0, 120.0)), 0)
    r1 = _seed(viewer, _box_shape(ShapeKind.RECTANGLE, rotation_deg=-15.0,
                                  rect=(50.0, 40.0, 150.0, 100.0)), 1)
    viewer._selection.set([r0.id, r1.id])
    start = _start(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    transform = _rotate_transform(start, target)
    for original, off in ((r0, _offset(viewer, 0)), (r1, _offset(viewer, 1))):
        got = _store(viewer).get(original.id)
        expected = apply_group(original, off, transform)
        assert got.rotation_deg == pytest.approx(
            expected.rotation_deg, abs=1e-6)
        assert got.width_mm == pytest.approx(original.width_mm, abs=1e-9)
    assert _history_entries(viewer) == 1


# ====================================================== oriented frame

def test_rotation_rotates_the_frame_itself(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    # The committed (idle) frame is the *start* frame rotated about the
    # same pivot — an oriented quad, not the axis-aligned bounds of the
    # rotated content.
    factor = start["factor"]
    nw, ne, se, sw = [(x / factor, y / factor) for x, y in start["corners"]]
    px = (nw[0] + se[0]) / 2.0
    py = (nw[1] + se[1]) / 2.0
    transform = _rotate_transform_oriented(start, target)
    rad = math.radians(transform.angle_deg)
    c = math.cos(rad)
    s = math.sin(rad)

    def rot(p):
        ex, ey = p[0] - px, p[1] - py
        return ((px + ex * c - ey * s) * factor,
                (py + ex * s + ey * c) * factor)

    expected = [rot(p) for p in ((nw, ne, se, sw))]
    got = viewer._selection_frame_corners()
    for (gx0, gy0), (ex0, ey0) in zip(got, expected):
        assert gx0 == pytest.approx(ex0, abs=1e-3)
        assert gy0 == pytest.approx(ey0, abs=1e-3)
    assert _angle_from_corners(got) == pytest.approx(90.0, abs=1.0)
    # A quarter turn swaps the frame's side lengths exactly.
    left, top, width, height = viewer._selection_frame_widget_rect()
    assert width == pytest.approx(
        math.hypot(sw[0] - nw[0], sw[1] - nw[1]) * factor, abs=1e-3)
    assert height == pytest.approx(
        math.hypot(ne[0] - nw[0], ne[1] - nw[1]) * factor, abs=1e-3)


def test_resize_grips_follow_the_oriented_frame(qtbot, tmp_path,
                                                cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 45.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    corners = viewer._selection_frame_corners()
    assert _angle_from_corners(corners) == pytest.approx(45.0, abs=1.0)
    points = viewer._selection_handle_points()
    expected = _expected_handles(corners)
    for name in ("nw", "n", "ne", "e", "se", "s", "sw", "w", "rotate"):
        assert points[name][0] == pytest.approx(expected[name][0], abs=1e-6)
        assert points[name][1] == pytest.approx(expected[name][1], abs=1e-6)
    # Every oriented grip is hittable.
    for name in ("nw", "ne", "se", "sw", "rotate"):
        assert viewer._transform_handle_at(*points[name]) == name


def test_oriented_frame_tracks_zoom_without_losing_orientation(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 30.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    corners0 = viewer._selection_frame_corners()
    angle0 = _angle_from_corners(corners0)

    def _edge_pt(corners):
        # Page-pt length of the top edge with the constant screen pad
        # removed (the pad never scales, so the *content* edge is the
        # view-independent invariant).
        edge = math.hypot(corners[1][0] - corners[0][0],
                          corners[1][1] - corners[0][1])
        return (edge - 2.0 * _SELECT_FRAME_PAD_PX) / _factor(viewer)

    assert angle0 == pytest.approx(30.0, abs=1.0)
    side0 = _edge_pt(corners0)
    for zoom in (1.5, 2.0):
        viewer.set_zoom(zoom)
        qtbot.wait(30)
        corners = viewer._selection_frame_corners()
        # Orientation is view-independent: zooming re-projects the same
        # oriented frame instead of collapsing to an axis-aligned box.
        assert _angle_from_corners(corners) == pytest.approx(angle0, abs=1e-6)
        assert _edge_pt(corners) == pytest.approx(side0, rel=1e-6)


def test_followup_resize_uses_rotated_local_axes(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    rect = _seed(viewer, _box_shape(ShapeKind.RECTANGLE))
    other = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([rect.id, other.id])
    rot_start = _start_oriented(viewer, "rotate")
    gx, gy = rot_start["grip"]
    target = _rotate_target_oriented(rot_start, 90.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy,
               start=rot_start)
    # The follow-up resize transforms the *post-rotation* stored geometry
    # (the gesture-start snapshots), not the pre-rotation originals.
    snap_rect = _store(viewer).get(rect.id)
    snap_other = _store(viewer).get(other.id)
    # Follow-up resize starts from the *rotated* frame.
    start = _start_oriented(viewer, "se")
    assert _angle_from_corners(start["corners"]) == pytest.approx(90.0,
                                                                  abs=1.0)
    gx, gy = start["grip"]
    tgt = (gx + 40.0, gy + 30.0)
    _drag_grip(viewer, "se", 40.0, 30.0, start=start)
    transform = _resize_transform_oriented(start, "se", tgt)
    assert transform.frame_deg == pytest.approx(90.0, abs=1.0)
    for snap in (snap_rect, snap_other):
        got = _store(viewer).get(snap.id)
        expected = apply_group(snap, _offset(viewer), transform)
        if isinstance(snap, Shape):
            assert got.rect.x0 == pytest.approx(expected.rect.x0, abs=1e-3)
            assert got.rect.x1 == pytest.approx(expected.rect.x1, abs=1e-3)
            assert got.rotation_deg == pytest.approx(
                expected.rotation_deg, abs=1e-6)
        else:
            _assert_points(got, expected, tol=1e-3)
    assert _history_entries(viewer) == 2


def test_rotation_preview_samples_do_not_drift(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        for deg in (30.0, 75.0, 20.0, 120.0, 150.0):
            tw = _rotate_target_oriented(start, deg)
            _move(viewer, tw[0], tw[1])
            transform = _rotate_transform_oriented(start, tw)
            candidate = viewer._transform_preview[a.id]
            _assert_points(candidate, apply_group(a, _offset(viewer),
                                                  transform))
            corners = viewer._selection_frame_corners()
            assert _angle_from_corners(corners) == pytest.approx(
                transform.angle_deg, abs=1e-6)
        _release(viewer, gx, gy)
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_undo_restores_geometry_and_resets_frame_orientation(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    rect = _seed(viewer, _box_shape(ShapeKind.RECTANGLE))
    viewer._selection.set([rect.id])
    _rotate_selection_90(viewer, direction=1.0)
    assert _angle_from_corners(viewer._selection_frame_corners()) \
        == pytest.approx(90.0, abs=1.0)
    viewer._on_undo_requested()
    # Geometry is restored exactly, and the frame is recomputed from the
    # restored content (axis-aligned again) rather than left stale.
    assert _store(viewer).get(rect.id) is rect
    corners = viewer._selection_frame_corners()
    assert _angle_from_corners(corners) == pytest.approx(0.0, abs=1e-6)
    viewer._on_redo_requested()
    assert _store(viewer).get(rect.id) is not rect


# ======================================================= resize visuals

def _settle_phase(viewer):
    viewer._selection_timer.stop()
    viewer._selection_phase = 15.0  # sine peak -> pulse at maximum
    viewer.update()


def _top_band(corners):
    nw, ne = corners[0], corners[1]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    top = (nw[1] + ne[1]) / 2.0
    return (min(xs) - 10.0, top - 30.0, max(xs) + 10.0, top - 8.0)


def test_rotation_grip_and_connector_absent_during_resize(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _settle_phase(viewer)
    band = _top_band(viewer._selection_frame_corners())
    with _chrome_hidden(viewer):
        idle = viewer.grab().toImage()
        assert _accent_count(idle, *band) > 0    # grip + connector visible
        start = _start_oriented(viewer, "se")
        gx, gy = start["grip"]
        _press(viewer, gx, gy)
        _move(viewer, gx + 40.0, gy + 30.0)
        assert viewer._move_gesture["mode"] == "resize"
        during = viewer.grab().toImage()
        assert _accent_count(during, *band) == 0
        _release(viewer, gx + 40.0, gy + 30.0)
    # Restored once the resize ends.
    _settle_phase(viewer)
    band = _top_band(viewer._selection_frame_corners())
    with _chrome_hidden(viewer):
        after = viewer.grab().toImage()
        assert _accent_count(after, *band) > 0


def test_cancel_resize_restores_idle_visual(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "se")
    gx, gy = start["grip"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, gx + 50.0, gy + 40.0)
        assert _accent_count(viewer.grab().toImage(),
                             *_top_band(viewer._selection_frame_corners())) == 0
        viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                       Qt.Key.Key_Escape,
                                       Qt.KeyboardModifier.NoModifier))
        assert viewer._move_gesture is None
        assert _store(viewer).get(a.id) is a
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        assert _accent_count(viewer.grab().toImage(),
                             *_top_band(viewer._selection_frame_corners())) > 0


def test_oriented_frame_is_drawn_not_its_bounding_box(qtbot, tmp_path,
                                                      cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(120.0, 90.0), p1=(280.0, 150.0)))
    viewer._selection.set([a.id])
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 45.0)
    _drag_grip(viewer, "rotate", target[0] - gx, target[1] - gy, start=start)
    _settle_phase(viewer)
    corners = viewer._selection_frame_corners()
    assert _angle_from_corners(corners) == pytest.approx(45.0, abs=1.0)
    left, top, width, height = viewer._selection_frame_widget_rect()
    with _chrome_hidden(viewer):
        image = viewer.grab().toImage()
        # Accent ink sits on the diagonal frame near its (rotated) NW
        # vertex, but NOT at the axis-aligned bound's top-left corner,
        # which lies outside the oriented quad.
        nw = corners[0]
        assert _accent_count(image, nw[0] - 5, nw[1] - 5,
                             nw[0] + 5, nw[1] + 5) > 0
        assert _accent_count(image, left + 1, top + 1,
                             left + 6, top + 6) == 0


# ========================================================= move visuals

def _body_widget(viewer, lx=200.0, ly=150.0):
    """Widget point on the seeded stroke's body (inside the frame)."""
    return _widget(viewer, lx, ly)


def _rotate_grip_box(viewer, pad=9.0):
    """A small widget-space box centred on the displayed rotation grip.

    The grip sits a fixed 22 px outward from the oriented top-edge
    midpoint, so this box contains rotation-centre ink and nothing else
    (no corner/edge resize grip, no frame line).
    """
    points = viewer._selection_handle_points()
    assert points is not None
    rx, ry = points["rotate"]
    return (rx - pad, ry - pad, rx + pad, ry + pad)


def test_rotation_grip_and_connector_absent_during_move(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _settle_phase(viewer)
    band = _top_band(viewer._selection_frame_corners())
    with _chrome_hidden(viewer):
        assert _accent_count(viewer.grab().toImage(), *band) > 0
        bx, by = _body_widget(viewer)
        _press(viewer, bx, by)
        # Hidden the instant the move gesture arms — no delay, no first
        # painted frame that still trails the rotation centre.
        assert viewer._move_gesture["mode"] == "move"
        assert _accent_count(
            viewer.grab().toImage(),
            *_top_band(viewer._selection_frame_corners())) == 0
        # Still hidden once the group actually moves: neither the
        # connector (the band above the top edge) nor the marker (the
        # grip's would-be position) is rendered.
        _move(viewer, bx + 120.0, by + 90.0)
        image = viewer.grab().toImage()
        assert _accent_count(
            image, *_top_band(viewer._selection_frame_corners())) == 0
        assert _accent_count(image, *_rotate_grip_box(viewer)) == 0
        # Unrelated visuals keep working: the oriented frame and its
        # eight resize grips are still drawn.
        nw = viewer._selection_frame_corners()[0]
        assert _accent_count(image, nw[0] - 6, nw[1] - 6,
                             nw[0] + 6, nw[1] + 6) > 0
        _release(viewer, bx + 120.0, by + 90.0)
    assert _history_entries(viewer) == 1


def test_move_preview_frame_follows_the_selection(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    before = tuple(viewer._selection_frame_corners())
    angle_before = _angle_from_corners(before)
    bx, by = _body_widget(viewer)
    dx, dy = 120.0, 90.0
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        _move(viewer, bx + dx, by + dy)
        during = tuple(viewer._selection_frame_corners())
        _release(viewer, bx + dx, by + dy)
    # The frame translated exactly with the pointer delta and kept its
    # orientation — it is never left behind at the press position.
    for (sx, sy), (wx, wy) in zip(before, during):
        assert wx == pytest.approx(sx + dx, abs=1e-6)
        assert wy == pytest.approx(sy + dy, abs=1e-6)
    assert _angle_from_corners(during) == pytest.approx(angle_before, abs=1e-6)


def test_move_commit_restores_rotation_centre_at_new_frame(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    old_band = _top_band(viewer._selection_frame_corners())
    bx, by = _body_widget(viewer)
    dx, dy = 120.0, 90.0
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        _move(viewer, bx + dx, by + dy)
        _release(viewer, bx + dx, by + dy)
    assert _history_entries(viewer) == 1
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        image = viewer.grab().toImage()
        # Restored above the *committed* frame, at its displayed grip.
        assert _accent_count(
            image, *_top_band(viewer._selection_frame_corners())) > 0
        assert _accent_count(image, *_rotate_grip_box(viewer)) > 0
        # Nothing stale (no brief line after release) at the old place.
        assert _accent_count(image, *old_band) == 0


def test_cancel_move_restores_idle_visual_without_mutation(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    band = _top_band(viewer._selection_frame_corners())
    bx, by = _body_widget(viewer)
    with _chrome_hidden(viewer):
        _press(viewer, bx, by)
        _move(viewer, bx + 120.0, by + 90.0)
        assert _accent_count(
            viewer.grab().toImage(),
            *_top_band(viewer._selection_frame_corners())) == 0
        viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                       Qt.Key.Key_Escape,
                                       Qt.KeyboardModifier.NoModifier))
        assert viewer._move_gesture is None
        assert viewer._transform_preview is None
    # Cancelling mutates neither geometry nor history.
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        assert _accent_count(viewer.grab().toImage(),
                             *_top_band(viewer._selection_frame_corners())) > 0


def test_rotation_gesture_keeps_the_rotation_centre(qtbot, tmp_path,
                                                    cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _settle_phase(viewer)
    start = _start_oriented(viewer, "rotate")
    gx, gy = start["grip"]
    target = _rotate_target_oriented(start, 30.0)
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        _move(viewer, target[0], target[1])
        assert viewer._move_gesture["mode"] == "rotate"
        # A rotation keeps the grip and its connector (the affordance
        # being dragged); only move/resize suppress them.
        assert _accent_count(viewer.grab().toImage(),
                             *_rotate_grip_box(viewer)) > 0
        _release(viewer, target[0], target[1])
    assert _history_entries(viewer) == 1


def test_pan_move_suppresses_rotation_centre(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke())
    bx, by = _body_widget(viewer)
    _pan_double_click_widget(viewer, bx, by)
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        assert _accent_count(viewer.grab().toImage(),
                             *_top_band(viewer._selection_frame_corners())) > 0
        _press(viewer, bx, by)
        _move(viewer, bx + 120.0, by + 90.0)
        assert viewer._move_gesture["mode"] == "move"
        band = _top_band(viewer._selection_frame_corners())
        assert _accent_count(viewer.grab().toImage(), *band) == 0
        _release(viewer, bx + 120.0, by + 90.0)
    assert _history_entries(viewer) == 1
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        assert _accent_count(viewer.grab().toImage(),
                             *_top_band(viewer._selection_frame_corners())) > 0
