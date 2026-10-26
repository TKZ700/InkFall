"""Library fullscreen chrome regression tests.

The Library shares the single top row with the PDF workspace: one tab
strip plus the minimize / maximize-restore / fullscreen / close controls.
Fullscreen is a window state, not a page: the workspace hides that row for
immersion (top-edge hover reveals it), while the Library keeps it visible
so navigation and window controls stay reachable without restoring the
window.
"""

import pytest
from PySide6.QtCore import QPoint, Qt, SIGNAL

from app.main_window import MainWindow
from app.titlebar import CONTROL_W, TITLE_BAR_H
from document.manager import DocumentManager
from library.service import LibraryService
from library.store import LibraryStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def shell(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setOrganizationName("Inkfall")
        app.setApplicationName("Inkfall")
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.show()
    qtbot.addWidget(window)
    try:
        yield window, service
    finally:
        try:
            window.close_document()
        except Exception:
            pass
        store.close()


def _make_pdf(tmp_path, name="doc.pdf", pages=2):
    from tests.helpers import make_rich_pdf

    return make_rich_pdf(str(tmp_path / name), pages=pages)


def _open_document(window, service, tmp_path, qtbot):
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    return item


def _enter_fullscreen(window, qtbot):
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    assert window._strip_fullscreen is True


def _assert_shared_chrome_visible(window):
    bar = window.titlebar
    assert bar.isVisible() is True
    assert bar.height() == TITLE_BAR_H
    # The same strip widgets as every other page, not a Library copy.
    assert window._tab_strip.isVisible() is True
    assert window._tab_strip.parentWidget() is bar
    for btn in (bar._min_btn, bar._max_btn, bar._fs_btn, bar._close_btn):
        assert btn.isVisible() is True
        assert btn.isEnabled() is True
        assert btn.width() == CONTROL_W


# -- Library in fullscreen -----------------------------------------------------


def test_library_fullscreen_keeps_shared_tabbar_and_window_controls(
        shell, qtbot):
    window, _service = shell
    assert window._stack.currentIndex() == 0

    _enter_fullscreen(window, qtbot)

    assert window._stack.currentIndex() == 0
    _assert_shared_chrome_visible(window)
    assert window.titlebar._fs_btn.accessibleName() == "Exit fullscreen"


def test_fullscreen_toggle_control_enters_and_exits_from_library(
        shell, qtbot):
    window, _service = shell
    bar = window.titlebar

    bar._fs_btn.click()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    _assert_shared_chrome_visible(window)

    bar._fs_btn.click()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.waitUntil(lambda: bar.isVisible() is True, timeout=5000)
    qtbot.waitUntil(lambda: bar.height() == TITLE_BAR_H, timeout=5000)
    assert bar.height() == TITLE_BAR_H
    assert bar._fs_btn.accessibleName() == "Fullscreen mode"


def test_tab_strip_background_gestures_do_not_exit_library_fullscreen(
        shell, qtbot):
    window, _service = shell
    _enter_fullscreen(window, qtbot)

    # Empty strip space (no tabs open): a press starts no system move and
    # a double-click must not restore the window out of fullscreen.
    pos = QPoint(max(8, window._tab_strip.width() // 2), TITLE_BAR_H // 2)
    qtbot.mouseClick(window._tab_strip, Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseDClick(window._tab_strip, Qt.MouseButton.LeftButton, pos=pos)
    qtbot.wait(150)

    assert window.isFullScreen() is True
    assert window._stack.currentIndex() == 0
    _assert_shared_chrome_visible(window)


def test_list_grid_switch_keeps_library_fullscreen_chrome(shell, qtbot):
    window, _service = shell
    page = window.library_page
    _enter_fullscreen(window, qtbot)

    page._list_btn.click()
    assert page._list_mode is True
    _assert_shared_chrome_visible(window)
    assert window.isFullScreen() is True

    page._grid_btn.click()
    assert page._list_mode is False
    _assert_shared_chrome_visible(window)
    assert window.isFullScreen() is True


# -- navigating between pages in fullscreen ------------------------------------


def test_document_to_library_preserves_fullscreen_and_shows_chrome(
        shell, tmp_path, qtbot):
    window, service = shell
    _open_document(window, service, tmp_path, qtbot)
    _enter_fullscreen(window, qtbot)
    assert window._stack.currentIndex() == 1
    assert window.titlebar.isVisible() is False  # workspace stays immersive

    window._tab_strip._home.click()
    qtbot.waitUntil(lambda: window._stack.currentIndex() == 0, timeout=5000)
    qtbot.waitUntil(lambda: window.titlebar.isVisible() is True, timeout=5000)

    assert window.isFullScreen() is True
    _assert_shared_chrome_visible(window)


def test_library_to_document_preserves_fullscreen(shell, tmp_path, qtbot):
    window, service = shell
    _open_document(window, service, tmp_path, qtbot)
    _enter_fullscreen(window, qtbot)

    window.show_library()
    qtbot.waitUntil(lambda: window._stack.currentIndex() == 0, timeout=5000)
    qtbot.waitUntil(lambda: window.titlebar.isVisible() is True, timeout=5000)
    assert window.isFullScreen() is True

    window._tab_strip._tabs[0].click()
    qtbot.waitUntil(lambda: window._stack.currentIndex() == 1, timeout=5000)
    qtbot.waitUntil(lambda: window.titlebar.isVisible() is False,
                    timeout=5000)
    assert window.isFullScreen() is True

    # Still fullscreen: back to the Library restores the persistent row.
    window.show_library()
    qtbot.waitUntil(lambda: window._stack.currentIndex() == 0, timeout=5000)
    qtbot.waitUntil(lambda: window.titlebar.isVisible() is True, timeout=5000)
    assert window.isFullScreen() is True


# -- existing control wiring ---------------------------------------------------


def test_window_controls_keep_their_existing_connections(shell):
    window, _service = shell
    bar = window.titlebar
    for name in ("minimizeRequested", "maximizeToggleRequested",
                 "fullscreenToggleRequested", "closeRequested"):
        assert bar.receivers(SIGNAL(f"{name}()")) == 1, name


def test_maximize_restore_control_still_toggles_windowed_library(
        shell, qtbot):
    window, _service = shell
    bar = window.titlebar
    window.showNormal()

    bar._max_btn.click()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    assert bar._max_btn.property("icon_name") == "restore"

    bar._max_btn.click()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    assert bar._max_btn.property("icon_name") == "maximize"
