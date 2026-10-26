"""Home / Library page — a file explorer for books.

One spacious full-window surface, no sidebar and no details panel:
Home shows three sections in order — Recent Opened, Folders (the root
folders, the entry point into the hierarchy) and Ungrouped Books
(root-level PDFs, collapsed at launch) — and folders open IN PLACE
like a file explorer. The content area always lists the current
folder's immediate children (subfolders first, then books); a
breadcrumb trail in the header leads back through ancestors. Books can
be dragged onto folder cards; Search is global and groups folder +
book matches with location context.

Metadata-first: rows come from LibraryService queries only — PDFs are
never opened to populate the view. Thumbnails load lazily through
ThumbnailService; the view cancels off-screen work on scroll.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDrag, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from app.icons import app_icon, icon
from app.library.delegate import LibraryDelegate
from app.library.dialogs import LibraryConfirmModal, LibraryPromptModal
from app.library.qt_model import (
    MIME_LIBRARY_ITEM,
    LibraryListModel,
    LibraryRow,
    Role,
    SectionHeader,
)
from app.menu_material import InkfallMenu, add_submenu
from app.modal import ModalButton
from app.theme.multilingual import font_for
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font
from app.thumbnails.service import ThumbnailService
from library.service import LibraryService, SearchQuery, SortKey, SortOrder

__all__ = ["LibraryPage"]

_RECENT_CAP = 8

# Collapsible Home sections (visibility-only, SESSION-scoped): Recent
# Opened and Folders launch expanded; Ungrouped Books launches
# COLLAPSED (its books stay in the library untouched). Nothing is
# persisted across restarts.
_SECTION_KEYS = ("recent", "folders", "ungrouped")
_LAUNCH_COLLAPSED = ("ungrouped",)

_SORT_OPTIONS: list[tuple[str, SortKey, SortOrder]] = [
    ("Name", SortKey.NAME, SortOrder.ASCENDING),
    ("Recently modified", SortKey.MODIFIED, SortOrder.DESCENDING),
    ("Recently created", SortKey.CREATED, SortOrder.DESCENDING),
    ("Largest first", SortKey.SIZE, SortOrder.DESCENDING),
    ("Last opened", SortKey.LAST_OPENED, SortOrder.DESCENDING),
]


def _header(eyebrow: str, title: str, count: int, key: str = "") -> LibraryRow:
    """Build a section header row (collapsible divider)."""
    return LibraryRow(is_folder=False, is_header=True,
                      header=SectionHeader(eyebrow=eyebrow, title=title,
                                           count=count, key=key))


def _relative_time(ts: float | None) -> str:
    """Compact relative label for the Recent Opened meta line."""
    if ts is None or ts <= 0.0:
        return ""
    delta = time.time() - ts
    if delta < 60.0:
        return "Opened just now"
    if delta < 3600.0:
        return f"Opened {int(delta // 60)}m ago"
    if delta < 86400.0:
        return f"Opened {int(delta // 3600)}h ago"
    if delta < 7 * 86400.0:
        return f"Opened {int(delta // 86400)}d ago"
    opened = time.localtime(ts)
    now = time.localtime()
    if opened.tm_year == now.tm_year:
        return time.strftime("Opened %b %d", opened)
    return time.strftime("Opened %b %d, %Y", opened)


class _LibraryView(QListView):
    """Library list view with book→folder drag & drop.

    Document rows are drag sources (the model flags them); folder rows
    are the only drop targets. Drags carry the Inkfall-internal MIME
    type with the stable library item id; URL drags (external PDF
    import) are ignored here so they keep propagating to the page's
    import handler. Drops persist through ``LibraryService.move_item``
    — the view never mutates the model directly.
    """

    def __init__(self, page: "LibraryPage") -> None:
        super().__init__(page)
        self._page = page

    def _apply_drag_config(self) -> None:
        """Reassert drag/drop configuration after layout-mode changes.

        QListView derives drag/drop enablement from its movement setting
        inside setMovement() and setViewMode(). Static movement is
        required to prevent icon repositioning, so explicitly restore
        the intended drag/drop configuration after layout changes.
        """
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(False)
        # Drops are delivered to the viewport. setMovement(Static) clears
        # the viewport's acceptance while leaving the view's own flag set;
        # setAcceptDrops(True) is then a no-op, so restore it explicitly.
        self.viewport().setAcceptDrops(True)

    # -- drag sources ------------------------------------------------------

    def startDrag(self, actions) -> None:  # noqa: N802
        """Drag the pressed book with a card ghost (Qt threshold rules).

        The default flow already applies the drag-distance threshold
        before calling this. The ghost render is the ONLY fallible part
        and is contained on its own (a render failure degrades to a
        ghost-less drag — it must never silently abort the feature, and
        QDrag construction/exec run OUTSIDE any handler so a wiring bug
        surfaces instead of hiding).
        """
        index = self.currentIndex()
        row = self._page._model.row_at(index.row()) if index.isValid() else None
        if row is None or row.is_folder or row.is_header or row.is_hint \
                or row.item is None:
            return
        rect = self.visualRect(index)
        if rect.isEmpty():
            return
        pixmap = self._drag_pixmap(index, rect)
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData([index]))
        if pixmap is not None:
            drag.setPixmap(pixmap)
            drag.setHotSpot(QPoint(rect.width() // 2, rect.height() // 2))
        # Explicit supported + default actions: the acceptance chain
        # below answers with MoveAction unconditionally, so the drag
        # must propose MoveAction too (a proposed IgnoreAction would
        # make every acceptProposedAction a silent no-op).
        drag.exec(Qt.DropAction.MoveAction, Qt.DropAction.MoveAction)
        # The drag loop consumes the mouse release, so the view's own
        # state machine never sees it — reset explicitly so a finished
        # drag can never swallow the next click.
        try:
            self.setState(QAbstractItemView.State.NoState)
        except Exception:
            pass

    def _drag_pixmap(self, index, rect):
        """Delegate-rendered ghost of the card, or None (contained)."""
        try:
            dpr = float(self.devicePixelRatioF() or 1.0)
            pixmap = QPixmap(max(1, int(rect.width() * dpr)),
                             max(1, int(rect.height() * dpr)))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                if painter.isActive():
                    option = QStyleOptionViewItem()
                    option.rect = QRect(0, 0, rect.width(), rect.height())
                    option.widget = self
                    self.itemDelegate().paint(painter, option, index)
            finally:
                try:
                    if painter.isActive():
                        painter.end()
                except Exception:
                    pass
            return pixmap
        except Exception:
            return None

    # -- drop targets --------------------------------------------------------

    def _is_internal_drag(self, event) -> bool:
        """True when the drag carries this view's internal MIME payload.

        Discriminates by MIME type alone: only this view's drags
        produce ``MIME_LIBRARY_ITEM``, and external PDF imports carry
        URLs and never this type. The drag source object is NOT
        compared (``event.source() is self``) — that identity check
        depends on the binding returning the identical Python wrapper
        inside drag events, and a mismatch here silently kills the
        whole accept/move/drop chain (the drag then propagates to the
        page, which rejects it as a non-PDF, and nothing happens).
        """
        try:
            return event.mimeData().hasFormat(MIME_LIBRARY_ITEM)
        except Exception:
            return False

    def _folder_id_at(self, pos) -> str | None:
        try:
            index = self.indexAt(pos)
            if not index.isValid():
                return None
            row = self._page._model.row_at(index.row())
            if row is None or not row.is_folder or row.folder is None:
                return None
            return row.folder.id
        except Exception:
            return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._is_internal_drag(event):
            self._accept_move(event)
        else:
            # URL drags keep propagating to the page's import handler.
            self._page._set_drop_target(None)
            event.ignore()

    def _accept_move(self, event) -> None:
        """Accept a drag with MoveAction set explicitly.

        ``acceptProposedAction()`` would inherit whatever action the
        drag manager proposed; pinning MoveAction here and in
        dragMoveEvent makes the acceptance independent of that
        runtime-provided value.
        """
        try:
            event.setDropAction(Qt.DropAction.MoveAction)
        except Exception:
            pass
        event.accept()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not self._is_internal_drag(event):
            self._page._set_drop_target(None)
            event.ignore()
            return
        pos = event.position().toPoint()
        folder_id = self._folder_id_at(pos)
        item_id = self._page._mime_item_id(event.mimeData())
        item = self._page._service.get_item(item_id) if item_id else None
        if folder_id is None or item is None or item.parent_id == folder_id:
            # Only folder cards accept drops; the book's own current
            # folder is a no-op target (no highlight, forbidden cursor).
            self._page._set_drop_target(None)
            event.ignore()
            return
        self._page._set_drop_target(folder_id)
        self._accept_move(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._page._set_drop_target(None)
        super().dragLeaveEvent(event)
        try:
            self.setState(QAbstractItemView.State.NoState)
        except Exception:
            pass

    def dropEvent(self, event) -> None:  # noqa: N802
        page = self._page
        folder_id = self._folder_id_at(event.position().toPoint())
        item_id = page._mime_item_id(event.mimeData())
        page._set_drop_target(None)
        try:
            self.setState(QAbstractItemView.State.NoState)
        except Exception:
            pass
        if not self._is_internal_drag(event) or item_id is None:
            event.ignore()
            return
        if folder_id is None:
            # Released over a non-folder row or empty space: no change.
            page.statusMessage.emit("Drop books onto a folder", "")
            event.ignore()
            return
        item = page._service.get_item(item_id)
        folder = page._service.get_folder(folder_id)
        if item is None or folder is None:
            # Stale payload: the entry vanished mid-drag.
            page.statusMessage.emit("That entry no longer exists", "")
            event.ignore()
            return
        if item.parent_id == folder_id:
            event.ignore()  # duplicate move: already there, nothing to do
            return
        try:
            page._service.move_item(item_id, folder_id)
        except Exception as exc:
            page.statusMessage.emit("Cannot move document", str(exc))
            event.ignore()
            return
        self._accept_move(event)
        page.refresh()
        page.statusMessage.emit(f"Moved to “{folder.name}”", "")


class _EmptyState(QWidget):
    """Intentional, polished empty state with one or two actions."""

    actioned = Signal()
    actioned2 = Signal()

    def __init__(self, tokens: InkfallTokens | None = None, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)
        self._art = QLabel()
        self._art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = QLabel("Nothing here yet")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint = QLabel("")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setProperty("class", "secondary")
        self._hint.setWordWrap(True)
        self._button = QPushButton("Import PDFs")
        self._button.setProperty("class", "primary")
        self._button.clicked.connect(self.actioned.emit)
        self._button2 = QPushButton("New Folder")
        self._button2.clicked.connect(self.actioned2.emit)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        actions.addWidget(self._button)
        actions.addWidget(self._button2)
        layout.addWidget(self._art)
        layout.addWidget(self._title)
        layout.addWidget(self._hint)
        layout.addLayout(actions)
        if tokens is not None:
            self.update_tokens(tokens)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle for a theme switch."""
        self._title.setFont(role_font(tokens, "section"))
        self._hint.setFont(role_font(tokens, "body"))
        self._tokens = tokens
        # M4.6.7: primary action keeps a keyboard-only token ring
        # (TabFocus: mouse clicks never focus it; outline suppressed).
        for btn in (self._button, self._button2):
            btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            btn.setStyleSheet(
                f"QPushButton:focus {{ border: 1px solid {tokens.input_focus};"
                f" border-radius: 8px; outline: 0; }}"
            )

    def show_state(self, title: str, hint: str, action: str, art: str = "empty",
                   action2: str = "") -> None:
        """Configure the empty-state copy (second action optional)."""
        tokens = getattr(self, "_tokens", None)
        if tokens is not None:
            art_pm = icon(art, tokens.text_muted, 56).pixmap(56, 56)
            self._art.setPixmap(art_pm)
        self._title.setText(title)
        self._hint.setText(hint)
        self._button.setText(action)
        self._button.setVisible(bool(action))
        self._button2.setText(action2)
        self._button2.setVisible(bool(action2))


