"""M6.8.2 Stage 5.1: context-menu routing + page rotate/delete wiring.

Widget-level coverage for the two Stage 5.1 behaviours:

* the annotation context menu opens under *every* active tool (not just
  the selection-capable ones), while the active tool is unchanged and
  the right-click still starts no gesture;
* the page context menu opens on an empty area of the page under the
  pointer (Rotate Page CW/CCW/180, Delete Page), targets only that page,
  is inert on open/dismiss, opens nothing outside every page, and
  disables Delete Page for the last remaining page;
* page rotation and deletion are single undoable steps with exact
  undo/redo, per-page isolation, selection self-healing, layout/cache
  rebuild, and hit-testing that follows the new frame.

Pure geometry and the document layer are covered Qt-free in
``tests/test_page_ops_m682_stage5_1.py``.
"""

from __future__ import annotations

import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from annotations.models import InkStroke, Shape, ShapeKind
from core.types import Point, Rect, Size
from document.models import page_id_for
from tests.helpers import make_sized_pdf
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


ALL_TOOL_KINDS = (
    ToolType.PAN, ToolType.SELECTION, ToolType.LASSO, ToolType.PEN,
    ToolType.HIGHLIGHTER, ToolType.ERASER, ToolType.LINE,
    ToolType.RECTANGLE, ToolType.ELLIPSE, ToolType.ARROW,
    ToolType.POLYGON, ToolType.NOTE,
)


def _tool(kind):
    return ToolInstance(kind=kind, name=str(kind))


def _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0), **kw):
    base = dict(points=(Point(*p0), Point(*p1)), color="#e01b24",
                width_mm=1.0, opacity=1.0)
    base.update(kw)
    return InkStroke(**base)


# -------------------------------------------------------- viewer helpers

def _open_window_with_path(qtbot, tmp_path, tool=None, sizes=None,
                           name="s51.pdf", height=1200):
    from app.main_window import MainWindow

    if sizes is None:
        sizes = [(400.0, 300.0)] * 3
    pdf = make_sized_pdf(str(tmp_path / name), sizes)
    window = MainWindow()
    window.resize(900, height)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool if tool is not None else _tool(
        ToolType.SELECTION))
    qtbot.wait(50)
    return window, viewer, pdf


def _open_window(qtbot, tmp_path, tool=None, sizes=None, name="s51.pdf",
                 height=1200):
    window, viewer, _pdf = _open_window_with_path(
        qtbot, tmp_path, tool=tool, sizes=sizes, name=name, height=height)
    return window, viewer


def _store(viewer):
    return viewer._document.annotations


def _index_of(viewer, annotation):
    return viewer._page_index_for(annotation.page_id)


def _seed(viewer, annotation, page_index=0):
    entry_page = viewer._pipeline.layout.entry(page_index).page_id
    stored = dataclasses.replace(annotation, page_id=entry_page)
    _store(viewer).add(stored, page_index)
    return stored


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
    """A widget point that reliably hits ``annotation`` (rotation agnostic)."""
    index = _index_of(viewer, annotation)
    if isinstance(annotation, Shape):
        rect = annotation.rect
        lx = rect.origin.x + rect.width / 2.0
        ly = rect.origin.y + rect.height / 2.0
    else:
        pts = annotation.points
        mid = max(1, len(pts) // 2) if len(pts) > 1 else 0
        a, b = pts[mid - 1], pts[mid]
        lx = (a.x + b.x) / 2.0
        ly = (a.y + b.y) / 2.0
    return _widget(viewer, lx, ly, index)


# ------------------------------------------------------- event injection

def _press(viewer, x, y, button=Qt.MouseButton.RightButton):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        button, button, Qt.KeyboardModifier.NoModifier))


def _release(viewer, x, y, button=Qt.MouseButton.RightButton):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        button, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _move(viewer, x, y, button=Qt.MouseButton.RightButton):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, button,
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


def _menu(viewer):
    return viewer._selection_menu


