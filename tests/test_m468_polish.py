"""M4.6.8: window-level motion, modal truth, sections Grid+List,
category press, tooltip removal, live-width reflow.
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


def _headers(page):
    page.refresh()
    found = []
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and row.is_header and row.header is not None:
            found.append((r, row.header.key))
    return found


def _folder_row(page):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and row.is_folder and row.folder is not None:
            return page._model.index(r, 0), row.folder.id
    raise AssertionError("no folder row")


# -- window-level motion --------------------------------------------------------

def test_startup_window_opacity_fades_in(qtbot, tmp_path):
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    window = None
    try:
        service = LibraryService(store)
        window = MainWindow(DocumentManager(), library_service=service)
        qtbot.addWidget(window)
        window.show()
        # Pre-show the top-level window is transparent (no time-zero flash).
        assert window.windowOpacity() < 1.0
        qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)
    finally:
        try:
            if window is not None:
                window.close()
        except Exception:
            pass
        store.close()


def test_minimize_window_dip_then_minimized(shell, qtbot):
    window, _service = shell
    window.showNormal()
    qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)
    window.titlebar.minimizeRequested.emit()
    qtbot.wait(60)
    assert window.windowOpacity() < 1.0  # top-level dip, not content-only
    qtbot.waitUntil(lambda: window.isMinimized() is True, timeout=5000)
    window.showNormal()
    qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)


def test_maximize_flips_state_synchronously(shell, qtbot):
    # Authoritative-first transitions: native state flips inside the
    # toggle itself — no tween seam where the window looks maximized
    # while still logically Normal (that seam caused double-click
    # confusion: a second click observed stale state and re-maximized).
    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window.toggle_maximize_restore()
    assert window.isMaximized() is True  # immediate, no tween wait
    assert window._normal_geom is not None
    assert window.titlebar._max_btn.property("icon_name") == "restore"


def test_restore_dips_and_settles(shell, qtbot):
    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    window.toggle_maximize_restore()
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)
    assert window.titlebar._max_btn.property("icon_name") == "maximize"


def test_close_fades_window_first(qtbot, tmp_path):
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    window = None
    try:
        service = LibraryService(store)
        window = MainWindow(DocumentManager(), library_service=service)
        window.show()
        qtbot.addWidget(window)
        qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)
        window.titlebar.closeRequested.emit()
        qtbot.wait(60)
        assert window.windowOpacity() < 1.0  # visible close animation
        qtbot.waitUntil(lambda: window.isVisible() is False, timeout=5000)
        window = None
    finally:
        store.close()


def test_fullscreen_exit_dips_and_restores(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    qtbot.wait(400)
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    window.toggle_fullscreen()
    qtbot.wait(40)
    assert window.windowOpacity() < 1.0  # exit dip masks the snap
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.windowOpacity() >= 1.0, timeout=5000)
    assert window.titlebar.isVisible() is True
    window.close_document()


def test_window_helpers_registered():
    from app import fx

    assert "window_fade" in fx.__all__
    assert "window_tween_geometry" in fx.__all__
    assert fx.window_fade is not None and fx.window_tween_geometry is not None


# -- modal truth ----------------------------------------------------------------

def test_token_rgba_parses():
    # M4.6.8: QColor("rgba(...)") does not parse (invalid -> opaque
    # black), so tokens go through token_rgba.
    from app.theme.tokens import token_rgba

    assert token_rgba("rgba(0,0,0,0.45)") == (0, 0, 0, 0.45)
    assert token_rgba("rgba(139,147,224,0.20)") == (139, 147, 224, 0.20)
    assert token_rgba("#0B0C0F") == (11, 12, 15, 1.0)
    assert token_rgba("#00000000") == (0, 0, 0, 0.0)


def test_modal_veil_is_translucent_not_black(shell, qtbot):
    import PySide6.QtGui as _g

    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    assert modal._plate_valid() is True
    # Sample beside the panel (never under it): dimmed Library must
    # read there, not panel fill and not opaque black.
    geo = modal._panel.geometry()
    sx, sy = min(geo.right() + 40, modal.width() - 8), geo.center().y()
    plate_px = _g.QColor(modal._backdrop_pm.toImage().pixel(sx, sy))
    assert (plate_px.red(), plate_px.green(), plate_px.blue()) != (0, 0, 0)
    modal_px = _g.QColor(modal.grab().toImage().pixel(sx, sy))
    assert (modal_px.red(), modal_px.green(), modal_px.blue()) != (0, 0, 0)
    # Veil darkens the real Library instead of replacing it.
    assert modal_px.red() < plate_px.red()
    assert modal_px.green() < plate_px.green()
    assert modal_px.blue() < plate_px.blue()
    modal.close_modal()
    qtbot.wait(400)


def test_modal_plate_valid_and_veiled(shell, qtbot):
    import re

    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    assert window._stack.isVisible()  # Library never hidden
    assert modal._backdrop_pm is not None and not modal._backdrop_pm.isNull()
    assert modal._plate_valid() is True
    dim = window._theme.tokens.modal_dim
    m = re.search(r"([\d.]+)\s*\)\s*$", dim)
    alpha = float(m.group(1)) if m else 0.45
    assert 0.40 <= alpha <= 0.55
    modal.close_modal()
    qtbot.wait(400)


def test_modal_recaptures_on_parent_resize(shell, qtbot):
    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    assert modal._plate_valid() is True
    window.resize(window.width() + 120, window.height())
    qtbot.wait(400)
    assert modal._plate_valid() is True  # re-fit + recaptured, never stretched
    modal.close_modal()
    qtbot.wait(400)


def test_modal_rapid_toggle_clean(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    for _ in range(3):
        modal.open_modal(window.settings_fab)
        modal.close_modal()
    modal.open_modal(window.settings_fab)
    qtbot.waitUntil(lambda: modal.isVisible() is True, timeout=5000)
    qtbot.wait(400)
    assert modal._plate_valid() is True
    assert abs(modal._dim - 1.0) < 0.05
    modal.close_modal()
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


def test_new_settings_share_foundation(shell):
    from app.modal import CenteredModal

    window, _service = shell
    assert isinstance(window._settings_modal, CenteredModal)
    assert isinstance(window._new_modal, CenteredModal)
    assert window._settings_modal.parent() is window._content
    assert window._new_modal.parent() is window._content


def test_modal_opaque_safe_fallback(shell, qtbot):
    window, _service = shell
    modal = window._settings_modal
    modal.open_modal(window.settings_fab)
    qtbot.wait(400)
    modal._release_backdrop()  # simulate capture failure
    modal.update()
    qtbot.wait(100)
    px = modal.grab().toImage().pixel(4, 4)
    import PySide6.QtGui as _g

    color = _g.QColor(px)
    assert color.alpha() == 255  # opaque theme surface, never a black hole
    assert (color.red(), color.green(), color.blue()) != (0, 0, 0)
    modal.close_modal()
    qtbot.wait(400)


def test_action_row_press_feedback(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    modal = window._new_modal
    modal.open_modal(window.library_page._new_btn)
    qtbot.wait(400)
    row = modal._rows[0]
    try:
        row.activated.disconnect()  # press feedback only, no dialog path
    except Exception:
        pass
    # Press only (a full click would run the Save-As dialog path).
    QTest.mousePress(row, Qt.MouseButton.LeftButton)
    assert row.graphicsEffect() is not None  # ~100ms press dip running
    QTest.mouseRelease(row, Qt.MouseButton.LeftButton)
    modal.close_modal(animate=False)


# -- sections Grid + List ---------------------------------------------------------

def _seed_docs(service, tmp_path, n, prefix="seed"):
    import tempfile

    tmp = tempfile.mkdtemp()
    return [service.import_path(_make_pdf(f"{tmp}/{prefix}{i}.pdf")).id
            for i in range(n)]


def test_chevron_inside_viewport_both_modes(shell, qtbot, tmp_path):
    window, service = shell
    _seed_docs(service, tmp_path, 3)
    page = window.library_page
    for mode in (False, True):
        page.set_list_mode(mode)
        page.refresh()
        qtbot.wait(300)
        headers = _headers(page)
        assert headers, "sections must exist"
        vw = page._view.viewport().width()
        for r, _key in headers:
            rect = page._view.visualRect(page._model.index(r, 0))
            assert rect.right() <= vw + 1, (mode, rect, vw)
            anchor = min(rect.right(), vw) - 10  # chevron center x
            assert 0 <= anchor < vw, (mode, anchor, vw)
    page.set_list_mode(False)
    qtbot.wait(200)


def test_collapse_expand_all_sections(shell, qtbot, tmp_path):
    import tempfile

    from PySide6.QtCore import QSettings

    window, service = shell
    ids = _seed_docs(service, tmp_path, 2, prefix="col")
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(f"{tempfile.mkdtemp()}/colc.pdf"), folder.id)
    service.toggle_favorite(ids[0])
    window.open_library_item(ids[1])
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    window.close_document()
    qtbot.wait(300)
    page = window.library_page
    present = [k for _, k in _headers(page)]
    assert set(present) >= {"recent", "categories", "pinned", "documents"}
    for key in ("recent", "categories", "pinned", "documents"):
        rows_before = page._model.rowCount()
        page.toggle_section(key)
        qtbot.wait(300)
        assert page._collapsed[key] is True
        assert QSettings().value(f"library/sections/{key}_collapsed") in (True, "true")
        assert page._model.rowCount() < rows_before
        page.toggle_section(key)
        qtbot.wait(300)
        assert page._collapsed[key] is False
        assert page._model.rowCount() == rows_before


def test_section_anim_singleton_160ms(shell):
    window, _service = shell
    anim = window.library_page._section_anim
    assert anim.duration() == 160


# -- categories ---------------------------------------------------------------------

def test_category_press_anim_runs_and_settles(shell, qtbot):
    import tempfile

    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"), folder.id)
    page = window.library_page
    assert page._cat_anim.duration() == 130
    page._play_cat_press(folder.id)
    assert page._cat_anim_id == folder.id
    page._cat_progress = 0.5
    assert page._cat_compact(folder.id) < 1.0  # mid-press compacts
    assert page._cat_compact("other") == 1.0
    qtbot.waitUntil(lambda: page._cat_anim_id is None, timeout=5000)
    assert page._cat_compact(folder.id) == 1.0


def test_category_click_filters_same_page(shell, qtbot):
    import tempfile

    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tempfile.mkdtemp() + "/c.pdf"), folder.id)
    page = window.library_page
    idx, fid = _folder_row(page)
    assert window._stack.currentIndex() == 0
    page._on_view_clicked(idx)
    qtbot.wait(300)
    assert page.selected_category_id == fid  # inline filter, no navigation
    assert window._stack.currentIndex() == 0
    assert page._filter_bar.isVisible() is True


def test_category_hover_strength_and_no_widgets(shell):
    import inspect

    from PySide6.QtWidgets import QWidget

    window, _service = shell
    src = inspect.getsource(window.library_page._delegate._paint_folder)
    assert "30 * hover_p" in src  # animated inset wash (M4.6.10 progress)
    assert "drawPath(wash)" in src  # hairline border
    assert window.library_page._view.viewport().findChildren(QWidget) == []


# -- open transitions ---------------------------------------------------------------

def test_open_transitions_workspace_synchronously(shell, qtbot):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/o.pdf"))
    assert window.open_library_item(item.id) is True
    assert window._stack.currentIndex() == 1  # immediate, pre-prep
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    window.close_document()


def test_rapid_a_to_b_final_is_b(shell, qtbot):
    import tempfile

    window, service = shell
    tmp = tempfile.mkdtemp()
    a = service.import_path(_make_pdf(tmp + "/a.pdf"))
    b = service.import_path(_make_pdf(tmp + "/b.pdf"))
    assert window.open_library_item(a.id) is True
    assert window.open_library_item(b.id) is True  # supersedes before finish
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    qtbot.wait(500)
    assert window._viewer_bar._title.text() == "b.pdf"  # B won the race
    window.close_document()


def test_same_doc_open_suppressed(shell, qtbot):
    import tempfile

    window, service = shell
    item = service.import_path(_make_pdf(tempfile.mkdtemp() + "/s.pdf"))
    page = window.library_page
    calls = []
    page.openRequested.connect(calls.append)
    page._request_open(item.id)  # first funnel pass emits
    page._request_open(item.id)  # same-document duplicate suppressed
    assert calls == [item.id]
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    window.close_document()


# -- tooltips gone, accessibility kept -------------------------------------------------

def test_no_application_tooltips(shell):
    from PySide6.QtWidgets import QWidget

    window, _service = shell
    offenders = [w for w in window.findChildren(QWidget) if w.toolTip() != ""]
    assert offenders == []


def test_no_tooltip_constructs_in_source():
    import pathlib

    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.py"))
    assert "setToolTip" not in blob
    assert "ToolTipRole" not in blob
    assert "QToolTip" not in blob


def test_accessible_names_preserved(shell):
    window, _service = shell
    page = window.library_page
    controls = [
        page._search_btn, page._search, page._new_btn, page._sort_btn,
        page._grid_btn, page._list_btn, page._filter_clear, page._view,
        window.settings_fab, window.titlebar._min_btn,
        window.titlebar._max_btn, window.titlebar._close_btn,
        window._viewer_bar._back, window._viewer_bar._fullscreen,
    ]
    for ctrl in controls:
        assert ctrl.accessibleName() != "", ctrl
    assert page._view.accessibleDescription() != ""
    assert window.settings_fab.accessibleDescription() != ""


# -- live width --------------------------------------------------------------------------

def test_body_has_no_width_cap_and_fills_page(shell, qtbot):
    window, _service = shell
    page = window.library_page
    assert page._body.maximumWidth() >= 32767
    assert page._body.width() == page.width()
    assert page.layout().count() == 1  # no trailing stretch strip


def test_reflow_across_widths(shell, qtbot):
    from PySide6.QtCore import Qt

    from app.library.delegate import card_width_for, grid_columns_for_width

    window, _service = shell
    page = window.library_page
    assert page._view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    for w, h in ((1280, 720), (1440, 900), (1920, 1080)):
        window.resize(w, h)
        qtbot.wait(300)
        assert page._body.width() == page.width()
        vw = page._view.viewport().width()
        usable = vw - page._view.spacing()
        cols = grid_columns_for_width(usable)
        card = card_width_for(usable, cols)
        assert cols >= 2
        assert 170 <= card <= 210
        assert cols * card + (cols - 1) * 16 <= usable + 1.5
    window.resize(1280, 800)


def test_shared_origin_and_final_row(shell, qtbot):
    import tempfile

    window, service = shell
    tmp = tempfile.mkdtemp()
    for i in range(7):
        service.import_path(_make_pdf(f"{tmp}/f{i}.pdf"))
    page = window.library_page
    page.refresh()
    qtbot.wait(300)
    model = page._model
    header_x, cards = None, []
    for r in range(model.rowCount()):
        row = model.row_at(r)
        rect = page._view.visualRect(model.index(r, 0))
        if row is not None and row.is_header and row.header.key == "documents":
            header_x = rect.x()
        elif row is not None and not row.is_folder and row.item is not None:
            cards.append((rect.x(), rect.y()))
    assert header_x is not None and cards
    assert all(x == header_x for x, _y in cards[:1])  # shared left origin
    rows: dict[int, list[int]] = {}
    for x, y in cards:
        rows.setdefault(y, []).append(x)
    for y, xs in rows.items():
        assert xs == sorted(xs), (y, xs)  # row flows left-to-right
        assert xs[0] == header_x, (y, xs)  # every row starts at origin
