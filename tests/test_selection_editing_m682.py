"""M6.8.2 Stage 3: group move through Selection and Lasso.

A press on a *selected* annotation (either tool) arms a group move:
dragging it moves the whole selected group — one transient preview
painted from the existing paths (store untouched), then one grouped
undo entry per completed move. Coverage:

* group movement: single/multiple/mixed (ink + highlighter + shape)
  selections, page assignment and ids kept, relative geometry and
  unrelated fields preserved, sub-threshold release keeps normal
  click/selection semantics with no history entry;
* cross-page transfer (corrective extension): a member whose moved
  center crosses onto another displayed page is reassigned to that
  page — it stays visible during and after the drag, becomes part of
  the destination page's store (removed from the source), keeps its
  identity/properties/z-order, remains hit-testable and selectable and
  movable again, works upward/downward/through boundaries/gutters and
  for mixed multi-member groups spanning three or more pages, with one
  grouped undo entry restoring the exact source pages, positions,
  objects, and paint order;
* cross-page/rotated movement: unequal page sizes, one shared
  widget-space delta mapping to the identical page-pt delta on every
  page (offsets cancel for translation), exact undo/redo restoration;
* preview contract: no store writes while dragging, the shared frame
  follows the preview, painted pixels move (and keep clipping to the
  page), preview disappears cleanly on commit or cancel;
* cancellation: Escape, tool switch, hide, document close, undo/redo;
* stale state during preflight: partial staleness proceeds with
  survivors and cleans them only after commit, an all-stale selection
  is a true no-op (ids retained), an intervening replacement of a
  still-existing selected annotation aborts the whole operation
  without overwriting it, and an aborted/cancelled move leaves
  history untouched;
* routing regressions: a press on a selected body never opens a
  marquee/lasso, a press on an unselected object keeps M6.8.1
  behavior, one stylus gesture moves the group exactly once (no
  duplicate through synthesized mouse events), and Pan never moves
  annotations;
* scroll stability (corrective): scrolling the viewport during an
  active body/group move (wheel, touchpad pixel deltas, scrollbar, or
  several viewport lengths) keeps the preview pinned to the pointer,
  recalculates the destination page against the *current* viewport, and
  commits to the correct page/position, with cancellation and
  stale/id-conflict safety unchanged.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import (
    QHideEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QTabletEvent,
    QWheelEvent,
)

from annotations.hit_test import annotation_extent, pick_annotation
from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from app import viewer as viewer_module
from core.types import Point, Rect, Size
from tests.helpers import make_intrinsic_pdf, make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_MOVE = viewer_module._MARQUEE_MIN_PX  # the established drag threshold


# --------------------------------------------------------------- fixtures

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


def _selection_tool():
    return ToolInstance(kind=ToolType.SELECTION, name="Select")


def _lasso_tool():
    return ToolInstance(kind=ToolType.LASSO, name="Lasso")


def _pan_tool():
    return ToolInstance(kind=ToolType.PAN, name="Pan")


def _pen_tool():
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#ff0000",
                        width_mm=1.0, opacity=0.9)


def _pen_stroke(p0=(100.0, 200.0), p1=(400.0, 200.0), width_mm=1.0,
                color="#e01b24", **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color=color,
                width_mm=width_mm, opacity=1.0)
    base.update(kw)
    return InkStroke(**base)


def _rect_shape(x0, y0, x1, y1, **kw):
    base = dict(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(x0, y0), Size(x1 - x0, y1 - y0)),
                color="#000000", width_mm=1.0)
    base.update(kw)
    return Shape(**base)


def _box_shape(**kw):
    base = dict(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(100.0, 260.0), Size(120.0, 60.0)),
                color="#101bc4", width_mm=2.0)
    base.update(kw)
    return Shape(**base)


def _highlighter(x=90.0, y=100.0, **kw):
    base = dict(points=(Point(x, y), Point(x + 260.0, y)),
                color="#f0e13d", width_mm=8.0, opacity=0.4)
    base.update(kw)
    return Highlighter(**base)


# ------------------------------------------------------- viewer helpers

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


@contextlib.contextmanager
def _chrome_hidden(viewer):
    """Temporarily hide floating chrome for a synthetic canvas event."""
    hidden = []
    for name in ("_shelf", "_undo_hud", "_tool_popup"):
        widget = getattr(viewer, name, None)
        if widget is not None:
            hidden.append(widget)
            widget.hide()
    yield
    for widget in hidden:
        widget.show()


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


def _tablet(viewer, etype, x, y, pressure=0.6):
    event = QTabletEvent(
        etype, _stylus(), QPointF(x, y), QPointF(x, y), float(pressure),
        0.0, 0.0, 0.0, 0.0, 0.0, Qt.KeyboardModifier.NoModifier,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    viewer.tabletEvent(event)
    return event


def _open_window(qtbot, tmp_path, tool=None, name="m682.pdf", sizes=None,
                 pdf=None):
    from app.main_window import MainWindow

    if pdf is None:
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
    """Commit an annotation straight into the store on the right page."""
    stored = dataclasses.replace(annotation,
                                 page_id=_page_id(viewer, page_index))
    _store(viewer).add(stored, page_index)
    return stored


def _push_add(viewer, annotation, page_index=0):
    """Commit an annotation through the undo history (can be undone)."""
    stored = dataclasses.replace(annotation,
                                 page_id=_page_id(viewer, page_index))
    viewer.document_history.push_add(stored, page_index)
    return stored


def _widget(viewer, lx, ly, page_index=0):
    """Invert ``_region_page_pt`` for one page (test mapping helper)."""
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


def _factor(viewer):
    """The global projection factor ``B`` (page pt -> widget px)."""
    return (float(viewer._pipeline.layout.raster_scale)
            * viewer._display_scale())


def _selected_values(viewer):
    return tuple(annotation_id.value
                 for annotation_id in viewer._selection.selected_ids)


def _history_entries(viewer):
    return viewer.document_history.stack.count()


def _marquee(viewer, page_anchor, page_end, page_index=0, steps=4):
    """Press -> drag -> release a rectangular marquee (page-local pts)."""
    ax0, ay0 = _widget(viewer, page_anchor[0], page_anchor[1], page_index)
    ax1, ay1 = _widget(viewer, page_end[0], page_end[1], page_index)
    with _chrome_hidden(viewer):
        _press(viewer, ax0, ay0)
        for i in range(1, steps + 1):
            frac = i / steps
            _move(viewer, ax0 + (ax1 - ax0) * frac,
                  ay0 + (ay1 - ay0) * frac)
        _release(viewer, ax1, ay1)


def _lasso_widget(viewer, widget_points):
    """Press -> drag -> release a freehand loop over widget points."""
    pts = [(float(p[0]), float(p[1])) for p in widget_points]
    with _chrome_hidden(viewer):
        _press(viewer, *pts[0])
        for i in range(1, len(pts)):
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            _move(viewer, x0 + (x1 - x0) * 0.5, y0 + (y1 - y0) * 0.5)
            _move(viewer, x1, y1)
        _release(viewer, *pts[-1])


def _body_drag(viewer, widget_anchor, dx, dy, steps=5):
    """Press on a widget point, drag by (dx, dy), release."""
    x0, y0 = widget_anchor
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        for i in range(1, steps + 1):
            frac = i / steps
            _move(viewer, x0 + dx * frac, y0 + dy * frac)
        _release(viewer, x0 + dx, y0 + dy)


def _render_viewer(viewer):
    """Render the whole widget to an image (chrome already hidden)."""
    image = QImage(int(viewer.width()), int(viewer.height()),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0xFF000000)
    viewer.render(image)
    return image


def _ink_present(image, wx, wy, radius=9):
    """True when red pen ink (#e01b24) is near a widget point."""
    x0, x1 = int(wx) - radius, int(wx) + radius
    y0, y1 = int(wy) - radius, int(wy) + radius
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if 0 <= x < image.width() and 0 <= y < image.height():
                color = image.pixelColor(x, y)
                if color.red() >= 140 and color.green() <= 70 \
                        and color.blue() <= 70:
                    return True
    return False


# ================================================================ group move

def test_selection_body_drag_moves_whole_group_one_undo_entry(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    c = _seed(viewer, _pen_stroke((100.0, 345.0), (400.0, 345.0)))
    baseline = _store(viewer).page(0)
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    assert _history_entries(viewer) == 0

    # Press on one SELECTED member's body: the move gesture arms
    # instead of a marquee.
    anchor = _widget(viewer, 250.0, 100.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
    assert viewer._region_gesture is None
    assert viewer._move_gesture is not None

    _body_drag(viewer, anchor, 40.0, 0.0)
    factor = _factor(viewer)
    pdx = 40.0 / factor
    stored = _store(viewer).page(0)
    assert tuple(item.id.value for item in stored) == (
        a.id.value, b.id.value, c.id.value)  # ids + z-order kept
    moved_a = _store(viewer).get(a.id)
    moved_b = _store(viewer).get(b.id)
    assert (moved_a.points[0].x, moved_a.points[1].x) == pytest.approx(
        (100.0 + pdx, 400.0 + pdx))
    assert (moved_b.points[0].y, moved_b.points[1].y) == pytest.approx(
        (300.0, 300.0))
    unchanged_c = _store(viewer).get(c.id)
    assert unchanged_c is c  # unselected body is untouched
    assert moved_a.page_id.value == _page_id(viewer).value
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}  # ids kept
    assert viewer._transform_preview is None    # preview gone
    assert viewer._move_gesture is None
    assert _history_entries(viewer) == 1         # ONE grouped entry

    history = viewer.document_history
    history.undo()
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a
    history.redo()
    restored = _store(viewer).get(a.id)
    assert restored.points[0].x == pytest.approx(100.0 + pdx)


def test_single_selected_annotation_moves_and_stays_identical(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _marquee(viewer, (60.0, 100.0), (440.0, 200.0))
    assert _selected_values(viewer) == (stroke.id.value,)
    anchor = _widget(viewer, 260.0, 150.0)
    _body_drag(viewer, anchor, 0.0, -60.0)
    pdy = -60.0 / _factor(viewer)
    moved = _store(viewer).get(stroke.id)
    assert (moved.points[0].y, moved.points[1].y) == pytest.approx(
        (150.0 + pdy, 150.0 + pdy))
    viewer.document_history.undo()
    back = _store(viewer).get(stroke.id)
    assert (back.points[0].y, back.points[1].y) == pytest.approx(
        (150.0, 150.0))


def test_mixed_group_move_preserves_types_and_unrelated_fields(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _seed(viewer, _pen_stroke((90.0, 60.0), (380.0, 60.0),
                                       width_mm=1.4, opacity=0.9,
                                       pressures=(0.3, 0.9),
                                       times_ms=(1100, 1250)))
    hl = _seed(viewer, _highlighter(x=90.0, y=140.0, width_mm=8.0,
                                    opacity=0.4))
    shape = _seed(viewer, _box_shape(width_mm=2.0))
    _marquee(viewer, (60.0, 30.0), (440.0, 340.0))
    assert set(_selected_values(viewer)) == {
        stroke.id.value, hl.id.value, shape.id.value}
    anchor = _widget(viewer, 160.0, 260.0)   # on the shape's outline band
    _body_drag(viewer, anchor, 50.0, -30.0)
    pdx = 50.0 / _factor(viewer)
    pdy = -30.0 / _factor(viewer)
    moved_stroke = _store(viewer).get(stroke.id)
    assert isinstance(moved_stroke, InkStroke)
    assert moved_stroke.color == stroke.color
    assert moved_stroke.opacity == stroke.opacity
    assert moved_stroke.width_mm == stroke.width_mm      # move never rescales
    assert moved_stroke.pressures == stroke.pressures
    assert moved_stroke.times_ms == stroke.times_ms
    assert (moved_stroke.points[0].x, moved_stroke.points[1].x) == (
        pytest.approx(90.0 + pdx), pytest.approx(380.0 + pdx))
    moved_hl = _store(viewer).get(hl.id)
    assert isinstance(moved_hl, Highlighter)
    assert (moved_hl.color, moved_hl.opacity) == (hl.color, hl.opacity)
    assert moved_hl.points[0].x == pytest.approx(90.0 + pdx)
    moved_shape = _store(viewer).get(shape.id)
    assert isinstance(moved_shape, Shape)
    assert moved_shape.kind == shape.kind
    assert moved_shape.rect.origin.x == pytest.approx(100.0 + pdx)
    assert moved_shape.rect.origin.y == pytest.approx(260.0 + pdy)
    assert moved_shape.rect.height == shape.rect.height
    assert moved_shape.color == shape.color
    assert _history_entries(viewer) == 1


def test_cross_page_marquee_group_move_same_widget_delta(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="cross.pdf",
        sizes=[(600.0, 120.0), (600.0, 120.0)])
    a = _seed(viewer, _pen_stroke((100.0, 40.0), (400.0, 40.0)), 0)
    b = _seed(viewer, _pen_stroke((100.0, 80.0), (400.0, 80.0)), 1)
    # One marquee spanning both pages selects the cross-page group.
    x0, y0 = _widget(viewer, 60.0, 10.0, 0)
    x1, y1 = _widget(viewer, 440.0, 110.0, 1)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, (x0 + x1) / 2.0, (y0 + y1) / 2.0)
        _release(viewer, x1, y1)
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}

    anchor = _widget(viewer, 250.0, 40.0, 0)
    _body_drag(viewer, anchor, -30.0, 0.0)
    pdx = -30.0 / _factor(viewer)
    got_a = _store(viewer).get(a.id)
    got_b = _store(viewer).get(b.id)
    # The SAME shared-space delta lands on every page: offsets cancel.
    assert (got_a.points[0].x, got_a.points[1].x) == pytest.approx(
        (100.0 + pdx, 400.0 + pdx))
    assert (got_b.points[0].x, got_b.points[1].x) == pytest.approx(
        (100.0 + pdx, 400.0 + pdx))
    assert got_a.page_id.value == _page_id(viewer, 0).value
    assert got_b.page_id.value == _page_id(viewer, 1).value
    # Relative positions preserved: the per-page y members are untouched.
    assert got_a.points[0].y == pytest.approx(40.0)
    assert got_b.points[0].y == pytest.approx(80.0)
    # One undo restores the exact original sequence on both pages.
    viewer.document_history.undo()
    assert _store(viewer).page(0)[0] is a
    assert _store(viewer).page(1)[0] is b
    assert _store(viewer).page(0)[0].points[0].x == pytest.approx(100.0)
    assert _store(viewer).page(1)[0].points[0].x == pytest.approx(100.0)
    viewer.document_history.redo()
    assert _store(viewer).page(0)[0] is not a
    assert _store(viewer).page(1)[0] is not b


def test_lasso_body_drag_moves_group_same_behavior(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    c = _seed(viewer, _pen_stroke((100.0, 345.0), (400.0, 345.0)))
    x0, y0 = _widget(viewer, 60.0, 40.0)
    x1, y1 = _widget(viewer, 440.0, 330.0)
    _lasso_widget(viewer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    anchor = _widget(viewer, 250.0, 300.0)
    _body_drag(viewer, anchor, 25.0, 35.0)
    pdx, pdy = 25.0 / _factor(viewer), 35.0 / _factor(viewer)
    got_b = _store(viewer).get(b.id)
    assert (got_b.points[0].x, got_b.points[0].y) == pytest.approx(
        (100.0 + pdx, 300.0 + pdy))
    assert _store(viewer).get(c.id) is c
    assert _history_entries(viewer) == 1
    viewer.document_history.undo()
    assert _store(viewer).get(
        b.id).points[0].x == pytest.approx(100.0)


def test_rotated_page_group_move(qtbot, tmp_path, cleared_settings):
    pdf_path = make_intrinsic_pdf(str(tmp_path / "rot.pdf"),
                                  rotations=(90,))
    _window, viewer = _open_window(qtbot, tmp_path, pdf=pdf_path,
                                   name="rot.pdf")
    stroke = _seed(viewer, _pen_stroke((250.0, 300.0), (450.0, 300.0),
                                       width_mm=0.8))
    _marquee(viewer, (200.0, 250.0), (500.0, 350.0))
    assert _selected_values(viewer) == (stroke.id.value,)
    anchor = _widget(viewer, 350.0, 300.0)
    _body_drag(viewer, anchor, 30.0, 20.0)
    pdx, pdy = 30.0 / _factor(viewer), 20.0 / _factor(viewer)
    moved = _store(viewer).get(stroke.id)
    assert (moved.points[0].x, moved.points[0].y) == pytest.approx(
        (250.0 + pdx, 300.0 + pdy))
    assert moved.page_id.value == _page_id(viewer).value
    viewer.document_history.undo()
    back = _store(viewer).get(stroke.id)
    assert (back.points[0].x, back.points[0].y) == pytest.approx(
        (250.0, 300.0))


# ================================================== preview & commit contract

def test_preview_avoids_store_writes_and_frame_follows(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    b = _seed(viewer, _pen_stroke((100.0, 330.0), (400.0, 330.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 360.0))
    baseline_pages = _store(viewer).page(0)
    frame_before = viewer._selection_frame_screen_box()
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 80.0, anchor[1] + 0.0)
    # Zero store writes while dragging (identical objects, same order).
    assert _store(viewer).page(0) == baseline_pages
    # The preview carries the moved candidates under the same ids.
    preview = viewer._transform_preview
    assert set(preview) == {a.id, b.id}
    factor = _factor(viewer)
    assert preview[a.id].points[0].x == pytest.approx(100.0 + 80.0 / factor)
    assert preview[b.id].points[1].x == pytest.approx(400.0 + 80.0 / factor)
    # The shared frame follows the preview geometry.
    frame_after = viewer._selection_frame_screen_box()
    assert frame_after is not None and frame_before is not None
    assert frame_after.x() == pytest.approx(frame_before.x() + 80.0, abs=3.0)
    assert frame_before.size() == frame_after.size()
    # Escape-like cancellation is tested elsewhere; finish by pressing
    # nothing more and letting the release commit (checked in the
    # dedicated commit tests).
    with _chrome_hidden(viewer):
        _release(viewer, anchor[0] + 80.0, anchor[1])
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None


def test_move_commits_single_grouped_entry_and_restores_exactly(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    baseline = _store(viewer).page(0)
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    anchor = _widget(viewer, 250.0, 300.0)
    _body_drag(viewer, anchor, 44.0, -22.0)
    moved_page = _store(viewer).page(0)
    assert tuple(item.id.value for item in moved_page) == (
        a.id.value, b.id.value)
    viewer.document_history.undo()
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(b.id) is b
    # Post-undo revalidation keeps the still-valid ids.
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    viewer.document_history.redo()
    after = _store(viewer).page(0)
    pdx, pdy = 44.0 / _factor(viewer), -22.0 / _factor(viewer)
    assert after[0].points[0].x == pytest.approx(100.0 + pdx)
    assert after[1].points[0].y == pytest.approx(300.0 + pdy)
    # Post-commit revalidation kept the selected ids.
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}


def test_subthreshold_release_keeps_click_semantics_no_history(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    baseline = _store(viewer).page(0)
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    entries = _history_entries(viewer)
    anchor = _widget(viewer, 250.0, 100.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
    assert viewer._move_gesture is not None
    with _chrome_hidden(viewer):
        _move(viewer, anchor[0] + 2.0, anchor[1])   # under the threshold
        _release(viewer, anchor[0] + 2.0, anchor[1])
    # Body is unchanged and no history entry appeared.
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    # Selection click semantics retained: the press hit becomes the
    # selection (M6.8.1's choose-one click on a selected body).
    assert _selected_values(viewer) == (a.id.value,)


def test_lasso_subthreshold_click_on_selected_body_retains_selection(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    x0, y0 = _widget(viewer, 60.0, 40.0)
    x1, y1 = _widget(viewer, 440.0, 330.0)
    _lasso_widget(viewer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    entries = _history_entries(viewer)
    baseline = _store(viewer).page(0)
    anchor = _widget(viewer, 250.0, 100.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)             # arms the move gesture
        _move(viewer, anchor[0], anchor[1] + 2.0)
        _release(viewer, anchor[0], anchor[1] + 2.0)
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries
    # A click on a group member must NOT dissolve the selection.
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}


def test_preview_pixels_move_and_stay_clipped_to_page(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path,
                                   name="clip.pdf",
                                   sizes=[(600.0, 400.0)])
    stroke = _seed(viewer, _pen_stroke((470.0, 180.0), (700.0, 180.0),
                                       width_mm=1.0))
    _marquee(viewer, (430.0, 140.0), (590.0, 220.0))
    anchor = _widget(viewer, 520.0, 180.0)
    with _chrome_hidden(viewer):
        image_before = _render_viewer(viewer)
    assert _ink_present(image_before, anchor[0], anchor[1])
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 90.0, anchor[1])
        image_preview = _render_viewer(viewer)
    assert viewer._transform_preview is not None
    factor = _factor(viewer)
    # The preview moved with the pointer: probe a visible page-local
    # point of the candidate span (the dragged stroke extends past the
    # page edge, so only its left part stays on the page).
    pdx_probe = 90.0 / factor
    probe = _widget(viewer, 520.0 + pdx_probe - 35.0, 180.0)
    assert _ink_present(image_preview, probe[0], probe[1])
    # ...and the old position's ink is gone.
    assert not _ink_present(image_preview, anchor[0], anchor[1])
    # Clipping: the candidate extends past the page edge; every rendered
    # ink pixel stays inside the page's displayed bounds.
    entry = viewer._pipeline.layout.entry(0)
    page_rect = viewer._region_page_screen(entry)
    edge_x = page_rect.x + page_rect.width
    page_y0, page_y1 = page_rect.y, page_rect.y + page_rect.height
    for y in range(int(page_y0), int(page_y1), 4):
        color = image_preview.pixelColor(int(edge_x) + 6, y)
        assert not (color.red() >= 140 and color.green() <= 70
                    and color.blue() <= 70)
    assert _store(viewer).page(0)[0] is stroke  # still untouched
    with _chrome_hidden(viewer):
        _release(viewer, anchor[0] + 90.0, anchor[1])
    image_after = _render_viewer(viewer)
    assert _ink_present(image_after, probe[0], probe[1])


# ==================================================== cancellation & stale

def test_escape_cancels_active_move_preview(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 70.0, anchor[1])
    assert viewer._transform_preview is not None
    viewer.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier))
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == entries
    assert _selected_values(viewer) == (a.id.value,)


def test_tool_switch_cancels_active_move(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 70.0, anchor[1])
    assert viewer._transform_preview is not None
    viewer._on_tool_selected(_pen_tool())
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries    # cancelled, not committed
    # The chosen M6.8.1 policy: leaving Selection clears the selection.
    assert viewer._selection.has_selection is False


def test_hide_event_cancels_active_move(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    baseline = _store(viewer).page(0)
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 70.0, anchor[1])
    assert viewer._transform_preview is not None
    viewer.hideEvent(QHideEvent())
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a


def test_close_document_cleans_move_state(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 70.0, anchor[1])
    viewer.close_document()
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert viewer._selection.has_selection is False


def test_undo_request_cancels_preview_before_history(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _push_add(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    anchor = _widget(viewer, 250.0, 120.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 70.0, anchor[1])
    assert viewer._transform_preview is not None
    entries_before = _history_entries(viewer)
    viewer._on_undo_requested()   # removes the pushed add — exactly once
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert len(_store(viewer)) == 0
    assert _history_entries(viewer) == entries_before  # preview never pushed
    assert viewer.document_history.can_redo() is True
    assert _store(viewer).get(stroke.id) is None


# ======================================================= stale-state preflight

def test_partial_stale_moves_survivors_and_cleans_after_commit(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    anchor = _widget(viewer, 250.0, 300.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)          # gesture armed, snapshots taken
    # One member disappears mid-gesture (e.g. an eraser elsewhere).
    assert _store(viewer).remove(b.id) is True
    with _chrome_hidden(viewer):
        _move(viewer, anchor[0] + 60.0, anchor[1])
        _release(viewer, anchor[0] + 60.0, anchor[1])
    pdx = 60.0 / _factor(viewer)
    moved_a = _store(viewer).get(a.id)
    assert moved_a.points[0].x == pytest.approx(100.0 + pdx)
    assert _store(viewer).get(b.id) is None          # stayed gone
    # Survivor moves; the vanished id is cleaned only after commit.
    assert _selected_values(viewer) == (a.id.value,)
    reader = viewer.document_history
    assert _history_entries(viewer) == 1             # one grouped survivors op
    reader.undo()
    assert _store(viewer).get(a.id).points[0].x == pytest.approx(100.0)


def test_all_stale_true_noop_and_selection_retained(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    ids = {a.id.value, b.id.value}
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    anchor = _widget(viewer, 250.0, 100.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
    assert _store(viewer).remove(a.id) is True
    assert _store(viewer).remove(b.id) is True
    entries = _history_entries(viewer)
    with _chrome_hidden(viewer):
        _move(viewer, anchor[0] + 60.0, anchor[1])
        _release(viewer, anchor[0] + 60.0, anchor[1])
    # Every member vanished: nothing was pushed, no group opened.
    assert _history_entries(viewer) == entries
    assert _store(viewer).page(0) == ()
    # Preflight must NOT clean the selection as a side effect: no commit
    # happened, so the ids stay exactly as the gesture left them.
    assert set(_selected_values(viewer)) == ids
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None


def test_intervening_replacement_aborts_without_overwrite(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    _marquee(viewer, (60.0, 60.0), (440.0, 320.0))
    anchor = _widget(viewer, 250.0, 300.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
    # An external edit replaces a still-existing selected annotation
    # with a NEW object under the same id while the move is armed.
    replacement = dataclasses.replace(
        a, points=(Point(0.0, 0.0), Point(3.0, 3.0)))
    assert _store(viewer).remove(a.id) is True
    _store(viewer).add(replacement, 0)
    entries = _history_entries(viewer)
    can_undo_before = viewer.document_history.can_undo()
    with _chrome_hidden(viewer):
        _move(viewer, anchor[0] + 60.0, anchor[1])
        _release(viewer, anchor[0] + 60.0, anchor[1])
    # The unexpected edit is kept byte-exact — never overwritten.
    assert _store(viewer).get(a.id) is replacement
    assert _store(viewer).get(a.id).points == replacement.points
    assert _store(viewer).get(b.id) is b    # untouched too (abort, not move)
    assert _history_entries(viewer) == entries    # no entry appeared
    assert viewer.document_history.can_undo() is can_undo_before
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    # The selection itself is not altered by a preflight abort.
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}


# ======================================================= routing regressions

def test_press_on_unselected_body_keeps_m681_marquee(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    c = _seed(viewer, _pen_stroke((100.0, 360.0), (400.0, 360.0)))
    _marquee(viewer, (60.0, 60.0), (440.0, 140.0))
    assert _selected_values(viewer) == (a.id.value,)
    # Press on the UNSELECTED stroke below: the M6.8.1 marquee opens.
    anchor = _widget(viewer, 250.0, 360.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
    assert viewer._move_gesture is None
    assert viewer._region_gesture is not None
    with _chrome_hidden(viewer):
        _move(viewer, anchor[0] + 100.0, anchor[1] - 40.0)
        _release(viewer, anchor[0] + 100.0, anchor[1] - 40.0)
    assert _history_entries(viewer) == 0
    assert viewer._region_gesture is None


def test_tablet_group_move_not_duplicated_by_synthetic_mouse(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke((100.0, 120.0), (400.0, 120.0)))
    _marquee(viewer, (60.0, 80.0), (440.0, 160.0))
    anchor = _widget(viewer, 250.0, 120.0)
    target = _widget(viewer, 250.0 + 60.0 / _factor(viewer), 120.0)
    # One stylus gesture: press, two moves, release. The tablet events
    # are accepted, so Qt synthesizes no fallback mouse stream — and the
    # dual-stream guard additionally drops one if it appears.
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, *anchor)
        assert viewer._selection_tablet_open is True
        _tablet(viewer, QEvent.Type.TabletMove,
                anchor[0] + 30.0, anchor[1])
        # A synthesized mouse press while the stylus owns the pointer:
        # dropped, never a second gesture.
        _press(viewer, *anchor)
        _move(viewer, *target)
        _tablet(viewer, QEvent.Type.TabletMove, *target)
        _tablet(viewer, QEvent.Type.TabletRelease, *target)
        # The stylus release already closed the gesture; the trailing
        # synthesized mouse release is a routed no-op.
        _release(viewer, *target)
    pdx = 60.0 / _factor(viewer)
    moved = _store(viewer).get(a.id)
    assert moved.points[0].x == pytest.approx(100.0 + pdx)
    assert _history_entries(viewer) == 1


def test_pan_drag_over_selected_body_moves_selection(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    a = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)))
    # Pan makes selections only via double-click; select the annotation
    # directly so this test is about Pan-drag semantics only.
    viewer._selection.set([a.id])
    assert _selected_values(viewer) == (a.id.value,)
    anchor = _widget(viewer, 250.0, 100.0)
    _body_drag(viewer, anchor, 80.0, 0.0)
    # Pan drag on a selected body moves the selection (one history entry).
    moved = _store(viewer).get(a.id)
    assert moved is not a
    assert _history_entries(viewer) == 1


# ================================================ cross-page group transfer


def _entry_offset(viewer, page_index):
    """Per-page offset ``o = b / B`` (via the pure affine inverse)."""
    entry = viewer._pipeline.layout.entry(page_index)
    pt = viewer._region_page_pt(entry, 0.0, 0.0)
    return (-float(pt[0]), -float(pt[1]))


def _pages_snapshot(viewer, pages=(0, 1)):
    store = _store(viewer)
    return tuple(
        tuple((item.id.value, store.locate(item.id))
              for item in store.page(p))
        for p in pages)


def test_transfer_to_next_page_keeps_visible_selectable(
        qtbot, tmp_path, cleared_settings):
    """Deliberate drop onto page 1: visible, stored there, undoable."""
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage0.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 50.0), (400.0, 50.0)), 0)
    b = _seed(viewer, _pen_stroke((50.0, 60.0), (200.0, 60.0)), 1)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0))
    assert _selected_values(viewer) == (a.id.value,)

    anchor = _widget(viewer, 250.0, 50.0)
    dx, dy = 40.0, 100.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdx, pdy = dx / factor, dy / factor

    # Stored on the DESTINATION page and gone from the source page: the
    # annotation became part of page 1's annotation store.
    moved = _store(viewer).get(a.id)
    assert moved is not None and moved is not a  # new object, same id
    assert moved.page_id.value == _page_id(viewer, 1).value
    assert _store(viewer).locate(a.id) == (1, 1)  # appended after b
    assert _store(viewer).page(0) == ()
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        b.id.value, a.id.value)
    assert _store(viewer).page(1)[0] is b  # unrelated annotation untouched

    # Verified shared-space conversion: p_j = p_k - (o_j - o_k) with the
    # shared widget delta already baked into p_k.
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    assert o0 != o1  # unequal page sizes/offsets are actually exercised
    exp0 = (100.0 + pdx + o0[0] - o1[0], 50.0 + pdy + o0[1] - o1[1])
    exp1 = (400.0 + pdx + o0[0] - o1[0], 50.0 + pdy + o0[1] - o1[1])
    assert (moved.points[0].x, moved.points[0].y) == pytest.approx(
        exp0, abs=1e-6)
    assert (moved.points[1].x, moved.points[1].y) == pytest.approx(
        exp1, abs=1e-6)
    exp_c = ((exp0[0] + exp1[0]) / 2.0, exp0[1])
    layout = viewer._pipeline.layout
    new_c = viewer._page_widget_point(layout.entry(1), exp_c[0], exp_c[1])
    orig_c = viewer._page_widget_point(layout.entry(0), 250.0, 50.0)
    # Widget-space invariance across unequal page sizes and offsets.
    assert new_c == pytest.approx((orig_c[0] + dx, orig_c[1] + dy),
                                  abs=1e-6)

    # Visible at the intended widget-space position after the release.
    with _chrome_hidden(viewer):
        image = _render_viewer(viewer)
    assert _ink_present(image, new_c[0], new_c[1])

    # Hit-testable on the destination page's rendered geometry.
    assert pick_annotation(_store(viewer).page(1), exp_c[0], exp_c[1],
                           2.0) is moved

    # Still selectable by marquee on its new page.
    _marquee(viewer, (exp0[0] - 20.0, exp0[1] - 15.0),
             (exp1[0] + 20.0, exp0[1] + 15.0), page_index=1)
    assert _selected_values(viewer) == (a.id.value,)

    # One grouped undo entry restores the exact pristine state...
    assert _history_entries(viewer) == 1
    history = viewer.document_history
    history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).locate(a.id) == (0, 0)
    # ...and redo reproduces the identical committed layout and object.
    history.redo()
    redone = _store(viewer).get(a.id)
    assert _store(viewer).locate(a.id) == (1, 1)
    assert redone is moved
    assert (redone.points[0].x, redone.points[0].y) == pytest.approx(
        exp0, abs=1e-6)
    assert redone.page_id.value == _page_id(viewer, 1).value


def test_transfer_upward_to_previous_page(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage1.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 60.0), (400.0, 60.0)), 1)
    d = _seed(viewer, _pen_stroke((50.0, 40.0), (200.0, 40.0)), 0)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0), page_index=1)
    assert _selected_values(viewer) == (a.id.value,)

    anchor = _widget(viewer, 250.0, 60.0, 1)
    dx, dy = -40.0, -100.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdx, pdy = dx / factor, dy / factor

    moved = _store(viewer).get(a.id)
    assert moved.page_id.value == _page_id(viewer, 0).value
    assert _store(viewer).locate(a.id) == (0, 1)   # appended after d
    assert _store(viewer).page(1) == ()            # removed from source
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    exp0 = (100.0 + pdx + o1[0] - o0[0], 60.0 + pdy + o1[1] - o0[1])
    exp1 = (400.0 + pdx + o1[0] - o0[0], 60.0 + pdy + o1[1] - o0[1])
    assert (moved.points[0].x, moved.points[0].y) == pytest.approx(
        exp0, abs=1e-6)
    assert (moved.points[1].x, moved.points[1].y) == pytest.approx(
        exp1, abs=1e-6)
    exp_c = ((exp0[0] + exp1[0]) / 2.0, exp0[1])
    layout = viewer._pipeline.layout
    new_c = viewer._page_widget_point(layout.entry(0), exp_c[0], exp_c[1])
    orig_c = viewer._page_widget_point(layout.entry(1), 250.0, 60.0)
    assert new_c == pytest.approx((orig_c[0] + dx, orig_c[1] + dy),
                                  abs=1e-6)
    with _chrome_hidden(viewer):
        image = _render_viewer(viewer)
    assert _ink_present(image, new_c[0], new_c[1])
    assert pick_annotation(_store(viewer).page(0), exp_c[0], exp_c[1],
                           2.0) is moved
    # One entry; undo restores exact pages/objects, redo reproduces.
    assert _history_entries(viewer) == 1
    history = viewer.document_history
    history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1
    assert _store(viewer).get(a.id) is a
    history.redo()
    assert _pages_snapshot(viewer) == (
        ((d.id.value, (0, 0)), (a.id.value, (0, 1))),
        ())
    assert _store(viewer).get(a.id) is not a  # committed layout again