def _labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def _action(menu, text):
    for action in menu.actions():
        if action.text() == text:
            return action
    return None


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
    raise AssertionError(
        f"no action {path!r} in {[a.text() for a in target.actions()]}")


def _close_menu(viewer):
    menu = viewer._selection_menu
    if menu is not None:
        menu.close()
        menu.hide()
    viewer._on_selection_menu_hidden()
    QApplication.processEvents()


def _history_entries(viewer):
    return viewer.document_history.stack.count()


def _selected_values(viewer):
    return tuple(a.value for a in viewer._selection.selected_ids)


# ============================================ annotation menu under a tool

@pytest.mark.parametrize("kind", ALL_TOOL_KINDS)
def test_annotation_menu_opens_for_every_tool(qtbot, tmp_path,
                                              cleared_settings, kind):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_tool(kind))
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer, *_pick_widget(viewer, a))
    menu = _menu(viewer)
    assert menu is not None, kind
    assert "Delete" in _labels(menu)
    # The active tool is unchanged by a right-click.
    assert viewer._interaction.active_kind == kind
    # The annotation was selected (single-click semantics) but nothing
    # was committed.
    assert set(_selected_values(viewer)) == {a.id.value}
    assert _history_entries(viewer) == 0
    _close_menu(viewer)


@pytest.mark.parametrize("kind", ALL_TOOL_KINDS)
def test_right_click_on_empty_page_area_is_a_page_menu_for_every_tool(
        qtbot, tmp_path, cleared_settings, kind):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_tool(kind))
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0))
    menu = _menu(viewer)
    assert menu is not None, kind
    labels = _labels(menu)
    assert any("Rotate Page" in label for label in labels)
    assert any(label.startswith("Delete Page") for label in labels)
    assert "Delete" not in labels
    # Never mutates the annotation selection, the store, or history.
    assert not viewer._selection.has_selection
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id) is a
    _close_menu(viewer)


@pytest.mark.parametrize("kind", ALL_TOOL_KINDS)
def test_right_click_never_draws_for_any_tool(qtbot, tmp_path,
                                              cleared_settings, kind):
    _window, viewer = _open_window(qtbot, tmp_path, tool=_tool(kind))
    a = _seed(viewer, _pen_stroke())
    point = _widget(viewer, 360.0, 20.0)
    _press(viewer, *point)
    _release(viewer, *point)
    _context_menu(viewer, *point)
    assert _history_entries(viewer) == 0
    assert len(_store(viewer)) == 1
    assert _store(viewer).get(a.id) is a
    _close_menu(viewer)


@pytest.mark.parametrize(
    "kind", (ToolType.SELECTION, ToolType.PAN, ToolType.PEN,
             ToolType.ERASER))
