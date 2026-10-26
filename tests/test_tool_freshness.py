"""M5-B.1.1: popup reopens show the stored slot configuration.

Root cause: shelf edit callbacks capture the ToolInstance built with the
buttons; live edits replace the stored instance, so reopening from the
captured (stale) object reinitialized PropertiesForm with old values —
and a later edit could overwrite the good stored values. The opener now
resolves the current instance by slot id before constructing the form.
"""

import dataclasses

import pytest
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens
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


def _make_viewer(qtbot):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    return viewer


def _open_with(viewer, qtbot, slot):
    """Open the popup the way the shelf does (possibly stale slot)."""
    assert viewer._open_properties_popup(slot, viewer.tool_shelf) is True
    qtbot.wait(600)  # QML palette grid loads
    popup = viewer._tool_popup
    assert popup is not None
    return popup


def _close(viewer, qtbot):
    viewer._close_tool_popup()
    qtbot.wait(100)


def test_reopen_shows_edited_color(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    popup = _open_with(viewer, qtbot, slot)
    form = popup._content
    form._color_section._emit("#ff0000")
    assert viewer.tool_store.find(slot.id.value).color == "#ff0000"
    _close(viewer, qtbot)

    # Reopen with the stale pre-edit snapshot, as the shelf closure does.
    popup2 = _open_with(viewer, qtbot, stale)
    try:
        assert popup2._content._color_section._color == "#ff0000"
    finally:
        _close(viewer, qtbot)


def test_reopen_shows_edited_width_opacity_fill_line_endings(
        qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(
        ToolType.RECTANGLE, name="R1", color="#112233", width_mm=0.5,
        opacity=1.0, fill_enabled=False, fill_color="#ffffff",
        fill_opacity=1.0, line_style="solid", start_ending="none",
        end_ending="none", square=False)
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    popup = _open_with(viewer, qtbot, slot)
    form = popup._content
    form._size_section._emit(1.5)
    form._opacity_section._emit(0.4)
    viewer.tool_store.update_slot(
        slot.id.value, fill_enabled=True, fill_color="#00ff00",
        fill_opacity=0.5, line_style="dashed", start_ending="bar",
        end_ending="arrow", square=True)
    _close(viewer, qtbot)

    popup2 = _open_with(viewer, qtbot, stale)
    try:
        form2 = popup2._content
        assert form2._size_section._value == 1.5
        assert form2._opacity_section._value == 0.4
        assert form2._fill_section._enabled is True
        assert form2._fill_section._color == "#00ff00"
        assert form2._line_section._style == "dashed"
        assert form2._line_section._start == "bar"
        assert form2._line_section._end == "arrow"
    finally:
        _close(viewer, qtbot)


def test_reopen_across_editable_kinds(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    cases = [
        (ToolType.PEN, {"color": "#123456"}),
        (ToolType.HIGHLIGHTER, {"color": "#abcdef", "opacity": 0.3}),
        (ToolType.LINE, {"width_mm": 2.0}),
    ]
    for kind, fields in cases:
        slot = viewer.tool_store.add_slot(kind, name=f"T-{kind.value}")
        viewer.tool_shelf.refresh()
        stale = dataclasses.replace(slot)
        popup = _open_with(viewer, qtbot, slot)
        try:
            viewer.tool_store.update_slot(slot.id.value, **fields)
        finally:
            _close(viewer, qtbot)
        popup2 = _open_with(viewer, qtbot, stale)
        try:
            live = viewer.tool_store.find(slot.id.value)
            form2 = popup2._content
            if "color" in fields:
                assert form2._color_section._color == fields["color"], kind
            if "width_mm" in fields:
                assert form2._size_section._value == fields["width_mm"], kind
            if "opacity" in fields:
                assert form2._opacity_section._value == fields["opacity"], \
                    kind
            assert live.id.value == slot.id.value
        finally:
            _close(viewer, qtbot)


def test_noop_open_close_preserves_store(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(
        ToolType.PEN, name="P9", color="#445566", width_mm=0.8, opacity=0.7)
    viewer.tool_shelf.refresh()

    def _snapshot():
        return [(s.id.value, s.kind, s.name, s.color, s.width_mm,
                 s.opacity, s.fill_enabled, s.fill_color, s.fill_opacity,
                 s.line_style, s.start_ending, s.end_ending)
                for s in viewer.tool_store.slots]

    before = _snapshot()
    popup = _open_with(viewer, qtbot, slot)
    _close(viewer, qtbot)
    assert _snapshot() == before


def test_edit_after_reopen_does_not_clobber_saved_properties(
        qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="PC")
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)

    popup = _open_with(viewer, qtbot, slot)
    popup._content._color_section._emit("#ff0000")
    _close(viewer, qtbot)

    # Reopen from stale, then edit an unrelated property: the saved
    # color must survive (previously overwritten with the stale value).
    popup2 = _open_with(viewer, qtbot, stale)
    try:
        popup2._content._size_section._emit(1.2)
        live = viewer.tool_store.find(slot.id.value)
        assert live.color == "#ff0000"
        assert live.width_mm == 1.2
    finally:
        _close(viewer, qtbot)


def test_slot_identity_and_persistence_unchanged(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="PI")
    viewer.tool_shelf.refresh()
    stale = dataclasses.replace(slot)
    popup = _open_with(viewer, qtbot, slot)
    popup._content._color_section._emit("#010203")
    _close(viewer, qtbot)

    live = viewer.tool_store.find(slot.id.value)
    assert live is not None
    assert live.id.value == stale.id.value
    assert live.color == "#010203"
    viewer._flush_tool_persist()

    from app.toolshelf.store import ToolShelfStore

    restored = ToolShelfStore.restore()
    found = restored.find(slot.id.value)
    assert found is not None and found.color == "#010203"
