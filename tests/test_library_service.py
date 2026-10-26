"""Library service tests (M4): import/search/sort/mutations/safety."""

import os

import pytest

from library.errors import LibraryNotFoundError
from library.service import LibraryService, SearchQuery, SortKey, SortOrder
from library.store import LibraryStore


@pytest.fixture()
def service(tmp_path):
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    svc = LibraryService(store)
    yield svc
    store.close()


def _pdf(tmp_path, name="doc.pdf", size=100):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return str(path)


def test_import_captures_filesystem_facts(service, tmp_path):
    path = _pdf(tmp_path, size=2048)
    item = service.import_path(path)
    assert item.display_name == "doc.pdf"
    assert item.size_bytes >= 2048
    assert item.missing is False
    assert service.item_count == 1


def test_duplicate_import_returns_existing(service, tmp_path):
    path = _pdf(tmp_path)
    first = service.import_path(path)
    # Different spelling/case of the same file must not duplicate.
    again = service.import_path(os.path.join(str(tmp_path), ".", "doc.pdf"))
    assert again.id == first.id
    assert service.item_count == 1


def test_import_never_opens_pdf(service, tmp_path, monkeypatch):
    from document import backend as backend_mod

    def _boom(*args, **kwargs):
        raise AssertionError("library must not open PDFs")

    monkeypatch.setattr(backend_mod.PdfBackend, "open", staticmethod(_boom))
    service.import_path(_pdf(tmp_path))
    assert service.item_count == 1


def test_rename_move_favorite_recent_persist(service, tmp_path):
    item = service.import_path(_pdf(tmp_path, "a.pdf"))
    folder = service.create_folder("Work")
    service.rename_item(item.id, "My Report")
    service.move_item(item.id, folder.id)
    service.set_favorite(item.id, True)
    assert service.get_item(item.id).display_name == "My Report"
    assert service.get_item(item.id).parent_id == folder.id
    service.touch_opened(item.id, page_count=9)
    got = service.get_item(item.id)
    assert got.favorite is True and got.last_opened_ts is not None and got.page_count == 9
    # Reload from disk: everything survived.
    service.reload()
    got2 = service.get_item(item.id)
    assert got2 is not None and got2.display_name == "My Report"
    assert got2.favorite is True and got2.page_count == 9


def test_toggle_favorite_and_unknown_ids(service):
    with pytest.raises(LibraryNotFoundError):
        service.toggle_favorite("nope")
    with pytest.raises(LibraryNotFoundError):
        service.rename_item("nope", "x")
    with pytest.raises(LibraryNotFoundError):
        service.move_item("nope", None)


def test_remove_keeps_file_but_delete_removes_it(service, tmp_path):
    keep = _pdf(tmp_path, "keep.pdf")
    item = service.import_path(keep)
    service.remove_item(item.id)
    assert os.path.isfile(keep)  # file untouched
    assert service.item_count == 0

    doomed = _pdf(tmp_path, "doomed.pdf")
    item2 = service.import_path(doomed)
    service.delete_file(item2.id)
    assert not os.path.exists(doomed)
    assert service.item_count == 0


def test_missing_file_handled_gracefully(service, tmp_path):
    path = _pdf(tmp_path, "ghost.pdf")
    item = service.import_path(path)
    os.remove(path)
    refreshed = service.refresh_filesystem_facts(item.id)
    assert refreshed.missing is True
    # Queries still work and can exclude missing.
    assert len(service.query()) == 1
    assert service.query(SearchQuery(include_missing=False)) == []


def test_search_title_filename_folder(service, tmp_path):
    folder = service.create_folder("Invoices 2026")
    a = service.import_path(_pdf(tmp_path, "report-alpha.pdf"), folder.id)
    service.rename_item(a.id, "Q3 Financial Summary")
    service.import_path(_pdf(tmp_path, "vacation.jpg.pdf"))
    assert [i.id for i in service.query(SearchQuery(text="financial"))] == [a.id]
    assert [i.id for i in service.query(SearchQuery(text="report-alpha"))] == [a.id]
    assert [i.id for i in service.query(SearchQuery(text="invoices"))] == [a.id]
    assert service.query(SearchQuery(text="zzz-no-match")) == []


def test_sort_deterministic(service, tmp_path):
    names = ["delta.pdf", "Alpha.pdf", "charlie.pdf", "bravo.pdf"]
    for n in names:
        service.import_path(_pdf(tmp_path, n))
    got = [i.display_name for i in service.query(sort_key=SortKey.NAME)]
    assert got == ["Alpha.pdf", "bravo.pdf", "charlie.pdf", "delta.pdf"]
    # Size sort is total too (ties fall back to name).
    by_size = service.query(sort_key=SortKey.SIZE, sort_order=SortOrder.DESCENDING)
    assert len(by_size) == 4


def test_favorites_and_recent_queries(service, tmp_path):
    a = service.import_path(_pdf(tmp_path, "a.pdf"))
    b = service.import_path(_pdf(tmp_path, "b.pdf"))
    service.set_favorite(a.id, True)
    assert [i.id for i in service.query(SearchQuery(favorites_only=True))] == [a.id]
    service.touch_opened(b.id)
    recent = service.query(SearchQuery(recent_only=True))
    assert [i.id for i in recent] == [b.id]


def test_folder_hierarchy_counts(service, tmp_path):
    root = service.create_folder("Root")
    sub = service.create_folder("Sub", parent_id=root.id)
    service.import_path(_pdf(tmp_path, "a.pdf"), sub.id)
    service.import_path(_pdf(tmp_path, "b.pdf"), root.id)
    assert service.item_count_in(sub.id) == 1
    assert service.item_count_in(root.id) == 1
    service.delete_folder(sub.id)
    # Child item moved up to the deleted folder's parent.
    assert service.item_count_in(root.id) == 2
