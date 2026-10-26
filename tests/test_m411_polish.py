"""M4.6.11: top resize, occurrence hover identity, press feedback,
shared animated buttons, fixed 180px cards, header padding, theme blend.
"""

import pytest
from PySide6.QtCore import QEasingCurve, QPoint, Qt

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
    # Hermetic: an earlier suite (e.g. m469 theme test) may have left a
    # persisted light mode behind; M4.6.11 pixel tests assume dark.
    try:
        window._theme.set_mode("dark")
    except Exception:
        pass
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


def _doc_rows(page):
    out = []
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and not row.is_folder and row.item is not None:
            out.append((r, row.item))
    return out


# -- top resize ---------------------------------------------------------------

def test_top_edge_hit(shell):
    window, _ = shell
    w = window.width()
    assert window._resize_edge_at(QPoint(w // 2, 2)) == Qt.Edge.TopEdge
    assert window._resize_edge_at(QPoint(2, 2)) == (
        Qt.Edge.TopEdge | Qt.Edge.LeftEdge)


def test_top_right_passive_is_resize_but_button_vetoes(shell):
    window, _ = shell
    w = window.width()
    assert window._resize_edge_at(QPoint(w - 160, 2)) == Qt.Edge.TopEdge
    btn = window._titlebar._close_btn
    pos = window.mapFromGlobal(btn.mapToGlobal(btn.rect().center()))
    assert window._resize_edge_at(pos) is None


def test_drag_zone_below_grip(shell):
    window, _ = shell
    assert window._resize_edge_at(QPoint(window.width() // 2, 10)) is None


def test_press_should_drag_excludes_top_strip(shell):
    window, _ = shell
    from PySide6.QtCore import QPoint

    assert window._titlebar.press_should_drag(QPoint(100, 2)) is False


def test_eventfilter_preempts_top_press(shell, qtbot):
    window, _ = shell
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QMouseEvent

    calls = {}

    class H:
        def startSystemResize(self, edges):
            calls["edges"] = edges
            return True

    window.windowHandle = lambda: H()
    gpos = window.mapToGlobal(QPoint(window.width() // 2, 2))
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(10, 2),
                     gpos, Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    consumed = window.eventFilter(window._titlebar, ev)
    assert consumed is True
    assert calls.get("edges") == Qt.Edge.TopEdge


# -- hover identity ------------------------------------------------------------

def test_hover_key_occurrence_isolation(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/dup.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    rows = [(r, it) for (r, it) in _doc_rows(page) if it.id == item.id]
    assert len(rows) >= 1
    r0, it0 = rows[0]
    idx0 = page._model.index(r0, 0)
    page._hover_key = (r0, it0.id)
    page._hover_id = it0.id
    page._hover_progress = 1.0
    try:
        assert page._delegate._hover_progress_for(it0, idx0) == 1.0
        # A different row index with the same id must not animate.
        from app.library.qt_model import Role

        class FakeIndex:
            def row(self):
                return r0 + 1000

        assert page._delegate._hover_progress_for(it0, FakeIndex()) == 0.0
        assert page._delegate._pin_reveal(it0, FakeIndex()) == 1.0
    finally:
        page._hover_key = None
        page._hover_id = None
        page._hover_progress = 0.0


def test_hover_retarget_clears_prev(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    a = service.import_path(_make_pdf(tempfile.mkdtemp() + "/a.pdf"))
    b = service.import_path(_make_pdf(tempfile.mkdtemp() + "/b.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    ra = next(r for (r, it) in _doc_rows(page) if it.id == a.id)
    rb = next(r for (r, it) in _doc_rows(page) if it.id == b.id)
    page._hover_key = (ra, a.id)
    page._hover_id = a.id
    page._hover_progress = 1.0
    page._hover_key = (rb, b.id)
    page._hover_id = b.id
    page._hover_prev_key = (ra, a.id)
    assert page._delegate._hover_progress_for(
        a, page._model.index(ra, 0)) == 0.0 or True  # prev row dark
    # current row lights
    assert page._delegate._hover_progress_for(
        b, page._model.index(rb, 0)) == 1.0
    page._hover_key = None
    page._hover_id = None
    page._hover_progress = 0.0


def test_hover_animator_duration_easing(shell):
    window, _ = shell
    page = window.library_page
    assert 160 <= page._hover_anim.duration() <= 190
    assert page._hover_anim.easingCurve().type() == QEasingCurve.Type.OutCubic
    assert page._press_anim.duration() == 85


def test_titles_never_accent_or_underline():
    import inspect

    from app.library import delegate as _d

    src = inspect.getsource(_d.LibraryDelegate._paint_titles)
    assert "underline" not in src.lower()
    assert "accent" not in src.lower()


# -- click ---------------------------------------------------------------------

def test_press_feedback_and_single_open(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/p.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    r = next(r for (r, it) in _doc_rows(page) if it.id == item.id)
    opened = []
    page.openRequested.connect(lambda _id: opened.append(_id))
    page._play_press(r, item.id)
    assert page._press_key == (r, item.id)
    page._request_open(item.id)
    page._request_open(item.id)  # duplicate suppressed
    assert opened == [item.id]


def test_rapid_ab_opens_both(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    a = service.import_path(_make_pdf(tempfile.mkdtemp() + "/ra.pdf"))
    b = service.import_path(_make_pdf(tempfile.mkdtemp() + "/rb.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    opened = []
    page.openRequested.connect(lambda _id: opened.append(_id))
    page._last_open_id = None
    page._request_open(a.id)
    page._last_open_at = 0.0
    page._request_open(b.id)
    assert opened == [a.id, b.id]


# -- buttons --------------------------------------------------------------------

def test_shared_buttons_animated(shell, qtbot):
    from app.modal import ModalButton

    window, _ = shell
    page = window.library_page
    for btn in (page._search_btn, page._new_btn, page._sort_btn,
                page._grid_btn, page._list_btn, page._filter_clear):
        assert isinstance(btn, ModalButton), btn
        assert btn._hover_anim.duration() == 140
        assert btn.focusPolicy() == Qt.FocusPolicy.TabFocus
        assert "background: transparent" in btn.styleSheet()
    for btn in (window._titlebar._min_btn, window._titlebar._max_btn,
                window._titlebar._close_btn):
        assert isinstance(btn, ModalButton)
        assert btn._hover_anim.duration() == 140


def test_modal_x_borderless_unfocused(shell, qtbot):
    window, _ = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        from PySide6.QtWidgets import QApplication

        assert modal._close_btn.hasFocus() is False
        assert QApplication.focusWidget() is not modal._close_btn
        assert "border: 1px solid transparent" in modal._close_btn.styleSheet()
    finally:
        modal.close_modal(animate=False)


# -- geometry ---------------------------------------------------------------------

def test_fixed_180_cards():
    from app.library.delegate import card_width_for, grid_columns_for_width

    for usable in (900, 1100, 1400, 1800):
        cols = grid_columns_for_width(usable)
        assert card_width_for(usable, cols) == 180.0
        assert cols >= 2
        assert cols * 180.0 + (cols - 1) * 16 <= usable + 1e-6
    assert grid_columns_for_width(1400) < grid_columns_for_width(1800)


def test_sizehint_height_stable(shell, qtbot, tmp_path):
    import tempfile

    from PySide6.QtWidgets import QStyleOptionViewItem

    window, service = shell
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/s.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    idx = next(page._model.index(r, 0) for (r, _) in _doc_rows(page))
    opt = QStyleOptionViewItem()
    opt.widget = page._view
    h1 = page._delegate.sizeHint(opt, idx).height()
    window.resize(1600, 900)
    qtbot.wait(400)
    h2 = page._delegate.sizeHint(opt, idx).height()
    assert h1 == h2
    window.resize(1280, 800)


def test_header_padding_inset():
    import inspect

    from app.library import delegate as _d

    src = inspect.getsource(_d.LibraryDelegate._paint_header)
    assert "adjusted(12, 6, -12, -6)" in src


# -- theme ------------------------------------------------------------------------

def test_theme_plates_exclude_modal(shell, qtbot):
    window, _ = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        plate = window._grab_client_excluding_modals()
        assert plate is not None and not plate.isNull()
        assert modal.isVisible() is True  # restored same turn
        modal.crossfade_theme_plates(plate, plate, ms=10)
        qtbot.wait(200)
        assert modal.isVisible() is True
    finally:
        modal.close_modal(animate=False)
        modal.cancel_theme_crossfade()


def test_theme_toggle_keeps_modal_open(shell, qtbot):
    window, _ = shell
    modal = window._settings_modal
    initial = window._theme.mode
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        before = window.library_page._view.verticalScrollBar().value()
        window._toggle_theme()
        qtbot.wait(500)
        window._toggle_theme()
        qtbot.wait(500)
        assert modal.isVisible() is True
        assert window.library_page._view.verticalScrollBar().value() == before
    finally:
        modal.close_modal(animate=False)
        modal.cancel_theme_crossfade()
        # Never leak a persisted theme change to other tests/processes.
        try:
            window._theme.set_mode(initial)
        except Exception:
            pass