def test_preview_stays_visible_even_as_destination_flips(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage2.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 50.0), (400.0, 50.0)), 0)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0))
    anchor = _widget(viewer, 250.0, 50.0)
    factor = _factor(viewer)
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        pointer = list(anchor)
        layout = viewer._pipeline.layout
        row0 = viewer._region_page_screen(layout.entry(0))
        row1 = viewer._region_page_screen(layout.entry(1))
        assert row1.y > row0.y + row0.height  # a real gutter exists
        for dy in (30.0, 60.0, 76.0, 95.0, 130.0, 160.0):
            pointer[1] = anchor[1] + dy
            _move(viewer, pointer[0], pointer[1])
            # Nothing is ever written while dragging.
            assert _store(viewer).page(0) == baseline0
            assert _store(viewer).page(1) == baseline1
            # The destination policy flips by the moved center: page
            # containment first, nearest rect (the gutter's Ry midpoint)
            # inside the gutter.
            center = 50.0 + dy
            in_gutter = row0.y + row0.height < center < row1.y
            midpoint = (row0.y + row0.height + row1.y) / 2.0
            crossed = center > midpoint
            dst = viewer._transform_dsts.get(a.id)
            assert dst == (1 if crossed else 0)
            if in_gutter:
                # The inter-page gap still shows a visible, continuous
                # (nearest-assigned) preview — asserted below by ink.
                pass
            candidate = viewer._transform_preview[a.id]
            expected_home = _page_id(viewer, 1 if crossed else 0).value
            assert candidate.page_id.value == expected_home
            expected_y = 50.0 + dy / factor
            if crossed:
                expected_y -= (o1[1] - o0[1])
            # ...while the preview stays at the pointer either way.
            assert candidate.points[0].y == pytest.approx(
                expected_y, abs=1e-6)
            image = _render_viewer(viewer)
            assert _ink_present(image, pointer[0], pointer[1])
        _release(viewer, pointer[0], pointer[1])
    # The release committed the transfer it previewed.
    assert _store(viewer).locate(a.id)[0] == 1
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None


