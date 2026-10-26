"""M6.7 shape annotation integration.

Straight Line, Arrow, Rectangle, Ellipse and Polygon are real,
document-owned annotations: a transient preview is produced by the same
geometry code as the commit, the canonical ``Shape`` is committed through
the existing ``AddAnnotationCommand``, and rendering reads the owning
document's ``AnnotationStore`` (the viewer keeps no parallel committed
collection). Coverage: Qt-free handler geometry (snapping, constraints,
polygon protocol), the model's additive fields, viewer routing and
preview, store-backed rendering, per-slot properties, and the shared
undo/redo history (including mixed tools).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from annotations.interaction import (
    SHAPE_TOOL_KINDS,
    InteractionController,
    ShapeSnapContext,
)
from annotations.models import Shape, ShapeKind
from core.types import PageId, Point
from tests.helpers import make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

ABS = 1e-6


# --------------------------------------------------------------- fixtures

def _shape_tool(kind, **kw):
    base = dict(kind=kind, name="Shape", color="#e01b24", width_mm=1.5,
                opacity=0.8)
    base.update(kw)
    return ToolInstance(**base)


def _pen_tool(color="#ff0000", width_mm=1.0, opacity=0.9):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color=color,
                        width_mm=width_mm, opacity=opacity)


def _hl_tool(color="#FFFF00", width=3.0, opacity=0.4):
    return ToolInstance(kind=ToolType.HIGHLIGHTER, name="Highlight",
                        color=color, width_mm=width, opacity=opacity)


def _eraser_tool(width_mm=2.0):
    return ToolInstance(kind=ToolType.ERASER, name="Eraser", width_mm=width_mm)


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

        keys = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                "inkfall/m5/toolbar/active",
                "inkfall/m5/toolbar/custom_colors",
                "inkfall/m5/toolbar/presets")
        for key in keys:
            QSettings().remove(key)
    except Exception:
        pass


def _press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _move(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _dbl(viewer, x, y):
    viewer.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _open_window(qtbot, tmp_path, tool=None, name="m67.pdf", sizes=None):
    from app.main_window import MainWindow

    if sizes is None:
        pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                             page_height=800.0)
    else:
        pdf = make_sized_pdf(str(tmp_path / name), sizes)
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


def _page_id(viewer, page_index=0):
    return viewer._pipeline.layout.entry(page_index).page_id


def _store(viewer):
    return viewer._document.annotations


def _shapes(viewer, page_index=0):
    return [a for a in _store(viewer).page(page_index)
            if isinstance(a, Shape)]


def _widget(viewer, lx, ly, page_index=0):
    """Invert ``_event_to_page_pt`` for one page (test mapping helper)."""
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


def _drag(viewer, p0, p1, page_index=0):
    a = _widget(viewer, *p0, page_index)
    b = _widget(viewer, *p1, page_index)
    _press(viewer, *a)
    _move(viewer, *b)
    _release(viewer, *b)
    return a, b


def _click(viewer, p, page_index=0):
    w = _widget(viewer, *p, page_index)
    _press(viewer, *w)
    _release(viewer, *w)
    return w


def _drive_ink(viewer, page_id, samples, highlighter=False):
    it = viewer._interaction
    t = 1000
    press = (it.handle_highlight_press if highlighter
             else it.handle_pen_press)
    move = it.handle_highlight_move if highlighter else it.handle_pen_move
    release = (it.handle_highlight_release if highlighter
               else it.handle_pen_release)
    viewer._apply_intent(press(page_id, samples[0][0], samples[0][1], 1.0, t))
    for sample in samples[1:]:
        t += 16
        viewer._apply_intent(move(page_id, sample[0], sample[1], 1.0, t))
    last = samples[-1]
    viewer._apply_intent(release(page_id, last[0], last[1], 1.0, t + 16))


def _erase_line(viewer, page_id, x0, y0, x1, y1):
    it = viewer._interaction
    viewer._apply_intent(it.handle_eraser_press(page_id, x0, y0))
    viewer._apply_intent(it.handle_eraser_move(page_id, x1, y1))
    viewer._apply_intent(it.handle_eraser_release(page_id, x1, y1))


def _pixel(viewer, x, y):
    return viewer.grab().toImage().pixelColor(int(x), int(y))


def _is_red(color) -> bool:
    return (color.red() > 150 and color.green() < 150 and color.blue() < 150)


# -------------------------------------------------- Part A: Qt-free handler

def test_handler_rectangle_commits_one_shape():
    c = InteractionController(_shape_tool(ToolType.RECTANGLE))
    pid = PageId()
    assert "shape_update" in c.handle_shape_press(pid, 10.0, 20.0)
    assert "shape_update" in c.handle_shape_move(pid, 60.0, 90.0)
    intent = c.handle_shape_release(pid, 60.0, 90.0)
    shape = intent["shape_done"]
    assert shape.kind is ShapeKind.RECTANGLE
    assert shape.rect.x0 == pytest.approx(10.0)
    assert shape.rect.y0 == pytest.approx(20.0)
    assert shape.rect.width == pytest.approx(50.0)
    assert shape.rect.height == pytest.approx(70.0)
    assert shape.points == ()
    assert c.shape_active() is False


def test_handler_line_endpoints_keep_direction():
    c = InteractionController(_shape_tool(ToolType.LINE))
    pid = PageId()
    c.handle_shape_press(pid, 100.0, 100.0)
    shape = c.handle_shape_release(pid, 10.0, 40.0)["shape_done"]
    assert shape.kind is ShapeKind.LINE
    assert (shape.points[0].x, shape.points[0].y) == (100.0, 100.0)
    assert (shape.points[1].x, shape.points[1].y) == (10.0, 40.0)


def test_handler_arrow_direction_reversed():
    c = InteractionController(_shape_tool(ToolType.ARROW, end_ending="arrow"))
    pid = PageId()
    c.handle_shape_press(pid, 300.0, 100.0)
    shape = c.handle_shape_release(pid, 100.0, 100.0)["shape_done"]
    assert shape.kind is ShapeKind.ARROW
    assert shape.points[0].x == pytest.approx(300.0)
    assert shape.points[1].x == pytest.approx(100.0)
    assert shape.end_ending == "arrow"


def test_handler_degenerate_line_cancelled():
    c = InteractionController(_shape_tool(ToolType.LINE))
    pid = PageId()
    c.handle_shape_press(pid, 5.0, 5.0)
    assert c.handle_shape_release(pid, 5.0, 5.0) == {"shape_cancelled": True}
    assert c.shape_active() is False


def test_handler_degenerate_rectangle_cancelled():
    c = InteractionController(_shape_tool(ToolType.RECTANGLE))
    pid = PageId()
    c.handle_shape_press(pid, 5.0, 5.0)
    assert c.handle_shape_release(pid, 5.2, 50.0) == {"shape_cancelled": True}


def test_handler_raw_viewport_input_never_creates_shape():
    c = InteractionController(_shape_tool(ToolType.RECTANGLE))
    assert c.handle_press(10, 10) == {"noop": True}
    assert c.handle_move(20, 20) == {"noop": True}
    assert c.handle_release(20, 20) == {"noop": True}


def test_handler_square_constraint():
    c = InteractionController(_shape_tool(ToolType.RECTANGLE, square=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    shape = c.handle_shape_release(pid, 40.0, 10.0)["shape_done"]
    assert shape.rect.width == pytest.approx(shape.rect.height)


def test_handler_circle_constraint():
    c = InteractionController(_shape_tool(ToolType.ELLIPSE, circle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    shape = c.handle_shape_release(pid, 10.0, 50.0)["shape_done"]
    assert shape.kind is ShapeKind.ELLIPSE
    assert shape.rect.width == pytest.approx(shape.rect.height)


def test_handler_snap_angle_constrains_direction():
    c = InteractionController(_shape_tool(ToolType.LINE, snap_angle=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    shape = c.handle_shape_release(pid, 100.0, 10.0)["shape_done"]
    dx = shape.points[1].x
    dy = shape.points[1].y
    # Snapped to 0 degrees (nearest 15-degree multiple of ~5.7 degrees).
    assert dy == pytest.approx(0.0, abs=1e-6)
    assert dx > 0.0


def test_handler_connect_endpoints_snaps():
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 50.0),), tol_pt=10.0)
    c = InteractionController(
        _shape_tool(ToolType.LINE, connect_endpoints=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    shape = c.handle_shape_release(pid, 105.0, 52.0)["shape_done"]
    assert shape.points[1].x == pytest.approx(100.0)
    assert shape.points[1].y == pytest.approx(50.0)


def test_handler_connect_intersections_snaps():
    seg = (Point(150.0, 50.0), Point(150.0, 150.0))
    ctx = ShapeSnapContext(segments=(seg,), tol_pt=10.0)
    c = InteractionController(
        _shape_tool(ToolType.LINE, connect_intersections=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 100.0, snap=ctx)
    shape = c.handle_shape_release(pid, 160.0, 100.0)["shape_done"]
    assert shape.points[1].x == pytest.approx(150.0)
    assert shape.points[1].y == pytest.approx(100.0)


def test_handler_latches_properties_at_press():
    import dataclasses

    tool = _shape_tool(ToolType.LINE, color="#111111", width_mm=1.0)
    c = InteractionController(tool)
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    # A mid-gesture preset edit on the same slot must not alter the shape
    # being drawn: properties are latched at press time.
    c._tool = dataclasses.replace(tool, color="#999999", width_mm=5.0)
    shape = c.handle_shape_release(pid, 50.0, 0.0)["shape_done"]
    assert shape.color == "#111111"
    assert shape.width_mm == pytest.approx(1.0)


def test_handler_polygon_vertices_ordered():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    c.handle_shape_press(pid, 100.0, 0.0)
    c.handle_shape_release(pid, 100.0, 0.0)
    c.handle_shape_press(pid, 100.0, 100.0)
    c.handle_shape_release(pid, 100.0, 100.0)
    shape = c.handle_shape_press(pid, 3.0, 3.0)["shape_done"]
    assert shape.kind is ShapeKind.POLYGON
    assert [(p.x, p.y) for p in shape.points] == [
        (0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]


def test_handler_polygon_double_click_finishes_once():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    for pt in ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0)):
        c.handle_shape_press(pid, *pt)
        c.handle_shape_release(pid, *pt)
    intent = c.handle_shape_double_click()
    assert "shape_done" in intent
    assert len(intent["shape_done"].points) == 3
    assert c.shape_active() is False
    # A second finish on a closed polygon is a no-op (no duplicate commit).
    assert c.handle_shape_double_click() == {"noop": True}


def test_handler_polygon_too_few_vertices_cancels():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    c.handle_shape_press(pid, 50.0, 0.0)
    c.handle_shape_release(pid, 50.0, 0.0)
    assert c.handle_shape_double_click() == {"shape_cancelled": True}


def test_handler_polygon_dedups_consecutive_vertex():
    c = InteractionController(_shape_tool(ToolType.POLYGON))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    assert c.handle_shape_press(pid, 0.0, 0.0) == {"noop": True}


def test_handler_polygon_always_closed_even_with_legacy_off():
    # M6.7.2: Polygon is always closed. An explicit close_path=True and a
    # legacy/default close_path=False both commit a closed polygon; there
    # is no user-facing control and no persisted value that can open it.
    for kw in ({"close_path": True}, {"close_path": False}, {}):
        c = InteractionController(_shape_tool(ToolType.POLYGON, **kw))
        pid = PageId()
        for pt in ((0.0, 0.0), (100.0, 0.0), (100.0, 100.0)):
            c.handle_shape_press(pid, *pt)
            c.handle_shape_release(pid, *pt)
        shape = c.handle_shape_double_click()["shape_done"]
        assert shape.close_path is True
        assert c.shape_active() is False


def test_handler_cancel_resets_gesture():
    c = InteractionController(_shape_tool(ToolType.RECTANGLE))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    assert c.handle_shape_move(pid, 50.0, 50.0).get("shape_update") is not None
    assert c.cancel() == {"shape_cancelled": True}
    assert c.shape_active() is False
    assert c.handle_shape_move(pid, 60.0, 60.0) == {"noop": True}


# ----------------------------------------------------- Part B: shape model

def test_shape_kinds_cover_all_required():
    assert {k.value for k in ShapeKind} == {
        "line", "rectangle", "ellipse", "arrow", "polygon"}


def test_shape_model_additive_defaults_and_legacy_construction():
    # Legacy keyword construction (used by existing store tests) still works.
    assert Shape().kind is ShapeKind.RECTANGLE
    assert Shape(kind=ShapeKind.ARROW).kind is ShapeKind.ARROW
    s = Shape()
    assert s.opacity == 1.0
    assert s.fill_enabled is False
    assert s.fill_color == "#ffffff"
    assert s.fill_opacity == 1.0
    assert s.line_style == "solid"
    assert s.start_ending == "none"
    assert s.end_ending == "none"
    assert s.points == ()
    assert s.close_path is False


def test_shape_model_normalizes_safely():
    s = Shape(opacity=5.0, fill_opacity=-2.0, line_style="bogus",
              start_ending="zzz", end_ending="nope",
              points=[Point(1.0, 2.0)])
    assert s.opacity == 1.0
    assert s.fill_opacity == 0.0
    assert s.line_style == "solid"
    assert s.start_ending == "none"
    assert s.end_ending == "none"
    assert isinstance(s.points, tuple)


def test_shape_model_is_qt_free():
    source = Path("annotations/models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(
                    ("PySide6", "PySide2", "PyQt")), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("PySide6", "PySide2", "PyQt")), node.module


def test_interaction_handler_is_qt_free():
    source = Path("annotations/interaction.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(
                    ("PySide6", "PySide2", "PyQt")), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                ("PySide6", "PySide2", "PyQt")), node.module
    assert "class ShapeHandler" in source


# ---------------------------------------------- Part C: viewer integration

def test_line_drag_creates_exactly_one_line(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 100.0))
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        s = shapes[0]
        assert s.kind is ShapeKind.LINE
        assert s.color == "#e01b24"
        assert s.opacity == pytest.approx(0.8)
        assert s.width_mm == pytest.approx(1.5)
    finally:
        viewer.close_document()


def test_arrow_drag_is_directed(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.ARROW, end_ending="arrow"))
    try:
        _drag(viewer, (300.0, 100.0), (100.0, 250.0))
        s = _shapes(viewer)[0]
        assert s.kind is ShapeKind.ARROW
        assert s.points[0].x == pytest.approx(300.0)
        assert s.points[1].x == pytest.approx(100.0)
        assert s.end_ending == "arrow"
    finally:
        viewer.close_document()


def test_rectangle_works_in_every_drag_direction(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        corners = [((100.0, 100.0), (300.0, 250.0)),
                   ((300.0, 250.0), (100.0, 100.0)),
                   ((300.0, 100.0), (100.0, 250.0)),
                   ((100.0, 250.0), (300.0, 100.0))]
        for i, (a, b) in enumerate(corners):
            _drag(viewer, a, b)
            s = _shapes(viewer)[i]
            assert s.rect.x0 == pytest.approx(100.0)
            assert s.rect.y0 == pytest.approx(100.0)
            assert s.rect.width == pytest.approx(200.0)
            assert s.rect.height == pytest.approx(150.0)
    finally:
        viewer.close_document()


def test_ellipse_valid_bounds(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.ELLIPSE))
    try:
        _drag(viewer, (120.0, 200.0), (360.0, 340.0))
        s = _shapes(viewer)[0]
        assert s.kind is ShapeKind.ELLIPSE
        assert s.rect.width > 0.0 and s.rect.height > 0.0
    finally:
        viewer.close_document()


def test_polygon_click_close_commits_once(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.POLYGON,
                                               fill_enabled=True,
                                               fill_color="#2255ff"))
    try:
        for pt in ((100.0, 100.0), (300.0, 100.0), (300.0, 250.0)):
            _click(viewer, pt)
        assert _shapes(viewer) == []
        assert viewer._live_shape is not None
        _click(viewer, (103.0, 103.0))  # near the first vertex -> close
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        s = shapes[0]
        assert s.kind is ShapeKind.POLYGON
        assert len(s.points) == 3
        assert s.fill_enabled is True
        assert s.fill_color == "#2255ff"
        assert viewer._live_shape is None
    finally:
        viewer.close_document()


def test_polygon_double_click_single_commit(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.POLYGON))
    try:
        _click(viewer, (100.0, 100.0))
        _click(viewer, (300.0, 100.0))
        # A real double-click ends with the first click placing a vertex
        # and the second delivered as a DblClick that only closes.
        _click(viewer, (300.0, 250.0))
        w = _widget(viewer, 300.0, 250.0)
        _dbl(viewer, *w)
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        assert len(shapes[0].points) == 3
        history = viewer._history_for(viewer._document, create=False)
        assert history.stack.count() == 1
    finally:
        viewer.close_document()


def test_incomplete_gesture_creates_no_annotation(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        a = _widget(viewer, 100.0, 100.0)
        _press(viewer, *a)
        _move(viewer, a[0] + 80, a[1] + 60)
        # Switch away mid-drag exactly like the shelf callback does
        # (cancel is applied, then the new tool is installed).
        viewer._apply_intent(viewer._interaction.cancel())
        viewer._interaction.set_tool(_shape_tool(ToolType.LINE))
        assert _shapes(viewer) == []
        history = viewer._history_for(viewer._document, create=False)
        assert history is None or history.can_undo() is False
    finally:
        viewer.close_document()


def test_repeated_release_does_not_double_commit(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        a, b = _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        _release(viewer, *b)
        _release(viewer, *b)
        assert len(_shapes(viewer)) == 1
        history = viewer._history_for(viewer._document, create=False)
        assert history.stack.count() == 1
    finally:
        viewer.close_document()


def test_degenerate_shape_not_committed(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        a = _widget(viewer, 150.0, 150.0)
        _press(viewer, *a)
        _release(viewer, *a)
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()


# ---------------------------------------------- Part D: properties/rendering

def test_outline_properties_preserved(qtbot, tmp_path, cleared_settings):
    tool = _shape_tool(ToolType.LINE, color="#123456", width_mm=2.5,
                       opacity=0.5, line_style="dashed",
                       start_ending="dot", end_ending="arrow")
    _w, viewer = _open_window(qtbot, tmp_path, tool=tool)
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 200.0))
        s = _shapes(viewer)[0]
        assert s.color == "#123456"
        assert s.width_mm == pytest.approx(2.5)
        assert s.opacity == pytest.approx(0.5)
        assert s.line_style == "dashed"
        assert s.start_ending == "dot"
        assert s.end_ending == "arrow"
    finally:
        viewer.close_document()


def test_fill_and_transparency_stored(qtbot, tmp_path, cleared_settings):
    tool = _shape_tool(ToolType.RECTANGLE, fill_enabled=True,
                       fill_color="#00cc44", fill_opacity=0.35)
    _w, viewer = _open_window(qtbot, tmp_path, tool=tool)
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        s = _shapes(viewer)[0]
        assert s.fill_enabled is True
        assert s.fill_color == "#00cc44"
        assert s.fill_opacity == pytest.approx(0.35)
    finally:
        viewer.close_document()


def test_preview_matches_committed_geometry(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE,
                                               snap_angle=True))
    try:
        a = _widget(viewer, 100.0, 100.0)
        b = _widget(viewer, 300.0, 130.0)
        _press(viewer, *a)
        _move(viewer, *b)
        preview = viewer._live_shape
        assert preview is not None
        _release(viewer, *b)
        done = _shapes(viewer)[0]
        assert preview.points == done.points
        assert preview.rect == done.rect
        assert preview.color == done.color
    finally:
        viewer.close_document()


def test_polygon_close_path_draws_closing_edge(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=2.0,
                         opacity=1.0, close_path=True))
    try:
        verts = [(150.0, 150.0), (350.0, 150.0), (350.0, 350.0),
                 (150.0, 350.0)]
        for v in verts:
            _click(viewer, v)
        _dbl(viewer, *_widget(viewer, *verts[-1]))
        viewer.repaint()
        edge = _widget(viewer, 150.0, 250.0)  # closing edge (v3 -> v0)
        assert _is_red(_pixel(viewer, *edge)) is True
    finally:
        viewer.close_document()


def test_polygon_always_closed_when_close_path_off(
        qtbot, tmp_path, cleared_settings):
    # M6.7.2: a legacy/default close_path=False cannot produce an open
    # polygon — the closing edge is always drawn.
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=2.0,
                         opacity=1.0))  # close_path defaults off
    try:
        verts = [(150.0, 150.0), (350.0, 150.0), (350.0, 350.0),
                 (150.0, 350.0)]
        for v in verts:
            _click(viewer, v)
        _dbl(viewer, *_widget(viewer, *verts[-1]))
        viewer.repaint()
        edge = _widget(viewer, 150.0, 250.0)  # closing edge (v3 -> v0)
        assert _is_red(_pixel(viewer, *edge)) is True
        top = _widget(viewer, 250.0, 150.0)   # a placed edge stays drawn
        assert _is_red(_pixel(viewer, *top)) is True
    finally:
        viewer.close_document()


def test_slot_specific_configuration(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE,
                                               color="#e01b24"))
    try:
        _drag(viewer, (100.0, 100.0), (200.0, 200.0))
        viewer._interaction.set_tool(
            _shape_tool(ToolType.RECTANGLE, color="#1a73e8"))
        _drag(viewer, (250.0, 100.0), (350.0, 200.0))
        shapes = _shapes(viewer)
        assert [s.color for s in shapes] == ["#e01b24", "#1a73e8"]
    finally:
        viewer.close_document()


def test_rendering_reads_the_store(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.RECTANGLE, color="#e01b24", width_mm=2.0,
                         opacity=1.0))
    try:
        a, b = _drag(viewer, (150.0, 150.0), (350.0, 350.0))
        viewer.repaint()
        mid_x = (a[0] + b[0]) / 2.0
        assert _is_red(_pixel(viewer, mid_x, a[1])) is True
        # Clearing the authoritative store must remove the pixels: the
        # viewer keeps no parallel committed-shape collection.
        _store(viewer).clear()
        assert viewer._live_shape is None
        viewer.repaint()
        assert _is_red(_pixel(viewer, mid_x, a[1])) is False
    finally:
        viewer.close_document()


def test_transparent_fill_shows_pdf_below(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.RECTANGLE, color="#e01b24", width_mm=1.0,
                         fill_enabled=True, fill_color="#00ff00",
                         fill_opacity=0.0))
    try:
        a, b = _drag(viewer, (150.0, 150.0), (350.0, 300.0))
        viewer.repaint()
        cx = (a[0] + b[0]) / 2.0
        cy = (a[1] + b[1]) / 2.0
        transparent = _pixel(viewer, cx, cy)
        viewer._on_undo_requested()
        viewer._interaction.set_tool(
            _shape_tool(ToolType.RECTANGLE, color="#e01b24", width_mm=1.0,
                        fill_enabled=False))
        _drag(viewer, (150.0, 150.0), (350.0, 300.0))
        viewer.repaint()
        unfilled = _pixel(viewer, cx, cy)
        assert (transparent.red(), transparent.green(), transparent.blue()) \
            == (unfilled.red(), unfilled.green(), unfilled.blue())
    finally:
        viewer.close_document()


def test_undo_removes_pixels_redo_restores(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.RECTANGLE, color="#e01b24", width_mm=2.0,
                         opacity=1.0))
    try:
        a, b = _drag(viewer, (150.0, 150.0), (350.0, 350.0))
        viewer.repaint()
        mid_x = (a[0] + b[0]) / 2.0
        assert _is_red(_pixel(viewer, mid_x, a[1])) is True
        viewer._on_undo_requested()
        viewer.repaint()
        assert _is_red(_pixel(viewer, mid_x, a[1])) is False
        assert _shapes(viewer) == []
        viewer._on_redo_requested()
        viewer.repaint()
        assert _is_red(_pixel(viewer, mid_x, a[1])) is True
    finally:
        viewer.close_document()


def test_ordering_is_deterministic(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        _drag(viewer, (100.0, 100.0), (150.0, 150.0))
        _drag(viewer, (200.0, 100.0), (250.0, 150.0))
        _drag(viewer, (300.0, 100.0), (350.0, 150.0))
        ids = [a.id.value for a in _store(viewer).page(0)]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        # Store order is the insertion order (deterministic layering).
        assert ids == [a.id.value for a in _shapes(viewer)]
    finally:
        viewer.close_document()


def test_multi_page_keeps_shape_on_its_page(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path, tool=_shape_tool(ToolType.RECTANGLE),
        sizes=[(600.0, 200.0), (600.0, 200.0)])
    try:
        assert viewer._document.page_count == 2
        _drag(viewer, (100.0, 100.0), (200.0, 150.0), page_index=0)
        _drag(viewer, (100.0, 100.0), (200.0, 150.0), page_index=1)
        assert len(_shapes(viewer, 0)) == 1
        assert len(_shapes(viewer, 1)) == 1
        assert _shapes(viewer, 0)[0].page_id == _page_id(viewer, 0)
        assert _shapes(viewer, 1)[0].page_id == _page_id(viewer, 1)
    finally:
        viewer.close_document()


def test_zoom_preserves_page_local_geometry(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        before = _shapes(viewer)[0]
        rect_before = (before.rect.x0, before.rect.y0,
                       before.rect.width, before.rect.height)
        viewer.zoom_in()
        after = _shapes(viewer)[0]
        rect_after = (after.rect.x0, after.rect.y0,
                      after.rect.width, after.rect.height)
        assert rect_before == rect_after
        assert after is before
    finally:
        viewer.close_document()


# --------------------------------------------------- Part E: undo / history

def test_one_gesture_one_history_command(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        history = viewer._history_for(viewer._document, create=True)
        assert history.stack.count() == 0
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        assert history.stack.count() == 1
    finally:
        viewer.close_document()


def test_undo_redo_restores_identity(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        original = _shapes(viewer)[0]
        viewer._on_undo_requested()
        assert _shapes(viewer) == []
        viewer._on_redo_requested()
        restored = _shapes(viewer)[0]
        assert restored.id == original.id
        assert restored.rect == original.rect
        assert restored.color == original.color
    finally:
        viewer.close_document()


def test_repeated_undo_redo_cycles_no_duplicates(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        for _ in range(5):
            viewer._on_undo_requested()
            viewer._on_redo_requested()
        assert len(_shapes(viewer)) == 1
    finally:
        viewer.close_document()


def test_polygon_commit_undo_redo(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.POLYGON))
    try:
        _click(viewer, (100.0, 100.0))
        _click(viewer, (300.0, 100.0))
        _click(viewer, (300.0, 250.0))
        _click(viewer, (103.0, 103.0))
        assert len(_shapes(viewer)) == 1
        viewer._on_undo_requested()
        assert _shapes(viewer) == []
        viewer._on_redo_requested()
        assert len(_shapes(viewer)) == 1
        assert len(_shapes(viewer)[0].points) == 3
    finally:
        viewer.close_document()


def test_mixed_tool_history_is_coherent(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    try:
        pid = _page_id(viewer)
        # 1) Pen stroke.
        _drive_ink(viewer, pid, [(100.0, 300.0), (200.0, 300.0),
                                 (300.0, 300.0)])
        # 2) Highlighter stroke.
        viewer._interaction.set_tool(_hl_tool())
        _drive_ink(viewer, pid, [(100.0, 400.0), (300.0, 400.0)],
                   highlighter=True)
        # 3) Rectangle.
        viewer._interaction.set_tool(_shape_tool(ToolType.RECTANGLE))
        _drag(viewer, (100.0, 100.0), (300.0, 200.0))
        # 4) Erase the pen stroke.
        viewer._interaction.set_tool(_eraser_tool())
        _erase_line(viewer, pid, 200.0, 250.0, 200.0, 350.0)

        from annotations.models import Highlighter, InkStroke

        def kinds():
            return {type(a).__name__ for a in _store(viewer).page(0)}

        assert kinds() == {"Highlighter", "Shape"}
        history = viewer._history_for(viewer._document, create=False)
        # Undo reverses the eraser first: the pen stroke returns.
        history.undo()
        assert kinds() == {"InkStroke", "Highlighter", "Shape"}
        # Then the rectangle.
        history.undo()
        assert kinds() == {"InkStroke", "Highlighter"}
        # Then the highlighter.
        history.undo()
        assert kinds() == {"InkStroke"}
        # Then the pen stroke.
        history.undo()
        assert kinds() == set()
        # Redo restores the same chronological order.
        history.redo()
        assert kinds() == {"InkStroke"}
    finally:
        viewer.close_document()


def test_documents_keep_independent_stores(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE),
                              name="docA.pdf")
    try:
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        assert len(_shapes(viewer)) == 1
        second = make_probe_pdf(str(tmp_path / "docB.pdf"),
                                page_width=600.0, page_height=800.0)
        viewer.open_document(second)
        viewer._interaction.set_tool(_shape_tool(ToolType.RECTANGLE))
        assert _shapes(viewer) == []
        history = viewer._history_for(viewer._document, create=False)
        assert history is None or history.can_undo() is False
        _drag(viewer, (120.0, 120.0), (280.0, 240.0))
        assert len(_shapes(viewer)) == 1
    finally:
        viewer.close_document()


def test_hud_tracks_shape_history(qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        hud = viewer._undo_hud
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        viewer._on_undo_requested()
        assert hud.can_redo() is True
        assert hud._redo_btn.isEnabled() is True
    finally:
        viewer.close_document()


def test_tool_switch_during_polygon_no_leak(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.POLYGON))
    try:
        _click(viewer, (100.0, 100.0))
        _click(viewer, (300.0, 100.0))
        assert viewer._live_shape is not None
        viewer._apply_intent(viewer._interaction.cancel())
        viewer._interaction.set_tool(_shape_tool(ToolType.RECTANGLE))
        assert viewer._live_shape is None
        assert _shapes(viewer) == []
        _drag(viewer, (100.0, 100.0), (300.0, 250.0))
        assert len(_shapes(viewer)) == 1
        assert _shapes(viewer)[0].kind is ShapeKind.RECTANGLE
    finally:
        viewer.close_document()


def test_document_close_discards_unfinished_shape(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        a = _widget(viewer, 100.0, 100.0)
        _press(viewer, *a)
        _move(viewer, a[0] + 80, a[1] + 60)
        viewer.close_document()
        assert viewer._live_shape is None
        assert viewer._interaction.shape_active() is False
    finally:
        viewer.close_document()


def test_eraser_still_works_with_a_shape_present(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    try:
        pid = _page_id(viewer)
        _drive_ink(viewer, pid, [(100.0, 300.0), (300.0, 300.0)])
        viewer._interaction.set_tool(_shape_tool(ToolType.RECTANGLE))
        _drag(viewer, (100.0, 500.0), (300.0, 600.0))
        viewer._interaction.set_tool(_eraser_tool())
        _erase_line(viewer, pid, 200.0, 250.0, 200.0, 350.0)
        remaining = _store(viewer).page(0)
        assert len(remaining) == 1
        assert isinstance(remaining[0], Shape)
    finally:
        viewer.close_document()


def test_shape_kinds_are_registered_in_tool_model():
    assert ToolType.LINE in SHAPE_TOOL_KINDS
    assert ToolType.POLYGON in SHAPE_TOOL_KINDS


# ===================================================== M6.7.1 corrections
#
# Three corrective behaviors on top of the M6.7 shape engine:
#   1. Polygon preview shows the confirmed outline edges plus ONE
#      provisional live segment from the last confirmed vertex to the
#      pointer (M6.7.2 follow-up), but never a premature closing edge; the
#      live fill contour and vertex markers are drawn separately.
#   2. Connect Lines is ONE user-facing setting across every shape popup.
#   3. Both Eraser modes erase committed shapes in full (see
#      test_shape_erase_m672).

# --------------------------------------------- 1. polygon preview edges

def test_polygon_preview_confirmed_outline_plus_provisional_segment():
    c = InteractionController(_shape_tool(ToolType.POLYGON, close_path=True))
    pid = PageId()
    # Confirm A.
    c.handle_shape_press(pid, 0.0, 0.0)
    c.handle_shape_release(pid, 0.0, 0.0)
    # Confirm B -> outline shows only A->B, still open.
    preview = c.handle_shape_press(pid, 100.0, 0.0)["shape_update"]
    c.handle_shape_release(pid, 100.0, 0.0)
    assert [(p.x, p.y) for p in preview.points] == [(0.0, 0.0),
                                                    (100.0, 0.0)]
    assert preview.close_path is False
    # Pointer movement adds ONLY the provisional segment from the last
    # confirmed vertex to the pointer; it never appends a confirmed vertex
    # and never draws the closing edge back to the first vertex.
    moved = c.handle_shape_move(pid, 400.0, 400.0)
    assert [(p.x, p.y) for p in moved["shape_update"].points] == [
        (0.0, 0.0), (100.0, 0.0)]
    assert moved["shape_update"].close_path is False
    geo = moved["shape_preview"]
    assert [(p.x, p.y) for p in geo.vertices] == [(0.0, 0.0),
                                                  (100.0, 0.0)]
    assert [(p.x, p.y) for p in geo.segment] == [(100.0, 0.0),
                                                 (400.0, 400.0)]
    assert [(p.x, p.y) for p in geo.fill] == [
        (0.0, 0.0), (100.0, 0.0), (400.0, 400.0)]
    # Confirm C -> A->B->C, still open (closing edge only on completion).
    preview = c.handle_shape_press(pid, 100.0, 100.0)["shape_update"]
    c.handle_shape_release(pid, 100.0, 100.0)
    assert [(p.x, p.y) for p in preview.points] == [
        (0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    assert preview.close_path is False
    # Closing (double-click) commits exactly the confirmed vertices once.
    done = c.handle_shape_double_click()["shape_done"]
    assert [(p.x, p.y) for p in done.points] == [
        (0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    assert done.close_path is True
    assert c.handle_shape_double_click() == {"noop": True}


def test_polygon_preview_pixels_confirmed_edges_and_provisional(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=2.0,
                         opacity=1.0, close_path=True))
    try:
        _click(viewer, (150.0, 150.0))

        def red(lx, ly):
            return _is_red(_pixel(viewer, *_widget(viewer, lx, ly)))

        _click(viewer, (350.0, 150.0))
        viewer.repaint()
        assert red(250.0, 150.0) is True            # confirmed A->B
        # Move the pointer: the provisional edge B -> pointer is drawn.
        _move(viewer, *_widget(viewer, 150.0, 450.0))
        viewer.repaint()
        assert red(250.0, 300.0) is True            # provisional B -> pointer
        assert red(250.0, 150.0) is True            # A->B still painted

        _click(viewer, (350.0, 350.0))
        viewer.repaint()
        assert red(350.0, 250.0) is True            # confirmed B->C
        # Move again: the provisional edge is C -> pointer only, never a
        # premature closing edge pointer -> A or C -> A.
        _move(viewer, *_widget(viewer, 150.0, 350.0))
        viewer.repaint()
        assert red(250.0, 350.0) is True            # provisional C -> pointer
        assert red(150.0, 250.0) is False           # closing pointer -> A
        assert red(250.0, 250.0) is False           # premature closing C->A

        # Closing adds the final edge exactly once (one commit, one shape).
        _dbl(viewer, *_widget(viewer, 350.0, 350.0))
        viewer.repaint()
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        assert len(shapes[0].points) == 3
        history = viewer._history_for(viewer._document, create=False)
        assert history.stack.count() == 1
        assert red(250.0, 250.0) is True            # closing edge C->A
    finally:
        viewer.close_document()


def test_polygon_preview_movement_leaves_undo_and_geometry_unchanged(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.POLYGON, color="#e01b24", width_mm=2.0,
                         opacity=1.0, close_path=True))
    try:
        _click(viewer, (150.0, 150.0))
        _click(viewer, (350.0, 150.0))
        _click(viewer, (350.0, 350.0))
        before = [(p.x, p.y) for p in viewer._live_shape.points]
        for pt in ((200.0, 400.0), (100.0, 100.0), (400.0, 400.0)):
            _move(viewer, *_widget(viewer, *pt))
        assert [(p.x, p.y) for p in viewer._live_shape.points] == before
        # No history entry appears from preview-only movement.
        history = viewer._history_for(viewer._document, create=False)
        assert history is None or history.stack.count() == 0
        _dbl(viewer, *_widget(viewer, 350.0, 350.0))
        shapes = _shapes(viewer)
        assert len(shapes) == 1
        assert [(p.x, p.y) for p in shapes[0].points] == before
        history = viewer._history_for(viewer._document, create=False)
        assert history.stack.count() == 1
        viewer._on_undo_requested()
        assert _shapes(viewer) == []
        viewer._on_redo_requested()
        redo = _shapes(viewer)
        assert len(redo) == 1
        assert [(p.x, p.y) for p in redo[0].points] == before
        assert redo[0].close_path is True
    finally:
        viewer.close_document()


# -------------------------------------- 2. one canonical Connect Lines

def test_connect_lines_is_the_single_canonical_flag():
    on = ToolInstance(kind=ToolType.LINE, name="L", connect_lines=True)
    assert on.connect_lines is True
    assert on.connect_endpoints is True
    assert on.connect_intersections is True
    # Legacy construction still promotes to the single canonical setting.
    legacy_ep = ToolInstance(kind=ToolType.LINE, name="L",
                             connect_endpoints=True)
    assert (legacy_ep.connect_lines, legacy_ep.connect_intersections) == (
        True, True)
    legacy_ix = ToolInstance(kind=ToolType.LINE, name="L",
                             connect_intersections=True)
    assert (legacy_ix.connect_lines, legacy_ix.connect_endpoints) == (
        True, True)
    off = ToolInstance(kind=ToolType.LINE, name="L")
    assert (off.connect_lines, off.connect_endpoints,
            off.connect_intersections) == (False, False, False)


def test_connect_lines_are_never_contradictory():
    # Any combination collapses to one value across all three mirrors.
    for kw in ({"connect_lines": True}, {"connect_endpoints": True},
               {"connect_intersections": True},
               {"connect_endpoints": True, "connect_intersections": False},
               {"connect_lines": False, "connect_endpoints": True}):
        slot = ToolInstance(kind=ToolType.POLYGON, name="P", **kw)
        assert (slot.connect_lines == slot.connect_endpoints
                == slot.connect_intersections)


def test_connect_lines_legacy_dict_migrates_without_loss():
    from app.toolshelf.store import _slot_from_dict, _slot_to_dict

    legacy = {"kind": "line", "name": "L",
              "connect_endpoints": True, "connect_intersections": False}
    slot = _slot_from_dict(legacy)
    assert slot.connect_lines is True  # enabled legacy setting preserved
    blob = _slot_to_dict(slot)
    assert blob["connect_lines"] is True
    assert blob["connect_endpoints"] is True
    assert blob["connect_intersections"] is True
    # And a fresh round-trip is stable.
    again = _slot_from_dict(blob)
    assert again.connect_lines is True


def test_connect_lines_single_flag_enables_both_snap_behaviors():
    # Endpoints.
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 50.0),), tol_pt=10.0)
    c = InteractionController(_shape_tool(ToolType.LINE, connect_lines=True))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    shape = c.handle_shape_release(pid, 105.0, 52.0)["shape_done"]
    assert shape.points[1].x == pytest.approx(100.0)
    assert shape.points[1].y == pytest.approx(50.0)
    # Intersections.
    seg = (Point(150.0, 50.0), Point(150.0, 150.0))
    ctx = ShapeSnapContext(segments=(seg,), tol_pt=10.0)
    c = InteractionController(_shape_tool(ToolType.LINE, connect_lines=True))
    c.handle_shape_press(pid, 0.0, 100.0, snap=ctx)
    shape = c.handle_shape_release(pid, 160.0, 100.0)["shape_done"]
    assert shape.points[1].x == pytest.approx(150.0)
    assert shape.points[1].y == pytest.approx(100.0)


def test_connect_lines_off_disables_both_snaps():
    ctx = ShapeSnapContext(endpoints=(Point(100.0, 50.0),), tol_pt=10.0)
    c = InteractionController(_shape_tool(ToolType.LINE))  # connect off
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0, snap=ctx)
    shape = c.handle_shape_release(pid, 105.0, 52.0)["shape_done"]
    assert shape.points[1].x == pytest.approx(105.0)
    assert shape.points[1].y == pytest.approx(52.0)


def test_snap_angle_remains_independent_of_connect_lines():
    c = InteractionController(
        _shape_tool(ToolType.LINE, snap_angle=True, connect_lines=False))
    pid = PageId()
    c.handle_shape_press(pid, 0.0, 0.0)
    shape = c.handle_shape_release(pid, 100.0, 10.0)["shape_done"]
    assert shape.points[1].y == pytest.approx(0.0, abs=1e-6)
    assert shape.points[1].x > 0.0


def test_connect_lines_popup_exposes_one_option(qtbot, cleared_settings):
    from app.theme.tokens import dark_tokens
    from app.toolshelf.tool_popup import PropertiesForm

    # M6.7.2: only the tools that expose Connect Lines show the single
    # toggle; Polygon exposes neither Connect Lines nor Close Shape.
    for kind in (ToolType.LINE, ToolType.ARROW):
        slot = ToolInstance(kind=kind, name=kind.value)
        form = PropertiesForm(slot, dark_tokens())
        try:
            assert form._connect is not None, kind
            fields = [t._field for t in form._connect._toggles]
            assert fields == ["connect_lines"], kind
            assert "connect_endpoints" not in fields, kind
            assert "connect_intersections" not in fields, kind
        finally:
            form.close()
    poly = PropertiesForm(
        ToolInstance(kind=ToolType.POLYGON, name="P"), dark_tokens())
    try:
        assert poly._connect is None
    finally:
        poly.close()


def test_connect_lines_popup_toggle_updates_tool_config(
        qtbot, cleared_settings):
    from app.theme.tokens import dark_tokens
    from app.toolshelf.tool_popup import PropertiesForm

    slot = ToolInstance(kind=ToolType.LINE, name="L")
    form = PropertiesForm(slot, dark_tokens())
    try:
        form._connect._toggles[0]._option.click()
        assert form._slot.connect_lines is True
        assert form._slot.connect_endpoints is True
        assert form._slot.connect_intersections is True
    finally:
        form.close()


def test_connect_lines_preset_switch_restores_state():
    from app.toolshelf.store import _slot_from_dict, _slot_to_dict

    slot_a = ToolInstance(kind=ToolType.POLYGON, name="A",
                          connect_lines=True)
    slot_b = ToolInstance(kind=ToolType.POLYGON, name="B")
    restored_a = _slot_from_dict(_slot_to_dict(slot_a))
    restored_b = _slot_from_dict(_slot_to_dict(slot_b))
    assert restored_a.connect_lines is True
    assert restored_b.connect_lines is False
