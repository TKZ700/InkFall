"""M6.8.2 Stage 5: selection context menu, delete, recolor, rotate.

Widget-level coverage for the additive Stage 5 operations layered on the
M6.8.2 selection-editing system:

* the selection context menu (Qt's ``QContextMenuEvent``), its
  right-click selection rules (member keeps the whole selection, an
  unselected target is selected, the page *background* clears the whole
  selection and opens the page menu, and nothing outside every page opens),
  single-open-per-gesture, keyboard invocation, action applicability, and
  the no-mutation-on-open/dismiss guarantee;
* Delete over the whole selection (single/multi/multi-page), one grouped
  undo entry, exact object/order/page restoration on undo and the same
  deletion on redo, and all-or-nothing preflight (a replaced target
  aborts before any mutation);
* Recolor (stroke and fill) with the same grouped, preflighted,
  all-or-nothing contract, exact undo/redo, highlighter alpha
  preservation, and stroke/fill independence;
* the three rotate commands reusing the group transform: mixed and
  cross-page selections about the shared centre, page assignment and
  paint order preserved, the oriented frame kept in agreement, exact
  undo/redo, and no entry for a genuine no-op.

The pure recolor policy is covered Qt-free in
``tests/test_recolor_m682_stage5.py`` and the pure transform maths in
``tests/test_transform_geometry_m682.py``; these tests validate the
viewer wiring, event handling, and history/undo behaviour.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import (
    QContextMenuEvent,
    QKeyEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import QApplication

from annotations.hit_test import annotation_extent
from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from annotations.transform import rotate_point
from core.types import Point, Rect, Size
from tests.helpers import make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

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


# ------------------------------------------------------------ tool + seeds

def _selection_tool():
    return ToolInstance(kind=ToolType.SELECTION, name="Select")


def _lasso_tool():
    return ToolInstance(kind=ToolType.LASSO, name="Lasso")


def _pan_tool():
    return ToolInstance(kind=ToolType.PAN, name="Pan")


def _pen_tool(color="#e01b24"):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color=color,
                        width_mm=1.0, opacity=0.9)


def _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0), **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#e01b24",
                width_mm=1.0, opacity=1.0)
    base.update(kw)
    return InkStroke(**base)


def _highlighter(p0=(120.0, 300.0), p1=(280.0, 300.0), **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#f5d90a",
                width_mm=4.0, opacity=0.4)
    base.update(kw)
    return Highlighter(**base)


def _box(kind=ShapeKind.RECTANGLE, rect=(400.0, 100.0, 520.0, 200.0), **kw):
    base = dict(kind=kind,
                rect=Rect(Point(rect[0], rect[1]),
                          Size(rect[2] - rect[0], rect[3] - rect[1])),
                color="#000000", width_mm=1.0,
                fill_enabled=True, fill_color="#ffffff")
    base.update(kw)
    return Shape(**base)


# -------------------------------------------------------- viewer helpers

def _open_window(qtbot, tmp_path, tool=None, name="s5.pdf", sizes=None):
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
    qtbot.wait(50)
    return window, viewer


def _index_of(viewer, annotation):
    return viewer._page_index_for(annotation.page_id)


def _store(viewer):
    return viewer._document.annotations


def _seed(viewer, annotation, page_index=0):
    entry_page = viewer._pipeline.layout.entry(page_index).page_id
    stored = dataclasses.replace(annotation, page_id=entry_page)
    _store(viewer).add(stored, page_index)
    return stored


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


def _pick_widget(viewer, annotation):
    """A widget point that reliably hits ``annotation`` (rotation 0)."""
    index = _index_of(viewer, annotation)
    if isinstance(annotation, Shape):
        if annotation.kind in (ShapeKind.LINE, ShapeKind.ARROW,
                               ShapeKind.POLYGON):
            v = annotation.points[0]
            lx, ly = v.x, v.y
        else:
            rect = annotation.rect
            lx = rect.origin.x + rect.width / 2.0
            ly = rect.origin.y
    else:
        pts = annotation.points
        mid = max(1, len(pts) // 2) if len(pts) > 1 else 0
        a, b = pts[mid - 1], pts[mid]
        lx = (a.x + b.x) / 2.0
        ly = (a.y + b.y) / 2.0
    return _widget(viewer, lx, ly, index)


def _history_entries(viewer):
    return viewer.document_history.stack.count()


def _selected_values(viewer):
    return tuple(a.value for a in viewer._selection.selected_ids)


def _shared_extent(viewer, annotation):
    index = _index_of(viewer, annotation)
    ext = annotation_extent(annotation)
    ox, oy = _offset(viewer, index)
    return (ext[0] + ox, ext[1] + oy, ext[2] + ox, ext[3] + oy)


def _shared_pivot(viewer, annotations):
    extents = [_shared_extent(viewer, a) for a in annotations]
    x0 = min(e[0] for e in extents)
    y0 = min(e[1] for e in extents)
    x1 = max(e[2] for e in extents)
    y1 = max(e[3] for e in extents)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


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


# ------------------------------------------------------- event injection

def _press(viewer, x, y, button=Qt.MouseButton.LeftButton):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        button, button, Qt.KeyboardModifier.NoModifier))


def _release(viewer, x, y, button=Qt.MouseButton.LeftButton):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        button, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _context_menu(viewer, wx=None, wy=None, reason=None):
    if reason is None:
        reason = (QContextMenuEvent.Reason.Mouse if wx is not None
                  else QContextMenuEvent.Reason.Keyboard)
    if wx is None:
        local = QPoint(-1, -1)
        glob = QPoint(-1, -1)
    else:
        local = QPoint(int(round(wx)), int(round(wy)))
        glob = viewer.mapToGlobal(local)
    event = QContextMenuEvent(reason, local, glob)
    viewer.contextMenuEvent(event)
    return event


def _key(viewer, key):
    event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    viewer.keyPressEvent(event)
    return event


def _menu(viewer):
    return viewer._selection_menu


def _labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def _submenu(menu, text):
    for action in menu.actions():
        if action.text() == text:
            return action.menu()
    return None


def _trigger(menu, path):
    target = menu
    for label in path[:-1]:
        target = _submenu(target, label)
        assert target is not None, path
    for action in target.actions():
        if action.text() == path[-1]:
            action.trigger()
            return
    raise AssertionError(f"no action {path!r} in "
                         f"{[a.text() for a in target.actions()]}")


def _close_menu(viewer):
    menu = viewer._selection_menu
    if menu is not None:
        menu.close()
        menu.hide()
    viewer._on_selection_menu_hidden()
    QApplication.processEvents()


def _replace_in_store(viewer, annotation, **changes):
    """Simulate an intervening edit: swap the object (same id, same slot)."""
    clone = dataclasses.replace(annotation, **changes)
    _store(viewer).replace(annotation.id, [clone])
    return clone


# ======================================================= context menu

def test_context_menu_structure_for_single_stroke(qtbot, tmp_path,
                                                  cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _context_menu(viewer, *_pick_widget(viewer, a))
    menu = _menu(viewer)
    assert menu is not None
    labels = _labels(menu)
    assert "Rotate" in labels
    assert "Recolor" in labels
    assert "Delete" in labels
    rotate = _submenu(menu, "Rotate")
    assert _labels(rotate) == ["Rotate Left 90°", "Rotate Right 90°",
                               "Rotate 180°"]
    recolor = _submenu(menu, "Recolor")
    assert _labels(recolor) == ["Stroke Color"]
    # Delete is last and carries the destructive affordance.
    actions = [a for a in menu.actions() if a.text()]
    assert actions[-1].text() == "Delete"
    assert not actions[-1].icon().isNull()
    _close_menu(viewer)


def test_fill_color_only_when_a_shape_supports_fill(qtbot, tmp_path,
                                                    cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    stroke = _seed(viewer, _pen_stroke())
    box = _seed(viewer, _box())

    viewer._selection.set([stroke.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    menu = viewer._build_selection_menu(ids, snaps)
    assert _labels(_submenu(menu, "Recolor")) == ["Stroke Color"]

    viewer._selection.set([box.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    menu = viewer._build_selection_menu(ids, snaps)
    assert _labels(_submenu(menu, "Recolor")) == ["Stroke Color",
                                                  "Fill Color"]
    # A line shape supports stroke but not fill.
    line = _seed(viewer, _box(kind=ShapeKind.LINE,
                              points=(Point(50.0, 350.0), Point(150.0, 360.0))))
    viewer._selection.set([line.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    menu = viewer._build_selection_menu(ids, snaps)
    assert _labels(_submenu(menu, "Recolor")) == ["Stroke Color"]


def test_right_click_selected_member_keeps_whole_selection(qtbot, tmp_path,
                                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(200.0, 150.0)))
    b = _seed(viewer, _pen_stroke(p0=(100.0, 260.0), p1=(200.0, 300.0)))
    viewer._selection.set([a.id, b.id])
    _context_menu(viewer, *_pick_widget(viewer, b))
    assert set(_selected_values(viewer)) == {a.id.value, b.id.value}
    menu = _menu(viewer)
    assert menu is not None
    assert _history_entries(viewer) == 0
    # The menu's Delete targets both members.
    _trigger(menu, ["Delete"])
    assert _store(viewer).get(a.id) is None
    assert _store(viewer).get(b.id) is None
    assert _history_entries(viewer) == 1
    _close_menu(viewer)


def test_right_click_unselected_selects_it(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(200.0, 150.0)))
    b = _seed(viewer, _pen_stroke(p0=(100.0, 260.0), p1=(200.0, 300.0)))
    viewer._selection.set([a.id])
    _context_menu(viewer, *_pick_widget(viewer, b))
    assert set(_selected_values(viewer)) == {b.id.value}
    assert _history_entries(viewer) == 0
    _close_menu(viewer)


def test_right_click_empty_page_area_clears_selection_page_menu(qtbot, tmp_path,
                                                               cleared_settings):
    """Stage 5.2: empty *page* area clears the selection, opens page menu.

    An empty area *inside a displayed page* targets that page, and the page
    background is not the selection's surface: the pending annotation
    selection is cleared (ids, frame, handles, animation) before the menu is
    shown. Nothing else is mutated, and the menu stays inert until an action
    is chosen.
    """
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    assert viewer._selection_handle_points() is not None
    empty = _widget(viewer, 560.0, 20.0)
    _context_menu(viewer, *empty)
    menu = _menu(viewer)
    assert menu is not None
    labels = _labels(menu)
    # The *page* menu, never the annotation menu.
    assert any("Rotate Page" in label for label in labels)
    assert any(label.startswith("Delete Page") for label in labels)
    assert "Delete" not in labels
    # The annotation selection is cleared; nothing else mutated.
    assert viewer._selection.has_selection is False
    assert viewer._selection.selected_ids == ()
    assert viewer._selection_handle_points() is None
    assert viewer._selection_timer.isActive() is False
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id).points == a.points
    _close_menu(viewer)


def test_right_click_outside_any_page_opens_nothing(qtbot, tmp_path,
                                                    cleared_settings):
    """Stage 5.1: a right-click off every page (and annotation) is inert."""
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    # Below the only page: rejected by the page mapping (vertical gap).
    outside = _widget(viewer, 300.0, 1200.0)
    _context_menu(viewer, *outside)
    assert _menu(viewer) is None
    assert set(_selected_values(viewer)) == {a.id.value}
    assert _history_entries(viewer) == 0



def test_menu_open_and_dismiss_is_inert(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _context_menu(viewer, *_pick_widget(viewer, a))
    assert _menu(viewer) is not None
    _close_menu(viewer)
    assert _menu(viewer) is None
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id) == a
    assert not viewer._selection.has_selection or \
        _selected_values(viewer) == (a.id.value,)


def test_context_menu_opens_once_per_gesture(qtbot, tmp_path,
                                             cleared_settings, monkeypatch):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    calls = {"n": 0}
    original = viewer._build_selection_menu

    def counting(ids, snapshots):
        calls["n"] += 1
        return original(ids, snapshots)

    monkeypatch.setattr(viewer, "_build_selection_menu", counting)
    point = _pick_widget(viewer, a)
    _context_menu(viewer, *point)
    first = _menu(viewer)
    assert calls["n"] == 1
    # A second context-menu event for the same gesture must not open a
    # second menu (Qt may deliver press-path and release-path events).
    _context_menu(viewer, *point)
    assert calls["n"] == 1
    assert _menu(viewer) is first
    _close_menu(viewer)


def test_keyboard_context_menu_targets_selection_or_page(qtbot, tmp_path,
                                                         cleared_settings):
    """Stage 5.1: keyboard invocation resolves from the current context.

    With a selection it opens the annotation menu; with none it opens the
    page menu for the current page instead of inventing a pointer.
    """
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    # No selection: the current page's menu opens.
    _context_menu(viewer)
    menu = _menu(viewer)
    assert menu is not None
    assert any(label.startswith("Delete Page") for label in _labels(menu))
    _close_menu(viewer)
    # With a selection: the annotation menu opens.
    viewer._selection.set([a.id])
    _context_menu(viewer)
    assert _menu(viewer) is not None
    assert "Delete" in _labels(_menu(viewer))
    _close_menu(viewer)


def test_context_menu_available_for_non_selection_tool(qtbot, tmp_path,
                                                       cleared_settings):
    """Stage 5.1: the annotation menu opens under *every* tool.

    A Pen tool has no selection affordance of its own, but a right-click
    on an annotation still selects and targets it (the active tool is
    unchanged).
    """
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer, *_pick_widget(viewer, a))
    menu = _menu(viewer)
    assert menu is not None
    assert "Delete" in _labels(menu)
    assert set(_selected_values(viewer)) == {a.id.value}
    # The active tool never changed.
    assert viewer._interaction.active_kind == ToolType.PEN
    _close_menu(viewer)
    # An empty page area under the Pen tool opens the page menu instead.
    _context_menu(viewer, *_widget(viewer, 560.0, 20.0))
    assert any(label.startswith("Delete Page")
               for label in _labels(_menu(viewer)))
    _close_menu(viewer)


@pytest.mark.parametrize("tool_factory", [_lasso_tool, _pan_tool])
def test_context_menu_available_for_lasso_and_pan(qtbot, tmp_path,
                                                  cleared_settings,
                                                  tool_factory):
    _window, viewer = _open_window(qtbot, tmp_path, tool=tool_factory())
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _context_menu(viewer, *_pick_widget(viewer, a))
    assert _menu(viewer) is not None
    assert "Delete" in _labels(_menu(viewer))
    _close_menu(viewer)


def test_right_click_never_starts_a_gesture(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    point = _pick_widget(viewer, a)
    with _chrome_hidden(viewer):
        _press(viewer, *point, button=Qt.MouseButton.RightButton)
        _release(viewer, *point, button=Qt.MouseButton.RightButton)
    assert viewer._move_gesture is None
    assert viewer._region_gesture is None
    assert viewer._mouse_stroke_open is False
    assert _history_entries(viewer) == 0
    # Selection preserved (right-click on a member never clears it).
    assert set(_selected_values(viewer)) == {a.id.value}


def test_right_click_does_not_draw_with_pen_tool(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_pen_tool())
    with _chrome_hidden(viewer):
        _press(viewer, 250.0, 250.0, button=Qt.MouseButton.RightButton)
        _release(viewer, 250.0, 250.0, button=Qt.MouseButton.RightButton)
    assert viewer._mouse_stroke_open is False
    assert _store(viewer).count(0) == 0


# ============================================================ delete

def test_delete_single_object_undo_redo(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    assert _delete_and_count(viewer) == 1
    assert _store(viewer).get(a.id) is None
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a
    assert viewer._document.annotations.locate(a.id) == (0, 0)
    viewer.document_history.redo()
    assert _store(viewer).get(a.id) is None
    assert _history_entries(viewer) == 1


def _delete_and_count(viewer):
    before = _history_entries(viewer)
    viewer._delete_selection()
    return _history_entries(viewer) - before


def test_delete_multi_same_page_restores_exact_order(qtbot, tmp_path,
                                                     cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(50.0, 50.0), p1=(90.0, 50.0)))
    b = _seed(viewer, _pen_stroke(p0=(50.0, 80.0), p1=(90.0, 80.0)))
    c = _seed(viewer, _pen_stroke(p0=(50.0, 110.0), p1=(90.0, 110.0)))
    # Delete the first and last (non-adjacent) leaves b.
    viewer._selection.set([a.id, c.id])
    assert _delete_and_count(viewer) == 1
    assert [x.id for x in _store(viewer).page(0)] == [b.id]
    viewer.document_history.undo()
    assert [x.id for x in _store(viewer).page(0)] == [a.id, b.id, c.id]
    assert _store(viewer).get(a.id) is a
    assert _store(viewer).get(c.id) is c
    viewer.document_history.redo()
    assert [x.id for x in _store(viewer).page(0)] == [b.id]


def test_delete_multi_page_one_entry(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[
        (600.0, 400.0), (600.0, 400.0)])
    a = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)), 0)
    b = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)), 1)
    viewer._selection.set([a.id, b.id])
    assert _delete_and_count(viewer) == 1
    assert _store(viewer).get(a.id) is None
    assert _store(viewer).get(b.id) is None
    viewer.document_history.undo()
    assert _store(viewer).locate(a.id) == (0, 0)
    assert _store(viewer).locate(b.id) == (1, 0)
    viewer.document_history.redo()
    assert _store(viewer).get(a.id) is None
    assert _store(viewer).get(b.id) is None


def test_delete_empty_selection_is_noop(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    _seed(viewer, _pen_stroke())
    assert viewer._delete_selection() is False
    assert _history_entries(viewer) == 0
    assert _store(viewer).count(0) == 1


def test_delete_all_stale_is_noop(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    b = _seed(viewer, _pen_stroke(p0=(50.0, 260.0), p1=(90.0, 260.0)))
    viewer._selection.set([a.id, b.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    _store(viewer).remove(a.id)
    _store(viewer).remove(b.id)
    assert viewer._delete_targets(ids, snaps, "Delete") is False
    assert _history_entries(viewer) == 0


def test_delete_preflight_failure_no_partial(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(50.0, 50.0), p1=(90.0, 50.0)))
    b = _seed(viewer, _pen_stroke(p0=(50.0, 80.0), p1=(90.0, 80.0)))
    c = _seed(viewer, _pen_stroke(p0=(50.0, 110.0), p1=(90.0, 110.0)))
    viewer._selection.set([a.id, b.id, c.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    # Replace the middle target: the snapshot no longer names the live
    # object, so the whole operation must abort before any mutation.
    _replace_in_store(viewer, b, color="#123456")
    assert viewer._delete_targets(ids, snaps, "Delete") is False
    assert _history_entries(viewer) == 0
    assert [x.id for x in _store(viewer).page(0)] == [a.id, b.id, c.id]


def test_keyboard_delete_uses_same_operation(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    _key(viewer, Qt.Key.Key_Delete)
    assert _store(viewer).get(a.id) is None
    assert _history_entries(viewer) == 1
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) is a


def test_keyboard_delete_without_selection_is_noop(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    _key(viewer, Qt.Key.Key_Delete)
    assert _store(viewer).get(a.id) is not None
    assert _history_entries(viewer) == 0


# ============================================================ recolor

def test_recolor_stroke_covers_ink_highlighter_and_shape(qtbot, tmp_path,
                                                         cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    ink = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)))
    high = _seed(viewer, _highlighter(p0=(60.0, 120.0), p1=(160.0, 120.0)))
    box = _seed(viewer, _box())
    viewer._selection.set([ink.id, high.id, box.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, stroke="#2b6cff") is True
    assert _history_entries(viewer) == 1
    assert _store(viewer).get(ink.id).color == "#2b6cff"
    assert _store(viewer).get(high.id).color == "#2b6cff"
    assert _store(viewer).get(box.id).color == "#2b6cff"
    # Non-color fields survive.
    assert _store(viewer).get(high.id).opacity == 0.4
    assert _store(viewer).get(ink.id).points == ink.points
    assert _store(viewer).get(box.id).fill_color == "#ffffff"
    viewer.document_history.undo()
    assert _store(viewer).get(ink.id).color == "#e01b24"
    assert _store(viewer).get(high.id).color == "#f5d90a"
    assert _store(viewer).get(high.id).opacity == 0.4
    viewer.document_history.redo()
    assert _store(viewer).get(high.id).color == "#2b6cff"


def test_recolor_mixed_cross_page(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[
        (600.0, 400.0), (600.0, 400.0)])
    ink = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)), 0)
    high = _seed(viewer, _highlighter(p0=(60.0, 60.0), p1=(160.0, 60.0)), 1)
    viewer._selection.set([ink.id, high.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, stroke="#8e4ec6") is True
    assert _history_entries(viewer) == 1
    assert _store(viewer).locate(ink.id)[0] == 0
    assert _store(viewer).locate(high.id)[0] == 1
    assert _store(viewer).get(ink.id).color == "#8e4ec6"
    assert _store(viewer).get(high.id).color == "#8e4ec6"
    viewer.document_history.undo()
    assert _store(viewer).get(ink.id).color == "#e01b24"
    assert _store(viewer).get(high.id).color == "#f5d90a"


def test_recolor_fill_leaves_non_fill_members_alone(qtbot, tmp_path,
                                                    cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    ink = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)))
    box = _seed(viewer, _box())
    viewer._selection.set([ink.id, box.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, fill="#30d158") is True
    assert _history_entries(viewer) == 1
    assert _store(viewer).get(box.id).fill_color == "#30d158"
    # Stroke colour untouched; the ink has no fill and is unchanged.
    assert _store(viewer).get(box.id).color == "#000000"
    assert _store(viewer).get(ink.id).color == "#e01b24"
    assert _store(viewer).get(ink.id) == ink


def test_recolor_noop_creates_no_history(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(color="#e01b24"))
    viewer._selection.set([a.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, stroke="#e01b24") is False
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id) is a


def test_recolor_preflight_failure_no_partial(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)))
    b = _seed(viewer, _pen_stroke(p0=(60.0, 90.0), p1=(120.0, 90.0)))
    viewer._selection.set([a.id, b.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    _replace_in_store(viewer, b, color="#123456")
    assert viewer._recolor_targets(ids, snaps, stroke="#2b6cff") is False
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id).color == "#e01b24"


# ============================================================ rotation

@pytest.mark.parametrize("angle,text", [
    (-90.0, "Rotate Left"),
    (90.0, "Rotate Right"),
    (180.0, "Rotate 180°"),
])
def test_rotate_single_stroke_about_group_centre(qtbot, tmp_path,
                                                 cleared_settings, angle, text):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0)))
    viewer._selection.set([a.id])
    px, py = _shared_pivot(viewer, [a])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, angle, text) is True
    assert _history_entries(viewer) == 1
    after = _store(viewer).get(a.id)
    ox, oy = _offset(viewer, 0)
    for before_pt, after_pt in zip(a.points, after.points):
        expected = rotate_point(
            Point(before_pt.x + ox, before_pt.y + oy), px, py, angle)
        assert after_pt.x == pytest.approx(expected.x - ox, abs=1e-6)
        assert after_pt.y == pytest.approx(expected.y - oy, abs=1e-6)
    # Width and identity survive a pure rotation.
    assert after.width_mm == a.width_mm
    assert after.id == a.id
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) == a
    viewer.document_history.redo()
    assert _store(viewer).get(a.id) == after


def test_rotate_mixed_cross_page_preserves_pages_and_distances(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[
        (600.0, 400.0), (600.0, 400.0)])
    a = _seed(viewer, _pen_stroke(p0=(80.0, 80.0), p1=(160.0, 80.0)), 0)
    b = _seed(viewer, _pen_stroke(p0=(80.0, 80.0), p1=(140.0, 140.0)), 1)
    viewer._selection.set([a.id, b.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 90.0, "Rotate Right") is True
    assert _history_entries(viewer) == 1
    ra = _store(viewer).get(a.id)
    rb = _store(viewer).get(b.id)
    # Page assignment unchanged.
    assert _store(viewer).locate(a.id)[0] == 0
    assert _store(viewer).locate(b.id)[0] == 1
    # Shared-space pairwise distance is preserved by a rigid rotation.
    oa = _offset(viewer, 0)
    ob = _offset(viewer, 1)
    before = ((a.points[0].x + oa[0] - b.points[0].x - ob[0]) ** 2
              + (a.points[0].y + oa[1] - b.points[0].y - ob[1]) ** 2)
    after = ((ra.points[0].x + oa[0] - rb.points[0].x - ob[0]) ** 2
             + (ra.points[0].y + oa[1] - rb.points[0].y - ob[1]) ** 2)
    assert after == pytest.approx(before, abs=1e-6)
    viewer.document_history.undo()
    assert _store(viewer).get(a.id) == a
    assert _store(viewer).get(b.id) == b


def test_rotate_rectangle_accumulates_rotation_deg(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _box(rect=(300.0, 100.0, 420.0, 200.0)))
    # A second member so the group centre is not the box centre.
    companion = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(160.0, 100.0)))
    viewer._selection.set([box.id, companion.id])
    px, py = _shared_pivot(viewer, [box, companion])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 90.0, "Rotate Right") is True
    after = _store(viewer).get(box.id)
    assert after.kind == ShapeKind.RECTANGLE
    assert after.rotation_deg == pytest.approx(90.0, abs=1e-6)
    # The unrotated rect dims are unchanged; only the orbit moves.
    assert after.rect.width == pytest.approx(box.rect.width, abs=1e-6)
    assert after.rect.height == pytest.approx(box.rect.height, abs=1e-6)
    # The box centre orbits the shared pivot (shared space).
    ox, oy = _offset(viewer, 0)
    before_centre = Point(box.rect.origin.x + box.rect.width / 2.0 + ox,
                          box.rect.origin.y + box.rect.height / 2.0 + oy)
    after_centre = Point(after.rect.origin.x + after.rect.width / 2.0 + ox,
                         after.rect.origin.y + after.rect.height / 2.0 + oy)
    expected = rotate_point(before_centre, px, py, 90.0)
    assert after_centre.x == pytest.approx(expected.x, abs=1e-6)
    assert after_centre.y == pytest.approx(expected.y, abs=1e-6)


def test_rotate_preserves_paint_order(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)))
    b = _seed(viewer, _highlighter(p0=(60.0, 120.0), p1=(160.0, 120.0)))
    c = _seed(viewer, _pen_stroke(p0=(60.0, 180.0), p1=(120.0, 180.0)))
    order = [x.id for x in _store(viewer).page(0)]
    viewer._selection.set([a.id, b.id, c.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 180.0, "Rotate 180°") is True
    assert [x.id for x in _store(viewer).page(0)] == order


def test_rotate_updates_oriented_frame(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0)))
    viewer._selection.set([a.id])
    corners_before = viewer._selection_frame_corners()
    assert corners_before is not None
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 90.0, "Rotate Right") is True
    corners_after = viewer._selection_frame_corners()
    assert corners_after is not None
    # The persistent frame carries the rotation.
    assert viewer._frame_state is not None
    assert viewer._frame_state["angle"] == pytest.approx(90.0, abs=1e-6)
    # The rotated frame encloses the rotated annotation (its new extent).
    after = _store(viewer).get(a.id)
    rect = viewer._selection_frame_widget_rect()
    assert rect is not None
    left, top, width, height = rect
    ax, ay = _pick_widget(viewer, after)
    assert left - 2.0 <= ax <= left + width + 2.0
    assert top - 2.0 <= ay <= top + height + 2.0


def test_rotate_empty_selection_is_noop(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    _seed(viewer, _pen_stroke())
    ids = tuple()
    assert viewer._rotate_selection(ids, {}, 90.0, "Rotate") is False
    assert _history_entries(viewer) == 0


def test_rotate_full_turn_creates_no_entry(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    viewer._selection.set([a.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 360.0, "Rotate") is True
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id) == a


def test_rotate_preflight_failure_no_partial(qtbot, tmp_path,
                                             cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(60.0, 60.0), p1=(120.0, 60.0)))
    b = _seed(viewer, _pen_stroke(p0=(60.0, 90.0), p1=(120.0, 90.0)))
    viewer._selection.set([a.id, b.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    _replace_in_store(viewer, b, color="#123456")
    assert viewer._rotate_selection(ids, snaps, 90.0, "Rotate Right") is False
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id).points == a.points


def test_rotate_then_hit_testing_reflects_new_geometry(qtbot, tmp_path,
                                                       cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0)))
    viewer._selection.set([a.id])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 90.0, "Rotate Right") is True
    after = _store(viewer).get(a.id)
    hit = viewer._annotation_at_widget(*_pick_widget(viewer, after))
    assert hit is not None
    assert hit.id == a.id


def test_rotate_reuses_transform_helpers_cross_page_pivot(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[
        (600.0, 400.0), (600.0, 400.0)])
    a = _seed(viewer, _pen_stroke(p0=(80.0, 80.0), p1=(200.0, 80.0)), 0)
    b = _seed(viewer, _pen_stroke(p0=(80.0, 80.0), p1=(200.0, 80.0)), 1)
    viewer._selection.set([a.id, b.id])
    px, py = _shared_pivot(viewer, [a, b])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, -90.0, "Rotate Left") is True
    ra = _store(viewer).get(a.id)
    oa = _offset(viewer, 0)
    expected = rotate_point(
        Point(a.points[0].x + oa[0], a.points[0].y + oa[1]), px, py, -90.0)
    assert ra.points[0].x == pytest.approx(expected.x - oa[0], abs=1e-6)
    assert ra.points[0].y == pytest.approx(expected.y - oa[1], abs=1e-6)


def test_rotate_ellipse_and_highlighter(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    ellipse = _seed(viewer, _box(kind=ShapeKind.ELLIPSE,
                                 rect=(300.0, 100.0, 420.0, 200.0)))
    high = _seed(viewer, _highlighter(p0=(100.0, 100.0), p1=(200.0, 100.0)))
    viewer._selection.set([ellipse.id, high.id])
    px, py = _shared_pivot(viewer, [ellipse, high])
    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._rotate_selection(ids, snaps, 180.0, "Rotate 180°") is True
    after_ellipse = _store(viewer).get(ellipse.id)
    after_high = _store(viewer).get(high.id)
    assert after_ellipse.kind == ShapeKind.ELLIPSE
    assert after_ellipse.rotation_deg == pytest.approx(180.0, abs=1e-6)
    assert after_ellipse.rect.width == pytest.approx(ellipse.rect.width,
                                                     abs=1e-6)
    assert isinstance(after_high, Highlighter)
    assert after_high.opacity == 0.4
    oh = _offset(viewer, 0)
    expected = rotate_point(
        Point(high.points[0].x + oh[0], high.points[0].y + oh[1]),
        px, py, 180.0)
    assert after_high.points[0].x == pytest.approx(expected.x - oh[0], abs=1e-6)
    assert after_high.points[0].y == pytest.approx(expected.y - oh[1], abs=1e-6)


def test_recolor_stroke_and_fill_are_independent(qtbot, tmp_path,
                                                 cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    box = _seed(viewer, _box(color="#000000", fill_color="#ffffff"))
    viewer._selection.set([box.id])

    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, stroke="#2b6cff") is True
    after_stroke = _store(viewer).get(box.id)
    assert after_stroke.color == "#2b6cff"
    assert after_stroke.fill_color == "#ffffff"

    ids = tuple(viewer._selection.selected_ids)
    snaps = viewer._selection_target_snapshots(ids)
    assert viewer._recolor_targets(ids, snaps, fill="#30d158") is True
    after_fill = _store(viewer).get(box.id)
    assert after_fill.color == "#2b6cff"
    assert after_fill.fill_color == "#30d158"
    # Two independent entries; undo restores colour step by step.
    assert _history_entries(viewer) == 2
    viewer.document_history.undo()
    assert _store(viewer).get(box.id).fill_color == "#ffffff"
    assert _store(viewer).get(box.id).color == "#2b6cff"
    viewer.document_history.undo()
    assert _store(viewer).get(box.id).color == "#000000"
    assert _store(viewer).get(box.id).fill_color == "#ffffff"
