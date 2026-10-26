"""M5-A2.2: inline QML numeric editors (Size + Opacity).

QML owns presentation; Python ToolInstance stays authoritative. The
bridge forwards validated commits through the existing section paths.
"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens, light_tokens
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


def _open(section, qtbot):
    section._custom_btn.click()
    qtbot.wait(900)
    assert section._editor is not None
    assert section._bridge is not None
    assert not section._editor.errors()
    return section._bridge


def test_qml_editor_ready_no_errors(qtbot, cleared_settings):
    from PySide6.QtQuickWidgets import QQuickWidget

    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    for section in (form._size_section, form._opacity_section):
        section._custom_btn.click()
        qtbot.wait(900)
        assert section._editor.status() == QQuickWidget.Ready
        assert section._editor.errors() == []
        # Rounded drawer has real geometry (not a collapsed strip).
        assert section._editor.height() >= 40
        assert section._editor.width() > 100


def test_size_commit_clamp_per_kind(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    hl = viewer.tool_store.add_slot(ToolType.HIGHLIGHTER, name="H1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(hl, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    bridge = _open(form._size_section, qtbot)
    assert (bridge.minimum, bridge.maximum) == (1.5, 5.0)
    bridge.commit("99")  # clamps to highlighter max, never corrupts
    assert form._slot.width_mm == pytest.approx(5.0)
    bridge.commit("abc")  # invalid: model untouched
    assert form._slot.width_mm == pytest.approx(5.0)
    assert bridge.displayText.startswith("5.0")


def test_opacity_percent_and_zero(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    bridge = _open(form._opacity_section, qtbot)
    assert bridge.unit == "%"
    bridge.commit("0")
    assert form._slot.opacity == 0.0
    bridge.commit("100")
    assert form._slot.opacity == 1.0
    bridge.commit("33")
    assert form._slot.opacity == pytest.approx(0.33)
    # Slider + readout follow the commit through the existing path.
    assert form._opacity_section._slider.value() == 33
    assert "%" in form._opacity_section._readout.text()


def test_tool_switch_reseeds_no_leak(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1", width_mm=0.5)
    era = viewer.tool_store.add_slot(ToolType.ERASER, name="E1",
                                     width_mm=3.0)
    viewer.tool_shelf.refresh()
    form_pen = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form_pen)
    form_pen.show()
    qtbot.wait(100)
    bridge_pen = _open(form_pen._size_section, qtbot)
    bridge_pen.commit("1.2")  # uncommitted-then-committed on pen
    assert form_pen._slot.width_mm == pytest.approx(1.2)
    # A different tool's popup seeds from its own model value.
    form_era = PropertiesForm(era, dark_tokens(), viewer)
    qtbot.addWidget(form_era)
    form_era.show()
    qtbot.wait(100)
    bridge_era = _open(form_era._size_section, qtbot)
    assert bridge_era.displayText.startswith("3.0")
    assert (bridge_era.minimum, bridge_era.maximum) == (1.0, 5.0)


def test_keyboard_enter_escape_across_boundary(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L1",
                                      width_mm=0.5)
    viewer.tool_shelf.refresh()
    form = PropertiesForm(line, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    _open(form._size_section, qtbot)
    form._size_section._editor.setFocus()
    qtbot.wait(250)
    QTest.keyClicks(form._size_section._editor, "1.1")
    qtbot.wait(150)
    QTest.keyClick(form._size_section._editor, Qt.Key.Key_Enter)
    qtbot.wait(150)
    assert form._slot.width_mm == pytest.approx(1.1)
    form._size_section._editor.setFocus()
    qtbot.wait(250)
    QTest.keyClicks(form._size_section._editor, "9")
    qtbot.wait(150)
    QTest.keyClick(form._size_section._editor, Qt.Key.Key_Escape)
    qtbot.wait(150)
    assert form._slot.width_mm == pytest.approx(1.1)


def test_theme_update_reaches_qml(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    _open(form._size_section, qtbot)
    form._size_section.update_tokens(light_tokens())
    qtbot.wait(200)
    assert (form._size_section._theme_bridge.inputBg
            == light_tokens().input_background)


def test_narrow_popup_no_clip(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.resize(300, form.sizeHint().height())
    form.show()
    qtbot.wait(100)
    _open(form._size_section, qtbot)
    qtbot.wait(300)
    editor = form._size_section._editor
    assert editor.width() > 100
    assert editor.height() >= 40
    # Editor stays inside the drawer (no overflow past num_wrap).
    assert (editor.mapTo(form._size_section._num_wrap, editor.rect()
                         .topRight()).x()
            <= form._size_section._num_wrap.width())
