"""Library shell tests (M4.6.3): one workspace, viewer integration."""

import pytest

from app.main_window import MainWindow
from document.manager import DocumentManager
from library.service import LibraryService
from library.store import LibraryStore

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def shell(qtbot, tmp_path):
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


def test_shell_starts_on_library_with_viewer_hosted(shell):
    window, _service = shell
    assert window.windowTitle() == "Inkfall"
    assert window.viewer is not None
    assert window.library_page is not None
    assert window._stack.currentIndex() == 0


def test_single_workspace_no_sidebar_targets(shell, qtbot):
    window, service = shell
    # No sidebar, no navigation targets: one inline workspace.
    assert not hasattr(window, "_sidebar")
    assert not hasattr(window.library_page, "set_target")
    assert window.settings_fab is not None
    assert service.item_count == 0
    window.library_page.refresh()
    assert window.library_page.selected_category_id is None


def test_import_and_grid_list_toggle(shell, tmp_path):
    window, service = shell
    pdf = _make_pdf(tmp_path)
    assert window.library_page.import_paths([pdf]) == 1
    assert service.item_count == 1
    assert window.library_page._model.rowCount() >= 1
    window.library_page.set_list_mode(True)
    assert window.library_page._list_mode is True
    window.library_page.set_list_mode(False)
    assert window.library_page._list_mode is False


def test_open_library_item_uses_manager_and_returns(shell, tmp_path, qtbot):
    window, service = shell
    pdf = _make_pdf(tmp_path, pages=3)
    item = service.import_path(pdf)
    assert window.open_library_item(item.id) is True
    # M4.6.7: workspace switches synchronously; document preparation
    # (and recency bookkeeping) completes deferred.
    assert window._stack.currentIndex() == 1
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    assert service.get_item(item.id).last_opened_ts is not None
    assert service.get_item(item.id).page_count == 3
    window.close_document()
    assert not window.viewer.has_document
    assert window._stack.currentIndex() == 0


def test_reopen_same_pdf_preserves_duplicate_semantics(shell, tmp_path, qtbot):
    window, service = shell
    pdf = _make_pdf(tmp_path)
    item = service.import_path(pdf)
    assert window.open_library_item(item.id) is True
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    # M1 duplicate-open semantics live at the manager level: while the
    # document is open, manager.open returns the SAME managed document.
    manager = window._manager
    doc_id = window.viewer._document.id
    assert manager.open(pdf).id == doc_id
    # Reopening through the library still works (fresh viewer session).
    assert window.open_library_item(item.id) is True
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    window.close_document()


def test_search_and_sort_reflect_in_view(shell, tmp_path):
    window, service = shell
    for name in ("alpha.pdf", "beta.pdf"):
        service.import_path(_make_pdf(tmp_path, name))
    window.library_page.refresh()
    window.library_page._search.setText("alpha")
    window.library_page.refresh()
    rows = [window.library_page._model.row_at(i) for i in range(window.library_page._model.rowCount())]
    docs = [r for r in rows if r is not None and not r.is_header and not r.is_folder]
    headers = [r for r in rows if r is not None and r.is_header]
    assert len(docs) == 1
    assert len(headers) == 1  # Documents section header
    window.library_page._search.clear()
    window.library_page.refresh()
    rows = [window.library_page._model.row_at(i) for i in range(window.library_page._model.rowCount())]
    assert len([r for r in rows if r is not None and not r.is_header and not r.is_folder]) == 2


def test_empty_states_look_intentional(shell):
    window, _service = shell
    window.library_page.refresh()
    assert window.library_page._stack.currentWidget() is window.library_page._empty
    assert "empty" in window.library_page._empty._title.text().casefold()


def test_theme_toggle_applies_both_modes(shell):
    window, _service = shell
    from app.theme.manager import ThemeMode

    # Normalize first: toggle() flips explicit modes, so ambient
    # persisted state (e.g. System) must not leak into the assertion.
    window._theme.set_mode(ThemeMode.DARK)
    window._toggle_theme()
    assert window._theme.mode == ThemeMode.LIGHT
    window._toggle_theme()
    assert window._theme.mode == ThemeMode.DARK
