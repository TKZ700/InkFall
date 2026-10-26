"""Library service: in-memory index over LibraryStore (M4).

Qt-free and deterministic. All filesystem facts come from ``os.stat``
at import/refresh time — PDFs are never opened here. Search matches
title, filename and folder name (case-insensitive substring). Sorting
is total (secondary key: casefolded display name, then id) so results
are stable across runs.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum

from document.manager import normalize_path
from library.errors import LibraryInvalidParentError, LibraryNotFoundError
from library.models import LibraryFolder, LibraryItem, strip_pdf_suffix
from library.store import LibraryStore

__all__ = ["LibraryService", "SearchQuery", "SortKey", "SortOrder"]


class SortKey(Enum):
    """Supported sort dimensions."""

    NAME = "name"
    MODIFIED = "modified"
    CREATED = "created"
    SIZE = "size"
    LAST_OPENED = "last_opened"


class SortOrder(Enum):
    """Sort direction."""

    ASCENDING = "asc"
    DESCENDING = "desc"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Metadata-first search parameters."""

    text: str = ""
    folder_id: str | None = None
    favorites_only: bool = False
    recent_only: bool = False
    recent_limit: int = 12
    include_missing: bool = True


def _stat_facts(path: str) -> tuple[str, float, float, int]:
    """Return (display_name, created, modified, size) from the filesystem."""
    base = path.replace("\\", "/").rsplit("/", 1)[-1] or "Untitled"
    try:
        st = os.stat(path)
        return base, float(st.st_ctime), float(st.st_mtime), int(st.st_size)
    except OSError:
        now = time.time()
        return base, now, now, 0


