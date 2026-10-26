"""M6.8.1 Selection/Lasso foundation (drag-region correction).

Drag-region selection of *committed* annotations with reliable,
rendered-geometry hit-testing and view-only selection state. Coverage:

* the Qt-free ordered multi-selection state (set/add/toggle/clear and
  self-healing), the Qt-free frame geometry, and the Qt-free region
  hit-testing — including the explicit containment / crossing /
  contact / disjoint case matrix for both the rectangular marquee and
  the lasso loop;
* viewer routing: a click (or sub-threshold drag) never selects an
  individual annotation; the marquee selects multiple intersecting
  annotations and excludes unrelated ones; the lasso encloses and
  boundary-crosses; empty gestures clear; gestures create no
  annotation, no stroke preview, and no undo entry;
* live marquee/lasso overlay pixels during the drag and a single
  animated ``#8B93E0`` shared selection frame around the whole group
  after release (one frame per selection, never per annotation);
* zoom/scroll/page-offset behaviour, gestures spanning pages (mixed
  page sizes and rotated pages included);
* mouse and stylus parity with no duplicate gesture processing (one
  tablet gesture = one commit, store and history untouched);
* stale-id self-healing on undo/redo/erase ordering, tool switches,
  and document close.

The selection is view state only: it is never persisted, never enters
the store, and never pushes an undo command.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QTabletEvent

from annotations.hit_test import (
    annotation_extent,
    pick_annotation,
    point_hits_annotation,
    point_hits_shape,
    point_hits_stroke,
    polygon_area,
    select_in_polygon,
    select_in_rect,
    stroke_centerline_geometry,
)
from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from annotations.selection import SelectionFrame, SelectionState, frame_for
from app import viewer as viewer_module
from core.types import AnnotationId, Point, Rect, Size
from tests.helpers import make_intrinsic_pdf, make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_ACCENT = (139, 147, 224)  # #8B93E0
_FRAME_PAD = 4.0           # app.viewer._SELECT_FRAME_PAD_PX
_FRAME_PULSE_PHASE = 15.0  # _SELECT_PULSE_PERIOD_TICKS / 4 -> pulse = 1.0


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
    return InkStroke(points=(Point(*p0), Point(*p1)), color=color,
                     width_mm=width_mm, opacity=1.0, **kw)


def _rect_shape(x0, y0, x1, y1, **kw):
    base = dict(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(x0, y0), Size(x1 - x0, y1 - y0)),
                color="#000000", width_mm=1.0)
    base.update(kw)
    return Shape(**base)


def _ellipse_shape(x0, y0, x1, y1, **kw):
    base = dict(kind=ShapeKind.ELLIPSE,
                rect=Rect(Point(x0, y0), Size(x1 - x0, y1 - y0)),
                color="#000000", width_mm=1.0)
    base.update(kw)
    return Shape(**base)


def _line_shape(p0, p1, kind=ShapeKind.LINE, **kw):
    (x0, y0), (x1, y1) = p0, p1
    base = dict(kind=kind,
                points=(Point(*p0), Point(*p1)),
                rect=Rect(Point(min(x0, x1), min(y0, y1)),
                          Size(abs(x1 - x0), abs(y1 - y0))),
                color="#000000", width_mm=1.0)
    base.update(kw)
    return Shape(**base)


def _polygon_shape(points, **kw):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    base = dict(kind=ShapeKind.POLYGON,
                points=tuple(Point(*p) for p in points),
                rect=Rect(Point(min(xs), min(ys)),
                          Size(max(xs) - min(xs), max(ys) - min(ys))),
                color="#000000", width_mm=1.0)
    base.update(kw)
    return Shape(**base)


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


def _open_window(qtbot, tmp_path, tool=None, name="m681.pdf", sizes=None,
                 pdf=None):
    from app.main_window import MainWindow

    if pdf is None:
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
    viewer._interaction.set_tool(tool if tool is not None
                                 else _selection_tool())
    qtbot.wait(120)
    return window, viewer


def _page_id(viewer, page_index=0):
    return viewer._pipeline.layout.entry(page_index).page_id


def _store(viewer):
    return viewer._document.annotations


def _seed(viewer, annotation, page_index=0):
    """Commit an annotation straight into the store on the right page.

    Rewrites ``page_id`` to the layout's page identity so the paint path
    (which matches by page identity) draws its frame, and returns the
    stored (replaced) annotation so tests can compare identities.
    """
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


def _partial_erase_fragments(viewer, stroke, cut_x, page_index=0):
    """Split a seeded stroke with the production partial-eraser.

    Returns the surviving fragments (the first keeps the original id) and
    swaps them into the store, so the store then holds genuine retained
    partial-erase fragments.
    """
    import types

    ys = [p.y for p in stroke.points]
    seg = types.SimpleNamespace(x0=float(cut_x), y0=min(ys) - 20.0,
                                x1=float(cut_x), y1=max(ys) + 20.0)
    frags = viewer._partial_erase_annotation(stroke, seg, 6.0)
    if not isinstance(frags, list) or len(frags) < 2:
        return None
    _store(viewer).remove(stroke.id)
    for frag in frags:
        _store(viewer).add(frag, page_index)
    return frags


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


def _page_widget_corners(viewer, lx0, ly0, lx1, ly1, page_index=0):
    """A marquee's widget-space corner pair from two page-local points."""
    x0, y0 = _widget(viewer, lx0, ly0, page_index)
    x1, y1 = _widget(viewer, lx1, ly1, page_index)
    return (x0, y0), (x1, y1)


def _marquee(viewer, page_anchor=(230.0, 150.0), page_end=(430.0, 300.0),
             page_index=0, steps=4):
    """Press -> drag -> release a rectangular marquee (page-local pts)."""
    (x0, y0), (x1, y1) = _page_widget_corners(
        viewer, *page_anchor, page_end[0], page_end[1], page_index)
    _marquee_widget(viewer, (x0, y0), (x1, y1), steps=steps)


def _marquee_widget(viewer, anchor, end, steps=4):
    """Marquee gesture in raw widget coordinates (may span pages)."""
    x0, y0 = anchor
    x1, y1 = end
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        for i in range(1, steps + 1):
            _move(viewer, x0 + (x1 - x0) * i / steps,
                  y0 + (y1 - y0) * i / steps)
        _release(viewer, x1, y1)


def _lasso(viewer, page_points, page_index=0):
    """Press -> draw -> release a freehand lasso loop (page-local pts)."""
    corners = [_widget(viewer, lx, ly, page_index)
               for lx, ly in page_points]
    _lasso_widget(viewer, corners)


def _lasso_widget(viewer, widget_points):
    """Lasso gesture in raw widget coordinates (may span pages)."""
    with _chrome_hidden(viewer):
        _press(viewer, *widget_points[0])
        for wx, wy in widget_points[1:]:
            _move(viewer, wx, wy)
        _release(viewer, *widget_points[-1])


def _click(viewer, lx, ly, page_index=0):
    wx, wy = _widget(viewer, lx, ly, page_index)
    with _chrome_hidden(viewer):
        _press(viewer, wx, wy)
        _release(viewer, wx, wy)


def _dblclick(viewer, wx, wy):
    viewer.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(wx, wy),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _double_click_widget(viewer, wx, wy):
    """The full Qt double-click sequence: press, release, dblclick, release.

    Drives the viewer's real handlers so the routing (never the
    synthesized first click) is what selects.
    """
    with _chrome_hidden(viewer):
        _press(viewer, wx, wy)
        _release(viewer, wx, wy)
        _dblclick(viewer, wx, wy)
        _release(viewer, wx, wy)


def _pan_double_click(viewer, lx, ly, page_index=0):
    wx, wy = _widget(viewer, lx, ly, page_index)
    _double_click_widget(viewer, wx, wy)


def _pan_click(viewer, lx, ly, page_index=0):
    _click(viewer, lx, ly, page_index)


def _pan_drag(viewer, lx0, ly0, lx1, ly1, page_index=0):
    """Pan a document by dragging between two page-local points."""
    x0, y0 = _widget(viewer, lx0, ly0, page_index)
    x1, y1 = _widget(viewer, lx1, ly1, page_index)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, x1, y1)
        _release(viewer, x1, y1)
    return (x1 - x0, y1 - y0)


def _tablet_tap(viewer, wx, wy):
    """One stationary stylus tap (tablet press + release) at a widget point."""
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, wx, wy)
        _tablet(viewer, QEvent.Type.TabletRelease, wx, wy)


def _tablet_double_tap(viewer, lx, ly, page_index=0):
    """Two stationary stylus taps at one page-local point.

    A stylus tap never produces a ``MouseButtonDblClick``, so this drives
    the tablet-side double-tap recognition directly.
    """
    wx, wy = _widget(viewer, lx, ly, page_index)
    _tablet_tap(viewer, wx, wy)
    _tablet_tap(viewer, wx, wy)


@contextlib.contextmanager
def _chrome_hidden(viewer):
    """Temporarily hide floating chrome for a synthetic canvas click.

    A synthetic ``mousePressEvent`` bypasses Qt's child dispatch, so the
    viewer's own chrome guard would otherwise swallow any press that lands
    under the floating shelf / undo HUD. Hiding the chrome keeps these
    tests about selection behaviour rather than about shelf placement
    (real input never reaches the viewer through a child anyway).
    """
    hidden = []
    for name in ("_shelf", "_undo_hud"):
        widget = getattr(viewer, name, None)
        if widget is None:
            continue
        hidden.append((widget, widget.isVisible()))
        try:
            widget.setVisible(False)
        except Exception:
            pass
    try:
        yield
    finally:
        for widget, visible in hidden:
            try:
                widget.setVisible(visible)
            except Exception:
                pass


def _selected_values(viewer):
    return tuple(sorted(a.value for a in viewer._selection.selected_ids))


def _selected_value(viewer):
    ids = viewer._selection.selected_ids
    return ids[0].value if len(ids) == 1 else None


# --------------------------------------------------- tablet test helpers

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


def _tablet_marquee(viewer, page_anchor, page_end, page_index=0):
    (x0, y0), (x1, y1) = _page_widget_corners(
        viewer, *page_anchor, page_end[0], page_end[1], page_index)
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, x0, y0)
        _tablet(viewer, QEvent.Type.TabletMove, (x0 + x1) / 2, (y0 + y1) / 2)
        _tablet(viewer, QEvent.Type.TabletMove, x1, y1)
        _tablet(viewer, QEvent.Type.TabletRelease, x1, y1)


# ------------------------------------------------------- pixel helpers

def _accent_like(color):
    return (abs(color.red() - _ACCENT[0]) <= 45
            and abs(color.green() - _ACCENT[1]) <= 45
            and abs(color.blue() - _ACCENT[2]) <= 45)


