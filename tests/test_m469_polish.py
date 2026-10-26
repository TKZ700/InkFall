"""M4.6.9: QPainter safety, exact geometry, 4-side resize, header
hover, smooth PDF hover, modal controls. All animations stay
view-level; no per-card/per-category widgets.
"""

import pytest
from PySide6.QtCore import Qt

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
    qtbot.wait(600)
    yield window, service
    try:
        window.close()
    except Exception:
        pass
    store.close()


def _make_pdf(tmp_path, name="doc.pdf", pages=2):
    import os

    from tests.helpers import make_rich_pdf

    base = str(tmp_path)
    path = os.path.join(base, name) if os.path.isdir(base) else base
    return make_rich_pdf(path, pages=pages)


def _seed(service, tmp_path, n, prefix="m469"):
    import tempfile

    tmp = tempfile.mkdtemp()
    return [service.import_path(_make_pdf(f"{tmp}/{prefix}{i}.pdf")).id
            for i in range(n)]


def _doc_ids(page):
    page.refresh()
    return [row.item.id for r in range(page._model.rowCount())
            if (row := page._model.row_at(r)) is not None
            and not row.is_folder and row.item is not None]


def _row_center(page, item_id):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if (row is not None and not row.is_folder and row.item is not None
                and row.item.id == item_id):
            return page._view.visualRect(page._model.index(r, 0)).center()
    raise AssertionError(f"row for {item_id} not found")


def _header_center(page, key):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if (row is not None and row.is_header and row.header is not None
                and row.header.key == key):
            return page._view.visualRect(page._model.index(r, 0)).center()
    raise AssertionError(f"header {key} not found")


# -- QPainter hard gate --------------------------------------------------------

def test_qpainter_storm_no_warnings(shell, qtbot, tmp_path, capfd):
    window, service = shell
    _seed(service, tmp_path, 3)
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(f"{tmp_path}/w.pdf"), folder.id)
    page = window.library_page
    window._settings_modal.open_modal(window.settings_fab)
    qtbot.wait(150)
    window.resize(1340, 800)
    qtbot.wait(150)
    window._settings_modal.close_modal()
    qtbot.wait(150)
    window._new_modal.open_modal(page._new_btn)
    qtbot.wait(150)
    window.resize(1280, 800)
    qtbot.wait(150)
    window._new_modal.close_modal(animate=False)
    qtbot.wait(100)
    page.toggle_section("documents")
    qtbot.wait(150)
    page.toggle_section("documents")
    qtbot.wait(150)
    page.set_list_mode(True)
    qtbot.wait(150)
    page.set_list_mode(False)
    qtbot.wait(150)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    out, err = capfd.readouterr()
    assert "QPainter" not in err
    assert "not active" not in err


# -- exact geometry --------------------------------------------------------------

def test_normal_geom_tracked_on_resize(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.resize(1000, 700)
    qtbot.wait(300)
    assert window._normal_geom is not None
    assert window._normal_geom.getRect() == window.geometry().getRect()


def test_normal_geom_frozen_while_maximized(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.resize(1000, 700)
    qtbot.wait(300)
    saved = window._normal_geom.getRect()
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    assert window._normal_geom.getRect() == saved
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)


def test_maximize_restore_exact_rect(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.resize(1000, 700)
    qtbot.wait(300)
    before = window.geometry().getRect()
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.geometry().getRect() == before, timeout=5000)


def test_fullscreen_restore_exact_rect(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.resize(1024, 720)
    qtbot.wait(300)
    before = window.geometry().getRect()
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.geometry().getRect() == before, timeout=5000)


def test_max_then_fullscreen_restores_maximized(shell, qtbot):
    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)


def test_resize_then_maximize_restores_resized(shell, qtbot):
    window, _service = shell
    window.showNormal()
    window.resize(1100, 740)
    qtbot.wait(300)
    before = window.geometry().getRect()
    assert (before[2], before[3]) == (1100, 740)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.geometry().getRect() == before, timeout=5000)


