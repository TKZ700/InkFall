"""M6.5 stroke-level Eraser integration.

The Eraser detects committed Pen/Highlighter strokes intersected by its
path and removes the *entire* canonical annotation through the existing
document-owned ``AnnotationStore`` / ``DocumentHistory`` delete commands.
One continuous gesture is one undo step, no matter how many strokes it
touches; a gesture that hits nothing leaves no history entry.

Coverage: handler path capture (Qt-free), geometric hit-testing
(including pressure-variable widths and near misses), gesture semantics
and grouping, identity/order/state, lifecycle safety, and rendering.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from annotations.interaction import (
    EraserHandler,
    EraserSegment,
    InteractionController,
)
from annotations.models import Highlighter, InkStroke
from core.types import PageId, Point
from tests.helpers import committed_strokes, make_probe_pdf, make_sized_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------- fixtures

def _pen_tool(color="#ff0000", width_mm=1.0, opacity=0.9, pressure=False):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color=color,
                        width_mm=width_mm, opacity=opacity,
                        pressure=pressure)


def _hl_tool(color="#FFFF00", width=3.0, opacity=0.4):
    return ToolInstance(kind=ToolType.HIGHLIGHTER, name="Highlight",
                        color=color, width_mm=width, opacity=opacity)


def _eraser_tool(width_mm=2.0):
    return ToolInstance(kind=ToolType.ERASER, name="Eraser",
                        width_mm=width_mm)


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


def _draw(viewer, coords):
    _press(viewer, *coords[0])
    for c in coords[1:-1]:
        _move(viewer, *c)
    _release(viewer, *coords[-1])


def _open_window(qtbot, tmp_path, tool=None, name="m65.pdf"):
    from app.main_window import MainWindow

    pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool or _eraser_tool())
    qtbot.wait(120)
    return window, viewer


def _page_id(viewer, page_index=0):
    return viewer._pipeline.layout.entry(page_index).page_id


def _store(viewer):
    return viewer._document.annotations


def _add_stroke(viewer, points, page_index=0, **kwargs):
    """Add a committed pen stroke directly to the store (no Add command).

    Bypassing the history keeps the store as the single authoritative
    source while letting eraser tests observe only the erase commands.
    """
    kwargs.setdefault("color", "#ff0000")
    kwargs.setdefault("width_mm", 1.0)
    kwargs.setdefault("opacity", 0.9)
    stroke = InkStroke(page_id=_page_id(viewer, page_index),
                       points=tuple(Point(float(x), float(y))
                                    for x, y in points), **kwargs)
    _store(viewer).add(stroke, page_index)
    return stroke


def _add_highlighter(viewer, points, page_index=0, **kwargs):
    kwargs.setdefault("color", "#FFFF00")
    kwargs.setdefault("width_mm", 3.0)
    kwargs.setdefault("opacity", 0.4)
    hl = Highlighter(page_id=_page_id(viewer, page_index),
                     points=tuple(Point(float(x), float(y))
                                  for x, y in points), **kwargs)
    _store(viewer).add(hl, page_index)
    return hl


def _erase_line(viewer, page_id, x0, y0, x1, y1):
    """Drive one eraser segment in page-local pt through the real handler."""
    it = viewer._interaction
    viewer._apply_intent(it.handle_eraser_press(page_id, x0, y0))
    viewer._apply_intent(it.handle_eraser_move(page_id, x1, y1))
    viewer._apply_intent(it.handle_eraser_release(page_id, x1, y1))


def _page_pt_to_widget(viewer, lx, ly, page_index=0):
    """Invert ``_event_to_page_pt`` for one page (test mapping helper)."""
    from rendering.layout import DocumentLayout

    layout = viewer._pipeline.layout
    entry = layout.entry(page_index)
    scale = viewer._display_scale()
    vw_render, _vh = viewer._render_viewport()
    scroll_doc = viewer._scroll_doc()
    scroll_doc_x = viewer._scroll_doc_x()
    bucket = float(layout.raster_scale)
    x_off = DocumentLayout.page_x_origin(
        vw_render, float(layout.total_width_px),
        float(entry.render_width_px))
    wx = (x_off + float(lx) * bucket - scroll_doc_x) * scale
    wy = (float(entry.y_offset_px) + float(ly) * bucket - scroll_doc) * scale
    return wx, wy


def _pixel_is_red(viewer, x, y) -> bool:
    try:
        color = viewer.grab().toImage().pixelColor(int(x), int(y))
        return (color.red() > 150 and color.green() < 150
                and color.blue() < 150)
    except Exception:
        return False


# ------------------------------------------------ Part A: handler capture

def test_eraser_handler_emits_page_local_segments():
    page = PageId()
    handler = EraserHandler()
    assert handler.down(page, 10.0, 20.0) == {"eraser_begin": True}
    update = handler.move(page, 30.0, 40.0)
    seg = update["eraser_update"]
    assert isinstance(seg, EraserSegment)
    assert (seg.page_id, seg.x0, seg.y0, seg.x1, seg.y1) == (
        page, 10.0, 20.0, 30.0, 40.0)
    out = handler.up(page, 50.0, 60.0)
    final = out["eraser_update"]
    assert (final.x0, final.y0, final.x1, final.y1) == (
        30.0, 40.0, 50.0, 60.0)
    assert out["eraser_done"] is True
    assert handler.events == ["down", "move", "up"]


def test_eraser_press_alone_never_creates_a_segment():
    page = PageId()
    handler = EraserHandler()
    handler.down(page, 10.0, 10.0)
    # Releasing without movement yields no segment (a bare press erases
    # nothing by itself).
    assert handler.up(page, 10.0, 10.0) == {"eraser_done": True}


def test_eraser_handler_move_without_press_is_noop():
    page = PageId()
    handler = EraserHandler()
    assert handler.move(page, 1.0, 1.0) == {"noop": True}
    assert handler.up(page, 1.0, 1.0) == {"eraser_done": True}


def test_eraser_handler_never_bridges_across_pages():
    p0, p1 = PageId(), PageId()
    handler = EraserHandler()
    handler.down(p0, 10.0, 10.0)
    seg = handler.move(p1, 500.0, 500.0)["eraser_update"]
    assert seg.page_id == p1
    # A page change starts a fresh sub-path (a point, never a bridge).
    assert (seg.x0, seg.y0) == (500.0, 500.0)
    assert (seg.x1, seg.y1) == (500.0, 500.0)


def test_eraser_handler_cancel_resets():
    page = PageId()
    handler = EraserHandler()
    handler.down(page, 1.0, 2.0)
    assert handler.cancel() == {"eraser_cancelled": True}
    assert handler.move(page, 3.0, 4.0) == {"noop": True}


def test_eraser_raw_viewport_input_is_ignored():
    controller = InteractionController(_eraser_tool())
    assert isinstance(controller._handler(), EraserHandler)
    # Like pen/highlighter, raw viewport-px input is ignored: the eraser
    # must arrive mapped through the handle_eraser_* API.
    assert controller.handle_press(10.0, 20.0) == {"noop": True}
    assert controller.handle_move(30.0, 40.0) == {"noop": True}
    assert controller.handle_release(30.0, 40.0) == {"noop": True}
    page = PageId()
    assert controller.handle_eraser_press(page, 1.0, 1.0) == (
        {"eraser_begin": True})
    assert "eraser_update" in controller.handle_eraser_move(page, 5.0, 5.0)
    assert controller.handle_eraser_release(page, 5.0, 5.0)["eraser_done"]


def test_tool_switch_cancels_eraser_gesture():
    controller = InteractionController(_eraser_tool())
    handler = controller._eraser_handler()
    controller.handle_eraser_press(PageId(), 1.0, 1.0)
    controller.set_tool(_pen_tool())
    assert handler.events[-1] == "cancel"
    assert handler.move(PageId(), 2.0, 2.0) == {"noop": True}


# --------------------------------------------------- Part B: hit-testing

def test_normal_pen_stroke_erased_on_intersection(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_highlighter_erased_on_intersection(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_highlighter(viewer, [(100.0, 300.0), (300.0, 300.0)])
        assert _store(viewer).count(0) == 1
        _erase_line(viewer, _page_id(viewer), 200.0, 150.0, 200.0, 450.0)
        assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_pressure_wide_outline_erased_narrow_preserved(
        qtbot, tmp_path, cleared_settings):
    """The eraser honors the rendered (pressure-variable) width, not just
    the base bbox: the same 6 pt offset hits an inked-wide sample and
    misses an inked-narrow one."""
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        # base 6 mm => half-width 8.5 pt at full pressure.
        wide = _add_stroke(
            viewer, [(100.0, 300.0), (300.0, 300.0)], width_mm=6.0,
            pressure_enabled=True, pressures=(1.0, 1.0))
        narrow = _add_stroke(
            viewer, [(100.0, 400.0), (300.0, 400.0)], width_mm=6.0,
            pressure_enabled=True, pressures=(0.0, 0.0))
        assert viewer._pressure_outline(wide) is not None
        # 6 pt below each centerline: inside the wide outline, outside
        # the narrow one (and outside the eraser reach of the narrow).
        _erase_line(viewer, _page_id(viewer), 150.0, 306.0, 250.0, 306.0)
        assert _store(viewer).page(0) == (narrow,)
    finally:
        viewer.close_document()


def test_near_miss_preserves_stroke(qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)],
                             width_mm=1.0)
        # Effective reach is ~4.75 pt; a parallel pass 12 pt away misses.
        _erase_line(viewer, _page_id(viewer), 150.0, 312.0, 250.0, 312.0)
        assert _store(viewer).page(0) == (stroke,)
    finally:
        viewer.close_document()


def test_bounding_box_overlap_alone_does_not_delete(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 100.0), (200.0, 200.0)],
                             width_mm=1.0)
        # The eraser segment sits inside the diagonal's bbox but ~60 pt
        # from the ink itself: geometry, not the rectangle, decides.
        _erase_line(viewer, _page_id(viewer), 105.0, 195.0, 105.0, 205.0)
        assert _store(viewer).page(0) == (stroke,)
    finally:
        viewer.close_document()


def test_zoom_and_scroll_preserve_hit_testing(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool())
    try:
        # Baseline at zoom 1: drag along the just-drawn stroke.
        _draw(viewer, [(120.0, 120.0), (260.0, 120.0)])
        assert len(committed_strokes(viewer)) == 1
        viewer._interaction.set_tool(_eraser_tool())
        _press(viewer, 120.0, 120.0)
        _move(viewer, 260.0, 120.0)
        _release(viewer, 260.0, 120.0)
        assert committed_strokes(viewer) == []
        # Now under zoom + scroll: map the stored page-local points back
        # to widget space and erase there.
        viewer._interaction.set_tool(_pen_tool())
        _draw(viewer, [(120.0, 140.0), (260.0, 140.0)])
        stroke = committed_strokes(viewer)[0]
        viewer.set_zoom(2.0)
        viewer._scrollbar.setValue(min(40, viewer._scrollbar.maximum()))
        qtbot.wait(60)
        p0 = stroke.points[0]
        p1 = stroke.points[1]
        a = _page_pt_to_widget(viewer, p0.x, p0.y)
        b = _page_pt_to_widget(viewer, p1.x, p1.y)
        viewer._interaction.set_tool(_eraser_tool())
        _press(viewer, *a)
        _move(viewer, *b)
        _release(viewer, *b)
        assert committed_strokes(viewer) == []
    finally:
        viewer.close_document()


def test_hit_testing_uses_owning_page_geometry(
        qtbot, tmp_path, cleared_settings):
    from app.main_window import MainWindow

    pdf = make_sized_pdf(str(tmp_path / "m65pages.pdf"),
                         [(600.0, 800.0), (600.0, 800.0)])
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(_eraser_tool())
    qtbot.wait(120)
    try:
        assert viewer._pipeline.layout.page_count == 2
        stroke0 = _add_stroke(viewer, [(100.0, 100.0), (200.0, 100.0)],
                              page_index=0)
        # Same page-local coordinates, but the gesture is bound to page 1.
        _erase_line(viewer, _page_id(viewer, 1), 100.0, 100.0, 200.0, 100.0)
        assert _store(viewer).page(0) == (stroke0,)
        assert _store(viewer).page(1) == ()
    finally:
        viewer.close_document()


# ----------------------------------------------- Part C: gesture semantics

def test_gesture_with_no_hits_creates_no_history(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 100.0), (200.0, 100.0)])
        _erase_line(viewer, _page_id(viewer), 400.0, 400.0, 500.0, 500.0)
        history = viewer.document_history
        assert history.stack.count() == 0
        assert history.can_undo() is False
        assert history.group_open is False
        assert _store(viewer).count(0) == 1
    finally:
        viewer.close_document()


def test_one_gesture_one_stroke_is_one_undo_step(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        history = viewer.document_history
        assert _store(viewer).page(0) == ()
        assert history.stack.count() == 1
        assert history.can_undo() is True
    finally:
        viewer.close_document()


def test_one_gesture_erases_multiple_strokes_once_each(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 200.0), (300.0, 200.0)])
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _add_stroke(viewer, [(100.0, 400.0), (300.0, 400.0)])
        # One vertical pass crosses all three; repeated passes over the
        # same already-erased region must add nothing.
        it = viewer._interaction
        pid = _page_id(viewer)
        viewer._apply_intent(it.handle_eraser_press(pid, 200.0, 150.0))
        for y in (250.0, 450.0, 250.0, 350.0, 450.0):
            viewer._apply_intent(it.handle_eraser_move(pid, 200.0, y))
        viewer._apply_intent(it.handle_eraser_release(pid, 200.0, 450.0))
        history = viewer.document_history
        assert _store(viewer).page(0) == ()
        assert history.stack.count() == 1
    finally:
        viewer.close_document()


def test_one_undo_restores_all_and_redo_removes_all(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        a = _add_stroke(viewer, [(100.0, 200.0), (300.0, 200.0)])
        b = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        c = _add_stroke(viewer, [(100.0, 400.0), (300.0, 400.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 150.0, 200.0, 450.0)
        history = viewer.document_history
        assert _store(viewer).page(0) == ()
        history.undo()
        assert [s.id for s in _store(viewer).page(0)] == [a.id, b.id, c.id]
        assert _store(viewer).page(0) == (a, b, c)
        history.redo()
        assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_multiple_gestures_are_distinct_undo_steps(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        a = _add_stroke(viewer, [(100.0, 200.0), (300.0, 200.0)])
        b = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 150.0, 200.0, 250.0)
        # Second, separate gesture.
        it = viewer._interaction
        pid = _page_id(viewer)
        viewer._apply_intent(it.handle_eraser_press(pid, 200.0, 250.0))
        viewer._apply_intent(it.handle_eraser_move(pid, 200.0, 350.0))
        viewer._apply_intent(it.handle_eraser_release(pid, 200.0, 350.0))
        history = viewer.document_history
        assert history.stack.count() == 2
        history.undo()  # reverses only the second gesture
        assert _store(viewer).page(0) == (b,)
        history.undo()  # reverses the first gesture too
        assert _store(viewer).page(0) == (a, b)
        assert history.can_undo() is False
    finally:
        viewer.close_document()


def test_interrupted_gesture_leaves_no_open_group(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _press(viewer, 200.0, 200.0)
        _move(viewer, 200.0, 400.0)  # deletes -> group now open
        assert viewer._eraser_history is not None
        assert viewer.document_history.group_open is True
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._eraser_history is None
        assert viewer.document_history.group_open is False
        history = viewer.document_history
        assert history.can_undo() is True
        # M6.5 hotfix: cancellation must still leave a usable HUD control.
        hud = viewer.undo_hud
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        assert _store(viewer).page(0) == (stroke,)
    finally:
        viewer.close_document()


def test_tool_and_document_switches_leave_no_stale_state(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _press(viewer, 200.0, 200.0)
        _move(viewer, 200.0, 400.0)
        assert viewer._eraser_history is not None
        # Tool switch: cancel() finalizes the group.
        viewer._apply_intent(viewer._interaction.cancel())
        viewer._interaction.set_tool(_pen_tool())
        assert viewer._eraser_history is None
        assert viewer._eraser_mouse_open is False
        assert viewer._eraser_tablet_open is False
        # Document switch closes the old document, which must finalize
        # and drop all eraser state.
        pdf2 = make_probe_pdf(str(tmp_path / "m65b.pdf"), page_width=600.0,
                              page_height=800.0)
        assert viewer.open_document(pdf2) is True
        assert viewer._eraser_history is None
        assert viewer._eraser_mouse_open is False
        assert viewer._eraser_tablet_open is False
        assert _store(viewer).count(0) == 0
    finally:
        viewer.close_document()


# ------------------------------------------ Part D: identity/order/state

def test_undo_restores_original_identity_and_data(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)],
                             color="#123456", width_mm=1.5, opacity=0.8)
        sid = stroke.id
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert _store(viewer).page(0) == ()
        viewer.document_history.undo()
        got = _store(viewer).page(0)[0]
        assert got is stroke and got.id == sid
        assert got.points == stroke.points
        assert (got.color, got.width_mm, got.opacity) == (
            "#123456", 1.5, 0.8)
        assert got.page_id == stroke.page_id
    finally:
        viewer.close_document()


def test_multiple_removed_recover_original_relative_order(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        items = [_add_stroke(viewer, [(100.0, 100.0 + 100.0 * i),
                                      (300.0, 100.0 + 100.0 * i)])
                 for i in range(3)]
        # Remove the first and third (leave the middle): order must not
        # be corrupted by an incidental restore order.
        _erase_line(viewer, _page_id(viewer), 200.0, 50.0, 200.0, 150.0)
        assert _store(viewer).page(0) == (items[1], items[2])
        _erase_line(viewer, _page_id(viewer), 200.0, 250.0, 200.0, 350.0)
        assert _store(viewer).page(0) == (items[1],)
        viewer.document_history.undo()
        assert _store(viewer).page(0) == (items[1], items[2])
        viewer.document_history.undo()
        assert _store(viewer).page(0) == tuple(items)
    finally:
        viewer.close_document()


def test_store_stays_consistent_after_repeated_cycles(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        items = [_add_stroke(viewer, [(100.0, 200.0 + 100.0 * i),
                                      (300.0, 200.0 + 100.0 * i)])
                 for i in range(3)]
        _erase_line(viewer, _page_id(viewer), 200.0, 150.0, 200.0, 450.0)
        history = viewer.document_history
        for _ in range(4):
            history.undo()
            assert _store(viewer).page(0) == tuple(items)
            history.redo()
            assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_two_documents_isolate_erasures(
        qtbot, tmp_path, cleared_settings):
    from app.main_window import MainWindow

    pdf1 = make_probe_pdf(str(tmp_path / "m65a.pdf"), page_width=600.0,
                          page_height=800.0)
    pdf2 = make_probe_pdf(str(tmp_path / "m65b.pdf"), page_width=600.0,
                          page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf1) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(_eraser_tool())
    qtbot.wait(120)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        doc1 = viewer._document
        assert doc1.annotations.count(0) == 0
        assert viewer.open_document(pdf2) is True
        assert doc1.is_closed
        viewer._interaction.set_tool(_eraser_tool())
        qtbot.wait(120)
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert viewer._document is not doc1
        assert viewer._document.annotations.count(0) == 0
        assert doc1.annotations.count(0) == 0
        assert doc1.id.value not in viewer._undo_histories
    finally:
        viewer.close_document()


def test_close_document_releases_eraser_state(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _press(viewer, 200.0, 200.0)
        _move(viewer, 200.0, 400.0)
        assert viewer._eraser_history is not None
        doc = viewer._document
        viewer.close_document()
        assert viewer._eraser_history is None
        assert viewer._eraser_mouse_open is False
        assert viewer._eraser_tablet_open is False
        assert doc.is_closed
        assert len(doc.annotations) == 0
    finally:
        viewer.close_document()


# ------------------------------------------------ Part E: rendering

def test_erased_stroke_disappears_and_undo_restores_pixels(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool())
    try:
        _draw(viewer, [(250.0, 300.0), (450.0, 300.0)])
        viewer.repaint()
        assert _pixel_is_red(viewer, 350.0, 300.0)
        viewer._interaction.set_tool(_eraser_tool())
        _press(viewer, 250.0, 300.0)
        _move(viewer, 450.0, 300.0)
        _release(viewer, 450.0, 300.0)
        viewer.repaint()
        # No residual pixels anywhere along the erased stroke.
        assert not _pixel_is_red(viewer, 350.0, 300.0)
        assert not _pixel_is_red(viewer, 280.0, 300.0)
        assert not _pixel_is_red(viewer, 430.0, 300.0)
        # M6.5 hotfix: after a completed gesture the real HUD control must
        # be enabled and clickable (it was left disabled before the fix).
        hud = viewer.undo_hud
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        viewer.repaint()
        assert _pixel_is_red(viewer, 350.0, 300.0)
        assert hud.can_redo() is True
        assert hud._redo_btn.isEnabled() is True
        hud._redo_btn.click()
        viewer.repaint()
        assert not _pixel_is_red(viewer, 350.0, 300.0)
    finally:
        viewer.close_document()


def test_pen_drawing_unaffected_outside_eraser_ops(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool())
    try:
        _draw(viewer, [(200.0, 200.0), (300.0, 200.0)])
        assert len(committed_strokes(viewer)) == 1
        # Selecting (and leaving) the eraser must not touch pen capture.
        viewer._interaction.set_tool(_eraser_tool())
        viewer._interaction.set_tool(_pen_tool())
        _draw(viewer, [(200.0, 260.0), (300.0, 260.0)])
        assert len(committed_strokes(viewer)) == 2
        assert _store(viewer).count(0) == 2
    finally:
        viewer.close_document()


# ------------------------------------------- Part G: HUD availability (hotfix)

def test_completed_gesture_enables_hud_undo_redo(qtbot, tmp_path,
                                                 cleared_settings):
    """Real gesture path: after release the HUD buttons must be usable."""
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        history = viewer.document_history
        assert _store(viewer).page(0) == ()
        assert history.group_open is False
        assert history.can_undo() is True
        hud = viewer.undo_hud
        # The control must reflect the real stack, not stay disabled.
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        assert hud._redo_btn.isEnabled() is False
        # Undo through the actual button.
        hud._undo_btn.click()
        assert [s.id for s in _store(viewer).page(0)] == [stroke.id]
        assert _store(viewer).page(0) == (stroke,)
        assert hud.can_redo() is True
        assert hud._redo_btn.isEnabled() is True
        # Redo through the actual button.
        hud._redo_btn.click()
        assert _store(viewer).page(0) == ()
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
    finally:
        viewer.close_document()


def test_multi_annotation_gesture_real_events_one_step_and_hud(
        qtbot, tmp_path, cleared_settings):
    """A multi-stroke drag delivered as real viewer events is one step."""
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        a = _add_stroke(viewer, [(100.0, 200.0), (300.0, 200.0)])
        b = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        c = _add_stroke(viewer, [(100.0, 400.0), (300.0, 400.0)])
        wx, wy0 = _page_pt_to_widget(viewer, 200.0, 150.0)
        _, wy1 = _page_pt_to_widget(viewer, 200.0, 450.0)
        _press(viewer, wx, wy0)
        _move(viewer, wx, wy1)
        _release(viewer, wx, wy1)
        history = viewer.document_history
        assert _store(viewer).page(0) == ()
        assert history.stack.count() == 1  # exactly one logical step
        assert history.group_open is False
        hud = viewer.undo_hud
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        # One undo restores every stroke in its original page order.
        assert [s.id for s in _store(viewer).page(0)] == [a.id, b.id, c.id]
        assert _store(viewer).page(0) == (a, b, c)
        assert hud._redo_btn.isEnabled() is True
        hud._redo_btn.click()
        assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_no_hit_gesture_leaves_hud_and_history_unchanged(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        stroke = _add_stroke(viewer, [(100.0, 100.0), (300.0, 100.0)])
        hud = viewer.undo_hud
        assert hud.can_undo() is False
        assert hud._undo_btn.isEnabled() is False
        _erase_line(viewer, _page_id(viewer), 500.0, 100.0, 560.0, 100.0)
        history = viewer.document_history
        assert history.stack.count() == 0
        assert history.group_open is False
        assert hud.can_undo() is False
        assert hud._undo_btn.isEnabled() is False
        assert _store(viewer).page(0) == (stroke,)
    finally:
        viewer.close_document()


def test_no_hit_gesture_after_erase_keeps_history_and_hud(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        hud = viewer.undo_hud
        assert hud._undo_btn.isEnabled() is True
        # A second, empty gesture must not touch the existing step.
        _erase_line(viewer, _page_id(viewer), 500.0, 100.0, 560.0, 100.0)
        history = viewer.document_history
        assert history.stack.count() == 1
        assert history.group_open is False
        assert hud._undo_btn.isEnabled() is True
    finally:
        viewer.close_document()


def test_pen_stroke_after_erase_is_undoable_via_hud(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        erased = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 250.0, 200.0, 350.0)
        assert _store(viewer).page(0) == ()
        viewer._interaction.set_tool(_pen_tool())
        wx0, wy = _page_pt_to_widget(viewer, 150.0, 600.0)
        wx1, _ = _page_pt_to_widget(viewer, 350.0, 600.0)
        _draw(viewer, [(wx0, wy), (wx1, wy)])
        assert len(_store(viewer).page(0)) == 1
        hud = viewer.undo_hud
        assert hud._undo_btn.isEnabled() is True
        # Undo the pen stroke, then the erase, through the HUD.
        hud._undo_btn.click()
        assert _store(viewer).page(0) == ()
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        assert [s.id for s in _store(viewer).page(0)] == [erased.id]
    finally:
        viewer.close_document()


def test_highlighter_stroke_after_erase_is_undoable_via_hud(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        erased = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 250.0, 200.0, 350.0)
        assert _store(viewer).page(0) == ()
        viewer._interaction.set_tool(_hl_tool())
        wx0, wy = _page_pt_to_widget(viewer, 150.0, 620.0)
        wx1, _ = _page_pt_to_widget(viewer, 350.0, 620.0)
        _draw(viewer, [(wx0, wy), (wx1, wy)])
        assert len(_store(viewer).page(0)) == 1
        hud = viewer.undo_hud
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        assert _store(viewer).page(0) == ()
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        assert [s.id for s in _store(viewer).page(0)] == [erased.id]
    finally:
        viewer.close_document()


def test_document_close_after_gesture_leaves_hud_disabled(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert viewer.undo_hud._undo_btn.isEnabled() is True
        viewer.close_document()
        # Closing drops the history: the HUD must not target stale state.
        assert viewer.undo_hud.can_undo() is False
        assert viewer.undo_hud._undo_btn.isEnabled() is False
        assert viewer.undo_hud.can_redo() is False
    finally:
        viewer.close_document()


# ------------------------------------------------------- Part F: boundaries

def test_annotation_layers_stay_qt_free():
    root = Path(__file__).resolve().parent.parent
    for rel in ("annotations/models.py", "annotations/store.py",
                "annotations/interaction.py",
                "annotations/ink_geometry.py"):
        source = (root / rel).read_text(encoding="utf-8")
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert not ({"PySide6", "PySide2", "PyQt5", "PyQt6"} & modules), rel
