"""SQLite persistence for the library (M4). Qt-free, stdlib only.

Design: one file, two tables (``folders``, ``items``), integer
``user_version`` for future migrations. The API is deliberately small:

- :meth:`LibraryStore.open` / :meth:`close`
- :meth:`save_item` / :meth:`save_folder` (upserts)
- :meth:`delete_item` / :meth:`delete_folder` (folder delete reparents
  children to the deleted folder's parent)
- :meth:`load_all` -> ``(items, folders)`` skipping corrupt rows

Corrupt/stale rows never prevent startup: decoding is wrapped per-row
and bad rows are skipped. An unreadable database file is quarantined
(rename to ``*.corrupt-<ts>.bak``) and replaced with a fresh schema so
Inkfall always starts.
"""

from __future__ import annotations

import os
import sqlite3
import time

from library.models import LibraryFolder, LibraryItem

__all__ = ["LibraryStore", "SCHEMA_VERSION"]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT,
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    normalized_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    parent_id TEXT,
    created_ts REAL NOT NULL,
    modified_ts REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    favorite INTEGER NOT NULL,
    last_opened_ts REAL,
    page_count INTEGER,
    added_ts REAL NOT NULL
);
"""


class LibraryStore:
    """Tiny SQLite owner for library rows."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> str:
        """Filesystem path of the database file."""
        return self._db_path

    def open(self) -> None:
        """Open (creating parent dirs) and ensure schema, tolerantly."""
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()
        except sqlite3.DatabaseError:
            self._quarantine_and_recreate()

    def _quarantine_and_recreate(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        try:
            if os.path.isfile(self._db_path):
                bak = f"{self._db_path}.corrupt-{int(time.time())}.bak"
                os.replace(self._db_path, bak)
        except Exception:
            pass
        self._conn = sqlite3.connect(self._db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        """Close the handle (idempotent)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self) -> LibraryStore:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        assert self._conn is not None
        return self._conn

    def save_item(self, item: LibraryItem) -> None:
        """Insert or replace one item row."""
        conn = self._ensure()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO items (id, normalized_path, display_name,"
                " parent_id, created_ts, modified_ts, size_bytes, favorite,"
                " last_opened_ts, page_count, added_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.id,
                    item.normalized_path,
                    item.display_name,
                    item.parent_id,
                    float(item.created_ts),
                    float(item.modified_ts),
                    int(item.size_bytes),
                    1 if item.favorite else 0,
                    item.last_opened_ts,
                    item.page_count,
                    float(item.added_ts),
                ),
            )
            conn.commit()
        except sqlite3.DatabaseError:
            try:
                conn.rollback()
            except Exception:
                pass

    def save_folder(self, folder: LibraryFolder) -> None:
        """Insert or replace one folder row."""
        conn = self._ensure()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO folders (id, name, parent_id, created_ts)"
                " VALUES (?,?,?,?)",
                (folder.id, folder.name, folder.parent_id, float(folder.created_ts)),
            )
            conn.commit()
        except sqlite3.DatabaseError:
            try:
                conn.rollback()
            except Exception:
                pass

    def delete_item(self, item_id: str) -> None:
        """Delete one item row (no filesystem touch)."""
        conn = self._ensure()
        try:
            conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            conn.commit()
        except sqlite3.DatabaseError:
            pass

    def delete_folder(self, folder_id: str) -> None:
        """Delete a folder; children move up to the deleted folder's parent."""
        conn = self._ensure()
        try:
            cur = conn.execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,))
            row = cur.fetchone()
            new_parent = row[0] if row else None
            conn.execute("UPDATE folders SET parent_id=? WHERE parent_id=?", (new_parent, folder_id))
            conn.execute("UPDATE items SET parent_id=? WHERE parent_id=?", (new_parent, folder_id))
            conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            conn.commit()
        except sqlite3.DatabaseError:
            pass

    # -- reads ----------------------------------------------------------

    def load_all(self) -> tuple[list[LibraryItem], list[LibraryFolder]]:
        """Load all rows, skipping corrupt ones. Never raises."""
        conn = self._ensure()
        items: list[LibraryItem] = []
        folders: list[LibraryFolder] = []
        try:
            for row in conn.execute(
                "SELECT id, normalized_path, display_name, parent_id, created_ts,"
                " modified_ts, size_bytes, favorite, last_opened_ts, page_count,"
                " added_ts FROM items"
            ):
                try:
                    items.append(
                        LibraryItem(
                            id=str(row[0]),
                            normalized_path=str(row[1] or ""),
                            display_name=str(row[2] or "Untitled"),
                            parent_id=row[3],
                            created_ts=float(row[4]),
                            modified_ts=float(row[5]),
                            size_bytes=int(row[6] or 0),
                            favorite=bool(row[7]),
                            last_opened_ts=row[8],
                            page_count=row[9],
                            added_ts=float(row[10]),
                        )
                    )
                except Exception:
                    continue
        except sqlite3.DatabaseError:
            pass
        try:
            for row in conn.execute("SELECT id, name, parent_id, created_ts FROM folders"):
                try:
                    folders.append(
                        LibraryFolder(
                            id=str(row[0]),
                            name=str(row[1] or "Untitled folder"),
                            parent_id=row[2],
                            created_ts=float(row[3]),
                        )
                    )
                except Exception:
                    continue
        except sqlite3.DatabaseError:
            pass
        return items, folders