def test_tween_abort_on_user_press(shell, qtbot):
    from PySide6.QtCore import QPoint, QPointF, QRect
    from PySide6.QtGui import QMouseEvent

    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    # Drive the shared geometry-tween helper directly (fullscreen entry
    # uses the same path): a tween is in flight...
    window._tween_window_geometry(QRect(0, 0, 800, 600), 400, lambda: None)
    assert window._tweening_geom is True
    assert window._geom_anim is not None
    pos = QPoint(2, window.height() // 2)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(pos),
                        QPointF(window.mapToGlobal(pos)),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    window.mousePressEvent(event)  # user grabs a grip mid-tween
    assert window._tweening_geom is False
    try:
        assert window._geom_anim is None or not window._geom_anim.isRunning()
    finally:
        window.showNormal()
        qtbot.wait(200)


def test_statechange_aborts_tween(shell, qtbot):
    from PySide6.QtCore import QRect

    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window._tween_window_geometry(QRect(0, 0, 800, 600), 400, lambda: None)
    assert window._tweening_geom is True
    window.showMaximized()  # external crossing (Snap/OS)
    qtbot.wait(200)
    assert window._tweening_geom is False
    window.showNormal()
    qtbot.wait(200)


# -- four-side resize --------------------------------------------------------------

def test_top_edge_and_corners(shell):
    from PySide6.QtCore import QPoint

    window, _service = shell
    window.showNormal()
    w, h = window.width(), window.height()
    assert window._resize_edge_at(QPoint(w // 2, 2)) == Qt.Edge.TopEdge
    assert window._resize_edge_at(QPoint(2, 2)) == (
        Qt.Edge.TopEdge | Qt.Edge.LeftEdge)
    # Top-right corner yields to the close button (correct veto: the
    # button must always win the pixels it owns).
    bar = window.titlebar
    tr_btn = bar.mapTo(window, bar._close_btn.pos() + QPoint(2, 2))
    assert window._resize_edge_at(tr_btn) is None
    assert window._resize_edge_at(QPoint(w - 160, 2)) == Qt.Edge.TopEdge
    assert window._resize_edge_at(QPoint(2, h - 2)) == (
        Qt.Edge.LeftEdge | Qt.Edge.BottomEdge)
    assert window._resize_edge_at(QPoint(w - 2, h - 2)) == (
        Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
    assert window._resize_edge_at(QPoint(w // 2, 10)) is None  # drag zone kept
    assert window._resize_edge_at(QPoint(2, h // 2)) == Qt.Edge.LeftEdge
    window.showMaximized()
    assert window._resize_edge_at(QPoint(w // 2, 2)) is None
    window.showNormal()


def test_resize_cursors_top_and_corners(shell):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QMouseEvent

    def _move(pos):
        event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(pos),
                            QPointF(window.mapToGlobal(pos)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        window.mouseMoveEvent(event)

    window, _service = shell
    window.showNormal()
    w, h = window.width(), window.height()
    _move(QPoint(w // 2, 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeVerCursor
    _move(QPoint(2, 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
    # Top-right sits under the close button: veto -> Arrow (correct).
    _move(QPoint(w - 2, 2))
    assert window.cursor().shape() == Qt.CursorShape.ArrowCursor
    _move(QPoint(2, h // 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeHorCursor
    _move(QPoint(w // 2, h // 2))
    window.unsetCursor()


def test_interactive_veto_on_top_strip(shell):
    from PySide6.QtCore import QPoint

    window, _service = shell
    window.showNormal()
    bar = window.titlebar
    btn_pos = bar.mapTo(window, bar._close_btn.pos() + QPoint(2, 2))
    assert btn_pos.y() <= 6  # the button reaches into the grip strip
    assert window._resize_edge_at(btn_pos) is None  # but clicks still win


# -- section header hover ------------------------------------------------------------

def test_header_hover_anim_state(shell, qtbot, tmp_path):
    window, service = shell
    _seed(service, tmp_path, 2)
    page = window.library_page
    assert page._hdr_anim.duration() == 140
    center = _header_center(page, "documents")
    page._track_header_hover(center)
    assert page._hdr_anim_key == "documents"
    qtbot.waitUntil(lambda: page._hdr_progress >= 1.0, timeout=5000)
    assert page._hdr_anim_key == "documents"


def test_header_hover_clears_on_leave(shell, qtbot, tmp_path):
    window, service = shell
    _seed(service, tmp_path, 2)
    page = window.library_page
    page._track_header_hover(_header_center(page, "documents"))
    qtbot.waitUntil(lambda: page._hdr_progress >= 1.0, timeout=5000)
    page._clear_header_hover()
    qtbot.waitUntil(lambda: page._hdr_anim_key is None, timeout=5000)
    assert page._hdr_progress == 0.0


def test_header_press_and_lit_branches():
    import inspect

    from app.library import delegate as _d

    src = inspect.getsource(_d.LibraryDelegate._paint_header)
    assert "_pressed(option)" in src  # 1-2% compact press
    assert "hover_p > 0.5" in src  # lit chevron + brighter count


def test_collapse_after_hover_integration(shell, qtbot, tmp_path):
    window, service = shell
    _seed(service, tmp_path, 2)
    page = window.library_page
    page._track_header_hover(_header_center(page, "documents"))
    qtbot.wait(200)
    before = page._model.rowCount()
    page.toggle_section("documents")
    qtbot.wait(300)
    assert page._model.rowCount() < before
    page.toggle_section("documents")
    qtbot.wait(300)
    assert page._model.rowCount() == before


# -- PDF hover --------------------------------------------------------------------------

def test_hover_duration_170(shell):
    window, _service = shell
    assert window.library_page._hover_anim.duration() == 170


def test_hover_retarget_from_current(shell, qtbot, tmp_path):
    window, service = shell
    ids = _seed(service, tmp_path, 2)
    page = window.library_page
    center_b = _row_center(page, ids[1])  # resolve first (refreshes)
    page._hover_id = ids[0]
    page._hover_progress = 0.6
    page._track_hover(center_b)
    assert page._hover_id == ids[1]  # immediate swap, no stuck state
    assert page._hover_prev_id == ids[0]  # outgoing keeps repainting
    assert page._hover_anim.startValue() == 0.6  # resumes, never restarts


def test_hover_prev_clears_on_done(shell, qtbot, tmp_path):
    window, service = shell
    ids = _seed(service, tmp_path, 1)
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    page._track_hover(_row_center(page, ids[0]))
    qtbot.waitUntil(lambda: page._hover_progress >= 1.0, timeout=5000)
    page._clear_hover()
    qtbot.waitUntil(lambda: page._hover_progress <= 0.0, timeout=5000)
    qtbot.waitUntil(lambda: page._hover_prev_id is None, timeout=5000)


def test_titles_never_underline_or_accent():
    import inspect

    from app.library import delegate as _d

    src = inspect.getsource(_d)
    assert "Underline" not in src
    assert "_ = hover" in inspect.getsource(_d.LibraryDelegate._paint_titles)


def test_pin_reveal_factor(shell, qtbot, tmp_path):
    window, service = shell
    ids = _seed(service, tmp_path, 1)
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    item = service.get_item(ids[0])
    assert page._delegate._pin_reveal(item) == 1.0  # no hover: full
    page._hover_id = ids[0]
    page._hover_progress = 0.5
    assert page._delegate._pin_reveal(item) == 0.5  # fades in with hover


def test_list_row_press_branch():
    import inspect

    from app.library import delegate as _d

    assert "_pressed(option)" in inspect.getsource(_d.LibraryDelegate._paint_row)


# -- categories (verify + lock) ----------------------------------------------------------------

def test_category_hover_never_lifts(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"), folder.id)
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and row.is_folder:
            pos = page._view.visualRect(page._model.index(r, 0)).center()
            assert page._hover_item_id_at(pos) is None  # tiles never lift
            return
    raise AssertionError("no folder row")


def test_category_anim_bounds(shell):
    window, _service = shell
    page = window.library_page
    assert page._cat_anim.duration() == 130
    page._cat_anim_id = "probe"
    page._cat_progress = 0.5
    compact = page._cat_compact("probe")
    assert 0.97 <= compact < 1.0  # subtle 1-3% compression
    page._cat_anim_id = None
    page._cat_progress = 1.0


# -- New modal rows -------------------------------------------------------------------------------

def test_row_hover_progress_and_icon_swap(shell, qtbot):
    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    row = modal._rows[0]
    assert row._hover_anim.duration() == 140
    row._hover_glide_to(1.0)
    qtbot.waitUntil(lambda: row._hover_p >= 1.0, timeout=5000)
    assert row._hover_lit is True  # icon swapped muted -> accent
    row._hover_glide_to(0.0)
    qtbot.waitUntil(lambda: row._hover_p <= 0.0, timeout=5000)
    assert row._hover_lit is False
    modal.close_modal(animate=False)


def test_row_keyboard_press_dip(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    row = modal._rows[0]
    try:
        row.activated.disconnect()  # dip only, no dialog path
    except Exception:
        pass
    QTest.keyClick(row, Qt.Key.Key_Return)
    assert row.graphicsEffect() is not None  # keyboard press answers too
    modal.close_modal(animate=False)


def test_row_no_native_rect(shell, qtbot):
    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    row = modal._rows[0]
    assert row.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert "outline: 0" in row.styleSheet()
    assert ":hover" not in row.styleSheet()  # painted progress instead
    modal.close_modal(animate=False)


def test_close_btn_borderless_pressed(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    btn = modal._close_btn
    # M4.6.10: transparent idle border (borderless render, no geometry
    # shift); hover/press are painted ModalButton progress.
    assert "border: 1px solid transparent" in btn.styleSheet()
    assert "border-radius: 16px" in btn.styleSheet()  # circular wash
    assert btn._hover_anim.duration() == 140
    btn._hover_glide_to(1.0)
    qtbot.waitUntil(lambda: btn._hover_p >= 1.0, timeout=5000)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton)  # press only: no close
    assert btn.graphicsEffect() is not None
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    modal.close_modal(animate=False)


def test_settings_seg_hover_press(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    btn = modal._mode_buttons[next(iter(modal._mode_buttons))]
    # M4.6.10 ModalButton: painted hover (no instant QSS switch).
    assert "border: 1px solid transparent" in btn.styleSheet()
    assert btn._hover_anim.duration() == 140
    btn._hover_glide_to(1.0)
    qtbot.waitUntil(lambda: btn._hover_p >= 1.0, timeout=5000)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton)
    assert btn.graphicsEffect() is not None
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    modal.close_modal(animate=False)


def test_settings_nav_hover_press(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    btn = modal._nav_buttons[0]
    assert "border: 1px solid transparent" in btn.styleSheet()
    assert btn._hover_anim.duration() == 140
    btn._hover_glide_to(1.0)
    qtbot.waitUntil(lambda: btn._hover_p >= 1.0, timeout=5000)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton)
    assert btn.graphicsEffect() is not None
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    modal.close_modal(animate=False)


def test_modal_close_resets_rows(shell, qtbot):
    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    row = modal._rows[0]
    row._hover_glide_to(1.0)
    qtbot.waitUntil(lambda: row._hover_p >= 1.0, timeout=5000)
    modal.close_modal()
    qtbot.wait(400)
    assert row._hover_p == 0.0
    assert row._hover_lit is False


# -- modal preservation -----------------------------------------------------------------------------

def test_modal_maximize_recapture(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    window.showMaximized()
    qtbot.wait(500)
    assert modal.isVisible() is True
    assert modal._plate_valid() is True
    window.showNormal()
    qtbot.wait(400)


def test_modal_theme_change_stable(shell, qtbot):
    from app.theme.manager import ThemeMode

    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    current = window._theme.mode
    target = ThemeMode.LIGHT if current != ThemeMode.LIGHT else ThemeMode.DARK
    window._theme.set_mode(target)
    qtbot.wait(400)
    assert modal.isVisible() is True
    assert modal._plate_valid() is True
    modal.close_modal()
    qtbot.wait(400)


# -- render invariants ---------------------------------------------------------------------------------

def test_geom_ops_zero_renders(shell, qtbot, tmp_path):
    import time

    import tempfile

    from app.library.qt_model import Role

    window, service = shell
    tmp = tempfile.mkdtemp()
    for i in range(12):
        service.import_path(_make_pdf(f"{tmp}/g{i}.pdf"))
    page = window.library_page
    page.refresh()
    model = page._model
    bar = page._view.verticalScrollBar()

    def ready():
        return sum(1 for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None
                   and not r.is_header and not r.is_folder
                   and model.data(model.index(i, 0), Role.THUMB) is not None)

    t_end = time.time() + 30000
    while ready() < 12 and time.time() < t_end:
        bar.setValue(bar.maximum())
        qtbot.wait(300)
        bar.setValue(0)
        qtbot.wait(300)
    assert ready() >= 12
    base = window._thumbs.renders_started
    qtbot.wait(600)
    assert window._thumbs.renders_started == base
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.wait(1000)
    assert window._thumbs.renders_started == base  # +0 across window ops