def test_gap_release_drops_on_nearest_page(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage3.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 60.0), (400.0, 60.0)), 1)
    d = _seed(viewer, _pen_stroke((50.0, 40.0), (200.0, 40.0)), 0)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0), page_index=1)

    # Target widget center: the left gutter beside page 0's rect
    # (x < 150) within page 0's row (y = 100). It is inside NO displayed
    # page rectangle, so the deterministic nearest-rect policy applies:
    # page 0's rect is strictly closer than page 1's.
    anchor = _widget(viewer, 250.0, 60.0, 1)
    dx, dy = -415.0, -96.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdx, pdy = dx / factor, dy / factor
    moved = _store(viewer).get(a.id)
    assert _store(viewer).locate(a.id) == (0, 1)   # nearest page wins
    assert moved.page_id.value == _page_id(viewer, 0).value
    assert _store(viewer).page(1) == ()
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    exp0 = (100.0 + pdx + o1[0] - o0[0], 60.0 + pdy + o1[1] - o0[1])
    exp1 = (400.0 + pdx + o1[0] - o0[0], 60.0 + pdy + o1[1] - o0[1])
    assert (moved.points[0].x, moved.points[0].y) == pytest.approx(
        exp0, abs=1e-6)
    exp_c = ((exp0[0] + exp1[0]) / 2.0, exp0[1])
    new_c = viewer._page_widget_point(
        viewer._pipeline.layout.entry(0), exp_c[0], exp_c[1])
    orig_c = _widget(viewer, 250.0, 60.0, 1)  # the press-space center
    assert new_c == pytest.approx((orig_c[0] + dx, orig_c[1] + dy),
                                  abs=1e-6)
    with _chrome_hidden(viewer):
        image = _render_viewer(viewer)
    # Parts beyond the destination page's edges stay clipped by
    # page-rect painting (established app-wide contract); the in-page
    # tail is visible at the drop row.
    assert _ink_present(image, 180.0, new_c[1])
    viewer.document_history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1