def test_right_drag_on_a_selected_annotation_never_transforms_or_pans(
        qtbot, tmp_path, cleared_settings, kind):
    """A right-drag over the (selected) frame must not move/resize it."""
    _window, viewer = _open_window(qtbot, tmp_path, tool=_tool(kind))
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(300.0, 200.0)))
    viewer._selection.set([a.id])
    scroll_before = viewer._scroll_doc()
    start = _pick_widget(viewer, a)
    end = (start[0] + 50.0, start[1] + 40.0)
    _press(viewer, *start)
    _move(viewer, (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    _move(viewer, *end)
    _release(viewer, *end)
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id).points[0] == a.points[0]
    assert viewer._move_gesture is None
    assert viewer._scroll_doc() == scroll_before
    assert viewer._selection.selected_ids == (a.id,)


# ============================================ page menu targeting

def test_page_menu_targets_the_pointer_page(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    assert viewer._document.page_count == 3
    for index in range(3):
        _context_menu(viewer, *_widget(viewer, 360.0, 20.0, page_index=index))
        menu = _menu(viewer)
        assert menu is not None
        assert f"Page {index + 1} of 3" in _labels(menu)
        assert f"Delete Page {index + 1}" in _labels(menu)
        _close_menu(viewer)


def test_page_menu_never_opens_for_an_annotation_hit(qtbot, tmp_path,
                                                     cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer, *_pick_widget(viewer, a))
    menu = _menu(viewer)
    assert menu is not None
    assert "Delete" in _labels(menu)  # annotation menu
    assert not any("Rotate Page" in label for label in _labels(menu))
    _close_menu(viewer)


def test_page_menu_opens_once_per_right_click(qtbot, tmp_path,
                                              cleared_settings, monkeypatch):
    _window, viewer = _open_window(qtbot, tmp_path)
    calls = {"n": 0}
    original = viewer._build_page_menu

    def counting(page_index):
        calls["n"] += 1
        return original(page_index)

    monkeypatch.setattr(viewer, "_build_page_menu", counting)
    point = _widget(viewer, 360.0, 20.0)
    _context_menu(viewer, *point)
    first = _menu(viewer)
    assert calls["n"] == 1
    _context_menu(viewer, *point)
    assert calls["n"] == 1
    assert _menu(viewer) is first
    _close_menu(viewer)


def test_page_menu_open_and_dismiss_is_inert(qtbot, tmp_path,
                                            cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0))
    assert _menu(viewer) is not None
    _close_menu(viewer)
    assert _menu(viewer) is None
    assert _history_entries(viewer) == 0
    assert _store(viewer).get(a.id) is a
    assert viewer._document.page_count == 3


def test_delete_page_disabled_on_the_last_page(qtbot, tmp_path,
                                              cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path,
                                   sizes=[(400.0, 300.0)])
    assert viewer._document.page_count == 1
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0))
    menu = _menu(viewer)
    assert menu is not None
    delete = _action(menu, "Delete Page 1")
    assert delete is not None
    assert not delete.isEnabled()
    # The direct call is guarded too.
    assert viewer._delete_page(0, "Delete Page") is False
    assert viewer._document.page_count == 1
    _close_menu(viewer)


def test_keyboard_context_menu_prefers_selection_then_page(qtbot, tmp_path,
                                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    _context_menu(viewer)
    menu = _menu(viewer)
    assert menu is not None
    assert any(label.startswith("Delete Page") for label in _labels(menu))
    _close_menu(viewer)
    viewer._selection.set([a.id])
    _context_menu(viewer)
    assert "Delete" in _labels(_menu(viewer))
    _close_menu(viewer)


# ============================================ page rotation wiring

def test_rotate_page_action_swaps_layout_and_remaps_annotation(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path,
                                   sizes=[(400.0, 300.0), (400.0, 300.0)])
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(150.0, 130.0)))
    before = viewer._pipeline.layout.entry(0)
    assert before.effective_rotation == 0
    # Probe a point that stays inside the rotated frame: unrotated (100,100)
    # displays at (H - 100, 100) = (200, 100) after a 90 deg clockwise turn.
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0))
    menu = _menu(viewer)
    rotate = _submenu(menu, "Rotate Page")
    assert _labels(rotate) == ["Rotate Clockwise 90°",
                              "Rotate Counterclockwise 90°",
                              "Rotate 180°"]
    _trigger(menu, ["Rotate Page", "Rotate Clockwise 90°"])
    _close_menu(viewer)

    assert _history_entries(viewer) == 1
    entry = viewer._pipeline.layout.entry(0)
    assert entry.effective_rotation == 90
    # Quarter turn swaps the displayed page dimensions.
    assert (entry.width_pt, entry.height_pt) == (400.0, 300.0)
    assert (entry.render_width_px, entry.render_height_px) == (300, 400)
    # The annotation follows the content.
    moved = _store(viewer).get(a.id)
    assert abs(moved.points[0].x - 200.0) < 1e-6
    assert abs(moved.points[0].y - 100.0) < 1e-6
    # Hit-testing agrees with the new frame.
    hit = viewer._annotation_at_widget(*_widget(viewer, 200.0, 100.0))
    assert hit is not None and hit.id.value == a.id.value
    # Other pages are untouched.
    assert viewer._pipeline.layout.entry(1).effective_rotation == 0


