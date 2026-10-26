"""Maximize/restore single-click acceptance (M5-A1 window-chrome item).

Every transition here is driven by a REAL QTest.mouseClick on the actual
titlebar maximize button with production-like animation durations
(motion mode "full") — never by signal emission or direct
toggle_maximize_restore(), which bypass the press/release/clicked
delivery path where the double-press bug lived.

Root cause covered: _save_normal_geom_once() used to save transitional
compositor geometry when a maximize/fullscreen entry landed inside the
restore-settle window (native state already Normal, DWM geometry still
in flight), permanently poisoning exact-restore. The reclick test below
reproduces that ordering deterministically and fails without the
_restoring_geom guard.
"""

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import fx
from app.main_window import MainWindow
from document.manager import DocumentManager
from library.service import LibraryService
from library.store import LibraryStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def shell(qtbot, tmp_path):
    app = QApplication.instance()
    if app is not None:
        app.setOrganizationName("Inkfall")
        app.setApplicationName("Inkfall")
    fx.set_motion_mode("full")  # production-like durations: seams observable
    assert fx._effective_duration(200) == 200
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.resize(900, 650)
    window.move(100, 80)
    window.show()
    window.showNormal()
    qtbot.addWidget(window)
    yield window, service
    store.close()
    try:
        fx.set_motion_mode("follow")
    except Exception:
        pass


def _clicks(btn):
    """Exactly-once clicked-event counter for delivery assertions."""
    fired = []
    btn.clicked.connect(lambda: fired.append(True))
    return fired


def _click_once(qtbot, window, btn, fired):
    """One real mouse click; asserts exactly one clicked event delivered."""
    before = len(fired)
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(fired) == before + 1, timeout=5000)
    return fired


def _settle(qtbot, window, want_max):
    qtbot.waitUntil(lambda: window.isMaximized() is want_max, timeout=8000)
    qtbot.wait(300)  # fade-back / enforce settle


def _glyph(window):
    return window.titlebar._max_btn.property("icon_name")


def test_single_click_maximize_then_restore(shell, qtbot):
    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    assert window.isMaximized() is False

    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    assert _glyph(window) == "restore"

    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert _glyph(window) == "maximize"
    assert len(fired) == 2  # exactly one clicked per mouse click


def test_five_consecutive_cycles(shell, qtbot):
    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    for _ in range(5):
        _click_once(qtbot, window, btn, fired)
        _settle(qtbot, window, True)
        _click_once(qtbot, window, btn, fired)
        _settle(qtbot, window, False)
    assert len(fired) == 10
    assert window.isMaximized() is False
    assert _glyph(window) == "maximize"


def test_reclick_during_restore_settle_preserves_normal_rect(shell, qtbot):
    """Deterministic repro of the poisoned-rect root cause.

    Holds the restore settle open (slow-compositor stand-in: native state
    Normal, geometry still near-maximized, _restoring_geom set), then
    delivers a second REAL click into that window. Without the
    _restoring_geom guard in _save_normal_geom_once, the transitional
    rect is saved and exact-restore is corrupted.
    """
    from PySide6.QtCore import QRect

    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    true_normal = QRect(window.geometry())

    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)

    gate = {"hold": True}
    orig_enforce = window._enforce_saved_normal_geom

    def gated_enforce():
        if gate["hold"]:
            QTimer.singleShot(50, gated_enforce)
            return
        orig_enforce()

    window._enforce_saved_normal_geom = gated_enforce
    try:
        _click_once(qtbot, window, btn, fired)  # restore #1
        qtbot.waitUntil(lambda: getattr(window, "_restoring_geom", False) is True,
                        timeout=5000)
        assert window.isMaximized() is False  # native already Normal...
        assert window.geometry().getRect() != true_normal.getRect()  # ...geom in flight
        _click_once(qtbot, window, btn, fired)  # click #2 into the settle
        _settle(qtbot, window, True)  # genuine re-maximize (toggle semantics)
        # ...but the saved rect must still be the true normal rect.
        assert window._normal_geom.getRect() == true_normal.getRect()
    finally:
        gate["hold"] = False
        window._enforce_saved_normal_geom = orig_enforce

    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert window.geometry().getRect() == true_normal.getRect()  # exact restore