def test_transferred_annotation_remains_movable(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage4.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 50.0), (400.0, 50.0)), 0)
    b = _seed(viewer, _pen_stroke((50.0, 60.0), (200.0, 60.0)), 1)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0))
    _body_drag(viewer, _widget(viewer, 250.0, 50.0), 0.0, 100.0)
    assert _store(viewer).locate(a.id) == (1, 1)
    # Committed page-1-local geometry via the verified conversion.
    factor = _factor(viewer)
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    mo = _store(viewer).get(a.id)
    exp_y = 50.0 + 100.0 / factor + o0[1] - o1[1]
    assert mo.points[0].y == pytest.approx(exp_y, abs=1e-6)
    # Select it again on its new page and move it once more — this
    # time back upward onto page 0.
    _marquee(viewer, (10.0, 5.0), (340.0, 45.0), page_index=1)
    assert _selected_values(viewer) == (a.id.value,)
    cx = (mo.points[0].x + mo.points[1].x) / 2.0
    anchor = viewer._page_widget_point(
        viewer._pipeline.layout.entry(1), cx, exp_y)
    _body_drag(viewer, anchor, -40.0, -95.0)
    assert _store(viewer).locate(a.id) == (0, 0)
    assert _history_entries(viewer) == 2
    viewer.document_history.undo()
    assert _store(viewer).locate(a.id) == (1, 1)   # first move restored
    viewer.document_history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1
    assert _store(viewer).get(a.id) is a