def test_rotate_page_undo_redo_is_exact(qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path,
                                   sizes=[(400.0, 300.0)])
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(150.0, 130.0)))
    viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    assert viewer._pipeline.layout.entry(0).effective_rotation == 90
    viewer._on_undo_requested()
    assert viewer._pipeline.layout.entry(0).effective_rotation == 0
    back = _store(viewer).get(a.id)
    assert abs(back.points[0].x - 100.0) < 1e-6
    assert abs(back.points[0].y - 100.0) < 1e-6
    assert _history_entries(viewer) == 1
    viewer._on_redo_requested()
    assert viewer._pipeline.layout.entry(0).effective_rotation == 90
    again = _store(viewer).get(a.id)
    assert abs(again.points[0].x - 200.0) < 1e-6


def test_rotate_page_three_times_is_a_full_turn(qtbot, tmp_path,
                                                cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path,
                                   sizes=[(400.0, 300.0)])
    a = _seed(viewer, _pen_stroke())
    for _ in range(3):
        viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    assert viewer._pipeline.layout.entry(0).effective_rotation == 270
    for _ in range(3):
        viewer._on_undo_requested()
    assert viewer._pipeline.layout.entry(0).effective_rotation == 0
    back = _store(viewer).get(a.id)
    assert abs(back.points[0].x - a.points[0].x) < 1e-6
    assert abs(back.points[0].y - a.points[0].y) < 1e-6


# ============================================ page deletion wiring

def test_delete_page_action_removes_page_and_annotations(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(), page_index=0)
    b = _seed(viewer, _pen_stroke(p0=(100.0, 40.0), p1=(150.0, 60.0)),
              page_index=2)
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0, page_index=0))
    menu = _menu(viewer)
    _trigger(menu, ["Delete Page 1"])
    _close_menu(viewer)

    assert viewer._document.page_count == 2
    assert viewer._pipeline.layout.page_count == 2
    assert _history_entries(viewer) == 1
    assert _store(viewer).get(a.id) is None
    survivor = _store(viewer).get(b.id)
    # Identity is preserved; the value is re-keyed to its new page index.
    assert survivor.id == b.id
    assert _store(viewer).locate(b.id) == (1, 0)
    assert survivor.page_id == page_id_for(viewer._document.id, 1)
    # Layout page ids follow the surviving pages.
    assert (viewer._pipeline.layout.entry(0).page_id
            == page_id_for(viewer._document.id, 0))


def test_delete_page_selection_self_heals_across_pages(qtbot, tmp_path,
                                                       cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(), page_index=0)
    b = _seed(viewer, _pen_stroke(p0=(100.0, 40.0), p1=(150.0, 60.0)),
              page_index=1)
    viewer._selection.set([a.id, b.id])
    assert viewer._delete_page(0, "Delete Page") is True
    # The deleted page's annotation left the selection; the survivor stayed.
    assert _selected_values(viewer) == (b.id.value,)
    assert viewer._document.page_count == 2


def test_delete_page_undo_restores_page_and_annotations(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(), page_index=1)
    b = _seed(viewer, _pen_stroke(p0=(100.0, 240.0), p1=(150.0, 260.0)),
              page_index=1)
    c = _seed(viewer, _pen_stroke(), page_index=2)
    entries = [viewer._pipeline.layout.entry(i).page_id for i in range(3)]
    assert viewer._delete_page(1, "Delete Page") is True
    assert viewer._document.page_count == 2

    viewer._on_undo_requested()
    assert viewer._document.page_count == 3
    assert viewer._pipeline.layout.page_count == 3
    assert _store(viewer).page(1) == (a, b)
    assert _store(viewer).locate(c.id) == (2, 0)
    assert [viewer._pipeline.layout.entry(i).page_id for i in range(3)] == \
        entries
    # Redo removes it again.
    viewer._on_redo_requested()
    assert viewer._document.page_count == 2
    assert _store(viewer).get(a.id) is None
    assert _store(viewer).locate(c.id) == (1, 0)


