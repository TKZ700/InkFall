"""M5-B.2.0.1: Windows Ink input-reliability fixes (F1-F3).

Real QTabletEvents (synthetic delivery, offscreen) — never duck-typed
stubs here. Proves toolbar pen activation, single-stream strokes,
unmapped-press handling, and state cleanup. OS-level delivery
(tablet vs tablet+mouse) still requires native hardware verification.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

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
            "test-pen", 1, QInputDevice.DeviceType.Stylus,
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
    pdf = make_probe_pdf(str(tmp_path / "rel.pdf"), page_width=600.0,
                         page_height=800.0)
    assert viewer.open_document(pdf) is True
    qtbot.wait(200)
    return viewer


def _select_pen(viewer):
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="Rel", color="#112233",
                                      width_mm=0.7, opacity=1.0)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(slot)
    return slot


def _mouse_press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_move(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _pen_handler(viewer):
    return viewer._interaction._handler()


# -- F1: toolbar chrome ------------------------------------------------------

def test_pen_tap_clicks_shelf_button_without_stroke(
        qtbot, tmp_path, cleared_settings):
    from app.toolshelf.slot_button import SlotButton

    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    buttons = viewer.tool_shelf.findChildren(SlotButton)
    assert buttons
    target = buttons[0]
    clicks = []
    target.clicked.connect(lambda: clicks.append(1))
    center = target.mapTo(viewer, target.rect().center())
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, center.x(),
                               center.y(), 0.6))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, center.x() + 1,
                               center.y(), 0.6))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, center.x() + 1,
                               center.y(), 0.0))
    assert clicks == [1]
    assert viewer._live_stroke is None
    assert committed_strokes(viewer) == []


def test_pen_tap_on_scrollbar_draws_no_stroke(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    center = viewer._scrollbar.geometry().center()
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, center.x(),
                               center.y(), 0.5))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, center.x(),
                               center.y(), 0.0))
    assert viewer._live_stroke is None
    assert committed_strokes(viewer) == []


def test_chrome_target_resolution(qtbot, tmp_path, cleared_settings):
    from app.toolshelf.slot_button import SlotButton

    viewer = _make_viewer(qtbot, tmp_path)
    target = viewer.tool_shelf.findChildren(SlotButton)[0]
    center = target.mapTo(viewer, target.rect().center())
    assert viewer._tablet_chrome_target(center.x(), center.y()) is target
    assert viewer._tablet_chrome_target(300.0, 300.0) is None


def test_canvas_pen_tap_outside_popup_dismisses_and_draws(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = _select_pen(viewer)
    assert viewer._open_properties_popup(slot, viewer.tool_shelf) is True
    qtbot.wait(600)
    assert viewer._tool_popup is not None
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.5))
    assert viewer._tool_popup is None
    assert viewer._live_stroke is not None
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, 300.0, 150.0, 0.5))
    assert len(committed_strokes(viewer)) == 1
    assert committed_strokes(viewer)[0].pressures == (0.5,)


def test_pen_tap_inside_popup_keeps_it_open(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    slot = _select_pen(viewer)
    assert viewer._open_properties_popup(slot, viewer.tool_shelf) is True
    qtbot.wait(600)
    popup = viewer._tool_popup
    center = popup.geometry().center()
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, center.x(),
                               center.y(), 0.5))
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, center.x(),
                               center.y(), 0.0))
    assert viewer._tool_popup is not None
    assert committed_strokes(viewer) == []
    viewer._close_tool_popup()


# -- F2: single-pointer streams -----------------------------------------------

def test_tablet_then_mouse_press_yields_one_stroke(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    handler = _pen_handler(viewer)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.4,
                               ))
    _mouse_press(viewer, 300.0, 150.0)  # synthesized duplicate: dropped
    assert handler.events.count("down") == 1
    viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, 350.0, 160.0, 0.6))
    _mouse_release(viewer, 350.0, 160.0)  # other stream: must not finalize
    assert viewer._live_stroke is not None
    assert len(committed_strokes(viewer)) == 0
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, 350.0, 160.0, 0.6))
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert [p.x for p in stroke.points][0] == pytest.approx(
        viewer._event_to_page_pt(300.0, 150.0)[1])
    assert stroke.pressures == (0.4, 0.6)


def test_mouse_then_tablet_press_yields_one_stroke(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    handler = _pen_handler(viewer)
    _mouse_press(viewer, 300.0, 150.0)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.4))
    assert handler.events.count("down") == 1
    viewer.tabletEvent(_tablet(QEvent.Type.TabletMove, 350.0, 160.0, 0.6))
    assert viewer._live_stroke is not None
    # Tablet stream dropped: still exactly the mouse press point.
    assert len(viewer._live_stroke.points) == 1
    _mouse_release(viewer, 350.0, 160.0)
    assert len(committed_strokes(viewer)) == 1
    assert committed_strokes(viewer)[0].pressures == (1.0, 1.0)


def test_tool_switch_clears_stream_guards(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.5))
    assert viewer._tablet_stroke_open is True
    pan_slot = next(s for s in viewer.tool_store.slots
                    if s.kind == ToolType.PAN)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(pan_slot)
    assert viewer._tablet_stroke_open is False
    assert viewer._mouse_stroke_open is False
    assert viewer._live_stroke is None
    # Mouse now pans instead of being dropped.
    _mouse_press(viewer, 300.0, 300.0)
    _mouse_move(viewer, 300.0, 250.0)
    assert viewer._scrollbar.value() > 0


# -- F3: unmapped presses -------------------------------------------------------

def test_margin_tablet_press_ignored_without_state(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    handler = _pen_handler(viewer)
    downs_before = handler.events.count("down")
    ev = _tablet(QEvent.Type.TabletPress, 100.0, 300.0, 0.5)  # gutter
    assert viewer._event_to_page_pt(100.0, 300.0) is None
    viewer.tabletEvent(ev)
    assert handler.events.count("down") == downs_before
    assert viewer._live_stroke is None
    assert viewer._tablet_stroke_open is False
    # A later in-page press still works normally.
    viewer.tabletEvent(_tablet(QEvent.Type.TabletPress, 300.0, 150.0, 0.5))
    assert viewer._live_stroke is not None
    viewer.tabletEvent(_tablet(QEvent.Type.TabletRelease, 300.0, 150.0, 0.5))
    assert len(committed_strokes(viewer)) == 1
