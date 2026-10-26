"""Qt list model over LibraryService results (M4).

The model is a thin, dumb projection: filtering/sorting live in the
Qt-free service (testable), this model only maps rows to Qt roles.
Section/folder rows are represented as distinct row types so the
delegate can paint them without extra queries.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, Qt

__all__ = ["LibraryRow", "LibraryListModel", "Role", "MIME_LIBRARY_ITEM"]

from library.models import LibraryFolder, LibraryItem

#: Inkfall-internal drag payload: newline-separated stable library item
#: ids. Carried by view-level drags only — external PDF imports (URL
#: drags) never carry this type, so the two flows cannot be confused.
MIME_LIBRARY_ITEM = "application/x-inkfall-library-item"


class Role:
    """Custom model roles."""

    ITEM_ID = Qt.ItemDataRole.UserRole + 1
    ITEM = Qt.ItemDataRole.UserRole + 2
    IS_FOLDER = Qt.ItemDataRole.UserRole + 3
    FOLDER = Qt.ItemDataRole.UserRole + 4
    THUMB = Qt.ItemDataRole.UserRole + 5
    IS_HEADER = Qt.ItemDataRole.UserRole + 6
    HEADER = Qt.ItemDataRole.UserRole + 7
    META = Qt.ItemDataRole.UserRole + 8
    IS_HINT = Qt.ItemDataRole.UserRole + 9
    HINT = Qt.ItemDataRole.UserRole + 10


@dataclass(slots=True)
class SectionHeader:
    """A section divider (eyebrow + title + count + collapse state).

    ``key`` identifies the section (recent/folders/ungrouped at Home;
    search-result headers carry no key and never collapse);
    ``collapsed`` is a paint-time snapshot owned by the page. Collapse
    is a session-only visibility toggle — Recent Opened and Folders
    launch expanded, Ungrouped Books launches collapsed, and nothing
    is persisted across restarts.
    """

    eyebrow: str = ""
    title: str = ""
    count: int = 0
    key: str = ""
    collapsed: bool = False


@dataclass(slots=True)
class LibraryRow:
    """One view row: section header, folder tile, hint line, or document.

    ``section`` tags a document row with its surface ("recent" for the
    Recent Opened shelf, "search" for search results) so the delegate
    can vary the meta line; ``meta_override`` carries the precomputed
    meta text (relative time, location trail). ``is_hint`` rows paint a
    quiet single-line explanation (e.g. the empty Recent shelf).
    """

    is_folder: bool
    item: LibraryItem | None = None
    folder: LibraryFolder | None = None
    count: int = 0
    is_header: bool = False
    header: SectionHeader | None = None
    is_hint: bool = False
    hint: str = ""
    section: str = ""
    meta_override: str = ""


class LibraryListModel(QAbstractListModel):
    """Snapshot model; call :meth:`set_rows` after each service query."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[LibraryRow] = []
        self._thumbs: dict[str, object] = {}

    def set_rows(self, rows: list[LibraryRow]) -> None:
        """Replace all rows (resets the view)."""
        self.beginResetModel()
        self._rows = list(rows)
        # Drop thumbnails for rows that vanished.
        alive = {r.item.id for r in self._rows if not r.is_folder and r.item is not None}
        for key in [k for k in self._thumbs if k not in alive]:
            self._thumbs.pop(key, None)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def row_at(self, row: int) -> LibraryRow | None:
        """Return the row object (for controllers, bypassing roles)."""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def flags(self, index: QModelIndex):  # noqa: N802
        """Row capabilities for selection, navigation, and drag & drop.

        Document rows are drag sources (books can be dragged onto
        folders); folder rows are the ONLY drop targets. Headers stay
        keyboard-navigable so Enter/Space toggles collapse; the
        delegate paints no selection chrome on them and clicks never
        open anything. Hint rows are inert.
        """
        flags = super().flags(index)
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return flags
        row = self._rows[index.row()]
        if row.is_header or row.is_hint:
            if row.is_hint:
                # Hint rows (empty-state lines) are inert explanations:
                # never selectable/openable/draggable like a book.
                return flags & ~Qt.ItemFlag.ItemIsSelectable
            return flags
        if row.is_folder:
            return flags | Qt.ItemFlag.ItemIsDropEnabled
        if row.item is not None:
            return flags | Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def supportedDragActions(self):  # noqa: N802
        """Library drags MOVE entries between folders (organization only)."""
        return Qt.DropAction.MoveAction

    def supportedDropActions(self):  # noqa: N802
        return Qt.DropAction.MoveAction

    def mimeTypes(self):  # noqa: N802
        return [MIME_LIBRARY_ITEM]

    def mimeData(self, indexes):  # noqa: N802
        """Encode the dragged rows' stable item ids (documents only)."""
        mime = QMimeData()
        ids: list[str] = []
        for index in indexes:
            row = self.row_at(index.row()) if index.isValid() else None
            if (row is not None and not row.is_folder and not row.is_header
                    and not row.is_hint and row.item is not None):
                ids.append(row.item.id)
        if ids:
            mime.setData(MIME_LIBRARY_ITEM, "\n".join(ids).encode("utf-8"))
        return mime

    def dropMimeData(self, data, action, row, column, parent):  # noqa: N802
        """Never mutate the model through the default drop path.

        Book→folder drops are handled (and persisted via the service)
        by the view's ``dropEvent``; returning False here guarantees
        Qt's built-in row-insertion semantics can never fire.
        """
        return False

    def data(self, index: QModelIndex, role=int(Qt.ItemDataRole.DisplayRole)):  # noqa: N802
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == Role.IS_HEADER:
            return row.is_header
        if role == Role.IS_HINT:
            return row.is_hint
        if row.is_header:
            if role == Role.HEADER:
                return row.header
            if role == Qt.ItemDataRole.DisplayRole:
                return row.header.title if row.header else ""
            return None
        if row.is_hint:
            if role == Role.HINT:
                return row.hint
            return None
        if role == Role.IS_FOLDER:
            return row.is_folder
        if row.is_folder:
            if role == Role.FOLDER:
                return row.folder
            if role == Role.ITEM_ID:
                return f"folder:{row.folder.id}" if row.folder else None
            if role == Qt.ItemDataRole.DisplayRole:
                return row.folder.name if row.folder else ""
            return None
        item = row.item
        if item is None:
            return None
        if role == Role.ITEM_ID:
            return item.id
        if role == Role.ITEM:
            return item
        if role == Role.THUMB:
            return self._thumbs.get(item.id)
        if role == Role.META:
            return row.meta_override
        if role == Qt.ItemDataRole.DisplayRole:
            # Clean display title everywhere (final .pdf stripped by the
            # single shared helper on the item itself).
            return item.display_title
        return None

    def set_thumbnail(self, item_id: str, image: object) -> bool:
        """Attach a loaded thumbnail; returns True if a row was updated."""
        self._thumbs[item_id] = image
        for i, row in enumerate(self._rows):
            if not row.is_folder and row.item is not None and row.item.id == item_id:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [Role.THUMB])
                return True
        return False

    def mark_failed(self, item_id: str) -> bool:
        """Record a thumbnail failure (delegate paints the failure state)."""
        self._thumbs[item_id] = None
        for i, row in enumerate(self._rows):
            if not row.is_folder and row.item is not None and row.item.id == item_id:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [Role.THUMB])
                return True
        return False
