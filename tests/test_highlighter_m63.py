"""M6.3 free-draw Highlighter integration.

Behavioral coverage only: press/move/release capture -> one canonical
``Highlighter`` in the document-owned ``AnnotationStore`` -> rendering
from that store -> undo/redo through the existing HUD. No persistence,
no other tool, and no duplicate committed representation in the viewer.
"""

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from annotations.interaction import (
    HighlighterHandler,
    InteractionController,
    LiveHighlighter,
)
from annotations.models import Highlighter, InkStroke
from core.types import PageId
from app.viewport_controller import page_screen_rect
from tests.helpers import committed_strokes, make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------- fixtures

def _hl_tool(color="#FFFF00", width=3.0, opacity=0.4):
    return ToolInstance(kind=ToolType.HIGHLIGHTER, name="Highlight",
                        color=color, width_mm=width, opacity=opacity)


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


def _open_hl_window(qtbot, tmp_path, tool=None, name="hl.pdf"):
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
    viewer._interaction.set_tool(tool or _hl_tool())
    qtbot.wait(150)
    return window, viewer


def _pixel(viewer, x, y):
    image = viewer.grab().toImage()
    return image.pixelColor(int(x), int(y))


def _is_yellow_hue(color) -> bool:
    """True when a pixel is dominated by yellow (R≈G, low blue).

    Holds whether the page behind the stroke is a rendered tile or an
    unloaded placeholder: both are neutral gray, so a yellow-hued pixel
    can only come from the highlighter.
    """
    return (abs(color.red() - color.green()) <= 4
            and (color.green() - color.blue()) >= 30)


def _assert_translucent_stroke(viewer, x, y, fg=(255, 255, 0),
                               opacity=0.4) -> None:
    """Assert the pixel equals ``fg`` over the page at ``opacity``.

    The reference background is sampled 12 px below (same page, same
    tile, clear of the ~8 px stroke), so this proves genuine alpha
    compositing rather than a flat opaque fill — independent of whether
    the async tile behind it has loaded yet.
    """
    stroke = _pixel(viewer, x, y)
    bg = _pixel(viewer, x, y + 12)
    for got, f, b in ((stroke.red(), fg[0], bg.red()),
                      (stroke.green(), fg[1], bg.green()),
                      (stroke.blue(), fg[2], bg.blue())):
        expected = f * opacity + b * (1.0 - opacity)
        assert abs(got - expected) <= 6, (
            stroke.getRgb(), bg.getRgb(), expected)


# --------------------------------------------------- Part 1: interaction

def test_handler_press_move_release_builds_one_highlighter():
    handler = HighlighterHandler()
    handler.configure(_hl_tool())
    page = PageId()
    first = handler.down(page, 10.0, 20.0)
    live = first["highlight_update"]
    assert isinstance(live, LiveHighlighter)
    assert live.seq == 1
    assert tuple((p.x, p.y) for p in live.points) == ((10.0, 20.0),)
    handler.move(page, 30.0, 40.0)
    ann = handler.up(page, 30.0, 40.0)["highlight_done"]
    assert isinstance(ann, Highlighter)
    assert isinstance(ann.points, tuple)
    assert tuple((p.x, p.y) for p in ann.points) == ((10.0, 20.0),
                                                    (30.0, 40.0))
    assert (ann.color, ann.width_mm, ann.opacity) == ("#FFFF00", 3.0, 0.4)
    assert ann.page_id == page


def test_handler_cancel_produces_no_annotation():
    handler = HighlighterHandler()
    page = PageId()
    handler.down(page, 1.0, 1.0)
    handler.move(page, 2.0, 2.0)
    assert handler.cancel() == {"highlight_cancelled": True}
    assert handler.up(page, 3.0, 3.0) == {"noop": True}


def test_handler_dedupes_and_ignores_other_pages():
    handler = HighlighterHandler()
    page, other = PageId(), PageId()
    handler.down(page, 5.0, 5.0)
    assert handler.move(page, 5.0, 5.0) == {"noop": True}
    assert handler.move(other, 6.0, 6.0) == {"noop": True}
    handler.move(page, 7.0, 7.0)
    ann = handler.up(page, 7.0, 7.0)["highlight_done"]
    assert tuple((p.x, p.y) for p in ann.points) == ((5.0, 5.0),
                                                    (7.0, 7.0))


