"""M4.6.3 inline-IA tests: one workspace, filter chip, FAB, no sidebar.

Covers the §20 matrix: Library IA, Pins, Categories, Settings, sidebar
removal, scrolling, context menus. Thumbnail lifecycle + perf gates live
in their dedicated suites.
"""

import pytest
from PySide6.QtCore import Qt

from app import fx
from app.main_window import MainWindow
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


def _eyebrows(window):
    model = window.library_page._model
    return [model.row_at(i).header.eyebrow
            for i in range(model.rowCount())
            if (r := model.row_at(i)) is not None and r.is_header]


def _docs_in_section(window, eyebrow):
    """Document rows belonging to a section eyebrow."""
    model = window.library_page._model
    rows, current = [], None
    for i in range(model.rowCount()):
        r = model.row_at(i)
        if r is None:
            continue
        if r.is_header:
            current = r.header.eyebrow
        elif not r.is_folder and r.item is not None and current == eyebrow:
            rows.append(r.item)
    return rows


def _menu_texts(menu):
    """All action labels including submenu entries."""
    labels = []
    for action in menu.actions():
        if action.isSeparator() or not action.text():
            continue
        labels.append(action.text())
        if action.menu() is not None:
            labels.extend(a.text() for a in action.menu().actions()
                          if a.text() and not a.isSeparator())
    return labels


def _dispatch_for(menu, dispatch, label):
    """Find the dispatch callable by action label (incl. submenus)."""
    for action in menu.actions():
        if action.text() == label:
            return dispatch[action]
        if action.menu() is not None:
            for sub in action.menu().actions():
                if sub.text() == label:
                    return dispatch[sub]
    raise AssertionError(f"no menu action {label!r}")


def _doc_row_idx(window, item_id=None):
    model = window.library_page._model
    return next(i for i in range(model.rowCount())
                if (r := model.row_at(i)) is not None
                and not r.is_header and not r.is_folder
                and (item_id is None or r.item.id == item_id))


def _folder_row_idx(window):
    model = window.library_page._model
    return next(i for i in range(model.rowCount())
                if (r := model.row_at(i)) is not None and r.is_folder)


# -- Library IA ------------------------------------------------------------

def test_single_workspace_section_order(shell, tmp_path, qtbot):
    window, service = shell
    folder = service.create_folder("Work")
    a = service.import_path(_make_pdf(tmp_path, "a.pdf"), folder.id)
    b = service.import_path(_make_pdf(tmp_path, "b.pdf"))
    assert window.open_library_item(b.id) is True  # enters recency
    # M4.6.7: recency is recorded by the deferred open finish.
    qtbot.waitUntil(lambda: service.get_item(b.id).last_opened_ts is not None,
                    timeout=15000)
    window.close_document()
    service.toggle_favorite(b.id)  # pinned
    window.library_page.refresh()
    assert _eyebrows(window) == ["Recent", "Categories", "Pinned", "Documents"]
    assert [i.id for i in _docs_in_section(window, "Recent")] == [b.id]
    assert [i.id for i in _docs_in_section(window, "Pinned")] == [b.id]
    assert {i.id for i in _docs_in_section(window, "Documents")} == {a.id, b.id}


def test_no_sidebar_widget_or_chrome(shell):
    window, _service = shell
    assert not hasattr(window, "_sidebar")
    assert not hasattr(window, "sidebar")
    # No collapse/compact/navigation remnants anywhere in the shell.
    for attr in ("set_compact", "is_compact", "compactChanged", "themeToggle",
                 "newFolderRequested", "set_active"):
        assert not hasattr(window, attr), attr
        assert not hasattr(window.library_page, attr), attr


def test_no_navigation_api(shell):
    window, _service = shell
    page = window.library_page
    for attr in ("set_target", "_target", "_back_btn", "_crumb_wrap",
                 "_go_parent", "_shelf", "_shelf_model", "_nav_direction"):
        assert not hasattr(page, attr), attr
    import app.library.library_page as mod

    assert not hasattr(mod, "NavTarget")
    import app.library as pkg

    assert "NavTarget" not in pkg.__all__


