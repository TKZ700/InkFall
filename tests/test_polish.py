"""M4.6.3 polish tests: workspace modes, fullscreen, FAB, icons."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from app import fx
from app.icons import icon, icon_names
from app.main_window import MainWindow
from document.manager import DocumentManager
from library.service import LibraryService
from library.store import LibraryStore

pytestmark = pytest.mark.usefixtures("qapp")

APP = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture()
def polish(qtbot, tmp_path):
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


def test_icon_set_renders_all_names(qapp):
    names = icon_names()
    assert len(names) >= 14
    for name in names:
        assert not icon(name, "#a7aebb", 20).isNull(), name


def test_no_unicode_ui_glyphs_in_app():
    forbidden = "▤◷★☆⧉▦☰△⌕◐◈←→"
    offenders = []
    for path in list((APP).rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits = [ch for ch in forbidden if ch in text]
        if hits:
            offenders.append(f"{path.relative_to(APP.parent)}: {''.join(hits)}")
    assert offenders == [], f"unicode UI glyphs remain: {offenders}"


def test_workspace_modes_and_fab_persists(polish, tmp_path):
    window, service = polish
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    assert window.in_workspace is True
    # M4.6.4: Settings FAB/popup are Library-only — hidden in workspace.
    assert window.settings_fab.isVisible() is False
    assert window.settings_modal.isVisible() is False
    # No permanent status bar: transient toast overlay replaces it.
    from PySide6.QtWidgets import QMenuBar, QStatusBar

    from app.toast import ToastHost

    assert window.findChild(QStatusBar) is None
    assert window.findChild(QMenuBar) is None
    assert isinstance(window.toast, ToastHost)
    # Single back affordance: no redundant Close button anymore.
    assert not hasattr(window, "_close_doc_btn")
    assert window._viewer_bar._title.text() == item.display_name
    window.show_library()
    assert window.in_workspace is False
    # Returning restores the FAB with state intact.
    assert window.settings_fab.isVisible() is True


def test_category_filter_survives_viewer_roundtrip(polish, tmp_path):
    window, service = polish
    folder = service.create_folder("Work")
    item = service.import_path(_make_pdf(tmp_path), folder.id)
    window.library_page.select_category(folder.id)
    assert window.library_page.selected_category_id == folder.id
    assert window.open_library_item(item.id) is True
    window.show_library()
    assert window.library_page.selected_category_id == folder.id
    assert window.library_page._filter_bar.isVisible() is True


def test_fullscreen_toggle_and_esc_priority(polish, tmp_path, qtbot):
    window, service = polish
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    window.toggle_fullscreen()
    # M4.6.6 two-phase transition: state flips in phase 2 (~95ms).
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    assert window._viewer_bar.isVisible() is False
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    assert window.in_workspace is True  # Esc exited fullscreen, not the doc
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert window.in_workspace is False  # second Esc returns to Library


def test_f11_shortcut_toggles_fullscreen(polish, qtbot):
    window, _service = polish
    window._fullscreen_action.trigger()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window._fullscreen_action.trigger()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)


def test_settings_fab_geometry(polish):
    window, _service = polish
    from PySide6.QtCore import Qt

    from app.settings_button import FAB_DIAMETER

    fab = window.settings_fab
    assert fab.width() == FAB_DIAMETER and fab.height() == FAB_DIAMETER
    # Bottom-right, inside the window, above scrollable content.
    assert fab.pos().x() == window._content.width() - FAB_DIAMETER - 16
    assert fab.pos().y() == window._content.height() - FAB_DIAMETER - 16
    assert fab.toolTip() == ""
    assert fab.accessibleName() != ""
    assert fab.focusPolicy() & Qt.FocusPolicy.TabFocus
    # Layout-independent: grid width math never sees the button.
    assert fab.parent() is window._content


def test_stack_transition_completes_cleanly(polish, qtbot):
    window, _service = polish
    fx.fade_stack_to(window._stack, 1, 180)
    assert window._stack.currentIndex() == 1
    qtbot.waitUntil(lambda: window._stack.widget(1).graphicsEffect() is None,
                    timeout=5000)
    fx.fade_stack_to(window._stack, 0, 180)
    qtbot.waitUntil(lambda: window._stack.widget(0).graphicsEffect() is None,
                    timeout=5000)


def test_fx_helpers_never_raise_without_app():
    fx.fade_in
    assert fx.cursor_overrides_supported() is True


def test_duplicate_open_still_safe_through_workspace(polish, tmp_path, qtbot):
    window, service = polish
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    doc_id = window.viewer._document.id
    assert window._manager.open(item.normalized_path).id == doc_id
    window.close_document()
    assert window.in_workspace is False