def test_mixed_group_splits_pages_by_member_centers(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage5.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    stroke = _seed(viewer, _pen_stroke((90.0, 25.0), (380.0, 25.0)), 0)
    hl = _seed(viewer, _highlighter(x=90.0, y=60.0), 0)
    shape = _seed(viewer, _rect_shape(160.0, 85.0, 280.0, 110.0), 0)
    b = _seed(viewer, _pen_stroke((50.0, 80.0), (200.0, 80.0)), 1)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 10.0), (440.0, 115.0))
    assert set(_selected_values(viewer)) == {
        stroke.id.value, hl.id.value, shape.id.value}

    anchor = _widget(viewer, 200.0, 25.0, 0)
    dx, dy = 0.0, 70.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdy = dy / factor
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)

    # Center policy per member: the stroke's center is still inside
    # page 0's rect (in-place move), while the highlighter and the
    # shape cross onto page 1 (independent transfers, one group op).
    assert _store(viewer).locate(stroke.id) == (0, 0)
    assert _store(viewer).locate(hl.id) == (1, 1)
    assert _store(viewer).locate(shape.id) == (1, 2)
    # Destination z-order: existing content first, then arrivals in
    # ascending (source page, source position) order.
    assert tuple(item.id.value for item in _store(viewer).page(0)) == (
        stroke.id.value,)
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        b.id.value, hl.id.value, shape.id.value)

    got_stroke = _store(viewer).get(stroke.id)
    assert (got_stroke.points[0].y, got_stroke.points[1].y) == (
        pytest.approx(25.0 + pdy), pytest.approx(25.0 + pdy))
    assert got_stroke.points[0].x == pytest.approx(90.0)
    got_hl = _store(viewer).get(hl.id)
    assert isinstance(got_hl, Highlighter)
    assert got_hl.color == hl.color and got_hl.opacity == hl.opacity
    assert got_hl.points[0].y == pytest.approx(
        60.0 + pdy + o0[1] - o1[1], abs=1e-6)
    assert got_hl.points[0].x == pytest.approx(
        90.0 + o0[0] - o1[0], abs=1e-6)
    got_shape = _store(viewer).get(shape.id)
    assert isinstance(got_shape, Shape) and got_shape.kind == shape.kind
    assert got_shape.rect.origin.y == pytest.approx(
        85.0 + pdy + o0[1] - o1[1], abs=1e-6)
    assert got_shape.rect.origin.x == pytest.approx(
        160.0 + o0[0] - o1[0], abs=1e-6)
    assert got_shape.rect.height == shape.rect.height
    assert got_shape.rect.width == shape.rect.width

    # ONE grouped entry; undo restores the exact pristine state.
    assert _history_entries(viewer) == 1
    history = viewer.document_history
    history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1
    assert _store(viewer).get(stroke.id) is stroke
    assert _store(viewer).get(b.id) is b
    history.redo()
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        b.id.value, hl.id.value, shape.id.value)
    snap = _pages_snapshot(viewer)
    history.undo()
    history.redo()
    assert _pages_snapshot(viewer) == snap