def _max_accent_row(image, y_center, x0, x1, radius=4):
    best = 0
    for y in range(int(y_center) - radius, int(y_center) + radius + 1):
        if y < 0 or y >= image.height():
            continue
        count = sum(1 for x in range(int(x0), int(x1))
                    if _accent_like(image.pixelColor(x, y)))
        best = max(best, count)
    return best


def _max_accent_col(image, x_center, y0, y1, radius=4):
    best = 0
    for x in range(int(x_center) - radius, int(x_center) + radius + 1):
        if x < 0 or x >= image.width():
            continue
        count = sum(1 for y in range(int(y0), int(y1))
                    if _accent_like(image.pixelColor(x, y)))
        best = max(best, count)
    return best


def _extent_widget_rect(viewer, annotation, page_index=0):
    """(left, top, right, bottom) widget coords of one annotation extent."""
    minx, miny, maxx, maxy = annotation_extent(annotation)
    x0, y0 = _widget(viewer, minx, miny, page_index)
    x1, y1 = _widget(viewer, maxx, maxy, page_index)
    return (x0, y0, x1, y1)


def _expected_group_rect(viewer, pairs):
    """Expected shared-frame widget rect for ``(annotation, page)`` pairs.

    Mirrors :meth:`PdfViewer._selection_frame_widget_rect`: the union of
    each annotation's widget-space extent, padded outward by
    ``_SELECT_FRAME_PAD_PX``.
    """
    xs = []
    ys = []
    for annotation, page_index in pairs:
        x0, y0, x1, y1 = _extent_widget_rect(viewer, annotation, page_index)
        xs.extend((x0, x1))
        ys.extend((y0, y1))
    return (min(xs) - _FRAME_PAD, min(ys) - _FRAME_PAD,
            max(xs) + _FRAME_PAD, max(ys) + _FRAME_PAD)


def _group_frame_widget_rect(viewer):
    """The viewer's single shared frame as (left, top, right, bottom)."""
    rect = viewer._selection_frame_widget_rect()
    if rect is None:
        return None
    left, top, width, height = rect
    return (left, top, left + width, top + height)


def _settle_frame(viewer, phase=_FRAME_PULSE_PHASE):
    """Pin a deterministic animation phase (default: pulse at maximum).

    The paint path keeps the timer running but never resets the phase, and
    a running timer cannot tick without the event loop, so the pinned
    phase survives a synchronous ``grab()``.
    """
    viewer._selection_timer.stop()
    viewer._selection_phase = float(phase)
    viewer._selection_timer.start()
    viewer.update()


# =====================================================================
# Qt-free: SelectionState (ordered multi-selection)
# =====================================================================

def test_selection_state_change_reporting():
    state = SelectionState()
    a = AnnotationId()
    b = AnnotationId()
    assert state.has_selection is False
    assert state.selected_ids == ()
    assert state.count == 0
    assert state.clear() is False            # nothing to clear
    assert state.set(()) is False            # empty replace on empty
    assert state.set([a]) is True
    assert state.add([b]) is True
    assert state.add([b]) is False           # duplicate: no change
    assert state.toggle(b) is True           # drop
    assert state.count == 1
    assert state.toggle(b) is True           # re-add
    assert state.count == 2
    assert state.select(None) is True        # replace with none = clear
    assert state.has_selection is False


def test_selection_state_set_replaces_in_order():
    state = SelectionState()
    a = AnnotationId()
    b = AnnotationId()
    c = AnnotationId()
    assert state.set([a, b]) is True
    assert state.selected_ids == (a, b)
    assert state.set([a, b]) is False        # identical order: no-op
    assert state.set([b, a]) is True         # order change = change
    assert state.selected_ids == (b, a)
    assert state.set([a, a, c]) is True      # duplicates de-duplicated
    assert state.selected_ids == (a, c)
    assert state.set([]) is True             # set to empty = cleared
    assert state.has_selection is False


def test_selection_state_tolerates_helper_values():
    state = SelectionState()
    state.set(["plain-string-id"])
    assert state.is_selected("plain-string-id") is True
    assert state.is_selected(None) is False
    assert state.count == 1


def test_selection_state_revalidate_drops_absent():
    from annotations.store import AnnotationStore

    store = AnnotationStore(1)
    shape = Shape(kind=ShapeKind.RECTANGLE,
                  rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)))
    other = Shape(kind=ShapeKind.RECTANGLE,
                  rect=Rect(Point(20.0, 0.0), Size(10.0, 10.0)))
    store.add(shape, 0)
    store.add(other, 0)
    state = SelectionState()
    state.set([shape.id, other.id])
    assert state.revalidate(store) is False
    store.remove(shape.id)
    assert state.revalidate(store) is True
    assert state.selected_ids == (other.id,)
    assert state.has_selection is True
    store.remove(other.id)
    assert state.revalidate(store) is True
    assert state.has_selection is False
    # An unusable store clears rather than keeping stale references.
    state.set([other.id])
    assert state.revalidate(None) is True
    assert state.has_selection is False


def test_selection_state_resolve_self_clears():
    from annotations.store import AnnotationStore

    store = AnnotationStore(1)
    shape = Shape(kind=ShapeKind.RECTANGLE,
                  rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)))
    other = Shape(kind=ShapeKind.RECTANGLE,
                  rect=Rect(Point(20.0, 0.0), Size(10.0, 10.0)))
    store.add(shape, 0)
    store.add(other, 0)
    state = SelectionState()
    state.set([shape.id, other.id])
    resolved = state.resolve(store)
    assert resolved == (shape, other)
    store.remove(shape.id)
    resolved = state.resolve(store)
    assert resolved == (other,)
    assert state.has_selection is True
    store.remove(other.id)
    assert state.resolve(store) == ()
    assert state.has_selection is False


# =====================================================================
# Qt-free: SelectionFrame
# =====================================================================

def test_frame_for_bounds_and_handles():
    shape = _rect_shape(100.0, 100.0, 200.0, 200.0, width_mm=0.0)
    frame = frame_for(shape)
    assert isinstance(frame, SelectionFrame)
    # width 0 -> no padding: bounds are the raw rect extent.
    assert frame.bounds == (100.0, 100.0, 200.0, 200.0)
    assert len(frame.handles) == 8
    assert frame.handles[0] == (100.0, 100.0)   # top-left first
    assert frame.handles[2] == (200.0, 100.0)   # top-right
    assert frame.handles[4] == (200.0, 200.0)   # bottom-right
    assert frame.handles[6] == (100.0, 200.0)   # bottom-left
    assert (150.0, 100.0) in frame.handles      # top edge midpoint
    assert (100.0, 150.0) in frame.handles      # left edge midpoint


def test_frame_for_pads_half_width_and_empty_when_meaningless():
    shape = _rect_shape(0.0, 0.0, 10.0, 10.0, width_mm=1.0)
    frame = frame_for(shape)
    half = 1.0 * 72.0 / 25.4 / 2.0
    assert frame.bounds[0] == pytest.approx(-half)
    assert frame.bounds[2] == pytest.approx(10.0 + half)
    assert frame_for(None).bounds == ()
    empty = InkStroke(points=())
    assert frame_for(empty).bounds == ()


# =====================================================================
# Qt-free: region hit-testing case matrix (marquee + lasso)
# =====================================================================

def test_marquee_intersecting_geometry_selects():
    # Rect outline crossing the marquee boundary.
    shape = _rect_shape(0.0, 0.0, 100.0, 100.0)
    assert select_in_rect([shape], (90.0, 40.0, 200.0, 60.0), 0.0)
    # Stroke crossing the marquee (the rect spans the stroke's lane).
    stroke = _pen_stroke((0.0, 100.0), (200.0, 100.0))
    assert select_in_rect([stroke], (90.0, 90.0, 110.0, 300.0), 0.0)


def test_marquee_unfilled_interior_not_selected():
    # The marquee lies entirely inside an unfilled shape's interior:
    # no rendered geometry there, so nothing is selected.
    shape = _rect_shape(0.0, 0.0, 100.0, 100.0)
    assert not select_in_rect([shape], (40.0, 40.0, 60.0, 60.0), 0.0)


def test_marquee_filled_interior_selects_both_containments():
    # Filled shape containing the marquee.
    blob = _rect_shape(0.0, 0.0, 100.0, 100.0, fill_enabled=True)
    assert select_in_rect([blob], (40.0, 40.0, 60.0, 60.0), 0.0)
    # Small filled shape fully inside the marquee.
    small = _rect_shape(10.0, 10.0, 20.0, 20.0, fill_enabled=True)
    assert select_in_rect([small], (0.0, 0.0, 100.0, 100.0), 0.0)
    # Small unfilled shape fully inside the marquee: the outline is
    # rendered geometry and lies within the rect.
    outline = _rect_shape(10.0, 10.0, 20.0, 20.0)
    assert select_in_rect([outline], (0.0, 0.0, 100.0, 100.0), 0.0)


def test_marquee_excludes_disjoint_and_bbox_only():
    shape = _rect_shape(0.0, 0.0, 100.0, 100.0)
    assert not select_in_rect([shape], (200.0, 200.0, 300.0, 300.0), 0.0)
    # A marquee that only overlaps the annotation's bbox but not its
    # ink: an L-shaped stroke's bbox quadrant off the ink.
    stroke = InkStroke(
        points=(Point(0.0, 0.0), Point(100.0, 0.0),
                Point(100.0, 100.0)),
        color="#000000", width_mm=0.5)
    assert not select_in_rect([stroke], (10.0, 30.0, 60.0, 80.0), 0.0)
    assert select_in_rect([stroke], (50.0, 30.0, 120.0, 40.0), 0.0)


def test_marquee_pressure_band_respected():
    base = dict(points=(Point(0.0, 0.0), Point(100.0, 0.0)), width_mm=4.0)
    thin = InkStroke(**base, pressures=(0.0, 0.0), pressure_enabled=True)
    # OPT into the light-pressure band: the marquee beyond the narrowed
    # half-width does not select, the full band would.
    half = 4.0 * 72.0 / 25.4 / 2.0
    _, halves = stroke_centerline_geometry(thin)
    assert max(halves) < half
    # Beyond the narrowed band: no contact.
    beyond = (50.0, 3.5, 60.0, 4.0)
    assert not select_in_rect([thin], beyond, 0.0)
    # Inside the band: the marquee straddles the centerline.
    assert select_in_rect([thin], (50.0, -0.5, 60.0, 0.5), 0.0)


def test_marquee_tolerance_zoom_correct_semantics():
    stroke = _pen_stroke((200.0, 200.0), (400.0, 400.0), width_mm=0.5)
    far = (100.0, 300.0, 110.0, 310.0)
    assert not select_in_rect([stroke], far, 0.0)
    # A large tolerance reaches the geometry (this is how the zoom
    # converted screen tolerance behaves at low zoom).
    assert select_in_rect([stroke], far, 150.0)


