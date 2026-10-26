"""M6.4: pen strokes in the document-owned store + shared undo/redo.

Committed pen strokes now live in the owning document's ``AnnotationStore``
(the authoritative state) and are painted from there, mirroring the M6.3
highlighter integration. The pen preview, geometry, pressure, and
invalidation algorithms are unchanged; only the commit destination and the
paint source move to the store. Pen and highlighter now share one
per-document history (the M6.2 ``DocumentHistory`` / ``QUndoStack``), so
undo/redo reverses the most recent operation across both tools in true
chronological (LIFO) order.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from annotations.models import Highlighter, InkStroke
from core.types import Point
from tests.helpers import committed_strokes, make_probe_pdf
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


def _drive(viewer, page_id, samples):
    """Feed page-space (x, y, pressure) samples through the real path."""
    it = viewer._interaction
    t = 1000
    viewer._apply_intent(it.handle_pen_press(page_id, *samples[0], t))
    for sample in samples[1:]:
        t += 16
        viewer._apply_intent(it.handle_pen_move(page_id, *sample, t))
    viewer._apply_intent(it.handle_pen_release(page_id, *samples[-1], t + 16))


def _open_window(qtbot, tmp_path, tool=None, name="m64.pdf"):
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
    viewer._interaction.set_tool(tool or _pen_tool())
    qtbot.wait(200)
    return window, viewer


def _page_id(viewer):
    return viewer._pipeline.layout.entry(0).page_id


def _page(viewer):
    return viewer._document.annotations.page(0)


def _pixel_is_red(viewer, x, y) -> bool:
    try:
        color = viewer.grab().toImage().pixelColor(int(x), int(y))
        return (color.red() > 150 and color.green() < 150
                and color.blue() < 150)
    except Exception:
        return False


# ------------------------------------------------ Part A: commit + storage

def test_release_commits_one_canonical_stroke_to_store(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        assert committed_strokes(viewer) == []
        _draw(viewer, [(250.0, 400.0), (350.0, 410.0), (450.0, 420.0)])
        store = viewer._document.annotations
        assert store.count(0) == 1 and len(store) == 1
        stroke = store.page(0)[0]
        assert isinstance(stroke, InkStroke)
        assert stroke.page_id == _page_id(viewer)
        assert len(stroke.points) == 3
        assert (stroke.color, stroke.width_mm, stroke.opacity) == (
            "#ff0000", 1.0, 0.9)
        assert viewer._live_stroke is None
        # Exactly one committed representation: the store, nothing else.
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()


def test_no_legacy_committed_stroke_collection(
        qtbot, tmp_path, cleared_settings):
    """The viewer keeps NO parallel committed-stroke list (M6.4)."""
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (400.0, 400.0)])
        assert not hasattr(viewer, "_strokes")
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()


def test_pen_commit_pushes_exactly_one_add_command(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (400.0, 400.0)])
        history = viewer.document_history
        assert history is not None
        assert history.stack.count() == 1
        assert history.stack.command(0).text() == "Add Annotation"
        assert history.can_undo() is True
        assert history.can_redo() is False
    finally:
        viewer.close_document()


def test_cancel_commits_nothing_and_creates_no_history(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _press(viewer, 250.0, 400.0)
        _move(viewer, 320.0, 410.0)
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._live_stroke is None
        assert committed_strokes(viewer) == []
        assert viewer._document.annotations.count(0) == 0
        assert viewer.document_history.stack.count() == 0
    finally:
        viewer.close_document()


def test_duplicate_completion_intent_does_not_commit_twice(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 400.0), (400.0, 400.0)])
        stroke = committed_strokes(viewer)[0]
        # A duplicate completion intent for the same stroke (repeated
        # release / duplicate routing) must not add a second copy or
        # command: the store rejects the duplicate identity.
        viewer._apply_intent({"stroke_done": stroke})
        assert viewer._document.annotations.count(0) == 1
        assert viewer.document_history.stack.count() == 1
    finally:
        viewer.close_document()


def test_pressure_mode_data_preserved_in_store(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _pen_tool(pressure=True, width_mm=3.0))
    try:
        page = _page_id(viewer)
        samples = [(100.0 + 50.0 * i, 250.0, p)
                   for i, p in enumerate((0.2, 0.5, 0.9, 1.0))]
        _drive(viewer, page, samples)
        strokes = committed_strokes(viewer)
        assert len(strokes) == 1
        stroke = strokes[0]
        assert stroke.page_id == page
        assert stroke.pressure_enabled is True
        assert len(stroke.pressures) == len(stroke.points) == len(samples)
        for (_, _, p), got in zip(samples, stroke.pressures):
            assert abs(got - p) < 1e-9
        # The store-backed painter still builds the variable-width outline.
        assert viewer._pressure_outline(stroke) is not None
    finally:
        viewer.close_document()


def test_stored_coordinates_survive_scroll_and_zoom(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(300.0, 150.0), (400.0, 170.0)])
        before = [(p.x, p.y) for p in committed_strokes(viewer)[0].points]
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        viewer.set_zoom(2.0)
        viewer.set_zoom(4.0)  # raster bucket 1.0 -> 4.0
        after = [(p.x, p.y) for p in committed_strokes(viewer)[0].points]
        assert after == before
    finally:
        viewer.close_document()


# ------------------------------------------------- Part B: rendering parity

def test_committed_stroke_painted_from_store(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 300.0), (450.0, 300.0)])
        viewer.repaint()
        assert _pixel_is_red(viewer, 350.0, 300.0)
    finally:
        viewer.close_document()


def test_off_view_page_annotation_remains_stored(
        qtbot, tmp_path, cleared_settings):
    """A committed stroke on a non-visible page stays in the store and is
    gathered (for painting) only when that page is the one being painted."""
    from app.main_window import MainWindow
    from tests.helpers import make_sized_pdf

    pdf = make_sized_pdf(str(tmp_path / "m64pages.pdf"),
                         [(600.0, 800.0), (600.0, 800.0)])
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(_pen_tool())
    qtbot.wait(150)
    try:
        layout = viewer._pipeline.layout
        assert layout.page_count == 2
        entry1 = layout.entry(1)
        off = InkStroke(page_id=entry1.page_id,
                        points=(Point(50.0, 50.0), Point(150.0, 60.0)),
                        color="#ff0000", width_mm=1.0, opacity=0.9)
        viewer.document_history.push_add(off, 1)
        # Associated with its own page only; the store keeps it whether
        # or not page 1 is currently rendered.
        assert viewer._page_pen_strokes(entry1) == [off]
        assert viewer._page_pen_strokes(layout.entry(0)) == []
        viewer.update()
        assert len(committed_strokes(viewer)) == 1
        assert viewer._document.annotations.count(1) == 1
    finally:
        viewer.close_document()


def test_undo_removes_pixels_and_redo_restores(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(250.0, 300.0), (450.0, 300.0)])
        viewer.repaint()
        assert _pixel_is_red(viewer, 350.0, 300.0)
        # Undo through the HUD path: repaint must clear the stroke's
        # pixels entirely (no ghost).
        viewer.undo_hud.undoRequested.emit()
        viewer.repaint()
        assert not _pixel_is_red(viewer, 350.0, 300.0)
        # Redo restores the same appearance.
        viewer.undo_hud.redoRequested.emit()
        viewer.repaint()
        assert _pixel_is_red(viewer, 350.0, 300.0)
    finally:
        viewer.close_document()


def test_outline_cache_stable_across_undo_redo(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        page = _page_id(viewer)
        _drive(viewer, page, [(100.0 + 40.0 * i, 120.0, 0.3 + 0.1 * i)
                              for i in range(5)])
        stroke = committed_strokes(viewer)[0]
        outline = viewer._cached_outline(stroke)
        assert outline is not None
        assert viewer._outline_cache.get(stroke) is outline
        viewer.undo_hud.undoRequested.emit()
        viewer.undo_hud.redoRequested.emit()
        # Same identity object survives undo/redo: its geometry cache
        # entry is reused, not rebuilt.
        again = committed_strokes(viewer)[0]
        assert again is stroke
        assert viewer._cached_outline(stroke) is outline
    finally:
        viewer.close_document()


def test_close_clears_caches_and_store(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        page = _page_id(viewer)
        _drive(viewer, page, [(100.0 + 40.0 * i, 120.0, 0.3 + 0.1 * i)
                              for i in range(5)])
        doc = viewer._document
        assert len(committed_strokes(viewer)) == 1
        assert viewer._outline_cache  # warmed by commit + paint
        viewer.close_document()
        assert doc.is_closed
        assert len(doc.annotations) == 0
        assert viewer._outline_cache == {}
        assert viewer._bbox_memo == {}
        assert viewer._off_smooth_cache == {}
        assert viewer._hl_smooth_cache == {}
        assert viewer._history_for(viewer._document, create=False) is None
    finally:
        viewer.close_document()


# --------------------------------------------------- Part C: shared history

def test_pen_only_undo_redo_preserves_identity_and_order(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(200.0, 200.0), (320.0, 200.0)])
        _draw(viewer, [(200.0, 320.0), (320.0, 320.0)])
        ids = [s.id for s in committed_strokes(viewer)]
        assert len(ids) == 2 and ids[0] != ids[1]
        history = viewer.document_history
        assert history.stack.count() == 2
        history.undo()
        assert [s.id for s in committed_strokes(viewer)] == ids[:1]
        history.undo()
        assert committed_strokes(viewer) == []
        history.redo()
        assert [s.id for s in committed_strokes(viewer)] == ids[:1]
        history.redo()
        assert [s.id for s in committed_strokes(viewer)] == ids
    finally:
        viewer.close_document()


def test_interleaved_pen_highlighter_undo_redo_lifo(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        # Pen A.
        _draw(viewer, [(200.0, 150.0), (320.0, 150.0)])
        a = committed_strokes(viewer)[-1].id
        # Highlighter B.
        viewer._interaction.set_tool(_hl_tool())
        _draw(viewer, [(200.0, 220.0), (320.0, 220.0)])
        b = [ann.id for ann in _page(viewer)
             if isinstance(ann, Highlighter)][0]
        # Pen C.
        viewer._interaction.set_tool(_pen_tool())
        _draw(viewer, [(200.0, 300.0), (320.0, 300.0)])
        c = committed_strokes(viewer)[-1].id
        assert [ann.id for ann in _page(viewer)] == [a, b, c]
        history = viewer.document_history
        assert history.stack.count() == 3
        # Undo reverses C, then B, then A (chronological LIFO).
        for expected in ([a, b], [a], []):
            viewer.undo_hud.undoRequested.emit()
            assert [ann.id for ann in _page(viewer)] == expected
        # Redo restores A, then B, then C — same identities.
        for expected in ([a], [a, b], [a, b, c]):
            viewer.undo_hud.redoRequested.emit()
            assert [ann.id for ann in _page(viewer)] == expected
    finally:
        viewer.close_document()


def test_repeated_undo_redo_cycles_do_not_duplicate(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        _draw(viewer, [(200.0, 150.0), (320.0, 150.0)])
        viewer._interaction.set_tool(_hl_tool())
        _draw(viewer, [(200.0, 220.0), (320.0, 220.0)])
        # Two committed annotations (one pen, one highlighter).
        assert viewer._document.annotations.count(0) == 2
        strokes = committed_strokes(viewer)
        assert len(strokes) == 1
        sid = strokes[0].id
        for _ in range(4):
            viewer.undo_hud.undoRequested.emit()
            viewer.undo_hud.undoRequested.emit()
            assert viewer._document.annotations.count(0) == 0
            viewer.undo_hud.redoRequested.emit()
            viewer.undo_hud.redoRequested.emit()
            assert viewer._document.annotations.count(0) == 2
            assert committed_strokes(viewer)[0].id == sid
    finally:
        viewer.close_document()


def test_hud_reflects_pen_commits(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path)
    try:
        assert viewer.undo_hud.can_undo() is False
        _draw(viewer, [(250.0, 400.0), (400.0, 400.0)])
        assert viewer.undo_hud.can_undo() is True
        assert viewer.undo_hud.can_redo() is False
        viewer.undo_hud.undoRequested.emit()
        assert viewer.undo_hud.can_undo() is False
        assert viewer.undo_hud.can_redo() is True
        viewer.undo_hud.redoRequested.emit()
        assert viewer.undo_hud.can_undo() is True
        assert viewer.undo_hud.can_redo() is False
    finally:
        viewer.close_document()


def test_two_documents_isolate_stores_and_histories(
        qtbot, tmp_path, cleared_settings):
    from app.main_window import MainWindow

    pdf1 = make_probe_pdf(str(tmp_path / "m64a.pdf"), page_width=600.0,
                          page_height=800.0)
    pdf2 = make_probe_pdf(str(tmp_path / "m64b.pdf"), page_width=600.0,
                          page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf1) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(_pen_tool())
    qtbot.wait(150)
    try:
        _draw(viewer, [(250.0, 300.0), (350.0, 300.0)])
        doc1 = viewer._document
        history1 = viewer.document_history
        a_id = committed_strokes(viewer)[0].id
        assert history1.stack.count() == 1
        assert len(doc1.annotations) == 1
        # Switching documents closes the first: its store is cleared and
        # its history is dropped, so nothing leaks into the second.
        assert viewer.open_document(pdf2) is True
        viewer._interaction.set_tool(_pen_tool())
        qtbot.wait(150)
        doc2 = viewer._document
        assert doc2 is not doc1 and doc1.is_closed
        assert len(doc1.annotations) == 0
        assert doc1.id.value not in viewer._undo_histories
        _draw(viewer, [(250.0, 400.0), (350.0, 400.0)])
        b_id = committed_strokes(viewer)[0].id
        assert b_id != a_id
        assert len(doc2.annotations) == 1
        history2 = viewer.document_history
        assert history2 is not history1
        assert history2.stack.count() == 1
    finally:
        viewer.close_document()


# --------------------------------------------- Part D: boundaries

def test_annotation_layers_stay_qt_free():
    """M6.4 keeps models/store/interaction/geometry Qt-free."""
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