def test_cycle_with_pdf_open(shell, qtbot, tmp_path):
    from tests.helpers import make_rich_pdf

    window, service = shell
    item = service.import_path(make_rich_pdf(str(tmp_path / "doc.pdf"), pages=2))
    assert window.open_library_item(item.id) is True
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def _hold(widget, qtbot, ms=450):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton,
                     pos=widget.rect().center())
    qtbot.wait(ms)


def test_maximize_first_click_after_handle_drag(shell, qtbot):
    from PySide6.QtWidgets import QWidget

    window, _service = shell
    shelf = window._viewer.tool_shelf
    handle = shelf._handle
    _hold(handle, qtbot)
    assert QWidget.mouseGrabber() is handle
    QTest.mouseMove(handle, pos=handle.rect().center() + QPoint(80, 0))
    qtbot.wait(100)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    qtbot.wait(150)
    assert QWidget.mouseGrabber() is None
    # First click after the drag must toggle immediately.
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    assert len(fired) == 1


def test_restore_first_click_after_handle_esc(shell, qtbot):
    from PySide6.QtWidgets import QWidget

    window, _service = shell
    shelf = window._viewer.tool_shelf
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    handle = shelf._handle
    _hold(handle, qtbot)
    assert QWidget.mouseGrabber() is handle
    QTest.keyClick(handle, Qt.Key.Key_Escape)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    qtbot.wait(150)
    assert QWidget.mouseGrabber() is None
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def test_maximize_first_click_after_reorder(shell, qtbot):
    from PySide6.QtWidgets import QWidget

    from tools.models import ToolType

    window, _service = shell
    store = window._viewer.tool_store
    a = store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    b = store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    window._viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn_b = window._viewer.tool_shelf._buttons[b.id.value]
    _hold(btn_b, qtbot)
    QTest.mouseMove(
        btn_b, pos=btn_b.rect().center() + QPoint(0, 60))
    qtbot.wait(100)
    assert window._viewer._reordering is True
    QTest.mouseRelease(btn_b, Qt.MouseButton.LeftButton)
    qtbot.wait(150)
    assert window._viewer._reordering is False
    assert QWidget.mouseGrabber() is None
    assert a.id.value != b.id.value
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    assert len(fired) == 1


def test_cycle_after_fullscreen_exit(shell, qtbot, tmp_path):
    from tests.helpers import make_rich_pdf

    window, service = shell
    item = service.import_path(make_rich_pdf(str(tmp_path / "doc.pdf"), pages=1))
    assert window.open_library_item(item.id) is True
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=8000)
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=8000)
    qtbot.wait(400)
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def test_cycle_after_modal_popup_closed(shell, qtbot):
    window, _service = shell
    window.toggle_settings_panel()
    qtbot.waitUntil(lambda: window._settings_modal.isVisible() is True, timeout=5000)
    window.toggle_settings_panel()
    qtbot.waitUntil(lambda: window._settings_modal.isVisible() is False, timeout=5000)
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


@pytest.mark.parametrize("dock", ["top", "left", "right", "bottom"])
def test_cycle_per_toolbar_dock(shell, qtbot, dock):
    window, _service = shell
    window._viewer.tool_shelf.set_dock(dock)
    qtbot.wait(200)
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def test_window_controls_keep_transparent_focus_borders(shell):
    """No blue/persistent focus or pressed border on Min/Max/Close."""
    window, _service = shell
    for btn in (window.titlebar._min_btn, window.titlebar._max_btn,
                window.titlebar._close_btn):
        assert btn.focusPolicy() & Qt.FocusPolicy.TabFocus
        qss = btn.styleSheet()
        assert "transparent" in qss
        assert "outline: 0" in qss


def test_ten_consecutive_cycles(shell, qtbot):
    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    for _ in range(10):
        _click_once(qtbot, window, btn, fired)
        _settle(qtbot, window, True)
        _click_once(qtbot, window, btn, fired)
        _settle(qtbot, window, False)
    assert len(fired) == 20
    assert window.isMaximized() is False
    assert _glyph(window) == "maximize"


