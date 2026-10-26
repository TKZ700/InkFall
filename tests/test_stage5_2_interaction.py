"""M6.8.2 Stage 5.2: frosted menus, right-click selection, rotation paint
and repaint correctness, frame-interior group move, hover cursors.

Focused widget-level coverage for the Stage 5.2 correctives:

* the shared acrylic menu material is applied to the annotation and page
  context menus (and their submenus) without breaking popup/submenu
  behaviour, and without disturbing the platform popup shadow;
* a right-click under *every* tool selects the annotation it targets and
  opens exactly one menu, never drawing/erasing/moving anything — for the
  mouse and for a stylus barrel-button click;
* Rectangle/Ellipse paint honours ``rotation_deg`` (so a rotated selection
  no longer reads as squashed) and the dirty-region bound is rotation
  aware (so a rotated shape can never leave stale pixels);
* a rotation gesture's scoped repaint covers the rotation grip footprint
  (the "temporary-looking marks" ghost);
* a press anywhere inside the oriented frame starts a group move for every
  tool (Pan included) while grips keep their precedence, a genuine tool
  change still clears the selection, and a right-click selection under any
  tool leaves the whole frame painted, hoverable, and interactive;
* hover cursors follow the grips/frame interior and never go stale.

The pure maths is covered elsewhere (``test_transform_geometry_m682.py``,
``test_selection_transform_m682_stage4.py``); these tests validate the
viewer wiring and the visible/pixel-level effects.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from annotations.models import InkStroke, Shape, ShapeKind
from core.types import Point, Rect, Size
from tests.helpers import make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_SETTINGS_KEYS = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                  "inkfall/m5/toolbar/active",
                  "inkfall/m5/toolbar/custom_colors",
                  "inkfall/m5/toolbar/presets")

_ACCENT = (139, 147, 224)  # _SELECT_ACCENT (#8B93E0)


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


# ------------------------------------------------------------ tool + seeds

def _selection_tool():
    return ToolInstance(kind=ToolType.SELECTION, name="Select")


def _lasso_tool():
    return ToolInstance(kind=ToolType.LASSO, name="Lasso")


def _pan_tool():
    return ToolInstance(kind=ToolType.PAN, name="Pan")


def _pen_tool():
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#e01b24",
                        width_mm=1.0, opacity=0.9)


def _stroke(p0=(100.0, 100.0), p1=(300.0, 200.0), **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#e01b24",
                width_mm=1.0, opacity=1.0)
    base.update(kw)
    return InkStroke(**base)


def _rect(rect=(100.0, 100.0, 220.0, 180.0), **kw):
    base = dict(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(rect[0], rect[1]),
                          Size(rect[2] - rect[0], rect[3] - rect[1])),
                color="#ff0000", width_mm=1.0, fill_enabled=False)
    base.update(kw)
    return Shape(**base)


# --------------------------------------------------------- event + viewer

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


def _right_press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier))


def _context_menu(viewer, x, y):
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                              QPoint(int(x), int(y)),
                              viewer.mapToGlobal(QPoint(int(x), int(y))))
    viewer.contextMenuEvent(event)


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


def _open_window(qtbot, tmp_path, tool=None, name="s52.pdf"):
    from app.main_window import MainWindow

    pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                         page_height=400.0)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool if tool is not None
                                 else _selection_tool())
    qtbot.wait(50)
    return window, viewer


def _store(viewer):
    return viewer._document.annotations


def _seed(viewer, annotation, page_index=0):
    page_id = viewer._pipeline.layout.entry(page_index).page_id
    stored = dataclasses.replace(annotation, page_id=page_id)
    _store(viewer).add(stored, page_index)
    return stored


def _factor(viewer):
    return (float(viewer._pipeline.layout.raster_scale)
            * viewer._display_scale())


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


def _select(viewer, annotations):
    viewer._selection.set([a.id for a in annotations])
    viewer.update()
    QApplication.instance().processEvents()


def _close_menu(viewer):
    menu = getattr(viewer, "_selection_menu", None)
    if menu is not None:
        try:
            menu.close()
        except Exception:
            pass
    viewer._on_selection_menu_hidden()
    QApplication.instance().processEvents()


def _new_stroke_open(viewer):
    return bool(getattr(viewer, "_mouse_stroke_open", False)
                or getattr(viewer, "_tablet_stroke_open", False)
                or getattr(viewer, "_live_stroke", None) is not None)


# ------------------------------------------------------------- pixel tools

def _red_bbox(image, box):
    """Bounding box of strongly red pixels inside ``box`` (or ``None``)."""
    x0, y0, x1, y1 = box
    minx = miny = maxx = maxy = None
    for y in range(max(0, int(y0)), min(image.height(), int(y1))):
        for x in range(max(0, int(x0)), min(image.width(), int(x1))):
            color = image.pixelColor(x, y)
            r, g, b = color.red(), color.green(), color.blue()
            if r > 150 and r - max(g, b) > 60:
                minx = x if minx is None else min(minx, x)
                maxx = x if maxx is None else max(maxx, x)
                miny = y if miny is None else min(miny, y)
                maxy = y if maxy is None else max(maxy, y)
    if minx is None:
        return None
    return (minx, miny, maxx + 1, maxy + 1)


def _page_box(viewer, page_index=0):
    entry = viewer._pipeline.layout.entry(page_index)
    rect = viewer._region_page_screen(entry)
    return (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)


def _paint_bounds(viewer, shape, page_index=0):
    entry = viewer._pipeline.layout.entry(page_index)
    rect = viewer._region_page_screen(entry)
    return viewer._shape_paint_rect(shape, rect, _factor(viewer))


class _UpdateRecorder:
    """Record the union of the widget rects an operation invalidates."""

    def __init__(self, widget):
        self.widget = widget
        self._orig = widget.update
        self.rect = None

    def __enter__(self):
        widget, orig = self.widget, self._orig

        def recorder(*args):
            if args and isinstance(args[0], QRect):
                rect = args[0]
            elif args:
                rect = QRect(args[0])
            else:
                rect = QRect(0, 0, widget.width(), widget.height())
            self.rect = rect if self.rect is None else self.rect.united(rect)
            return orig(*args)

        widget.update = recorder
        return self

    def __exit__(self, *_exc):
        self.widget.update = self._orig
        return False

    def bounds(self):
        if self.rect is None:
            return None
        return (self.rect.x(), self.rect.y(),
                self.rect.x() + self.rect.width(),
                self.rect.y() + self.rect.height())


def _contains(outer, inner, pad=0.0):
    if outer is None or inner is None:
        return False
    return (outer[0] - pad <= inner[0] and outer[1] - pad <= inner[1]
            and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad)


def _grip_box(viewer, pad=6.0):
    points = viewer._selection_handle_points()
    assert points is not None
    gx, gy = points["rotate"]
    return (gx - pad, gy - pad, gx + pad, gy + pad)


_ACCENT = (139, 147, 224)  # viewer._SELECT_ACCENT (#8B93E0)


def _accent_like(color):
    return (abs(color.red() - _ACCENT[0]) <= 45
            and abs(color.green() - _ACCENT[1]) <= 45
            and abs(color.blue() - _ACCENT[2]) <= 45)


def _accent_count(image, x0, y0, x1, y1):
    """Count selection-accent pixels inside a widget box."""
    count = 0
    for y in range(max(0, int(y0)), min(image.height(), int(y1))):
        for x in range(max(0, int(x0)), min(image.width(), int(x1))):
            if _accent_like(image.pixelColor(x, y)):
                count += 1
    return count


def _settle_phase(viewer):
    """Pin the frame at maximum pulse so pixel checks are deterministic."""
    viewer._selection_timer.stop()
    viewer._selection_phase = 15.0
    viewer.update()
    QApplication.instance().processEvents()


# ============================================ 1. acrylic menu material

def test_annotation_and_page_menus_share_acrylic_material(
        qtbot, tmp_path, cleared_settings):
    from app.menu_material import (MENU_MATERIAL_PROPERTY, InkfallMenu,
                                   add_submenu, menu_material_qss)
    from app.theme.tokens import dark_tokens, light_tokens

    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    menu = viewer._build_selection_menu((box.id,), {box.id: box})
    assert menu is not None
    assert isinstance(menu, InkfallMenu)
    assert menu.property(MENU_MATERIAL_PROPERTY) is True
    assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    # The subtle lift comes from the platform popup shadow: the hint that
    # would suppress it must never be set.
    assert not (menu.windowFlags()
                & Qt.WindowType.NoDropShadowWindowHint)
    # Structure preserved: the Rotate and Recolor submenus exist, share the
    # material, and the Delete action is still there.
    texts = [a.text() for a in menu.actions()]
    assert "Delete" in texts
    subs = [a.menu() for a in menu.actions() if a.menu() is not None]
    assert subs, texts
    for sub in subs:
        assert isinstance(sub, InkfallMenu)
        assert sub.property(MENU_MATERIAL_PROPERTY) is True
        assert sub.testAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground)
        for inner in [a.menu() for a in sub.actions() if a.menu()]:
            assert inner.property(MENU_MATERIAL_PROPERTY) is True

    page_menu = viewer._build_page_menu(0)
    assert page_menu is not None
    assert isinstance(page_menu, InkfallMenu)
    assert page_menu.property(MENU_MATERIAL_PROPERTY) is True

    # The material is translucent tint, not an opaque slab, in both themes,
    # and the stylesheet rule is scoped to tagged menus only.
    for tokens in (dark_tokens(), light_tokens()):
        qss = menu_material_qss(tokens)
        assert 'QMenu[inkfallAcrylic="true"]' in qss
        assert "rgba(" in qss
        assert "border-radius" in qss
        assert "border: 1px solid" in qss
    sheet = QApplication.instance().styleSheet() or ""
    if "QMenu {" in sheet:
        assert 'QMenu[inkfallAcrylic="true"]' in sheet

    # Popup/submenu still work with a translucent window.
    menu.popup(QPoint(40, 40))
    qtbot.wait(30)
    assert menu.isVisible()
    sub = add_submenu(menu, "Extra", dark_tokens())
    assert sub.property(MENU_MATERIAL_PROPERTY) is True
    menu.close()
    qtbot.wait(10)
    assert menu.isVisible() is False


def test_untagged_menus_keep_the_solid_surface(qtbot, tmp_path,
                                               cleared_settings):
    """Only opted-in menus change: a plain QMenu stays untagged."""
    from PySide6.QtWidgets import QMenu

    from app.menu_material import MENU_MATERIAL_PROPERTY

    plain = QMenu()
    assert plain.property(MENU_MATERIAL_PROPERTY) in (None, False)
    assert not plain.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground)


# ==================================== 2. right-click selects + one menu

@pytest.mark.parametrize("tool_kind", [ToolType.PEN, ToolType.HIGHLIGHTER,
                                       ToolType.ERASER,
                                       ToolType.RECTANGLE,
                                       ToolType.SELECTION, ToolType.LASSO,
                                       ToolType.PAN])
def test_right_click_selects_annotation_under_every_tool(
        qtbot, tmp_path, cleared_settings, tool_kind):
    """A right-click targets the annotation and opens exactly one menu."""
    tool = ToolInstance(kind=tool_kind, name=str(tool_kind))
    _window, viewer = _open_window(qtbot, tmp_path, tool=tool)
    box = _seed(viewer, _rect())
    other = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _close_menu(viewer)
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    assert _selected_values(viewer) == ()

    wx, wy = _widget(viewer, 160.0, 100.0)   # on the rect's top edge
    with _chrome_hidden(viewer):
        _right_press(viewer, wx, wy)
        _context_menu(viewer, wx, wy)

    assert _selected_values(viewer) == (box.id.value,)
    assert viewer._selection.has_selection is True
    menu = viewer._selection_menu
    assert menu is not None
    texts = [a.text() for a in menu.actions()]
    assert "Delete" in texts            # annotation menu
    assert not any("Page" in t for t in texts)
    # One physical right-click opens exactly one menu: a second request is
    # served by the still-open menu, not by a new one.
    same = viewer._selection_menu
    _context_menu(viewer, wx, wy)
    assert viewer._selection_menu is same
    # Never a tool gesture: no store mutation, no history entry, no stroke.
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries
    assert _new_stroke_open(viewer) is False
    assert viewer._interaction.active_kind == tool_kind
    assert _store(viewer).get(other.id) == other
    _close_menu(viewer)


def test_right_click_on_selection_member_keeps_whole_selection(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])
    _close_menu(viewer)

    wx, wy = _widget(viewer, 160.0, 100.0)
    _context_menu(viewer, wx, wy)
    assert set(_selected_values(viewer)) == {box.id.value, stroke.id.value}
    menu = viewer._selection_menu
    assert menu is not None
    texts = [a.text() for a in menu.actions()]
    assert "Delete" in texts
    _close_menu(viewer)


def test_right_click_empty_page_opens_page_menu(qtbot, tmp_path,
                                                cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    _seed(viewer, _rect())
    wx, wy = _widget(viewer, 520.0, 360.0)   # empty page area
    _context_menu(viewer, wx, wy)
    menu = viewer._selection_menu
    assert menu is not None
    texts = [a.text() for a in menu.actions()]
    assert any("Page" in t for t in texts)
    _close_menu(viewer)


def test_right_click_blank_page_background_deselects(
        qtbot, tmp_path, cleared_settings):
    """The page background is not the selection's surface (Stage 5.2).

    A right-click on blank page area clears every selected annotation —
    including a selection on another page — removes the frame and its
    handles, drops the selection cursor, and still opens the page menu for
    the page under the pointer. Nothing is mutated.
    """
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    other = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, other])
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    assert viewer._selection_handle_points() is not None
    assert viewer._selection_timer.isActive() is True

    blank = _widget(viewer, 520.0, 360.0)
    assert viewer._annotation_at_widget(*blank) is None
    _context_menu(viewer, *blank)

    assert viewer._selection.has_selection is False
    assert viewer._selection.selected_ids == ()
    assert viewer._selection_handle_points() is None
    assert viewer._selection_timer.isActive() is False
    assert viewer._hover_cursor_key is None
    assert viewer._frame_state is None
    menu = viewer._selection_menu
    assert menu is not None
    texts = [a.text() for a in menu.actions()]
    assert any("Page" in t for t in texts)      # page menu, not annotation
    assert not any(t == "Delete" for t in texts)
    # Inert: no annotation, page, or history mutation.
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries
    _close_menu(viewer)


def test_right_click_outside_every_page_keeps_selection(
        qtbot, tmp_path, cleared_settings):
    """Off every page and every annotation: no menu, no deselect."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    outside = _widget(viewer, 640.0, 20.0)   # right of the page edge
    assert viewer._page_index_at_widget(*outside) is None
    assert viewer._annotation_at_widget(*outside) is None
    _context_menu(viewer, *outside)
    assert viewer._selection_menu is None
    assert _selected_values(viewer) == (box.id.value,)


