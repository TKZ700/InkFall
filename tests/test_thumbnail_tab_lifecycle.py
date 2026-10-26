"""Thumbnail lifecycle regression tests (M4.6.1.1, M4.6.3 inline IA).

Section/filter cycles (refresh/model rebind) must never lose an
already-READY thumbnail: the synchronous cache hit repopulates the
current model with zero additional PDF renders.
"""

import pytest

from app.library.qt_model import Role
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


def _thumb_of(window, item_id):
    model = window.library_page._model
    for i in range(model.rowCount()):
        row = model.row_at(i)
        if row is not None and not row.is_header and not row.is_folder \
                and row.item is not None and row.item.id == item_id:
            return model.data(model.index(i, 0), Role.THUMB)
    return None


def _wait_ready(qtbot, window, item_id, timeout=20000):
    qtbot.waitUntil(lambda: _thumb_of(window, item_id) is not None, timeout=timeout)


def _import_ready(qtbot, window, service, tmp_path, name="doc.pdf"):
    item = service.import_path(_make_pdf(tmp_path, name))
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    return item


def test_tab_switch_return_keeps_thumbnail(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    renders = window._thumbs.renders_started
    assert renders == 1
    window.library_page.refresh()
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    assert window._thumbs.renders_started == renders  # 0 additional renders


def test_repeated_switch_return_no_renders(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    assert window._thumbs.renders_started == 1
    for _ in range(3):
        window.library_page.refresh()
        window.library_page.refresh()
        _wait_ready(qtbot, window, item.id)
    assert window._thumbs.renders_started == 1


def test_multiple_pdfs_all_ready_after_tabs(shell, qtbot, tmp_path):
    window, service = shell
    items = [_import_ready(qtbot, window, service, tmp_path, f"d{i}.pdf") for i in range(3)]
    renders = window._thumbs.renders_started
    window.library_page.refresh()
    window.library_page.refresh()
    for item in items:
        _wait_ready(qtbot, window, item.id)
    assert window._thumbs.renders_started == renders


def test_refresh_after_return_keeps_ready(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    window.library_page.refresh()
    window.library_page.refresh()
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    assert window._thumbs.renders_started == 1


def test_sort_after_return_keeps_ready(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    window.library_page.refresh()
    window.library_page.refresh()
    window.library_page._sort_index = 3
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    assert window._thumbs.renders_started == 1


def test_search_clear_keeps_ready(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path, "alpha.pdf")
    _import_ready(qtbot, window, service, tmp_path, "beta.pdf")
    window.library_page._search.setText("alpha")
    window.library_page.refresh()
    window.library_page._search.clear()
    window.library_page.refresh()
    assert _thumb_of(window, item.id) is not None


def test_folder_switch_return_keeps_ready(shell, qtbot, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    item = service.import_path(_make_pdf(tmp_path))
    service.move_item(item.id, folder.id)
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    renders = window._thumbs.renders_started
    # Item lives in the folder: filter to it, clear, filter again —
    # the thumb must resolve in Documents with no new renders.
    window.library_page.select_category(folder.id)
    window.library_page.clear_filter()
    window.library_page.select_category(folder.id)
    assert _thumb_of(window, item.id) is not None
    assert window._thumbs.renders_started == renders


def test_refresh_idempotent_no_duplicates(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    renders = window._thumbs.renders_started
    window.library_page.refresh()
    window.library_page.refresh()
    window.library_page.refresh()
    assert _thumb_of(window, item.id) is not None
    assert window._thumbs.renders_started == renders
    assert item.id not in window.library_page._thumb_failed


def test_stable_item_id_across_reconstruction(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    before = item.id
    # Filter/search/sort cycles never disturb item identity.
    window.library_page.select_category(None)
    window.library_page.refresh()
    window.library_page._search.setText("doc")
    window.library_page.refresh()
    window.library_page._search.clear()
    window.library_page.refresh()
    assert service.get_item(before) is not None
    assert service.get_item(before).id == before


def test_completion_signal_once_per_item(shell, qtbot, tmp_path):
    window, service = shell
    counts: dict = {}
    window._thumbs.ready.connect(lambda i, img: counts.update({i: counts.get(i, 0) + 1}))
    item = service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    _wait_ready(qtbot, window, item.id)
    window.library_page.refresh()
    window.library_page.refresh()
    qtbot.wait(500)
    assert counts.get(item.id, 0) == 1


def test_no_identity_crossover_after_tabs(shell, qtbot, tmp_path):
    window, service = shell
    a = _import_ready(qtbot, window, service, tmp_path, "a.pdf")
    b = _import_ready(qtbot, window, service, tmp_path, "b.pdf", )
    window.library_page.refresh()
    window.library_page.refresh()
    _wait_ready(qtbot, window, a.id)
    _wait_ready(qtbot, window, b.id)
    ta, tb = _thumb_of(window, a.id), _thumb_of(window, b.id)
    assert ta is not None and tb is not None
    assert ta is not tb


def test_missing_and_corrupt_terminate(shell, qtbot, tmp_path):
    import os

    window, service = shell
    ghost = service.import_path(_make_pdf(tmp_path, "ghost.pdf"))
    os.remove(os.path.join(str(tmp_path), "ghost.pdf"))
    bad_path = str(tmp_path / "bad.pdf")
    with open(bad_path, "wb") as f:
        f.write(b"not-a-pdf")
    bad = service.import_path(bad_path)
    window.library_page.refresh()
    qtbot.waitUntil(lambda: ghost.id in window.library_page._thumb_failed, timeout=20000)
    qtbot.waitUntil(lambda: bad.id in window.library_page._thumb_failed, timeout=20000)
    # Section cycles never clear terminal states into limbo.
    window.library_page.refresh()
    window.library_page.refresh()
    assert window._thumbs.renders_started <= 4


def test_recent_section_recovers(shell, qtbot, tmp_path):
    window, service = shell
    item = _import_ready(qtbot, window, service, tmp_path)
    assert window.open_library_item(item.id) is True  # enters recency
    # M4.6.7: recency + first paint land with the deferred finish.
    qtbot.waitUntil(lambda: service.get_item(item.id).last_opened_ts is not None,
                    timeout=15000)
    window.close_document()
    window.library_page.refresh()
    window.library_page.refresh()
    model = window.library_page._model

    def recent_thumb_ready():
        in_recent = False
        for i in range(model.rowCount()):
            row = model.row_at(i)
            if row is not None and row.is_header:
                in_recent = row.header.eyebrow == "Recent"
            if (in_recent and row is not None and not row.is_header and not row.is_folder
                    and row.item is not None and row.item.id == item.id):
                return model.data(model.index(i, 0), Role.THUMB) is not None
        return False

    qtbot.waitUntil(recent_thumb_ready, timeout=20000)