def test_group_transfer_spans_three_pages(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage6.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0), (560.0, 100.0)])
    s0 = _seed(viewer, _pen_stroke((100.0, 40.0), (400.0, 40.0)), 0)
    s1 = _seed(viewer, _pen_stroke((60.0, 80.0), (260.0, 80.0)), 1)
    s2 = _seed(viewer, _pen_stroke((80.0, 50.0), (300.0, 50.0)), 2)
    baseline = tuple(_store(viewer).page(p) for p in (0, 1, 2))
    x0, y0 = _widget(viewer, 30.0, 10.0, 0)
    x1, y1 = _widget(viewer, 520.0, 90.0, 2)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, (x0 + x1) / 2.0, (y0 + y1) / 2.0)
        _release(viewer, x1, y1)
    assert set(_selected_values(viewer)) == {
        s0.id.value, s1.id.value, s2.id.value}

    anchor = _widget(viewer, 250.0, 40.0, 0)
    dx, dy = 30.0, 140.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdx, pdy = dx / factor, dy / factor
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    o2 = _entry_offset(viewer, 2)

    # Every member resolved its own destination from its moved center:
    # s0 -> page 1, s1 -> page 2, s2 dropped below everything but with
    # page 2's rect strictly nearest -> it stays on (and within) its
    # own page. One move produced a group spanning pages.
    assert _store(viewer).locate(s0.id) == (1, 0)
    assert _store(viewer).locate(s1.id) == (2, 1)
    assert _store(viewer).locate(s2.id) == (2, 0)
    assert _store(viewer).page(0) == ()
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        s0.id.value,)
    assert tuple(item.id.value for item in _store(viewer).page(2)) == (
        s2.id.value, s1.id.value)   # stayer slot, then arrival on top
    got_s0 = _store(viewer).get(s0.id)
    assert (got_s0.points[0].x, got_s0.points[0].y) == pytest.approx(
        (100.0 + pdx + o0[0] - o1[0], 40.0 + pdy + o0[1] - o1[1]), abs=1e-6)
    got_s1 = _store(viewer).get(s1.id)
    assert (got_s1.points[0].x, got_s1.points[0].y) == pytest.approx(
        (60.0 + pdx + o1[0] - o2[0], 80.0 + pdy + o1[1] - o2[1]), abs=1e-6)
    got_s2 = _store(viewer).get(s2.id)
    assert (got_s2.points[0].x, got_s2.points[0].y) == pytest.approx(
        (80.0 + pdx, 50.0 + pdy), abs=1e-6)   # stayer: shared delta only

    # One grouped entry; undo restores the exact three-page pristine
    # state, redo reproduces it, cycles are stable.
    assert _history_entries(viewer) == 1
    history = viewer.document_history
    history.undo()
    for expected, page in zip(baseline, (0, 1, 2)):
        assert _store(viewer).page(page) == expected
    assert _store(viewer).get(s0.id) is s0
    assert _store(viewer).get(s1.id) is s1
    assert _store(viewer).get(s2.id) is s2
    history.redo()
    snap = _pages_snapshot(viewer, (0, 1, 2))
    history.undo()
    history.redo()
    assert _pages_snapshot(viewer, (0, 1, 2)) == snap


def test_cross_page_undo_redo_exact_pages_order_objects(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage7.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 50.0), (400.0, 50.0)), 0)
    b = _seed(viewer, _pen_stroke((50.0, 60.0), (200.0, 60.0)), 1)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    _marquee(viewer, (60.0, 20.0), (440.0, 110.0))
    anchor = _widget(viewer, 250.0, 50.0)
    _body_drag(viewer, anchor, 25.0, 105.0)
    committed = _store(viewer).get(a.id)
    first_layout = _pages_snapshot(viewer)
    assert _store(viewer).locate(a.id) == (1, 1)
    history = viewer.document_history
    # UNDO: exact source pages, positions, objects, properties, and
    # paint order.
    history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1
    assert _store(viewer).get(a.id) is a
    assert _selected_values(viewer) == (a.id.value,)
    # REDO: same annotations to the same destinations with the same
    # committed geometry and order.
    history.redo()
    assert _pages_snapshot(viewer) == first_layout
    redone = _store(viewer).get(a.id)
    assert redone is committed
    assert (redone.points[0].x, redone.points[1].x) == pytest.approx(
        (committed.points[0].x, committed.points[1].x))
    # Repeated undo/redo cycles are stable.
    history.undo()
    assert _store(viewer).page(0) == baseline0
    history.redo()
    assert _pages_snapshot(viewer) == first_layout


def test_partial_stale_transfer_keeps_survivor_contract(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, name="xpage8.pdf",
        sizes=[(600.0, 120.0), (450.0, 160.0)])
    a = _seed(viewer, _pen_stroke((100.0, 50.0), (400.0, 50.0)), 0)
    b = _seed(viewer, _pen_stroke((100.0, 100.0), (400.0, 100.0)), 0)
    c = _seed(viewer, _pen_stroke((50.0, 60.0), (200.0, 60.0)), 1)
    _marquee(viewer, (60.0, 20.0), (440.0, 115.0))
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    anchor = _widget(viewer, 250.0, 50.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)          # gesture armed, snapshots taken
    assert _store(viewer).remove(b.id) is True   # vanishes mid-gesture
    _body_drag(viewer, anchor, 75.0, 100.0)
    # Survivor a transferred per the shared contract (D7); the stale id
    # was cleaned only after commit.
    assert _store(viewer).locate(a.id) == (1, 1)
    assert _store(viewer).page(0) == ()
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        c.id.value, a.id.value)
    assert _history_entries(viewer) == 1
    assert _selected_values(viewer) == (a.id.value,)
    viewer.document_history.undo()
    assert tuple(item.id.value for item in _store(viewer).page(0)) == (
        a.id.value,)   # b stays gone (its erasing edit is untouched)
    assert _store(viewer).page(1)[0] is c
    assert _store(viewer).get(a.id) is a


