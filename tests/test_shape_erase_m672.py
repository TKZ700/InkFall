"""M6.7.2 shape erasing: BOTH eraser modes erase a touched shape in full.

Corrective slice on M6.7.1. A shape annotation (Line, Arrow, Rectangle,
Ellipse, Polygon) is completely deleted whenever the eraser's visible
geometry touches it — its rendered outline OR, for a filled shape, its
filled interior. ``Partial Stroke`` remains partial only for Pen and
Highlighter strokes; committed shapes are never clipped/substituted.

Coverage: Qt-free hit geometry (``annotations.shape_hit``) plus the real
viewer event path for both eraser modes across every shape kind, with
store/history assertions (one gesture = one undo step, identity and order
restored on undo, no history on a miss).
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from annotations.models import InkStroke, Shape, ShapeKind
from annotations.shape_hit import (
    fill_contains,
    outline_polylines,
    segment_hits_shape,
    shape_extent,
)
from core.types import Point, Rect, Size
from tests.helpers import make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------- helpers

def _shape_tool(kind, **kw):
    base = dict(kind=kind, name="Shape", color="#e01b24", width_mm=1.5,
                opacity=1.0)
    base.update(kw)
    return ToolInstance(**base)


def _eraser_tool(mode="whole", width_mm=2.0):
    return ToolInstance(kind=ToolType.ERASER, name="Eraser",
                        width_mm=width_mm, eraser_mode=mode)


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


def _open_window(qtbot, tmp_path, tool=None):
    from app.main_window import MainWindow

    pdf = make_probe_pdf(str(tmp_path / "m672.pdf"),
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


def _page_id(viewer, page_index=0):
    return viewer._pipeline.layout.entry(page_index).page_id


def _store(viewer):
    return viewer._document.annotations


def _shapes(viewer, page_index=0):
    return [a for a in _store(viewer).page(page_index)
            if isinstance(a, Shape)]


def _strokes(viewer, page_index=0):
    return [a for a in _store(viewer).page(page_index)
            if isinstance(a, InkStroke)]


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


def _drag(viewer, p0, p1, page_index=0):
    a = _widget(viewer, *p0, page_index)
    b = _widget(viewer, *p1, page_index)
    _press(viewer, *a)
    _move(viewer, *b)
    _release(viewer, *b)
    return a, b


def _build_polygon(viewer, verts, page_index=0):
    for v in verts:
        w = _widget(viewer, *v, page_index)
        _press(viewer, *w)
        _release(viewer, *w)
    w = _widget(viewer, *verts[-1], page_index)
    _dbl(viewer, *w)


def _erase_line(viewer, page_id, x0, y0, x1, y1):
    it = viewer._interaction
    viewer._apply_intent(it.handle_eraser_press(page_id, x0, y0))
    viewer._apply_intent(it.handle_eraser_move(page_id, x1, y1))
    viewer._apply_intent(it.handle_eraser_release(page_id, x1, y1))


def _history(viewer):
    return viewer._history_for(viewer._document, create=False)


def _count(viewer):
    history = _history(viewer)
    return 0 if history is None else history.stack.count()


# ============================================ Qt-free geometry: shape_hit

def _line_shape(x0, y0, x1, y1, kind=ShapeKind.LINE, **kw):
    pts = (Point(x0, y0), Point(x1, y1))
    return Shape(
        kind=kind, points=pts,
        rect=Rect(Point(min(x0, x1), min(y0, y1)),
                  Size(abs(x1 - x0), abs(y1 - y0))),
        width_mm=2.0, **kw)


def _rect_shape(**kw):
    return Shape(kind=ShapeKind.RECTANGLE,
                 rect=Rect(Point(0.0, 0.0), Size(100.0, 100.0)),
                 width_mm=2.0, **kw)


def _ellipse_shape(**kw):
    return Shape(kind=ShapeKind.ELLIPSE,
                 rect=Rect(Point(0.0, 0.0), Size(100.0, 100.0)),
                 width_mm=2.0, **kw)


def _polygon_shape(**kw):
    return Shape(
        kind=ShapeKind.POLYGON,
        points=(Point(0.0, 0.0), Point(100.0, 0.0),
                Point(100.0, 100.0), Point(0.0, 100.0)),
        rect=Rect(Point(0.0, 0.0), Size(100.0, 100.0)),
        width_mm=2.0, close_path=True, **kw)


def test_module_exposes_only_hit_geometry():
    # The partial-erase infrastructure was removed with no shim.
    import annotations

    assert not (__import__("pathlib").Path(annotations.__file__).parent
                / "shape_erase.py").exists()
    for name in ("outline_polylines", "fill_contains",
                 "segment_hits_shape", "shape_extent"):
        assert callable(globals()[name])
    import annotations.shape_hit as hit

    for gone in ("erase_shape", "capsule_polygon", "fill_ring",
                 "erase_stroke"):
        assert not hasattr(hit, gone)


def test_geometry_outline_hit_and_near_miss_per_kind():
    cases = [
        (_line_shape(0.0, 0.0, 100.0, 0.0), (-5.0, 0.0, 5.0, 0.0)),
        (_line_shape(0.0, 0.0, 100.0, 0.0, ShapeKind.ARROW),
         (-5.0, 0.0, 5.0, 0.0)),
        (_rect_shape(), (-5.0, 0.0, 5.0, 0.0)),
        (_ellipse_shape(), (-5.0, 50.0, 5.0, 50.0)),
        (_polygon_shape(), (-5.0, 0.0, 5.0, 0.0)),
    ]
    for shape, (hx0, hy0, hx1, hy1) in cases:
        # Touching a known outline point hits...
        assert segment_hits_shape(shape, hx0, hy0, hx1, hy1,
                                  1.0, 1.0) is True, shape.kind
        # ...a far near-miss preserves the shape.
        assert segment_hits_shape(shape, 500.0, 500.0, 520.0, 500.0,
                                  1.0, 1.0) is False, shape.kind


def test_geometry_filled_interior_hit_but_hollow_interior_missed():
    filled = _rect_shape(fill_enabled=True)
    hollow = _rect_shape()
    # Deep inside the shape, away from the outline.
    assert segment_hits_shape(filled, 50.0, 50.0, 60.0, 50.0,
                              1.0, 1.0) is True
    assert segment_hits_shape(hollow, 50.0, 50.0, 60.0, 50.0,
                              1.0, 1.0) is False


def test_geometry_fill_containment_per_kind():
    assert fill_contains(_rect_shape(fill_enabled=True), 50.0, 50.0) is True
    assert fill_contains(_rect_shape(fill_enabled=True), 150.0, 50.0) is False
    assert fill_contains(_ellipse_shape(fill_enabled=True),
                         50.0, 50.0) is True
    assert fill_contains(_ellipse_shape(fill_enabled=True),
                         99.0, 99.0) is False
    assert fill_contains(_polygon_shape(fill_enabled=True),
                         50.0, 50.0) is True
    # A filled polygon that is not closed contributes no fill region.
    open_poly = Shape(
        kind=ShapeKind.POLYGON,
        points=(Point(0.0, 0.0), Point(100.0, 0.0), Point(100.0, 100.0)),
        fill_enabled=True, close_path=False)
    assert fill_contains(open_poly, 85.0, 15.0) is False
    assert segment_hits_shape(open_poly, 85.0, 15.0, 90.0, 15.0,
                              1.0, 1.0) is False


def test_geometry_outline_polylines_and_extent():
    assert outline_polylines(_line_shape(0.0, 0.0, 100.0, 0.0))
    assert outline_polylines(_rect_shape())[0][0] == \
        outline_polylines(_rect_shape())[0][-1]  # closed ring
    assert shape_extent(_rect_shape()) == (0.0, 0.0, 100.0, 100.0)
    assert shape_extent(_line_shape(5.0, 5.0, 40.0, 20.0)) == \
        (5.0, 5.0, 40.0, 20.0)


# ================================================ Whole + Partial (viewer)

_ALL_KINDS = [
    (ToolType.LINE, {}, (200.0, 300.0, 200.0, 320.0)),
    (ToolType.ARROW, {"end_ending": "arrow"}, (200.0, 300.0, 200.0, 320.0)),
]


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_both_modes_delete_line_family(qtbot, tmp_path, cleared_settings,
                                       mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 300.0), (300.0, 300.0))
        original = _shapes(viewer)[0]
        before = _count(viewer)
        viewer._interaction.set_tool(_eraser_tool(mode))
        _erase_line(viewer, pid, 200.0, 300.0, 200.0, 320.0)
        assert _shapes(viewer) == []
        assert _count(viewer) == before + 1
        viewer._on_undo_requested()
        restored = _shapes(viewer)
        assert len(restored) == 1
        assert restored[0].id == original.id
        assert restored[0].points == original.points
        viewer._on_redo_requested()
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_both_modes_delete_rectangle_ellipse_polygon(
        qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 400.0), (300.0, 600.0))          # rectangle
        viewer._interaction.set_tool(_shape_tool(ToolType.ELLIPSE))
        _drag(viewer, (400.0, 400.0), (600.0, 600.0))          # ellipse
        viewer._interaction.set_tool(
            _shape_tool(ToolType.POLYGON, close_path=True))
        _build_polygon(viewer, [(150.0, 650.0), (350.0, 650.0),
                                (350.0, 780.0), (150.0, 780.0)])
        kinds = sorted(s.kind.value for s in _shapes(viewer))
        assert kinds == ["ellipse", "polygon", "rectangle"]
        viewer._interaction.set_tool(_eraser_tool(mode))
        # Touch each shape's outline in turn.
        _erase_line(viewer, pid, 200.0, 400.0, 200.0, 420.0)   # rect top
        assert [s.kind for s in _shapes(viewer)] == [
            ShapeKind.ELLIPSE, ShapeKind.POLYGON]
        _erase_line(viewer, pid, 400.0, 500.0, 420.0, 500.0)   # ellipse left
        assert [s.kind for s in _shapes(viewer)] == [ShapeKind.POLYGON]
        _erase_line(viewer, pid, 250.0, 650.0, 250.0, 670.0)   # poly top
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_both_modes_delete_by_touching_fill_interior(
        qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.RECTANGLE, fill_enabled=True,
                         fill_color="#00cc44", fill_opacity=1.0))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 400.0), (300.0, 600.0))
        assert len(_shapes(viewer)) == 1
        viewer._interaction.set_tool(_eraser_tool(mode))
        # Eraser entirely inside the filled region (never touching the
        # outline) must still delete the shape in BOTH modes.
        _erase_line(viewer, pid, 200.0, 500.0, 210.0, 500.0)
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_both_modes_preserve_near_miss(qtbot, tmp_path, cleared_settings,
                                       mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 400.0), (300.0, 600.0))
        viewer._interaction.set_tool(_eraser_tool(mode))
        before = _count(viewer)
        _erase_line(viewer, pid, 100.0, 340.0, 100.0, 360.0)  # ~40pt above
        assert len(_shapes(viewer)) == 1
        assert _count(viewer) == before
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_both_modes_multiple_shapes_one_gesture_one_undo(
        qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 300.0), (300.0, 300.0))
        _drag(viewer, (100.0, 450.0), (300.0, 450.0))
        originals = [s.id for s in _shapes(viewer)]
        assert len(originals) == 2
        viewer._interaction.set_tool(_eraser_tool(mode))
        before = _count(viewer)
        _erase_line(viewer, pid, 200.0, 250.0, 200.0, 500.0)
        assert _shapes(viewer) == []
        assert _count(viewer) == before + 1
        viewer._on_undo_requested()
        assert [s.id for s in _shapes(viewer)] == originals
    finally:
        viewer.close_document()


def test_undo_restores_identity_geometry_style_and_order(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(
        qtbot, tmp_path,
        tool=_shape_tool(ToolType.RECTANGLE, color="#e01b24",
                         width_mm=2.5, opacity=0.8, fill_enabled=True,
                         fill_color="#00cc44", fill_opacity=1.0))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 300.0), (250.0, 450.0))
        viewer._interaction.set_tool(
            _shape_tool(ToolType.ELLIPSE, color="#2b6cff", width_mm=3.5))
        _drag(viewer, (300.0, 300.0), (450.0, 450.0))
        originals = _shapes(viewer)
        assert len(originals) == 2
        viewer._interaction.set_tool(_eraser_tool("whole"))
        # One horizontal gesture crosses both shapes.
        _erase_line(viewer, pid, 50.0, 375.0, 550.0, 375.0)
        assert _shapes(viewer) == []
        viewer._on_undo_requested()
        restored = _shapes(viewer)
        assert [s.id for s in restored] == [s.id for s in originals]
        assert [s.kind for s in restored] == [s.kind for s in originals]
        assert restored[0].color == "#e01b24"
        assert restored[0].width_mm == pytest.approx(2.5)
        assert restored[0].opacity == pytest.approx(0.8)
        assert restored[0].fill_enabled is True
        assert restored[0].fill_color == "#00cc44"
        assert restored[1].color == "#2b6cff"
        assert restored[1].width_mm == pytest.approx(3.5)
        assert restored[1].rect == originals[1].rect
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_redo_is_reproducible(qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 300.0), (300.0, 300.0))
        original_id = _shapes(viewer)[0].id
        viewer._interaction.set_tool(_eraser_tool(mode))
        _erase_line(viewer, pid, 200.0, 300.0, 200.0, 320.0)
        for _ in range(3):
            viewer._on_undo_requested()
            assert [s.id for s in _shapes(viewer)] == [original_id]
            viewer._on_redo_requested()
            assert _shapes(viewer) == []
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_no_hit_creates_no_history(qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 400.0), (300.0, 600.0))
        original = _shapes(viewer)[0]
        viewer._interaction.set_tool(_eraser_tool(mode))
        before = _count(viewer)
        _erase_line(viewer, pid, 100.0, 340.0, 100.0, 360.0)  # near miss
        assert len(_shapes(viewer)) == 1
        assert _shapes(viewer)[0].id == original.id
        assert _count(viewer) == before
    finally:
        viewer.close_document()


@pytest.mark.parametrize("mode", ["whole", "partial"])
def test_hud_synced_after_gesture(qtbot, tmp_path, cleared_settings, mode):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 300.0), (300.0, 300.0))
        viewer._interaction.set_tool(_eraser_tool(mode))
        _erase_line(viewer, pid, 200.0, 300.0, 200.0, 320.0)
        history = _history(viewer)
        assert history.stack.canUndo() is True
        assert history.stack.canRedo() is False
    finally:
        viewer.close_document()


def test_partial_mode_still_partial_for_pen_not_shapes(
        qtbot, tmp_path, cleared_settings):
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.LINE,
                                               color="#e01b24", width_mm=2.0))
    try:
        pid = _page_id(viewer)
        # A committed shape and a pen stroke, both crossed by one gesture.
        _drag(viewer, (100.0, 300.0), (300.0, 300.0))
        viewer._interaction.set_tool(ToolInstance(
            kind=ToolType.PEN, name="Pen", color="#ffffff", width_mm=2.0))
        viewer._apply_intent(viewer._interaction.handle_pen_press(
            pid, 100.0, 450.0, 1.0, 1000))
        viewer._apply_intent(viewer._interaction.handle_pen_move(
            pid, 300.0, 450.0, 1.0, 1016))
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            pid, 300.0, 450.0, 1.0, 1032))
        assert len(_shapes(viewer)) == 1
        assert len(_strokes(viewer)) == 1
        viewer._interaction.set_tool(_eraser_tool("partial"))
        _erase_line(viewer, pid, 200.0, 250.0, 200.0, 500.0)
        # The shape is gone; the Pen stroke survives (partially erased
        # into fragments) — Partial Stroke stays partial only for strokes.
        assert _shapes(viewer) == []
        assert len(_strokes(viewer)) >= 1
    finally:
        viewer.close_document()


def test_eraser_radius_respected_for_shape_hit(qtbot, tmp_path,
                                               cleared_settings):
    # A wider eraser reaches further: same near-miss geometry now hits.
    _w, viewer = _open_window(qtbot, tmp_path,
                              tool=_shape_tool(ToolType.RECTANGLE,
                                               width_mm=2.0))
    try:
        pid = _page_id(viewer)
        _drag(viewer, (100.0, 400.0), (300.0, 600.0))
        viewer._interaction.set_tool(_eraser_tool("whole", width_mm=40.0))
        _erase_line(viewer, pid, 200.0, 370.0, 200.0, 380.0)
        assert _shapes(viewer) == []
    finally:
        viewer.close_document()
