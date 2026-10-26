"""Library model tests (M4): item creation, folders, normalization."""

import os

from library.models import LibraryFolder, LibraryItem


def test_item_defaults_sane():
    item = LibraryItem(normalized_path="C:/docs/a.pdf", display_name="a.pdf")
    assert item.id
    assert item.favorite is False
    assert item.last_opened_ts is None
    assert item.page_count is None
    assert item.missing is False


def test_item_blank_name_falls_back():
    item = LibraryItem(normalized_path="x", display_name="   ")
    assert item.display_name == "Untitled"


def test_item_negative_size_clamped():
    item = LibraryItem(normalized_path="x", size_bytes=-5)
    assert item.size_bytes == 0


def test_file_name_from_windows_and_posix_paths():
    assert LibraryItem(normalized_path="C:\\Docs\\Report.pdf").file_name == "Report.pdf"
    assert LibraryItem(normalized_path="/home/u/a b.pdf").file_name == "a b.pdf"


def test_refresh_missing_true_and_false(tmp_path):
    real = tmp_path / "a.pdf"
    real.write_bytes(b"%PDF-1.4")
    item = LibraryItem(normalized_path=str(real))
    assert item.refresh_missing() is False
    gone = LibraryItem(normalized_path=str(tmp_path / "nope.pdf"))
    assert gone.refresh_missing() is True


def test_folder_name_fallback_and_nesting():
    root = LibraryFolder(name="Work")
    assert root.parent_id is None
    child = LibraryFolder(name="  ", parent_id=root.id)
    assert child.name == "Untitled folder"
    assert child.parent_id == root.id


def test_ids_unique():
    ids = {LibraryItem(normalized_path=f"p{i}").id for i in range(50)}
    assert len(ids) == 50
    assert len({LibraryFolder(name="f").id for _ in range(20)}) == 20