def test_tool_change_clears_selection_and_same_tool_does_not(
        qtbot, tmp_path, cleared_settings):
    """A real tool change deselects everything; re-selecting does not."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)

    _select(viewer, [box])
    assert viewer._hover_cursor_key is None
    viewer._update_selection_cursor(
        *viewer._selection_handle_points()["rotate"])
    assert viewer._hover_cursor_key == "rotate"

    # Selecting the already-active tool is not a change: nothing clears.
    viewer._on_tool_selected(_selection_tool())
    assert _selected_values(viewer) == (box.id.value,)
    assert viewer._selection_timer.isActive() is True

    # Every real transition clears the ids, the frame, the handles, the
    # animation, and the selection cursor.
    for tool in (_lasso_tool(), _pan_tool(), _pen_tool(),
                 ToolInstance(kind=ToolType.HIGHLIGHTER, name="HL"),
                 ToolInstance(kind=ToolType.ERASER, name="Eraser"),
                 ToolInstance(kind=ToolType.RECTANGLE, name="Rect")):
        # Start from Selection so the cursor precondition is meaningful,
        # then re-select (that first switch cleared the previous selection).
        viewer._on_tool_selected(_selection_tool())
        _select(viewer, [box])
        viewer._update_selection_cursor(
            *viewer._selection_handle_points()["rotate"])
        assert viewer._hover_cursor_key == "rotate"
        viewer._on_tool_selected(tool)
        assert viewer._selection.has_selection is False, tool.kind
        assert viewer._selection.selected_ids == ()
        assert viewer._selection_handle_points() is None
        assert viewer._selection_timer.isActive() is False
        assert viewer._hover_cursor_key is None
        assert viewer._frame_state is None

    # No content, history, or page mutation from any of it.
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries


def test_tool_change_cancels_a_live_transform_without_committing(
        qtbot, tmp_path, cleared_settings):
    """A tool switch cancels an armed transform (existing lifecycle)."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    _select(viewer, [box])
    gx, gy = viewer._selection_handle_points()["rotate"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        assert (viewer._move_gesture or {}).get("mode") == "rotate"
        _move(viewer, gx + 40.0, gy + 40.0)
        assert viewer._transform_preview is not None
        viewer._on_tool_selected(_pen_tool())
    assert viewer._move_gesture is None
    assert viewer._transform_preview is None
    assert viewer._selection.has_selection is False
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries


class _FakeTablet:
    """Minimal stylus event stub for the barrel-button guard."""

    def __init__(self, etype, x, y, button, buttons, pressure=0.5):
        self._type = etype
        self._pos = QPointF(x, y)
        self._button = button
        self._buttons = buttons
        self._pressure = pressure
        self.accepted = False

    def type(self):
        return self._type

    def position(self):
        return self._pos

    def pressure(self):
        return self._pressure

    def button(self):
        return self._button

    def buttons(self):
        return self._buttons

    def modifiers(self):
        return Qt.KeyboardModifier.NoModifier

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


def test_stylus_barrel_click_never_starts_a_stroke(qtbot, tmp_path,
                                                   cleared_settings):
    """A pen barrel-button right-click must not draw (it opens the menu)."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    _seed(viewer, _rect())
    baseline = len(_store(viewer).page(0))
    wx, wy = _widget(viewer, 160.0, 100.0)
    event = _FakeTablet(QEvent.Type.TabletPress, wx, wy,
                       Qt.MouseButton.RightButton,
                       Qt.MouseButton.RightButton)
    viewer.tabletEvent(event)
    assert event.accepted is True
    assert _new_stroke_open(viewer) is False
    assert len(_store(viewer).page(0)) == baseline
    # The same stylus event with the primary button still reaches the tool.
    event2 = _FakeTablet(QEvent.Type.TabletPress, wx, wy,
                        Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton)
    viewer.tabletEvent(event2)
    assert event2.accepted is True
    assert _new_stroke_open(viewer) is True
    viewer._apply_intent(viewer._interaction.cancel())


# ============================ 3. rotation paint + rotation-aware repaint

def test_rotated_box_paints_rotated(qtbot, tmp_path, cleared_settings):
    """A 90-degree rotation turns the painted box (was: axis-aligned)."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())          # (100,100)-(220,180): 120x80 pt
    page_box = _page_box(viewer)
    before = _red_bbox(viewer.grab().toImage(), page_box)
    assert before is not None
    w0, h0 = before[2] - before[0], before[3] - before[1]
    assert w0 > h0                        # wide before rotating

    _select(viewer, [box])
    ok = viewer._rotate_selection((box.id,), {box.id: box}, 90.0,
                                 "Rotate Right")
    assert ok is True
    viewer._selection.clear()
    viewer.update()
    QApplication.instance().processEvents()
    assert dataclasses.replace(box).rotation_deg in (0.0,)
    assert _store(viewer).get(box.id).rotation_deg == 90.0

    after = _red_bbox(viewer.grab().toImage(), page_box)
    assert after is not None
    w1, h1 = after[2] - after[0], after[3] - after[1]
    # Dimensions swap: the painted box really turned a quarter turn, at
    # rigour ~the same size (stroke rounding only).
    assert h1 > w1
    assert abs(w1 - h0) <= 6 and abs(h1 - w0) <= 6


def test_rotated_box_dirty_rect_covers_rotated_geometry(
        qtbot, tmp_path, cleared_settings):
    """The repaint bound follows the rotated geometry, not the stored box."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    factor = _factor(viewer)
    page_box = _page_box(viewer)

    plain = _paint_bounds(viewer, box)
    assert plain is not None
    x0 = page_box[0] + box.rect.origin.x * factor
    y0 = page_box[1] + box.rect.origin.y * factor
    assert _contains((plain.x(), plain.y(), plain.x() + plain.width(),
                      plain.y() + plain.height()),
                     (x0, y0, x0 + box.rect.width * factor,
                      y0 + box.rect.height * factor), pad=1.0)

    _select(viewer, [box])
    assert viewer._rotate_selection((box.id,), {box.id: box}, 90.0,
                                    "Rotate Right") is True
    rotated = _store(viewer).get(box.id)
    assert rotated.rotation_deg == 90.0

    bounds = _paint_bounds(viewer, rotated)
    assert bounds is not None
    out = (bounds.x(), bounds.y(),
           bounds.x() + bounds.width(), bounds.y() + bounds.height())
    # Rotated extent (a quarter turn of the 120x80 box about its centre).
    cx = page_box[0] + (rotated.rect.origin.x
                        + rotated.rect.width / 2.0) * factor
    cy = page_box[1] + (rotated.rect.origin.y
                        + rotated.rect.height / 2.0) * factor
    half_w = rotated.rect.height / 2.0 * factor
    half_h = rotated.rect.width / 2.0 * factor
    assert _contains(out, (cx - half_w, cy - half_h,
                           cx + half_w, cy + half_h))
    # The old, unrotated-only bound would have missed the new corners:
    # the bound is at least as large as the rotated geometry diagonal box.
    assert (out[3] - out[1]) >= (2.0 * half_h - 1.0)


def test_rotation_repaint_covers_the_rotation_grip(qtbot, tmp_path,
                                                   cleared_settings):
    """Scoped rotation repaints include the grip footprint (no ghosts)."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])

    frame = viewer._selection_frame_screen_box()
    grip = _grip_box(viewer)
    frame_bounds = (frame.x(), frame.y(),
                    frame.x() + frame.width(), frame.y() + frame.height())
    gx, gy = viewer._selection_handle_points()["rotate"]
    # The grip sits outside the oriented quad...
    assert viewer._pan_point_in_frame(gx, gy) is False
    # ...but inside the frame's repaint bound, which is what makes the
    # scoped rotation repaint cover it (the pre-fix bound was the quad's
    # axis-aligned box only, so grip pixels were never invalidated).
    assert _contains(frame_bounds, grip)

    from app import viewer as viewer_module

    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        assert (viewer._move_gesture or {}).get("mode") == "rotate"
        # Step past the dead zone, then keep rotating.
        first = gy - viewer_module._MARQUEE_MIN_PX - 8.0
        checked = 0
        last = (gx, first)
        for step in range(6):
            target = (gx + (step + 1) * 3.0, first - step * 6.0)
            old_grip = _grip_box(viewer)
            with _UpdateRecorder(viewer) as rec:
                _move(viewer, *target)
            new_grip = _grip_box(viewer)
            last = target
            region = rec.bounds()
            if region is None:
                continue
            checked += 1
            assert _contains(region, old_grip), (step, region, old_grip)
            assert _contains(region, new_grip), (step, region, new_grip)
        _release(viewer, *last)
    assert checked >= 1


# ============================== 4. frame-interior group move precedence

def test_frame_interior_drag_moves_the_whole_group(qtbot, tmp_path,
                                                   cleared_settings):
    """Empty space inside the oriented frame is a draggable surface."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])
    entries = _history_entries(viewer)
    factor = _factor(viewer)

    # A point inside the frame with no annotation geometry under it.
    probe = _widget(viewer, 300.0, 225.0)
    assert viewer._annotation_at_widget(*probe) is None
    assert viewer._pan_point_in_frame(*probe) is True
    assert viewer._frame_interior_hit(*probe) is not None

    dx, dy = 48.0, 30.0
    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        assert viewer._move_gesture is not None
        assert viewer._move_gesture.get("mode") == "move"
        assert viewer._move_gesture.get("interior") is True
        for i in (1, 2, 3):
            _move(viewer, probe[0] + dx * i / 3.0, probe[1] + dy * i / 3.0)
        _release(viewer, probe[0] + dx, probe[1] + dy)

    moved_box = _store(viewer).get(box.id)
    moved_stroke = _store(viewer).get(stroke.id)
    assert moved_box != box and moved_stroke != stroke
    exp_dx, exp_dy = dx / factor, dy / factor
    assert moved_box.rect.origin.x == pytest.approx(
        box.rect.origin.x + exp_dx, abs=1.0)
    assert moved_box.rect.origin.y == pytest.approx(
        box.rect.origin.y + exp_dy, abs=1.0)
    assert moved_stroke.points[0].x == pytest.approx(
        stroke.points[0].x + exp_dx, abs=1.0)
    assert moved_stroke.points[0].y == pytest.approx(
        stroke.points[0].y + exp_dy, abs=1.0)
    # One grouped entry, and the selection is untouched.
    assert _history_entries(viewer) == entries + 1
    assert set(_selected_values(viewer)) == {box.id.value, stroke.id.value}


def test_subthreshold_click_inside_frame_keeps_the_selection(
        qtbot, tmp_path, cleared_settings):
    """A click on the frame's empty interior is not a deselect."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])
    baseline = _store(viewer).page(0)
    entries = _history_entries(viewer)
    probe = _widget(viewer, 300.0, 225.0)
    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        _release(viewer, *probe)
    assert set(_selected_values(viewer)) == {box.id.value, stroke.id.value}
    assert _store(viewer).page(0) == baseline
    assert _history_entries(viewer) == entries


def test_grips_keep_precedence_over_the_interior_move(qtbot, tmp_path,
                                                      cleared_settings):
    """A press on a grip is never treated as an interior move."""
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    points = viewer._selection_handle_points()
    assert points is not None

    for name in ("se", "n", "rotate"):
        gx, gy = points[name]
        assert viewer._transform_handle_at(gx, gy) == name
        assert viewer._frame_interior_hit(gx, gy) is None
        assert viewer._group_move_target(gx, gy) is None

    # An actual grip drag still resizes (no move), with one history entry.
    entries = _history_entries(viewer)
    gx, gy = points["se"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        assert (viewer._move_gesture or {}).get("mode") == "resize"
        for i in (1, 2, 3):
            _move(viewer, gx + 8.0 * i, gy + 6.0 * i)
        _release(viewer, gx + 24.0, gy + 18.0)
    resized = _store(viewer).get(box.id)
    assert resized.rect.width > box.rect.width
    # A resize scales about the opposite corner of the *padded* frame, so
    # the anchored origin can pick up a sub-point drift — the point here is
    # that the gesture resized (not moved) and committed one entry.
    assert abs(resized.rect.origin.x - box.rect.origin.x) < 3.0
    assert abs(resized.rect.origin.y - box.rect.origin.y) < 3.0
    assert _history_entries(viewer) == entries + 1


def test_pen_frame_surface_takes_precedence_inside_and_draws_outside(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2.1: the visible frame wins inside, Pen still draws out.

    A selection makes the whole oriented frame an active manipulation
    surface even under Pen: a press in its empty interior arms the shared
    group move (never a stroke). Outside the frame the Pen is untouched and
    draws normally.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    box = _seed(viewer, _rect())
    other = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, other])
    factor = _factor(viewer)
    entries = _history_entries(viewer)
    interior = _widget(viewer, 300.0, 225.0)   # empty space inside the frame
    assert viewer._pan_point_in_frame(*interior) is True
    assert viewer._annotation_at_widget(*interior) is None

    with _chrome_hidden(viewer):
        _press(viewer, *interior)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        assert _new_stroke_open(viewer) is False
        for i in (1, 2, 3):
            _move(viewer, interior[0] + 39.0 * i / 3.0, interior[1])
        _release(viewer, interior[0] + 39.0, interior[1])

    moved = _store(viewer).get(box.id)
    assert moved.rect.origin.x == pytest.approx(
        box.rect.origin.x + 39.0 / factor, abs=1.0)
    assert _history_entries(viewer) == entries + 1

    # Outside the frame the same Pen draws a new stroke (not a cage).
    page_before = _store(viewer).page(0)
    outside = _widget(viewer, 40.0, 40.0)
    assert viewer._pan_point_in_frame(*outside) is False
    with _chrome_hidden(viewer):
        _press(viewer, *outside)
        assert _new_stroke_open(viewer) is True
        _move(viewer, outside[0] + 20.0, outside[1] + 12.0)
        _release(viewer, outside[0] + 20.0, outside[1] + 12.0)
    assert _store(viewer).page(0) != page_before
    assert _history_entries(viewer) == entries + 2


def test_pan_frame_interior_drag_moves_the_group(qtbot, tmp_path,
                                                 cleared_settings):
    """M6.8.2 Stage 5.2.1: Pan's frame interior moves, never pans.

    The whole oriented frame - including the empty space inside a selected
    box - arms the shared group move, so a Pan drag there moves the
    selection instead of the viewport.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    box = _seed(viewer, _rect(rect=(150.0, 150.0, 400.0, 330.0)))
    _select(viewer, [box])
    factor = _factor(viewer)
    entries = _history_entries(viewer)
    probe = _widget(viewer, 275.0, 300.0)   # inside the frame, no body
    assert viewer._annotation_at_widget(*probe) is None
    assert viewer._pan_point_in_frame(*probe) is True
    scroll_before = viewer._scrollbar.value()

    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        assert (viewer._move_gesture or {}).get("interior") is True
        for i in (1, 2, 3):
            _move(viewer, probe[0] + 42.0 * i / 3.0,
                  probe[1] - 24.0 * i / 3.0)
        _release(viewer, probe[0] + 42.0, probe[1] - 24.0)

    moved = _store(viewer).get(box.id)
    assert moved.rect.origin.x == pytest.approx(
        box.rect.origin.x + 42.0 / factor, abs=1.0)
    assert moved.rect.origin.y == pytest.approx(
        box.rect.origin.y - 24.0 / factor, abs=1.0)
    assert _history_entries(viewer) == entries + 1
    assert viewer._scrollbar.value() == scroll_before   # never panned
    assert set(_selected_values(viewer)) == {box.id.value}


def test_pan_drag_outside_the_frame_still_pans(qtbot, tmp_path,
                                               cleared_settings):
    """Outside the oriented frame Pan keeps its navigation and deselect."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    outside = _widget(viewer, 40.0, 40.0)
    assert viewer._pan_point_in_frame(*outside) is False
    with _chrome_hidden(viewer):
        _press(viewer, *outside)
        # The immediate Pan deselect ran, and no move was armed.
        assert viewer._selection.has_selection is False
        assert viewer._move_gesture is None
        _move(viewer, outside[0], outside[1] - 40.0)
        _release(viewer, outside[0], outside[1] - 40.0)
    assert _store(viewer).get(box.id) == box
    assert viewer._selection.has_selection is False


def test_pan_selected_body_still_moves_the_group(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pan_tool())
    stroke = _seed(viewer, _stroke((150.0, 200.0), (400.0, 200.0)))
    _select(viewer, [stroke])
    probe = _widget(viewer, 275.0, 200.0)
    assert viewer._annotation_at_widget(*probe) is not None
    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        assert viewer._move_gesture is not None
        for i in (1, 2, 3):
            _move(viewer, probe[0] + 30.0 * i / 3.0, probe[1])
        _release(viewer, probe[0] + 30.0, probe[1])
    moved = _store(viewer).get(stroke.id)
    assert moved.points[0].x > stroke.points[0].x


# =============================================== 5. hover cursor feedback

def test_lasso_frame_interior_drag_moves_the_group(qtbot, tmp_path,
                                                   cleared_settings):
    """The visible frame is a movement surface for Lasso too (Stage 5.2.1)."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])
    factor = _factor(viewer)
    entries = _history_entries(viewer)
    probe = _widget(viewer, 300.0, 225.0)   # empty space inside the frame
    assert viewer._pan_point_in_frame(*probe) is True
    assert viewer._annotation_at_widget(*probe) is None

    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        for i in (1, 2, 3):
            _move(viewer, probe[0] + 36.0 * i / 3.0, probe[1])
        _release(viewer, probe[0] + 36.0, probe[1])

    moved = _store(viewer).get(box.id)
    assert moved.rect.origin.x == pytest.approx(
        box.rect.origin.x + 36.0 / factor, abs=1.0)
    assert _history_entries(viewer) == entries + 1


def test_lasso_selected_body_still_moves_the_group(qtbot, tmp_path,
                                                   cleared_settings):
    """Lasso keeps the established selected-body drag.

    Only the empty-interior rule is Selection-only; dragging an actual
    selected object under Lasso is unchanged.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_lasso_tool())
    stroke = _seed(viewer, _stroke((150.0, 200.0), (400.0, 200.0)))
    _select(viewer, [stroke])
    probe = _widget(viewer, 275.0, 200.0)
    assert viewer._annotation_at_widget(*probe) is not None
    with _chrome_hidden(viewer):
        _press(viewer, *probe)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        for i in (1, 2, 3):
            _move(viewer, probe[0] + 30.0 * i / 3.0, probe[1])
        _release(viewer, probe[0] + 30.0, probe[1])
    moved = _store(viewer).get(stroke.id)
    assert moved.points[0].x > stroke.points[0].x


