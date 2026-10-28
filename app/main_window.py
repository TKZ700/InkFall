"""Inkfall application shell (M4.6.4 + Phase 2 document tabs).

Custom minimal chrome (frameless title bar), no menu bar, no status bar,
no sidebar: one full-window Library workspace plus a floating settings
control (bottom-right, Library only), and a dedicated PDF workspace
(minimal 48px bar, pure-black canvas) with a 180ms cross-fade that never
gates document loading.

Phase 2: one shared :class:`~app.viewer.PdfViewer` with only ONE live
render pipeline. The window owns a :class:`_TabSession` record per open
tab (document identity, in-memory view state, stable tab id); switching
validates/builds the target pipeline before releasing the outgoing one,
and inactive tabs keep their managed ``Document`` alive. The tab strip
(:mod:`app.tabbar`) is presentation only and reports intent by signal.
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import (
    QEvent,
    QEventLoop,
    QObject,
    QRunnable,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QScrollBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import fx
from app.library.library_page import LibraryPage
from app.settings_button import SettingsFab
from app.settings_panel import SettingsModal
from app.new_blank_modal import BlankPreferencesModal
from app.new_panel import NewModal
from app.duplicate_open_modal import DuplicateOpenModal
from app.instance_hub import InstanceHub
from core.instance_registry import normalize_key, scan_instances
from app.theme.manager import ThemeManager
from app.thumbnails.service import ThumbnailService
from app.titlebar import CONTROL_W, TITLE_BAR_H, TitleBar
from app.toast import ToastHost
from app.tabbar import TabStrip
from app.viewer import PdfViewer
from app.workspace import ViewerBar
from core.errors import InkfallError
from document.backend import (
    EncryptedRewriteError,
    NativeAnnotationError,
    SaveLockedError,
    SaveVerificationError,
)
from document.backend import enumerate_page_infos
from document.errors import DocumentClosedError, InvalidPdfError
from document.manager import BLANK_KEY_PREFIX, DocumentManager
from library.models import strip_pdf_suffix
from library.service import LibraryService
from library.store import LibraryStore
from rendering.pipeline import DocumentPipeline
from tools.models import ToolType

__all__ = ["MainWindow", "default_library_db_path"]

#: Edge-resize grip thickness (frameless window, Qt-native resize).
_RESIZE_MARGIN = 6


class _TabSession:
    """Per-tab document session record (the viewer itself stays shared).

    ``doc`` is the managed :class:`document.document.Document` owned by this
    tab (closed only when the tab closes). ``view_state`` is the in-memory
    payload captured when the tab was last suspended (reading position, fit
    mode, appearance); ``tab_id`` is stable for the lifetime of the tab so
    the strip, signals and reordering never have to renumber identity.
    """

    __slots__ = ("tab_id", "doc", "view_state")

    def __init__(self, doc, tab_id: str) -> None:
        self.tab_id = str(tab_id)
        self.doc = doc
        self.view_state: dict | None = None


class _PendingSwitch:
    """One in-flight asynchronous tab-switch request.

    Three distinct states are modeled around it: the REQUESTED tab
    (``session`` here, highlighted in the strip immediately), the
    document ATTACHED to the viewer (``MainWindow._active_session`` +
    the viewer's own document, unchanged until commit), and this pending
    preparation, identified by ``epoch``. The latest request always
    wins: a new request replaces the pending record and the stale
    completion is retired without activating anything.

    ``failure_mode`` selects the recovery when preparation fails:
    ``"drop"`` — the tab was freshly created for this open (remove it);
    ``"restore"`` — a deferred open superseded a live tab (fall back);
    ``"keep"`` — an established tab was clicked (revert the highlight to
    the still-attached session; the viewer was never detached).
    """

    __slots__ = ("epoch", "session", "focus", "enter_workspace",
                 "failure_mode")

    def __init__(self, epoch: int, session, *, focus: bool,
                 enter_workspace: bool, failure_mode: str) -> None:
        self.epoch = int(epoch)
        self.session = session
        self.focus = bool(focus)
        self.enter_workspace = bool(enter_workspace)
        self.failure_mode = str(failure_mode)


class _SwitchHub(QObject):
    """GUI-thread delivery point for background preparation results.

    The hub lives on the GUI thread (parented to the window); workers
    emit from a pool thread and Qt's auto-connection queues the signal,
    so the completion handler always runs where widgets are touchable.
    """

    done = Signal(int, object)


class _SwitchPrep(QRunnable):
    """Background preparation of a destination pipeline.

    Deliberately contains NO Qt widgets/objects created on the pool
    thread. ``DocumentPipeline`` is pure Python (threading +
    ``multiprocessing.spawn``; no QObject, no timers, no event-loop
    dependency — see its module contract "Orchestrator only, no Qt"), so
    constructing it off-thread is safe by design. Page geometry comes
    from a PRIVATE short-lived fitz handle on the document's render path
    (:func:`document.backend.enumerate_page_infos`) — never the
    GUI-owned, single-threaded ``PdfBackend`` handle. Results cross to
    the GUI thread as plain data through the hub signal.
    """

    def __init__(self, hub: _SwitchHub, epoch: int, document_id,
                 render_path: str, zoom: float) -> None:
        super().__init__()
        self._hub = hub
        self._epoch = int(epoch)
        self._document_id = document_id
        self._render_path = str(render_path)
        self._zoom = float(zoom)

    def run(self) -> None:  # noqa: N802
        try:
            pages = enumerate_page_infos(self._render_path,
                                         self._document_id)
            pipeline = DocumentPipeline(
                self._document_id, self._render_path, pages,
                ui_zoom=self._zoom, user_rotation=0)
            try:
                # Spawn the decoder worker processes NOW (off the GUI
                # thread); the first decode would otherwise pay the
                # ~300ms spawn on the submitting thread.
                pipeline.prewarm()
            except Exception:
                pass
            self._hub.done.emit(self._epoch, {
                "ok": True, "pipeline": pipeline, "zoom": self._zoom})
        except Exception as exc:
            self._hub.done.emit(self._epoch, {
                "ok": False, "pipeline": None, "zoom": self._zoom,
                "error": str(exc) or exc.__class__.__name__})


class _SaveHub(QObject):
    """GUI-thread delivery point for one background save step.

    Lives on the GUI thread (parented to the window); the pool thread
    emits from there and Qt queues the signal, so the waiting loop always
    resumes where widgets are touchable.
    """

    done = Signal(int, object)


class _CallJob(QRunnable):
    """Run one plain callable on the save pool and report its outcome.

    Deliberately contains no Qt objects. Only the Qt-free save steps go
    through it (working-copy materialization, pipeline rebind); the
    export itself cannot use a thread because PyMuPDF holds the GIL for
    the entire ``Document.save`` call, which is why it runs in a child
    process instead (see :meth:`PdfBackend.export_pdf_in_process`).
    """

    def __init__(self, hub: _SaveHub, epoch: int, func) -> None:
        super().__init__()
        self._hub = hub
        self._epoch = int(epoch)
        self._func = func

    def run(self) -> None:  # noqa: N802
        try:
            value = self._func()
            result = {"ok": True, "value": value}
        except BaseException as exc:  # noqa: BLE001 - re-raised on the GUI thread
            result = {"ok": False, "error": exc}
        try:
            self._hub.done.emit(self._epoch, result)
        except Exception:
            pass


#: Human-readable progress labels for the backend's opaque stage names.
_SAVE_STAGES = {
    "prepare": "Preparing the document…",
    "rebind": "Releasing the previous file…",
    "snapshot": "Creating a snapshot…",
    "annotations": "Writing annotations…",
    "write": "Writing the PDF…",
    "verify": "Verifying the saved file…",
    "replace": "Replacing the file…",
}


class _PipelineClose(QRunnable):
    """Background retirement of a detached pipeline.

    ``DocumentPipeline.close`` cancels in-flight work, JOINS the engine
    thread pool and shuts down the process decoder (feeder/collector
    joins, per-worker join timeouts) — all blocking work that used to
    run on the GUI thread at every switch. The pipeline is Qt-free and
    its own close is documented thread-safe (no pipeline lock held
    across joins), so running it on the pool is safe.
    """

    def __init__(self, pipeline) -> None:
        super().__init__()
        self._pipeline = pipeline

    def run(self) -> None:  # noqa: N802
        try:
            self._pipeline.close()
        except Exception:
            pass


def _apply_native_rounded_corners(window) -> bool:
    """Prefer the platform-native rounded-corner mechanism (M4.6.12).

    Windows 11+: ``DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_
    PREFERENCE=DWMWCP_ROUND)``. No translucency, no mask, no style
    or architecture change — native move/resize/maximize/fullscreen
    and the 6px grip path are untouched. Returns True when applied;
    silent no-op (False) on other platforms or any failure.
    """
    try:
        import sys

        if sys.platform != "win32":
            return False
        try:
            hwnd = int(window.winId())
        except Exception:
            return False
        import ctypes

        try:
            dwm = ctypes.WinDLL("dwmapi")
        except Exception:
            return False
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        hr = dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
        return int(hr) == 0
    except Exception:
        return False


def default_library_db_path() -> str:
    """Default on-disk location for the library database."""
    from PySide6.QtCore import QStandardPaths

    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if not base:
        import os

        base = os.path.join(os.path.expanduser("~"), ".inkfall")
    import os

    return os.path.join(base, "inkfall", "library.db")


class MainWindow(QMainWindow):
    """Root window: library shell + immersive PDF workspace."""

    def __init__(
        self,
        document_manager: DocumentManager | None = None,
        library_service: LibraryService | None = None,
        theme: ThemeManager | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Inkfall")
        # Custom chrome: frameless shell, native taskbar presence kept.
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(800, 600)
        self.resize(1280, 800)
        # M4.6.8 window-level entrance: the top-level window starts
        # transparent and fades in after first show (see showEvent /
        # _play_startup_entrance). Set pre-show so time zero never
        # flashes a fully-formed window.
        try:
            self.setWindowOpacity(0.0)
        except Exception:
            pass
        self._geom_epoch = 0  # cancels stale geometry tweens
        self._geom_anim = None  # owned window-geometry animator
        # M4.6.9 exact geometry: the last genuine user-controlled
        # Normal-state rect (continuous tracking in resize/move; never
        # written while maximized/fullscreen/minimized/tweening).
        self._normal_geom = None
        # True while OUR geometry tween drives setGeometry (tracking
        # pauses so tween frames never corrupt the saved rect).
        self._tweening_geom = False
        # Pre-fullscreen state (restored explicitly on exit, never guessed).
        self._was_maximized_before_fs = False
        # True while a restore/exit finish owns the seam: Normal-state
        # resize/move events then must not rewrite the saved rect with
        # transitional (still-maximized-size) geometry.
        self._restoring_geom = False
        try:
            from app.icons import app_icon

            _brand = app_icon()
            if not _brand.isNull():
                self.setWindowIcon(_brand)
        except Exception:
            pass

        self._manager = document_manager if document_manager is not None else DocumentManager()
        self._theme = theme if theme is not None else ThemeManager(self)
        self._theme.apply()
        if library_service is not None:
            self._library = library_service
            self._own_store = False
        else:
            self._store = LibraryStore(default_library_db_path())
            self._store.open()
            self._library = LibraryService(self._store)
            self._own_store = True
        self._thumbs = ThumbnailService(parent=self)
        self._theme.changed.connect(self._on_theme_changed)
        try:
            from app import fx as _motion_fx

            _motion_fx.restore_motion_mode()
        except Exception:
            pass

        # -- Phase 2 tab model: records + one shared viewer session --------
        # `_tab_sessions` is the authoritative tab order; `_active_session`
        # is the ONE tab whose document is attached to the viewer (and
        # therefore the only live render pipeline).
        self._tab_sessions: list[_TabSession] = []
        self._active_session: _TabSession | None = None
        # Tab that was active when a deferred open started (failure fallback).
        self._pending_open_fallback: _TabSession | None = None
        # Deferred-open epoch (stale finishes never attach).
        self._open_epoch = 0
        # Fullscreen auto-hide state for the strip.
        self._strip_fullscreen = False
        self._strip_revealed = False
        self._reveal_armed = True
        # -- Async tab switching (Phase 2): the requested tab, the
        # attached document and the pending preparation are distinct.
        # The latest request wins; stale results retire in background.
        self._switch_epoch = 0
        self._pending_switch: _PendingSwitch | None = None
        self._switch_hub = _SwitchHub(self)
        self._switch_hub.done.connect(self._on_switch_prepared)
        self._switch_pool = QThreadPool(self)
        self._switch_pool.setMaxThreadCount(2)
        # -- background saves: exclusive document handoff ------------------
        # One save at a time. The worker owns the document's live handle
        # for the duration; the GUI thread stays in a nested progress loop
        # (window keeps painting, platform never marks it unresponsive).
        self._save_pool = QThreadPool(self)
        self._save_pool.setMaxThreadCount(1)
        self._save_epoch = 0
        self._save_in_flight = False
        try:
            self._viewer.set_pipeline_retirer(self._retire_pipeline_async)
        except Exception:
            pass

        # -- shell layout: title bar + full-width content + overlays -----
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self._titlebar = TitleBar(self._theme.tokens, root)
        root_layout.addWidget(self._titlebar)
        # Phase 2: the strip shares the title-bar row with the window
        # controls (one unified top bar, no extra chrome row).
        self._tab_strip = TabStrip(self._theme.tokens, self._titlebar)
        self._titlebar.set_tab_strip(self._tab_strip)
        self._tab_strip.set_home_active(True)
        self._titlebar.pointerExited.connect(self._conceal_tab_strip)
        try:
            self._titlebar.installEventFilter(self)
        except Exception:
            pass

        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        content = QWidget()
        self._content = content
        content_grid = QGridLayout(content)
        content_grid.setContentsMargins(0, 0, 0, 0)
        content_grid.setSpacing(0)
        self._stack = QStackedWidget()
        self._library_page = LibraryPage(self._library, self._thumbs, self._theme.tokens)
        self._stack.addWidget(self._library_page)
        viewer_wrap = QWidget()
        viewer_layout = QVBoxLayout(viewer_wrap)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        self._viewer_bar = ViewerBar(self._theme.tokens, viewer_wrap)
        # Compatibility shim: the interactive ViewerBar is no longer part of
        # the visible chrome (tabs + window controls own the top row). It is
        # kept constructed but never laid out or shown so its public labels
        # and accessible controls remain available to existing callers.
        self._viewer_bar.setVisible(False)
        self._viewer = PdfViewer(self._manager)
        viewer_layout.addWidget(self._viewer, stretch=1)
        self._viewer_wrap = viewer_wrap
        self._stack.addWidget(viewer_wrap)
        content_grid.addWidget(self._stack, 0, 0)
        self._toast = ToastHost(self._theme.tokens, content)
        content_grid.addWidget(self._toast, 0, 0)
        # Floating settings: overlaid child, fixed bottom-right relative
        # to the window, above scrollable content, never in page layout.
        # Library only — hidden (with the modals) in the PDF workspace.
        self._settings_fab = SettingsFab(self._theme, self._theme.tokens, content)
        # Centered modals overlay `content` (full client area below the
        # title bar): dimmed backdrop, centered panel, layout untouched.
        self._settings_modal = SettingsModal(self._theme, self._theme.tokens, content)
        self._new_modal = NewModal(self._theme, self._theme.tokens, content)
        self._blank_modal = BlankPreferencesModal(self._theme.tokens, content)
        # Duplicate-open choice (Book is Already Open): same centered
        # overlay family; targets are held on the window per invocation.
        self._duplicate_modal = DuplicateOpenModal(self._theme.tokens, content)
        self._duplicate_modal.continueRequested.connect(self._on_duplicate_continue)
        self._duplicate_modal.readonlyRequested.connect(self._on_duplicate_readonly)
        self._duplicate_target_session = None
        self._duplicate_target_path = None
        # Cross-instance coordination: this window advertises its open
        # documents and answers release-requests from other InkFall
        # windows saving the same canonical file. Refusals/timeouts flow
        # into the ordinary SaveLockedError recovery during saves. A hub
        # that cannot start (exotic environment) must never break the
        # window: every consumer tolerates a None hub and saves fall
        # back to the ordinary destination-lock handling.
        self._instance_hub = None
        self._instance_release_saving = False
        try:
            hub = InstanceHub(self)
            hub.releaseRequested.connect(self._on_instance_release_request)
            self._instance_hub = hub
            self._publish_instance_docs()
        except Exception:
            self._instance_hub = None
        self._settings_fab.clicked.connect(self.toggle_settings_panel)
        self._position_fab()
        shell_layout.addWidget(content, stretch=1)
        root_layout.addWidget(shell, stretch=1)
        self.setCentralWidget(root)
        self._apply_chrome_tokens(self._theme.tokens)
        # M4.6.10 cursor delivery: moves are delivered per-widget, so
        # every descendant needs tracking for the app-level observer
        # to see edge hovers. Tracking never disturbs drag/controls.
        try:
            self.setMouseTracking(True)
            for child in self.findChildren(QWidget):
                try:
                    child.setMouseTracking(True)
                except Exception:
                    pass
        except Exception:
            pass

        # -- shortcuts without chrome (menu bar removed) -------------------
        # The registry is the single owner of every customizable binding
        # (Save included); no legacy hardcoded handlers remain.
        self._last_non_pan_slot_id: str | None = None
        self._install_shortcuts()

        # -- wiring ------------------------------------------------------------
        # Phase 2: the window observes viewer hover moves for the fullscreen
        # tab-strip reveal (no overlay widget steals canvas/stylus input).
        try:
            self._viewer.installEventFilter(self)
        except Exception:
            pass
        self._titlebar.minimizeRequested.connect(self._minimize_with_feedback)
        self._titlebar.maximizeToggleRequested.connect(self.toggle_maximize_restore)
        self._titlebar.fullscreenToggleRequested.connect(self.toggle_fullscreen)
        self._titlebar.closeRequested.connect(self._close_with_feedback)
        self._viewer_bar.backRequested.connect(self.show_library)
        self._viewer_bar.fullscreenRequested.connect(self.toggle_fullscreen)
        self._library_page.openRequested.connect(self.open_library_item)
        self._library_page.statusMessage.connect(self._toast.show_message)
        # Phase 2: the strip reports intent only; the window owns sessions.
        self._tab_strip.activateRequested.connect(self._activate_tab_by_id)
        self._tab_strip.closeRequested.connect(self._close_tab_by_id)
        self._tab_strip.homeRequested.connect(self.show_library)
        self._tab_strip.tabsReordered.connect(self._reorder_tab_sessions)
        # Empty strip background keeps native move / maximize behavior while
        # tab presses stay owned by the tabs (no drag/reorder conflict).
        # The Library in fullscreen ignores those background gestures: its
        # chrome is reachable without restoring the window (library-only
        # guard; workspace reveal behavior is unchanged).
        self._tab_strip.chromeDragRequested.connect(
            self._on_chrome_drag_requested)
        self._tab_strip.chromeMaximizeRequested.connect(
            self._on_chrome_maximize_requested)
        self._tab_strip.menuClosed.connect(self._conceal_after_menu)
        self._tab_strip.closeAllRequested.connect(self._close_all_tabs_guarded)
        # M6.8.2 clipboard slice: a failed Copy/Cut/Paste reports through
        # the existing toast host instead of window chrome reaching into
        # the viewer (or the viewer inventing its own status surface).
        self._viewer.statusMessage.connect(self._toast.show_message)
        self._library_page.newRequested.connect(self.toggle_new_panel)
        self._new_modal.createBlankRequested.connect(self._create_blank_dialog)
        self._new_modal.openPdfRequested.connect(self._open_dialog)
        self._blank_modal.createRequested.connect(self._create_blank_workspace)
        # M8: unsaved-workspace title + Save affordances.
        self._viewer.documentStateChanged.connect(self._sync_workspace_title)
        self._viewer.saveRequested.connect(self._save_current)

    def _install_shortcuts(self) -> None:
        """Install the centralized shortcut registry (single owner).

        Every customizable application command is routed through
        :mod:`app.shortcuts`; the former window-level QAction block is gone
        so a rebound key genuinely replaces the old binding.
        """
        from app.shortcuts import registry

        viewer = self._viewer
        handlers = {
            "view.zoom_in": viewer.zoom_in,
            "view.zoom_out": viewer.zoom_out,
            "view.fit_width": lambda: viewer.apply_view_mode("width"),
            "view.fit_height": lambda: viewer.apply_view_mode("height"),
            "view.actual_size": lambda: viewer.apply_view_mode("actual"),
            "view.next_page": viewer._view_next_page,
            "view.prev_page": viewer._view_prev_page,
            "edit.undo": viewer._on_undo_requested,
            "edit.redo": viewer._on_redo_requested,
            "edit.delete": viewer.delete_selected_annotation,
            "edit.deselect": self._cancel_or_leave,
            "file.save": self._save_current,
            "file.save_all": self._save_all_dirty,
            "file.open": self._open_dialog,
            "file.import": self._import_dialog,
            "file.next_pdf": lambda: self._cycle_tab(1),
            "file.prev_pdf": lambda: self._cycle_tab(-1),
            "file.close_tab": self._close_active_tab,
            "app.library": self.show_library,
            "app.theme": self._toggle_theme,
            "app.fullscreen": self.toggle_fullscreen,
            "tools.toggle_pan": self._toggle_pan_tool,
        }
        for index in range(10):
            handlers[f"tools.slot_{index + 1}"] = (
                lambda _checked=False, n=index: self._activate_tool_slot(n))
        registry().install(
            self, viewer, handlers,
            workspace_provider=lambda: self.in_workspace)
        # Back-compat handle for callers that referenced the old action.
        self._fullscreen_action = registry().action("app.fullscreen")
        self._viewer.tool_shelf.toolSelected.connect(
            self._remember_active_tool)

    def _activate_tool_slot(self, index: int) -> None:
        """Resolve toolbar slot ``index`` to its *current* assigned tool."""
        try:
            store = self._viewer.tool_store
            slots = store.slots
            if index < 0 or index >= len(slots):
                self._toast.show_message(
                    f"Tool slot {index + 1} is empty",
                    "Assign a tool in the toolbar first.")
                return
            self._viewer.tool_shelf.select_slot(slots[index].id.value)
        except Exception:
            pass

    def _remember_active_tool(self, slot) -> None:
        """Track the last non-Pan slot for the Pan toggle."""
        try:
            if slot is not None and slot.kind != ToolType.PAN:
                self._last_non_pan_slot_id = slot.id.value
        except Exception:
            pass
        self._refresh_shortcuts()

    def _toggle_pan_tool(self) -> None:
        """Toggle Pan and the previously active non-Pan tool.

        Uses the normal tool-selection path, so toolbar state, cursor and
        gesture handling behave exactly like a slot click. If the
        remembered tool was removed, fall back to the first non-Pan slot.
        """
        try:
            store = self._viewer.tool_store
            shelf = self._viewer.tool_shelf
            pan_slot = next(
                (slot for slot in store.slots
                 if slot.kind == ToolType.PAN), None)
            if pan_slot is None:
                return
            active = store.active()
            if active is not None and active.id.value == pan_slot.id.value:
                target = None
                if self._last_non_pan_slot_id:
                    target = store.find(self._last_non_pan_slot_id)
                if target is None:
                    target = next(
                        (slot for slot in store.slots
                         if slot.kind != ToolType.PAN), None)
                if target is not None:
                    shelf.select_slot(target.id.value)
                return
            if active is not None and active.kind != ToolType.PAN:
                self._last_non_pan_slot_id = active.id.value
            shelf.select_slot(pan_slot.id.value)
        except Exception:
            pass

    # -- shared chrome gestures -------------------------------------------

    def _on_chrome_drag_requested(self) -> None:
        """Empty tab-strip press starts a native move (not from Library FS).

        Fullscreen is a window state, not a page: dragging the Library's
        visible strip must never restore/move the window behind the user's
        back. Workspace fullscreen keeps the established native-move path.
        """
        if self.isFullScreen() and not self.in_workspace:
            return
        self._titlebar.begin_system_move()

    def _on_chrome_maximize_requested(self) -> None:
        """Empty strip double-click toggles maximize/restore.

        On the Library in fullscreen the strip is persistently visible, so
        an accidental double-click is ignored rather than silently leaving
        fullscreen. The actual Maximize/Restore control keeps its
        established behavior (fullscreen exits to the recorded state).
        """
        if self.isFullScreen() and not self.in_workspace:
            return
        self.toggle_maximize_restore()

    def _cancel_or_leave(self) -> None:
        """Escape: cancel a move / deselect, else the existing Library step.

        The registry disables this action in fullscreen and whenever a
        modal or text editor owns focus, so those contexts keep their
        existing Escape handling.
        """
        try:
            if self._viewer.cancel_selection():
                return
        except Exception:
            pass
        if self.in_workspace:
            self.show_library()

    def _refresh_shortcuts(self) -> None:
        """Re-evaluate registry guards after workspace/state transitions."""
        try:
            from app.shortcuts import registry

            registry().refresh()
        except Exception:
            pass

    # -- accessors --------------------------------------------------------------

    @property
    def viewer(self) -> PdfViewer:
        """The hosted document viewer (sole session owner)."""
        return self._viewer

    @property
    def library_page(self) -> LibraryPage:
        """The Home/Library page."""
        return self._library_page

    @property
    def library_service(self) -> LibraryService:
        """The application-level library (metadata only)."""
        return self._library

    @property
    def settings_fab(self) -> SettingsFab:
        """The floating settings control (bottom-right, Library only)."""
        return self._settings_fab

    @property
    def settings_modal(self) -> SettingsModal:
        """The centered settings modal (Library only, never modal-loop)."""
        return self._settings_modal

    @property
    def new_panel(self) -> NewModal:
        """The centered New-action modal (Library only)."""
        return self._new_modal

    @property
    def blank_modal(self) -> BlankPreferencesModal:
        """The New Blank paper-customization modal (Library only)."""
        return self._blank_modal

    @property
    def titlebar(self) -> TitleBar:
        """The custom minimal window chrome."""
        return self._titlebar

    @property
    def toast(self) -> ToastHost:
        """The transient notification overlay."""
        return self._toast

    @property
    def in_workspace(self) -> bool:
        """True while the PDF workspace is shown."""
        return self._stack.currentIndex() == 1

    # -- frameless edge resize (Qt-native, never manual geometry) ----------

    def _resize_edge_at(self, pos) -> object | None:
        """Qt.Edges for a window-local pos near a resize grip (M4.6.9).

        All four sides plus all four corners, Qt-native via
        ``startSystemResize``. The outer 6px of the top strip is the
        top grip; the title bar keeps drag/double-click below it, and
        interactive descendants (window buttons, etc.) always veto.
        Pure helper, safe to test.
        """
        try:
            if self.isMaximized() or self.isFullScreen():
                return None
            w, h, m = self.width(), self.height(), _RESIZE_MARGIN
            top = pos.y() <= m
            if pos.y() <= TITLE_BAR_H and not top:
                return None  # title-bar drag zone (below the top grip)
            left = pos.x() <= m
            right = pos.x() >= w - m
            bottom = pos.y() >= h - m
            if not (left or right or bottom or top):
                return None
            node = self.childAt(pos)
            while node is not None:
                # M4.6.10 narrow veto: inside the 6px frame grip, only
                # true controls veto resize (buttons, text fields, FAB,
                # title-bar band, scrollbars). Item views and passive
                # gaps yield — the frame owns its edge pixels.
                # M4.6.12 scrollbar veto: a scrollbar (reader, library,
                # any item view) owns its pixels — it must keep the
                # normal scrollbar cursor/interaction and must never
                # start a system resize. This fixes BOTH the resize
                # cursor and press capture; cursor-only masking would
                # leave press-stealing in place. Ancestor walk covers
                # scrollbar sub-controls painted without child widgets.
                if isinstance(node, QScrollBar):
                    return None
                if isinstance(node, (QAbstractButton, QLineEdit)):
                    return None
                if node is self._settings_fab:
                    return None
                try:
                    if node is self._viewer.tool_shelf:
                        return None
                    parent = node.parent()
                    while parent is not None:
                        if parent is self._viewer.tool_shelf:
                            return None
                        parent = parent.parent()
                except Exception:
                    pass
                # The title bar vetoes side/bottom grips over its band,
                # but the top grip explicitly owns the outer strip.
                if node is self._titlebar and not top:
                    return None
                node = node.parent()
            if top and left:
                return Qt.Edge.TopEdge | Qt.Edge.LeftEdge
            if top and right:
                return Qt.Edge.TopEdge | Qt.Edge.RightEdge
            if left and bottom:
                return Qt.Edge.LeftEdge | Qt.Edge.BottomEdge
            if right and bottom:
                return Qt.Edge.RightEdge | Qt.Edge.BottomEdge
            if top:
                return Qt.Edge.TopEdge
            if left:
                return Qt.Edge.LeftEdge
            if right:
                return Qt.Edge.RightEdge
            return Qt.Edge.BottomEdge
        except Exception:
            return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Press on a passive edge grip starts a native system resize."""
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                # M4.6.9: user intent always wins over a running tween.
                self._abort_window_tween(user=True)
                edges = self._resize_edge_at(event.position().toPoint())
                if edges is not None:
                    handle = self.windowHandle()
                    if handle is not None and handle.startSystemResize(edges):
                        event.accept()
                        return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Resize-grip cursor feedback (no behavior change on press)."""
        try:
            self._update_resize_cursor(event.position().toPoint())
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def _update_resize_cursor(self, window_pos) -> None:
        """Set/unset the resize cursor for a window-local pos.

        Shared by the direct move handler and the app-level move
        observer (M4.6.10 cursor delivery). Window-inherited cursor
        only — child explicit cursors (PointingHand controls) always
        win their own pixels. Diagonal mapping follows Qt docs:
        TL/BR = "/" (FDiag), TR/BL = "\\" (BDiag).
        """
        try:
            edges = self._resize_edge_at(window_pos)
            if edges is None:
                self.unsetCursor()
            elif edges == (Qt.Edge.TopEdge | Qt.Edge.LeftEdge):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edges == (Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edges == (Qt.Edge.TopEdge | Qt.Edge.RightEdge):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edges == (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
        except Exception:
            pass

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Abort geometry tweens on title-bar drag (user wins, M4.6.9).

        M4.6.11: presses in the outer 6px top strip pre-empt the
        title-bar move and start a native system resize instead.
        """
        try:
            from PySide6.QtCore import QEvent

            if (obj is self._titlebar
                    and event.type() == QEvent.Type.MouseButtonPress):
                self._abort_window_tween(user=True)
                try:
                    if event.button() == Qt.MouseButton.LeftButton:
                        gpos = event.globalPosition().toPoint()
                        wpos = self.mapFromGlobal(gpos)
                        edges = self._resize_edge_at(wpos)
                        if edges is not None:
                            handle = self.windowHandle()
                            if (handle is not None
                                    and handle.startSystemResize(edges)):
                                event.accept()
                                return True
                except Exception:
                    pass
                return False
            # Phase 2 fullscreen reveal: a hover near the very top of the
            # canvas asks for the hidden tab strip. Plain pointer tracking
            # only — no overlay above the canvas, so inking and stylus input
            # are untouched. The ``_reveal_armed`` latch (re-armed once the
            # pointer moves well below the edge) gives hysteresis, so hiding
            # the strip can never immediately re-reveal it.
            if obj is self._viewer and self._strip_fullscreen:
                etype = event.type()
                if etype in (QEvent.Type.MouseMove, QEvent.Type.TabletMove):
                    y = None
                    try:
                        y = float(event.position().y())
                    except Exception:
                        try:
                            y = float(event.posF().y())
                        except Exception:
                            y = None
                    if y is not None:
                        if y > 24.0:
                            self._reveal_armed = True
                        elif y <= 6.0 and self._reveal_armed:
                            self._reveal_tab_strip()
            # M4.6.10 cursor delivery: observe moves app-wide (moves
            # over children never reach mouseMoveEvent). Window-local
            # mapping, inherited cursor only, never consumes.
            if (obj is self._titlebar
                    and event.type() in (QEvent.Type.MouseMove,
                                         QEvent.Type.HoverMove)):
                try:
                    target = obj.window() if hasattr(obj, "window") else None
                except Exception:
                    target = None
                if target is self and self.isVisible():
                    try:
                        gpos = event.globalPosition().toPoint()
                    except Exception:
                        gpos = None
                    if gpos is not None:
                        self._update_resize_cursor(self.mapFromGlobal(gpos))
        except Exception:
            pass
        return False

    def _position_fab(self) -> None:
        """Pin the settings button bottom-right (layout-independent)."""
        try:
            margin = 16
            self._settings_fab.move(
                max(0, self._content.width() - self._settings_fab.width() - margin),
                max(0, self._content.height() - self._settings_fab.height() - margin),
            )
            self._settings_fab.raise_()
        except Exception:
            pass

    def toggle_settings_panel(self) -> None:
        """Open/close the centered Settings modal."""
        try:
            fx.press_feedback(self._settings_fab, self._theme.tokens.dur_fast_ms)
            if self._settings_modal.isVisible():
                self._settings_modal.close_modal()
                return
            self._settings_modal.open_modal(self._settings_fab)
        except Exception:
            pass

    def toggle_new_panel(self) -> None:
        """Open/close the centered New-action modal."""
        try:
            fx.press_feedback(self._library_page._new_btn,
                              self._theme.tokens.dur_fast_ms)
            if self._new_modal.isVisible():
                self._new_modal.close_modal()
                return
            self._new_modal.open_modal(self._library_page._new_btn)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep the floating settings button anchored on resize."""
        super().resizeEvent(event)
        self._position_fab()
        self._sync_normal_geom()

    def moveEvent(self, event) -> None:  # noqa: N802
        """Track genuine Normal-state moves for exact restore."""
        super().moveEvent(event)
        self._sync_normal_geom()

    def _is_plain_normal(self) -> bool:
        """True only for a genuine user-controlled Normal state."""
        try:
            return (not self.isMaximized() and not self.isFullScreen()
                    and not self.isMinimized())
        except Exception:
            return False

    def _sync_normal_geom(self) -> None:
        """Remember the exact Normal rect (M4.6.9).

        Called from resize/move. Writes ONLY from genuine Normal
        geometry with no tween driving — maximized, fullscreen,
        minimized, and tween frames never touch the saved rect, so
        restore always returns to the true pre-transition rect.
        """
        try:
            if not self._is_plain_normal():
                return
            if getattr(self, "_tweening_geom", False):
                return
            if getattr(self, "_restoring_geom", False):
                return
            from PySide6.QtCore import QRect

            self._normal_geom = QRect(self.geometry())
        except Exception:
            pass

    def _save_normal_geom_once(self) -> None:
        """Exact-once save at maximize/fullscreen entry (M4.6.9).

        Only from genuine Normal state; while maximized/fullscreen/
        minimized/tweening the tracked rect is already correct and
        must not be overwritten. Also refuses while a restore settle
        is in flight: native state has already left maximized but the
        compositor geometry is still transitional (DWM un-maximize
        animation), so saving here would poison exact-restore with a
        near-maximized rect and strand later clicks in no-op
        transitions. Mirrors the _restoring_geom guard in
        _sync_normal_geom.
        """
        try:
            if not self._is_plain_normal():
                return
            if getattr(self, "_tweening_geom", False):
                return
            if getattr(self, "_restoring_geom", False):
                return
            from PySide6.QtCore import QRect

            self._normal_geom = QRect(self.geometry())
        except Exception:
            pass

    def showEvent(self, event) -> None:  # noqa: N802
        """Re-pin the floating button once real geometry exists."""
        super().showEvent(event)
        self._position_fab()
        # M4.6.12 native rounded corners (HWND exists from first show;
        # guarded no-op off Windows 11 — never affects resize/move).
        try:
            if not getattr(self, "_rounded_corners_applied", False):
                self._rounded_corners_applied = True
                _apply_native_rounded_corners(self)
        except Exception:
            pass
        # M4.6.9: a screen/DPI change invalidates tween targets — the
        # native manager stays authoritative after the abort.
        # M4.6.10: observe moves app-wide for resize cursors.
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None and not getattr(self, "_app_filter_installed", False):
                self._app_filter_installed = True
                app.installEventFilter(self)
        except Exception:
            pass
        try:
            if not getattr(self, "_screen_watch_installed", False):
                self._screen_watch_installed = True
                handle = self.windowHandle()
                if handle is not None:
                    handle.screenChanged.connect(
                        lambda _screen: self._abort_window_tween(user=True))
        except Exception:
            pass
        # M4.6.7 startup entrance: deferred to the next event-loop turn
        # so the window is genuinely visible (first compositor frame
        # laid out) BEFORE the animation runs. Starting it inside
        # showEvent raced first paint and read as "no animation".
        # One-shot, non-blocking; readiness never waits on animation.
        if not getattr(self, "_startup_played", False):
            self._startup_played = True
            QTimer.singleShot(0, self._play_startup_entrance)

    def _play_startup_entrance(self) -> None:
        """Start the entrance only once actually visible (M4.6.8).

        The ACTUAL top-level window fades in (~220ms); the content
        entrance runs alongside (never alone, never gating input).
        """
        try:
            if not self.isVisible():
                return
            fx.window_fade(self, 1.0, 220, self,
                            finished=self._finish_startup_entrance)
            fx.startup_entrance(self._stack, 220, self)
        except Exception:
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass

    def _finish_startup_entrance(self) -> None:
        """Pin full opacity (animator rounding must never stick)."""
        try:
            self.setWindowOpacity(1.0)
        except Exception:
            pass

    # -- modes ---------------------------------------------------------------------

    def _enter_library_mode(self) -> None:
        """Return to the Library with a reverse directional transition."""
        fx.slide_fade_stack_to(self._stack, 0, 200, self, dx_px=-8)
        # Settings belong to the Library: restore the FAB, keep state.
        try:
            self._settings_fab.setVisible(True)
        except Exception:
            pass
        try:
            self._tab_strip.set_home_active(True)
        except Exception:
            pass
        # Fullscreen never belongs to one page: the shared title row is
        # persistent on the Library (tabs + window controls stay
        # reachable) instead of the workspace's hover-revealed chrome.
        if self._strip_fullscreen:
            self._show_fullscreen_titlebar()

    def _enter_workspace_mode(self) -> None:
        """Enter the immersive workspace (no Library chrome, no settings).

        The page switches instantly — rendering never waits — then the
        workspace settles in with a small directional shift.
        """
        fx.slide_fade_stack_to(self._stack, 1, 200, self, dx_px=8)
        for overlay in ("_settings_modal", "_new_modal", "_blank_modal"):
            try:
                getattr(self, overlay).close_modal(animate=False)
            except Exception:
                pass
        try:
            self._settings_fab.setVisible(False)
        except Exception:
            pass
        try:
            self._tab_strip.set_home_active(False)
        except Exception:
            pass
        # Entering the PDF workspace in fullscreen is immersive: the
        # shared row hides and the top-edge hover reveals it.
        if self._strip_fullscreen:
            self._hide_fullscreen_titlebar()
        self._refresh_shortcuts()

    def show_library(self) -> None:
        """Return to the Library (keeps every document tab intact)."""
        # M4.6.7: leaving cancels any pending deferred open — a stale
        # finish must never swap a document in after the user went back.
        try:
            self._open_epoch = getattr(self, "_open_epoch", 0) + 1
        except Exception:
            pass
        self._pending_open_fallback = None
        # Leaving for Home also cancels any pending preparation: a late
        # commit must never yank the window back into the workspace.
        self._cancel_pending_switch()
        try:
            self._viewer.exit_loading()
        except Exception:
            pass
        self._titlebar.set_context("")
        self._enter_library_mode()
        self._library_page.refresh()
        self._refresh_shortcuts()

    # -- document tabs (Phase 2) ---------------------------------------------

    def _session_for_doc(self, doc) -> _TabSession | None:
        """The open tab that owns ``doc`` (identity match), or ``None``."""
        if doc is None:
            return None
        for session in self._tab_sessions:
            if session.doc is doc:
                return session
        return None

    def _tab_title_for(self, doc) -> str:
        """Display title for a tab (library title > clean file name).

        A tracked file shows the Library's own display title (the same
        ``display_title`` the Library rows paint — the ``items`` schema
        keys ``normalized_path`` UNIQUE, so the path resolves at most
        one entry and the rule is deterministic). An untracked file
        falls back to its filename with only the final ``.pdf`` suffix
        removed. A pathless or still-blank document (synthetic
        ``inkfall-blank:`` registry key, never a file) keeps the
        metadata/``Untitled`` behavior. Resolved live from the current
        library state on every call: nothing is cached, so renames,
        refreshes and read-only copies can never go stale or leak a
        title across tabs.
        """
        path = getattr(doc, "normalized_path", None)
        if path and not str(path).startswith(BLANK_KEY_PREFIX):
            path = str(path)
            try:
                item = self._library.item_for_path(path)
            except Exception:
                item = None
            if item is not None:
                title = item.display_title
                if title:
                    return title
            return strip_pdf_suffix(Path(path).name) or "Untitled"
        try:
            title = doc.info().title or ""
        except Exception:
            title = ""
        return title or "Untitled"

    def _register_session(self, doc, *, active: bool = False) -> _TabSession:
        """Create a tab record + strip entry for a managed document."""
        session = _TabSession(doc, uuid4().hex)
        self._tab_sessions.append(session)
        try:
            self._tab_strip.add_tab(
                session.tab_id, self._tab_title_for(doc),
                dirty=bool(getattr(doc, "is_dirty", False)),
                active=active)
        except Exception:
            pass
        self._publish_instance_docs()
        return session

    def _drop_session(self, session: _TabSession) -> None:
        """Remove one tab record + strip entry (never closes the document)."""
        if session is None:
            return
        # A pending switch INTO this tab dissolves with it: the stale
        # completion is invalidated (epoch) and the still-attached
        # previous document returns from behind the loading veil.
        if (self._pending_switch is not None
                and self._pending_switch.session is session):
            self._cancel_pending_switch()
            try:
                self._viewer.exit_loading()
            except Exception:
                pass
        try:
            self._tab_sessions.remove(session)
        except ValueError:
            pass
        try:
            self._tab_strip.remove_tab(session.tab_id)
        except Exception:
            pass
        if self._active_session is session:
            self._active_session = None
        if self._pending_open_fallback is session:
            self._pending_open_fallback = None
        self._publish_instance_docs()

    def _sync_tab_titles(self) -> None:
        """Push titles and dirty dots for every tab (in place, no rebuild)."""
        for session in list(self._tab_sessions):
            try:
                title = self._tab_title_for(session.doc)
                if getattr(session.doc, "read_only", False):
                    title = f"{title} (read-only)"
                self._tab_strip.update_tab(
                    session.tab_id,
                    title=title,
                    dirty=bool(session.doc.is_dirty))
            except Exception:
                pass

    def _activate_session(self, session: _TabSession, *,
                          focus: bool = True,
                          enter_workspace: bool = True,
                          failure_mode: str = "keep") -> bool:
        """Request ``session`` as the live viewer session (non-blocking).

        The request answers IMMEDIATELY: the strip highlight and title
        move now, the viewer shows its loading veil, and the destination
        pipeline is prepared on a background pool. When preparation lands
        (GUI-thread completion) the viewer commits the prepared session
        and restores view state. Only ONE live session/attached pipeline
        exists at any time; the outgoing one stays attached (behind the
        veil) until the commit, so a failure or supersede restores it
        without any re-preparation.

        Returns True once the request is ACCEPTED (document identity
        valid and dispatch started) — not "content is showing"; callers
        must not treat it as synchronous success. A later preparation
        failure is recovered by the completion path (toast + per-mode
        fallback), never by blocking here.
        """
        if session is None or session not in self._tab_sessions:
            return False
        doc = session.doc
        try:
            if doc is None or doc.is_closed:
                return False
        except Exception:
            return False

        # Latest request wins: a superseded preparation retires its
        # pipeline in background when the stale completion lands.
        self._cancel_pending_switch()
        epoch = self._switch_epoch = getattr(self, "_switch_epoch", 0) + 1

        # That document is still attached (it is either the live session
        # or the veiled outgoing one of a pending switch): restore it
        # directly — no preparation, no pipeline rebuild.
        try:
            attached_live = (self._viewer.current_document is doc
                             and self._viewer.has_document
                             and not doc.is_closed)
        except Exception:
            attached_live = False
        if attached_live:
            # Already-attached selection (clicking the live tab, a
            # duplicate open, or RETURNING FROM HOME): the pipeline is
            # valid — no preparation, no rebuild. Session identity is
            # NOT proof the workspace is visible: show_library() keeps
            # the document attached while the Library page is on the
            # stack, so the workspace entry happens HERE, before the
            # early return.
            self._cancel_pending_switch()
            self._viewer.exit_loading()
            self._active_session = session
            try:
                self._tab_strip.activate_tab(session.tab_id)
            except Exception:
                pass
            if enter_workspace and not self.in_workspace:
                self._enter_workspace_mode()
            self._sync_workspace_title()
            if focus:
                try:
                    self._viewer.setFocus()
                except Exception:
                    pass
            self._refresh_shortcuts()
            return True

        # Immediate feedback: the requested tab wins the highlight and
        # the title NOW; its content follows when preparation lands.
        pending_now = self._pending_switch
        if pending_now is not None and pending_now.session is session:
            # This very request is already in flight: keep it (a newer
            # epoch would only duplicate the same preparation) but honor
            # an explicit request to enter the workspace now.
            if enter_workspace and not self.in_workspace:
                self._enter_workspace_mode()
            if focus:
                try:
                    self._viewer.setFocus()
                except Exception:
                    pass
            return True
        try:
            self._tab_strip.activate_tab(session.tab_id)
        except Exception:
            pass
        self._show_pending_title(doc)
        if enter_workspace and not self.in_workspace:
            self._enter_workspace_mode()
        try:
            self._viewer.enter_loading(self._tab_title_for(doc))
        except Exception:
            pass
        if focus:
            try:
                self._viewer.setFocus()
            except Exception:
                pass
        self._refresh_shortcuts()

        self._pending_switch = _PendingSwitch(
            epoch, session, focus=focus, enter_workspace=enter_workspace,
            failure_mode=failure_mode)
        # Zoom resolution touches the reading-position store (a GUI-thread
        # resource); it is resolved HERE, before dispatch, and the value
        # crosses to the worker as plain data.
        try:
            zoom = self._viewer.resolve_session_zoom(doc, session.view_state)
        except Exception:
            zoom = 1.0
        self._switch_pool.start(_SwitchPrep(
            self._switch_hub, epoch, doc.id, doc.render_path, zoom))
        return True

    def _cancel_pending_switch(self) -> None:
        """Invalidate any pending preparation (the epoch check does the
        rest: a stale completion is never activated, only retired)."""
        self._pending_switch = None

    def _retire_pipeline_async(self, pipeline) -> None:
        """Retire a detached pipeline on the background pool."""
        if pipeline is None:
            return
        try:
            self._switch_pool.start(_PipelineClose(pipeline))
        except Exception:
            try:
                pipeline.close()
            except Exception:
                pass

    def _show_pending_title(self, doc) -> None:
        """Workspace title for the REQUESTED document (before commit)."""
        name = self._tab_title_for(doc)
        try:
            dirty = bool(doc.is_dirty)
        except Exception:
            dirty = False
        display = f"{name} •" if dirty else name
        try:
            self._viewer_bar.set_title(display)
            self._titlebar.set_context(f"/  {display}")
        except Exception:
            pass

    def _on_switch_prepared(self, epoch: int, result: object) -> None:
        """Background preparation landed (queued onto the GUI thread)."""
        data = result if isinstance(result, dict) else {}
        pipeline = data.get("pipeline")
        pending = self._pending_switch
        if pending is None or pending.epoch != int(epoch):
            # Superseded/invalidated: never activate, never overwrite the
            # newer view; release what the stale worker prepared.
            self._retire_pipeline_async(pipeline)
            return
        self._pending_switch = None
        session = pending.session
        if session not in self._tab_sessions:
            # The tab was closed while preparing: nothing to attach.
            self._retire_pipeline_async(pipeline)
            self._viewer.exit_loading()
            self._after_switch_chrome()
            return
        doc = session.doc
        try:
            doc_closed = bool(doc.is_closed)
        except Exception:
            doc_closed = True
        if doc_closed:
            self._retire_pipeline_async(pipeline)
            self._viewer.exit_loading()
            self._switch_failed(pending, "The document is no longer open.")
            return
        if not data.get("ok"):
            self._viewer.exit_loading()
            self._switch_failed(
                pending, str(data.get("error") or "Cannot open document."))
            return
        previous = self._active_session
        ok, outgoing = self._viewer.commit_prepared_session(
            doc, pipeline, session.view_state, data.get("zoom"))
        self._viewer.exit_loading()
        if not ok:
            # Commit refused (validated before the outgoing session was
            # touched): the still-attached session is untouched.
            self._switch_failed(pending, "Cannot open document.")
            return
        if previous is not None and previous is not session:
            previous.view_state = outgoing
        self._pending_open_fallback = None
        self._active_session = session
        try:
            self._tab_strip.activate_tab(session.tab_id)
        except Exception:
            pass
        if pending.enter_workspace and not self.in_workspace:
            self._enter_workspace_mode()
        self._sync_workspace_title()
        self._sync_tab_titles()
        if pending.focus:
            try:
                self._viewer.setFocus()
            except Exception:
                pass
        self._refresh_shortcuts()

    def _reattach_existing_session(self, session: _TabSession) -> bool:
        """Restore a session whose pipeline is STILL attached (no prep).

        Used by failure/supersede recovery: the outgoing session was
        never detached behind the loading veil, so restoring it is a
        chrome-only operation.
        """
        try:
            doc = session.doc
            if (doc is None or doc.is_closed
                    or self._viewer.current_document is not doc
                    or not self._viewer.has_document):
                return False
        except Exception:
            return False
        self._viewer.exit_loading()
        self._active_session = session
        try:
            self._tab_strip.activate_tab(session.tab_id)
        except Exception:
            pass
        self._sync_workspace_title()
        self._refresh_shortcuts()
        return True

    def _switch_failed(self, pending: _PendingSwitch, error: str) -> None:
        """Recover from a failed preparation (GUI thread, veil cleared)."""
        session = pending.session
        title = "document"
        try:
            title = self._tab_title_for(session.doc)
        except Exception:
            pass
        self._toast.show_message(
            "Cannot open document", f"{title}: {error}", kind="error")
        mode = pending.failure_mode
        if mode == "drop":
            # A tab created FOR this open: remove it and fall back.
            self._drop_session(session)
            previous = self._pending_open_fallback
            if previous is not None and previous in self._tab_sessions \
                    and self._reattach_existing_session(previous):
                self._pending_open_fallback = None
                return
            self._restore_after_open_failure()
            return
        if mode == "restore":
            self._restore_after_open_failure()
            return
        # "keep": an established tab was requested. The previously
        # attached session (if any) is still live behind the veil.
        active = self._active_session
        if (active is not None and active is not session
                and active in self._tab_sessions
                and self._reattach_existing_session(active)):
            return
        # Nothing attached to restore: detach the dead remnant, try the
        # remaining tabs (staying out of the workspace — the failure may
        # have happened at Home), else Home.
        try:
            self._viewer.close_document()
        except Exception:
            pass
        for candidate in list(self._tab_sessions):
            if candidate is not session \
                    and self._reattach_existing_session(candidate):
                return
        for candidate in list(self._tab_sessions):
            if candidate is not session:
                self._activate_session(candidate, focus=False,
                                       enter_workspace=False)
                return
        self.show_library()

    def _after_switch_chrome(self) -> None:
        """Re-align chrome after a pending switch dissolved unused."""
        active = self._active_session
        if (active is not None and active in self._tab_sessions
                and self._reattach_existing_session(active)):
            return
        if self._tab_sessions:
            for candidate in list(self._tab_sessions):
                if self._reattach_existing_session(candidate):
                    return
            self._activate_session(self._tab_sessions[0], focus=False,
                                   enter_workspace=False)
            return
        self.show_library()

    def _cancel_pending_open(self) -> None:
        """User took over (tab click / cycle): a pending open must not land."""
        try:
            self._open_epoch = getattr(self, "_open_epoch", 0) + 1
        except Exception:
            pass
        self._pending_open_fallback = None

    def _activate_tab_by_id(self, tab_id: str) -> None:
        for session in list(self._tab_sessions):
            if session.tab_id == str(tab_id):
                self._cancel_pending_open()
                self._activate_session(session)
                return

    def _close_tab_by_id(self, tab_id: str) -> None:
        for session in list(self._tab_sessions):
            if session.tab_id == str(tab_id):
                self._close_session(session)
                return

    def _close_active_tab(self) -> None:
        """Ctrl+W / registry entry: guarded close of the active tab."""
        session = self._active_session
        if session is None:
            return
        self._close_session(session)

    def _cycle_tab(self, direction: int) -> None:
        """Ctrl+Tab / Ctrl+Shift+Tab: cycle document tabs (Home excluded)."""
        sessions = list(self._tab_sessions)
        if len(sessions) < 2:
            return
        self._cancel_pending_open()
        try:
            index = sessions.index(self._active_session)
        except ValueError:
            index = -1
        if index < 0:
            target = sessions[0] if direction >= 0 else sessions[-1]
        else:
            target = sessions[(index + int(direction)) % len(sessions)]
        self._activate_session(target)

    def _reorder_tab_sessions(self, order) -> None:
        """Apply a committed strip drag order (tab ids stay stable)."""
        try:
            by_id = {session.tab_id: session
                     for session in self._tab_sessions}
            ordered = [by_id[str(tab_id)] for tab_id in order
                       if str(tab_id) in by_id]
        except Exception:
            return
        if not ordered:
            return
        ordered.extend(session for session in self._tab_sessions
                       if session not in ordered)
        self._tab_sessions = ordered

    def _next_tab_neighbour(self, session: _TabSession):
        """Right neighbour, else the left one, else ``None`` (tab close)."""
        sessions = list(self._tab_sessions)
        try:
            index = sessions.index(session)
        except ValueError:
            return None
        for candidate in (index + 1, index - 1):
            if 0 <= candidate < len(sessions):
                return sessions[candidate]
        return None

    def _close_session(self, session: _TabSession) -> bool:
        """Close one tab: guard, close its document, pick a neighbour.

        Switching is never a prompt and never closes a document; only this
        explicit path owns a document's teardown (and only for that tab's
        document — other tabs, histories and pipelines are untouched).
        """
        if session is None or session not in self._tab_sessions:
            return False
        if not self._confirm_close_session(session):
            return False
        neighbour = self._next_tab_neighbour(session)
        was_active = session is self._active_session
        was_home = not self.in_workspace
        doc = session.doc
        self._drop_session(session)

        switched = False
        if was_active:
            # Non-blocking succession: the best candidate is REQUESTED
            # now (its preparation runs in background behind the loading
            # veil); if that preparation later fails, the completion path
            # falls back to the remaining tabs or Home. A dispatch
            # refusal (unusable document) tries the next candidate.
            candidates = []
            if neighbour is not None:
                candidates.append(neighbour)
            for candidate in list(self._tab_sessions):
                if candidate not in candidates:
                    candidates.append(candidate)
            for candidate in candidates:
                if self._activate_session(candidate, focus=False,
                                          enter_workspace=not was_home):
                    switched = True
                    break
            if not switched:
                # No usable successor at all: detach the live session
                # through the viewer, which also closes the managed
                # document (the closed tab was its only owner).
                try:
                    self._viewer.close_document()
                except Exception:
                    pass
        if not was_active or switched:
            try:
                self._manager.close(doc.id)
            except Exception:
                pass
        # The tab was the document's last owner: its undo history must not
        # outlive the document (the live path already dropped it above).
        try:
            self._viewer.forget_document(doc)
        except Exception:
            pass
        if not self._tab_sessions:
            self.show_library()
        elif was_active and not switched:
            # The viewer is empty while other tabs remain: Home is the
            # honest state; the remaining tabs stay clickable/cyclable.
            self.show_library()
        else:
            self._sync_workspace_title()
            self._sync_tab_titles()
        self._refresh_shortcuts()
        return True

    def close_all_tabs(self) -> None:
        """Overflow-menu Close All (public alias of the guarded flow)."""
        self._close_all_tabs_guarded()

    def _close_all_tabs_guarded(self) -> None:
        """Close every tab and return Home, behind the unsaved-work gate.

        The application-wide guard
        (:meth:`_confirm_leave_unsaved` with ``for_exit=True``) considers
        EVERY managed dirty document — unsaved blanks included — with
        Save All / Save / Discard / Cancel. Nothing is closed before the
        guard passes: Cancel or a failed required save (a picker
        declined, a locked file, a blocked manifest) leaves every tab,
        dirty flag and session exactly as it was; earlier documents that
        DID save simply stay saved. Teardown then runs through the
        established lifecycle only: pending switches are invalidated,
        the viewer releases its attached pipeline, and every tab's
        document goes through ``DocumentManager.close`` — no widget is
        destroyed manually and no document bypasses the manager.
        """
        if not self._confirm_leave_unsaved(
                for_exit=True, subject="Close every open document?"):
            return
        self._cancel_pending_switch()
        try:
            self._viewer.exit_loading()
        except Exception:
            pass
        try:
            self._viewer.close_document()
        except Exception:
            pass
        self._close_all_tabs()
        self.show_library()

    def _close_all_tabs(self) -> None:
        """Application shutdown: close every tab's owned document."""
        sessions = list(self._tab_sessions)
        self._tab_sessions = []
        self._active_session = None
        self._pending_open_fallback = None
        self._cancel_pending_switch()
        for session in sessions:
            try:
                self._manager.close(session.doc.id)
            except Exception:
                pass
            try:
                self._viewer.forget_document(session.doc)
            except Exception:
                pass
        try:
            self._tab_strip.clear_tabs()
        except Exception:
            pass

    # -- document flow (Library -> manager -> tab session) -------------------

    def open_library_item(self, item_id: str) -> bool:
        """Begin opening a library item into its own tab (non-blocking).

        The workspace transition starts IMMEDIATELY on click (titles,
        chrome, page switch, focus); document preparation plus the
        validated viewer switch run deferred on the next event-loop turn.
        Returns True once the open is accepted. Callers must not assume the
        switch (or even the tab) completed synchronously.
        """
        item = self._library.get_item(item_id)
        if item is None:
            self._toast.show_message("Library entry is gone")
            return False
        if item.missing:
            self._toast.show_message("Missing file", item.display_name,
                                     kind="error")
            return False
        epoch = getattr(self, "_open_epoch", 0) + 1
        self._open_epoch = epoch
        self._pending_open_fallback = self._active_session
        self._viewer_bar.set_title(item.display_name)
        self._titlebar.set_context(f"/  {item.display_name}")
        self._enter_workspace_mode()
        self._viewer.setFocus()
        QTimer.singleShot(0, lambda: self._finish_open(item_id, epoch))
        return True

    def _finish_open(self, item_id: str, epoch: int) -> None:
        """Deferred preparation of a library open (validated before swap)."""
        try:
            if epoch != getattr(self, "_open_epoch", epoch):
                return  # superseded by a newer open; the newer one wins
            item = self._library.get_item(item_id)
            if item is None or item.missing:
                if item is not None:
                    self._toast.show_message(
                        "Missing file", item.display_name, kind="error")
                else:
                    self._toast.show_message("Library entry is gone")
                self._restore_after_open_failure()
                return
            try:
                doc = self._manager.open(item.normalized_path)
            except InkfallError as exc:
                self._toast.show_message(
                    "Cannot open document", str(exc), kind="error")
                self._restore_after_open_failure()
                return
            except Exception as exc:
                self._toast.show_message(
                    "Cannot open document", str(exc), kind="error")
                self._restore_after_open_failure()
                return
            if not self._open_document_tab(doc):
                self._restore_after_open_failure()
                return
            # The fallback record now belongs to the in-flight switch: it
            # is consumed on completion (success) or by the failure path,
            # so a preparation failure can still restore the previous tab.
            # Post-paint bookkeeping only (never before first pixels).
            try:
                self._library.touch_opened(
                    item_id, page_count=doc.page_count)
            except Exception:
                pass
            self._library_page.refresh()
        except Exception:
            pass

    def _finish_open_path(self, path: str, epoch: int) -> None:
        """Deferred open by path (library import unavailable)."""
        try:
            if epoch != getattr(self, "_open_epoch", epoch):
                return
            try:
                doc = self._manager.open(path)
            except Exception as exc:
                self._toast.show_message(
                    "Cannot open document", str(exc), kind="error")
                self._restore_after_open_failure()
                return
            if not self._open_document_tab(doc):
                self._restore_after_open_failure()
            # The fallback record stays for the in-flight switch (see
            # _finish_open); the completion/failure paths consume it.
        except Exception:
            pass

    def _open_document_tab(self, doc) -> bool:
        """Activate the tab owning ``doc``, or create one and switch to it.

        Duplicate opens are handled here: ``DocumentManager.open`` returns
        the existing managed document for the same file, identity lookup
        finds its tab, and the Book-is-Already-Open dialog offers an
        explicit choice — continue in the existing (editable) session, or
        open a separate read-only view — instead of silently activating
        or duplicating it. Read-only duplicates created this way carry
        their own Document object, so their session can never collide
        with the primary tab.
        """
        existing = self._session_for_doc(doc)
        if existing is not None:
            if getattr(doc, "read_only", False):
                # A read-only duplicate re-invoked: it owns no editable
                # session to activate, so just re-open another copy.
                return self._open_readonly_tab(doc.normalized_path)
            return self._offer_duplicate_open(existing, doc)
        session = self._register_session(doc)
        # "drop": this tab exists FOR this open; a preparation failure
        # removes it and falls back (never leaves an empty stranded tab).
        if self._activate_session(session, failure_mode="drop"):
            return True
        # Dispatch itself failed (document already unusable): drop quietly.
        self._drop_session(session)
        try:
            self._manager.close(doc.id)
        except Exception:
            pass
        return False

    def _offer_duplicate_open(self, session: _TabSession, doc) -> bool:
        """Show the Book-is-Already-Open choice for one open session.

        Deterministic resolution when more than one session shows the
        same source (possible only via earlier read-only copies): the
        FIRST tab in strip order wins — never whichever object an
        identity lookup happened to return first.
        """
        target = session
        try:
            key = doc.normalized_path
            for candidate in self._tab_sessions:
                if (candidate.doc is not doc
                        and not getattr(candidate.doc, "read_only", False)
                        and candidate.doc.normalized_path == key):
                    target = candidate
                    break
        except Exception:
            pass
        self._duplicate_target_session = target
        self._duplicate_target_path = doc.normalized_path
        try:
            self._duplicate_modal.open_duplicate(
                "Book is Already Open",
                f"“{self._tab_title_for(target.doc)}” is already open in"
                " Inkfall. Continue in the existing session, or open a"
                " separate read-only copy for side-by-side reading?")
        except Exception:
            # The dialog must never block the open itself: fall back to
            # the established silent-activate behavior.
            return self._activate_session(target)
        return True

    def _on_duplicate_continue(self) -> None:
        """Continue in Current Session: focus the existing tab untouched."""
        session = getattr(self, "_duplicate_target_session", None)
        self._duplicate_target_session = None
        self._duplicate_target_path = None
        if session is not None and session in self._tab_sessions:
            self._activate_session(session)

    def _on_duplicate_readonly(self) -> None:
        """Open Read-Only Copy: a second, non-persisting session beside it."""
        path = getattr(self, "_duplicate_target_path", None)
        session = getattr(self, "_duplicate_target_session", None)
        self._duplicate_target_session = None
        self._duplicate_target_path = None
        if path:
            self._open_readonly_tab(path, after=session)

    def _open_readonly_tab(self, path: str, after: _TabSession | None = None) -> bool:
        """Create the read-only duplicate session's tab (never editable).

        ``after`` seats the new tab visually beside the session that
        triggered it when the strip supports insertion; otherwise it
        appends (the established tab model owns final ordering).
        """
        try:
            doc = self._manager.open_readonly(path)
        except Exception as exc:
            self._toast.show_message(
                "Cannot open read-only copy", str(exc), kind="error")
            return False
        session = self._register_session(doc)
        try:
            self._tab_strip.update_tab(
                session.tab_id,
                title=f"{self._tab_title_for(doc)} (read-only)")
        except Exception:
            pass
        _ = after  # strip ordering follows the existing tab model
        if self._activate_session(session, failure_mode="drop"):
            return True
        self._drop_session(session)
        try:
            self._manager.close(doc.id)
        except Exception:
            pass
        return False

    def _restore_after_open_failure(self) -> None:
        """A failed open leaves the previous tab (or Home) fully usable."""
        previous = self._pending_open_fallback
        self._pending_open_fallback = None
        if previous is not None and previous in self._tab_sessions:
            if self._activate_session(previous):
                self._sync_workspace_title()
                return
        if self._tab_sessions:
            # Deferred failure with live tabs but no fallback record.
            if self._activate_session(self._tab_sessions[-1]):
                return
        self.show_library()

    def close_document(self) -> None:
        """Explicitly close the ACTIVE tab and return to Home.

        Unlike opening/switching, this is an explicit discard point, so a
        dirty document gets the per-tab guard first. Cancel leaves the
        workspace exactly as it was.
        """
        session = self._active_session
        if session is None:
            try:
                self._viewer.close_document()
            finally:
                self.show_library()
            return
        self._close_session(session)

    # -- cross-instance same-file coordination -------------------------------

    _INSTANCE_RELEASE_TIMEOUT_S = 60.0

    def _publish_instance_docs(self) -> None:
        """Advertise this window's open documents to other InkFall windows.

        The registry copy is a discovery aid only; a peer deciding how to
        answer a release request always re-reads the LIVE dirty state of
        its own documents, so staleness here can never discard edits.
        """
        hub = getattr(self, "_instance_hub", None)
        if hub is None:
            return
        docs: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for session in self._tab_sessions:
            doc = session.doc
            try:
                key = doc.normalized_path
                if key in seen:
                    continue
                seen.add(key)
                docs.append((key, bool(doc.is_dirty)))
            except Exception:
                continue
        try:
            hub.publish(docs)
        except Exception:
            pass

    def _on_instance_release_request(self, request_id: str, path: str) -> None:
        """Another window asks this one to release ``path`` for a save.

        A clean document — read-only duplicates included — closes
        silently through the ordinary teardown. A dirty editable
        document gets the explicit Save / Discard / Cancel guard; a
        cancelled prompt answers ``refused`` and preserves every edit.
        The peer's own save that triggered this request must not
        re-coordinate (it would ask us while we wait for it), so the
        nested save path sets ``_instance_release_saving``.
        """
        hub = getattr(self, "_instance_hub", None)
        if hub is None:
            return
        self._instance_release_saving = True
        try:
            status = "released" if self._release_documents_for_path(
                path) else "refused"
        except Exception:
            status = "refused"
        finally:
            self._instance_release_saving = False
        try:
            hub.respond(request_id, status)
        except Exception:
            pass
        self._publish_instance_docs()

    def _release_documents_for_path(self, path: str) -> bool:
        """Close every session of ``path`` in THIS window.

        Only matching canonical paths are touched, never unrelated
        documents. Closing runs through the guarded per-tab path, so a
        cancelled unsaved-changes prompt refuses the whole release (the
        requesting window keeps its file and saves elsewhere).
        """
        try:
            key = normalize_key(path)
        except Exception:
            return False
        released = True
        for session in list(self._tab_sessions):
            doc = session.doc
            try:
                if normalize_key(doc.normalized_path) != key:
                    continue
            except Exception:
                continue
            if not self._close_session(session):
                released = False
        return released

    def _coordinate_save_start(self, target: str) -> None:
        """Release same-file holders before a save needs exclusive access.

        Closes THIS window's read-only duplicates of the target (their
        render handles keep the file open on Windows) and asks every
        other live InkFall window editing the same canonical file to
        release it, with a bounded wait. Refusal or a timeout raises
        :class:`SaveLockedError`, which the ordinary save-failure
        recovery (toast + Save-As picker) handles. Coordination runs
        before any temp file exists, so a failure leaves the save
        transaction untouched. The atomic replace itself remains the
        final arbiter against a window that opens the file afterwards.
        """
        if getattr(self, "_instance_release_saving", False):
            # This save IS the peer's release in progress; re-coordinating
            # would deadlock the two windows against each other.
            return
        self._close_readonly_duplicates_for_target(target)
        hub = getattr(self, "_instance_hub", None)
        if hub is None:
            return
        try:
            key = normalize_key(target)
        except Exception:
            return
        try:
            peers = [
                record for record in scan_instances(hub.directory)
                if record.server != hub.server_name
            ]
        except Exception:
            return
        for record in peers:
            if not any(doc_path == key for doc_path, _dirty in record.docs):
                continue
            try:
                if self._save_dialog is not None:
                    self._save_dialog.setLabelText(
                        "Asking other Inkfall windows to release this"
                        " file…")
            except Exception:
                pass
            status = hub.request_release(
                record.server, key,
                timeout_s=self._INSTANCE_RELEASE_TIMEOUT_S)
            if status != "released":
                raise SaveLockedError(
                    "Another Inkfall window is editing this file and did"
                    " not release it. Close the document there, or save"
                    " to a different file.")

    def _close_readonly_duplicates_for_target(self, target: str) -> None:
        """Close this window's read-only duplicates of ``target``.

        A read-only duplicate holds its own backend handle and render
        working copies for the same file, which would surface as a
        destination-in-use error on Windows. It can never hold unsaved
        edits (its store is frozen), so the guarded close is silent.
        """
        try:
            key = normalize_key(target)
        except Exception:
            return
        for session in list(self._tab_sessions):
            doc = session.doc
            try:
                if not getattr(doc, "read_only", False):
                    continue
                if normalize_key(doc.normalized_path) != key:
                    continue
            except Exception:
                continue
            self._close_session(session)

    # -- custom chrome: maximize/restore, fullscreen, active state ----------

    def _window_target_rect(self, fullscreen: bool):
        """Screen rect for geometry tweens (None when unavailable).

        Maximized keeps the taskbar (availableGeometry); true
        fullscreen covers it (geometry).
        """
        try:
            screen = self.screen()
            if screen is None:
                from PySide6.QtWidgets import QApplication

                screen = QApplication.primaryScreen()
            if screen is None:
                return None
            return screen.geometry() if fullscreen else screen.availableGeometry()
        except Exception:
            return None

    def _tween_window_geometry(self, to_rect, ms: int, done) -> None:
        """Tween the real window geometry (epoch-guarded, preemptive)."""
        self._geom_epoch = getattr(self, "_geom_epoch", 0) + 1
        epoch = self._geom_epoch
        try:
            if self._geom_anim is not None:
                self._geom_anim.stop()
        except Exception:
            pass
        self._geom_anim = None
        self._tweening_geom = True
        try:
            self._tween_start_state = self.windowState()
        except Exception:
            self._tween_start_state = None

        def _done() -> None:
            try:
                self._tweening_geom = False
                self._tween_start_state = None
            except Exception:
                pass
            if epoch != getattr(self, "_geom_epoch", epoch):
                return
            try:
                done()
            except Exception:
                pass

        try:
            self._geom_anim = fx.window_tween_geometry(self, to_rect, ms, self,
                                                       finished=_done)
        except Exception:
            _done()

    def _abort_window_tween(self, user: bool = False) -> None:
        """Stop any geometry tween (user/screen/state wins, M4.6.9).

        ``user=True`` (press/drag/resize/screen) additionally releases
        restore ownership: the user's live geometry becomes truth again.
        State notifications use ``user=False`` (tween dies, an active
        restore finish keeps the seam).
        """
        try:
            self._geom_epoch = getattr(self, "_geom_epoch", 0) + 1
        except Exception:
            pass
        try:
            if self._geom_anim is not None:
                self._geom_anim.stop()
        except Exception:
            pass
        try:
            self._geom_anim = None
            self._tweening_geom = False
            self._tween_start_state = None
            if user:
                self._restoring_geom = False
        except Exception:
            pass

    def toggle_maximize_restore(self) -> None:
        """Maximize or restore with the native state flipping FIRST.

        The native transition (showMaximized/showNormal) runs
        synchronously inside the click: there is no tween/fade seam
        where the window looks maximized while still logically Normal
        (or vice versa), so every click observes truth and performs
        exactly one transition. Visual settle (fades) runs strictly
        AFTER the authoritative flip and never gates state. From
        fullscreen, exits to the recorded pre-fullscreen state.
        Snap/DPI/multi-monitor and the saved rect are preserved.
        """
        try:
            if self.isFullScreen():
                # Leave fullscreen via the state-aware exit (restores
                # maximized-vs-normal explicitly, plus chrome).
                self._exit_fullscreen()
                return
            if self.isMinimized():
                self.showNormal()
                return
            if self.isMaximized():
                self._restore_now()
                return
            self._maximize_now()
        except Exception:
            pass

    def _maximize_now(self) -> None:
        """Authoritative instant maximize (native state first)."""
        try:
            self._abort_window_tween(user=False)
        except Exception:
            pass
        self._save_normal_geom_once()
        try:
            if not self.isMaximized():
                self.showMaximized()
        except Exception:
            pass
        try:
            self._titlebar.set_maximized(True)
        except Exception:
            pass
        try:
            fx.fade_in(self._stack, 150, self)
        except Exception:
            pass

    def _enforce_saved_normal_geom(self) -> None:
        """Set the exact saved rect once genuinely Normal (M4.6.9).

        Deferred past native state crossings: native code owns the
        seam, this owns the final rect. Retries (bounded) while the
        platform has not settled yet, then verifies once applied.
        """
        try:
            from PySide6.QtCore import QRect, QTimer

            tries = int(getattr(self, "_enforce_tries", 0))
            if self.isMaximized() or self.isFullScreen() or self.isMinimized():
                if tries < 12:
                    self._enforce_tries = tries + 1
                    QTimer.singleShot(60, self._enforce_saved_normal_geom)
                else:
                    self._enforce_tries = 0
                    self._restoring_geom = False
                return
            if getattr(self, "_tweening_geom", False):
                # A newer geometry tween owns the frames right now
                # (e.g. a fullscreen entry started mid-settle): do not
                # yank geometry out from under it and do not burn
                # retries — defer until the tween releases authority
                # (bounded: a wedged tween must not pin restore open).
                defers = int(getattr(self, "_enforce_defers", 0))
                if defers < 12:
                    self._enforce_defers = defers + 1
                    QTimer.singleShot(60, self._enforce_saved_normal_geom)
                else:
                    self._enforce_defers = 0
                    self._enforce_tries = 0
                    self._restoring_geom = False
                return
            try:
                self._enforce_defers = 0
            except Exception:
                pass
            saved = getattr(self, "_normal_geom", None)
            if saved is None:
                self._enforce_tries = 0
                self._restoring_geom = False
                return
            want = QRect(saved)
            if self.geometry().getRect() != want.getRect():
                self.setGeometry(want)
                if self.geometry().getRect() != want.getRect() and tries < 12:
                    self._enforce_tries = tries + 1
                    QTimer.singleShot(60, self._enforce_saved_normal_geom)
                    return
            self._enforce_tries = 0
            self._restoring_geom = False
        except Exception:
            pass

    def _restore_now(self) -> None:
        """Authoritative instant restore (native state first).

        showNormal runs synchronously inside the click; the exact
        saved rect is enforced after the crossing settles (never WM
        memory). The dip/settle fades are pure visuals AFTER the flip.
        Aborts any in-flight geometry tween first so a stale finish
        (e.g. a fullscreen-entry tween) can never re-apply afterwards.
        """
        if getattr(self, "_closing", False):
            try:
                self.showNormal()
            except Exception:
                pass
            return
        try:
            self._abort_window_tween(user=False)
        except Exception:
            pass
        try:
            # Own the seam BEFORE the state leave: showNormal delivers
            # move/resize synchronously (nested, state already normal).
            self._restoring_geom = True
            try:
                self.showNormal()
            except Exception:
                self._restoring_geom = False
        except Exception:
            pass
        # The exact saved rect is enforced after the state crossing
        # settles (next loop turn — setGeometry while still maximized
        # would be ignored).
        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self._enforce_saved_normal_geom)
        except Exception:
            pass
        try:
            self._titlebar.set_maximized(False)
        except Exception:
            pass
        # Pure visual settle AFTER the authoritative flip (masks the
        # compositor-owned un-maximize snap); never gates state.
        try:
            fx.window_fade(self, 0.7, 60, self,
                            finished=self._finish_restore_visual)
        except Exception:
            self._finish_restore_visual()

    def _finish_restore_visual(self) -> None:
        try:
            fx.window_fade(self, 1.0, 140, self)
            fx.fade_in(self._stack, 150, self)
        except Exception:
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass

    def _minimize_with_feedback(self) -> None:
        """Short top-level dip, then native minimize (M4.6.8).

        ~110ms window-level feedback where the platform permits; the
        compositor owns the flight to the taskbar. Un-minimize
        restores full opacity (see changeEvent).
        """
        if self.isMinimized():
            return
        try:
            fx.window_fade(self, 0.55, 110, self,
                            finished=self._finish_minimize)
        except Exception:
            self._finish_minimize()

    def _finish_minimize(self) -> None:
        try:
            self._stack.setGraphicsEffect(None)
        except Exception:
            pass
        try:
            if not self.isMinimized():
                self.showMinimized()
        except Exception:
            pass

    def _close_with_feedback(self) -> None:
        """Visible top-level close fade, then native close (M4.6.8).

        ~130ms window fade; shutdown ordering (thumbs/viewer/store)
        runs in closeEvent exactly as before — never blocked, never
        delayed beyond the fade. The four-choice unsaved gate runs before
        the fade so a Cancel never leaves an invisible window; the passed
        flag keeps closeEvent from prompting a second time.
        """
        if getattr(self, "_closing", False):
            return
        if not self._confirm_leave_unsaved(for_exit=True):
            return
        self._leave_guard_passed = True
        self._closing = True
        try:
            self._close_anim = fx.window_fade(
                self, 0.0, 130, self, finished=self._finish_close)
        except Exception:
            self._close_anim = None
            self._finish_close()

    def _finish_close(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # -- fullscreen ---------------------------------------------------------------

    def toggle_fullscreen(self) -> None:
        """Enter/exit fullscreen with the native state flipping FIRST.

        Enter establishes true WindowFullScreen synchronously (no
        geometry tween across the state boundary: tween frames sized
        to the screen rect can synthesize a hostile Maximized flip
        whose notification murders the tween that caused it). Content
        settle runs strictly after the authoritative flip. Exit keeps
        its dip-then-crossing shape (epoch-guarded, converges on
        re-entry). F11/Esc behavior unchanged; no PDF scaling.
        """
        if self.isFullScreen():
            self._exit_fullscreen()
            return
        for overlay in ("_settings_modal", "_new_modal"):
            try:
                getattr(self, overlay).close_modal(animate=False)
            except Exception:
                pass
        # M4.6.9: record pre-fullscreen state explicitly (normal rect
        # plus maximized-vs-normal) before any transition.
        try:
            if self.isMaximized():
                self._was_maximized_before_fs = True
            elif not self.isMinimized():
                self._was_maximized_before_fs = False
                self._save_normal_geom_once()
        except Exception:
            pass
        try:
            self._fs_epoch = getattr(self, "_fs_epoch", 0) + 1
        except Exception:
            pass
        try:
            self._abort_window_tween(user=False)
        except Exception:
            pass
        try:
            if not self.isFullScreen():
                self.showFullScreen()
        except Exception:
            pass
        self._refresh_shortcuts()
        try:
            self._viewer_bar.setVisible(False)
        except Exception:
            pass
        try:
            self._titlebar.set_fullscreen(True)
        except Exception:
            pass
        self._set_strip_fullscreen(True)
        try:
            fx.slide_fade_in(self._stack, 0, 150, self, dy_px=4)
        except Exception:
            pass

    def _exit_fullscreen(self) -> None:
        epoch = getattr(self, "_fs_epoch", 0) + 1
        self._fs_epoch = epoch
        try:
            fx.window_fade(self, 0.7, 80, self,
                            finished=lambda: self._finish_exit_fullscreen(epoch))
        except Exception:
            self._finish_exit_fullscreen(epoch)

    def _finish_exit_fullscreen(self, epoch: int) -> None:
        """Phase 2 of exiting fullscreen (cancelled when stale).

        M4.6.9: restores the recorded pre-fullscreen state explicitly —
        maximized returns to maximized, otherwise the EXACT saved
        normal rect (never WM memory, never a guess).
        """
        try:
            if epoch != getattr(self, "_fs_epoch", epoch):
                return
            was_max = bool(getattr(self, "_was_maximized_before_fs", False))
            if was_max:
                try:
                    self.showMaximized()
                except Exception:
                    pass
            else:
                try:
                    self._restoring_geom = True
                    try:
                        self.showNormal()
                    except Exception:
                        self._restoring_geom = False
                except Exception:
                    pass
                try:
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(0, self._enforce_saved_normal_geom)
                except Exception:
                    pass
            self._titlebar.setVisible(True)
            # ViewerBar stays a hidden compatibility shim (never a row).
            self._viewer_bar.setVisible(False)
            try:
                self._titlebar.set_fullscreen(False)
            except Exception:
                pass
            self._set_strip_fullscreen(False)
            try:
                self._titlebar.set_maximized(bool(self.isMaximized()))
            except Exception:
                pass
            fx.window_fade(self, 1.0, 140, self)
            fx.slide_fade_in(self._stack, 0, 150, self, dy_px=-4)
            self._refresh_shortcuts()
        except Exception:
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass

    # -- fullscreen title-row auto-hide (Phase 2) ---------------------------

    def _stop_titlebar_height_tween(self) -> None:
        """Cancel a running reveal/conceal tween before a mode-owned snap."""
        try:
            group = self._titlebar.property("_inkfall_tween_h")
            if group is not None:
                group.stop()
            self._titlebar.setProperty("_inkfall_tween_h", None)
        except Exception:
            pass

    def _show_fullscreen_titlebar(self) -> None:
        """Fullscreen on the Library: shared chrome stays persistent.

        The Library has no canvas hover to reveal a hidden row, so the
        same title bar (tab strip + window controls) remains laid out at
        its normal height while WindowFullScreen is active. No geometry
        or window-state change is involved.
        """
        self._stop_titlebar_height_tween()
        self._strip_revealed = True
        self._reveal_armed = False
        try:
            self._titlebar.setFixedHeight(TITLE_BAR_H)
            self._titlebar.setVisible(True)
            self._titlebar.raise_()
        except Exception:
            pass

    def _hide_fullscreen_titlebar(self) -> None:
        """Fullscreen in the PDF workspace: immersive, hover-revealed row."""
        self._stop_titlebar_height_tween()
        self._strip_revealed = False
        self._reveal_armed = True
        try:
            self._titlebar.setFixedHeight(0)
            self._titlebar.setVisible(False)
        except Exception:
            pass

    def _set_strip_fullscreen(self, active: bool) -> None:
        """Own the shared title row while fullscreen is active.

        The row is one control surface for every page: the PDF workspace
        hides it for immersion (top-edge hover reveals it), while the
        Library keeps it visible so tabs, minimize, maximize/restore,
        the fullscreen toggle and close stay reachable. Exiting fullscreen
        restores the row with a single settle animation.
        """
        self._strip_fullscreen = bool(active)
        if active:
            if self.in_workspace:
                self._hide_fullscreen_titlebar()
            else:
                self._show_fullscreen_titlebar()
            return
        self._strip_revealed = False
        self._reveal_armed = True
        try:
            self._titlebar.setFixedHeight(0)
            self._titlebar.setVisible(True)
            fx.tween_height(self._titlebar, TITLE_BAR_H, 150, self)
        except Exception:
            try:
                self._titlebar.setVisible(True)
                self._titlebar.setFixedHeight(TITLE_BAR_H)
            except Exception:
                pass

    def _reveal_tab_strip(self) -> None:
        """Fullscreen: reveal the integrated title row at the top edge."""
        if (not self._strip_fullscreen or self._strip_revealed
                or not self.in_workspace):
            return
        try:
            if self._viewer.gesture_live:
                return  # never resize the canvas under a live stroke
        except Exception:
            pass
        self._strip_revealed = True
        self._reveal_armed = False
        try:
            self._titlebar.setFixedHeight(0)
            self._titlebar.setVisible(True)
            self._titlebar.raise_()
            fx.tween_height(self._titlebar, TITLE_BAR_H, 150, self)
        except Exception:
            try:
                self._titlebar.setFixedHeight(TITLE_BAR_H)
            except Exception:
                pass

    def _conceal_tab_strip(self) -> None:
        """Fullscreen: hide the revealed title row after the pointer leaves."""
        if (not self._strip_fullscreen or not self._strip_revealed
                or not self.in_workspace):
            return
        # The overflow popup is a separate window: opening it leaves the
        # row, but must not pull the row out from under the menu.
        try:
            if getattr(self._tab_strip, "_menu_open", False):
                return
        except Exception:
            pass
        self._strip_revealed = False
        self._reveal_armed = False
        try:
            fx.tween_height(self._titlebar, 0, 130, self,
                            finished=self._finish_conceal_tab_strip)
        except Exception:
            self._finish_conceal_tab_strip()

    def _conceal_after_menu(self) -> None:
        """Re-check concealment after the overflow popup closes."""
        if not self._strip_fullscreen or not self._strip_revealed:
            return
        try:
            from PySide6.QtGui import QCursor

            local = self._titlebar.mapFromGlobal(QCursor.pos())
            if self._titlebar.rect().contains(local):
                return
        except Exception:
            pass
        self._conceal_tab_strip()

    def _finish_conceal_tab_strip(self) -> None:
        if not self._strip_fullscreen or self._strip_revealed:
            return
        try:
            self._titlebar.setVisible(False)
        except Exception:
            pass

    def changeEvent(self, event) -> None:  # noqa: N802
        """Track active/inactive + maximized for the custom chrome."""
        try:
            etype = event.type()
            if etype == QEvent.Type.WindowActivate:
                self._titlebar.set_active(True)
            elif etype == QEvent.Type.WindowDeactivate:
                self._titlebar.set_active(False)
            elif etype == QEvent.Type.WindowStateChange:
                # M4.6.9: any native state crossing (ours or the OS/Snap)
                # ends tween authority — the manager wins from here.
                # Stale/spurious-aware: notifications may arrive late or
                # duplicated (compositor lag, offscreen/async delivery),
                # describing a crossing that predates the running tween
                # or no departure at all. Aborting those would murder a
                # valid newer tween (e.g. a restore settle's setGeometry
                # echo killing a fullscreen-entry tween), so they are
                # ignored: only a notification that leaves the state
                # the tween started in aborts it.
                try:
                    if getattr(self, "_tweening_geom", False):
                        start_state = getattr(
                            self, "_tween_start_state", None)
                        try:
                            old = event.oldState()
                        except Exception:
                            old = None
                        try:
                            current = self.windowState()
                        except Exception:
                            current = None
                        # Abort only when the notification describes a
                        # genuine departure from the tween-start state.
                        # Stale notifications (old != start: crossing
                        # predates the tween) and spurious ones
                        # (current == start: no departure at all) are
                        # ignored; unknown shapes fall back to aborting.
                        unknown = start_state is None or old is None
                        genuinely_left = (
                            not unknown and old == start_state
                            and current != start_state)
                        if genuinely_left or unknown:
                            self._abort_window_tween()
                    else:
                        self._abort_window_tween()
                except Exception:
                    try:
                        self._abort_window_tween()
                    except Exception:
                        pass
                self._titlebar.set_maximized(bool(self.isMaximized()))
                # M4.6.8: un-minimize restores full window opacity (the
                # minimize dip / restore settle converge here; close is
                # excluded via _closing).
                try:
                    if (not self.isMinimized()
                            and not getattr(self, "_closing", False)):
                        try:
                            opacity = float(self.windowOpacity())
                        except Exception:
                            opacity = 1.0
                        if opacity < 1.0:
                            fx.window_fade(self, 1.0, 120, self)
                except Exception:
                    pass
        except Exception:
            pass
        super().changeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Esc exits fullscreen first, then returns to the Library."""
        if event.key() == Qt.Key.Key_Escape:
            if self.isFullScreen():
                self._exit_fullscreen()
                event.accept()
                return
            if self.in_workspace:
                self.show_library()
                event.accept()
                return
        super().keyPressEvent(event)

    # -- dialogs ----------------------------------------------------------------------

    def _import_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Import PDFs", "", "PDF files (*.pdf)")
        if paths:
            self._enter_library_mode()
            self._library_page.import_paths(paths)

    def _open_dialog(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF files (*.pdf)")
        if not path:
            return
        # Direct open also registers in the library (recency without import
        # friction). Opening a PDF never prompts for unsaved edits: it
        # creates/activates that document's tab; the close/exit guards own
        # every discard decision.
        try:
            item = self._library.import_path(path)
        except Exception:
            item = None
        if item is not None:
            self.open_library_item(item.id)
            return
        display_name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        epoch = getattr(self, "_open_epoch", 0) + 1
        self._open_epoch = epoch
        self._pending_open_fallback = self._active_session
        self._viewer_bar.set_title(display_name)
        self._titlebar.set_context(f"/  {display_name}")
        self._enter_workspace_mode()
        self._viewer.setFocus()
        QTimer.singleShot(0, lambda: self._finish_open_path(path, epoch))

    def _create_blank_dialog(self) -> None:
        """Open the New Blank paper-customization dialog (M8).

        The New chooser closes first; the customization modal opens over
        the Library. Nothing is created until Create is pressed, so
        cancelling the dialog leaves the app exactly as it was.
        """
        try:
            self._new_modal.close_modal(animate=False)
        except Exception:
            pass
        try:
            self._blank_modal.open_modal(self._library_page._new_btn)
        except Exception:
            pass

    def _create_blank_workspace(self, spec) -> None:
        """Create an unsaved blank document in its OWN tab.

        No Save-As and no filesystem destination: the document is managed
        in memory (with a private render working copy) and is immediately
        editable. The tab record exists synchronously (the strip updates at
        once); the validated viewer switch finishes deferred like every
        open. A new blank never replaces, prompts for, or closes any other
        tab.
        """
        try:
            self._blank_modal.close_modal(animate=False)
        except Exception:
            pass
        try:
            doc = self._manager.create_blank(spec)
        except Exception as exc:
            self._toast.show_message("Cannot create document", str(exc),
                                     kind="error")
            return
        session = self._register_session(doc)
        epoch = getattr(self, "_open_epoch", 0) + 1
        self._open_epoch = epoch
        self._pending_open_fallback = self._active_session
        self._viewer_bar.set_title("Untitled")
        self._titlebar.set_context("/  Untitled")
        self._enter_workspace_mode()
        self._viewer.setFocus()
        QTimer.singleShot(0, lambda: self._finish_open_blank(session, epoch))

    def _finish_open_blank(self, session: _TabSession, epoch: int) -> None:
        """Deferred validated viewer switch for a freshly created blank."""
        try:
            if session not in self._tab_sessions:
                return  # tab was closed before the deferred attach
            if epoch != getattr(self, "_open_epoch", epoch):
                # Superseded by a newer open: the tab and its document
                # stay (activation will attach it later); nothing leaks.
                return
            if self._activate_session(session, failure_mode="drop"):
                # The fallback record stays for the in-flight switch; the
                # completion/failure paths consume it.
                self._sync_workspace_title()
                return
            self._drop_session(session)
            try:
                self._manager.close(session.doc.id)
            except Exception:
                pass
            self._restore_after_open_failure()
        except Exception:
            pass

    # -- M8 save + unsaved-workspace protection -----------------------------

    def _save_current(self) -> bool:
        """Save the active workspace (HUD Save / Ctrl+S / leave prompt).

        Existing PDFs save directly to their current path, with no picker.
        A new blank asks for a destination only on its first save. Later
        saves overwrite that same file from a fresh, verified snapshot.
        A read-only duplicate session never saves (document-side guards
        would refuse it anyway; this gives the user a clear reason).
        """
        doc = None
        try:
            doc = self._viewer.current_document
        except Exception:
            doc = None
        if doc is not None and bool(getattr(doc, "read_only", False)):
            self._toast.show_message(
                "Read-only session", "This copy cannot be saved.",
                kind="info")
            return False
        if doc is None:
            # Nothing attached (e.g. a switch is pending): the document
            # the user is opening is the honest save target.
            pending = self._pending_switch
            if (pending is not None
                    and pending.session in self._tab_sessions):
                return self._save_document_session(pending.session.doc)
            self._toast.show_message("Nothing to save")
            return False
        try:
            can_save = bool(doc.can_save)
        except Exception as exc:
            self._report_save_failure(exc)
            return False
        if not can_save:
            try:
                persistence = doc.persistence_error
            except Exception:
                persistence = None
            detail = persistence or "This document cannot be saved safely."
            self._toast.show_message(
                "Cannot update annotations safely" if persistence
                else "Cannot save document", detail, kind="error")
            return False
        try:
            target = doc.save_target
        except Exception as exc:
            self._report_save_failure(exc)
            return False
        if not target:
            suggested = self._suggested_save_name(doc)
            target, _selected = QFileDialog.getSaveFileName(
                self, "Save PDF", suggested, "PDF files (*.pdf)")
            if not target:
                return False
            if not target.lower().endswith(".pdf"):
                target += ".pdf"
        return self._save_document_to(doc, target, allow_recovery=True)

    def _recovery_save_target(self, doc) -> str:
        """A non-overwriting Save As suggestion derived from the original.

        ``document.pdf`` becomes ``document-copy.pdf`` in the same
        location, so the recovery picker never preselects the inaccessible
        original. Falls back to the plain suggested name for documents
        without a save target yet.
        """
        try:
            base = doc.save_target
        except Exception:
            base = None
        if not base:
            base = self._suggested_save_name(doc)
        normalized_seps = str(base).replace("\\", "/")
        if "/" in normalized_seps:
            directory, name = normalized_seps.rsplit("/", 1)
            directory += "/"
        else:
            directory, name = "", normalized_seps
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        name = "".join(
            ch for ch in name if ch not in '\\/:*?"<>|').strip() or "Untitled"
        return f"{directory}{name}-copy.pdf"

    def _offer_save_as_recovery(self, doc, exc: Exception) -> bool:
        """Offer Save As after a classified destination-write failure.

        Reached only for :class:`SaveLockedError` (file lock / unwritable
        destination) from an ordinary Save: the document, its annotations,
        and the undo history are all intact. The failure was already
        reported; the user picks a fresh destination for a copy. Cancelling
        or a failing copy save keeps the session dirty and never reports
        success (the copy save itself reports its own failure). A
        successful copy follows existing Save As semantics: the live
        session is re-pointed at the new file.
        """
        try:
            if not doc.can_save:
                return False
        except Exception:
            return False
        target, _selected = QFileDialog.getSaveFileName(
            self, "Save a copy as", self._recovery_save_target(doc),
            "PDF files (*.pdf)")
        if not target:
            return False
        if not target.lower().endswith(".pdf"):
            target += ".pdf"
        # No allow_recovery: the user explicitly chose a destination, so a
        # second failure there is reported once without another picker
        # (and never retries the original inaccessible path).
        return self._save_document_to(doc, target)

    def _suggested_save_name(self, doc) -> str:
        try:
            name = str(doc.info().title or "Untitled")
        except Exception:
            name = "Untitled"
        name = "".join(
            ch for ch in name if ch not in '\\/:*?"<>|').strip() or "Untitled"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        return name

    def _save_all_dirty(self) -> bool:
        """Save every dirty managed document, or leave the app open."""
        try:
            docs = list(self._manager.documents())
        except Exception as exc:
            self._toast.show_message("Cannot save documents", str(exc),
                                     kind="error")
            return False
        dirty = []
        for doc in docs:
            try:
                if doc.is_dirty:
                    dirty.append(doc)
            except Exception as exc:
                self._toast.show_message("Cannot inspect document state",
                                         str(exc), kind="error")
                return False
        if not dirty:
            return True
        for doc in dirty:
            try:
                if not doc.can_save:
                    persistence = None
                    try:
                        persistence = doc.persistence_error
                    except Exception:
                        persistence = None
                    self._toast.show_message(
                        "Cannot update annotations safely" if persistence
                        else "Cannot save document",
                        persistence
                        or "This document cannot be saved safely.",
                        kind="error")
                    return False
            except Exception as exc:
                self._report_save_failure(exc)
                return False
            try:
                target = doc.save_target
            except Exception as exc:
                self._report_save_failure(exc)
                return False
            if not target:
                target, _selected = QFileDialog.getSaveFileName(
                    self, "Save PDF", self._suggested_save_name(doc),
                    "PDF files (*.pdf)")
                if not target:
                    return False
                if not target.lower().endswith(".pdf"):
                    target += ".pdf"
            if not self._save_document_to(doc, target):
                return False
        return True

    def _report_save_failure(self, exc: BaseException) -> None:
        """Show a save failure with a category-specific title and detail.

        The underlying message is preserved so lock contention, encrypted
        sources, manifest safeguards, and export/verification failures are
        actionable instead of collapsing into one generic toast.
        """
        detail = str(exc) or exc.__class__.__name__
        if isinstance(exc, DocumentClosedError):
            title = "Cannot save closed document"
            detail = ("This document is no longer open; reopen it before "
                      "saving.")
        elif isinstance(exc, SaveLockedError):
            title = "Cannot save document: file in use"
        elif isinstance(exc, EncryptedRewriteError):
            title = "Cannot save encrypted PDF"
        elif isinstance(exc, NativeAnnotationError):
            title = "Cannot save annotations"
        elif isinstance(exc, SaveVerificationError):
            title = "Save verification failed"
        elif isinstance(exc, ValueError):
            title = "Cannot save document: invalid destination"
        elif isinstance(exc, OSError):
            title = "Cannot save document: file system error"
        elif isinstance(exc, InkfallError):
            title = "Cannot save PDF"
        else:
            title = "Unexpected save failure"
        try:
            self._toast.show_message(title, detail, kind="error")
        except Exception:
            pass

    def _save_document_to(self, doc, target: str,
                          allow_recovery: bool = False) -> bool:
        """Export ``doc`` to ``target`` and register the saved file.

        The materialize + rebind + export + verify + replace work runs in
        :meth:`_run_save_job` (pool thread for the Qt-free steps, child
        process for the GIL-holding MuPDF export); this method still
        returns only after the save finished, so Save All and the
        close/exit guards keep their synchronous contracts. With
        ``allow_recovery`` (ordinary Save only), a classified
        destination-write failure (:class:`SaveLockedError`) additionally
        offers the Save As copy flow. Serialization and invalid-document
        failures never trigger recovery.
        """
        if getattr(self, "_save_in_flight", False):
            try:
                self._toast.show_message(
                    "Save already in progress",
                    "Wait for the current save to finish before saving again.",
                    kind="info")
            except Exception:
                pass
            return False
        reading_key = None
        try:
            reading_key = doc.reading_key
        except Exception:
            reading_key = None
        try:
            path_key, key = self._manager.begin_save(doc, target)
            self._run_save_job(doc, target)
        except Exception as exc:
            self._report_save_failure(exc)
            if allow_recovery and isinstance(exc, SaveLockedError):
                return self._offer_save_as_recovery(doc, exc)
            return False
        try:
            normalized = self._manager.complete_save(
                doc, target, path_key, key)
        except Exception as exc:
            self._report_save_failure(exc)
            return False
        # The export succeeded: reset the dirty baseline (undo stack
        # clean index + document flag) before anything reads it.
        try:
            self._viewer.mark_document_saved(doc)
        except Exception:
            pass
        if doc is self._viewer.current_document:
            try:
                self._viewer.persist_reading_position(
                    migrate_from=reading_key)
            except Exception:
                pass
        elif reading_key:
            try:
                self._viewer.migrate_reading_position(
                    reading_key, doc.reading_key)
            except Exception:
                pass
        try:
            item = self._library.import_path(normalized)
            self._library.touch_opened(item.id, page_count=doc.page_count)
            self._library_page.refresh()
        except Exception:
            pass
        self._sync_workspace_title()
        self._sync_tab_titles()
        try:
            name = normalized.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            self._toast.show_message("Saved", name)
        except Exception:
            pass
        self._publish_instance_docs()
        return True

    def _run_save_job(self, doc, target: str) -> None:
        """Save ``doc`` to ``target`` without freezing the GUI thread.

        Each step runs where its blocking work actually happens:

        1. ``prepare_for_save`` materializes the sanitized working copy
           used by the renderer (same-path saves) on the save pool.
        2. The live pipeline is rebound to that copy on the save pool,
           BEFORE the export replaces the destination: this is what
           releases the source file (Windows sharing violation otherwise)
           and it is the second-largest stall after the export itself
           (~5.5 s of decoder-process joins), so it cannot stay on the
           GUI thread either.
        3. The export (annotate + rewrite + verify + atomic replace) runs
           in a spawned child process. PyMuPDF holds the GIL for the whole
           ``Document.save`` call, so a pool *thread* cannot keep Qt's
           event loop alive (measured: one loop iteration in 0.97 s); the
           child is polled here while Qt events are pumped.

        The window-modal busy dialog blocks user input and the active
        document's canvas is in the existing loading state (paint returns
        early, render timer stopped), which keeps the pool thread away
        from a pipeline the GUI is drawing. Registry changes and
        ``commit_saved`` still happen on the GUI thread after this
        returns, so a failure keeps the session dirty and intact.
        """
        dialog = None
        entered_loading = False
        self._save_in_flight = True
        self._save_dialog = None
        try:
            try:
                active = doc is self._viewer.current_document
            except Exception:
                active = False
            if active:
                try:
                    if not self._viewer.is_loading():
                        self._viewer.enter_loading(self._tab_title_for(doc))
                        entered_loading = True
                except Exception:
                    entered_loading = False
            try:
                dialog = QProgressDialog(
                    f"Saving {self._suggested_save_name(doc)}", "", 0, 0,
                    self)
                dialog.setWindowTitle("Saving PDF")
                dialog.setWindowModality(Qt.WindowModality.WindowModal)
                dialog.setCancelButton(None)
                dialog.setMinimumDuration(0)
                dialog.setAutoClose(False)
                dialog.setAutoReset(False)
                dialog.setWindowFlag(
                    Qt.WindowType.WindowCloseButtonHint, False)
                dialog.setLabelText(_SAVE_STAGES["prepare"])
                dialog.show()
                self._save_dialog = dialog
            except Exception:
                dialog = None
                self._save_dialog = None

            # Cross-instance coordination: other InkFall windows editing
            # the same file must release it before any save work starts
            # (bounded wait; refusal/timeout raises SaveLockedError, which
            # the ordinary recovery path handles).
            self._coordinate_save_start(target)

            self._run_pool_blocking(
                lambda: doc.prepare_for_save(target))

            try:
                plan = self._viewer.save_rebind_plan(doc)
            except Exception:
                plan = None
            if plan is not None:
                pipeline, render_path = plan
                self._show_save_stage("rebind")
                self._run_pool_blocking(
                    lambda: pipeline.rebind_source(render_path))
                try:
                    self._viewer.finish_save_rebind()
                except Exception:
                    pass

            doc.write_export(target, progress=self._show_save_stage,
                             pump=self._process_pump)
        finally:
            self._save_in_flight = False
            self._save_dialog = None
            if dialog is not None:
                try:
                    dialog.close()
                    dialog.deleteLater()
                except Exception:
                    pass
            if entered_loading:
                try:
                    self._viewer.exit_loading()
                except Exception:
                    pass

    def _run_pool_blocking(self, func):
        """Run ``func`` on the save pool; re-raise its failure here.

        The GUI thread waits in a nested event loop instead of blocking on
        the pool thread, so timers and window paint keep flowing and the
        platform never marks the window as unresponsive. The callable is
        Qt-free by contract (document materialization, pipeline rebind).
        """
        self._save_epoch += 1
        epoch = self._save_epoch
        hub = _SaveHub(self)
        state: dict = {}
        loop = QEventLoop(self)

        def _on_done(_epoch: int, result) -> None:
            if _epoch != epoch:
                return
            state.update(result or {})
            loop.quit()

        hub.done.connect(_on_done)
        try:
            self._save_pool.start(_CallJob(hub, epoch, func))
            loop.exec()
        finally:
            try:
                hub.done.disconnect(_on_done)
            except Exception:
                pass
        if not state:
            raise InvalidPdfError(
                "Saving was interrupted before it completed; the document "
                "and the original file are unchanged.")
        if not state.get("ok"):
            error = state.get("error")
            if isinstance(error, BaseException):
                raise error
            raise InvalidPdfError("The save did not complete.")
        return state.get("value")

    def _process_pump(self, poll):
        """``Document.write_export`` pump hook for the UI save path."""
        return self._pump_until(poll, self._show_save_stage)

    def _pump_until(self, poll, on_stage=None):
        """Wait for ``poll`` while keeping Qt's event loop alive.

        ``poll`` is the backend's non-blocking child-process poll
        (``poll() -> (done, payload)``); a payload may carry a ``stage``
        name, forwarded to ``on_stage`` for the progress label. This is
        deliberately a nested wait, not a blocking join: the child owns
        the GIL-holding MuPDF work, so pumping here is exactly what keeps
        the window painting and the platform from flagging it
        unresponsive.
        """
        app = QApplication.instance()
        while True:
            done, payload = poll()
            if payload:
                stage = payload.get("stage")
                if stage and on_stage is not None:
                    try:
                        on_stage(stage)
                    except Exception:
                        pass
            if done:
                return payload
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            time.sleep(0.01)

    def _show_save_stage(self, stage: str) -> None:
        """Update the busy dialog's label for one backend stage name."""
        dialog = getattr(self, "_save_dialog", None)
        if dialog is None:
            return
        label = _SAVE_STAGES.get(str(stage))
        if not label:
            return
        try:
            dialog.setLabelText(label)
        except Exception:
            pass

    # -- per-tab close guard (Phase 2) ---------------------------------------

    def _confirm_close_session(self, session: _TabSession) -> bool:
        """Dirty guard for ONE tab's document (Save / Discard / Cancel).

        Only the selected document is considered: other tabs, their dirty
        flags, histories and pipelines are never touched. A failed or
        cancelled save leaves the tab and its content intact.
        """
        doc = session.doc
        try:
            dirty = bool(doc.is_dirty)
        except Exception:
            dirty = False
        if not dirty:
            return True
        can_save = False
        try:
            can_save = bool(doc.can_save)
        except Exception:
            can_save = False
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Save changes to “{self._tab_title_for(doc)}” before closing?")
        detail = "This document has changes that are not saved yet."
        try:
            error = doc.persistence_error
        except Exception:
            error = None
        if error:
            detail += "\n\n" + str(error)
        box.setInformativeText(detail)
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton(
            "Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole)
        save_btn.setEnabled(can_save)
        if can_save:
            box.setDefaultButton(save_btn)
        else:
            box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return self._save_document_session(doc)
        if clicked is discard_btn:
            return True
        return False

    def _save_document_session(self, doc) -> bool:
        """Save one specific tab's document (picker only on first save)."""
        try:
            if not doc.can_save:
                persistence = None
                try:
                    persistence = doc.persistence_error
                except Exception:
                    persistence = None
                self._toast.show_message(
                    "Cannot update annotations safely" if persistence
                    else "Cannot save document",
                    persistence or "This document cannot be saved safely.",
                    kind="error")
                return False
            target = doc.save_target
        except Exception as exc:
            self._report_save_failure(exc)
            return False
        if not target:
            suggested = self._suggested_save_name(doc)
            target, _selected = QFileDialog.getSaveFileName(
                self, "Save PDF", suggested, "PDF files (*.pdf)")
            if not target:
                return False
            if not target.lower().endswith(".pdf"):
                target += ".pdf"
        return self._save_document_to(doc, target)

    def _confirm_leave_unsaved(self, for_exit: bool = False,
                               subject: str | None = None) -> bool:
        """Four-choice guard for explicit close or application exit.

        Explicitly closing the current PDF considers that document; exiting
        considers every managed dirty document. Save All must succeed for
        all of them before exit. Save saves only the active document and
        keeps the application open when other dirty documents would remain.
        A failed/cancelled save leaves all sessions and dirty flags intact.
        ``subject`` overrides the prompt line for other whole-workflow
        gates (Close All) without changing any semantics.
        """
        active = None
        try:
            active = self._viewer.current_document
        except Exception:
            active = None
        try:
            docs = list(self._manager.documents())
        except Exception as exc:
            self._toast.show_message(
                "Cannot inspect open documents", str(exc), kind="error")
            return False
        if for_exit:
            dirty = [doc for doc in docs if getattr(doc, "is_dirty", False)]
        else:
            dirty = ([active] if active is not None
                     and getattr(active, "is_dirty", False) else [])
        if not dirty:
            return True

        save_all_ok = True
        for doc in dirty:
            try:
                if not doc.can_save:
                    save_all_ok = False
                    break
            except Exception:
                save_all_ok = False
                break
        save_active_ok = False
        if active is not None and getattr(active, "is_dirty", False):
            try:
                save_active_ok = bool(active.can_save)
            except Exception:
                save_active_ok = False

        box = QMessageBox(self)
        box.setWindowTitle("Unsaved changes")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(subject or "Save your changes before leaving?")
        if len(dirty) == 1:
            detail = "This document has changes that are not saved yet."
        else:
            detail = (f"{len(dirty)} documents have changes that are not "
                      "saved yet.")
        blocked_errors = []
        for doc in dirty:
            try:
                error = doc.persistence_error
            except Exception:
                error = None
            if error and error not in blocked_errors:
                blocked_errors.append(error)
        if blocked_errors:
            detail += "\n\n" + blocked_errors[0]
        box.setInformativeText(detail)
        save_all_btn = box.addButton(
            "Save All", QMessageBox.ButtonRole.AcceptRole)
        save_btn = box.addButton(
            "Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton(
            "Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(
            "Cancel", QMessageBox.ButtonRole.RejectRole)
        save_all_btn.setEnabled(save_all_ok)
        save_btn.setEnabled(save_active_ok)
        if save_all_ok:
            box.setDefaultButton(save_all_btn)
        else:
            box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_all_btn:
            return self._save_all_dirty()
        if clicked is save_btn:
            active_dirty = (active is not None
                            and getattr(active, "is_dirty", False))
            if active_dirty and not self._save_current():
                return False
            if for_exit:
                try:
                    remaining = [doc for doc in self._manager.documents()
                                 if doc.is_dirty]
                except Exception as exc:
                    self._toast.show_message(
                        "Cannot inspect open documents", str(exc),
                        kind="error")
                    return False
                if remaining:
                    self._toast.show_message(
                        "Other documents still have unsaved changes",
                        "Use Save All to save every document before exiting.",
                        kind="error")
                    return False
            return True
        if clicked is discard_btn:
            return True
        return False

    def _sync_workspace_title(self) -> None:
        """Reflect the workspace document's name and dirty marker."""
        # Tab titles/dirty dots stay in step even while Home is shown
        # (e.g. the exit guard saving background documents).
        self._sync_tab_titles()
        try:
            if not self.in_workspace:
                return
        except Exception:
            return
        # A pending switch owns the title until it commits or fails:
        # the attached (outgoing) document must never show under the
        # requested tab's title.
        pending = self._pending_switch
        if (pending is not None
                and pending.session in self._tab_sessions):
            try:
                self._show_pending_title(pending.session.doc)
            except Exception:
                pass
            return
        doc = None
        try:
            doc = self._viewer.current_document
        except Exception:
            doc = None
        name = ""
        dirty = False
        if doc is not None:
            try:
                name = doc.info().title or ""
                dirty = bool(doc.is_dirty)
            except Exception:
                name = ""
        if not name:
            return
        display = f"{name} •" if dirty else name
        try:
            self._viewer_bar.set_title(display)
            self._titlebar.set_context(f"/  {display}")
        except Exception:
            pass

    def _toggle_theme(self) -> None:
        tokens = self._theme.toggle()
        self._apply_chrome_tokens(tokens)

    def _on_theme_changed(self, tokens) -> None:
        self._apply_chrome_tokens(tokens)

    def _grab_client_excluding_modals(self):
        """Grab content with open modals hidden, restored same turn."""
        settings_open = False
        new_open = False
        blank_open = False
        try:
            settings_open = bool(self._settings_modal.isVisible())
        except Exception:
            pass
        try:
            new_open = bool(self._new_modal.isVisible())
        except Exception:
            pass
        try:
            blank_open = bool(self._blank_modal.isVisible())
        except Exception:
            pass
        if not (settings_open or new_open or blank_open):
            try:
                pm = self._content.grab()
                return pm if pm is not None and not pm.isNull() else None
            except Exception:
                return None
        # Hide -> grab -> restore synchronously: no frame reaches the
        # screen with the modal missing; no layout/focus change.
        focused = None
        try:
            from PySide6.QtWidgets import QApplication

            focused = QApplication.focusWidget()
        except Exception:
            pass
        try:
            if settings_open:
                self._settings_modal.setVisible(False)
            if new_open:
                self._new_modal.setVisible(False)
            if blank_open:
                self._blank_modal.setVisible(False)
            pm = self._content.grab()
        except Exception:
            pm = None
        finally:
            try:
                if settings_open:
                    self._settings_modal.setVisible(True)
                    self._settings_modal.setGeometry(self._content.rect())
                if new_open:
                    self._new_modal.setVisible(True)
                    self._new_modal.setGeometry(self._content.rect())
                if blank_open:
                    self._blank_modal.setVisible(True)
                    self._blank_modal.setGeometry(self._content.rect())
                if focused is not None:
                    try:
                        focused.setFocus()
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            return pm if pm is not None and not pm.isNull() else None
        except Exception:
            return None

    def _play_theme_fade(self, old_pm, ms: int = 210) -> None:
        """Crossfade old client plate over the live new-theme UI."""
        if old_pm is None:
            return
        try:
            if old_pm.isNull():
                return
        except Exception:
            return
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation
            from PySide6.QtGui import QPainter
            from PySide6.QtWidgets import QLabel

            if getattr(self, "_theme_fade_anim", None) is not None:
                try:
                    self._theme_fade_anim.stop()
                except Exception:
                    pass
            if getattr(self, "_theme_fade_label", None) is not None:
                try:
                    self._theme_fade_label.deleteLater()
                except Exception:
                    pass
            label = QLabel(self._content)
            label.setPixmap(old_pm)
            label.setScaledContents(True)
            label.setGeometry(self._content.rect())
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            label.show()
            # Modals stay live above the fade.
            try:
                if self._settings_modal.isVisible():
                    self._settings_modal.raise_()
                if self._new_modal.isVisible():
                    self._new_modal.raise_()
                self._toast.raise_()
                self._settings_fab.raise_()
            except Exception:
                pass
            self._theme_fade_label = label
            anim = QVariantAnimation(self)
            anim.setDuration(max(1, int(ms)))
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            try:
                anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            except Exception:
                pass

            def _tick(value) -> None:
                try:
                    op = max(0.0, min(1.0, float(value)))
                    label.setWindowOpacity(op)
                    # QLabel windowOpacity on a child may not composite;
                    # fall back to painter alpha via graphics effect.
                    from PySide6.QtWidgets import QGraphicsOpacityEffect

                    eff = label.graphicsEffect()
                    if eff is None:
                        eff = QGraphicsOpacityEffect(label)
                        label.setGraphicsEffect(eff)
                    eff.setOpacity(op)
                except Exception:
                    pass

            def _done() -> None:
                try:
                    label.deleteLater()
                except Exception:
                    pass
                self._theme_fade_anim = None
                self._theme_fade_label = None

            anim.valueChanged.connect(_tick)
            anim.finished.connect(_done)
            anim.start()
            self._theme_fade_anim = anim
        except Exception:
            pass

    def _apply_chrome_tokens(self, tokens) -> None:
        """Restyle shell chrome (viewer canvas depth included).

        M4.6.11: animated 210ms crossfade. Old client plate (modal
        excluded) fades over the live new-theme UI; open modals blend
        old->new plates simultaneously and stay open with state intact.
        Retargets in-flight hover/section/pin/modal anims by repaint —
        no duplicate animators, no stale painter state, 0 PDF renders.
        """
        old_pm = None
        try:
            old_pm = self._grab_client_excluding_modals()
        except Exception:
            old_pm = None
        self._library_page.update_tokens(tokens)
        self._settings_fab.update_tokens(tokens)
        self._settings_modal.update_tokens(tokens)
        self._new_modal.update_tokens(tokens)
        self._blank_modal.update_tokens(tokens)
        self._titlebar.update_tokens(tokens)
        self._viewer_bar.update_tokens(tokens)
        try:
            self._tab_strip.update_tokens(tokens)
        except Exception:
            pass
        self._toast.update_tokens(tokens)
        try:
            self._viewer.update_chrome_tokens(tokens)
        except Exception:
            pass
        self._viewer_wrap.setStyleSheet(f"background-color: {tokens.canvas_deep};")
        try:
            self._library_page._view.viewport().update()
        except Exception:
            pass
        new_pm = None
        try:
            new_pm = self._grab_client_excluding_modals()
        except Exception:
            new_pm = None
        # Modal backdrop(s) blend old->new; main client fades old over
        # live new UI. Both 210ms OutCubic, same window.
        try:
            if self._settings_modal.isVisible():
                self._settings_modal.crossfade_theme_plates(old_pm, new_pm, 210)
            if self._new_modal.isVisible():
                self._new_modal.crossfade_theme_plates(old_pm, new_pm, 210)
            if self._blank_modal.isVisible():
                self._blank_modal.crossfade_theme_plates(old_pm, new_pm, 210)
        except Exception:
            pass
        try:
            self._play_theme_fade(old_pm, 210)
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        """Stop thumbnail workers, release rendering, persist library."""
        # M8/Phase 2: dirty managed documents get one Save All / Save /
        # Discard / Cancel gate; Cancel ignores the close and keeps the
        # workspace live. A close started by the title-bar button already
        # ran the gate (and faded the window), so it is never asked twice.
        if getattr(self, "_leave_guard_passed", False):
            self._leave_guard_passed = False
        elif not self._confirm_leave_unsaved(for_exit=True):
            event.ignore()
            # Direct closes (Alt+F4, taskbar, self.close()) may race an
            # in-flight title-bar fade; restore the window so a cancel
            # never strands an invisible session.
            try:
                anim = getattr(self, "_close_anim", None)
                if anim is not None:
                    anim.stop()
            except Exception:
                pass
            self._close_anim = None
            self._closing = False
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass
            return
        # M4.6.7: invalidate pending deferred opens first — a queued
        # finish must never spawn pipelines after teardown.
        try:
            self._open_epoch = getattr(self, "_open_epoch", 0) + 1
        except Exception:
            pass
        # Invalidate any pending switch and restore the viewer's inline
        # pipeline close: application exit owns the deterministic join
        # (background retirement would race the teardown below).
        self._cancel_pending_switch()
        try:
            self._viewer.exit_loading()
        except Exception:
            pass
        try:
            self._viewer.set_pipeline_retirer(None)
        except Exception:
            pass
        try:
            self._thumbs.shutdown()
        except Exception:
            pass
        try:
            self._viewer.shutdown()
        finally:
            # Phase 2: every tab owns a managed document. The active one
            # was released by the viewer above; close every remaining tab
            # document (and drop the strip) before the store goes away.
            try:
                self._close_all_tabs()
            except Exception:
                pass
            try:
                # Pipelines handed to the background pool during the
                # session are drained before the store closes.
                self._switch_pool.waitForDone(10000)
            except Exception:
                pass
            try:
                # A save worker must never outlive the documents it owns.
                # The modal progress loop prevents a close during a save;
                # this drain is the belt-and-braces guarantee.
                self._save_pool.waitForDone(30000)
            except Exception:
                pass
            try:
                # Other windows must stop seeing this instance in the
                # coordination registry before the process goes away.
                hub = getattr(self, "_instance_hub", None)
                if hub is not None:
                    hub.close()
            except Exception:
                pass
            try:
                if getattr(self, "_own_store", False):
                    self._store.close()
            except Exception:
                pass
            super().closeEvent(event)
