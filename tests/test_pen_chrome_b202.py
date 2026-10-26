"""M5-B.2.0.2 Stage 1: pen double-tap opens tool popup exactly once.

Real QTabletEvents (synthetic delivery, offscreen). A qualifying second
tap on the same chrome target forwards MouseButtonDblClick instead of a
second press — the only sequence Qt routes to mouseDoubleClickEvent.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

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


_pen_device = None


def _tablet_device():
    global _pen_device
    if _pen_device is None:
        from PySide6.QtGui import QInputDevice, QPointingDevice

        _pen_device = QPointingDevice(
            "test-pen2", 2, QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Pen,
            QInputDevice.Capability.Position
            | QInputDevice.Capability.Pressure
            | QInputDevice.Capability.MouseEmulation,
            1, 1)
    return _pen_device


def _tablet(etype, x, y, pressure=0.5):
    from PySide6.QtGui import QTabletEvent

    button = (Qt.MouseButton.LeftButton
              if etype == QEvent.Type.TabletPress
              else Qt.MouseButton.NoButton)
    buttons = (Qt.MouseButton.NoButton
               if etype == QEvent.Type.TabletRelease
               else Qt.MouseButton.LeftButton)
    return QTabletEvent(etype, _tablet_device(), QPointF(x, y),
                        QPointF(x, y), pressure, 0.0, 0.0, 0.0, 0.0, 0,
                        Qt.KeyboardModifier.NoModifier, button, buttons)


def _make_viewer(qtbot, tmp_path):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    pdf = make_probe_pdf(str(tmp_path / "dbl.pdf"), page_width=600.0,
                         page_height=800.0)
    assert viewer.open_document(pdf) is True
    qtbot.wait(200)
    return viewer


def _pen_button(viewer, name="Dbl"):
    slot = viewer.tool_store.add_slot(ToolType.PEN, name=name, color="#112233",
                                      width_mm=0.7, opacity=1.0)
    viewer.tool_shelf.refresh()
    from app.toolshelf.slot_button import SlotButton

    button = next(b for b in viewer.tool_shelf.findChildren(SlotButton)
                  if b._slot.name == name)
    return slot, button


def _tap(viewer, x, y, pressure=0.6):
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, x, y, pressure))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, x, y, 0.0))


def _tap_at_button(viewer, button):
    center = button.mapTo(viewer, button.rect().center())
    _tap(viewer, float(center.x()), float(center.y()))
    return center


def test_double_tap_opens_popup_exactly_once(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    clicks, edits, opens = [], [], []
    button.clicked.connect(lambda: clicks.append(1))
    button.editRequested.connect(lambda _b: edits.append(1))
    orig = viewer._open_properties_popup
    viewer._open_properties_popup = (
        lambda s, l: (opens.append(1), orig(s, l))[1])
    try:
        center = _tap_at_button(viewer, button)
        _tap(viewer, float(center.x()), float(center.y()))
        qtbot.wait(600)  # QML popup content loads
        assert clicks == [1]  # single selection, no duplicate
        assert edits == [1]  # dblclick handler fired exactly once
        assert len(opens) == 1
        assert viewer._tool_popup is not None
        assert committed_strokes(viewer) == []
        assert viewer._live_stroke is None
    finally:
        viewer._open_properties_popup = orig
        viewer._close_tool_popup()


def test_single_tap_selects_without_popup(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    clicks, edits = [], []
    button.clicked.connect(lambda: clicks.append(1))
    button.editRequested.connect(lambda _b: edits.append(1))
    _tap_at_button(viewer, button)
    assert clicks == [1]
    assert edits == []
    assert viewer._tool_popup is None
    assert committed_strokes(viewer) == []


def test_double_tap_on_pan_never_opens_popup(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    from app.toolshelf.slot_button import SlotButton

    pan = next(b for b in viewer.tool_shelf.findChildren(SlotButton)
               if b._slot.kind == ToolType.PAN)
    edits = []
    pan.editRequested.connect(lambda _b: edits.append(1))
    center = pan.mapTo(viewer, pan.rect().center())
    _tap(viewer, float(center.x()), float(center.y()))
    _tap(viewer, float(center.x()), float(center.y()))
    assert edits == []
    assert viewer._tool_popup is None


def test_split_targets_produce_two_singles(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot_a, button_a = _pen_button(viewer, name="SplitA")
    _slot_b, button_b = _pen_button(viewer, name="SplitB")
    edits = []
    button_a.editRequested.connect(lambda _b: edits.append(1))
    button_b.editRequested.connect(lambda _b: edits.append(1))
    _tap_at_button(viewer, button_a)
    _tap_at_button(viewer, button_b)
    assert edits == []
    assert viewer._tool_popup is None


def test_expired_chain_produces_two_singles(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    clicks, edits = [], []
    button.clicked.connect(lambda: clicks.append(1))
    button.editRequested.connect(lambda _b: edits.append(1))
    center = _tap_at_button(viewer, button)
    # Simulate a slow second tap without sleeping: backdate the chain.
    target, _ms, x, y = viewer._pen_last_tap
    viewer._pen_last_tap = (
        target, 0, x, y)  # epoch start: far outside the interval
    _tap(viewer, float(center.x()), float(center.y()))
    assert clicks == [1, 1]
    assert edits == []
    assert viewer._tool_popup is None


def test_canvas_press_breaks_pending_chain(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    edits = []
    button.editRequested.connect(lambda _b: edits.append(1))
    center = _tap_at_button(viewer, button)
    assert viewer._pen_last_tap is not None
    # Intervening canvas touch: next button tap is a fresh single.
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.5))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, 300.0, 150.0, 0.0))
    assert viewer._pen_last_tap is None
    _tap(viewer, float(center.x()), float(center.y()))
    assert edits == []
    assert viewer._tool_popup is None
    assert len(committed_strokes(viewer)) == 1  # canvas touch drew normally


def test_triple_tap_yields_one_popup_and_two_clicks(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    clicks, edits, opens = [], [], []
    button.clicked.connect(lambda: clicks.append(1))
    button.editRequested.connect(lambda _b: edits.append(1))
    orig = viewer._open_properties_popup
    viewer._open_properties_popup = (
        lambda s, l: (opens.append(1), orig(s, l))[1])
    center = _tap_at_button(viewer, button)
    _tap(viewer, float(center.x()), float(center.y()))
    _tap(viewer, float(center.x()), float(center.y()))
    qtbot.wait(600)
    try:
        assert clicks == [1, 1]
        assert edits == [1]
        assert opens == [1]
    finally:
        viewer._open_properties_popup = orig
        viewer._close_tool_popup()


def test_mouse_double_click_path_intact(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _slot, button = _pen_button(viewer)
    edits = []
    button.editRequested.connect(lambda _b: edits.append(1))
    center = button.rect().center()
    QApplication.sendEvent(
        button,
        QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(center),
                    QPointF(button.mapToGlobal(center)),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier))
    assert edits == [1]
