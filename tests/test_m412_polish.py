"""M4.6.12 compact headless invariants.

Covers ONLY the architectural/performance invariants (all pass under
QT_QPA_PLATFORM=offscreen from tests/conftest.py — no visible windows):
occurrence hover identity, fixed 180px cards, zero renders on
hover/resize, top-edge hit testing, scrollbar resize veto (cursor +
press share _resize_edge_at), QPainter-zero, cheap theme/modal state,
plus the M4.6.12 visual constants (wash, padding, count/chevron slots,
directional easing).
"""

import pytest
from PySide6.QtCore import QEasingCurve, QPoint

from app.library.delegate import card_width_for, folder_tile_layout
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
    try:
        window._theme.set_mode("dark")
    except Exception:
        pass
    window.show()  # offscreen: renders to memory, never visible
    qtbot.addWidget(window)
    qtbot.wait(200)
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


class _FakeIndex:
    def __init__(self, row):
        self._row = row

    def row(self):
        return self._row


# -- occurrence identity ------------------------------------------------------

def test_occurrence_identity(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/dup.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    rows = [(r, it) for (r, it) in _doc_rows(page) if it.id == item.id]
    assert rows
    r0, it0 = rows[0]
    page._hover_key = (r0, it0.id)
    page._hover_id = it0.id
    page._hover_progress = 1.0
    try:
        assert page._delegate._hover_progress_for(it0, page._model.index(r0, 0)) == 1.0
        assert page._delegate._hover_progress_for(it0, _FakeIndex(r0 + 1000)) == 0.0
    finally:
        page._hover_key = None
        page._hover_id = None
        page._hover_progress = 0.0


def test_retarget_animates_from_current(shell):
    window, _ = shell
    page = window.library_page
    page._hover_progress = 0.4
    page._hover_glide_to(1.0)
    try:
        assert page._hover_anim.startValue() == 0.4
        assert page._hover_anim.endValue() == 1.0
        assert page._hover_anim.easingCurve().type() == QEasingCurve.Type.OutQuart
        page._hover_glide_to(0.0)
        assert page._hover_anim.easingCurve().type() == QEasingCurve.Type.OutCubic
    finally:
        try:
            page._hover_anim.stop()
        except Exception:
            pass
        page._hover_progress = 0.0


# -- fixed geometry + M4.6.12 visual constants ---------------------------------

def test_fixed_180_cards():
    for usable in (600, 1214, 1374, 1864):
        assert card_width_for(usable, 6) == 180.0
    icon_x, text_x, text_w, elide_w = folder_tile_layout(170)
    assert icon_x == 16  # clears the wash edge
    assert text_x == 60  # text breathes from the LEFT tile edge
    assert text_w == 170 - 60 - 14
    assert elide_w == text_w - 4


def test_hover_wash_strength_bounds(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/w.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    r0, it0 = next((r, it) for (r, it) in _doc_rows(page) if it.id == item.id)
    page._hover_key = (r0, it0.id)
    page._hover_progress = 1.0
    try:
        lift, wash = page._delegate._hover_lift(it0, False, page._model.index(r0, 0))
        assert lift <= 2
        assert 24 / 255 <= wash <= 32 / 255  # moderate boost, never selection
    finally:
        page._hover_key = None
        page._hover_progress = 0.0


def test_titles_neutral():
    import inspect

    from app.library import delegate as _d

    src = inspect.getsource(_d.LibraryDelegate._paint_titles)
    assert "underline" not in src.lower()
    assert "accent" not in src.lower()


# -- zero-render invariants ----------------------------------------------------

def test_hover_causes_no_renders(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/h.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(300)
    base = window._thumbs.renders_started
    r0, it0 = next((r, it) for (r, it) in _doc_rows(page) if it.id == item.id)
    page._hover_key = (r0, it0.id)
    page._hover_progress = 1.0
    page._hover_repaint()
    qtbot.wait(150)
    page._hover_key = None
    page._hover_progress = 0.0
    page._hover_repaint()
    qtbot.wait(150)
    assert window._thumbs.renders_started == base


# -- resize hit-testing + scrollbar veto ---------------------------------------

def test_top_edge_and_corners_resize(shell):
    window, _ = shell
    from PySide6.QtCore import Qt

    w, h = window.width(), window.height()
    assert window._resize_edge_at(QPoint(w // 2, 2)) == Qt.Edge.TopEdge
    assert window._resize_edge_at(QPoint(w // 2, h // 2)) is None


def test_scrollbar_vetoes_resize(shell, qtbot):
    """Scrollbar pixels must not resize (cursor AND press share the helper)."""
    import inspect

    from PySide6.QtWidgets import QScrollBar

    window, _ = shell
    page = window.library_page
    qtbot.wait(200)
    bar = page._view.verticalScrollBar()
    assert isinstance(bar, QScrollBar)
    center = bar.mapTo(window, bar.rect().center())
    assert window._resize_edge_at(center) is None
    # Press path is gated on the same helper — verify structurally.
    src = inspect.getsource(window.mousePressEvent)
    assert "_resize_edge_at" in src and "startSystemResize" in src


# -- theme / modal cheap state --------------------------------------------------

def test_theme_roundtrip_cheap(shell):
    window, _ = shell
    try:
        window._theme.set_mode("light")
        assert window._theme.mode == "light"
    finally:
        window._theme.set_mode("dark")
    assert window._theme.mode == "dark"


def test_modal_focus_never_close():
    import inspect

    from app.modal import CenteredModal

    src = inspect.getsource(CenteredModal._focus_first)
    # Close button is explicitly skipped: first non-close focusable wins.
    assert "item is not close" in src


# -- QPainter safety ------------------------------------------------------------

def test_hover_resize_no_qpainter_warnings(shell, qtbot, tmp_path, capsys):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/q.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    rows = [(r, it) for (r, it) in _doc_rows(page) if it.id == item.id]
    if rows:
        r0, it0 = rows[0]
        page._hover_key = (r0, it0.id)
        page._hover_progress = 1.0
        page._hover_repaint()
        qtbot.wait(100)
        page._hover_key = None
        page._hover_progress = 0.0
        page._hover_repaint()
    window.resize(window.width() + 40, window.height())
    qtbot.wait(200)
    err = capsys.readouterr().err or ""
    assert "QPainter" not in err