def test_recent_capped_at_eight(shell, tmp_path):
    window, service = shell
    for i in range(12):
        item = service.import_path(_make_pdf(tmp_path, f"d{i:02d}.pdf"))
        service.touch_opened(item.id, page_count=1)
    window.library_page.refresh()
    assert len(_docs_in_section(window, "Recent")) == 8


def test_recent_hidden_when_empty(shell):
    window, _service = shell
    window.library_page.refresh()
    assert "Recent" not in _eyebrows(window)


def test_pinned_hidden_when_empty(shell, tmp_path):
    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    assert "Pinned" not in _eyebrows(window)
    assert "Documents" in _eyebrows(window)


def test_no_reading_progress_persistence(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=4))
    assert not hasattr(item, "progress")
    assert window.open_library_item(item.id) is True
    stored = service.get_item(item.id)
    assert not hasattr(stored, "progress")
    # M4.6.7: page-count bookkeeping lands with the deferred finish.
    qtbot.waitUntil(lambda: service.get_item(item.id).page_count == 4,
                    timeout=15000)
    window.close_document()


# -- Pins ------------------------------------------------------------------

def test_favorite_flag_appears_pinned(shell, tmp_path):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    service.toggle_favorite(item.id)  # zero migration: existing flag
    window.library_page.refresh()
    assert [i.id for i in _docs_in_section(window, "Pinned")] == [item.id]


def test_pin_unpin_immediate_ui_update(shell, tmp_path):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    assert "Pinned" not in _eyebrows(window)
    service.toggle_favorite(item.id)
    window.library_page.refresh()
    assert "Pinned" in _eyebrows(window)
    service.toggle_favorite(item.id)
    window.library_page.refresh()
    assert "Pinned" not in _eyebrows(window)


def test_pin_uses_muted_pin_icon(shell, tmp_path):
    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    icons = window.library_page._delegate._icons
    assert "pin" in icons and not icons["pin"].isNull()
    assert "star" not in icons and "star-filled" not in icons


def test_no_favorites_user_facing_strings():
    import pathlib

    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    bad = []
    for path in app.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "Favorites" in line or "Starred" in line:
                bad.append(f"{path.name}: {line.strip()}")
    assert bad == [], f"old Favorites UI remains: {bad}"


def test_doc_menu_pin_labels(shell, tmp_path):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    page = window.library_page
    menu, dispatch = page._context_menu_for(_doc_row_idx(window))
    try:
        assert "Pin to Pinned" in _menu_texts(menu)
    finally:
        menu.deleteLater()
    # Invoking the dispatch pins immediately (no navigation, no exec).
    menu2, dispatch2 = page._context_menu_for(_doc_row_idx(window))
    try:
        _dispatch_for(menu2, dispatch2, "Pin to Pinned")()
    finally:
        menu2.deleteLater()
    assert service.get_item(item.id).favorite is True
    window.library_page.refresh()
    menu3, _d3 = page._context_menu_for(_doc_row_idx(window))
    try:
        assert "Unpin from Pinned" in _menu_texts(menu3)
    finally:
        menu3.deleteLater()


# -- Categories ------------------------------------------------------------

