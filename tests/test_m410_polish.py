"""M4.6.10: category tile containment, card breathing room, settings
alignment, ModalButton controls, sort direction, resize zones/cursors.
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


NAMES = [
    "A",
    "Work documents here",
    "A very long category name that must elide cleanly every time",
    "اسناد کاری",
    "Chapter 3 - فصل اول mixed 123",
]


# -- category tile geometry ---------------------------------------------------------

def test_folder_tile_layout_invariants():
    from app.library.delegate import folder_tile_layout

    for width in (160, 170, 190, 210, 220):
        icon_x, text_x, text_w, elide_w = folder_tile_layout(width)
        assert icon_x == 16  # M4.6.12: icon clears the wash edge
        assert icon_x + 30 <= text_x - 14  # icon-to-text gap
        assert text_x >= 10
        assert text_x + text_w + 14 <= width  # 14px right inset
        assert elide_w == text_w - 4  # ink stops short of the rect


def test_folder_elision_contains_names():
    from PySide6.QtGui import QFontMetrics

    from app.library.delegate import _elided, folder_tile_layout
    from app.theme.tokens import dark_tokens
    from app.theme.typography import role_font

    font = role_font(dark_tokens(), "doc_title")
    metrics = QFontMetrics(font)
    for width in (160, 190, 220):
        _ix, _tx, _tw, elide_w = folder_tile_layout(width)
        for name in NAMES:
            out = _elided(font, name, elide_w)
            assert metrics.horizontalAdvance(out) <= elide_w + 2, (name, width)


def test_folder_hover_anim_state(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"), folder.id)
    page = window.library_page
    assert page._cat_hover_anim.duration() == 140
    page._track_folder_hover(_folder_center(page, folder.id))
    assert page._cat_hover_key == folder.id
    qtbot.waitUntil(lambda: page._cat_hover_p >= 1.0, timeout=5000)


def _folder_center(page, folder_id):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if (row is not None and row.is_folder and row.folder is not None
                and row.folder.id == folder_id):
            return page._view.visualRect(page._model.index(r, 0)).center()
    raise AssertionError("no folder row")


def test_folder_hover_retarget_clears_old(shell, qtbot, tmp_path):
    import tempfile

    window, service = shell
    fa = service.create_folder("Alpha")
    fb = service.create_folder("Beta Long Name Here")
    tmp = tempfile.mkdtemp()
    service.import_path(_make_pdf(f"{tmp}/a.pdf"), fa.id)
    service.import_path(_make_pdf(f"{tmp}/b.pdf"), fb.id)
    page = window.library_page
    ca = _folder_center(page, fa.id)
    cb = _folder_center(page, fb.id)  # refreshes; state set after
    page._track_folder_hover(ca)
    qtbot.waitUntil(lambda: page._cat_hover_p >= 1.0, timeout=5000)
    page._track_folder_hover(cb)  # A -> B: B takes over, A repaints clean
    assert page._cat_hover_key == fb.id
    qtbot.waitUntil(lambda: page._cat_hover_p >= 1.0, timeout=5000)
    page._clear_folder_hover()
    qtbot.waitUntil(lambda: page._cat_hover_key is None, timeout=5000)
    assert page._cat_hover_p == 0.0


def test_folder_tile_painted_inset(shell, qtbot, tmp_path):
    import tempfile

    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    window, service = shell
    folder = service.create_folder("A very long category name for paint")
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"), folder.id)
    page = window.library_page
    page.refresh()
    qtbot.wait(200)
    idx = None
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if (row is not None and row.is_folder and row.folder is not None
                and row.folder.id == folder.id):
            idx = page._model.index(r, 0)
            break
    assert idx is not None
    cell = page._view.visualRect(idx)
    # Full hover: wash + border + lit icon all painted.
    page._cat_hover_key = folder.id
    page._cat_hover_p = 1.0
    try:
        img = QImage(cell.width(), 64, QImage.Format.Format_ARGB32)
        img.fill(0)
        opt = QStyleOptionViewItem()
        opt.rect = page._view.visualRect(idx).translated(-cell.x(), -cell.y())
        opt.widget = page._view
        painter = QPainter(img)
        try:
            page._delegate.paint(painter, opt, idx)
        finally:
            painter.end()
        painted = [x for x in range(img.width())
                   if any(img.pixelColor(x, y).alpha() > 0 for y in range(img.height()))]
        assert painted, "tile must paint"
        assert min(painted) >= 4  # outer inset from the cell
        assert max(painted) <= img.width() - 4
        bright = [x for x in range(img.width()) for y in range(img.height())
                  if img.pixelColor(x, y).lightness() > 150]
        assert bright, "text must paint"
        # Text ink lives inside the tile with internal padding.
        assert min(bright) >= min(painted) + 10
        assert max(bright) <= max(painted) - 10
    finally:
        page._cat_hover_key = None
        page._cat_hover_p = 0.0


# -- PDF cards -----------------------------------------------------------------------------

def test_card_content_inset_and_shadow_source():
    import inspect

    from app.library import delegate as _d

    card = inspect.getsource(_d.LibraryDelegate._paint_card)
    assert "adjusted(8, 0, -8, 0)" in card  # content lives inside the wash
    assert "rect.right() - 24" in card  # pin inside the interaction area
    hint = inspect.getsource(_d.LibraryDelegate.sizeHint)
    assert "(card_w - 16)" in hint  # cell matches painted content


def test_card_paint_stays_in_item(shell, qtbot, tmp_path):
    import tempfile

    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(500)
    idx = None
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if (row is not None and not row.is_folder and row.item is not None
                and row.item.id == item.id):
            idx = page._model.index(r, 0)
            break
    assert idx is not None
    cell = page._view.visualRect(idx)
    page._hover_id = item.id
    page._hover_progress = 1.0  # full lift + shadow + pin
    try:
        img = QImage(cell.width() + 16, cell.height() + 16,
                     QImage.Format.Format_ARGB32)
        img.fill(0)
        opt = QStyleOptionViewItem()
        opt.rect = page._view.visualRect(idx).translated(
            -cell.x() + 8, -cell.y() + 8)
        opt.widget = page._view
        painter = QPainter(img)
        try:
            page._delegate.paint(painter, opt, idx)
        finally:
            painter.end()
        left, top = 8, 8
        right, bottom = 8 + cell.width(), 8 + cell.height()
        for x in range(img.width()):
            for y in range(img.height()):
                if img.pixelColor(x, y).alpha() > 0:
                    assert left <= x <= right, (x, y)  # no lateral escape
        # Vertical: lift moves up within the cell's own top inset.
        for x in range(img.width()):
            for y in range(img.height()):
                if img.pixelColor(x, y).alpha() > 0:
                    assert top - 3 <= y <= bottom, (x, y)
    finally:
        page._hover_id = None
        page._hover_progress = 0.0


# -- settings alignment + controls ------------------------------------------------------------------

def test_seg_equal_geometry(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        btns = list(modal._mode_buttons.values())
        assert len(btns) == 3
        # Equal heights/baselines (content-driven uniform geometry —
        # a FixedHeight would fight Qt's hint-tracked minimum).
        assert len({b.height() for b in btns}) == 1
        assert {b.pos().y() for b in btns} == {btns[0].pos().y()}
        widths = {b.width() for b in btns}
        assert max(widths) - min(widths) <= 1  # equal widths via stretch
        fonts = {(b.font().family(), b.font().pointSize()) for b in btns}
        assert len(fonts) == 1  # same baseline
    finally:
        modal.close_modal(animate=False)


def test_seg_nav_transparent_idle_border(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        from app.modal import ModalButton

        for btn in list(modal._mode_buttons.values()) + modal._nav_buttons:
            assert isinstance(btn, ModalButton)
            assert "border: 1px solid transparent" in btn.styleSheet()
            assert btn._hover_anim.duration() == 140
    finally:
        modal.close_modal(animate=False)


def test_modalbutton_hover_progress(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    try:
        btn = modal._nav_buttons[0]
        btn._hover_glide_to(1.0)
        qtbot.waitUntil(lambda: btn._hover_p >= 1.0, timeout=5000)
        btn._hover_glide_to(0.0)
        qtbot.waitUntil(lambda: btn._hover_p <= 0.0, timeout=5000)
    finally:
        modal.close_modal(animate=False)


def test_close_x_rendered_borderless_and_hover(shell, qtbot):
    from PySide6.QtGui import QColor

    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(500)
    try:
        from app.modal import ModalButton

        btn = modal._close_btn
        assert isinstance(btn, ModalButton)
        assert btn._circular is True
        # Defocus X so no focus ring paints (keyboard-only ring check).
        modal._rows[0].setFocus()
        qtbot.wait(200)
        t = window._theme.tokens
        fill = QColor(t.surface_raised)
        # The panel fill is intentionally unveiled (readable panel over
        # dimmed Library): idle X pixels must equal it exactly.
        expect = (fill.red(), fill.green(), fill.blue())

        def sample(qpoint):
            return modal.grab().toImage().pixel(
                btn.mapTo(modal, qpoint).x(), btn.mapTo(modal, qpoint).y())

        from PySide6.QtCore import QPoint

        idle_edge = QColor(sample(QPoint(16, 0)))
        assert (idle_edge.red(), idle_edge.green(), idle_edge.blue()) == expect, \
            (idle_edge.getRgb(), expect)  # borderless: edge == panel fill
        btn._hover_glide_to(1.0)
        qtbot.waitUntil(lambda: btn._hover_p >= 1.0, timeout=5000)
        hover_mid = QColor(sample(QPoint(5, 26)))
        want = QColor(t.surface_overlay)
        assert (hover_mid.red(), hover_mid.green(), hover_mid.blue()) == \
            (want.red(), want.green(), want.blue())  # circular wash renders
    finally:
        modal.close_modal(animate=False)


# -- sort direction ------------------------------------------------------------------------------

def test_sort_entrance_top_down():
    import inspect

    from app.library import library_page as _lp

    src = inspect.getsource(_lp.LibraryPage._on_sort_menu)
    assert "bottomLeft" in src  # anchor unchanged
    assert "-6" in src  # enters from above, settles to anchor


# -- resize zones + cursors ---------------------------------------------------------------------------

def test_all_eight_zones(shell):
    from PySide6.QtCore import QPoint

    E = Qt.Edge
    window, _service = shell
    window.showNormal()
    w, h = window.width(), window.height()
    assert window._resize_edge_at(QPoint(2, h // 2)) == E.LeftEdge
    assert window._resize_edge_at(QPoint(w - 2, h // 2)) == E.RightEdge
    assert window._resize_edge_at(QPoint(w // 2, 2)) == E.TopEdge
    assert window._resize_edge_at(QPoint(w // 2, h - 2)) == E.BottomEdge
    assert window._resize_edge_at(QPoint(2, 2)) == (E.TopEdge | E.LeftEdge)
    assert window._resize_edge_at(QPoint(2, h - 2)) == (E.LeftEdge | E.BottomEdge)
    assert window._resize_edge_at(QPoint(w - 2, h - 2)) == (E.RightEdge | E.BottomEdge)
    # Top-right yields to the close button (documented control veto).
    bar = window.titlebar
    tr = bar.mapTo(window, bar._close_btn.pos() + QPoint(2, 2))
    assert window._resize_edge_at(tr) is None
    assert window._resize_edge_at(QPoint(w // 2, h // 2)) is None
    assert window._resize_edge_at(QPoint(w // 2, 10)) is None  # drag band


def test_grips_win_over_views_and_scrollbars(shell, qtbot, tmp_path):
    import tempfile

    from PySide6.QtCore import QPoint

    E = Qt.Edge
    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf", pages=6))
    assert window.open_library_item(item.id) is True
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    window.showNormal()
    qtbot.wait(300)
    viewer = window._viewer
    bar = viewer._scrollbar
    # M4.6.12: the scrollbar owns its pixels — it vetoes the frame
    # grip (normal scrollbar cursor/interaction, no resize steal).
    # Bare frame pixels still resize.
    sb_pt = bar.mapTo(window, QPoint(bar.width() - 2, bar.height() // 2))
    assert window._resize_edge_at(sb_pt) is None
    window.close_document()
    # True controls still veto.
    search_pt = window.library_page._search_btn.mapTo(
        window, QPoint(19, 19))
    assert window._resize_edge_at(search_pt) is None


def test_cursor_mapping_all_zones(shell, qtbot):
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
    _move(QPoint(2, h // 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeHorCursor
    _move(QPoint(w - 2, h // 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeHorCursor
    _move(QPoint(w // 2, 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeVerCursor
    _move(QPoint(w // 2, h - 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeVerCursor
    _move(QPoint(2, 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
    _move(QPoint(w - 2, h - 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
    _move(QPoint(2, h - 2))
    assert window.cursor().shape() == Qt.CursorShape.SizeBDiagCursor
    _move(QPoint(w // 2, h // 2))
    # App-filter delivery (the real path, not a synthetic direct call):
    # drive the filter with a genuine move event on a child widget.
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    target = window.titlebar
    pos = QPoint(target.width() // 2, 2)
    event = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(pos),
                        QPointF(target.mapToGlobal(pos)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    assert window.eventFilter(target, event) is False  # never consumes
    assert window.cursor().shape() == Qt.CursorShape.SizeVerCursor
    window.unsetCursor()