def test_marquee_highlighter_selects():
    hl = Highlighter(points=(Point(0.0, 100.0), Point(200.0, 100.0)),
                     color="#FFFF00", width_mm=3.0, opacity=0.4)
    # The marquee reaches up into the highlighter's band (half of 3 mm
    # is ~8.5 pt below y=100); the crossing edge selects.
    assert select_in_rect([hl], (100.0, 95.0, 120.0, 200.0), 0.0)
    assert not select_in_rect([hl], (100.0, 130.0, 120.0, 260.0), 0.0)


def test_lasso_enclosed_and_partial_select():
    stroke = _pen_stroke((0.0, 0.0), (100.0, 100.0))
    # Fully enclosed.
    assert select_in_polygon(
        [stroke], ((-10.0, -10.0), (120.0, -10.0), (120.0, 120.0)),
        0.0) == (stroke,)
    # Partially enclosed / boundary crossing: intersection semantics.
    assert select_in_polygon(
        [stroke], ((40.0, 60.0), (80.0, 20.0), (60.0, 110.0)), 0.0)


def test_lasso_excludes_disjoint_and_degenerate():
    stroke = _pen_stroke((0.0, 0.0), (100.0, 100.0), width_mm=0.5)
    far = ((200.0, 200.0), (260.0, 200.0), (230.0, 260.0))
    assert select_in_polygon([stroke], far, 0.0) == ()
    assert select_in_polygon([stroke], ((10.0, 10.0), (20.0, 20.0)),
                             0.0) == ()       # zero-area (line)
    assert select_in_polygon([stroke], ((1.0, 1.0),), 0.0) == ()  # <3


def test_lasso_shapes_outline_fill_and_containment():
    # Filled shape fully enclosed; the loop drawn inside a large filled
    # shape selects it too (reverse containment).
    blob = _rect_shape(4000.0, 0.0, 6000.0, 300.0, fill_enabled=True)
    assert select_in_polygon(
        [blob], ((4400.0, 50.0), (4800.0, 50.0), (4600.0, 150.0)),
        0.0) == (blob,)
    # Unfilled outline crossing the loop boundary.
    edge = _rect_shape(0.0, 0.0, 100.0, 100.0)
    assert select_in_polygon(
        [edge], ((90.0, 40.0), (200.0, 40.0), (150.0, 60.0)), 0.0)
    # Disjoint.
    assert select_in_polygon(
        [edge], ((200.0, 200.0), (260.0, 200.0), (230.0, 260.0)),
        0.0) == ()


def test_marquee_preserves_document_order():
    a = _rect_shape(100.0, 100.0, 200.0, 200.0)   # first in document
    b = _pen_stroke((100.0, 400.0), (400.0, 400.0))
    c = _rect_shape(300.0, 300.0, 400.0, 400.0)
    hits = select_in_rect([a, b, c], (0.0, 0.0, 500.0, 500.0), 0.0)
    assert hits == (a, b, c)
    # Caller order is preserved verbatim (the viewer iterates pages in
    # order, giving document order).
    hits_rev = select_in_rect([c, b, a], (0.0, 0.0, 500.0, 500.0), 0.0)
    assert hits_rev == (c, b, a)


# =====================================================================
# Qt-free: point pick (click selection) case matrix
# =====================================================================

def test_point_hits_stroke_band_and_tolerance():
    stroke = _pen_stroke((0.0, 100.0), (200.0, 100.0), width_mm=2.0)
    centers, halves = stroke_centerline_geometry(stroke)
    half = 2.0 * 72.0 / 25.4 / 2.0
    assert point_hits_stroke(centers, halves, 100.0, 100.0, 0.0) is True
    # Just outside the rendered band, no tolerance.
    assert point_hits_stroke(centers, halves, 100.0, 100.0 + half + 1.0,
                             0.0) is False
    # The same point is selected once the pick tolerance covers it.
    assert point_hits_stroke(centers, halves, 100.0, 100.0 + half + 0.5,
                             1.0) is True


def test_point_hits_annotation_kinds_and_fill():
    pen = _pen_stroke((0.0, 50.0), (100.0, 50.0), width_mm=1.0)
    assert point_hits_annotation(pen, 50.0, 50.0, 0.0) is True
    assert point_hits_annotation(pen, 50.0, 200.0, 0.0) is False

    hl = Highlighter(points=(Point(0.0, 80.0), Point(100.0, 80.0)),
                     color="#FFFF00", width_mm=3.0)
    assert point_hits_annotation(hl, 50.0, 80.0, 0.0) is True

    # An unfilled rect is only its outline band; the interior is empty.
    rect = _rect_shape(0.0, 0.0, 100.0, 100.0)
    assert point_hits_shape(rect, 50.0, 0.0, 0.0) is True     # top edge
    assert point_hits_shape(rect, 50.0, 50.0, 0.0) is False   # empty inside
    # A filled rect selects anywhere inside.
    filled = _rect_shape(0.0, 0.0, 100.0, 100.0, fill_enabled=True)
    assert point_hits_shape(filled, 50.0, 50.0, 0.0) is True


def test_pick_annotation_respects_paint_order():
    # A highlighter, a shape, and a pen stroke all under the same point.
    hl = Highlighter(points=(Point(50.0, 50.0), Point(150.0, 50.0)),
                     color="#FFFF00", width_mm=4.0)
    shape = _rect_shape(40.0, 20.0, 160.0, 80.0, fill_enabled=True)
    pen = _pen_stroke((40.0, 50.0), (160.0, 50.0), width_mm=2.0)
    # Pen ink paints last -> topmost.
    assert pick_annotation([hl, shape, pen], 100.0, 50.0, 0.0) is pen
    # Without the pen, the shape is above the highlighter.
    assert pick_annotation([hl, shape], 100.0, 50.0, 0.0) is shape
    # Only the highlighter -> it.
    assert pick_annotation([hl], 100.0, 50.0, 0.0) is hl
    # Nothing under the point.
    assert pick_annotation([hl, shape, pen], 400.0, 400.0, 0.0) is None


def test_pick_annotation_later_insertion_wins_in_layer():
    a = _pen_stroke((40.0, 50.0), (160.0, 50.0), width_mm=2.0)
    b = _pen_stroke((40.0, 50.0), (160.0, 50.0), width_mm=2.0)
    assert pick_annotation([a, b], 100.0, 50.0, 0.0) is b
    assert pick_annotation([b, a], 100.0, 50.0, 0.0) is a


# =====================================================================
# Viewer: gesture routing
# =====================================================================