def test_lasso_drives_same_cross_page_transfer(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool(),
                                   name="xpage9.pdf",
                                   sizes=[(600.0, 120.0), (450.0, 160.0)])
    s0 = _seed(viewer, _pen_stroke((100.0, 40.0), (400.0, 40.0)), 0)
    s1 = _seed(viewer, _pen_stroke((50.0, 60.0), (200.0, 60.0)), 1)
    baseline0 = _store(viewer).page(0)
    baseline1 = _store(viewer).page(1)
    x0, y0 = _widget(viewer, 40.0, 10.0, 0)
    x1, y1 = _widget(viewer, 430.0, 105.0, 1)
    _lasso_widget(viewer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    assert set(_selected_values(viewer)) == {s0.id.value, s1.id.value}

    anchor = _widget(viewer, 250.0, 40.0, 0)
    dx, dy = 30.0, 95.0
    _body_drag(viewer, anchor, dx, dy)
    factor = _factor(viewer)
    pdx, pdy = dx / factor, dy / factor
    o0 = _entry_offset(viewer, 0)
    o1 = _entry_offset(viewer, 1)
    # s1's moved center stays inside page 1's rect (stayer, shared
    # delta only); s0 crosses onto page 1 and transfers there on top.
    assert _store(viewer).locate(s1.id) == (1, 0)
    assert _store(viewer).locate(s0.id) == (1, 1)
    assert tuple(item.id.value for item in _store(viewer).page(1)) == (
        s1.id.value, s0.id.value)
    assert _store(viewer).page(0) == ()
    got_s1 = _store(viewer).get(s1.id)
    assert got_s1.points[0].y == pytest.approx(60.0 + pdy)
    got_s0 = _store(viewer).get(s0.id)
    assert (got_s0.points[0].x, got_s0.points[0].y) == pytest.approx(
        (100.0 + pdx + o0[0] - o1[0], 40.0 + pdy + o0[1] - o1[1]), abs=1e-6)
    viewer.document_history.undo()
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).page(1) == baseline1


# ========================================== scrolling during an active drag
#
# M6.8.2 movement bugfix (scroll stability). A move gesture records its
# press point as a page-local anchor and measures every delta through the
# *current* viewport mapping, and an active body move is re-applied on
# every scroll. These tests drive the real scrollbars (the single funnel
# wheel/touchpad/scrollbar/keyboard all share) during a live drag.

def _scroll_by(viewer, amount):
    """Scroll the viewport by ``amount`` render px (shared scroll funnel)."""
    bar = viewer._scrollbar
    bar.setValue(bar.value() + int(round(amount)))