def test_controller_routes_highlighter_to_real_handler():
    controller = InteractionController(_hl_tool())
    assert isinstance(controller._handler(), HighlighterHandler)
    # Raw viewport-px gestures never open a highlighter stroke.
    assert controller.handle_press(10.0, 20.0) == {"noop": True}
    assert controller.handle_move(30.0, 40.0) == {"noop": True}
    assert controller.handle_release(30.0, 40.0) == {"noop": True}
    page = PageId()
    assert "highlight_update" in controller.handle_highlight_press(
        page, 5.0, 6.0)
    assert "highlight_done" in controller.handle_highlight_release(
        page, 7.0, 8.0)


def test_controller_tool_switch_cancels_open_highlighter():
    controller = InteractionController(_hl_tool())
    page = PageId()
    controller.handle_highlight_press(page, 1.0, 1.0)
    handler = controller._handler()
    controller.set_tool(ToolInstance(kind=ToolType.PAN, name="Pan"))
    # The old handler was cancelled: it can no longer complete a stroke.
    assert handler.up(page, 2.0, 2.0) == {"noop": True}


# ----------------------------------------------- Part 2: viewer + store

def test_stroke_commits_one_canonical_annotation_to_store(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        assert committed_strokes(viewer) == []
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        store = viewer._document.annotations
        assert store.count(0) == 1 and len(store) == 1
        ann = store.page(0)[0]
        assert isinstance(ann, Highlighter)
        assert isinstance(ann.points, tuple) and len(ann.points) == 3
        assert (ann.color, ann.width_mm, ann.opacity) == (
            "#FFFF00", 3.0, 0.4)
        # Exactly one Add command; no pen stroke is committed and no
        # duplicate committed representation exists.
        history = viewer.document_history
        assert history.stack.count() == 1
        assert history.stack.command(0).text() == "Add Annotation"
        assert committed_strokes(viewer) == []
        assert viewer._live_highlighter is None
        # Geometry equals the mapped inputs; correct page association.
        mapped = [viewer._event_to_page_pt(x, y)
                  for x, y in ((250.0, 400.0), (350.0, 400.0),
                               (450.0, 400.0))]
        assert all(m is not None for m in mapped)
        for point, m in zip(ann.points, mapped):
            assert abs(point.x - m[1]) < 1e-9
            assert abs(point.y - m[2]) < 1e-9
        assert ann.page_id == mapped[0][0]
        assert store.contains(ann.id) is True
    finally:
        viewer.close_document()


def test_transient_preview_then_gone_after_release(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _press(viewer, 250.0, 400.0)
        _move(viewer, 350.0, 400.0)
        assert viewer._live_highlighter is not None
        assert viewer._document.annotations.count(0) == 0  # not committed
        _release(viewer, 450.0, 400.0)
        assert viewer._live_highlighter is None
        assert viewer._document.annotations.count(0) == 1
    finally:
        viewer.close_document()


def test_cancel_leaves_no_annotation_and_no_command(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _press(viewer, 250.0, 400.0)
        _move(viewer, 350.0, 400.0)
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._live_highlighter is None
        assert viewer._document.annotations.count(0) == 0
        assert viewer.document_history.stack.count() == 0
    finally:
        viewer.close_document()


def test_tool_switch_mid_stroke_does_not_commit(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _press(viewer, 250.0, 400.0)
        _move(viewer, 350.0, 400.0)
        viewer._on_tool_selected(ToolInstance(kind=ToolType.PAN, name="Pan"))
        assert viewer._live_highlighter is None
        assert viewer._document.annotations.count(0) == 0
        assert viewer.document_history.stack.count() == 0
    finally:
        viewer.close_document()


def test_stray_release_does_not_double_commit(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        n = viewer.document_history.stack.count()
        # A second release with no open stroke must not commit anything.
        viewer._apply_intent(viewer._interaction.handle_highlight_release(
            PageId(), 460.0, 400.0))
        assert viewer._document.annotations.count(0) == 1
        assert viewer.document_history.stack.count() == n
    finally:
        viewer.close_document()


def test_undo_removes_and_redo_restores_same_identity(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        store = viewer._document.annotations
        ann = store.page(0)[0]
        ident = ann.id
        assert viewer.undo_hud.can_undo() is True
        assert viewer.undo_hud.can_redo() is False
        viewer.undo_hud.undoRequested.emit()
        assert store.count(0) == 0
        assert store.get(ident) is None
        assert viewer.undo_hud.can_undo() is False
        assert viewer.undo_hud.can_redo() is True
        viewer.undo_hud.redoRequested.emit()
        restored = store.get(ident)
        assert restored is ann
        assert restored.points == ann.points
        assert (restored.color, restored.width_mm, restored.opacity) == (
            "#FFFF00", 3.0, 0.4)
        assert store.page(0) == (ann,)
        assert viewer.undo_hud.can_undo() is True
        assert viewer.undo_hud.can_redo() is False
    finally:
        viewer.close_document()


def test_repeated_undo_redo_cycles_create_no_duplicates(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        store = viewer._document.annotations
        ident = store.page(0)[0].id
        for _ in range(4):
            viewer.undo_hud.undoRequested.emit()
            assert store.count(0) == 0
            viewer.undo_hud.redoRequested.emit()
            assert store.count(0) == 1
            assert store.get(ident) is not None
        assert viewer.document_history.stack.count() == 1
    finally:
        viewer.close_document()


# ------------------------------------------------------ Part 3: rendering

def test_committed_highlighter_renders_translucent(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        assert _is_yellow_hue(_pixel(viewer, 350, 400))
        _assert_translucent_stroke(viewer, 350, 400)
    finally:
        viewer.close_document()


def test_undo_and_redo_change_pixels_without_residue(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        _assert_translucent_stroke(viewer, 350, 400)
        viewer.undo_hud.undoRequested.emit()
        # Back to the bare page: the stroke pixel equals its background.
        off = _pixel(viewer, 350, 400)
        bg = _pixel(viewer, 350, 412)
        assert (abs(off.red() - bg.red()) <= 3
                and abs(off.green() - bg.green()) <= 3
                and abs(off.blue() - bg.blue()) <= 3)
        viewer.undo_hud.redoRequested.emit()
        # Same single translucent pass after the cycle (no residue).
        _assert_translucent_stroke(viewer, 350, 400)
    finally:
        viewer.close_document()


def test_repeated_repaint_does_not_darken(qtbot, tmp_path,
                                          cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        first = _pixel(viewer, 350, 400)
        for _ in range(3):
            viewer.repaint()
        again = _pixel(viewer, 350, 400)
        assert (first.red(), first.green(), first.blue()) == (
            again.red(), again.green(), again.blue())
        # Still exactly one translucent pass, never accumulated.
        _assert_translucent_stroke(viewer, 350, 400)
    finally:
        viewer.close_document()


def test_overlapping_separate_strokes_blend_deeper(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        single = _pixel(viewer, 300, 400)
        assert _is_yellow_hue(single)
        _draw(viewer, [(300.0, 350.0), (300.0, 400.0), (300.0, 450.0)])
        assert viewer._document.annotations.count(0) == 2
        overlap = _pixel(viewer, 300, 400)
        # A second translucent pass darkens the overlap (lower blue).
        assert overlap.blue() < single.blue()
    finally:
        viewer.close_document()


def test_committed_geometry_survives_zoom_and_scroll(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(150.0, 150.0), (250.0, 150.0), (350.0, 150.0)])
        store = viewer._document.annotations
        ann = store.page(0)[0]
        before = [(p.x, p.y) for p in ann.points]
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        assert [(p.x, p.y) for p in ann.points] == before
        viewer.set_zoom(2.0)
        assert [(p.x, p.y) for p in ann.points] == before
        viewer._scrollbar.setValue(0)
        viewer._hscrollbar.setValue(0)
        # The stored content reprojects onto itself and still renders.
        layout = viewer._pipeline.layout
        entry = layout.entry(0)
        scale = viewer._display_scale()
        vw, _vh = viewer._render_viewport()
        rect = page_screen_rect(
            float(entry.y_offset_px), float(entry.render_width_px),
            float(entry.render_height_px), viewer._scroll_doc(), vw, scale,
            scroll_x_doc=viewer._scroll_doc_x(),
            content_width_px=float(layout.total_width_px))
        bucket = layout.raster_scale
        lx, ly = before[1]
        sx = rect.x + lx * bucket * scale
        sy = rect.y + ly * bucket * scale
        assert 0 <= sx < viewer.width() and 0 <= sy < viewer.height()
        assert _is_yellow_hue(_pixel(viewer, sx, sy))
    finally:
        viewer.close_document()


# -------------------------------------------- Part 4: isolation + lifecycle

def test_two_open_documents_keep_independent_state(
        qtbot, tmp_path, cleared_settings):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    manager_a, manager_b = DocumentManager(), DocumentManager()
    viewer_a = viewer_b = None
    try:
        viewer_a, viewer_b = PdfViewer(manager_a), PdfViewer(manager_b)
        for viewer in (viewer_a, viewer_b):
            viewer.resize(800, 600)
            viewer.show()
            qtbot.addWidget(viewer)
        assert viewer_a.open_document(make_probe_pdf(
            str(tmp_path / "a.pdf"))) is True
        assert viewer_b.open_document(make_probe_pdf(
            str(tmp_path / "b.pdf"))) is True
        viewer_a._interaction.set_tool(_hl_tool())
        viewer_b._interaction.set_tool(_hl_tool())
        qtbot.wait(100)
        _draw(viewer_a, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        assert viewer_a._document.annotations.count(0) == 1
        assert viewer_b._document.annotations.count(0) == 0
        assert viewer_a.document_history.can_undo() is True
        assert viewer_b.document_history.can_undo() is False
        viewer_a.undo_hud.undoRequested.emit()
        assert viewer_a._document.annotations.count(0) == 0
        assert viewer_b._document.annotations.count(0) == 0
        assert viewer_b.document_history.stack.count() == 0
    finally:
        if viewer_a is not None:
            viewer_a.close()
        if viewer_b is not None:
            viewer_b.close()
        manager_a.close_all()
        manager_b.close_all()


def test_closing_document_clears_live_state_and_history(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_hl_window(qtbot, tmp_path)
    try:
        _press(viewer, 250.0, 400.0)
        _move(viewer, 350.0, 400.0)
        assert viewer._live_highlighter is not None
        viewer.close_document()
        assert viewer._live_highlighter is None
        assert viewer.document_history is None
        assert viewer.undo_hud.can_undo() is False
        assert viewer.undo_hud.can_redo() is False
        viewer.undo_hud.undoRequested.emit()  # safe no-op
        viewer.undo_hud.redoRequested.emit()
    finally:
        viewer.close()


# -------------------------------------- Part 5: boundaries + regressions

def test_highlighter_model_and_store_stay_qt_free():
    root = Path(__file__).resolve().parent.parent
    for rel in ("annotations/models.py", "annotations/store.py",
                "annotations/interaction.py"):
        source = (root / rel).read_text(encoding="utf-8")
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert not ({"PySide6", "PySide2", "PyQt5", "PyQt6"} & modules), rel


def test_pen_commits_one_canonical_stroke_to_store(
        qtbot, tmp_path, cleared_settings):
    """M6.4: the pen commits to the store via the shared history.

    Re-targeted from the M6.3 test that asserted the pre-M6.4 split
    (pen kept only in a viewer list, store untouched). The intended
    architecture legitimately changed, so the assertion is strengthened:
    exactly one canonical ``InkStroke`` is committed, exactly one Add
    command is pushed, and no duplicate committed representation exists.
    """
    window, viewer = _open_hl_window(
        qtbot, tmp_path,
        tool=ToolInstance(kind=ToolType.PEN, name="Pen #1",
                          color="#ff0000", width_mm=1.0, opacity=0.9))
    try:
        assert committed_strokes(viewer) == []
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0), (450.0, 400.0)])
        store = viewer._document.annotations
        assert store.count(0) == 1 and len(store) == 1
        stroke = store.page(0)[0]
        assert isinstance(stroke, InkStroke)
        assert (stroke.color, stroke.width_mm, stroke.opacity) == (
            "#ff0000", 1.0, 0.9)
        history = viewer.document_history
        assert history.stack.count() == 1
        assert history.stack.command(0).text() == "Add Annotation"
        assert len(committed_strokes(viewer)) == 1
        assert viewer._live_stroke is None
    finally:
        viewer.close_document()