def test_cycle_after_move_and_resize(shell, qtbot):
    from PySide6.QtCore import QRect

    window, _service = shell
    window.resize(1000, 700)
    window.move(60, 60)
    qtbot.wait(200)
    want = QRect(window.geometry())
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert window.geometry().getRect() == want.getRect()
    assert len(fired) == 2


def test_cycle_after_minimize_restore(shell, qtbot):
    window, _service = shell
    window.showMinimized()
    qtbot.waitUntil(lambda: window.isMinimized() is True, timeout=8000)
    window.showNormal()
    qtbot.waitUntil(lambda: window.isMinimized() is False, timeout=8000)
    qtbot.wait(300)
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def test_cycle_dark_and_light(shell, qtbot):
    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    window._toggle_theme()
    qtbot.wait(300)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    window._toggle_theme()
    qtbot.wait(300)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 4


def test_rapid_clicks_each_observe_truth(shell, qtbot):    # No tween/fade seam: rapid successive clicks alternate Max/Normal
    # exactly (previously a second click inside the tween re-maximized,
    # stranding users in multi-click recovery loops).
    window, _service = shell
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    expected = [True, False, True, False]
    for want in expected:
        QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
        qtbot.wait(60)
        assert window.isMaximized() is want
    assert len(fired) == 4


def test_stale_notification_does_not_kill_tween(shell, qtbot):
    # A late/duplicate notification about a PRE-tween crossing must not
    # murder a running tween (this killed fullscreen entry after a
    # restore: the tween's own screen-size frames echo back as a
    # hostile Maximized flip, and stale duplicates finished the job).
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QWindowStateChangeEvent
    from PySide6.QtWidgets import QApplication

    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    done = []
    window._tween_window_geometry(QRect(0, 0, 800, 600), 400,
                                  lambda: done.append(True))
    assert window._tweening_geom is True
    qtbot.wait(100)
    # Stale: claims a Maximized->? past while we have been Normal
    # throughout the tween.
    QApplication.postEvent(
        window, QWindowStateChangeEvent(Qt.WindowState.WindowMaximized))
    qtbot.waitUntil(lambda: len(done) == 1, timeout=5000)
    assert window._tweening_geom is False
    window.showNormal()
    qtbot.wait(200)


def test_restore_then_fullscreen_chain(shell, qtbot):
    # The murdered-tween regression: a restore immediately followed by
    # fullscreen entry must reach fullscreen (pending restore settle
    # echoes must neither yank geometry nor abort the entry).
    window, _service = shell
    btn = window.titlebar._max_btn
    _click_once(qtbot, window, btn, _clicks(btn))
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, _clicks(btn))
    _settle(qtbot, window, False)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=8000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=8000)


def _dblclick(widget, qtbot):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QMouseEvent

    pos = QPoint(26, 26)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPointF(pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    widget.mouseDoubleClickEvent(event)
    qtbot.wait(10)


def test_cycle_with_tool_popup_open_then_closed(shell, qtbot, tmp_path):
    from tests.helpers import make_rich_pdf
    from tools.models import ToolType

    window, service = shell
    item = service.import_path(make_rich_pdf(str(tmp_path / "doc.pdf"), pages=1))
    assert window.open_library_item(item.id) is True
    viewer = window._viewer
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer.tool_shelf.refresh()
    _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
    assert viewer._tool_popup is not None
    viewer._close_tool_popup()
    assert viewer._tool_popup is None
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2


def test_cycle_with_add_popup_open_then_closed(shell, qtbot, tmp_path):
    from tests.helpers import make_rich_pdf

    window, service = shell
    item = service.import_path(make_rich_pdf(str(tmp_path / "doc.pdf"), pages=1))
    assert window.open_library_item(item.id) is True
    viewer = window._viewer
    viewer._on_add_requested()
    qtbot.wait(200)
    assert viewer._tool_popup is not None
    viewer._close_tool_popup()
    assert viewer._tool_popup is None
    btn = window.titlebar._max_btn
    fired = _clicks(btn)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, True)
    _click_once(qtbot, window, btn, fired)
    _settle(qtbot, window, False)
    assert len(fired) == 2
