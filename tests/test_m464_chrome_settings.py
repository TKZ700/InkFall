"""M4.6.4 chrome/settings/scroll tests: popup, title bar, smoothing.

Covers the §15 matrix: Settings popup, window chrome, categories
polish, scrolling. Thumbnail lifecycle + perf gates live in their
dedicated suites.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView

from app import fx
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
    yield window, service
    store.close()


def _make_pdf(tmp_path, name="doc.pdf", pages=2):
    from tests.helpers import make_rich_pdf

    return make_rich_pdf(str(tmp_path / name), pages=pages)


# -- Settings --------------------------------------------------------------

def test_fab_bottom_right_in_library(shell):
    window, _service = shell
    from app.settings_button import FAB_DIAMETER

    fab = window.settings_fab
    assert fab.isVisible() is True
    assert fab.width() == FAB_DIAMETER and fab.height() == FAB_DIAMETER
    assert fab.pos().x() == window._content.width() - FAB_DIAMETER - 16
    assert fab.pos().y() == window._content.height() - FAB_DIAMETER - 16
    assert fab.toolTip() == ""
    assert fab.accessibleName() != ""
    assert fab.accessibleDescription() != ""
    assert fab.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert fab.parent() is window._content  # never in page layout


def test_fab_hidden_in_workspace_with_panel(shell, tmp_path):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    assert window.settings_fab.isVisible() is False
    assert window.settings_modal.isVisible() is False
    window.close_document()
    assert window.settings_fab.isVisible() is True


def test_workspace_open_closes_popup(shell):
    window, _service = shell
    window.toggle_settings_panel()
    assert window.settings_modal.isVisible() is True
    window._enter_workspace_mode()
    assert window.settings_modal.isVisible() is False
    assert window.settings_fab.isVisible() is False
    window._enter_library_mode()


def test_popup_structure(shell, qtbot):
    window, _service = shell
    panel = window.settings_modal
    window.toggle_settings_panel()
    try:
        assert panel.isVisible() is True
        # M4.6.5 centered modal: 800x540 split-pane, dimmed backdrop.
        assert panel._panel.width() >= 760 and panel._panel.width() <= 840
        assert panel._panel.height() >= 500 and panel._panel.height() <= 580
        assert panel._title.text() == "Settings"
        nav = [b.text() for b in panel._nav_buttons]
        assert nav[:2] == ["Appearance", "Interface"]
        modes = {b.text() for b in panel._mode_buttons.values()}
        assert modes == {"System", "Dark", "Light"}
        # M5-A1: Interface hosts the motion override (Follow/Reduced/Full).
        assert not hasattr(panel, "_motion_check")
        assert not hasattr(panel, "_motion_label")
        motion_modes = {b.text() for b in panel._motion_buttons.values()}
        assert motion_modes == {"Follow system", "Reduced", "Full"}
    finally:
        panel.close_modal()
        qtbot.waitUntil(lambda: panel.isVisible() is False, timeout=5000)


def test_popup_theme_buttons_switch_live(shell, qtbot):
    window, _service = shell
    from app.theme.manager import ThemeMode

    window.toggle_settings_panel()
    try:
        panel = window.settings_modal
        panel._mode_buttons[ThemeMode.LIGHT].click()
        assert window._theme.mode == ThemeMode.LIGHT
        assert panel._mode_buttons[ThemeMode.LIGHT].isChecked()
        panel._mode_buttons[ThemeMode.DARK].click()
        assert window._theme.tokens.name == "dark"
        panel._mode_buttons[ThemeMode.SYSTEM].click()
        assert window._theme.mode == ThemeMode.SYSTEM
    finally:
        window._theme.set_mode(ThemeMode.DARK)
        window.settings_modal.close_modal()
        qtbot.waitUntil(lambda: window.settings_modal.isVisible() is False,
                        timeout=5000)


def test_popup_escape_closes_and_returns_focus(shell, qtbot):
    window, _service = shell
    window.toggle_settings_panel()
    panel = window.settings_modal
    assert panel.isVisible() is True
    qtbot.keyClick(panel, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: panel.isVisible() is False, timeout=5000)


def test_popup_close_returns_focus(shell, qtbot):
    window, _service = shell
    window.toggle_settings_panel()
    window.settings_modal.close_modal()
    qtbot.waitUntil(lambda: window.settings_modal.isVisible() is False,
                    timeout=5000)


def test_panel_extension_point(shell):
    window, _service = shell
    before = len(window.settings_modal._nav_buttons)
    layout = window.settings_modal.add_section("Future")
    assert layout is not None
    # Extension area ships empty (no fictional settings).
    assert len(window.settings_modal._nav_buttons) == before + 1


# -- Window chrome -----------------------------------------------------------

def test_frameless_chrome_present(shell):
    window, _service = shell
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.windowTitle() == "Inkfall"
    bar = window.titlebar
    assert bar.height() >= 36 and bar.height() <= 40
    assert bar._wordmark.text() == "Inkfall"
    assert not bar._icon.pixmap().isNull()
    for btn in (bar._min_btn, bar._max_btn, bar._close_btn):
        assert btn.width() >= 32 and btn.width() <= 36, btn.width()
        assert btn.height() == TITLE_BAR_H
        assert not btn.icon().isNull()
        assert btn.toolTip() == ""
        assert btn.accessibleName() != ""
        assert btn.focusPolicy() & Qt.FocusPolicy.TabFocus


def test_minimize_maximize_restore(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.titlebar.minimizeRequested.emit()
    # M4.6.7: short content soften (~90ms) precedes native minimize.
    qtbot.waitUntil(lambda: window.isMinimized() is True, timeout=5000)
    window.showNormal()
    assert window.isMinimized() is False
    window.titlebar.maximizeToggleRequested.emit()
    # Authoritative-first transitions: native state flips in the toggle.
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    assert window.titlebar._max_btn.property("icon_name") == "restore"
    window.toggle_maximize_restore()
    # M4.6.8: restore dips (~80ms) to mask the compositor snap, then settles.
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    assert window.titlebar._max_btn.property("icon_name") == "maximize"


def test_close_signal_wired(shell):
    window, _service = shell
    # Signal emission path only (closing the fixture window is fatal).
    fired = []
    window.titlebar.closeRequested.connect(lambda: fired.append(True))
    window.titlebar.closeRequested.emit()
    assert fired == [True]


def test_close_releases_resources(qtbot, tmp_path):
    from tests.helpers import make_rich_pdf

    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.show()
    qtbot.addWidget(window)
    pdf = make_rich_pdf(str(tmp_path / "c.pdf"), pages=1)
    item = service.import_path(pdf)
    assert window.open_library_item(item.id) is True
    window.close()
    assert window.viewer.has_document is False
    store.close()


def test_drag_region_and_double_click(shell, qtbot):
    window, _service = shell
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QMouseEvent

    bar = window.titlebar
    assert bar.press_should_drag(QPoint(400, TITLE_BAR_H // 2)) is True
    btn_pos = bar._close_btn.pos() + QPoint(2, 2)
    assert bar.press_should_drag(btn_pos) is False

    def _dblclick(pos):
        from PySide6.QtCore import QPointF

        event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick,
                            QPointF(pos), QPointF(bar.mapToGlobal(pos)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        bar.mouseDoubleClickEvent(event)

    window.showNormal()
    _dblclick(QPoint(400, TITLE_BAR_H // 2))
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    _dblclick(QPoint(400, TITLE_BAR_H // 2))
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)


def test_resize_edges(shell):
    window, _service = shell
    from PySide6.QtCore import QPoint

    w, h = window.width(), window.height()
    assert window._resize_edge_at(QPoint(2, h // 2)) == Qt.Edge.LeftEdge
    assert window._resize_edge_at(QPoint(w - 2, h // 2)) == Qt.Edge.RightEdge
    assert window._resize_edge_at(QPoint(w // 2, h - 2)) == Qt.Edge.BottomEdge
    assert window._resize_edge_at(QPoint(w // 2, 10)) is None  # title bar owns top
    assert window._resize_edge_at(QPoint(w // 2, h // 2)) is None
    window.showMaximized()
    assert window._resize_edge_at(QPoint(2, h // 2)) is None
    window.showNormal()


def test_active_state_styling(shell):
    window, _service = shell
    window.titlebar.set_active(False)
    assert window.titlebar._active is False
    window.titlebar.set_active(True)
    assert window.titlebar._active is True


def test_fullscreen_hides_all_chrome(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    window.toggle_fullscreen()
    # M4.6.6 two-phase transition: state flips in phase 2 (~95ms).
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    assert window.titlebar.isVisible() is False
    assert window._viewer_bar.isVisible() is False
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    assert window.titlebar.isVisible() is True
    assert window.in_workspace is True  # Esc exited fullscreen, not the doc
    window.close_document()


def test_title_context_follows_document(shell, tmp_path):
    window, service = shell
    assert window.titlebar._context.text() == ""
    item = service.import_path(_make_pdf(tmp_path, "report.pdf"))
    assert window.open_library_item(item.id) is True
    assert "report.pdf" in window.titlebar._context.text()
    window.show_library()
    assert window.titlebar._context.text() == ""


# -- Categories polish --------------------------------------------------------

def test_single_click_filters_folder(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tmp_path, "q.pdf"), folder.id)
    window.library_page.refresh()
    model = window.library_page._model
    row_idx = next(i for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None and r.is_folder)
    window.library_page._on_view_clicked(model.index(row_idx, 0))
    assert window.library_page.selected_category_id == folder.id
    assert window.library_page._stack.currentWidget() is window.library_page._view


def test_keyboard_activation_filters_folder(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tmp_path, "q.pdf"), folder.id)
    window.library_page.refresh()
    model = window.library_page._model
    row_idx = next(i for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None and r.is_folder)
    # Enter key path (activated signal): same-page filter, no navigation.
    window.library_page._on_activated(model.index(row_idx, 0))
    assert window.library_page.selected_category_id == folder.id
    assert window.library_page._filter_bar.isVisible() is True


def test_selected_category_state_tracked(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    window.library_page.refresh()
    page = window.library_page
    assert page.selected_category_id is None
    page.select_category(folder.id)
    assert page.selected_category_id == folder.id
    # Delegate reads the same state for its selected wash.
    assert page._delegate.parent() is page


def test_no_category_navigation_chrome(shell):
    window, _service = shell
    page = window.library_page
    for attr in ("set_target", "_target", "_back_btn", "_crumb_wrap",
                 "_go_parent", "_shelf", "_nav_direction"):
        assert not hasattr(page, attr), attr
    assert window._stack is not None  # single stacked page, no sub-pages


# -- Scrolling -----------------------------------------------------------------

def test_library_scroll_config(shell):
    window, _service = shell
    view = window.library_page._view
    assert view.verticalScrollMode() == \
        QAbstractItemView.ScrollMode.ScrollPerPixel
    assert view.horizontalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_glide_retarget_and_release(shell, qtbot):
    window, _service = shell
    bar = window.library_page._view.verticalScrollBar()
    # M4.6.7: let post-show layout (incl. deferred startup) settle so
    # the view doesn't reset the manual range mid-glide.
    qtbot.wait(400)
    bar.setRange(0, 5000)
    bar.setValue(0)
    fx.smooth_scroll_to(bar, 4000, 140, window)
    first = bar.property("_inkfall_glide")
    assert first is not None
    from PySide6.QtCore import QAbstractAnimation

    fx.smooth_scroll_to(bar, 1000, 140, window)  # re-target: kills first
    assert first.state() != QAbstractAnimation.State.Running
    # Velocity-preserving: 4000 + (1000 - 0), clamped to maximum.
    assert bar.property("_inkfall_target") == 5000
    qtbot.waitUntil(lambda: bar.property("_inkfall_glide") is None, timeout=5000)
    assert bar.value() == 5000


def test_glide_accumulates_wheel_ticks(shell):
    window, _service = shell
    bar = window.library_page._view.verticalScrollBar()
    bar.setRange(0, 5000)
    bar.setValue(0)
    fx.smooth_scroll_to(bar, 120, 140, window)
    assert bar.property("_inkfall_target") == 120
    # Second tick extends the in-flight target (no restart stutter).
    fx.smooth_scroll_to(bar, bar.value() + 120, 140, window)
    assert bar.property("_inkfall_target") == 240


def test_viewer_wheel_glides(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=6))
    assert window.open_library_item(item.id) is True
    viewer = window.viewer
    bar = viewer._scrollbar
    # M4.6.7: geometry exists once the deferred open completes.
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=15000)
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    pos = QPoint(100, 100)
    event = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, -240),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    viewer.wheelEvent(event)
    assert bar.property("_inkfall_glide") is not None
    qtbot.waitUntil(lambda: bar.property("_inkfall_glide") is None, timeout=5000)
    assert bar.value() > 0
    window.close_document()


def test_library_glide_no_thumb_rerenders(shell, qtbot, tmp_path):
    window, service = shell
    for i in range(4):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    window.library_page.refresh()
    from app.library.qt_model import Role

    def ready_count():
        model = window.library_page._model
        return sum(1 for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None
                   and not r.is_header and not r.is_folder
                   and model.data(model.index(i, 0), Role.THUMB) is not None)

    qtbot.waitUntil(lambda: ready_count() >= 4, timeout=30000)
    renders = window._thumbs.renders_started
    bar = window.library_page._view.verticalScrollBar()
    for target in (bar.maximum(), 0, bar.maximum(), 0):
        fx.smooth_scroll_to(bar, target, 120, window)
        qtbot.waitUntil(lambda: bar.property("_inkfall_glide") is None,
                        timeout=5000)
    qtbot.wait(400)
    assert window._thumbs.renders_started == renders