def test_delete_page_remaps_later_annotations_and_frame_state(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    c = _seed(viewer, _pen_stroke(p0=(100.0, 200.0), p1=(160.0, 240.0)),
              page_index=2)
    viewer._delete_page(0, "Delete Page")
    assert viewer._document.page_count == 2
    assert _store(viewer).locate(c.id) == (1, 0)
    assert _store(viewer).get(c.id).page_id == \
        page_id_for(viewer._document.id, 1)
    # A previously cached frame orientation must not survive the reindex.
    assert viewer._frame_state is None


def test_delete_last_page_leaves_the_first(qtbot, tmp_path,
                                           cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(), page_index=0)
    b = _seed(viewer, _pen_stroke(), page_index=2)
    assert viewer._delete_page(2, "Delete Page") is True
    assert viewer._document.page_count == 2
    assert _store(viewer).get(b.id) is None
    assert _store(viewer).get(a.id) is a


def test_page_menu_rotation_and_deletion_are_separate_steps(
        qtbot, tmp_path, cleared_settings):
    """Rotate then delete then undo keeps each step independent."""
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke(), page_index=0)
    viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    viewer._delete_page(1, "Delete Page")
    assert _history_entries(viewer) == 2
    assert viewer._document.page_count == 2
    assert viewer._pipeline.layout.entry(0).effective_rotation == 90
    # Undo the deletion only.
    viewer._on_undo_requested()
    assert viewer._document.page_count == 3
    assert viewer._pipeline.layout.entry(0).effective_rotation == 90
    assert _store(viewer).get(a.id) is not None


# ============================================ in-memory / persistence


def test_page_rotation_is_in_memory_and_never_rewrites_the_source(
        qtbot, tmp_path, cleared_settings):
    """The same Document is kept open and only a working copy changes.

    Inkfall has no save/export workflow: a page edit is session-local
    and undoable. This test pins that contract down — the ``Document``
    object (and its ``PdfBackend`` handle) is never re-opened, the
    render pipeline is repointed at a fresh working copy, and the file
    on disk is left byte-for-byte untouched.
    """
    from document.backend import PdfBackend

    _window, viewer, pdf = _open_window_with_path(
        qtbot, tmp_path, sizes=[(400.0, 300.0)])
    document = viewer._document
    assert document.render_path == document.normalized_path
    original_bytes = open(pdf, "rb").read()

    viewer._rotate_page(0, 90, "Rotate Page Clockwise")

    # Same Document/backend object: nothing was re-opened.
    assert viewer._document is document
    assert document.page_info(0).rotation == 90
    # The pipeline renders through the working copy, not the raw file.
    assert document.render_path != document.normalized_path
    assert viewer._pipeline._source_path == document.render_path
    # The source file on disk is untouched.
    assert open(pdf, "rb").read() == original_bytes
    backend = PdfBackend.open(pdf)
    try:
        assert backend.page_info(0).rotation == 0
    finally:
        backend.close()


def test_working_copy_is_removed_on_close(qtbot, tmp_path, cleared_settings):
    """Page edits leave no temp files behind once the document closes."""
    import os

    _window, viewer, _pdf = _open_window_with_path(
        qtbot, tmp_path, sizes=[(400.0, 300.0), (400.0, 300.0)])
    document = viewer._document
    viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    viewer._delete_page(1, "Delete Page")
    work = document.render_path
    assert work != document.normalized_path
    assert os.path.exists(work)
    # Closing through the app path releases the pipeline (worker file
    # handles included) before the document prunes its working copies.
    viewer.close_document()
    assert not os.path.exists(work)


# ============================================ invalid / no-op guards