def test_hover_cursors_follow_grips_interior_and_restore(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    app = QApplication.instance()

    points = viewer._selection_handle_points()
    assert points is not None

    def hover(x, y):
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    # Rotation grip -> the custom (non-default) rotate cursor.
    gx, gy = points["rotate"]
    hover(gx, gy)
    assert viewer._hover_cursor_key == "rotate"
    assert viewer.cursor().shape() == Qt.CursorShape.BitmapCursor

    # Resize grips -> the resize cursor matching the on-screen direction.
    for name, expected in (("se", "SizeFDiagCursor"),
                           ("ne", "SizeBDiagCursor"),
                           ("n", "SizeVerCursor"),
                           ("e", "SizeHorCursor")):
        hx, hy = points[name]
        hover(hx, hy)
        assert viewer._hover_cursor_key == expected, name
        assert viewer.cursor().shape() == getattr(Qt.CursorShape, expected)

    # Movable interior -> the four-way move cursor (never a hand/pan
    # cursor). The rect has no fill, so its centre is inside the frame yet
    # hits no stroke (a movable surface).
    probe = _widget(viewer, 160.0, 140.0)
    assert viewer._annotation_at_widget(*probe) is None
    hover(*probe)
    assert viewer._hover_cursor_key == "SizeAllCursor"
    assert viewer.cursor().shape() == Qt.CursorShape.SizeAllCursor

    # A selected annotation *body* is the same four-way move cursor.
    body = viewer._annotation_at_widget(*_widget(viewer, 160.0, 100.0))
    assert body is not None and viewer._selection.is_selected(body.id)
    hover(*_widget(viewer, 160.0, 100.0))
    assert viewer._hover_cursor_key == "SizeAllCursor"
    assert viewer.cursor().shape() == Qt.CursorShape.SizeAllCursor

    # Away from the controls -> the tool default (no stale cursor).
    far = _widget(viewer, 560.0, 380.0)
    hover(*far)
    assert viewer._hover_cursor_key is None
    assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor

    # A drag keeps communicating "moving an object" (still four-way, not a
    # closed hand): the drag cursor is the move cursor.
    hover(gx, gy)
    assert viewer.cursor().shape() == Qt.CursorShape.BitmapCursor
    viewer._selection.set([box.id])
    viewer._move_press(box.id, probe[0], probe[1], "rect")
    viewer._update_selection_cursor(*probe, dragging=True)
    assert viewer._hover_cursor_key == "SizeAllCursor"
    assert viewer.cursor().shape() == Qt.CursorShape.SizeAllCursor
    viewer._move_cancel()
    assert viewer._hover_cursor_key is None

    # Tool switch clears it: Pen must never inherit a selection cursor.
    hover(gx, gy)
    assert viewer._hover_cursor_key == "rotate"
    viewer._on_tool_selected(_pen_tool())
    assert viewer._hover_cursor_key is None
    assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_frame_cursors_follow_the_frame_under_every_tool(
        qtbot, tmp_path, cleared_settings):
    """The frame's cursors are available wherever the frame is displayed.

    M6.8.2 Stage 5.2.1: a visible selection frame is a manipulation surface
    under every tool, so the grip and four-way move cursors appear under
    Lasso/Pan/Pen too - a context-menu selection under Pen is fully
    hoverable. A point outside the frame still resolves to the tool's own
    cursor, and a real tool change (which clears the selection) removes the
    cursors entirely.
    """
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    stroke = _seed(viewer, _stroke((400.0, 300.0), (500.0, 350.0)))
    _select(viewer, [box, stroke])
    app = QApplication.instance()

    def hover(x, y):
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    interior = _widget(viewer, 300.0, 225.0)   # empty space inside the frame
    body = _widget(viewer, 450.0, 325.0)       # on the selected stroke
    outside = _widget(viewer, 40.0, 40.0)
    assert viewer._annotation_at_widget(*interior) is None
    assert viewer._annotation_at_widget(*body) is not None
    assert viewer._pan_point_in_frame(*outside) is False

    for tool in (_selection_tool(), _lasso_tool(), _pan_tool(), _pen_tool()):
        viewer._on_tool_selected(tool)      # a real change clears the frame
        _select(viewer, [box, stroke])      # context-selection equivalent
        assert viewer._selection_frame_visible() is True
        hover(*interior)
        assert viewer._hover_cursor_key == "SizeAllCursor", tool.kind
        hover(*body)
        assert viewer._hover_cursor_key == "SizeAllCursor", tool.kind
        hover(*outside)
        assert viewer._hover_cursor_key is None, tool.kind

    # Grips keep their own cursors under a non-selection tool as well.
    points = viewer._selection_handle_points()
    hover(*points["rotate"])
    assert viewer._hover_cursor_key == "rotate"
    hover(*points["n"])
    assert viewer._hover_cursor_key == "SizeVerCursor"

    # A real tool change (Pen -> Rectangle) clears the selection: no stale
    # frame cursor survives it.
    viewer._on_tool_selected(ToolInstance(kind=ToolType.RECTANGLE,
                                          name="Rectangle"))
    assert viewer._selection.has_selection is False
    hover(*interior)
    assert viewer._hover_cursor_key is None
    assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_cursor_updates_on_selection_change_and_scroll(qtbot, tmp_path,
                                                       cleared_settings):
    from PySide6.QtGui import QWheelEvent

    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    app = QApplication.instance()
    gx, gy = viewer._selection_handle_points()["rotate"]
    viewer._update_selection_cursor(gx, gy)
    assert viewer._hover_cursor_key == "rotate"

    # Scrolling invalidates the hovered geometry: the cursor is dropped.
    event = QWheelEvent(QPointF(gx, gy), QPointF(gx, gy), QPoint(0, 0),
                        QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    viewer.wheelEvent(event)
    app.processEvents()
    assert viewer._hover_cursor_key is None

    # A selection change also drops it.
    viewer._update_selection_cursor(gx, gy)
    assert viewer._hover_cursor_key == "rotate"
    viewer._selection.clear()
    viewer._revalidate_selection()
    assert viewer._hover_cursor_key is None


# ============================== 6. visible selected state under any tool

@pytest.mark.parametrize("tool_kind", [
    ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER, ToolType.PAN,
    ToolType.RECTANGLE])
def test_right_click_selection_shows_the_whole_frame_under_any_tool(
        qtbot, tmp_path, cleared_settings, tool_kind):
    """M6.8.2 Stage 5.2.1: right-click selects *visibly* under every tool.

    After the annotation menu closes, the complete selected state - shared
    border, eight resize grips, the rotation grip and its connector - is
    painted and hoverable, and the original tool is still active (never
    switched to Selection).
    """
    tool = ToolInstance(kind=tool_kind, name=str(tool_kind))
    _window, viewer = _open_window(qtbot, tmp_path, tool=tool,
                                   name=f"s52-frame-{tool_kind}.pdf")
    box = _seed(viewer, _rect())
    _close_menu(viewer)
    wx, wy = _widget(viewer, 160.0, 100.0)      # on the rect's top edge
    with _chrome_hidden(viewer):
        _right_press(viewer, wx, wy)
        _context_menu(viewer, wx, wy)
    menu = viewer._selection_menu
    assert menu is not None
    assert "Delete" in [a.text() for a in menu.actions()]
    _close_menu(viewer)

    # The original tool is untouched and the whole selected state is live.
    assert viewer._interaction.active_kind == tool_kind
    assert _selected_values(viewer) == (box.id.value,)
    assert viewer._selection_frame_visible() is True
    points = viewer._selection_handle_points()
    assert points is not None
    assert set(points) == {"nw", "n", "ne", "e", "se", "s", "sw", "w",
                           "rotate"}
    assert viewer._selection_timer.isActive() is True

    # The border, the grips, and the rotation grip are really painted.
    _settle_phase(viewer)
    with _chrome_hidden(viewer):
        image = viewer.grab().toImage()
    frame_box = viewer._selection_frame_screen_box()
    assert frame_box is not None
    assert _accent_count(image, frame_box.x(), frame_box.y(),
                         frame_box.right() + 1,
                         frame_box.bottom() + 1) > 40
    gx, gy = points["rotate"]
    assert _accent_count(image, gx - 8, gy - 8, gx + 8, gy + 8) > 0

    # The same hit-testing precedence drives the hover cursor.
    app = QApplication.instance()

    def hover(x, y):
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        app.processEvents()

    hover(gx, gy)
    assert viewer._hover_cursor_key == "rotate"
    hover(*_widget(viewer, 160.0, 140.0))       # empty frame interior
    assert viewer._hover_cursor_key == "SizeAllCursor"
    hover(*_widget(viewer, 40.0, 40.0))         # outside the frame
    assert viewer._hover_cursor_key is None


def test_context_selection_under_pen_uses_the_shared_gesture_machine(
        qtbot, tmp_path, cleared_settings):
    """Right-click under Pen, then resize and move through the same paths.

    Grips stay actionable, the frame interior moves the group, and every
    manipulation uses the existing preview + grouped undo commands while
    Pen remains the active tool.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    box = _seed(viewer, _rect())
    _close_menu(viewer)
    wx, wy = _widget(viewer, 160.0, 100.0)
    with _chrome_hidden(viewer):
        _right_press(viewer, wx, wy)
        _context_menu(viewer, wx, wy)
    _close_menu(viewer)
    assert viewer._interaction.active_kind == ToolType.PEN
    assert viewer._selection_frame_visible() is True
    entries = _history_entries(viewer)
    factor = _factor(viewer)

    # 1) A resize grip resizes through the shared transform preview.
    points = viewer._selection_handle_points()
    assert viewer._transform_handle_at(*points["rotate"]) == "rotate"
    gx, gy = points["se"]
    with _chrome_hidden(viewer):
        _press(viewer, gx, gy)
        assert (viewer._move_gesture or {}).get("mode") == "resize"
        assert _new_stroke_open(viewer) is False
        for i in (1, 2, 3):
            _move(viewer, gx + 10.0 * i, gy + 8.0 * i)
        assert viewer._transform_preview is not None
        _release(viewer, gx + 30.0, gy + 24.0)
    resized = _store(viewer).get(box.id)
    assert resized.rect.width > box.rect.width
    assert _history_entries(viewer) == entries + 1

    # 2) The frame interior moves the (now resized) group.
    interior = _widget(viewer, 160.0, 140.0)
    assert viewer._annotation_at_widget(*interior) is None
    assert viewer._pan_point_in_frame(*interior) is True
    with _chrome_hidden(viewer):
        _press(viewer, *interior)
        assert (viewer._move_gesture or {}).get("mode") == "move"
        for i in (1, 2, 3):
            _move(viewer, interior[0] + 30.0 * i / 3.0, interior[1])
        _release(viewer, interior[0] + 30.0, interior[1])
    moved = _store(viewer).get(box.id)
    assert moved.rect.origin.x == pytest.approx(
        resized.rect.origin.x + 30.0 / factor, abs=1.5)
    assert _history_entries(viewer) == entries + 2
    assert _selected_values(viewer) == (box.id.value,)   # still selected


# ============ 7. tool use outside the frame drops a context selection

def test_pen_press_outside_frame_clears_the_context_selection(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2.2: after a right-click selection, Pen outside clears.

    The press belongs to the active tool, so the context selection is
    dropped first - frame, handles, cursor and animation included - and the
    very same press then starts exactly one normal stroke: no swallowed
    press, no duplicate stroke, no leftover move/transform gesture.
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    box = _seed(viewer, _rect())
    _close_menu(viewer)
    wx, wy = _widget(viewer, 160.0, 100.0)      # on the rect's top edge
    with _chrome_hidden(viewer):
        _right_press(viewer, wx, wy)
        _context_menu(viewer, wx, wy)
    _close_menu(viewer)
    assert _selected_values(viewer) == (box.id.value,)
    assert viewer._selection_frame_visible() is True
    entries = _history_entries(viewer)

    outside = _widget(viewer, 40.0, 360.0)      # on the page, outside frame
    assert viewer._pan_point_in_frame(*outside) is False
    with _chrome_hidden(viewer):
        _press(viewer, *outside)
        # 1) the stale selection is gone (frame, handles, cursor, preview)...
        assert _selected_values(viewer) == ()
        assert viewer._selection_frame_visible() is False
        assert viewer._selection_handle_points() is None
        assert viewer._selection_timer.isActive() is False
        assert viewer._hover_cursor_key is None
        assert viewer._move_gesture is None
        # 2) ...and the tool's own normal interaction started instead.
        assert _new_stroke_open(viewer) is True
        _move(viewer, outside[0] + 24.0, outside[1] - 10.0)
        _release(viewer, outside[0] + 24.0, outside[1] - 10.0)

    assert _history_entries(viewer) == entries + 1      # exactly one stroke
    assert _store(viewer).get(box.id) == box            # old ink untouched


def test_eraser_press_outside_frame_clears_and_still_erases(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2.2: erasing outside the frame deselects, then erases."""
    _window, viewer = _open_window(
        qtbot, tmp_path,
        tool=ToolInstance(kind=ToolType.ERASER, name="Eraser"))
    kept = _seed(viewer, _stroke((120.0, 120.0), (240.0, 140.0)))
    doomed = _seed(viewer, _stroke((360.0, 320.0), (500.0, 380.0)))
    _select(viewer, [kept])
    assert viewer._selection_frame_visible() is True
    entries = _history_entries(viewer)

    start = _widget(viewer, 430.0, 350.0)       # on `doomed`, outside the frame
    assert viewer._pan_point_in_frame(*start) is False
    assert viewer._annotation_at_widget(*start).id == doomed.id
    with _chrome_hidden(viewer):
        _press(viewer, *start)
        assert _selected_values(viewer) == ()   # dropped before the erase
        assert viewer._selection_frame_visible() is False
        _move(viewer, *_widget(viewer, 470.0, 367.14))
        _release(viewer, *_widget(viewer, 470.0, 367.14))

    assert _store(viewer).get(doomed.id) is None        # the erase ran
    assert _store(viewer).get(kept.id) == kept          # only the target went
    assert _history_entries(viewer) == entries + 1


def test_shape_press_outside_frame_clears_and_starts_the_shape(
        qtbot, tmp_path, cleared_settings):
    """M6.8.2 Stage 5.2.2: a shape gesture outside deselects, then starts."""
    _window, viewer = _open_window(
        qtbot, tmp_path,
        tool=ToolInstance(kind=ToolType.RECTANGLE, name="Rectangle"))
    box = _seed(viewer, _rect())
    _select(viewer, [box])
    entries = _history_entries(viewer)
    page_before = _store(viewer).page(0)

    start = _widget(viewer, 380.0, 280.0)       # well outside the frame
    assert viewer._pan_point_in_frame(*start) is False
    with _chrome_hidden(viewer):
        _press(viewer, *start)
        assert _selected_values(viewer) == ()
        assert viewer._selection_frame_visible() is False
        assert _new_stroke_open(viewer) is False    # pen/highlighter stayed out
        assert viewer._shape_mouse_open is True     # the shape gesture started
        _move(viewer, start[0] + 40.0, start[1] + 30.0)
        _release(viewer, start[0] + 40.0, start[1] + 30.0)

    assert _store(viewer).page(0) != page_before         # the shape landed
    assert _store(viewer).get(box.id) == box             # old shape untouched
    assert _history_entries(viewer) == entries + 1
