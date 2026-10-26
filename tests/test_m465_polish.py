"""M4.6.5 interaction tests: icon, modals, New, search, sort, grid/list,
fullscreen, startup, scroll continuity, hover, direction regression.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView

from app import fx
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


# -- App icon ---------------------------------------------------------------

def test_canonical_png_exists_and_loads():
    import pathlib

    from PySide6.QtGui import QPixmap

    from app.icons import _brand_dir, app_icon

    png = pathlib.Path(str(_brand_dir())) / "new_icon.png"
    assert png.is_file(), "user-supplied PNG missing"
    base = QPixmap(str(png))
    assert not base.isNull()
    assert base.width() == base.height()  # square artwork preserved
    icon = app_icon()
    assert not icon.isNull()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        assert not icon.pixmap(size, size).isNull(), size


def test_brand_surfaces_use_canonical_icon(shell):
    window, _service = shell
    assert not window.windowIcon().isNull()
    assert not window.titlebar._icon.pixmap().isNull()
    assert not window.library_page._brand_icon.pixmap().isNull()


# -- Centered modals ----------------------------------------------------------

def test_settings_modal_centered_with_backdrop(shell, qtbot):
    window, _service = shell
    from PySide6.QtGui import QColor

    window.toggle_settings_panel()
    modal = window.settings_modal
    try:
        assert modal.isVisible() is True
        assert modal.rect() == modal.parent().rect()  # full client area
        qtbot.waitUntil(lambda: modal._panel.graphicsEffect() is None,
                        timeout=5000)
        center = modal._panel.geometry().center()
        expect = modal.rect().center()
        assert abs(center.x() - expect.x()) <= 2
        assert abs(center.y() - expect.y()) <= 2
        assert modal._panel.width() >= 760 and modal._panel.width() <= 840
        img = modal.grab().toImage()
        corner = QColor.fromRgba(img.pixel(4, 4))
        assert corner.alpha() == 255  # backdrop dims (opaque composite)
    finally:
        modal.close_modal()
        qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


def test_modal_backdrop_click_closes(shell, qtbot):
    window, _service = shell
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    window.toggle_settings_panel()
    modal = window.settings_modal
    assert modal.isVisible() is True
    pos = QPointF(4, 400)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, pos, pos,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    modal.mousePressEvent(event)
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


def test_modal_focus_trap_and_return(shell, qtbot):
    from PySide6.QtWidgets import QApplication

    window, _service = shell
    window.toggle_settings_panel()
    modal = window.settings_modal
    items = modal._focusables()
    assert len(items) >= 2
    items[-1].setFocus()
    qtbot.keyClick(items[-1], Qt.Key.Key_Tab)
    # Tab on the last item wraps to the first (trap holds).
    assert QApplication.focusWidget() is items[0]
    modal.close_modal()
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


def test_new_panel_structure(shell, qtbot):
    window, _service = shell
    window.toggle_new_panel()
    modal = window.new_panel
    try:
        assert modal.isVisible() is True
        assert modal._panel.width() >= 420 and modal._panel.width() <= 500
        assert modal._title.text() == "New"
        assert len(modal._rows) == 2
        titles = [r._inkfall_name.text() for r in modal._rows]
        assert titles == ["Create Blank", "Open PDF"]
        for row in modal._rows:
            assert row._inkfall_sub.text() != ""
            assert not row._inkfall_art.pixmap().isNull()
    finally:
        modal.close_modal()
        qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


def test_open_pdf_uses_existing_path(shell, tmp_path, qtbot, monkeypatch):
    window, service = shell
    pdf = _make_pdf(tmp_path, "pick.pdf")
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getOpenFileName",
        lambda *a, **k: (pdf, "PDF files (*.pdf)"))
    window.toggle_new_panel()
    modal = window.new_panel
    from PySide6.QtTest import QTest

    QTest.mouseClick(modal._rows[1], Qt.MouseButton.LeftButton)  # Open PDF row
    qtbot.waitUntil(lambda: window.in_workspace is True, timeout=10000)
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    assert service.item_count == 1  # registered via existing import path
    window.close_document()


def test_create_blank_opens_unsaved_workspace(shell, tmp_path, qtbot, monkeypatch):
    """M8: Create opens an editable unsaved workspace; Save As is on save."""
    from PySide6.QtTest import QTest

    from tests.helpers import page_count_of

    window, service = shell
    window.toggle_new_panel()
    modal = window.new_panel
    QTest.mouseClick(modal._rows[0], Qt.MouseButton.LeftButton)  # Create Blank
    qtbot.waitUntil(lambda: window.blank_modal.isVisible() is True, timeout=5000)
    assert service.item_count == 0  # nothing is written before Create
    window.blank_modal._create_btn.click()
    qtbot.waitUntil(lambda: window.in_workspace is True, timeout=10000)
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    doc = window.viewer.current_document
    assert doc is not None
    assert doc.is_unsaved is True
    assert doc.saved_path is None
    assert service.item_count == 0  # still no destination file
    # First save asks for a path, writes the one-page PDF, and keeps the
    # session open and editable.
    target = str(tmp_path / "blank.pdf")
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *a, **k: (target, "PDF files (*.pdf)"))
    assert window._save_current() is True
    assert doc.is_unsaved is False
    assert doc.saved_path is not None
    assert page_count_of(target) == 1
    assert service.item_count == 1
    # A repeated save reuses the destination (no second Save As); a
    # transient file lock may still report failure, but it must never
    # ask for a path again.
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked again")))
    result = window._save_current()
    assert isinstance(result, bool)
    assert doc.saved_path is not None
    window.close_document()


def test_blank_helper_isolated():
    import ast
    import inspect

    from document import backend as backend

    assert hasattr(backend, "create_blank_pdf")
    tree = ast.parse(inspect.getsource(backend.create_blank_pdf))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # Only pymupdf file creation; nothing else.
    assert imported <= {"pymupdf"}, imported


def test_new_modal_hidden_in_workspace(shell, tmp_path):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    window.toggle_new_panel()
    assert window.new_panel.isVisible() is True
    assert window.open_library_item(item.id) is True
    assert window.new_panel.isVisible() is False
    window.close_document()


# -- Circular New button -------------------------------------------------------

def test_new_button_circular_icon_only(shell):
    window, _service = shell
    btn = window.library_page._new_btn
    assert btn.text() == ""
    assert btn.width() == btn.height() == 38
    assert not btn.icon().isNull()
    assert btn.toolTip() == ""
    assert btn.accessibleName() != "" and btn.accessibleDescription() != ""
    assert btn.focusPolicy() & Qt.FocusPolicy.TabFocus


# -- Circular search ------------------------------------------------------------

def test_search_resting_expanded_collapsed(shell, qtbot):
    window, _service = shell
    page = window.library_page
    assert page._search_btn.isVisible() is True
    assert page._search.isHidden() is True
    assert page._search_btn.width() == page._search_btn.height() == 38
    page._search_btn.click()
    qtbot.waitUntil(lambda: page._search.isVisible() is True, timeout=2000)
    qtbot.waitUntil(lambda: page._search.width() >= 250, timeout=5000)
    assert page._search.hasFocus()
    assert page._search_btn.isHidden() is True


def test_search_collapse_rules(shell, qtbot):
    from PySide6.QtGui import QFocusEvent, QKeyEvent
    from PySide6.QtWidgets import QApplication

    window, _service = shell
    page = window.library_page
    page._expand_search()
    qtbot.waitUntil(lambda: page._search.width() >= 250, timeout=5000)
    # Text keeps it expanded across focus loss.
    page._search.setText("alpha")
    QApplication.sendEvent(page._search, QFocusEvent(QFocusEvent.Type.FocusOut))
    assert page._search.isVisible() is True
    # Escape clears first, collapses when already empty.
    QApplication.sendEvent(
        page._search, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                Qt.KeyboardModifier.NoModifier))
    assert page._search.text() == ""
    assert page._search.isVisible() is True
    QApplication.sendEvent(
        page._search, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                Qt.KeyboardModifier.NoModifier))
    qtbot.waitUntil(lambda: page._search.isHidden() is True, timeout=5000)
    assert page._search_btn.isVisible() is True


def test_search_pair_geometry(shell):
    window, _service = shell
    page = window.library_page
    assert page._search_btn.height() == page._new_btn.height() == 38
    assert page._search_btn.width() == page._new_btn.width() == 38


# -- Sort cluster ---------------------------------------------------------------

def test_sort_immediately_left_of_grid_list(shell):
    window, _service = shell
    page = window.library_page
    s, g = page._sort_btn.geometry(), page._seg.geometry()
    assert s.right() < g.x()
    gap = g.x() - s.right()
    assert 8 <= gap <= 12, gap
    assert s.y() < g.y() + g.height() and g.y() < s.y() + s.height()
    assert s.height() == g.height()


def test_sort_menu_animated_open(shell, qtbot):
    from PySide6.QtWidgets import QMenu

    window, _service = shell
    menu = QMenu(window)
    menu.addAction("Name")
    fx.animate_menu_open(menu, 140, 5, window)
    menu.show()
    try:
        assert menu.graphicsEffect() is not None
        qtbot.waitUntil(lambda: menu.graphicsEffect() is None, timeout=5000)
    finally:
        menu.hide()
        menu.deleteLater()


# -- Grid/List transition ----------------------------------------------------------

def _settled_thumbs(qtbot, window, count, timeout=30000):
    from app.library.qt_model import Role

    page = window.library_page
    model = page._model
    bar = page._view.verticalScrollBar()

    def ready():
        return sum(1 for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None
                   and not r.is_header and not r.is_folder
                   and model.data(model.index(i, 0), Role.THUMB) is not None)

    # Sweep through (virtualized below-fold rows resolve on visibility).
    t_end = __import__("time").time() + timeout
    while ready() < count and __import__("time").time() < t_end:
        bar.setValue(bar.maximum())
        qtbot.wait(300)
        bar.setValue(0)
        qtbot.wait(300)
    assert ready() >= count
    before = window._thumbs.renders_started
    qtbot.wait(600)
    assert window._thumbs.renders_started == before
    return before


def test_grid_list_transition_both_ways(shell, qtbot, tmp_path):
    window, service = shell
    for i in range(12):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    window.library_page.refresh()
    renders = _settled_thumbs(qtbot, window, 12)
    page = window.library_page
    bar = page._view.verticalScrollBar()
    page.set_list_mode(True)
    from PySide6.QtWidgets import QListView

    assert page._view.viewMode() == QListView.ViewMode.ListMode
    assert page._view.viewport().graphicsEffect() is not None  # animating
    qtbot.waitUntil(
        lambda: page._view.viewport().graphicsEffect() is None, timeout=5000)
    assert window._thumbs.renders_started == renders  # +0
    page.set_list_mode(False)
    from PySide6.QtWidgets import QListView

    assert page._view.viewMode() == QListView.ViewMode.IconMode
    qtbot.waitUntil(
        lambda: page._view.viewport().graphicsEffect() is None, timeout=5000)
    assert window._thumbs.renders_started == renders  # +0
    model = page._model
    for i in range(model.rowCount()):
        assert page._view.indexWidget(model.index(i, 0)) is None


def test_drag_drop_ready_on_initial_open_and_after_view_switches(shell):
    """Book→folder drag/drop must be usable without toggling the layout.

    ``setMovement(Static)`` clears the viewport's drop acceptance while
    leaving the view's own flag set, so the configuration helper must
    restore the viewport explicitly. A first-open regression here made
    drags start but never reach dragEnter/dragMove/drop until one List↔Grid
    switch reset the view acceptance and forced Qt to propagate it.
    """
    window, _service = shell
    page = window.library_page
    view = page._view

    def assert_ready():
        assert view.dragEnabled() is True
        assert view.acceptDrops() is True
        assert view.viewport().acceptDrops() is True
        assert view.dragDropMode() == QAbstractItemView.DragDropMode.DragDrop
        assert view.defaultDropAction() == Qt.DropAction.MoveAction
        assert view.showDropIndicator() is False

    assert_ready()
    page.set_list_mode(True)
    assert_ready()
    page.set_list_mode(False)
    assert_ready()


def test_book_drop_into_folder_works_on_first_open_and_both_views(
        shell, qtbot, tmp_path):
    """A real drop moves the book immediately, then in List and Grid mode."""
    from PySide6.QtCore import QMimeData, QPointF
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from PySide6.QtWidgets import QApplication

    from app.library.qt_model import MIME_LIBRARY_ITEM

    window, service = shell
    page = window.library_page
    view = page._view
    folder = service.create_folder("Drag Targets")
    item = service.import_path(_make_pdf(tmp_path, "drag-book.pdf"))

    def settle():
        page.refresh()
        if page.is_section_collapsed("ungrouped"):
            page.toggle_section("ungrouped")
        qtbot.wait(50)

    def indices():
        model = page._model
        book = folder_index = None
        for row in range(model.rowCount()):
            data = model.row_at(row)
            if data is None:
                continue
            if (data.is_folder and data.folder is not None
                    and data.folder.id == folder.id):
                folder_index = model.index(row, 0)
            elif (not data.is_folder and not data.is_header
                    and not data.is_hint and data.item is not None
                    and data.item.id == item.id):
                book = model.index(row, 0)
        assert book is not None, "the book row is not visible"
        assert folder_index is not None, "the folder row is not visible"
        return book, folder_index

    def drop_book():
        book, folder_index = indices()
        mime = view.model().mimeData([book])
        assert mime.hasFormat(MIME_LIBRARY_ITEM)
        pos = view.visualRect(folder_index).center()
        assert not view.visualRect(folder_index).isEmpty()
        enter = QDragEnterEvent(
            pos, Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), enter)
        move = QDragMoveEvent(
            pos, Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), move)
        drop = QDropEvent(
            QPointF(pos), Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), drop)
        assert enter.isAccepted() is True
        assert move.isAccepted() is True
        assert drop.isAccepted() is True
        assert service.get_item(item.id).parent_id == folder.id

    # Initial Grid view: the reported failure happened before any switch.
    settle()
    drop_book()

    # List view keeps the same behavior.
    service.move_item(item.id, None)
    page.set_list_mode(True)
    qtbot.waitUntil(
        lambda: view.viewport().graphicsEffect() is None, timeout=5000)
    settle()
    drop_book()

    # Back to Grid view.
    service.move_item(item.id, None)
    page.set_list_mode(False)
    qtbot.waitUntil(
        lambda: view.viewport().graphicsEffect() is None, timeout=5000)
    settle()
    drop_book()


def test_grid_list_preserves_state_and_scroll(shell, qtbot, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    for i in range(12):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"),
                            folder.id if i % 2 else None)
    page = window.library_page
    page.select_category(folder.id)
    page._search.setText("d1")
    page.refresh()
    page._sort_index = 1
    page._sync_sort_label()
    page.refresh()
    renders = _settled_thumbs(qtbot, window, 2)
    bar = page._view.verticalScrollBar()
    frac_before = bar.value() / bar.maximum() if bar.maximum() else 0.0
    page.set_list_mode(True)
    qtbot.waitUntil(
        lambda: page._view.viewport().graphicsEffect() is None, timeout=5000)
    assert page.selected_category_id == folder.id
    assert page._search.text() == "d1"
    assert page._sort_index == 1
    frac_after = bar.value() / bar.maximum() if bar.maximum() else 0.0
    assert abs(frac_after - frac_before) <= 0.35
    assert window._thumbs.renders_started == renders
    page.clear_filter()
    page._search.clear()


# -- Hover -------------------------------------------------------------------------

def test_hover_animator_single_and_bounded(shell, qtbot, tmp_path):
    from PySide6.QtCore import QVariantAnimation

    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    page = window.library_page
    exact_anims = [c for c in page.findChildren(QVariantAnimation)
                   if type(c) is QVariantAnimation]
    # M4.6.10: six view-level singletons (hover, section, pin,
    # category press, header hover, category hover) — still no
    # per-card objects. M4.6.11: + press micro-interaction singleton,
    # + one shared lightweight hover animator per header chrome
    # button (ModalButton); those are the only permitted extras and
    # each must be parented to its button.
    singletons = {page._hover_anim, page._press_anim, page._section_anim,
                  page._pin_anim, page._cat_anim, page._hdr_anim,
                  page._cat_hover_anim}
    assert singletons <= set(exact_anims)
    from app.modal import ModalButton

    for anim in exact_anims:
        if anim in singletons:
            assert anim.parent() is page
        else:
            assert isinstance(anim.parent(), ModalButton)
    model = page._model
    idx = next(i for i in range(model.rowCount())
               if (r := model.row_at(i)) is not None
               and not r.is_header and not r.is_folder)
    pos = page._view.visualRect(model.index(idx, 0)).center()
    page._track_hover(pos)
    qtbot.waitUntil(lambda: page._hover_progress >= 1.0, timeout=5000)
    assert page._hover_id is not None
    page._clear_hover()
    qtbot.waitUntil(lambda: page._hover_id is None, timeout=5000)
    model2 = page._model
    for i in range(model2.rowCount()):
        assert page._view.indexWidget(model2.index(i, 0)) is None


def test_hover_never_underlines_title():
    import inspect

    from app.library.delegate import LibraryDelegate

    src = inspect.getsource(LibraryDelegate._paint_titles)
    assert "underline" not in src.lower()
    # M4.6.6: the title pen never changes with state — selection is
    # carried by the thumbnail ring alone (see _draw_thumb).
    assert "accent" not in src.lower()


# -- Fullscreen + startup -------------------------------------------------------------

def test_fullscreen_transition_and_esc(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    window.toggle_fullscreen()
    # M4.6.6 two-phase transition: state flips in phase 2 (~95ms).
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    assert window.titlebar.isVisible() is False
    qtbot.waitUntil(lambda: window._stack.graphicsEffect() is None, timeout=5000)
    qtbot.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    assert window.titlebar.isVisible() is True
    qtbot.waitUntil(lambda: window._stack.graphicsEffect() is None, timeout=5000)
    window.close_document()


def test_startup_entrance_immediate_usability(shell, tmp_path):
    window, service = shell
    assert getattr(window, "_startup_played", False) is True
    # Usable while the entrance is still playing (or just played).
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    window.close_document()


# -- Withdraw memo (freeze fix) ----------------------------------------------------------

def test_withdraw_skipped_when_viewport_unchanged(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=6))
    assert window.open_library_item(item.id) is True
    viewer = window.viewer
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    pipe = viewer._pipeline
    calls = []
    orig = pipe._proc.withdraw
    pipe._proc.withdraw = lambda ids: (calls.append(set(ids)), orig(ids))[1]
    try:
        vw, vh = viewer._render_viewport()
        pipe.update(viewer._scroll_doc(), int(vw), int(vh))
        pipe.update(viewer._scroll_doc(), int(vw), int(vh))
        assert len(calls) <= 1  # second identical viewport skips withdraw
        viewer._scrollbar.setValue(viewer._scrollbar.maximum())
        qtbot.wait(500)
        assert len(calls) >= 2  # real movement still withdraws
    finally:
        pipe._proc.withdraw = orig
    window.close_document()


# -- Direction regression ------------------------------------------------------------------

def test_random_page_up_down_no_stall(shell, tmp_path, qtbot):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent

    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, "big.pdf", pages=20))
    assert window.open_library_item(item.id) is True
    viewer = window.viewer
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    pipe = viewer._pipeline
    bar = viewer._scrollbar
    assert bar.maximum() > 2000

    def wheel(dy):
        pos = QPoint(100, 400)
        return QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, dy),
                           Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier,
                           Qt.ScrollPhase.NoScrollPhase, False)

    def arm(direction, ticks=8):
        import time as _t

        from PySide6.QtWidgets import QApplication

        updates = []
        orig = pipe.update

        def counting(scroll, vw, vh, *args, **kwargs):
            updates.append(round(float(scroll), 1))
            return orig(scroll, vw, vh, *args, **kwargs)

        pipe.update = counting
        try:
            bar.setValue(bar.maximum() // 2)
            qtbot.wait(1200)  # settle on the jumped page
            e0 = {k: v for k, v in pipe.stats()["engine"].items()
                  if isinstance(v, (int, float))}
            hb = []
            for _ in range(ticks):
                QApplication.sendEvent(viewer, wheel(direction * -240))
                # Fine-grained pump: genuine event-loop stall detector.
                t_end = _t.time() + 0.15
                last = _t.time()
                while _t.time() < t_end:
                    QApplication.processEvents()
                    now = _t.time()
                    hb.append(now - last)
                    last = now
                    _t.sleep(0.005)
            qtbot.wait(800)
            e1 = {k: v for k, v in pipe.stats()["engine"].items()
                  if isinstance(v, (int, float))}
            submitted = e1.get("submitted", 0) - e0.get("submitted", 0)
            return max(hb) * 1000, len(updates), submitted
        finally:
            pipe.update = orig

    down_hb, down_n, down_sub = arm(+1)
    up_hb, up_n, up_sub = arm(-1)
    assert down_hb < 100, down_hb
    assert up_hb < 100, up_hb  # no stall either direction (was: UP freeze)
    assert up_sub < 200 and down_sub < 200  # bounded render work
    assert up_n > 0 and down_n > 0
    window.close_document()