def test_category_click_filters_in_place(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Physics")
    service.import_path(_make_pdf(tmp_path, "q.pdf"), folder.id)
    service.import_path(_make_pdf(tmp_path, "root.pdf"))
    window.library_page.refresh()
    window.library_page.select_category(folder.id)
    assert window.library_page.selected_category_id == folder.id
    assert window.library_page._filter_bar.isVisible() is True
    assert "Physics" in window.library_page._filter_label.text()
    # Same page: stack still shows the view, no navigation occurred.
    assert window.library_page._stack.currentWidget() is window.library_page._view
    assert [i.display_name for i in _docs_in_section(window, "Documents")] == ["q.pdf"]
    # Categories section now shows children (none) — stays inline.
    assert "Categories" not in _eyebrows(window)


def test_filter_clear_returns_to_all(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Physics")
    service.import_path(_make_pdf(tmp_path, "q.pdf"), folder.id)
    service.import_path(_make_pdf(tmp_path, "root.pdf"))
    window.library_page.select_category(folder.id)
    window.library_page.clear_filter()
    assert window.library_page.selected_category_id is None
    assert window.library_page._filter_bar.isHidden() is True
    assert {i.display_name for i in _docs_in_section(window, "Documents")} == \
        {"q.pdf", "root.pdf"}


def test_subcategories_listed_inline(shell, tmp_path):
    window, service = shell
    parent = service.create_folder("University")
    child = service.create_folder("Physics", parent.id)
    window.library_page.refresh()
    model = window.library_page._model
    top = [model.row_at(i).folder.name for i in range(model.rowCount())
           if (r := model.row_at(i)) is not None and r.is_folder]
    assert top == ["University"]
    window.library_page.select_category(parent.id)
    model = window.library_page._model
    kids = [model.row_at(i).folder.name for i in range(model.rowCount())
            if (r := model.row_at(i)) is not None and r.is_folder]
    assert kids == ["Physics"]
    assert window.library_page._filter_label.text() == "Documents — University"
    # One level deeper: the trail names the full inline path.
    window.library_page.select_category(child.id)
    assert window.library_page._filter_label.text() == \
        "Documents — University / Physics"
    assert service.get_folder(child.id).parent_id == parent.id
    window.library_page.clear_filter()


def test_search_sort_work_with_filter(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tmp_path, "alpha.pdf"), folder.id)
    service.import_path(_make_pdf(tmp_path, "beta.pdf"), folder.id)
    service.import_path(_make_pdf(tmp_path, "alpha-root.pdf"))
    window.library_page.select_category(folder.id)
    window.library_page._search.setText("alpha")
    window.library_page.refresh()
    assert [i.display_name for i in _docs_in_section(window, "Documents")] == ["alpha.pdf"]
    window.library_page._search.clear()
    window.library_page._sort_index = 0
    window.library_page.refresh()
    assert len(_docs_in_section(window, "Documents")) == 2
    window.library_page.clear_filter()


def test_category_crud_and_safe_delete(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    item = service.import_path(_make_pdf(tmp_path), folder.id)
    service.rename_folder(folder.id, "Play")
    assert service.folder_name(folder.id) == "Play"
    assert window.library_page.folder_count(folder.id) == 1
    service.delete_folder(folder.id)
    assert service.get_item(item.id).parent_id is None  # re-parented, file kept
    assert service.get_folder(folder.id) is None
    window.library_page.refresh()
    assert "Categories" not in _eyebrows(window)


def test_delete_selected_category_clears_filter(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    window.library_page.select_category(folder.id)
    service.delete_folder(folder.id)
    window.library_page.refresh()  # stale filter self-heals
    assert window.library_page.selected_category_id is None
    assert window.library_page._filter_bar.isHidden() is True


def test_category_menu_actions(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    window.library_page.refresh()
    page = window.library_page
    menu, dispatch = page._context_menu_for(_folder_row_idx(window))
    try:
        for label in ("Show documents here", "Rename…", "New subcategory…",
                      "Delete category…"):
            assert label in _menu_texts(menu), _menu_texts(menu)
        # "Show documents here" filters in place (same page, no target).
        _dispatch_for(menu, dispatch, "Show documents here")()
    finally:
        menu.deleteLater()
    assert page.selected_category_id == folder.id
    assert page._filter_bar.isVisible() is True


def test_no_per_category_widgets(shell, tmp_path):
    window, service = shell
    for i in range(3):
        service.create_folder(f"C{i}")
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    window.library_page.refresh()
    model = window.library_page._model
    for i in range(model.rowCount()):
        assert window.library_page._view.indexWidget(model.index(i, 0)) is None


# -- Settings (M4.6.4: popup panel; matrix lives in test_m464) ----------

def test_settings_panel_owned_by_shell(shell):
    window, _service = shell
    assert window.settings_modal is not None
    assert window.settings_modal.isVisible() is False


def test_theme_mode_persists(shell):
    window, _service = shell
    from PySide6.QtCore import QSettings

    from app.theme.manager import ThemeMode

    window._theme.set_mode(ThemeMode.LIGHT)
    assert window._theme.mode == ThemeMode.LIGHT
    assert QSettings().value("inkfall/theme/mode", "") == ThemeMode.LIGHT
    window._theme.set_mode(ThemeMode.DARK)
    assert window._theme.tokens.name == "dark"


def test_available_modes_include_system():
    from app.theme.manager import ThemeManager

    assert set(ThemeManager.available_modes()) == {"dark", "light", "system"}


def test_system_mode_live_update_preserves_state(shell, tmp_path):
    window, service = shell
    from unittest import mock

    from app.theme.manager import ThemeMode

    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tmp_path, "alpha.pdf"), folder.id)
    window.library_page.select_category(folder.id)
    window.library_page._search.setText("alpha")
    window.library_page.refresh()
    rows_before = window.library_page._model.rowCount()
    window._theme.set_mode(ThemeMode.SYSTEM)
    # Simulate the OS flipping scheme while running.
    first = window._theme.tokens.name
    with mock.patch.object(type(window._theme), "_system_is_dark",
                           return_value=(first != "dark")):
        window._theme._on_system_scheme_changed(None)
    assert window._theme.tokens.name != first
    # Nothing disturbed: filter, search, sort, rows, thumbs all intact.
    assert window.library_page.selected_category_id == folder.id
    assert window.library_page._search.text() == "alpha"
    assert window.library_page._sort_index == 0
    assert window.library_page._model.rowCount() == rows_before
    window._theme.set_mode(ThemeMode.DARK)


def test_system_scheme_ignored_unless_system(shell):
    window, _service = shell
    from app.theme.manager import ThemeMode

    window._theme.set_mode(ThemeMode.DARK)
    before = window._theme.tokens
    window._theme._on_system_scheme_changed(None)
    assert window._theme.tokens is before
    window._theme.set_mode(ThemeMode.DARK)


# -- Scrolling ---------------------------------------------------------------

def test_scroll_configuration(shell):
    window, _service = shell
    from PySide6.QtWidgets import QAbstractItemView

    view = window.library_page._view
    assert view.verticalScrollMode() == \
        QAbstractItemView.ScrollMode.ScrollPerPixel
    assert view.horizontalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_smooth_scroll_helper_bounded(shell, qtbot):
    from app import fx as _fx

    bar = shell[0].library_page._view.verticalScrollBar()
    bar.setRange(0, 1000)
    bar.setValue(0)
    _fx.smooth_scroll_to(bar, 10**9)  # clamped to maximum, then glides
    qtbot.waitUntil(lambda: bar.property("_inkfall_glide") is None, timeout=5000)
    assert bar.value() == bar.maximum()


def test_no_per_card_animation_in_delegate():
    import inspect

    from app.library import delegate as d

    src = inspect.getsource(d)
    assert "QPropertyAnimation" not in src
    assert "QGraphicsOpacityEffect" not in src


# -- Context menus -------------------------------------------------------------

def test_doc_menu_identity_and_empty_space(shell, tmp_path):
    window, service = shell
    a = service.import_path(_make_pdf(tmp_path, "a.pdf"))
    window.library_page.refresh()
    page = window.library_page
    menu, _dispatch = page._context_menu_for(_doc_row_idx(window, a.id))
    try:
        for label in ("Open", "Rename…", "Move to category", "Library root",
                      "Remove from library…", "Delete file…"):
            assert label in _menu_texts(menu), _menu_texts(menu)
    finally:
        menu.deleteLater()
    # Header rows get no menu.
    model = page._model
    header_idx = next(i for i in range(model.rowCount())
                      if (r := model.row_at(i)) is not None and r.is_header)
    assert page._context_menu_for(header_idx) is None
    # Empty space: only global actions, never document identity.
    menu_empty, _de = page._context_menu_for(-1)
    try:
        assert set(_menu_texts(menu_empty)) == {"Import PDFs…", "New category…"}
    finally:
        menu_empty.deleteLater()