def test_click_selects_individual_annotation(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _click(viewer, 250.0, 150.0)      # a click ON the ink
    assert _selected_value(viewer) == stroke.id.value
    assert viewer._selection.count == 1
    assert viewer._region_gesture is None


def test_click_empty_canvas_clears_selection(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _click(viewer, 250.0, 150.0)
    assert viewer._selection.has_selection is True
    # A click on empty canvas clears (never selects). y=450 is blank
    # canvas between the ink band and the floating scrollbars.
    _click(viewer, 250.0, 450.0)
    assert viewer._selection.has_selection is False
    assert stroke.id.value not in _selected_values(viewer)


def test_click_replaces_previous_selection(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    a = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    b = _seed(viewer, _pen_stroke((100.0, 300.0), (400.0, 300.0)))
    _click(viewer, 250.0, 150.0)
    assert _selected_value(viewer) == a.id.value
    _click(viewer, 250.0, 300.0)
    assert _selected_value(viewer) == b.id.value   # replaced, not added


def test_click_picks_topmost_of_overlapping(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    # A filled shape and a pen stroke over the same point: pen ink wins.
    _seed(viewer, _rect_shape(150.0, 100.0, 350.0, 200.0,
                              fill_enabled=True))
    pen = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _click(viewer, 250.0, 150.0)
    assert _selected_value(viewer) == pen.id.value

    # Two pen strokes crossing the same point: the later-inserted wins.
    first = _seed(viewer, _pen_stroke((100.0, 500.0), (400.0, 500.0)))
    second = _seed(viewer, _pen_stroke((100.0, 500.0), (400.0, 500.0)))
    assert first.id.value != second.id.value
    _click(viewer, 250.0, 500.0)
    assert _selected_value(viewer) == second.id.value


def test_subthreshold_drag_on_ink_selects_individual(qtbot, tmp_path,
                                                     cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    with _chrome_hidden(viewer):
        wx, wy = _widget(viewer, 250.0, 150.0)
        _press(viewer, wx, wy)
        _move(viewer, wx + 2.0, wy + 2.0)   # below _MARQUEE_MIN_PX
        _release(viewer, wx + 3.0, wy + 3.0)
    assert _selected_value(viewer) == stroke.id.value


def test_subthreshold_drag_on_empty_canvas_clears(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _click(viewer, 250.0, 150.0)
    assert viewer._selection.has_selection is True
    with _chrome_hidden(viewer):
        wx, wy = _widget(viewer, 250.0, 450.0)
        _press(viewer, wx, wy)
        _move(viewer, wx + 2.0, wy + 2.0)
        _release(viewer, wx + 3.0, wy + 3.0)
    assert viewer._selection.has_selection is False
    assert stroke.id.value not in _selected_values(viewer)


def test_drag_beyond_threshold_marquees_not_clicks(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    a = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    b = _seed(viewer, _rect_shape(100.0, 250.0, 400.0, 320.0))
    # A real marquee drag selects both, exactly as before.
    _marquee(viewer, (50.0, 100.0), (450.0, 400.0))
    assert _selected_values(viewer) == tuple(sorted((a.id.value,
                                                     b.id.value)))


def test_marquee_selects_multiple_and_excludes(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    a = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    b = _seed(viewer, _rect_shape(100.0, 250.0, 400.0, 320.0))
    other = _seed(viewer, _pen_stroke((100.0, 600.0), (400.0, 600.0)))

    _marquee(viewer, (50.0, 120.0), (450.0, 500.0))
    assert _selected_values(viewer) == tuple(sorted((a.id.value,
                                                     b.id.value)))
    assert other.id.value not in _selected_values(viewer)


def test_marquee_selects_across_annotation_kinds(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    hl = _seed(viewer, Highlighter(points=(Point(100.0, 150.0),
                                           Point(400.0, 150.0)),
                                   color="#FFFF00", width_mm=3.0))
    shape = _seed(viewer, _rect_shape(120.0, 250.0, 380.0, 300.0,
                                      fill_enabled=True))
    pen = _seed(viewer, _pen_stroke((100.0, 400.0), (400.0, 400.0)))
    polygon = _seed(viewer, Shape(kind=ShapeKind.POLYGON,
                                  points=(Point(300.0, 480.0),
                                          Point(420.0, 480.0),
                                          Point(360.0, 560.0)),
                                  rect=Rect(Point(300.0, 480.0),
                                            Size(120.0, 80.0)),
                                  close_path=True, width_mm=1.0))
    _marquee(viewer, (50.0, 120.0), (460.0, 600.0))
    assert _selected_values(viewer) == tuple(sorted(
        (hl.id.value, shape.id.value, pen.id.value, polygon.id.value)))


def test_marquee_empty_gesture_clears(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert viewer._selection.has_selection is True
    # A marquee over empty canvas selects nothing -> clears.
    _marquee(viewer, (50.0, 640.0), (450.0, 700.0))
    assert viewer._selection.has_selection is False
    # And repeating the selecting marquee is a no-op change...
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert len(viewer._selection.selected_ids) == 1


def test_marquee_never_mutates_document_or_history(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(250.0, 250.0, 400.0, 350.0))
    _seed(viewer, _pen_stroke((250.0, 500.0), (400.0, 500.0)))
    before = tuple(_store(viewer).page(0))
    count = len(_store(viewer))

    _marquee(viewer, (240.0, 240.0), (410.0, 510.0))
    assert len(viewer._selection.selected_ids) == 2

    # Selection is view-only: no annotation added/changed/removed, no
    # undo entry, and no stray pen/highlighter/shape preview.
    assert tuple(_store(viewer).page(0)) == before
    assert len(_store(viewer)) == count == 2
    assert viewer.document_history.can_undo() is False
    assert viewer._live_stroke is None
    assert getattr(viewer, "_live_highlighter", None) is None
    assert _store(viewer).page(0)[0] is shape


def test_gesture_on_empty_canvas_never_creates_annotations(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    count = len(_store(viewer))
    with _chrome_hidden(viewer):
        corners = [_widget(viewer, lx, ly) for lx, ly in
                   ((60.0, 600.0), (200.0, 660.0), (80.0, 690.0),
                    (40.0, 640.0))]
        _press(viewer, *corners[0])
        for wx, wy in corners[1:]:
            _move(viewer, wx, wy)
        _release(viewer, *corners[-1])
    assert viewer._selection.has_selection is False
    assert len(_store(viewer)) == count == 0
    assert viewer.document_history.can_undo() is False


# =====================================================================
# Viewer: Selection click selects each annotation kind + fragments
# =====================================================================

def test_click_selects_pen_and_highlighter(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    pen = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    hl = _seed(viewer, Highlighter(points=(Point(100.0, 300.0),
                                           Point(400.0, 300.0)),
                                   color="#FFFF00", width_mm=3.0))
    _click(viewer, 250.0, 150.0)
    assert _selected_value(viewer) == pen.id.value
    _click(viewer, 250.0, 300.0)
    assert _selected_value(viewer) == hl.id.value


def test_click_selects_each_shape_kind(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    rect = _seed(viewer, _rect_shape(100.0, 60.0, 200.0, 120.0))
    ellipse = _seed(viewer, _ellipse_shape(100.0, 200.0, 200.0, 260.0))
    line = _seed(viewer, _line_shape((100.0, 340.0), (200.0, 340.0)))
    arrow = _seed(viewer, _line_shape((100.0, 460.0), (200.0, 460.0),
                                      kind=ShapeKind.ARROW))
    polygon = _seed(viewer, _polygon_shape(
        [(100.0, 580.0), (200.0, 580.0), (150.0, 660.0)], close_path=True))

    _click(viewer, 150.0, 60.0)            # rectangle top edge
    assert _selected_value(viewer) == rect.id.value
    _click(viewer, 200.0, 230.0)           # ellipse rightmost point
    assert _selected_value(viewer) == ellipse.id.value
    _click(viewer, 150.0, 340.0)           # line midpoint
    assert _selected_value(viewer) == line.id.value
    _click(viewer, 150.0, 460.0)           # arrow shaft midpoint
    assert _selected_value(viewer) == arrow.id.value
    _click(viewer, 150.0, 580.0)           # polygon top edge
    assert _selected_value(viewer) == polygon.id.value


def test_click_selects_partial_erase_fragment(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, InkStroke(
        points=(Point(100.0, 150.0), Point(175.0, 150.0),
                Point(250.0, 150.0), Point(325.0, 150.0),
                Point(400.0, 150.0)),
        color="#e01b24", width_mm=1.0, opacity=1.0))
    frags = _partial_erase_fragments(viewer, stroke, 250.0)
    assert frags is not None and len(frags) >= 2
    # A click on each surviving fragment selects that fragment (the first
    # keeps the original identity).
    _click(viewer, 130.0, 150.0)
    assert _selected_value(viewer) == frags[0].id.value
    _click(viewer, 380.0, 150.0)
    assert _selected_value(viewer) == frags[-1].id.value


# =====================================================================
# Viewer: Pan tool double-click selection (single-click/drag unchanged)
# =====================================================================

def test_pan_single_click_does_not_select(qtbot, tmp_path,
                                          cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _pan_click(viewer, 250.0, 150.0)
    assert viewer._selection.has_selection is False
    assert stroke.id.value not in _selected_values(viewer)


def test_pan_drag_still_pans(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    before = viewer._scrollbar.value()
    # Drag upward: PanHandler maps the vertical delta to a positive scroll.
    _dx, dy = _pan_drag(viewer, 300.0, 300.0, 300.0, 260.0)
    assert int(-dy) > 0
    expected = max(viewer._scrollbar.minimum(),
                   min(viewer._scrollbar.maximum(), before + int(-dy)))
    assert viewer._scrollbar.value() == expected
    assert viewer._scrollbar.value() > before
    assert viewer._selection.has_selection is False


def test_pan_double_click_selects_and_shows_frame(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))

    _pan_double_click(viewer, 200.0, 150.0)   # top edge of the shape
    assert _selected_value(viewer) == shape.id.value
    assert viewer._selection_timer.isActive() is True

    _settle_frame(viewer)
    left, top, right, _bottom = _expected_group_rect(viewer, [(shape, 0)])
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 10


def test_pan_double_click_empty_no_effect(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    before_scroll = viewer._scrollbar.value()
    before_zoom = viewer._display_scale()
    _pan_double_click(viewer, 250.0, 450.0)   # empty canvas
    assert viewer._selection.has_selection is False
    assert viewer._scrollbar.value() == before_scroll   # no pan
    assert viewer._display_scale() == before_zoom        # no zoom


def test_pan_double_click_single_selection_operation(qtbot, tmp_path,
                                                     cleared_settings,
                                                     monkeypatch):
    import app.viewer as viewer_mod

    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    calls = []
    original = viewer_mod.PdfViewer._pan_double_click_select

    def counting(self, wx, wy):
        calls.append((wx, wy))
        return original(self, wx, wy)

    monkeypatch.setattr(viewer_mod.PdfViewer,
                        "_pan_double_click_select", counting)

    _pan_double_click(viewer, 200.0, 150.0)
    assert len(calls) == 1                    # exactly one selection op
    assert _selected_value(viewer) == shape.id.value


def test_pan_double_click_view_only(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    before = tuple(_store(viewer).page(0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value
    assert tuple(_store(viewer).page(0)) == before
    assert viewer.document_history.can_undo() is False
    assert viewer.document_history.can_redo() is False
    assert viewer._live_stroke is None


def test_pan_selection_clears_on_tool_switch(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert viewer._selection.has_selection is True
    # A Pan selection survives Pan; switching to an unrelated tool clears
    # it and stops the animation.
    viewer._on_tool_selected(_pen_tool())
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False


def test_pan_selection_clears_on_document_close(qtbot, tmp_path,
                                                cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert viewer._selection.has_selection is True
    assert viewer._selection_timer.isActive() is True
    viewer.close_document()
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False
    assert viewer._document is None


# ------------------------------------- Pan click vs. double-click (follow-up)

def test_pan_click_inside_frame_retains_selection(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value

    _pan_click(viewer, 200.0, 200.0)   # interior: inside the shared frame
    assert _selected_value(viewer) == shape.id.value
    assert viewer._selection_timer.isActive() is True


def test_pan_tool_switch_clears_multi_selection_before_click(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2: *any* real tool change clears the selection.

    Supersedes the earlier "a Pan selection survives the switch" rule: the
    selection belongs to the tool that made it, so switching Selection to
    Pan clears the ids, the frame, and the animation — and a following Pan
    click on the (now empty) whitespace selects nothing.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    left = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    right = _seed(viewer, _rect_shape(350.0, 150.0, 450.0, 250.0))
    _marquee(viewer, (120.0, 120.0), (480.0, 280.0))
    assert set(_selected_values(viewer)) == {left.id.value, right.id.value}

    viewer._on_tool_selected(_pan_tool())
    assert viewer._selection.has_selection is False
    assert viewer._selection.selected_ids == ()
    assert viewer._selection_handle_points() is None
    assert viewer._selection_timer.isActive() is False

    # The midpoint sits in the whitespace between the two boxes. With the
    # selection gone there is no shared frame and nothing to retain.
    _pan_click(viewer, 300.0, 200.0)
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False


def test_selection_click_inside_frame_gap_retains_multi_selection(
        qtbot, tmp_path, cleared_settings):
    """The Selection tool keeps a multi-selection on a click in the gap.

    The same gesture that used to be covered through Pan (before the
    Stage 5.2 tool-change policy) is asserted where it still applies: with
    Selection active, a sub-threshold click in the whitespace between the
    members is part of the frame, so it neither clears nor collapses it.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    left = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    right = _seed(viewer, _rect_shape(350.0, 150.0, 450.0, 250.0))
    _marquee(viewer, (120.0, 120.0), (480.0, 280.0))
    assert set(_selected_values(viewer)) == {left.id.value, right.id.value}

    probe = _widget(viewer, 300.0, 200.0)
    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        _release(viewer, *probe)
    assert set(_selected_values(viewer)) == {left.id.value, right.id.value}
    assert viewer._selection_timer.isActive() is True


def test_pan_click_outside_frame_clears_selection(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert viewer._selection.has_selection is True

    _pan_click(viewer, 250.0, 450.0)          # empty, outside the frame
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False


def test_pan_click_other_annotation_clears_without_selecting(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    second = _seed(viewer, _rect_shape(150.0, 400.0, 300.0, 520.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == first.id.value

    # A single click on another annotation (outside the frame) clears the
    # old selection and never selects the clicked annotation — Pan needs a
    # double-click to select.
    _pan_click(viewer, 200.0, 400.0)
    assert viewer._selection.has_selection is False
    assert second.id.value not in _selected_values(viewer)


def test_pan_double_click_empty_outside_clears(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    second = _seed(viewer, _rect_shape(150.0, 400.0, 300.0, 520.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == first.id.value
    before_scroll = viewer._scrollbar.value()
    before_zoom = viewer._display_scale()

    # Empty space outside the frame: clears the selection, no pan/zoom.
    _pan_double_click(viewer, 450.0, 600.0)
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False
    assert viewer._scrollbar.value() == before_scroll
    assert viewer._display_scale() == before_zoom
    assert second.id.value not in _selected_values(viewer)


def test_pan_tool_switch_clears_then_double_click_empty_selects_nothing(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2 tool-change policy, double-click variant.

    Switching Selection to Pan clears the multi-selection (no shared frame
    survives the switch), so a Pan double-click on the whitespace between
    the two boxes has nothing to retain and selects nothing.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    left = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    right = _seed(viewer, _rect_shape(350.0, 150.0, 450.0, 250.0))
    _marquee(viewer, (120.0, 120.0), (480.0, 280.0))
    assert set(_selected_values(viewer)) == {left.id.value, right.id.value}

    viewer._on_tool_selected(_pan_tool())
    assert viewer._selection.has_selection is False
    _pan_double_click(viewer, 300.0, 200.0)
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False


def test_pan_double_click_different_annotation_replaces(qtbot, tmp_path,
                                                        cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    second = _seed(viewer, _rect_shape(150.0, 400.0, 300.0, 520.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == first.id.value

    set_calls = []
    clear_calls = []
    orig_set = viewer._selection.set
    orig_clear = viewer._selection.clear

    def rec_set(ids):
        set_calls.append(tuple(ids))
        return orig_set(ids)

    def rec_clear():
        clear_calls.append(True)
        return orig_clear()

    viewer._selection.set = rec_set
    viewer._selection.clear = rec_clear

    _pan_double_click(viewer, 200.0, 400.0)
    assert _selected_value(viewer) == second.id.value
    assert first.id.value not in _selected_values(viewer)
    # M6.8.2: the first tap's press (outside the previous frame) now clears
    # immediately — that is expected and allowed — but the double-click still
    # completes the new selection in exactly one replace operation.
    assert clear_calls == [True]
    assert len(set_calls) == 1
    assert set_calls[0] == (second.id,)


def test_pan_double_click_current_selection_no_duplicate_ops(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value

    set_calls = []
    clear_calls = []
    orig_set = viewer._selection.set
    orig_clear = viewer._selection.clear

    def rec_set(ids):
        set_calls.append(tuple(ids))
        return orig_set(ids)

    def rec_clear():
        clear_calls.append(True)
        return orig_clear()

    viewer._selection.set = rec_set
    viewer._selection.clear = rec_clear

    _pan_double_click(viewer, 200.0, 150.0)   # same annotation again
    assert viewer._selection.has_selection is True
    assert _selected_value(viewer) == shape.id.value
    assert clear_calls == []                  # first click never cleared
    assert len(set_calls) == 1                # exactly one replace op


def test_pan_drag_inside_frame_moves_the_group(qtbot, tmp_path,
                                               cleared_settings):
    """Stage 5.2.1: the frame interior moves the group instead of panning.

    Supersedes the M6.8.1 expectation that a Pan drag starting inside the
    selection frame deselects and pans the viewport: the whole oriented
    frame is now a movement surface, so the selection survives and the
    members follow the pointer.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 400.0, 400.0))
    _pan_double_click(viewer, 250.0, 150.0)
    assert _selected_value(viewer) == shape.id.value
    factor = (float(viewer._pipeline.layout.raster_scale)
              * viewer._display_scale())

    # Press inside the frame, then move past the drag threshold: the shared
    # group move is armed (never a pan) and the selection stays selected.
    x0, y0 = _widget(viewer, 300.0, 320.0)
    x1, y1 = _widget(viewer, 300.0, 280.0)
    before = viewer._scrollbar.value()
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        _move(viewer, x1, y1)
        assert viewer._selection.has_selection is True
        assert viewer._selection_timer.isActive() is True
        _release(viewer, x1, y1)
    moved = _store(viewer).get(shape.id)
    assert moved is not shape
    assert moved.rect.origin.y == pytest.approx(
        shape.rect.origin.y + (y1 - y0) / factor, abs=1.0)
    assert viewer._selection.has_selection is True
    assert viewer._scrollbar.value() == before      # never panned


def test_pan_drag_outside_frame_clears_selection(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value

    x0, y0 = _widget(viewer, 300.0, 500.0)   # starts well outside the frame
    x1, y1 = _widget(viewer, 300.0, 460.0)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, x1, y1)
        assert viewer._selection.has_selection is False   # cleared mid-drag
        assert viewer._selection_timer.isActive() is False
        _release(viewer, x1, y1)
    assert viewer._selection.has_selection is False


def test_pan_drag_from_frame_to_outside_keeps_moving_the_group(
        qtbot, tmp_path, cleared_settings):
    """A move armed inside the frame owns the pointer past the frame edge.

    Stage 5.2.1: the gesture never falls through to viewport panning and the
    selection is never cleared mid-drag, wherever the pointer travels.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value
    factor = (float(viewer._pipeline.layout.raster_scale)
              * viewer._display_scale())

    # Begin inside the frame and end well outside it.
    x0, y0 = _widget(viewer, 200.0, 200.0)
    x1, y1 = _widget(viewer, 480.0, 440.0)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, x1, y1)
        assert viewer._selection.has_selection is True   # never cleared
        assert (viewer._move_gesture or {}).get("mode") == "move"
        _release(viewer, x1, y1)
    moved = _store(viewer).get(shape.id)
    assert moved is not shape
    assert moved.rect.origin.x == pytest.approx(
        shape.rect.origin.x + (x1 - x0) / factor, abs=1.0)
    assert viewer._selection.has_selection is True
    assert viewer._pan_press_point is None
    assert viewer._pan_last_tap is None
    assert not hasattr(viewer, "_pan_click_timer")   # timer removed entirely


def test_pan_scroll_continues_after_deselect(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == shape.id.value
    before = viewer._scrollbar.value()

    # A real pan both scrolls (unchanged behaviour) and deselects.
    _dx, dy = _pan_drag(viewer, 300.0, 320.0, 300.0, 280.0)
    assert viewer._selection.has_selection is False
    expected = max(viewer._scrollbar.minimum(),
                   min(viewer._scrollbar.maximum(), before + int(-dy)))
    assert viewer._scrollbar.value() == expected
    assert viewer._scrollbar.value() > before


def test_pan_press_outside_frame_clears_before_release(qtbot, tmp_path,
                                                       cleared_settings):
    """#3: clearing is immediate on press — never after a double-click wait."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert viewer._selection.has_selection is True
    assert viewer._selection_timer.isActive() is True

    x0, y0 = _widget(viewer, 250.0, 450.0)     # empty, outside the frame
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        # Cleared on press, before the release and with no deferred timer.
        assert viewer._selection.has_selection is False
        assert viewer._selection_timer.isActive() is False
        _release(viewer, x0, y0)
    assert viewer._selection.has_selection is False


def test_pan_interior_subthreshold_click_keeps_the_selection(
        qtbot, tmp_path, cleared_settings):
    """A sub-threshold Pan press inside the frame keeps the selection."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 400.0, 400.0))
    _pan_double_click(viewer, 250.0, 150.0)
    assert viewer._selection.has_selection is True
    before = viewer._scrollbar.value()

    x0, y0 = _widget(viewer, 300.0, 320.0)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)                 # inside the frame
        _move(viewer, x0 + 1.0, y0 + 1.0)      # sub-threshold jitter
        _release(viewer, x0 + 1.0, y0 + 1.0)
    assert viewer._selection.has_selection is True
    assert viewer._selection_timer.isActive() is True
    assert viewer._scrollbar.value() == before          # never panned
    assert _store(viewer).get(shape.id) == shape        # never moved


def test_pan_first_tap_clears_then_double_click_selects(
        qtbot, tmp_path, cleared_settings):
    """#9: the first tap may clear; the double-click still selects its target."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    second = _seed(viewer, _rect_shape(150.0, 400.0, 300.0, 520.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == first.id.value

    # The first press of the double-click lands on the other annotation,
    # outside the shared frame, so it clears the old selection immediately...
    wx, wy = _widget(viewer, 200.0, 400.0)
    with _chrome_hidden(viewer):
        _press(viewer, wx, wy)
        assert viewer._selection.has_selection is False
        _release(viewer, wx, wy)
    # ...and the completed double-click still selects the new target.
    _dblclick(viewer, wx, wy)
    assert _selected_value(viewer) == second.id.value
    assert first.id.value not in _selected_values(viewer)


def test_pan_no_pending_timer_never_overwrites_new_selection(
        qtbot, tmp_path, cleared_settings):
    """#10: there is no deferred callback left to clear a fresh selection."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    # The deferred-timer machinery is gone entirely.
    assert not hasattr(viewer, "_pan_click_timer")
    assert not hasattr(viewer, "_pan_click_point")
    assert not hasattr(viewer, "_commit_pan_click")

    _pan_click(viewer, 250.0, 450.0)          # outside: clears immediately
    assert viewer._selection.has_selection is False
    _pan_double_click(viewer, 200.0, 150.0)   # fresh selection
    assert _selected_value(viewer) == shape.id.value
    # No stale callback may fire and clear/overwrite the fresh selection.
    qtbot.wait(150)
    assert _selected_value(viewer) == shape.id.value


def test_pan_click_animation_stops_and_double_click_restarts(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_double_click(viewer, 200.0, 150.0)
    assert viewer._selection.has_selection is True
    assert viewer._selection_timer.isActive() is True

    _pan_click(viewer, 250.0, 450.0)          # outside: clears on press
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False

    _pan_double_click(viewer, 200.0, 150.0)   # valid double-click select
    assert viewer._selection.has_selection is True
    assert viewer._selection_timer.isActive() is True


def test_pan_click_without_selection_is_noop(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _pan_click(viewer, 250.0, 450.0)
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False


def test_pan_click_sequences_never_mutate_store_or_history(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    second = _seed(viewer, _rect_shape(350.0, 150.0, 450.0, 250.0))
    before_page0 = tuple(_store(viewer).page(0))
    before_undo = viewer.document_history.can_undo()
    before_redo = viewer.document_history.can_redo()

    _pan_double_click(viewer, 200.0, 150.0)   # select first
    assert _selected_value(viewer) == first.id.value
    _pan_click(viewer, 250.0, 450.0)          # empty: clears immediately
    assert viewer._selection.has_selection is False
    _pan_double_click(viewer, 400.0, 150.0)   # select second
    assert _selected_value(viewer) == second.id.value

    assert tuple(_store(viewer).page(0)) == before_page0
    assert viewer.document_history.can_undo() == before_undo
    assert viewer.document_history.can_redo() == before_redo


# ------------------------------------- Pan stylus (tablet) double-tap (M6.8.2)

def test_pan_stylus_double_tap_selects_annotation(qtbot, tmp_path,
                                                  cleared_settings):
    """#4: two stationary stylus taps on an annotation select it."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _tablet_double_tap(viewer, 200.0, 150.0)   # on the shape's top edge
    assert _selected_value(viewer) == shape.id.value
    assert viewer._selection_timer.isActive() is True


def test_pan_stylus_single_tap_does_not_select(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    wx, wy = _widget(viewer, 250.0, 150.0)
    _tablet_tap(viewer, wx, wy)
    assert viewer._selection.has_selection is False


def test_pan_stylus_double_tap_replaces_selection(qtbot, tmp_path,
                                                  cleared_settings):
    """#5: a double-tap on another annotation replaces the selection."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    first = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    second = _seed(viewer, _rect_shape(150.0, 400.0, 300.0, 520.0))
    _tablet_double_tap(viewer, 200.0, 150.0)
    assert _selected_value(viewer) == first.id.value

    _tablet_double_tap(viewer, 200.0, 400.0)
    assert _selected_value(viewer) == second.id.value
    assert first.id.value not in _selected_values(viewer)
    assert viewer._selection_timer.isActive() is True


def test_pan_stylus_double_tap_empty_never_selects(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _tablet_double_tap(viewer, 250.0, 450.0)   # empty canvas
    assert viewer._selection.has_selection is False


def test_pan_stylus_double_tap_no_duplicate_with_synth_mouse(
        qtbot, tmp_path, cleared_settings, monkeypatch):
    """#6: the synthesized mouse fallback must not double-process the tap."""
    import app.viewer as viewer_mod

    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    wx, wy = _widget(viewer, 200.0, 150.0)

    calls = []
    original = viewer_mod.PdfViewer._pan_double_click_select

    def counting(self, x, y):
        calls.append((x, y))
        return original(self, x, y)

    monkeypatch.setattr(viewer_mod.PdfViewer,
                        "_pan_double_click_select", counting)

    # A stylus double-tap interleaved with every mouse event Qt may
    # synthesize for each tablet event (press, release, and a defensive
    # mouse double-click) must run the selection exactly once.
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, wx, wy)
        _press(viewer, wx, wy)                # synthesized
        _tablet(viewer, QEvent.Type.TabletRelease, wx, wy)
        _release(viewer, wx, wy)              # synthesized
        _tablet(viewer, QEvent.Type.TabletPress, wx, wy)
        _press(viewer, wx, wy)                # synthesized
        _dblclick(viewer, wx, wy)             # synthesized (defensive)
        _tablet(viewer, QEvent.Type.TabletRelease, wx, wy)
        _release(viewer, wx, wy)              # synthesized

    assert len(calls) == 1
    assert _selected_value(viewer) == shape.id.value


def test_selection_stylus_click_matches_mouse(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    wx, wy = _widget(viewer, 250.0, 150.0)
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, wx, wy)
        _tablet(viewer, QEvent.Type.TabletRelease, wx, wy)
    assert _selected_value(viewer) == stroke.id.value


# =====================================================================
# Viewer: lasso routing
# =====================================================================

def test_lasso_selects_enclosed_and_excludes_touching(qtbot, tmp_path,
                                                      cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    enclosed = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    outside = _seed(viewer, _rect_shape(400.0, 400.0, 500.0, 500.0))

    _lasso(viewer, [(100.0, 100.0), (300.0, 100.0), (300.0, 300.0),
                    (100.0, 300.0)])
    assert _selected_values(viewer) == (enclosed.id.value,)
    assert outside.id.value not in _selected_values(viewer)
    assert window.viewer is viewer


def test_lasso_selects_boundary_crossing(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0),
                                       width_mm=0.4))
    # The loop crosses the stroke's lane without enclosing it.
    _lasso(viewer, [(240.0, 100.0), (290.0, 60.0), (320.0, 180.0),
                    (250.0, 160.0)])
    assert _selected_values(viewer) == (stroke.id.value,)


def test_lasso_empty_gesture_clears(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _lasso(viewer, [(100.0, 100.0), (300.0, 100.0), (300.0, 200.0),
                    (100.0, 200.0)])
    assert viewer._selection.has_selection is True
    # An empty (disjoint) lasso clears.
    _lasso(viewer, [(60.0, 640.0), (200.0, 640.0), (160.0, 700.0)])
    assert viewer._selection.has_selection is False


def test_lasso_click_is_empty_gesture(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    # A click (sub-threshold) on ink does NOT select for the Lasso: it
    # stays an empty gesture that clears.
    _click(viewer, 250.0, 150.0)
    assert viewer._selection.has_selection is False
    assert stroke.id.value not in _selected_values(viewer)
    # And a sub-threshold stylus tap behaves identically.
    wx, wy = _widget(viewer, 250.0, 150.0)
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, wx, wy)
        _tablet(viewer, QEvent.Type.TabletRelease, wx, wy)
    assert viewer._selection.has_selection is False


def test_click_selection_view_only(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(250.0, 250.0, 400.0, 350.0))
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    before = tuple(_store(viewer).page(0))
    before_geometry = tuple(
        (getattr(a, "rect", None), tuple(
            (p.x, p.y) for p in (getattr(a, "points", ()) or ())))
        for a in _store(viewer).page(0))

    _click(viewer, 325.0, 250.0)     # shape top edge
    assert _selected_value(viewer) == shape.id.value
    _click(viewer, 250.0, 150.0)     # pen stroke
    assert _selected_value(viewer) == stroke.id.value

    assert tuple(_store(viewer).page(0)) == before
    assert tuple(
        (getattr(a, "rect", None), tuple(
            (p.x, p.y) for p in (getattr(a, "points", ()) or ())))
        for a in _store(viewer).page(0)) == before_geometry
    assert viewer.document_history.can_undo() is False
    assert viewer.document_history.can_redo() is False
    assert len(_store(viewer)) == 2


def test_lasso_does_not_create_pen_annotation(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    before = tuple(_store(viewer).page(0))
    _lasso(viewer, [(60.0, 60.0), (300.0, 80.0), (280.0, 200.0),
                    (80.0, 180.0)])
    assert tuple(_store(viewer).page(0)) == before
    assert viewer._live_stroke is None
    assert getattr(viewer, "_live_highlighter", None) is None


# =====================================================================
# Viewer: live overlay during the drag
# =====================================================================

def test_marquee_overlay_visible_mid_drag(qtbot, tmp_path,
                                          cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    (x0, y0), (x1, y1) = _page_widget_corners(
        viewer, 150.0, 150.0, 450.0, 350.0)
    with _chrome_hidden(viewer):
        _press(viewer, x0, y0)
        _move(viewer, (x0 + x1) / 2, (y0 + y1) / 2)
        image = viewer.grab().toImage()
        # Accent present along the top and left edges of the marquee.
        assert _max_accent_row(image, y0, x0, (x0 + x1) / 2) >= 8
        assert _max_accent_col(image, x0, y0, (y0 + y1) / 2 + 10) >= 6

        _move(viewer, x1, y1)
        image = viewer.grab().toImage()
        assert _max_accent_row(image, y1, (x0 + x1) / 2, x1) >= 8
        _release(viewer, x1, y1)
    # After the commit the transient overlay is gone (frames, not a
    # gesture outline, remain — asserted elsewhere).
    assert viewer._region_gesture is None


def test_lasso_overlay_visible_mid_drag(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    corners = [_widget(viewer, lx, ly) for lx, ly in
               ((100.0, 150.0), (300.0, 150.0), (300.0, 350.0),
                (100.0, 350.0))]
    with _chrome_hidden(viewer):
        _press(viewer, *corners[0])
        _move(viewer, *corners[1])
        image = viewer.grab().toImage()
        # Accent along the drawn top segment.
        assert _max_accent_row(image, corners[1][1],
                               corners[0][0], corners[1][0]) >= 8
        _move(viewer, *corners[2])
        image = viewer.grab().toImage()
        # Accent along the right segment.
        assert _max_accent_col(image, corners[2][0],
                               corners[1][1], corners[2][1]) >= 6
        _release(viewer, *corners[3])
    assert viewer._region_gesture is None


def test_marquee_no_overlay_when_released(qtbot, tmp_path,
                                          cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert viewer._region_gesture is None
    # The committed marquee over the empty strip clears; nothing
    # repaints the overlay afterwards.
    _marquee(viewer, (50.0, 640.0), (450.0, 700.0))
    assert viewer._region_gesture is None
    assert viewer._selection.has_selection is False


# =====================================================================
# Viewer: one shared selection frame (the union of the whole selection)
# =====================================================================

def test_single_selection_draws_exactly_one_frame(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert _selected_values(viewer) == (shape.id.value,)

    _settle_frame(viewer)
    expected = _expected_group_rect(viewer, [(shape, 0)])
    # Selecting one annotation still yields exactly one frame — and that
    # frame is the annotation's own padded box.
    assert _group_frame_widget_rect(viewer) == pytest.approx(expected)
    left, top, right, bottom = expected
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 10
    assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) >= 6
    # A frame, not a fill: the interior stays empty.
    mid_y = (top + bottom) / 2.0
    assert _max_accent_row(image, mid_y, left + 20.0, right - 20.0) == 0


def test_many_selection_draws_one_shared_frame(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    a = _seed(viewer, _rect_shape(100.0, 100.0, 240.0, 200.0))
    b = _seed(viewer, _rect_shape(340.0, 340.0, 480.0, 460.0))
    _marquee(viewer, (60.0, 60.0), (540.0, 500.0))
    assert _selected_values(viewer) == tuple(sorted(
        (a.id.value, b.id.value)))

    _settle_frame(viewer)
    expected = _expected_group_rect(viewer, [(a, 0), (b, 0)])
    # Exactly one frame, equal to the union — not the first annotation's.
    assert _group_frame_widget_rect(viewer) == pytest.approx(expected)
    left, top, right, bottom = expected
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 10
    assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) >= 6
    # No per-annotation frame: ``a``'s own interior right/bottom edges
    # (which lie strictly inside the shared box) stay unmarked.
    a_left, a_top, a_right, a_bottom = _expected_group_rect(
        viewer, [(a, 0)])
    assert _max_accent_col(image, a_right - _FRAME_PAD,
                           a_top + 6.0, a_bottom - 6.0) == 0
    assert _max_accent_row(image, a_bottom - _FRAME_PAD,
                           a_left + 6.0, a_right - 6.0) == 0


def test_shared_frame_encloses_mixed_selection_union(qtbot, tmp_path,
                                                     cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((120.0, 120.0), (200.0, 120.0)))
    highlighter = _seed(viewer, Highlighter(
        points=(Point(120.0, 180.0), Point(200.0, 180.0)),
        color="#FFFF00", width_mm=3.0, opacity=0.4))
    shape = _seed(viewer, _rect_shape(360.0, 260.0, 460.0, 360.0))
    _marquee(viewer, (60.0, 60.0), (520.0, 420.0))
    assert _selected_values(viewer) == tuple(sorted(
        (stroke.id.value, highlighter.id.value, shape.id.value)))

    _settle_frame(viewer)
    expected = _expected_group_rect(
        viewer, [(stroke, 0), (highlighter, 0), (shape, 0)])
    assert _group_frame_widget_rect(viewer) == pytest.approx(expected)
    left, top, right, bottom = expected
    # The shared frame contains every selected extent (the union).
    for annotation in (stroke, highlighter, shape):
        x0, y0, x1, y1 = _extent_widget_rect(viewer, annotation, 0)
        assert left <= x0 and top <= y0
        assert right >= x1 and bottom >= y1


def test_frames_cleared_after_empty_marquee(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    _settle_frame(viewer)
    left, top, right, _bottom = _expected_group_rect(viewer, [(shape, 0)])
    assert _max_accent_row(viewer.grab().toImage(), top,
                           left + 6.0, right - 6.0) >= 10
    _marquee(viewer, (420.0, 560.0), (560.0, 680.0))   # empty area
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) == 0
    assert _max_accent_col(image, left, top + 6.0,
                           _bottom - 6.0) == 0


def test_shared_frame_tracks_zoom_and_scroll(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert viewer._selection.has_selection is True

    for zoom in (1.0, 1.5, 2.0):
        viewer.set_zoom(zoom)
        qtbot.wait(40)
        _settle_frame(viewer)
        expected = _expected_group_rect(viewer, [(shape, 0)])
        assert _group_frame_widget_rect(viewer) == pytest.approx(expected)
        left, top, right, bottom = expected
        image = viewer.grab().toImage()
        assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 8
        assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) >= 5

    # Scrolling keeps the frame glued to the shape (both axes resync).
    viewer.set_zoom(1.0)
    qtbot.wait(40)
    viewer._scrollbar.setValue(min(60, viewer._scrollbar.maximum()))
    viewer._hscrollbar.setValue(min(40, viewer._hscrollbar.maximum()))
    qtbot.wait(40)
    _settle_frame(viewer)
    expected = _expected_group_rect(viewer, [(shape, 0)])
    assert _group_frame_widget_rect(viewer) == pytest.approx(expected)
    left, top, right, bottom = expected
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 8
    assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) >= 5


def test_multi_selection_frame_only_for_selection_tool(qtbot, tmp_path,
                                                       cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert viewer._selection.has_selection is True
    _settle_frame(viewer)
    left, top, right, _bottom = _expected_group_rect(viewer, [(shape, 0)])
    assert _max_accent_row(viewer.grab().toImage(), top,
                           left + 6.0, right - 6.0) >= 10

    viewer._on_tool_selected(_pen_tool())
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) == 0
    # And leaving the tool cleared the selection (existing policy).
    assert viewer._selection.has_selection is False


# =====================================================================
# Viewer: cross-page shared frame in viewer coordinates
# =====================================================================

def test_shared_frame_spans_pages_in_viewer_coordinates(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(
        qtbot, tmp_path, tool=_selection_tool(),
        sizes=[(600.0, 120.0), (600.0, 120.0)])
    page0 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=0)
    page1 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=1)
    anchor = _widget(viewer, 80.0, 5.0, 0)
    end = _widget(viewer, 440.0, 45.0, 1)
    _marquee_widget(viewer, anchor, end)
    assert _selected_values(viewer) == tuple(sorted(
        (page0.id.value, page1.id.value)))

    _settle_frame(viewer)
    expected = _expected_group_rect(viewer, [(page0, 0), (page1, 1)])
    got = _group_frame_widget_rect(viewer)
    # ONE frame in viewer coordinates, not one per page.
    assert got == pytest.approx(expected)
    top_extent = _extent_widget_rect(viewer, page0, 0)
    bottom_extent = _extent_widget_rect(viewer, page1, 1)
    assert got[0] <= top_extent[0] and got[2] >= top_extent[2]
    assert got[1] <= top_extent[1]
    assert got[3] >= bottom_extent[3]
    left, top, right, bottom = expected
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) >= 8
    assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) >= 6


# =====================================================================
# Viewer: shared-frame animation (deterministic clock, no long sleeps)
# =====================================================================

def test_frame_animation_dash_phase_advances(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert _selected_values(viewer) == (shape.id.value,)

    # The animation is armed (running) whenever a selection exists.
    assert viewer._selection_timer.isActive() is True

    viewer._selection_timer.stop()
    viewer._selection_phase = 0.0
    phases = []
    offsets = []
    for _ in range(8):
        phases.append(viewer._selection_phase)
        offsets.append(viewer._selection_frame_pen().dashOffset())
        viewer._advance_selection_animation()      # one deterministic tick

    assert phases[-1] > phases[0]                  # clock advances
    assert len(set(round(o, 6) for o in offsets)) > 1   # ants march
    cycle = viewer_module._SELECT_DASH_CYCLE
    assert all(0.0 <= o < cycle for o in offsets)  # wrapped, bounded


def test_frame_pulse_bounded_and_visible(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    viewer._selection_timer.stop()

    period = viewer_module._SELECT_PULSE_PERIOD_TICKS
    pulses = []
    for tick in range(2 * period):
        viewer._selection_phase = float(tick)
        pulses.append(viewer._selection_pulse())

    # Bounded opacity: never fully transparent, never over-bright.
    assert all(viewer_module._SELECT_PULSE_MIN - 1e-9 <= p
               <= viewer_module._SELECT_PULSE_MAX + 1e-9 for p in pulses)
    assert min(pulses) > 0.0
    for p in pulses:
        alpha = int(round(255 * p))
        assert 0 < alpha <= 255
    # It actually pulses (visible), but slowly: two full periods contain
    # exactly two maxima — no rapid flashing.
    assert max(pulses) - min(pulses) > 0.1
    maxima = 0
    for i in range(1, len(pulses) - 1):
        if pulses[i] >= pulses[i - 1] and pulses[i] > pulses[i + 1]:
            maxima += 1
    assert maxima == 2


def test_clear_stops_animation_and_removes_frame(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert viewer._selection_timer.isActive() is True
    _settle_frame(viewer)
    left, top, right, bottom = _expected_group_rect(viewer, [(shape, 0)])

    _marquee(viewer, (420.0, 560.0), (560.0, 680.0))   # empty -> clears
    assert viewer._selection.has_selection is False
    assert viewer._selection_timer.isActive() is False
    image = viewer.grab().toImage()
    assert _max_accent_row(image, top, left + 6.0, right - 6.0) == 0
    assert _max_accent_col(image, left, top + 6.0, bottom - 6.0) == 0


def test_close_document_stops_frame_animation(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert viewer._selection_timer.isActive() is True

    viewer.close_document()
    assert viewer._selection_timer.isActive() is False
    assert viewer._selection.has_selection is False
    assert viewer._should_animate_selection() is False


def test_viewer_destruction_owns_the_animation_timer(qtbot, tmp_path,
                                                     cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    _marquee(viewer, (100.0, 100.0), (340.0, 320.0))
    assert viewer._selection_timer.isActive() is True

    # Parented to the viewer: Qt destroys the timer with it, so the timer
    # and its callback can never outlive the widget.
    assert viewer._selection_timer.parent() is viewer
    # Closing the viewer dispatches closeEvent, which stops the animation
    # and releases the document before teardown.
    viewer.close()
    qtbot.wait(30)
    assert viewer._selection_timer.isActive() is False


def test_animation_never_touches_store_geometry_or_history(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    shape = _push_add(viewer, _rect_shape(150.0, 150.0, 300.0, 280.0))
    stroke = _push_add(viewer, _pen_stroke((120.0, 400.0), (360.0, 400.0)))
    _marquee(viewer, (80.0, 80.0), (420.0, 460.0))
    assert _selected_values(viewer) == tuple(sorted(
        (shape.id.value, stroke.id.value)))

    store = _store(viewer)
    before_geometry = tuple(
        (getattr(a, "rect", None), tuple(
            (p.x, p.y) for p in (getattr(a, "points", ()) or ())))
        for a in store.page(0))
    before_ids = tuple(a.id.value for a in store.page(0))
    before_undo = viewer.document_history.can_undo()
    before_redo = viewer.document_history.can_redo()

    # Advance the animation a full two periods; nothing may change.
    for _ in range(2 * viewer_module._SELECT_PULSE_PERIOD_TICKS):
        viewer._advance_selection_animation()

    after_geometry = tuple(
        (getattr(a, "rect", None), tuple(
            (p.x, p.y) for p in (getattr(a, "points", ()) or ())))
        for a in store.page(0))
    assert tuple(a.id.value for a in store.page(0)) == before_ids
    assert after_geometry == before_geometry
    assert viewer.document_history.can_undo() == before_undo
    assert viewer.document_history.can_redo() == before_redo



# =====================================================================
# Viewer: cross-page gestures, zoom/scroll, rotated pages
# =====================================================================

def test_marquee_spans_pages_and_sizes(qtbot, tmp_path, cleared_settings):
    # Two short pages so both are substantially visible in the 900x700
    # window at any raster bucket the pipeline picks (page 0 from the
    # top, page 1's head strip below it).
    _window, viewer = _open_window(
        qtbot, tmp_path, tool=_selection_tool(),
        sizes=[(600.0, 120.0), (600.0, 120.0)])
    page0 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=0)
    page1 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=1)

    # A marquee starting on page 0 and ending inside page 1's visible
    # bounds: each page's displayed bounds are clipped and converted to
    # that page's coordinates before its annotations are tested.
    anchor = _widget(viewer, 80.0, 5.0, 0)
    end = _widget(viewer, 440.0, 45.0, 1)
    assert end[1] > anchor[1]              # page 1 sits below page 0
    _marquee_widget(viewer, anchor, end)
    assert _selected_values(viewer) == tuple(sorted(
        (page0.id.value, page1.id.value)))

    # A marquee confined to page 0's displayed bounds leaves page 1's
    # annotation out (page-1 ink sits below the marquee in widget y).
    _marquee(viewer, (80.0, 5.0), (440.0, 60.0))
    assert _selected_values(viewer) == (page0.id.value,)


def test_lasso_spans_pages(qtbot, tmp_path, cleared_settings):
    # Same short-page corpus: one full page visible plus page 1's head.
    _window, viewer = _open_window(
        qtbot, tmp_path, tool=_lasso_tool(),
        sizes=[(600.0, 120.0), (600.0, 120.0)])
    page0 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=0)
    page1 = _seed(viewer, _pen_stroke((100.0, 20.0), (400.0, 20.0)),
                  page_index=1)

    # A loop over page 0's ink reaching into page 1's visible bounds
    # (a simple widget-space quad: two corners on page 0, two on page 1).
    fr0, top0 = _widget(viewer, 80.0, 5.0, 0)
    fr1, _ty = _widget(viewer, 440.0, 5.0, 0)
    _fx, fy1 = _widget(viewer, 440.0, 45.0, 1)
    _gx, gy1 = _widget(viewer, 80.0, 45.0, 1)
    _lasso_widget(viewer, [(fr0, top0), (fr1, top0),
                           (_fx, fy1), (_gx, gy1)])
    assert _selected_values(viewer) == tuple(sorted(
        (page0.id.value, page1.id.value)))

    # A loop confined to page 0 selects only page 0's annotation
    # (page 1's ink sits below the loop's page-0 span in widget y).
    fr0, top0 = _widget(viewer, 80.0, 5.0, 0)
    fr1, bot0 = _widget(viewer, 440.0, 60.0, 0)
    _lasso_widget(viewer, [(fr0, top0), (fr1, top0),
                           (fr1, bot0), (fr0, bot0)])
    assert _selected_values(viewer) == (page0.id.value,)


def test_rotated_page_marquee(qtbot, tmp_path, cleared_settings):
    pdf_path = make_intrinsic_pdf(str(tmp_path / "rotated.pdf"),
                                  rotations=(90,))
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool(),
                                   pdf=pdf_path, name="rotated.pdf")
    # Page 0 carries /Rotate 90: its displayed size is 800x600 (the
    # rotated render frame) and stored annotation geometry lives in
    # exactly that rotated page-local frame.
    width_pt = (float(viewer._pipeline.layout.entry(0).render_width_px)
                / float(viewer._pipeline.layout.raster_scale))
    height_pt = (float(viewer._pipeline.layout.entry(0).render_height_px)
                 / float(viewer._pipeline.layout.raster_scale))
    assert (round(width_pt), round(height_pt)) == (800, 600)
    stroke = _seed(viewer, _pen_stroke((250.0, 300.0), (450.0, 300.0),
                                       width_mm=0.8))
    # A marquee drawn over the stroke's page-local span selects it.
    _marquee(viewer, (200.0, 250.0), (500.0, 350.0))
    assert viewer._selection.has_selection is True
    assert _selected_values(viewer) == (stroke.id.value,)

    # And a marquee that misses it does not select anything.
    _marquee(viewer, (20.0, 20.0), (90.0, 90.0))
    assert viewer._selection.has_selection is False


# =====================================================================
# Viewer: state hygiene (undo/redo/erase, tool switches, close)
# =====================================================================

def test_undo_removal_clears_ids(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _push_add(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    assert viewer.document_history.can_undo() is True
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert _selected_values(viewer) == (stroke.id.value,)
    viewer._on_undo_requested()
    assert len(_store(viewer)) == 0
    assert viewer._selection.has_selection is False


def test_store_removal_self_heals_via_revalidate(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    keep = _seed(viewer, _pen_stroke((100.0, 400.0), (400.0, 400.0)))
    _marquee(viewer, (50.0, 100.0), (450.0, 450.0))
    assert len(viewer._selection.selected_ids) == 2

    # The eraser path removes annotations directly from the store (no
    # history entry); the selection must self-heal on revalidation.
    _store(viewer).remove(stroke.id)
    assert viewer._revalidate_selection() is True
    assert _selected_values(viewer) == (keep.id.value,)
    _store(viewer).remove(keep.id)
    assert viewer._revalidate_selection() is True
    assert viewer._selection.has_selection is False


def test_tool_switch_clears_selection(qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert viewer._selection.has_selection is True
    viewer._on_tool_selected(_pen_tool())
    assert viewer._selection.has_selection is False
    assert window.viewer is viewer


def test_lasso_tool_switch_clears_selection(qtbot, tmp_path,
                                            cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _lasso(viewer, [(80.0, 100.0), (300.0, 100.0), (300.0, 220.0),
                    (80.0, 220.0)])
    assert viewer._selection.has_selection is True
    viewer._on_tool_selected(_pen_tool())
    assert viewer._selection.has_selection is False
    assert window.viewer is viewer


def test_mid_gesture_tool_switch_abandons_gesture(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    with _chrome_hidden(viewer):
        (x0, y0), (x1, y1) = _page_widget_corners(
            viewer, 50.0, 100.0, 450.0, 200.0)
        _press(viewer, x0, y0)
        _move(viewer, (x0 + x1) / 2, (y0 + y1) / 2)
        assert viewer._region_gesture is not None
        viewer._on_tool_selected(_pen_tool())
        assert viewer._region_gesture is None
        _release(viewer, x1, y1)   # must not commit anything now
    assert viewer._selection.has_selection is False
    assert viewer._interaction.active_kind == ToolType.PEN


def test_close_document_clears_selection_and_gesture(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    _marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert viewer._selection.has_selection is True
    viewer.close_document()
    assert viewer._selection.has_selection is False
    assert viewer._document is None
    assert viewer._region_gesture is None


# =====================================================================
# Viewer: tablet/mouse parity (no duplicate gesture processing)
# =====================================================================

def _count_region_commits(monkeypatch):
    import app.viewer as viewer_mod

    calls = []
    original = viewer_mod.PdfViewer._region_commit

    def counting(self, region):
        calls.append(1)
        return original(self, region)

    monkeypatch.setattr(viewer_mod.PdfViewer, "_region_commit", counting)
    return calls


def test_tablet_marquee_matches_mouse(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    before = tuple(_store(viewer).page(0))

    _tablet_marquee(viewer, (50.0, 100.0), (450.0, 200.0))
    assert viewer._selection.has_selection is True
    assert _selected_value(viewer) == stroke.id.value
    assert viewer._region_gesture is None
    assert tuple(_store(viewer).page(0)) == before   # store untouched
    assert viewer.document_history.can_undo() is False  # no undo entry


def test_tablet_gesture_processes_exactly_once(qtbot, tmp_path,
                                               cleared_settings,
                                               monkeypatch):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_selection_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    calls = _count_region_commits(monkeypatch)
    before = tuple(_store(viewer).page(0))

    # One physical stylus drag followed by the synthetic mouse events
    # Qt may synthesize for the same gesture.
    (x0, y0), (x1, y1) = _page_widget_corners(
        viewer, 50.0, 100.0, 450.0, 200.0)
    _tablet(viewer, QEvent.Type.TabletPress, x0, y0)
    _tablet(viewer, QEvent.Type.TabletMove, (x0 + x1) / 2, (y0 + y1) / 2)
    _tablet(viewer, QEvent.Type.TabletMove, x1, y1)
    _press(viewer, x0, y0)          # synthesized fallback press
    _move(viewer, x1, y1)           # synthesized fallback move
    _tablet(viewer, QEvent.Type.TabletRelease, x1, y1)
    _release(viewer, x1, y1)        # synthesized fallback release

    assert calls == [1]                       # exactly one commit
    assert _selected_value(viewer) == stroke.id.value
    assert viewer._region_gesture is None
    assert tuple(_store(viewer).page(0)) == before
    assert viewer.document_history.can_undo() is False


def test_synthetic_mouse_then_tablet_gesture_once(qtbot, tmp_path,
                                                  cleared_settings,
                                                  monkeypatch):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    stroke = _seed(viewer, _pen_stroke((100.0, 150.0), (400.0, 150.0)))
    calls = _count_region_commits(monkeypatch)

    corners = [_widget(viewer, lx, ly) for lx, ly in
               ((80.0, 100.0), (300.0, 100.0), (300.0, 220.0),
                (80.0, 220.0))]
    _press(viewer, *corners[0])         # synthesized mouse press first
    _tablet(viewer, QEvent.Type.TabletPress, *corners[0])
    for wx, wy in corners[1:-1]:
        _tablet(viewer, QEvent.Type.TabletMove, wx, wy)
        _move(viewer, wx, wy)
    _tablet(viewer, QEvent.Type.TabletRelease, *corners[-1])
    _release(viewer, *corners[-1])

    assert calls == [1]
    assert _selected_value(viewer) == stroke.id.value
    assert viewer._region_gesture is None
    assert viewer.document_history.can_undo() is False


def _tablet_lasso(viewer, page_points, page_index=0):
    """A stylus freehand loop (press -> draw -> release)."""
    corners = [_widget(viewer, lx, ly, page_index)
               for lx, ly in page_points]
    with _chrome_hidden(viewer):
        _tablet(viewer, QEvent.Type.TabletPress, *corners[0])
        for wx, wy in corners[1:-1]:
            _tablet(viewer, QEvent.Type.TabletMove, wx, wy)
        _tablet(viewer, QEvent.Type.TabletRelease, *corners[-1])


def test_tablet_lasso_and_mouse_gestures_agree(qtbot, tmp_path,
                                               cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    a = _seed(viewer, _rect_shape(150.0, 150.0, 250.0, 250.0))
    b = _seed(viewer, _rect_shape(400.0, 400.0, 500.0, 500.0))

    # A stylus lasso behaves like a mouse lasso (encloses a, misses b).
    _tablet_lasso(viewer, [(80.0, 80.0), (300.0, 80.0), (300.0, 300.0),
                           (80.0, 300.0)])
    assert _selected_values(viewer) == (a.id.value,)

    _lasso(viewer, [(80.0, 80.0), (320.0, 80.0), (320.0, 320.0),
                    (80.0, 320.0)])
    assert _selected_values(viewer) == (a.id.value,)
    assert b.id.value not in _selected_values(viewer)


# =====================================================================
# Qt-free: rendering-geometry sanity (unchanged contracts)
# =====================================================================

def test_stroke_centerline_geometry_unchanged():
    stroke = _pen_stroke((0.0, 0.0), (100.0, 0.0), width_mm=2.0)
    centers, halves = stroke_centerline_geometry(stroke)
    assert len(centers) == len(halves) >= 2
    half = 2.0 * 72.0 / 25.4 / 2.0
    assert halves[0] == pytest.approx(half)


def test_annotation_extent_pads_half_width():
    stroke = _pen_stroke((0.0, 0.0), (100.0, 0.0), width_mm=2.0)
    half = 2.0 * 72.0 / 25.4 / 2.0
    extent = annotation_extent(stroke)
    assert extent == (0.0 - half, 0.0 - half,
                      100.0 + half, 0.0 + half)


def test_polygon_area_contract():
    assert polygon_area(((0.0, 0.0), (4.0, 0.0), (4.0, 3.0))) == 6.0
    assert polygon_area(((0.0, 0.0), (1.0, 1.0))) == 0.0     # degenerate
    assert polygon_area(()) == 0.0