class LibraryPage(QWidget):
    """One Library workspace: header, inline sections, filter chip."""

    openRequested = Signal(str)  # item_id
    statusMessage = Signal(str, str)
    newRequested = Signal()  # circular New button -> shell opens New panel

    def __init__(
        self,
        service: LibraryService,
        thumbs: ThumbnailService,
        tokens: InkfallTokens,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._thumbs = thumbs
        self._tokens = tokens
        self._current_folder_id: str | None = None  # None = Home root
        self._navigation_changed = False  # consumed by refresh() for motion
        # Explorer scroll memory: scrollbar value per folder for the
        # session, so going back restores where the user was.
        self._scroll_memory: dict[str | None, int] = {}
        self._thumb_failed: set[str] = set()
        self._auto_retried: set[str] = set()
        self._list_mode = False
        # Session-only collapsible sections: visibility-only collapse
        # state (never persisted; Recent Opened and Folders launch
        # expanded, Ungrouped Books launches COLLAPSED).
        self._collapsed: dict[str, bool] = {k: True for k in _LAUNCH_COLLAPSED}
        # Book→folder drag & drop: the folder currently highlighted as
        # the drop target (None while no drag hovers a folder card).
        self._drop_folder_id: str | None = None
        # M4.6.7 section toggle animation: ONE view-level animator
        # (chevron rotation only; content uses the container fade).
        self._section_anim_key: str | None = None
        self._section_from = False
        self._section_progress = 1.0
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        self._section_anim = QVariantAnimation(self)
        self._section_anim.setDuration(160)
        self._section_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._section_anim.setStartValue(0.0)
        self._section_anim.setEndValue(1.0)
        self._section_anim.valueChanged.connect(self._on_section_tick)
        self._section_anim.finished.connect(self._on_section_done)
        # M4.6.8 category press animation: ONE view-level animator for
        # the pressed tile only (1-3% compact + release settle, 130ms
        # OutCubic). Same-page filter behavior is untouched.
        self._cat_anim_id: str | None = None
        self._cat_progress = 1.0
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        self._cat_anim = QVariantAnimation(self)
        self._cat_anim.setDuration(130)
        self._cat_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._cat_anim.setStartValue(0.0)
        self._cat_anim.setEndValue(1.0)
        self._cat_anim.valueChanged.connect(self._on_cat_tick)
        self._cat_anim.finished.connect(self._on_cat_done)
        # M4.6.9 section-header hover: ONE view-level animator (140ms
        # OutCubic). Headers never lift — the animator only fades the
        # restrained hover surface (wash + accent chevron + indicator).
        self._hdr_anim_key: str | None = None
        self._hdr_progress = 0.0
        # M4.6.10 shared category-hover animator: ONE view-level
        # 140ms OutCubic progress for the inset tile surface (no
        # per-category objects or widgets).
        self._cat_hover_key: str | None = None
        self._cat_hover_p = 0.0
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        self._hdr_anim = QVariantAnimation(self)
        self._hdr_anim.setDuration(140)
        self._hdr_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._hdr_anim.setStartValue(0.0)
        self._hdr_anim.setEndValue(1.0)
        self._hdr_anim.valueChanged.connect(self._on_hdr_tick)
        self._hdr_anim.finished.connect(self._on_hdr_done)
        self._cat_hover_anim = QVariantAnimation(self)
        self._cat_hover_anim.setDuration(140)
        self._cat_hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._cat_hover_anim.setStartValue(0.0)
        self._cat_hover_anim.setEndValue(1.0)
        self._cat_hover_anim.valueChanged.connect(self._on_cat_hover_tick)
        self._cat_hover_anim.finished.connect(self._on_cat_hover_done)
        # M4.6.6 single-click open guard: suppress only SAME-document
        # re-opens inside the window (clicked+activated both fire on
        # double-click/Enter). A=>B rapid clicks always open both.
        self._last_open_id: str | None = None
        self._last_open_at = 0.0
        # M4.6.5 hover/lift: ONE view-level animator (no per-card timers
        # or widgets). `_hover_key=(row, item_id)` selects the exact
        # visible occurrence; the delegate reads `_hover_progress`
        # (0.0–1.0) while painting it. M4.6.12: 170ms; enter OutQuart,
        # leave OutCubic (per retarget in _hover_glide_to); ticks
        # repaint the previous + current hover rects only
        # (shadow/pin included via margin). M4.6.11: occurrence-scoped
        # so Recent/Documents duplicates animate independently
        # (`_hover_id` kept synced for legacy tests).
        self._hover_id: str | None = None
        self._hover_prev_id: str | None = None
        self._hover_key: tuple[int, str] | None = None
        self._hover_prev_key: tuple[int, str] | None = None
        self._hover_progress = 0.0
        # M4.6.11 press micro-interaction: ONE view-level 85ms
        # OutCubic animator for the clicked occurrence only (1-2%
        # compress, never gates opening).
        self._press_key: tuple[int, str] | None = None
        self._press_progress = 0.0
        from PySide6.QtCore import QEasingCurve, QVariantAnimation

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(170)
        self._hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._hover_anim.setStartValue(0.0)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.valueChanged.connect(self._on_hover_tick)
        self._hover_anim.finished.connect(self._on_hover_done)
        self._press_anim = QVariantAnimation(self)
        self._press_anim.setDuration(85)
        self._press_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._press_anim.setStartValue(0.0)
        self._press_anim.setEndValue(1.0)
        self._press_anim.valueChanged.connect(self._on_press_tick)
        self._press_anim.finished.connect(self._on_press_done)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        _body = QWidget()
        _body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._body = _body  # M4.6.8: width-harness hook (no cap).
        body = QVBoxLayout(_body)
        body.setContentsMargins(
            tokens.content_margin, 20, tokens.content_margin, 16
        )
        body.setSpacing(tokens.section_gap)
        # M4.6.8 live-width truth: the Library body owns the full client
        # width at every window size (no max-width cap, no trailing
        # stretch). The grid formula reflows from the live viewport, so
        # maximizing grows columns instead of opening a dead right-side
        # strip; cards stay in the compact range at any width.
        root.addWidget(_body, stretch=1)

        # -- header row 1: identity | search + New ------------------------
        header = QHBoxLayout()
        header.setSpacing(12)
        self._brand_icon = QLabel()
        brand_pm = app_icon().pixmap(24, 24)
        if not brand_pm.isNull():
            self._brand_icon.setPixmap(brand_pm)
        else:
            self._brand_icon.setPixmap(
                icon("library", tokens.text_secondary, 24).pixmap(24, 24))
        header.addWidget(self._brand_icon, alignment=Qt.AlignmentFlag.AlignVCenter)
        brand_col = QVBoxLayout()
        brand_col.setSpacing(2)
        # Dynamic page heading: "Home Library" at the root, the current
        # folder's name while browsing (file-explorer location title).
        self._heading = QLabel("Home Library")
        self._heading.setFont(role_font(tokens, "title"))
        brand_col.addWidget(self._heading)
        self._context = QLabel("")
        self._context.setFont(role_font(tokens, "meta"))
        self._context.setStyleSheet(f"color: {tokens.text_muted};")
        brand_col.addWidget(self._context)
        header.addLayout(brand_col)
        header.addStretch(1)
        # Circular Search (M4.6.5): icon-only resting state paired with
        # New; click expands a field in place (see _expand_search).
        # M4.6.11: shared ModalButton (140ms hover paint + 90ms press).
        self._search_btn = ModalButton("", circular=True)
        self._search_btn.setFixedSize(38, 38)
        self._search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._search_btn.setAccessibleName("Search library")
        self._search_btn.setAccessibleDescription("Expand the search field")
        self._search_btn.clicked.connect(self._expand_search)
        header.addWidget(self._search_btn)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search library…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(34)
        self._search.setMinimumWidth(0)
        self._search.setMaximumWidth(0)
        self._search.setVisible(False)
        self._search.addAction(
            icon("search", tokens.text_muted, tokens.icon_sm),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self._search.setAccessibleName("Search library")
        self._search.setAccessibleDescription(
            "Search library metadata (title, filename, folder)")
        self._search.installEventFilter(self)
        header.addWidget(self._search)
        # Circular New Folder (M refinement): compact folder-plus icon
        # beside Add Book, same 38px diameter and language. Context
        # aware — Home creates a root folder; inside a folder it
        # creates a subfolder there.
        self._new_folder_btn = ModalButton("", circular=True)
        self._new_folder_btn.setFixedSize(38, 38)
        self._new_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_folder_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._new_folder_btn.setAccessibleName("New Folder")
        self._new_folder_btn.setAccessibleDescription(
            "Create a folder in the current library location")
        self._new_folder_btn.setToolTip("New Folder")
        # clicked(bool) would bind `checked` onto parent_id and corrupt
        # the target folder — drop the signal argument explicitly.
        self._new_folder_btn.clicked.connect(
            lambda _checked=False: self.create_folder_dialog())
        header.addWidget(self._new_folder_btn)
        # Circular New action (M4.6.5): icon-only, paired with Search.
        # Opens the centered New panel via the shell (newRequested).
        # M4.6.11: shared ModalButton.
        self._new_btn = ModalButton("", circular=True)
        self._new_btn.setFixedSize(38, 38)
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._new_btn.setAccessibleName("New: create blank or open PDF")
        self._new_btn.setAccessibleDescription("Create a blank document or open a PDF")
        self._new_btn.clicked.connect(self.newRequested.emit)
        header.addWidget(self._new_btn)
        body.addLayout(header)

        # -- header row 2: breadcrumb + view controls ----------------------
        # Sort sits immediately LEFT of Grid/List (8-12px rhythm); the
        # New Folder affordance lives beside Add Book in header row 1.
        sub = QHBoxLayout()
        sub.setSpacing(10)
        self._sort_index = 0
        self._sort_menu = None  # owned popup (closed on retrigger)
        self._sort_btn = ModalButton("", radius=8)
        self._sort_btn.setFixedHeight(32)
        self._sort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # M4.6.7: TabFocus only — mouse clicks never leave a focus
        # ring; keyboard Tab keeps the token ring (stylesheet).
        self._sort_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._sort_btn.setAccessibleName("Sort library")
        self._sort_btn.setAccessibleDescription("Change the sort order")
        self._sort_btn.clicked.connect(self._on_sort_menu)
        sub.addStretch(1)
        sub.addWidget(self._sort_btn)
        seg = QWidget()
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(0)
        self._grid_btn = ModalButton("", radius=6)
        self._grid_btn.setIcon(icon("grid", tokens.text_secondary, tokens.icon_sm))
        self._grid_btn.setCheckable(True)
        self._grid_btn.setChecked(True)
        self._grid_btn.setFixedSize(34, 28)
        self._grid_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._grid_btn.setAccessibleName("Grid view")
        self._grid_btn.setAccessibleDescription("Show documents as a grid")
        self._list_btn = ModalButton("", radius=6)
        self._list_btn.setIcon(icon("list", tokens.text_secondary, tokens.icon_sm))
        self._list_btn.setCheckable(True)
        self._list_btn.setFixedSize(34, 28)
        self._list_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._list_btn.setAccessibleName("List view")
        self._list_btn.setAccessibleDescription("Show documents as a list")
        seg_layout.addWidget(self._grid_btn)
        seg_layout.addWidget(self._list_btn)
        self._seg = seg
        sub.addWidget(seg)
        body.addLayout(sub)

        # -- breadcrumb trail (visible only while inside a folder) --------
        # Flat crumb buttons separated by chevrons: Home Library / Math /
        # 11th Grade. Every crumb navigates; the bar rebuilds per move.
        self._crumbs_bar = QWidget()
        crumbs_layout = QHBoxLayout(self._crumbs_bar)
        crumbs_layout.setContentsMargins(0, 0, 0, 0)
        crumbs_layout.setSpacing(2)
        crumbs_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._crumbs_layout = crumbs_layout
        body.addWidget(self._crumbs_bar)
        self._crumbs_bar.setVisible(False)
        self._refresh_chrome_tokens(tokens)

        # -- content: one virtualized view ---------------------------------
        self._stack = QStackedWidget()
        self._view = _LibraryView(self)
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setSpacing(tokens.grid_gap_h)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setUniformItemSizes(False)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.setAccessibleName("Library documents")
        self._view.setAccessibleDescription("Click to open. Right-click for actions.")
        # Book→folder drag & drop: books start drags after Qt's normal
        # threshold; folder rows accept drops (handled in _LibraryView).
        # MoveAction only — the model's default drop path is disabled.
        # Placed after setMovement/setViewMode: both derive drag/drop
        # enablement from the movement setting, and set_list_mode's
        # later setViewMode re-runs that derivation, so the toggle
        # re-asserts it too.
        self._view._apply_drag_config()
        self._model = LibraryListModel(self)
        self._delegate = LibraryDelegate(tokens, self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self.setAcceptDrops(True)
        self._empty = _EmptyState(tokens)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._empty)

        body.addWidget(self._stack, stretch=1)

        # -- wiring ------------------------------------------------------------
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self.refresh)
        self._search.textChanged.connect(lambda _t: self._search_timer.start())
        self._grid_btn.clicked.connect(lambda: self.set_list_mode(False))
        self._list_btn.clicked.connect(lambda: self.set_list_mode(True))
        self._empty.actioned.connect(self._empty_action)
        self._empty.actioned2.connect(self._empty_action2)
        self._view.activated.connect(self._on_activated)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.verticalScrollBar().valueChanged.connect(lambda _v: self._schedule_thumb_pass())
        # Smooth wheel: Qt-native per-pixel steps + the shared fx glide
        # (re-targetable). Virtualization and lazy thumbs are untouched —
        # only scrollbar values move. The viewport filter also tracks
        # hover for the single lift animator (HoverMove needs tracking).
        self._view.viewport().installEventFilter(self)
        self._view.viewport().setMouseTracking(True)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(120)
        self._thumb_timer.timeout.connect(self._thumb_pass)
        self._thumbs.ready.connect(self._on_thumb_ready)
        self._thumbs.failed.connect(self._on_thumb_failed)
        self._thumbs.stale.connect(self._on_thumb_stale)
        self._view.clicked.connect(self._on_view_clicked)
        delete_action = QAction("Remove", self)
        delete_action.setShortcut(Qt.Key.Key_Delete)
        delete_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        delete_action.triggered.connect(self._remove_selected)
        self._view.addAction(delete_action)
        # Library organization dialogs (shared modal language). One
        # prompt + one confirm, reused per flow via a stored callback;
        # page-local overlays that close when the page hides.
        self._prompt_modal = LibraryPromptModal(tokens, self)
        self._confirm_modal = LibraryConfirmModal(tokens, self)
        self._prompt_callback = None
        self._confirm_callback = None
        self._prompt_modal.confirmed.connect(self._on_prompt_confirmed)
        self._confirm_modal.confirmed.connect(self._on_confirm_confirmed)
        # Initial population: the model starts empty until the first query.
        self._sync_sort_label()
        self.refresh()

    # -- theme ---------------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Apply a theme switch live."""
        self._tokens = tokens
        self._delegate.update_tokens(tokens)
        self._heading.setFont(role_font(tokens, "title"))
        self._context.setFont(role_font(tokens, "meta"))
        self._empty.update_tokens(tokens)
        self._prompt_modal.update_tokens(tokens)
        self._confirm_modal.update_tokens(tokens)
        self._refresh_chrome_tokens(tokens)
        self._view.viewport().update()

    def _refresh_chrome_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle header chrome (segmented control, sort, New, filter).

        M4.6.11: all chrome buttons share ModalButton painted hover
        (140ms) + press dip (90ms); stylesheets stay transparent-idle
        so hover never snaps via QSS.
        """
        t = tokens
        from app import fx as _fx

        def _wire(btn) -> None:
            try:
                btn.set_tokens(t)
            except Exception:
                pass
            try:
                if not getattr(btn, "_m411_press_wired", False):
                    btn._m411_press_wired = True
                    btn.pressed.connect(lambda: _fx.press_feedback(btn, 90))
            except Exception:
                pass

        self._grid_btn.setIcon(icon("grid", t.text_secondary, t.icon_sm))
        self._list_btn.setIcon(icon("list", t.text_secondary, t.icon_sm))
        for btn, active in ((self._grid_btn, not self._list_mode),
                            (self._list_btn, self._list_mode)):
            fg = t.text_primary if active else t.text_muted
            btn.setIcon(icon("grid" if btn is self._grid_btn else "list", fg, t.icon_sm))
            _wire(btn)
            try:
                btn.setChecked(bool(active))
            except Exception:
                pass
            btn.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid transparent;"
                " min-height: 26px; padding: 0px; border-radius: 6px; outline: 0; }"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )
        self._seg.setStyleSheet(
            f"background-color: {t.input_background};"
            f" border: 1px solid {t.input_border}; border-radius: 8px;"
        )
        _wire(self._sort_btn)
        self._sort_btn.setFont(role_font(t, "meta"))
        self._sort_btn.setStyleSheet(
            "QPushButton { color: palette(text); background: transparent;"
            " border: 1px solid transparent; min-height: 0px;"
            " border-radius: 6px; outline: 0;"
            " padding: 4px 6px; }"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        self._sync_sort_label()
        # Search / New Folder / New circles share one 38px language.
        for _btn, _glyph in ((self._search_btn, "search"),
                             (self._new_folder_btn, "folder-plus"),
                             (self._new_btn, "plus")):
            _wire(_btn)
            _btn.setIcon(icon(_glyph, t.text_primary, t.icon_md))
            _btn.setStyleSheet(
                "QPushButton { background: transparent; min-height: 36px;"
                f" color: {t.text_primary}; border: 1px solid transparent;"
                " border-radius: 19px; padding: 0px; outline: 0; }"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )
        self._search.setStyleSheet(
            f"QLineEdit {{ background-color: {t.input_background};"
            f" color: {t.text_primary}; border: 1px solid {t.input_border};"
            f" border-radius: 8px; padding: 0 12px 0 8px; }}"
            f"QLineEdit:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        # Breadcrumb crumbs: rebuilt per navigation (and per theme
        # switch) with fresh styling via _sync_crumbs.
        self._sync_crumbs()

    # -- circular search (expand/collapse, M4.6.5) -----------------------------

    def _expand_search(self) -> None:
        """Circle -> 260px field (width tween + autofocus)."""
        from app import fx as _fx

        if self._search.isVisible():
            self._search.setFocus()
            return
        self._search_btn.setVisible(False)
        self._search.setVisible(True)
        self._search.setFocus()
        _fx.tween_width(self._search, 260, 200, self)

    def _collapse_search(self) -> None:
        """Field -> circle, only when the query is empty."""
        from app import fx as _fx

        if self._search.text().strip():
            return  # text exists: stay expanded
        if not self._search.isVisible():
            return
        def _done() -> None:
            self._search.setVisible(False)
            self._search_btn.setVisible(True)

        _fx.tween_width(self._search, 0, 180, self, finished=_done)

    def _search_key(self, event) -> bool:
        """Escape in the field: clear first, collapse when already empty."""
        try:
            if self._search.text():
                self._search.clear()
            else:
                self._collapse_search()
            return True
        except Exception:
            return False

    def _sync_sort_label(self) -> None:
        label = _SORT_OPTIONS[self._sort_index][0]
        self._sort_btn.setText(f"Sort: {label}")
        self._sort_btn.setIcon(icon("chevron-down", self._tokens.text_muted, 14))

    def _on_sort_menu(self) -> None:
        from app import fx as _fx

        # M4.6.6: non-blocking popup anchored to the button (Qt-native
        # mapToGlobal, never screen origin). exec() blocked the loop so
        # the entrance animation never played and placement could jump.
        try:
            if self._sort_menu is not None:
                self._sort_menu.close()
        except Exception:
            pass
        menu = InkfallMenu(self, self._tokens)
        self._sort_menu = menu
        actions = []
        for i, (label, _k, _o) in enumerate(_SORT_OPTIONS):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(i == self._sort_index)
            actions.append(action)

        def _chosen(action) -> None:
            try:
                for i, candidate in enumerate(actions):
                    if action == candidate and i != self._sort_index:
                        self._sort_index = i
                        self._sync_sort_label()
                        self.refresh()
                        break
            except Exception:
                pass
            finally:
                try:
                    if self._sort_menu is menu:
                        self._sort_menu = None
                except Exception:
                    pass

        menu.triggered.connect(_chosen)
        anchor = self._sort_btn.mapToGlobal(self._sort_btn.rect().bottomLeft())
        menu.popup(anchor)
        # Animate AFTER popup: the menu is visible, so the fade + drop
        # plays top-to-bottom from the button anchor (M4.6.10).
        _fx.animate_menu_open(menu, 140, -6, self)

    # -- folder navigation (file-explorer model) ------------------------------

    @property
    def current_folder_id(self) -> str | None:
        """Folder being browsed (None = the Home root)."""
        return self._current_folder_id

    def open_folder(self, folder_id: str | None) -> None:
        """Navigate into a folder (or Home root) and list its children.

        Pure organization navigation: nothing on the filesystem is
        touched, scroll position is remembered per folder for the
        session, and the breadcrumb rebuilds.
        """
        if folder_id is not None and self._service.get_folder(folder_id) is None:
            return
        if self._current_folder_id == folder_id:
            return
        try:
            self._scroll_memory[self._current_folder_id] = (
                self._view.verticalScrollBar().value())
        except Exception:
            pass
        self._current_folder_id = folder_id
        self._navigation_changed = True
        self.refresh()
        # Discrete navigation (not high-frequency typing): run one
        # immediate pass so cache hits repopulate synchronously; the
        # debounced timer remains as backup for misses.
        try:
            self._thumb_pass()
        except Exception:
            pass

    def go_home(self) -> None:
        """Navigate back to the Home root."""
        self.open_folder(None)

    def _folder_trail(self) -> list[tuple[str, str]]:
        """Ancestor (id, name) pairs from the root to the current folder.

        Walks the parent chain with a cycle guard, so a corrupted
        parent row can never loop the walk.
        """
        seen: set[str] = set()
        fid = self._current_folder_id
        trail: list[tuple[str, str]] = []
        while fid is not None and fid not in seen:
            seen.add(fid)
            folder = self._service.get_folder(fid)
            if folder is None:
                break
            trail.append((folder.id, folder.name))
            fid = folder.parent_id
        trail.reverse()
        return trail

    def _trail_labels(self, folder_id: str | None) -> str:
        """'A / B / C' label for a folder id ('Home Library' for root)."""
        if folder_id is None:
            return "Home Library"
        seen: set[str] = set()
        fid: str | None = folder_id
        names: list[str] = []
        while fid is not None and fid not in seen:
            seen.add(fid)
            folder = self._service.get_folder(fid)
            if folder is None:
                break
            names.append(folder.name)
            fid = folder.parent_id
        names.reverse()
        return " / ".join(names) or "Home Library"

    def _style_crumb(self, btn, current: bool) -> None:
        """Flat crumb styling: current crumb is plain text, the rest
        are quiet buttons that navigate."""
        t = self._tokens
        btn.setFont(font_for(btn.text(), "meta", t))
        btn.setStyleSheet(
            "QPushButton { background: transparent; min-height: 0px;"
            f" color: {t.text_secondary if not current else t.text_muted};"
            " border: 1px solid transparent;"
            " border-radius: 6px; padding: 2px 6px; outline: 0; }"
            f"QPushButton:hover {{ color: {t.text_primary}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )

    def _sync_crumbs(self) -> None:
        """Rebuild the breadcrumb row for the current location."""
        trail = self._folder_trail()
        active = bool(trail)
        self._crumbs_bar.setVisible(active)
        if not active:
            return
        layout = self._crumbs_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        t = self._tokens
        home = ModalButton("Home Library", radius=6)
        home.setCursor(Qt.CursorShape.PointingHandCursor)
        home.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        home.setAccessibleName("Home Library")
        home.setAccessibleDescription("Go back to the library root")
        home.clicked.connect(self.go_home)
        self._style_crumb(home, current=False)
        layout.addWidget(home)
        for fid, name in trail:
            chev = QLabel()
            chev.setPixmap(icon("chevron-right", t.text_muted, 12).pixmap(12, 12))
            layout.addWidget(chev)
            if fid == self._current_folder_id:
                current = QLabel(name)
                current.setFont(font_for(name, "meta", t))
                current.setStyleSheet(f"color: {t.text_muted}; padding: 2px 6px;")
                layout.addWidget(current)
            else:
                crumb = ModalButton(name, radius=6)
                crumb.setCursor(Qt.CursorShape.PointingHandCursor)
                crumb.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                crumb.setAccessibleName(name)
                crumb.setAccessibleDescription("Open this folder")
                crumb.clicked.connect(
                    lambda _c=False, target=fid: self.open_folder(target))
                self._style_crumb(crumb, current=False)
                layout.addWidget(crumb)

    def _update_context(self, query: str, doc_count: int) -> None:
        """Heading + meta line for the current location."""
        folder = (self._service.get_folder(self._current_folder_id)
                  if self._current_folder_id is not None else None)
        if query:
            heading = "Home Library"
            text = f"{doc_count} result{'s' if doc_count != 1 else ''} for “{query}”"
        elif folder is not None:
            heading = folder.name
            sub_folders, sub_items = self._service.child_counts(folder.id)
            total = sub_folders + sub_items
            text = f"{total} item{'s' if total != 1 else ''}"
        else:
            heading = "Home Library"
            total = self._service.item_count
            text = f"{total} document{'s' if total != 1 else ''} in library"
        self._heading.setText(heading)
        self._heading.setFont(font_for(heading, "title", self._tokens))
        self._context.setText(text)
        self._context.setFont(font_for(text, "meta", self._tokens))

    # -- data ------------------------------------------------------------------

    def set_list_mode(self, enabled: bool) -> None:
        """Toggle grid/list with a container-level transition (M4.6.5).

        Same view, same model, same thumbnails: only the layout mode
        flips. Scroll fraction is preserved across the row-height
        change; search/sort/filter/selection are untouched (no refresh,
        no re-query, no re-render). A short fade + directional shift
        reads as a deliberate transformation, never per-card motion.
        """
        from app import fx as _fx

        if self._list_mode == bool(enabled):
            return
        bar = self._view.verticalScrollBar()
        try:
            fraction = (bar.value() / bar.maximum()) if bar.maximum() > 0 else 0.0
        except Exception:
            fraction = 0.0
        self._list_mode = bool(enabled)
        self._grid_btn.setChecked(not enabled)
        self._list_btn.setChecked(enabled)
        self._view.setViewMode(
            QListView.ViewMode.ListMode if enabled else QListView.ViewMode.IconMode
        )
        # setViewMode() re-derives drag/drop enablement from the movement
        # setting (Static => disabled); restore the drag configuration or
        # book→folder drags silently stop starting after a layout flip.
        self._view._apply_drag_config()
        try:
            if bar.maximum() > 0:
                bar.setValue(int(round(fraction * bar.maximum())))
        except Exception:
            pass
        self._refresh_chrome_tokens(self._tokens)
        # Directional settle: list reads as a downward settle, grid rises.
        _fx.slide_fade_in(self._view.viewport(), 0, 180, self,
                           dy_px=6 if enabled else -6)
        _fx.fade_in(self._seg, 140, self)
        self._schedule_thumb_pass()

    def folder_count(self, folder_id: str) -> int:
        """Direct-children count for folder cards (delegate hook)."""
        try:
            sub_folders, sub_items = self._service.child_counts(folder_id)
            return sub_folders + sub_items
        except Exception:
            return 0

    def is_section_collapsed(self, key: str) -> bool:
        """Collapse state for a section key (session-only)."""
        return bool(self._collapsed.get(key, False))

    def toggle_section(self, key: str) -> None:
        """Collapse/expand a section (visibility rows only, session-only)."""
        if key not in _SECTION_KEYS:
            return
        collapsed = not self._collapsed.get(key, False)
        self._collapsed[key] = collapsed
        self._rebuild_rows_light()
        self._play_section_toggle(key, collapsed)

    def _section_chevron(self, key: str) -> tuple[bool, float | None]:
        """(collapsed, anim_progress) for header chevron painting.

        Progress is None when no toggle animation runs for the key;
        otherwise 0.0->1.0 from the pre-toggle state.
        """
        collapsed = bool(self._collapsed.get(key, False))
        if self._section_anim_key != key:
            return collapsed, None
        try:
            return collapsed, max(0.0, min(1.0, float(self._section_progress)))
        except Exception:
            return collapsed, None

    def _play_section_toggle(self, key: str, _collapsed: bool) -> None:
        """Chevron rotation + container fade (single animator, M4.6.7)."""
        from app import fx as _fx

        try:
            self._section_anim.stop()
        except Exception:
            pass
        self._section_anim_key = key
        self._section_progress = 0.0
        self._section_anim.start()
        try:
            _fx.fade_in(self._view.viewport(), 160, self)
        except Exception:
            pass

    def _on_section_tick(self, value) -> None:
        try:
            self._section_progress = max(0.0, min(1.0, float(value)))
        except Exception:
            self._section_progress = 1.0
        try:
            # Repaint the toggling header only (chevron rotation).
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and row.is_header and row.header is not None
                        and row.header.key == self._section_anim_key):
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(r, 0)))
                    break
        except Exception:
            pass

    def _on_section_done(self) -> None:
        self._section_anim_key = None
        self._section_progress = 1.0
        try:
            self._view.viewport().update()
        except Exception:
            pass

    def _hover_header_key_at(self, pos) -> str | None:
        """Section key under a viewport pos (headers only, M4.6.9)."""
        try:
            idx = self._view.indexAt(pos)
            if not idx.isValid():
                return None
            row = self._model.row_at(idx.row())
            if row is None or not row.is_header or row.header is None:
                return None
            return row.header.key or None
        except Exception:
            return None

    def _repaint_header_key(self, key: str | None) -> None:
        """One synchronous repaint of a header row (clears stale wash)."""
        try:
            if key is None:
                return
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and row.is_header and row.header is not None
                        and row.header.key == key):
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(r, 0)))
                    break
        except Exception:
            pass

    def _track_header_hover(self, pos) -> None:
        """Retarget the header-hover animator (hover-in). Never consumes.

        M4.6.10: the key persists through leave-glides so the fade-out
        paints; it clears only at progress 0 (see _on_hdr_done).
        """
        try:
            key = self._hover_header_key_at(pos)
            if key == self._hdr_anim_key:
                if key is not None:
                    self._hdr_glide_to(1.0)
                return
            if key is None:
                self._hdr_glide_to(0.0)
                return
            old = self._hdr_anim_key
            self._hdr_anim_key = key
            if old is not None and old != key:
                self._repaint_header_key(old)
            self._hdr_glide_to(1.0)
        except Exception:
            pass

    def _clear_header_hover(self) -> None:
        """Glide header hover back out (hover-leave)."""
        try:
            if self._hdr_anim_key is None and self._hdr_progress <= 0.0:
                return
            self._hdr_glide_to(0.0)
        except Exception:
            pass

    def _hdr_glide_to(self, target: float) -> None:
        try:
            self._hdr_anim.stop()
            self._hdr_anim.setStartValue(float(self._hdr_progress))
            self._hdr_anim.setEndValue(float(target))
            self._hdr_anim.start()
        except Exception:
            pass

    def _hdr_update_key(self) -> None:
        """Repaint the hovered header row only (or everything)."""
        try:
            if self._hdr_anim_key is None:
                self._view.viewport().update()
                return
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and row.is_header and row.header is not None
                        and row.header.key == self._hdr_anim_key):
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(r, 0)))
                    break
            else:
                self._view.viewport().update()
        except Exception:
            pass

    def _on_hdr_tick(self, value) -> None:
        try:
            self._hdr_progress = max(0.0, min(1.0, float(value)))
        except Exception:
            self._hdr_progress = 1.0
        self._hdr_update_key()

    def _on_hdr_done(self) -> None:
        try:
            if self._hdr_progress <= 0.0:
                self._hdr_anim_key = None
        except Exception:
            pass
        self._hdr_update_key()

    def _build_section_rows(self) -> tuple[list[LibraryRow], int]:
        """Assemble rows for the current location (no side effects).

        Home root (no search): Recent Opened, then Folders (root
        folders only — the entry point into the hierarchy), then
        Ungrouped Books (root-level PDFs, collapsed at launch). Inside
        a folder: immediate children only, subfolders first, then
        books, no section headers. Searching: global matches grouped
        into Folders + Books with location context (non-collapsible).
        Returns (rows, doc_count). Collapsing omits content rows only;
        headers always render.
        """
        query_text = self._search.text().strip()
        _label, key, order = _SORT_OPTIONS[self._sort_index]
        current = self._current_folder_id
        if current is not None and self._service.get_folder(current) is None:
            current = None
            self._current_folder_id = None
        rows: list[LibraryRow] = []
        doc_count = 0
        if query_text:
            # Global search: folders match by name, books by metadata.
            needle = query_text.casefold()
            folders = [f for f in self._service.folders()
                       if needle in f.name.casefold()]
            items = self._service.query(SearchQuery(text=query_text), key, order)
            doc_count = len(items)
            if folders:
                rows.append(_header("Library", "Folders", len(folders)))
                rows += [LibraryRow(is_folder=True, folder=f) for f in folders]
            if items:
                rows.append(_header("Library", "Books", len(items)))
                for i in items:
                    rows.append(LibraryRow(
                        is_folder=False, item=i, section="search",
                        meta_override=self._trail_labels(i.parent_id)))
            return rows, doc_count
        if current is None:
            if self._service.item_count == 0 and self._service.folder_count == 0:
                # No rows at all: the full empty state (Import PDFs +
                # New Folder actions) speaks for the whole page.
                return rows, 0
            # -- Recent Opened: reliable open history, expanded ---------
            recent = self._service.query(
                SearchQuery(recent_only=True), SortKey.LAST_OPENED,
                SortOrder.DESCENDING)[:_RECENT_CAP]
            rows.append(_header("Recent", "Recent Opened", len(recent), "recent"))
            if not self._collapsed.get("recent"):
                if recent:
                    rows += [LibraryRow(
                        is_folder=False, item=i, section="recent",
                        meta_override=_relative_time(i.last_opened_ts))
                        for i in recent]
                elif (self._service.item_count > 0
                        or self._service.folder_count > 0):
                    # Restrained explanation for a shelf with no history
                    # yet — suppressed on a truly empty library, where
                    # the full empty state speaks instead.
                    rows.append(LibraryRow(
                        is_folder=False, is_hint=True,
                        hint="Books you open will appear here."))
            # -- Folders: the entry point into the hierarchy -------------
            root_folders = self._service.child_folders(None)
            root_items = [i for i in self._service.query(SearchQuery(), key, order)
                          if i.parent_id is None]
            rows.append(_header("Organize", "Folders", len(root_folders), "folders"))
            if not self._collapsed.get("folders"):
                if root_folders:
                    rows += [LibraryRow(is_folder=True, folder=f)
                             for f in root_folders]
                elif (self._service.item_count > 0
                        or self._service.folder_count > 0):
                    # Folder-creation affordance that never mislabels
                    # books as folders (suppressed on an empty library).
                    rows.append(LibraryRow(
                        is_folder=False, is_hint=True,
                        hint="Create folders to organize your books."))
            # -- Ungrouped Books: root PDFs, COLLAPSED at launch ---------
            rows.append(_header("Library", "Ungrouped Books",
                                len(root_items), "ungrouped"))
            if not self._collapsed.get("ungrouped"):
                if root_items:
                    rows += [LibraryRow(is_folder=False, item=i)
                             for i in root_items]
                else:
                    # Understated empty state for an expanded section
                    # with no root-level books (never shown collapsed).
                    rows.append(LibraryRow(
                        is_folder=False, is_hint=True,
                        hint="No ungrouped books yet."))
            doc_count = len(root_items)
            for row in rows:
                if (row.is_header and row.header is not None
                        and row.header.key):
                    row.header.collapsed = bool(
                        self._collapsed.get(row.header.key, False))
            return rows, doc_count
        # -- Inside a folder: immediate children, folders first ----------
        sub_folders = self._service.child_folders(current)
        sub_items = self._service.query(SearchQuery(folder_id=current), key, order)
        doc_count = len(sub_items)
        rows += [LibraryRow(is_folder=True, folder=f) for f in sub_folders]
        rows += [LibraryRow(is_folder=False, item=i) for i in sub_items]
        return rows, doc_count

    def _rebuild_rows_light(self) -> None:
        """Rebuild visible rows WITHOUT touching thumbnail/document state.

        Used by section toggles: no thumb bookkeeping reset, no
        failure-state clear, cache-backed re-request only (+0 renders),
        scrollbar fraction preserved.
        """
        bar = self._view.verticalScrollBar()
        try:
            maximum = bar.maximum()
            fraction = bar.value() / maximum if maximum > 0 else 0.0
        except Exception:
            fraction = 0.0
        rows, doc_count = self._build_section_rows()
        self._model.set_rows(rows)
        self._delegate.clear_badges()
        self._sync_crumbs()
        self._update_context(self._search.text().strip(), doc_count)
        self._update_empty(len(rows))
        self._schedule_thumb_pass()
        try:
            maximum = bar.maximum()
            bar.setValue(int(round(fraction * maximum)))
        except Exception:
            pass

    def refresh(self) -> None:
        """Re-run the service queries for the current location.

        Home root: Recent Opened + Folders + Ungrouped Books. Inside a
        folder: its immediate children. Searching: global results.
        Scroll position is remembered per location (session) and
        restored after the rebuild, so returning to a folder restores
        context.
        """
        from app import fx as _fx

        query_text = self._search.text().strip()
        bar = self._view.verticalScrollBar()
        try:
            self._scroll_memory[self._current_folder_id] = int(bar.value())
        except Exception:
            pass
        rows, doc_count = self._build_section_rows()
        self._model.set_rows(rows)
        # New query generation: retryable failures may request again.
        # Hover state resets too (rows rebound; the lift re-tracks).
        try:
            self._hover_anim.stop()
        except Exception:
            pass
        self._hover_id = None
        self._hover_prev_id = None
        self._hover_key = None
        self._hover_prev_key = None
        self._hover_progress = 0.0
        try:
            self._press_anim.stop()
        except Exception:
            pass
        self._press_key = None
        self._press_progress = 0.0
        # M4.6.10: category hover resets too (tiles rebound; re-tracks).
        try:
            self._cat_hover_anim.stop()
        except Exception:
            pass
        self._cat_hover_key = None
        self._cat_hover_p = 0.0
        # M4.6.9: header hover resets too (rows rebound; re-tracks).
        try:
            self._hdr_anim.stop()
        except Exception:
            pass
        self._hdr_anim_key = None
        self._hdr_progress = 0.0
        self._thumb_failed.clear()
        self._auto_retried.clear()
        self._delegate.clear_badges()
        self._sync_crumbs()
        self._update_context(query_text, doc_count)
        self._update_empty(len(rows))
        # Restore this location's remembered scroll (data refreshes keep
        # the position; navigation restores the target folder's memory).
        try:
            remembered = int(self._scroll_memory.get(self._current_folder_id, 0))
            bar.setValue(max(0, remembered))
        except Exception:
            pass
        self._schedule_thumb_pass()
        # Container-level transition (never gates PDF/thumbs): folder
        # navigation gets a subtle slide+fade, plain refreshes a short
        # fade.
        try:
            if self._navigation_changed:
                _fx.slide_fade_in(self._stack, 0, self._tokens.dur_med_ms, self)
            else:
                _fx.fade_in(self._view.viewport(), self._tokens.dur_fast_ms, self)
        except Exception:
            pass
        finally:
            self._navigation_changed = False

    def _update_empty(self, row_count: int) -> None:
        if row_count > 0:
            self._stack.setCurrentWidget(self._view)
            return
        self._stack.setCurrentWidget(self._empty)
        query = self._search.text().strip()
        if query:
            self._empty.show_state(
                "No results",
                f"Nothing matches “{query}”. Try a different title, file, or folder name.",
                "Clear search",
                "search",
            )
        elif self._current_folder_id is not None:
            self._empty.show_state(
                "This folder is empty",
                "Move documents here, import PDFs while browsing, or create a"
                " subfolder. Files on disk are never affected.",
                "New subfolder",
                "folder",
            )
        elif self._service.item_count == 0 and self._service.folder_count == 0:
            self._empty.show_state(
                "Your library is empty",
                "Import a PDF or create a folder to get started. Files stay where"
                " they are — the library only remembers them.",
                "Import PDFs",
                "empty",
                action2="New Folder",
            )
        else:
            self._empty.show_state(
                "Nothing here yet",
                "Open a book and it will appear in Recent Opened.",
                "Import PDFs",
                "empty",
            )

    def _empty_action(self) -> None:
        if self._search.text().strip():
            self._search.clear()
        elif self._current_folder_id is not None:
            self.create_folder_dialog()
        else:
            self.import_dialog()

    def _empty_action2(self) -> None:
        if self._search.text().strip():
            self._search.clear()
        else:
            self.create_folder_dialog()

    # -- thumbnails ------------------------------------------------------------

    def _schedule_thumb_pass(self) -> None:
        if not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def _visible_doc_ids(self) -> set[str]:
        visible: set[str] = set()
        model = self._model
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            if self._view.visualRect(idx).intersects(self._view.viewport().rect()):
                row_obj = model.row_at(row)
                if row_obj is not None and not row_obj.is_folder and row_obj.item is not None:
                    visible.add(row_obj.item.id)
        return visible

    def _thumb_pass(self) -> None:
        if self._stack.currentWidget() is not self._view:
            return
        visible = self._visible_doc_ids()
        try:
            self._thumbs.cancel_outside(visible)
        except Exception:
            pass
        model = self._model
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            if not self._view.visualRect(idx).intersects(self._view.viewport().rect()):
                continue
            self._request_row_thumb(model.row_at(row))
        # Re-arm while visible items remain unresolved (never stuck).
        if self._unresolved_visible():
            self._schedule_thumb_pass()

    def _request_row_thumb(self, row_obj) -> None:
        """Request one row's thumbnail, honoring synchronous cache hits.

        ``ThumbnailService.request()`` returns a cached image directly
        (memory/disk hit, zero PDF work) WITHOUT emitting a completion
        signal. The return value must populate the current model here —
        otherwise a rebound model keeps a permanent placeholder for an
        already-READY thumbnail.
        """
        if row_obj is None or row_obj.is_folder or row_obj.item is None:
            return
        item = row_obj.item
        if item.missing:
            return
        if item.id in self._thumb_failed and item.id in self._auto_retried:
            return  # bounded: one auto-retry per refresh generation
        if item.id in self._thumb_failed:
            self._auto_retried.add(item.id)
        try:
            image = self._thumbs.request(item.id, item.normalized_path, item.modified_ts,
                                         item.size_bytes, item.missing)
        except Exception:
            return
        if image is not None:
            try:
                if self._model.set_thumbnail(item.id, image):
                    self._view.viewport().update()
            except Exception:
                pass

    def _unresolved_visible(self) -> bool:
        """True when a visible, non-missing doc still lacks its thumbnail."""
        from app.library.qt_model import Role

        model = self._model
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            if not self._view.visualRect(idx).intersects(self._view.viewport().rect()):
                continue
            row_obj = model.row_at(row)
            if row_obj is None or row_obj.is_folder or row_obj.item is None:
                continue
            item = row_obj.item
            if item.missing or item.id in self._thumb_failed:
                continue
            if model.data(idx, Role.THUMB) is None:
                return True
        return False

    def _on_thumb_ready(self, item_id: str, image) -> None:
        try:
            self._thumb_failed.discard(item_id)
            self._auto_retried.discard(item_id)
            if self._model.set_thumbnail(item_id, image):
                self._view.viewport().update()
        except Exception:
            pass

    def _on_thumb_failed(self, item_id: str) -> None:
        self._thumb_failed.add(item_id)
        try:
            if self._model.mark_failed(item_id):
                self._view.viewport().update()
        except Exception:
            pass

    def _on_thumb_stale(self, item_id: str) -> None:
        """Drifted result discarded: refresh metadata, then re-request."""
        try:
            for row in range(self._model.rowCount()):
                row_obj = self._model.row_at(row)
                if (row_obj is not None and not row_obj.is_folder
                        and row_obj.item is not None and row_obj.item.id == item_id):
                    try:
                        self._service.refresh_filesystem_facts(row_obj.item.id)
                    except Exception:
                        pass
                    break
            self._schedule_thumb_pass()
        except Exception:
            pass

    def _request_open(self, item_id: str) -> None:
        """Emit openRequested unless this is a same-document duplicate.

        Single click, double click (clicked+activated both fire), and
        Enter all funnel here. Only the same document ID inside the
        guard window is suppressed; opening A then B always emits both.
        """
        try:
            now = time.monotonic()
            if item_id == self._last_open_id and (now - self._last_open_at) < 0.4:
                return
            self._last_open_id = item_id
            self._last_open_at = now
        except Exception:
            pass
        self.openRequested.emit(item_id)

    def _play_cat_press(self, folder_id: str) -> None:
        """Compact + release the pressed category tile (130ms)."""
        try:
            self._cat_anim.stop()
        except Exception:
            pass
        self._cat_anim_id = folder_id
        self._cat_progress = 0.0
        try:
            self._cat_anim.start()
        except Exception:
            pass

    def _cat_compact(self, folder_id: str) -> float:
        """Current compact factor for a tile (1.0 = settled)."""
        try:
            if self._cat_anim_id != folder_id:
                return 1.0
            import math

            p = max(0.0, min(1.0, float(self._cat_progress)))
            return 1.0 - 0.02 * math.sin(math.pi * p)
        except Exception:
            return 1.0

    def _on_cat_tick(self, value) -> None:
        try:
            self._cat_progress = max(0.0, min(1.0, float(value)))
        except Exception:
            self._cat_progress = 1.0
        try:
            self._view.viewport().update()
        except Exception:
            pass

    def _on_cat_done(self) -> None:
        self._cat_anim_id = None
        self._cat_progress = 1.0
        try:
            self._view.viewport().update()
        except Exception:
            pass

    def _hover_folder_key_at(self, pos) -> str | None:
        """Category id under a viewport pos (folders only, M4.6.10)."""
        try:
            idx = self._view.indexAt(pos)
            if not idx.isValid():
                return None
            row = self._model.row_at(idx.row())
            if row is None or not row.is_folder or row.folder is None:
                return None
            return row.folder.id
        except Exception:
            return None

    def _repaint_folder_key(self, key: str | None) -> None:
        """One synchronous repaint of a folder row (clears stale wash)."""
        try:
            if key is None:
                return
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and row.is_folder and row.folder is not None
                        and row.folder.id == key):
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(r, 0)))
                    break
        except Exception:
            pass

    def _track_folder_hover(self, pos) -> None:
        """Retarget the shared category-hover animator (M4.6.10).

        The key persists through leave-glides so the fade-out paints;
        it clears only when progress reaches 0 (see _on_cat_hover_done).
        """
        try:
            key = self._hover_folder_key_at(pos)
            if key == self._cat_hover_key:
                if key is not None:
                    self._cat_hover_glide_to(1.0)
                return
            if key is None:
                self._cat_hover_glide_to(0.0)
                return
            old = self._cat_hover_key
            self._cat_hover_key = key
            if old is not None and old != key:
                self._repaint_folder_key(old)
            self._cat_hover_glide_to(1.0)
        except Exception:
            pass

    def _clear_folder_hover(self) -> None:
        """Glide category hover back out (hover-leave)."""
        try:
            if self._cat_hover_key is None and self._cat_hover_p <= 0.0:
                return
            self._cat_hover_glide_to(0.0)
        except Exception:
            pass

    def _cat_hover_glide_to(self, target: float) -> None:
        try:
            self._cat_hover_anim.stop()
            self._cat_hover_anim.setStartValue(float(self._cat_hover_p))
            self._cat_hover_anim.setEndValue(float(target))
            self._cat_hover_anim.start()
        except Exception:
            pass

    def _cat_hover_update_key(self) -> None:
        """Repaint the hovered folder row only (or everything)."""
        try:
            if self._cat_hover_key is None:
                self._view.viewport().update()
                return
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and row.is_folder and row.folder is not None
                        and row.folder.id == self._cat_hover_key):
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(r, 0)))
                    break
            else:
                self._view.viewport().update()
        except Exception:
            pass

    def _on_cat_hover_tick(self, value) -> None:
        try:
            self._cat_hover_p = max(0.0, min(1.0, float(value)))
        except Exception:
            self._cat_hover_p = 1.0
        self._cat_hover_update_key()

    def _on_cat_hover_done(self) -> None:
        try:
            if self._cat_hover_p <= 0.0:
                self._cat_hover_key = None
        except Exception:
            pass
        self._cat_hover_update_key()

    # -- book→folder drag & drop (view-owned lifecycle) ------------------------

    def _set_drop_target(self, folder_id: str | None) -> None:
        """Highlight one folder card as the drop target (drag hover)."""
        if self._drop_folder_id == folder_id:
            return
        previous = self._drop_folder_id
        self._drop_folder_id = folder_id
        # Repaint only the affected folder rows (highlight in, old out).
        for key in (previous, folder_id):
            self._repaint_folder_key(key)

    def _mime_item_id(self, mime) -> str | None:
        """First stable item id from the internal drag payload."""
        try:
            data = bytes(mime.data(MIME_LIBRARY_ITEM)).decode("utf-8")
        except Exception:
            return None
        first = data.split("\n", 1)[0].strip()
        return first or None

    def _on_view_clicked(self, index: QModelIndex) -> None:
        """Click behavior (single-click open / folder navigation).

        Folder tile -> navigate into it. Failed document badge ->
        manual retry. Any other document click -> open immediately.
        Empty area and right-click selection never reach here with a
        document row.
        """
        try:
            row = self._model.row_at(index.row())
            if row is None or row.is_hint:
                return
            if row.is_header:
                # Header click toggles its section (no navigation).
                try:
                    if row.header is not None and row.header.key:
                        self.toggle_section(row.header.key)
                except Exception:
                    pass
                return
            if row.is_folder and row.folder is not None:
                # File-explorer navigation: opening a folder enters it.
                self._play_cat_press(row.folder.id)
                self.open_folder(row.folder.id)
                return
            if row.is_folder or row.item is None:
                return
            from PySide6.QtGui import QCursor
            item = row.item
            # M4.6.11: occurrence identity — press/open resolve the
            # clicked row, never every copy of the same document.
            clicked_row = int(index.row())
            try:
                _vp = self._view.viewport()
                _pos = _vp.mapFromGlobal(QCursor.pos())
            except Exception:
                _pos = None
            if item.id not in self._thumb_failed:
                if item.missing:
                    self.statusMessage.emit("Missing file", item.display_title)
                    return
                self._play_press(clicked_row, item.id)
                self._request_open(item.id)
                return
            if _pos is not None and self._delegate.retry_hit(
                    item.id, _pos, row=clicked_row):
                self._thumb_failed.discard(item.id)
                self._auto_retried.discard(item.id)
                try:
                    self._thumbs.retry(item.id, item.normalized_path, item.modified_ts,
                                       item.size_bytes, item.missing)
                except Exception:
                    pass
        except Exception:
            pass

    # -- selection helpers -----------------------------------------------------

    def _selected_row(self) -> LibraryRow | None:
        idx = self._view.currentIndex()
        if not idx.isValid():
            return None
        return self._model.row_at(idx.row())

    # -- interactions ----------------------------------------------------------

    def _on_activated(self, index: QModelIndex) -> None:
        row = self._model.row_at(index.row())
        if row is None or row.is_hint:
            return
        if row.is_header:
            # Enter/Space on a focused header toggles collapse.
            try:
                if row.header is not None and row.header.key:
                    self.toggle_section(row.header.key)
            except Exception:
                pass
            return
        if row.is_folder and row.folder is not None:
            # Enter/double-click on a folder navigates into it, same as
            # a single click (file-explorer funnel).
            self._play_cat_press(row.folder.id)
            self.open_folder(row.folder.id)
        elif not row.is_folder and row.item is not None:
            if row.item.missing:
                self.statusMessage.emit("Missing file", row.item.display_title)
                return
            # Enter key or double-click: same funnel as single click so
            # the same-document guard suppresses the duplicate.
            self._request_open(row.item.id)

    def import_dialog(self) -> None:
        """Import PDFs via file dialog (multi-select)."""
        paths, _ = QFileDialog.getOpenFileNames(self, "Import PDFs", "", "PDF files (*.pdf)")
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths: list[str]) -> int:
        """Import paths into the current folder. Returns import count."""
        folder_id = self._current_folder_id
        count, errors = 0, 0
        for path in paths:
            if not path.lower().endswith(".pdf"):
                continue
            try:
                self._service.import_path(path, folder_id)
                count += 1
            except Exception:
                errors += 1
        self.refresh()
        if count:
            self.statusMessage.emit(f"Imported {count} PDF{'s' if count != 1 else ''}", "")
        if errors:
            self.statusMessage.emit(
                "Some files could not be imported",
                f"{errors} file{'s' if errors != 1 else ''} skipped",
            )
        return count

    def create_folder_dialog(self, parent_id: str | None = None) -> None:
        """Create a folder in the given (or current) location via prompt."""
        if parent_id is None:
            parent_id = self._current_folder_id
        self._ask_text(
            "New Folder",
            "Folders organize books inside Inkfall. Files on disk are"
            " never moved or renamed.",
            "",
            "Folder name",
            "Create",
            lambda name: self._create_folder(parent_id, name),
            required=True,
        )

    def _create_folder(self, parent_id: str | None, name: str) -> None:
        try:
            folder = self._service.create_folder(
                name or "Untitled folder",
                parent_id,
            )
            self.refresh()
            where = "" if parent_id is None else "here"
            self.statusMessage.emit(
                f"Created folder “{folder.name}”" + (f" {where}" if where else ""), "")
        except Exception as exc:
            self.statusMessage.emit("Cannot create folder", str(exc))

    # -- prompt/confirm plumbing --------------------------------------------

    def _ask_text(self, title: str, message: str, value: str, placeholder: str,
                  confirm_label: str, callback, launcher=None,
                  required: bool = False) -> None:
        """Open the shared prompt; ``callback(text)`` runs on confirm.

        ``required`` keeps the dialog open with an inline error when
        the confirm lands on an empty name (folder creation); renames
        leave it False so the service fallback applies.
        """
        self._prompt_callback = callback
        self._prompt_modal.open_prompt(
            title, message, value, placeholder, confirm_label, launcher,
            required=required)

    def _ask_confirm(self, title: str, body: str, confirm_label: str,
                     danger: bool, callback, launcher=None) -> None:
        """Open the shared confirm; ``callback()`` runs on confirm."""
        self._confirm_callback = callback
        self._confirm_modal.open_confirm(
            title, body, confirm_label, danger, launcher)

    def _on_prompt_confirmed(self, text: str) -> None:
        callback = self._prompt_callback
        self._prompt_callback = None
        if callback is not None:
            callback(text)

    def _on_confirm_confirmed(self) -> None:
        callback = self._confirm_callback
        self._confirm_callback = None
        if callback is not None:
            callback()

    def _remove_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.is_folder or row.item is None:
            return
        self._confirm_remove(row.item.id)

    def hideEvent(self, event) -> None:  # noqa: N802
        """Close organization dialogs when the page hides.

        The workspace switch (stack index change) hides this page; an
        open prompt/confirm must not survive into the PDF workspace or
        silently reappear on the way back.
        """
        super().hideEvent(event)
        try:
            if self._prompt_modal.isVisible():
                self._prompt_modal.close_modal(animate=False)
        except Exception:
            pass
        try:
            if self._confirm_modal.isVisible():
                self._confirm_modal.close_modal(animate=False)
        except Exception:
            pass
        self._prompt_callback = None
        self._confirm_callback = None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Grid viewport filter: smooth wheel + hover-lift tracking."""
        try:
            if obj is self._search:
                if event.type() == QEvent.Type.FocusOut:
                    # Collapse only when the query is empty.
                    if not self._search.text().strip():
                        self._collapse_search()
                elif (event.type() == QEvent.Type.KeyPress
                        and event.key() == Qt.Key.Key_Escape):
                    return self._search_key(event)
                return super().eventFilter(obj, event)
            if obj is self._view.viewport():
                if event.type() == QEvent.Type.Wheel:
                    from app import fx as _fx

                    bar = self._view.verticalScrollBar()
                    pixel = event.pixelDelta()
                    delta = pixel if not pixel.isNull() else event.angleDelta()
                    _fx.smooth_scroll_to(bar, bar.value() - int(delta.y()),
                                         140, self)
                    event.accept()
                    return True
                if event.type() == QEvent.Type.HoverMove:
                    pos = event.position().toPoint()
                    self._track_hover(pos)
                    self._track_header_hover(pos)
                    self._track_folder_hover(pos)
                elif event.type() == QEvent.Type.HoverLeave:
                    self._clear_hover()
                    self._clear_header_hover()
                    self._clear_folder_hover()
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # -- hover/lift (single view-level animator) -------------------------------

    def _hover_item_id_at(self, pos) -> str | None:
        """Document id under a viewport pos (folders/headers never lift)."""
        key = self._hover_key_at(pos)
        return key[1] if key is not None else None

    def _hover_key_at(self, pos) -> tuple[int, str] | None:
        """(row, item_id) occurrence under a viewport pos (M4.6.11)."""
        try:
            idx = self._view.indexAt(pos)
            if not idx.isValid():
                return None
            row = self._model.row_at(idx.row())
            if row is None or row.is_folder or row.item is None:
                return None
            return (int(idx.row()), row.item.id)
        except Exception:
            return None

    def _track_hover(self, pos) -> None:
        """Retarget the lift animator (hover-in). Never consumes."""
        try:
            key = self._hover_key_at(pos)
            if key == self._hover_key:
                return
            # The outgoing occurrence keeps repainting until the
            # retarget covers it (no stale lift/shadow/pin).
            self._hover_prev_key = self._hover_key
            self._hover_prev_id = self._hover_id
            self._hover_key = key
            self._hover_id = key[1] if key is not None else None
            if key is None:
                self._clear_hover()
                return
            self._hover_glide_to(1.0)
        except Exception:
            pass

    def _clear_hover(self) -> None:
        """Glide the lift back out (hover-leave)."""
        try:
            if (self._hover_key is None and self._hover_id is None
                    and self._hover_progress <= 0.0):
                return
            self._hover_glide_to(0.0)
        except Exception:
            pass

    def _hover_glide_to(self, target: float) -> None:
        """Retarget the hover animator from the CURRENT progress.

        M4.6.12 one continuous response: enter uses OutQuart (quick
        but smooth attack — lift/wash/shadow build together), leave
        uses OutCubic (slightly faster return, no snap). Retargets
        (A->B->C->A) never restart from zero; repaint covers only the
        previous + current occurrence rects (see _hover_repaint).
        """
        try:
            from PySide6.QtCore import QEasingCurve

            self._hover_anim.stop()
            if float(target) >= 1.0:
                self._hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutQuart))
            else:
                self._hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            self._hover_anim.setStartValue(float(self._hover_progress))
            self._hover_anim.setEndValue(float(target))
            self._hover_anim.start()
        except Exception:
            pass

    def _hover_row_rect(self, item_id: str | tuple | None):
        """Visual rect for an occurrence (None when not found).

        Accepts ``(row, id)`` (exact occurrence, M4.6.11) or a legacy
        bare id (first match, kept for older tests).
        """
        try:
            if item_id is None:
                return None
            if isinstance(item_id, tuple):
                try:
                    r, want = int(item_id[0]), item_id[1]
                except Exception:
                    return None
                try:
                    row = self._model.row_at(r)
                except Exception:
                    row = None
                if (row is not None and not row.is_folder
                        and row.item is not None and row.item.id == want):
                    return self._view.visualRect(self._model.index(r, 0))
                return None
            for r in range(self._model.rowCount()):
                row = self._model.row_at(r)
                if (row is not None and not row.is_folder
                        and row.item is not None
                        and row.item.id == item_id):
                    return self._view.visualRect(self._model.index(r, 0))
        except Exception:
            pass
        return None

    def _hover_repaint(self) -> None:
        """Repaint previous + current hover rects only (M4.6.9).

        Paint is a pure function of (hover occurrence, progress), so
        the outgoing occurrence vanishes the tick after the swap — but
        ONLY if its rect repaints. Margin covers shadow/pin overflow.
        """
        try:
            vp = self._view.viewport()
            region = None
            looks = []
            for look in (self._hover_key, self._hover_prev_key):
                if look is not None:
                    looks.append(look)
            if not looks:
                for look in (self._hover_id, self._hover_prev_id):
                    if look is not None:
                        looks.append(look)
            for look in looks:
                rect = self._hover_row_rect(look)
                if rect is None:
                    continue
                grown = rect.adjusted(-8, -8, 8, 8)
                region = grown if region is None else region.united(grown)
            if region is None:
                vp.update()
            else:
                vp.update(region.intersected(vp.rect()))
        except Exception:
            try:
                self._view.viewport().update()
            except Exception:
                pass

    def _on_hover_tick(self, value) -> None:
        try:
            self._hover_progress = max(0.0, min(1.0, float(value)))
            self._hover_repaint()
        except Exception:
            pass

    def _on_hover_done(self) -> None:
        try:
            if self._hover_progress <= 0.0:
                self._hover_id = None
                self._hover_key = None
        except Exception:
            pass
        try:
            self._hover_repaint()
            self._hover_prev_id = None
            self._hover_prev_key = None
        except Exception:
            pass

    def _play_press(self, row: int, item_id: str) -> None:
        """Fire the 85ms occurrence press feedback (M4.6.11, never gates)."""
        try:
            self._press_anim.stop()
        except Exception:
            pass
        self._press_key = (int(row), str(item_id))
        self._press_progress = 0.0
        try:
            self._press_anim.setStartValue(0.0)
            self._press_anim.setEndValue(1.0)
            self._press_anim.start()
        except Exception:
            pass
        try:
            self._view.viewport().update(
                self._view.visualRect(self._model.index(int(row), 0)))
        except Exception:
            pass

    def _on_press_tick(self, value) -> None:
        try:
            # 0->1->decay: compress in, release out within one 85ms run.
            p = max(0.0, min(1.0, float(value)))
            self._press_progress = p if p < 0.5 else 1.0 - p
            if self._press_key is not None:
                try:
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(int(self._press_key[0]), 0)))
                except Exception:
                    pass
        except Exception:
            pass

    def _on_press_done(self) -> None:
        try:
            self._press_progress = 0.0
            if self._press_key is not None:
                try:
                    self._view.viewport().update(self._view.visualRect(
                        self._model.index(int(self._press_key[0]), 0)))
                except Exception:
                    pass
            self._press_key = None
        except Exception:
            pass

    def _on_context_menu(self, pos) -> None:
        """Context menu for the unified workspace (delegates to builder)."""
        idx = self._view.indexAt(pos)
        self._menu_for(idx.row() if idx.isValid() else -1, pos)

    def _menu_for(self, row_idx: int, pos=None) -> None:
        """Execute the context menu for a model row (empty space = -1)."""
        built = self._context_menu_for(row_idx)
        if built is None:
            return
        menu, dispatch = built
        if pos is None:
            if row_idx >= 0:
                pos = self._view.visualRect(self._model.index(row_idx, 0)).center()
            else:
                pos = self._view.viewport().rect().center()
        chosen = menu.exec(self._view.mapToGlobal(pos))
        action = dispatch.get(chosen)
        if action is not None:
            action()

    def _context_menu_for(self, row_idx: int):
        """Build (menu, dispatch) for a model row without executing.

        ``row_idx < 0`` means empty space (global actions only).
        Returns None for header/hint rows (no menu). Testable without
        exec: inspect the menu, invoke dispatch callables directly.
        Menus share the frosted Inkfall material.
        """
        model = self._model
        menu = InkfallMenu(self, self._tokens)
        dispatch: dict = {}
        if row_idx < 0:
            import_action = menu.addAction("Import PDFs…")
            dispatch[import_action] = self.import_dialog
            folder_action = menu.addAction("New Folder…")
            dispatch[folder_action] = lambda: self.create_folder_dialog()
            return menu, dispatch
        row = model.row_at(row_idx)
        if row is None or row.is_header or row.is_hint:
            return None
        if row.is_folder and row.folder is not None:
            folder = row.folder
            open_a = menu.addAction("Open")
            dispatch[open_a] = lambda: self._on_activated(model.index(row_idx, 0))
            rename_a = menu.addAction("Rename…")
            dispatch[rename_a] = lambda: self._rename_folder(folder.id)
            sub_a = menu.addAction("New subfolder…")
            dispatch[sub_a] = lambda: self.create_folder_dialog(parent_id=folder.id)
            move_menu = add_submenu(menu, "Move to folder", self._tokens)
            self._fill_folder_move_menu(move_menu, dispatch, folder.id)
            menu.addSeparator()
            delete_a = menu.addAction("Delete folder…")
            dispatch[delete_a] = lambda: self._delete_folder(folder.id)
            return menu, dispatch
        if row.item is None:
            return None
        item_id = row.item.id
        open_a = menu.addAction("Open")
        dispatch[open_a] = lambda: self._on_activated(model.index(row_idx, 0))
        menu.addSeparator()
        rename_a = menu.addAction("Rename…")
        dispatch[rename_a] = lambda: self._rename_item(item_id)
        move_menu = add_submenu(menu, "Move to folder", self._tokens)
        root_a = move_menu.addAction("Home Library")
        dispatch[root_a] = lambda: self._move_item_to(item_id, None)
        for folder in self._service.folders():
            label = self._trail_labels(folder.id)
            if label == "Home Library":
                label = folder.name
            action = move_menu.addAction(label)
            dispatch[action] = lambda fid=folder.id: self._move_item_to(item_id, fid)
        menu.addSeparator()
        remove_a = menu.addAction("Remove from library…")
        dispatch[remove_a] = lambda: self._confirm_remove(item_id)
        delete_a = menu.addAction("Delete file…")
        dispatch[delete_a] = lambda: self._confirm_delete_file(item_id)
        return menu, dispatch

    def _fill_folder_move_menu(self, move_menu, dispatch: dict,
                               folder_id: str) -> None:
        """Fill a folder's move targets (root + every valid folder).

        Self and descendants are excluded — the service rejects them,
        and offering an invalid target would be a broken affordance.
        """
        root_a = move_menu.addAction("Home Library")
        dispatch[root_a] = lambda: self._move_folder_to(folder_id, None)
        for folder in self._service.folders():
            if self._service.is_descendant(folder.id, folder_id):
                continue
            label = self._trail_labels(folder.id)
            if label == "Home Library":
                label = folder.name
            action = move_menu.addAction(label)
            dispatch[action] = lambda fid=folder.id: self._move_folder_to(folder_id, fid)

    def _move_item_to(self, item_id: str, folder_id) -> None:
        self._service.move_item(item_id, folder_id)
        self.refresh()
        self.statusMessage.emit(
            "Moved document",
            "" if folder_id is None else self._trail_labels(folder_id))

    def _move_folder_to(self, folder_id: str, target_id: str | None) -> None:
        try:
            self._service.move_folder(folder_id, target_id)
        except Exception as exc:
            self.statusMessage.emit("Cannot move folder", str(exc))
            return
        self.refresh()
        self.statusMessage.emit(
            "Moved folder",
            "" if target_id is None else self._trail_labels(target_id))

    def _rename_item(self, item_id: str) -> None:
        item = self._service.get_item(item_id)
        if item is None:
            return
        self._ask_text(
            "Rename Book",
            "Changes the library display name only. The file on disk keeps"
            " its current name and location.",
            item.display_title,
            item.display_title,
            "Rename",
            lambda name: self._rename_item_confirmed(item_id, name),
        )

    def _rename_item_confirmed(self, item_id: str, name: str) -> None:
        try:
            self._service.rename_item(item_id, name)
        except Exception as exc:
            self.statusMessage.emit("Cannot rename", str(exc))
            return
        self.refresh()

    def _rename_folder(self, folder_id: str) -> None:
        folder = self._service.get_folder(folder_id)
        if folder is None:
            return
        self._ask_text(
            "Rename Folder",
            "Folders keep their contents; only the name changes.",
            folder.name,
            folder.name,
            "Rename",
            lambda name: self._rename_folder_confirmed(folder_id, name),
        )

    def _rename_folder_confirmed(self, folder_id: str, name: str) -> None:
        try:
            self._service.rename_folder(folder_id, name)
        except Exception as exc:
            self.statusMessage.emit("Cannot rename", str(exc))
            return
        # The heading/breadcrumb may show this folder's name.
        self.refresh()

    def _delete_folder(self, folder_id: str) -> None:
        folder = self._service.get_folder(folder_id)
        if folder is None:
            return
        sub_folders, sub_items = self._service.child_counts(folder.id)
        if sub_folders or sub_items:
            body = (
                f"“{folder.name}” contains "
                f"{sub_folders} subfolder{'s' if sub_folders != 1 else ''} and "
                f"{sub_items} document{'s' if sub_items != 1 else ''}.\n\n"
                "Deleting it moves its contents up one level. Files on disk"
                " are not affected."
            )
        else:
            body = (
                f"Delete the empty folder “{folder.name}”?\n\n"
                "This cannot be undone."
            )
        self._ask_confirm(
            "Delete Folder", body, "Delete Folder", danger=True,
            callback=lambda: self._delete_folder_confirmed(folder.id),
        )

    def _delete_folder_confirmed(self, folder_id: str) -> None:
        folder = self._service.get_folder(folder_id)
        parent_id = folder.parent_id if folder is not None else None
        try:
            self._service.delete_folder(folder_id)
        except Exception as exc:
            self.statusMessage.emit("Cannot delete folder", str(exc))
            return
        if self._current_folder_id == folder_id:
            # Browsing the deleted folder: back out to its parent.
            self._current_folder_id = parent_id
        self.refresh()
        self.statusMessage.emit(
            "Folder deleted",
            "Contents moved up one level; files kept on disk")

    def _confirm_remove(self, item_id: str) -> None:
        item = self._service.get_item(item_id)
        if item is None:
            return
        self._ask_confirm(
            "Remove from Library",
            f"Remove “{item.display_title}” from the library?\n\n"
            "The file stays on disk — only the library entry is forgotten.",
            "Remove",
            danger=False,
            callback=lambda: self._remove_item_confirmed(item_id),
        )

    def _remove_item_confirmed(self, item_id: str) -> None:
        try:
            self._service.remove_item(item_id)
        except Exception as exc:
            self.statusMessage.emit("Cannot remove", str(exc))
            return
        self.refresh()
        self.statusMessage.emit("Removed from library", "File kept on disk")

    def _confirm_delete_file(self, item_id: str) -> None:
        item = self._service.get_item(item_id)
        if item is None:
            return
        self._ask_confirm(
            "Delete File",
            f"PERMANENTLY delete this file from disk?\n{item.normalized_path}\n\n"
            "The library entry is removed as well. This cannot be undone.",
            "Delete File",
            danger=True,
            callback=lambda: self._delete_file_confirmed(item_id),
        )

    def _delete_file_confirmed(self, item_id: str) -> None:
        try:
            self._service.delete_file(item_id)
        except Exception as exc:
            self.statusMessage.emit("Cannot delete file", str(exc))
            return
        self.refresh()
        self.statusMessage.emit("File deleted", "")

    # -- drag & drop (external PDFs only) --------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and any(
            u.toLocalFile().lower().endswith(".pdf") for u in event.mimeData().urls()
        ):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile().lower().endswith(".pdf")]
        if paths:
            event.acceptProposedAction()
            self.import_paths(paths)
        else:
            super().dropEvent(event)
