"""M5-A2.2: full-QML palette strip + darker recessed well.

The shared collection lives in ToolShelfStore; PresetBridge delegates
every mutation there. Selection stays per-strip (stroke vs fill).
"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens, light_tokens
from tools.models import ToolType
from tests.test_m5a22_numeric_qml import cleared_settings, _make_viewer  # noqa

pytestmark = pytest.mark.usefixtures("qapp")


def _strip(qtbot, tokens, **slot_kwargs):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R1",
                                      **slot_kwargs)
    viewer.tool_shelf.refresh()
    form = PropertiesForm(viewer.tool_store.find(slot.id.value),
                          tokens, viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(1200)  # QML grid Ready
    return viewer, form, form._color_section


def test_bridge_holds_no_state(qtbot, cleared_settings):
    from app.toolshelf.store import get_color_presets

    viewer, form, strip = _strip(qtbot, dark_tokens())
    assert strip._preset_bridge.colors == list(get_color_presets())
    # QML-side select/remove go straight to the store (same object).
    strip._preset_bridge.select(0)
    assert form._slot.color.lower() == get_color_presets()[0].lower()
    n = len(get_color_presets())
    strip._preset_bridge.removeAt(n - 1)
    assert len(get_color_presets()) == n - 1
    assert len(strip._preset_bridge.colors) == n - 1


def test_stroke_fill_independence_shared_collection(
        qtbot, cleared_settings):
    viewer, form, strip = _strip(
        qtbot, dark_tokens(), color="#1b1e26",
        fill_enabled=True, fill_color="#ffd60a")
    fill = form._fill_section._palette
    qtbot.wait(600)
    assert (strip._preset_bridge.colors
            == fill._preset_bridge.colors)  # one collection
    assert strip._preset_bridge is not fill._preset_bridge
    assert (strip._preset_bridge.selectedColor.lower() == "#1b1e26")
    assert (fill._preset_bridge.selectedColor.lower() == "#ffd60a")
    # Sibling mutation refreshes both strips, selections intact.
    fill._preset_bridge.removeAt(0)
    qtbot.wait(200)
    assert (strip._preset_bridge.colors
            == fill._preset_bridge.colors)
    assert strip._preset_bridge.selectedColor.lower() == "#1b1e26"


def test_click_add_remove_reorder_through_qml(qtbot, cleared_settings):
    from PySide6.QtQuickWidgets import QQuickWidget

    from app.toolshelf.store import get_color_presets

    viewer, form, strip = _strip(qtbot, dark_tokens())
    assert strip._grid_view.status() == QQuickWidget.Ready
    n = len(get_color_presets())
    # Click a swatch cell: live-apply through the existing path.
    QTest.mouseClick(strip._grid_view, Qt.MouseButton.LeftButton,
                     pos=strip._swatch_center(2))
    qtbot.wait(150)
    assert form._slot.color.lower() == get_color_presets()[2].lower()
    # Click the trailing add tile: picker opens (same add workflow).
    QTest.mouseClick(strip._grid_view, Qt.MouseButton.LeftButton,
                     pos=strip._swatch_center(n))
    qtbot.wait(400)
    assert strip._picker_wrap.isHidden() is False
    strip._set_picker_open(False)
    qtbot.wait(400)
    # Reorder via the bridge (QML drag path): store order follows.
    first = get_color_presets()[0]
    strip._preset_bridge.movePreset(0, 3)
    assert get_color_presets()[3].lower() == first.lower()
    assert strip._preset_bridge.colors[3].lower() == first.lower()


def test_persistence_roundtrip(qtbot, cleared_settings):
    from app.toolshelf.store import get_color_presets

    viewer, form, strip = _strip(qtbot, dark_tokens())
    form._color_section._color = "#41b6c8"
    assert form._color_section._commit_live_color() is True
    assert "#41b6c8" in get_color_presets()
    # Mirror the viewer live-apply path (form.changed -> store).
    viewer.tool_store.update_slot(form._slot.id.value, color="#41b6c8")
    # A reopened popup renders the persisted collection + selection.
    from app.toolshelf.tool_popup import PropertiesForm

    form2 = PropertiesForm(
        viewer.tool_store.find(form._slot.id.value),
        dark_tokens(), viewer)
    qtbot.addWidget(form2)
    form2.show()
    qtbot.wait(1200)
    assert "#41b6c8" in form2._color_section._preset_bridge.colors
    assert (form2._color_section._preset_bridge.selectedColor.lower()
            == "#41b6c8")


@pytest.mark.parametrize("tokens", [dark_tokens(), light_tokens()],
                         ids=["dark", "light"])
def test_well_darker_distinct_both_themes(qtbot, cleared_settings, tokens):
    from app.icons import contrast_ratio
    from app.toolshelf.tool_popup import (
        _palette_surface, _palette_well, _swatch_outline)

    viewer, form, strip = _strip(qtbot, tokens)
    bg, edge = _palette_well(tokens)
    popup = str(tokens.surface_raised)
    assert contrast_ratio(bg, _palette_surface(tokens)) >= 1.5
    assert contrast_ratio(edge, popup) >= 2.0
    assert contrast_ratio(edge, bg) >= 1.6
    # Every default swatch stays visible (directly or via outline).
    from app.toolshelf.store import get_color_presets

    for sw in get_color_presets():
        assert contrast_ratio(sw, bg) >= 1.8 or \
            _swatch_outline(sw, tokens, bg) != "transparent"
    # Rendered well: rounded (transparent corners), darker pad color.
    img = strip._grid_view.grabFramebuffer()
    assert img.pixelColor(1, 1).alpha() < 32
    pad = img.pixelColor(4, img.height() - 4)
    well = QColor(bg)
    assert max(abs(pad.red() - well.red()),
               abs(pad.green() - well.green()),
               abs(pad.blue() - well.blue())) < 40
