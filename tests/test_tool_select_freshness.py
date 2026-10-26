"""M5-B.1.1.1: selecting through a stale shelf reference must not reset
the drawing configuration to defaults.

Root cause: shelf selection callbacks capture the ToolInstance built
with the buttons; live edits replace the stored instance, so
_on_tool_selected installed a stale (often default-valued) object into
the InteractionController. _on_tool_selected now resolves the current
instance by slot id before set_tool (fallback: passed slot).
"""

import dataclasses

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens
from tests.helpers import committed_strokes, make_probe_pdf
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")


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


def _make_viewer(qtbot, tmp_path):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    pdf = make_probe_pdf(str(tmp_path / "sel.pdf"), page_width=600.0,
                         page_height=800.0)
    assert viewer.open_document(pdf) is True
    qtbot.wait(200)
    return viewer


def _open_with(viewer, qtbot, slot):
    assert viewer._open_properties_popup(slot, viewer.tool_shelf) is True
    qtbot.wait(600)
    assert viewer._tool_popup is not None
    return viewer._tool_popup


def _close(viewer, qtbot):
    viewer._close_tool_popup()
    qtbot.wait(100)


def _draw(viewer, x0=300.0, y0=150.0, x1=400.0, y1=170.0):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x0, y0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x1, y1),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x1, y1),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def test_stale_select_draws_edited_config(qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)  # shelf closure snapshot
    viewer.tool_store.update_slot(
        slot.id.value, color="#ff0000", width_mm=1.5, opacity=0.5)

    viewer._on_tool_selected(stale)
    _draw(viewer)
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#ff0000", 1.5, 0.5)


def test_popup_open_close_after_stale_select_keeps_config(
        qtbot, tmp_path, cleared_settings):
    """Exact reported repro: edit, stale-select, open/close popup, draw."""
    viewer = _make_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(
        ToolType.PEN, name="P2", color="#00aa00", width_mm=0.9, opacity=0.8)
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    viewer._on_tool_selected(stale)
    popup = _open_with(viewer, qtbot, stale)  # no edits inside
    assert popup._content._color_section._color == "#00aa00"
    _close(viewer, qtbot)

    _draw(viewer)
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#00aa00", 0.9, 0.8)


def test_post_select_edit_applies_and_old_strokes_keep_appearance(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(
        ToolType.PEN, name="P3", color="#ff0000", width_mm=0.7, opacity=1.0)
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    viewer._on_tool_selected(stale)
    _draw(viewer, y0=150.0, y1=170.0)
    viewer.tool_store.update_slot(
        slot.id.value, color="#0000ff", width_mm=1.2, opacity=0.6)
    viewer._on_tool_selected(stale)
    _draw(viewer, y0=250.0, y1=270.0)

    assert len(committed_strokes(viewer)) == 2
    first, second = committed_strokes(viewer)
    assert (first.color, first.width_mm, first.opacity) == (
        "#ff0000", 0.7, 1.0)
    assert (second.color, second.width_mm, second.opacity) == (
        "#0000ff", 1.2, 0.6)


def test_live_popup_edit_flows_to_next_stroke(qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P4")
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    popup = _open_with(viewer, qtbot, slot)
    popup._content._color_section._emit("#123456")
    popup._content._size_section._emit(1.1)
    _close(viewer, qtbot)

    viewer._on_tool_selected(stale)
    _draw(viewer)
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert stroke.color == "#123456"
    assert stroke.width_mm == 1.1


def test_default_pen_draws_defaults(qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P5")
    viewer.tool_shelf.refresh()
    stored = viewer.tool_store.find(slot.id.value)

    viewer._on_tool_selected(dataclasses.replace(slot))
    _draw(viewer)
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    # Unedited pen draws exactly its stored configuration.
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        stored.color, stored.width_mm, stored.opacity)


def test_unknown_slot_falls_back_to_passed_slot(qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    orphan = dataclasses.replace(
        viewer.tool_store.add_slot(ToolType.PEN, name="ORPH"))
    viewer.tool_store.remove_slot(orphan.id.value)
    # Must not raise and must not substitute a different slot.
    viewer._on_tool_selected(orphan)
    assert viewer._interaction.active_kind == ToolType.PEN
    assert viewer._interaction.active_tool.id.value == orphan.id.value
    _draw(viewer)
    assert len(committed_strokes(viewer)) == 1
    assert committed_strokes(viewer)[0].color == orphan.color