class LibraryService:
    """Owns library items/folders; persists every mutation immediately."""

    def __init__(self, store: LibraryStore) -> None:
        self._store = store
        self._items: dict[str, LibraryItem] = {}
        self._by_path: dict[str, str] = {}
        self._folders: dict[str, LibraryFolder] = {}
        self.reload()

    # -- lifecycle -------------------------------------------------------

    def reload(self) -> None:
        """Reload all rows from the store (tolerant of corruption)."""
        try:
            items, folders = self._store.load_all()
        except Exception:
            items, folders = [], []
        self._items = {i.id: i for i in items}
        self._by_path = {i.normalized_path: i.id for i in items if i.normalized_path}
        self._folders = {f.id: f for f in folders}

    @property
    def item_count(self) -> int:
        """Number of tracked items."""
        return len(self._items)

    @property
    def folder_count(self) -> int:
        """Number of tracked folders."""
        return len(self._folders)

    def get_item(self, item_id: str) -> LibraryItem | None:
        """Return an item by id, or None."""
        return self._items.get(item_id)

    def get_folder(self, folder_id: str) -> LibraryFolder | None:
        """Return a folder by id, or None."""
        return self._folders.get(folder_id)

    def item_for_path(self, path: str) -> LibraryItem | None:
        """Return the tracked item for ``path``, or None (pure read).

        ``items.normalized_path`` is UNIQUE in the store schema, so the
        path index resolves to at most one entry — the deterministic
        lookup rule for display purposes. Unlike :meth:`import_path`
        and :meth:`touch_opened_path` this never imports or writes.
        """
        try:
            key = normalize_path(path)
        except Exception:
            return None
        item_id = self._by_path.get(key)
        return self._items.get(item_id) if item_id is not None else None

    def folders(self) -> tuple[LibraryFolder, ...]:
        """All folders sorted by name (deterministic)."""
        return tuple(sorted(self._folders.values(), key=lambda f: (f.name.casefold(), f.id)))

    def child_folders(self, parent_id: str | None) -> tuple[LibraryFolder, ...]:
        """Immediate child folders of one parent (None = root), name-sorted."""
        return tuple(sorted(
            (f for f in self._folders.values() if f.parent_id == parent_id),
            key=lambda f: (f.name.casefold(), f.id),
        ))

    def folder_name(self, folder_id: str | None) -> str:
        """Resolve a folder display name ('' for library root)."""
        if folder_id is None:
            return ""
        folder = self._folders.get(folder_id)
        return folder.name if folder is not None else ""

    def item_count_in(self, folder_id: str | None) -> int:
        """Cheap document count for a folder (None = unfilled/root)."""
        if folder_id is None:
            return sum(1 for i in self._items.values() if i.parent_id is None)
        return sum(1 for i in self._items.values() if i.parent_id == folder_id)

    def child_counts(self, folder_id: str | None) -> tuple[int, int]:
        """(subfolder_count, document_count) directly inside one folder."""
        folders = sum(1 for f in self._folders.values() if f.parent_id == folder_id)
        return folders, self.item_count_in(folder_id)

    def is_descendant(self, folder_id: str, ancestor_id: str | None) -> bool:
        """True when ``folder_id`` lies at or below ``ancestor_id``.

        Walks the parent chain with a cycle guard (corrupt rows can
        never loop the walk). ``ancestor_id=None`` is the root, which
        every folder descends from — including unknown folders, so a
        stale id never reads as "safe to move to root" by accident.
        """
        if ancestor_id is None:
            return True
        seen: set[str] = set()
        cursor: str | None = folder_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            if cursor == ancestor_id:
                return True
            folder = self._folders.get(cursor)
            cursor = folder.parent_id if folder is not None else None
        return False

    # -- import ----------------------------------------------------------

    def import_path(self, path: str, folder_id: str | None = None) -> LibraryItem:
        """Track a PDF file. Duplicate paths return the existing entry."""
        key = normalize_path(path)
        existing_id = self._by_path.get(key)
        if existing_id is not None:
            existing = self._items.get(existing_id)
            if existing is not None:
                return existing
            self._by_path.pop(key, None)
        if folder_id is not None and folder_id not in self._folders:
            raise LibraryNotFoundError(f"Unknown folder: {folder_id}")
        # Resolve display path: prefer the existing file location spelling.
        try:
            from pathlib import Path as _P

            display = str(_P(path).expanduser().resolve())
        except Exception:
            display = key
        name, created, modified, size = _stat_facts(display)
        item = LibraryItem(
            normalized_path=key,
            # New entries store the clean display title (final .pdf
            # stripped); rows recorded before this rule render clean
            # anyway via LibraryItem.display_title at read time.
            display_name=strip_pdf_suffix(name),
            parent_id=folder_id,
            created_ts=created,
            modified_ts=modified,
            size_bytes=size,
        )
        item.refresh_missing()
        self._items[item.id] = item
        self._by_path[key] = item.id
        self._store.save_item(item)
        return item

    def refresh_filesystem_facts(self, item_id: str) -> LibraryItem:
        """Re-stat one file (size/mtimes/missing). Persists the result."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        try:
            from pathlib import Path as _P

            display = str(_P(item.normalized_path).expanduser())
        except Exception:
            display = item.normalized_path
        _name, created, modified, size = _stat_facts(display)
        item.created_ts = created
        item.modified_ts = modified
        item.size_bytes = size
        item.refresh_missing()
        self._store.save_item(item)
        return item

    # -- folders ----------------------------------------------------------

    def create_folder(self, name: str, parent_id: str | None = None) -> LibraryFolder:
        """Create a folder (names need not be unique)."""
        if parent_id is not None and parent_id not in self._folders:
            raise LibraryNotFoundError(f"Unknown parent folder: {parent_id}")
        folder = LibraryFolder(name=name, parent_id=parent_id)
        self._folders[folder.id] = folder
        self._store.save_folder(folder)
        return folder

    def rename_folder(self, folder_id: str, name: str) -> LibraryFolder:
        """Rename a folder (persists)."""
        folder = self._folders.get(folder_id)
        if folder is None:
            raise LibraryNotFoundError(f"Unknown folder: {folder_id}")
        folder.name = (name or "").strip() or "Untitled folder"
        self._store.save_folder(folder)
        return folder

    def move_folder(self, folder_id: str, new_parent_id: str | None) -> LibraryFolder:
        """Move a folder under a new parent (persists).

        Organization only: the folder row's ``parent_id`` changes and
        no filesystem path is touched. Moving a folder into itself or
        into one of its descendants is rejected BEFORE any mutation
        (the whole parent chain of the target is validated first).
        """
        folder = self._folders.get(folder_id)
        if folder is None:
            raise LibraryNotFoundError(f"Unknown folder: {folder_id}")
        if new_parent_id is not None and new_parent_id not in self._folders:
            raise LibraryNotFoundError(f"Unknown parent folder: {new_parent_id}")
        seen: set[str] = set()
        cursor: str | None = new_parent_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            if cursor == folder_id:
                raise LibraryInvalidParentError(
                    "Cannot move a folder into itself or its descendants")
            parent = self._folders.get(cursor)
            cursor = parent.parent_id if parent is not None else None
        folder.parent_id = new_parent_id
        self._store.save_folder(folder)
        return folder

    def delete_folder(self, folder_id: str) -> None:
        """Delete a folder; children move up (persists)."""
        if folder_id not in self._folders:
            raise LibraryNotFoundError(f"Unknown folder: {folder_id}")
        parent = self._folders[folder_id].parent_id
        for item in self._items.values():
            if item.parent_id == folder_id:
                item.parent_id = parent
                self._store.save_item(item)
        for folder in self._folders.values():
            if folder.parent_id == folder_id:
                folder.parent_id = parent
                self._store.save_folder(folder)
        del self._folders[folder_id]
        self._store.delete_folder(folder_id)

    # -- item mutations ----------------------------------------------------

    def rename_item(self, item_id: str, name: str) -> LibraryItem:
        """Rename the library display name (does NOT touch the file)."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        item.display_name = (
            (name or "").strip()
            or strip_pdf_suffix(item.file_name)
            or "Untitled"
        )
        self._store.save_item(item)
        return item

    def move_item(self, item_id: str, folder_id: str | None) -> LibraryItem:
        """Move an item between folders (persists)."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        if folder_id is not None and folder_id not in self._folders:
            raise LibraryNotFoundError(f"Unknown folder: {folder_id}")
        item.parent_id = folder_id
        self._store.save_item(item)
        return item

    def set_favorite(self, item_id: str, favorite: bool) -> LibraryItem:
        """Set the favorite flag (persists)."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        item.favorite = bool(favorite)
        self._store.save_item(item)
        return item

    def toggle_favorite(self, item_id: str) -> LibraryItem:
        """Flip the favorite flag (persists)."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        return self.set_favorite(item_id, not item.favorite)

    def touch_opened(self, item_id: str, page_count: int | None = None) -> LibraryItem:
        """Record a successful open (persists recency + optional page count)."""
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        item.last_opened_ts = time.time()
        if page_count is not None and page_count >= 0:
            item.page_count = page_count
        self._store.save_item(item)
        return item

    def touch_opened_path(self, path: str, page_count: int | None = None) -> LibraryItem | None:
        """Record an open by filesystem path (imports if untracked)."""
        try:
            key = normalize_path(path)
        except Exception:
            return None
        item_id = self._by_path.get(key)
        if item_id is None:
            try:
                return self.touch_opened(self.import_path(path).id, page_count)
            except Exception:
                return None
        try:
            return self.touch_opened(item_id, page_count)
        except LibraryNotFoundError:
            return None

    def remove_item(self, item_id: str) -> None:
        """Remove from the library ONLY (never touches the filesystem)."""
        if item_id not in self._items:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        item = self._items.pop(item_id)
        self._by_path.pop(item.normalized_path, None)
        self._store.delete_item(item_id)

    def delete_file(self, item_id: str) -> None:
        """Explicit filesystem delete + library removal.

        This is the ONLY destructive-filesystem path and must always sit
        behind a user confirmation in the UI.
        """
        item = self._items.get(item_id)
        if item is None:
            raise LibraryNotFoundError(f"Unknown item: {item_id}")
        try:
            if os.path.isfile(item.normalized_path):
                os.remove(item.normalized_path)
        except OSError:
            pass
        self.remove_item(item_id)

    # -- query --------------------------------------------------------------

    def query(
        self,
        search: SearchQuery | None = None,
        sort_key: SortKey = SortKey.NAME,
        sort_order: SortOrder = SortOrder.ASCENDING,
    ) -> list[LibraryItem]:
        """Filter + deterministically sort items (pure, testable)."""
        query = search if search is not None else SearchQuery()
        needle = query.text.strip().casefold()
        rows = list(self._items.values())
        if query.folder_id is not None:
            rows = [i for i in rows if i.parent_id == query.folder_id]
        if query.favorites_only:
            rows = [i for i in rows if i.favorite]
        if query.recent_only:
            rows = [i for i in rows if i.last_opened_ts is not None]
            rows.sort(key=lambda i: (-(i.last_opened_ts or 0.0), i.display_name.casefold(), i.id))
            return rows[: max(0, query.recent_limit)]
        if not query.include_missing:
            rows = [i for i in rows if not i.refresh_missing()]
        if needle:
            scored: list[LibraryItem] = []
            for item in rows:
                hay = f"{item.display_name}\n{item.file_name}\n{self.folder_name(item.parent_id)}".casefold()
                if needle in hay:
                    scored.append(item)
            rows = scored
        rows = _sort_rows(rows, sort_key, sort_order)
        return rows


def _sort_rows(
    rows: list[LibraryItem], sort_key: SortKey, sort_order: SortOrder
) -> list[LibraryItem]:
    """Total deterministic ordering for library rows."""
    reverse = sort_order == SortOrder.DESCENDING

    def key(item: LibraryItem) -> tuple:
        if sort_key == SortKey.MODIFIED:
            primary: float | int | str = item.modified_ts
        elif sort_key == SortKey.CREATED:
            primary = item.created_ts
        elif sort_key == SortKey.SIZE:
            primary = item.size_bytes
        elif sort_key == SortKey.LAST_OPENED:
            primary = item.last_opened_ts if item.last_opened_ts is not None else -1.0
        else:
            return (item.display_name.casefold(), item.id)
        if reverse:
            if isinstance(primary, (int, float)):
                return (-primary, item.display_name.casefold(), item.id)
            return (str(primary), item.display_name.casefold(), item.id)
        return (primary, item.display_name.casefold(), item.id)

    return sorted(rows, key=key)