def _wheel_angle(viewer, x, y, delta):
    """Send a real mouse-wheel event (angle delta, no pixel delta)."""
    from PySide6.QtCore import QPoint

    gp = viewer.mapToGlobal(QPointF(x, y))
    event = QWheelEvent(QPointF(x, y), QPointF(gp), QPoint(0, 0),
                        QPoint(0, int(delta)), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    viewer.wheelEvent(event)


def _wheel_pixels(viewer, x, y, dy):
    """Send a real touchpad-style wheel event (non-null pixel delta)."""
    from PySide6.QtCore import QPoint

    gp = viewer.mapToGlobal(QPointF(x, y))
    event = QWheelEvent(QPointF(x, y), QPointF(gp), QPoint(0, int(dy)),
                        QPoint(0, 0), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    viewer.wheelEvent(event)


@contextlib.contextmanager
def _instant_motion():
    """Resolve the wheel glide instantly so a scroll settles in-test."""
    from app import fx

    previous = fx.motion_mode()
    fx.set_motion_mode("reduced")
    try:
        yield
    finally:
        fx.set_motion_mode(previous)


def _local_center(annotation):
    extent = annotation_extent(annotation)
    if extent is None:
        return None
    return ((extent[0] + extent[2]) / 2.0, (extent[1] + extent[3]) / 2.0)


def _candidate_center_widget(viewer, annotation_id):
    """Widget-space centre of the live preview candidate through its page."""
    preview = viewer._transform_preview or {}
    candidate = preview.get(annotation_id)
    if candidate is None:
        return None
    dst = (viewer._transform_dsts or {}).get(annotation_id)
    entry = viewer._pipeline.layout.entry(int(dst))
    center = _local_center(candidate)
    if center is None:
        return None
    return viewer._page_widget_point(entry, center[0], center[1])


def _assert_pinned(viewer, annotation_id, expected_widget, tol=1e-4):
    center = _candidate_center_widget(viewer, annotation_id)
    assert center is not None
    assert center == pytest.approx(expected_widget, abs=tol)


def _dest_page_index(viewer, widget_pt):
    """Mirror the destination policy from the *current* viewport rects."""
    rects = []
    for index, entry in viewer._region_pages():
        rect = viewer._region_page_screen(entry)
        if rect is not None:
            rects.append((index, rect))
    for index, rect in rects:
        if (rect.x <= widget_pt[0] <= rect.x + rect.width
                and rect.y <= widget_pt[1] <= rect.y + rect.height):
            return index
    best = None
    for index, rect in rects:
        dx = max(rect.x - widget_pt[0], 0.0,
                 widget_pt[0] - (rect.x + rect.width))
        dy = max(rect.y - widget_pt[1], 0.0,
                 widget_pt[1] - (rect.y + rect.height))
        distance = dx * dx + dy * dy
        if best is None or distance < best[0]:
            best = (distance, index)
    return best[1] if best is not None else None


def _assert_page_contains(viewer, page_index, widget_pt):
    rect = viewer._region_page_screen(
        viewer._pipeline.layout.entry(int(page_index)))
    assert rect is not None
    assert (rect.x <= widget_pt[0] <= rect.x + rect.width
            and rect.y <= widget_pt[1] <= rect.y + rect.height)


@pytest.mark.parametrize("tool_factory",
                         [_selection_tool, _lasso_tool, _pan_tool],
                         ids=["selection", "lasso", "pan"])
def test_scroll_during_cross_page_drag_keeps_preview_attached(
        qtbot, tmp_path, cleared_settings, tool_factory):
    _window, viewer = _open_window(
        qtbot, tmp_path, tool=tool_factory(), name="scrollA.pdf",
        sizes=[(600.0, 400.0)] * 3)
    assert viewer._scrollbar.maximum() > 0     # the layout really scrolls
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    baseline0 = _store(viewer).page(0)
    viewer._selection.set([a.id])

    anchor = _widget(viewer, 200.0, 80.0, 0)
    dx, dy = 40.0, 210.0
    pointer = (anchor[0] + dx, anchor[1] + dy)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        assert viewer._move_gesture is not None
        assert viewer._move_gesture["mode"] == "move"
        assert _store(viewer).get(a.id) is a        # preview never writes
        _assert_pinned(viewer, a.id, pointer)
        _assert_page_contains(viewer, viewer._transform_dsts[a.id], pointer)
        frame_before = tuple(viewer._selection_frame_corners())

        # Scroll with the pointer stationary.
        _scroll_by(viewer, 160)
        assert viewer._move_gesture is not None     # gesture survives
        assert viewer._transform_preview
        assert _store(viewer).get(a.id) is a
        # No drift by the scroll amount: still pinned to the pointer.
        _assert_pinned(viewer, a.id, pointer)
        # The shared frame stays pinned to the previewed group (it does
        # not lag behind or detach when the viewport scrolls).
        frame_after = tuple(viewer._selection_frame_corners())
        for (bx, by), (ax, ay) in zip(frame_before, frame_after):
            assert (ax, ay) == pytest.approx((bx, by), abs=1e-4)
        # Destination recomputed against the current viewport.
        expected_dst = _dest_page_index(viewer, pointer)
        assert expected_dst is not None
        assert viewer._transform_dsts[a.id] == expected_dst
        _assert_page_contains(viewer, expected_dst, pointer)
        # Preview remains visible.
        assert _ink_present(_render_viewer(viewer), *pointer)
        _release(viewer, *pointer)

    # Committed to the page under the pointer, at the previewed spot.
    assert expected_dst != 0                        # crossed off the source
    moved = _store(viewer).get(a.id)
    assert moved is not None and moved is not a
    assert _store(viewer).locate(a.id)[0] == expected_dst
    assert moved.page_id.value == _page_id(viewer, expected_dst).value
    center = _local_center(moved)
    entry = viewer._pipeline.layout.entry(expected_dst)
    committed = viewer._page_widget_point(entry, center[0], center[1])
    assert committed == pytest.approx(pointer, abs=1e-4)
    with _chrome_hidden(viewer):
        assert _ink_present(_render_viewer(viewer), *committed)
    # Hit-testable on its destination page.
    assert pick_annotation(_store(viewer).page(expected_dst),
                           center[0], center[1], 2.0) is moved
    # One grouped entry; undo restores the exact original, redo reproduces.
    assert _history_entries(viewer) == 1
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).page(0) == baseline0
    assert _store(viewer).locate(a.id) == (0, 0)
    viewer.document_history.redo()
    redone = _store(viewer).get(a.id)
    assert _store(viewer).locate(a.id)[0] == expected_dst
    assert redone.page_id.value == _page_id(viewer, expected_dst).value


def test_wheel_scroll_during_drag_keeps_preview_attached(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollW.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    pointer = (anchor[0] + 40.0, anchor[1] + 210.0)
    with _instant_motion(), _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        before = viewer._scrollbar.value()
        _wheel_angle(viewer, pointer[0], pointer[1], -160)   # wheel down
        assert viewer._scrollbar.value() > before            # really scrolled
        assert viewer._move_gesture is not None
        _assert_pinned(viewer, a.id, pointer)
        expected_dst = _dest_page_index(viewer, pointer)
        assert viewer._transform_dsts[a.id] == expected_dst
        _release(viewer, *pointer)
    assert _store(viewer).locate(a.id)[0] == expected_dst


def test_touchpad_pixel_scroll_during_drag_keeps_preview_attached(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollT.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    pointer = (anchor[0] + 40.0, anchor[1] + 210.0)
    with _instant_motion(), _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        before = viewer._scrollbar.value()
        _wheel_pixels(viewer, pointer[0], pointer[1], -200)  # touchpad down
        assert viewer._scrollbar.value() > before
        assert viewer._move_gesture is not None
        _assert_pinned(viewer, a.id, pointer)
        expected_dst = _dest_page_index(viewer, pointer)
        assert viewer._transform_dsts[a.id] == expected_dst
        _release(viewer, *pointer)
    assert _store(viewer).locate(a.id)[0] == expected_dst


def test_scroll_several_viewports_source_page_leaves_view(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollF.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    pointer = (anchor[0] + 40.0, anchor[1] + 210.0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        _scroll_by(viewer, 2000)          # clamps at the bottom; many pages
        assert viewer._move_gesture is not None
        assert 0 not in [i for i, _ in viewer._region_pages()]  # source gone
        _assert_pinned(viewer, a.id, pointer)
        expected_dst = _dest_page_index(viewer, pointer)
        assert expected_dst is not None and expected_dst != 0
        assert viewer._transform_dsts[a.id] == expected_dst
        assert _ink_present(_render_viewer(viewer), *pointer)
        _release(viewer, *pointer)
    moved = _store(viewer).get(a.id)
    assert _store(viewer).locate(a.id)[0] == expected_dst
    center = _local_center(moved)
    entry = viewer._pipeline.layout.entry(expected_dst)
    committed = viewer._page_widget_point(entry, center[0], center[1])
    assert committed == pytest.approx(pointer, abs=1e-4)
    assert _history_entries(viewer) == 1


def test_scroll_without_pointer_movement_adds_no_extra_move(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollN.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    pointer = (anchor[0] + 20.0, anchor[1] + 10.0)   # small, still past thr.
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        assert viewer._move_gesture["moved"] is True
        before = _candidate_center_widget(viewer, a.id)
        assert before == pytest.approx(pointer, abs=1e-4)
        _scroll_by(viewer, 140)                       # pointer stationary
        # Scrolling alone adds no movement: the preview stays put on
        # screen (a double-counted scroll would drift it by ~140 px).
        assert _candidate_center_widget(viewer, a.id) == pytest.approx(
            before, abs=1e-4)
        assert _store(viewer).get(a.id) is a
        _release(viewer, *pointer)
    # Exactly the intended small move: no duplicate, no type change.
    total = sum(len(_store(viewer).page(i)) for i in range(3))
    assert total == 1
    got = _store(viewer).get(a.id)
    assert isinstance(got, InkStroke)
    # No visible jump: the committed object sits exactly under the pointer
    # (its widget position did not change across the scroll). The
    # document-space position shifted by the scroll, which is precisely
    # what keeps it under the pointer — never a double-count.
    dst = _store(viewer).locate(a.id)[0]
    entry = viewer._pipeline.layout.entry(dst)
    center = _local_center(got)
    assert viewer._page_widget_point(
        entry, center[0], center[1]) == pytest.approx(pointer, abs=1e-4)
    assert _history_entries(viewer) == 1


def test_scrolled_drag_of_group_spanning_pages_keeps_members_coherent(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollG.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 60.0), (300.0, 60.0)), 0)
    b = _seed(viewer, _pen_stroke((120.0, 60.0), (320.0, 60.0)), 1)
    baseline = _pages_snapshot(viewer, (0, 1))
    viewer._selection.set([a.id, b.id])

    a_orig = _widget(viewer, 200.0, 60.0, 0)
    b_orig = _widget(viewer, 220.0, 60.0, 1)
    anchor = a_orig
    dx, dy = 30.0, 70.0
    pointer = (anchor[0] + dx, anchor[1] + dy)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, *pointer)
        _scroll_by(viewer, 90)
        assert viewer._move_gesture is not None
        # Both members stay pinned to their own press-time widget spot +
        # the shared widget delta: the group stays visually coherent.
        _assert_pinned(viewer, a.id, (a_orig[0] + dx, a_orig[1] + dy))
        _assert_pinned(viewer, b.id, (b_orig[0] + dx, b_orig[1] + dy))
        _release(viewer, *pointer)

    got_a = _store(viewer).get(a.id)
    got_b = _store(viewer).get(b.id)
    entry_a = viewer._pipeline.layout.entry(
        _store(viewer).locate(a.id)[0])
    entry_b = viewer._pipeline.layout.entry(
        _store(viewer).locate(b.id)[0])
    center_a = _local_center(got_a)
    center_b = _local_center(got_b)
    assert viewer._page_widget_point(
        entry_a, center_a[0], center_a[1]) == pytest.approx(
            (a_orig[0] + dx, a_orig[1] + dy), abs=1e-4)
    assert viewer._page_widget_point(
        entry_b, center_b[0], center_b[1]) == pytest.approx(
            (b_orig[0] + dx, b_orig[1] + dy), abs=1e-4)
    # Identity and z-order are preserved; undo restores byte-exact.
    assert _history_entries(viewer) == 1
    viewer.document_history.undo()
    assert _pages_snapshot(viewer, (0, 1)) == baseline
    viewer.document_history.redo()
    assert _store(viewer).get(a.id) is got_a
    assert _store(viewer).get(b.id) is got_b


def test_escape_after_scroll_cancels_preview_without_mutation(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollE.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    baseline = _store(viewer).page(0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 40.0, anchor[1] + 210.0)
        _scroll_by(viewer, 160)
        assert viewer._transform_preview is not None
        viewer.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).locate(a.id) == (0, 0)
    assert _history_entries(viewer) == 0
    assert _selected_values(viewer) == (a.id.value,)


def test_tool_switch_after_scroll_cancels_preview(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollS.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    baseline = _store(viewer).page(0)
    viewer._selection.set([a.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 40.0, anchor[1] + 210.0)
        _scroll_by(viewer, 160)
        assert viewer._transform_preview is not None
        viewer._on_tool_selected(_pen_tool())
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert _store(viewer).page(0) == baseline
    assert _store(viewer).get(a.id) is a
    assert _history_entries(viewer) == 0


def test_intervening_replacement_during_scrolled_drag_aborts(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, name="scrollR.pdf",
                                   sizes=[(600.0, 400.0)] * 3)
    a = _seed(viewer, _pen_stroke((100.0, 80.0), (300.0, 80.0)), 0)
    b = _seed(viewer, _pen_stroke((120.0, 60.0), (320.0, 60.0)), 1)
    viewer._selection.set([a.id, b.id])
    anchor = _widget(viewer, 200.0, 80.0, 0)
    with _chrome_hidden(viewer):
        _press(viewer, *anchor)
        _move(viewer, anchor[0] + 40.0, anchor[1] + 210.0)
        _scroll_by(viewer, 160)
        # An external edit replaces a still-existing selected annotation.
        replacement = dataclasses.replace(
            a, points=(Point(0.0, 0.0), Point(3.0, 3.0)))
        assert _store(viewer).remove(a.id) is True
        _store(viewer).add(replacement, 0)
        _release(viewer, anchor[0] + 40.0, anchor[1] + 210.0)
    # The unexpected edit is kept byte-exact; the group never half-moves.
    assert _store(viewer).get(a.id) is replacement
    assert _store(viewer).get(a.id).points == replacement.points
    assert _store(viewer).get(b.id) is b
    assert _history_entries(viewer) == 0
    assert viewer._transform_preview is None
    assert viewer._move_gesture is None
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