def test_invalid_page_op_is_a_noop_without_history(qtbot, tmp_path,
                                                   cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path)
    a = _seed(viewer, _pen_stroke())
    # Non-quarter turn and out-of-range rotations change nothing.
    assert viewer._rotate_page(0, 360, "Rotate Page") is False
    assert viewer._rotate_page(0, 0, "Rotate Page") is False
    assert viewer._rotate_page(99, 90, "Rotate Page") is False
    # Out-of-range deletion and deleting the only page too.
    assert viewer._delete_page(99, "Delete Page") is False
    assert _history_entries(viewer) == 0
    assert viewer._document.page_count == 3
    assert viewer._pipeline.layout.entry(0).effective_rotation == 0
    assert _store(viewer).get(a.id) is a


# ============================================ rotated page-frame targeting


def test_page_menu_targeting_follows_a_rotated_frame(qtbot, tmp_path,
                                                     cleared_settings):
    """After a quarter turn the pointer mapping uses the swapped frame."""
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[(400.0, 300.0)])
    viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    entry = viewer._pipeline.layout.entry(0)
    # The displayed frame is now 300 x 400 pt (unrotated media stays 400
    # x 300): the mapping must reject the old horizontal extent.
    assert (entry.width_pt, entry.height_pt) == (400.0, 300.0)
    assert (entry.render_width_px, entry.render_height_px) == (300, 400)

    # Inside the unrotated frame but outside the rotated one: no menu.
    _context_menu(viewer, *_widget(viewer, 360.0, 20.0))
    assert _menu(viewer) is None

    # Inside the rotated frame: the page menu opens for this page.
    _context_menu(viewer, *_widget(viewer, 150.0, 380.0))
    menu = _menu(viewer)
    assert menu is not None
    assert "Page 1 of 1" in _labels(menu)
    _close_menu(viewer)


def test_selection_survives_a_page_rotation_and_hit_testing_follows(
        qtbot, tmp_path, cleared_settings):
    _window, viewer = _open_window(qtbot, tmp_path, sizes=[(400.0, 300.0)])
    a = _seed(viewer, _pen_stroke(p0=(100.0, 100.0), p1=(150.0, 130.0)))
    viewer._selection.set([a.id])
    viewer._rotate_page(0, 90, "Rotate Page Clockwise")
    assert viewer._selection.selected_ids == (a.id,)
    # The cached frame orientation is dropped and recomputed.
    assert viewer._frame_state is None
    hit = viewer._annotation_at_widget(*_widget(viewer, 200.0, 100.0))
    assert hit is not None and hit.id.value == a.id.value


# ============================================ write-failure containment


def test_failed_page_write_leaves_no_history_and_no_mutation(
        qtbot, tmp_path, cleared_settings, monkeypatch):
    """A failed working-copy write is contained: no entry, no change."""
    _window, viewer = _open_window(qtbot, tmp_path,
                                   sizes=[(400.0, 300.0), (400.0, 300.0)])
    a = _seed(viewer, _pen_stroke(), page_index=0)

    def boom(_self, _target):
        raise RuntimeError("simulated working-copy write failure")

    monkeypatch.setattr(type(viewer._document._backend), "save_copy", boom)

    assert viewer._rotate_page(0, 90, "Rotate Page Clockwise") is False
    assert viewer._delete_page(1, "Delete Page") is False

    assert _history_entries(viewer) == 0
    assert viewer.document_history.stack.canUndo() is False
    assert viewer._document.page_count == 2
    assert viewer._document.page_info(0).rotation == 0
    # The view was rebuilt from the unchanged document and stays usable.
    assert viewer._pipeline is not None
    assert viewer._pipeline.layout.page_count == 2
    assert viewer._pipeline.layout.entry(0).effective_rotation == 0
    assert (viewer._pipeline.layout.entry(0).render_width_px,
            viewer._pipeline.layout.entry(0).render_height_px) == (400, 300)
    assert _store(viewer).get(a.id) is a
