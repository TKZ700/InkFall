"""Library store tests (M4): persistence, reload, corruption tolerance."""

import sqlite3

from library.models import LibraryFolder, LibraryItem
from library.store import LibraryStore


def _store(tmp_path, name="lib.db"):
    store = LibraryStore(str(tmp_path / name))
    store.open()
    return store


def test_round_trip_items_and_folders(tmp_path):
    store = _store(tmp_path)
    try:
        folder = LibraryFolder(name="Work")
        store.save_folder(folder)
        item = LibraryItem(
            normalized_path="C:/docs/a.pdf", display_name="a.pdf", parent_id=folder.id,
            size_bytes=1234, favorite=True, last_opened_ts=1700000000.0, page_count=7,
        )
        store.save_item(item)
        items, folders = store.load_all()
        assert len(folders) == 1 and folders[0].name == "Work"
        assert len(items) == 1
        back = items[0]
        assert back.display_name == "a.pdf"
        assert back.parent_id == folder.id
        assert back.favorite is True
        assert back.page_count == 7
        assert back.last_opened_ts == 1700000000.0
    finally:
        store.close()


def test_reload_across_handles(tmp_path):
    store = _store(tmp_path)
    store.save_item(LibraryItem(normalized_path="k:/a.pdf", display_name="a.pdf"))
    store.close()
    store2 = LibraryStore(str(tmp_path / "lib.db"))
    store2.open()
    try:
        items, _folders = store2.load_all()
        assert len(items) == 1
    finally:
        store2.close()


def test_folder_delete_reparents_children(tmp_path):
    store = _store(tmp_path)
    try:
        parent = LibraryFolder(name="parent")
        child = LibraryFolder(name="child", parent_id=parent.id)
        store.save_folder(parent)
        store.save_folder(child)
        store.save_item(LibraryItem(normalized_path="k:/a.pdf", parent_id=child.id))
        store.delete_folder(child.id)
        items, folders = store.load_all()
        assert [f.id for f in folders] == [parent.id]
        assert items[0].parent_id == parent.id
    finally:
        store.close()


def test_corrupt_rows_skipped_not_fatal(tmp_path):
    store = _store(tmp_path)
    try:
        store.save_item(LibraryItem(normalized_path="k:/ok.pdf", display_name="ok"))
        conn = store._ensure()
        conn.execute(
            "INSERT OR REPLACE INTO items (id, normalized_path, display_name, parent_id,"
            " created_ts, modified_ts, size_bytes, favorite, last_opened_ts,"
            " page_count, added_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("bad1", "k:/bad.pdf", "bad", None, "not-a-float", 0.0, 0, 0, None, None, 0.0),
        )
        conn.commit()
        items, _folders = store.load_all()
        assert [i.normalized_path for i in items] == ["k:/ok.pdf"]
    finally:
        store.close()


def test_garbage_db_file_quarantined_and_starts(tmp_path):
    db = tmp_path / "lib.db"
    db.write_bytes(b"this is not sqlite at all" * 100)
    store = LibraryStore(str(db))
    store.open()
    try:
        items, folders = store.load_all()
        assert items == [] and folders == []
        store.save_item(LibraryItem(normalized_path="k:/a.pdf"))
        assert len(store.load_all()[0]) == 1
    finally:
        store.close()
    backups = list(tmp_path.glob("lib.db.corrupt-*.bak"))
    assert len(backups) == 1
