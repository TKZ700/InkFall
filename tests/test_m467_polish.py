"""M4.6.7: startup post-show, window controls, modal plate, focus,
collapsible sections, animated pin, open benchmark.
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
    qtbot.wait(500)
    yield window, service
    # Deterministic teardown: pytest-qt only holds a weakref, so an
    # unreferenced window may be GC'd WITHOUT closeEvent ever running —
    # leaking viewer pipelines/threads into later tests. Close here.
    try:
        window.close()
    except Exception:
        pass
    store.close()


def _make_pdf(tmp_path, name="doc.pdf", pages=2):
    from tests.helpers import make_rich_pdf

    return make_rich_pdf(str(tmp_path / name), pages=pages)


def _doc_row(page, item_id):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and not row.is_folder and row.item is not None:
            if row.item.id == item_id:
                return page._model.index(r, 0)
    raise AssertionError(f"row for {item_id} not found")


def _header_row(page, key):
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and row.is_header and row.header is not None:
            if row.header.key == key:
                return page._model.index(r, 0)
    raise AssertionError(f"header {key} not found")


# -- Startup --------------------------------------------------------------------

def test_startup_animation_deferred_until_shown(qtbot, tmp_path):
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    window = None
    try:
        service = LibraryService(store)
        window = MainWindow(DocumentManager(), library_service=service)
        qtbot.addWidget(window)
        assert getattr(window, "_startup_played", False) is False
        window.show()
        # Flag set in showEvent, but the entrance itself starts on the
        # next loop turn — never before first visibility.
        assert getattr(window, "_startup_played", False) is True
        qtbot.wait(500)
        qtbot.waitUntil(lambda: window._stack.graphicsEffect() is None,
                        timeout=5000)
        # Usable throughout: open works even mid-entrance.
        item = service.import_path(_make_pdf(tmp_path))
        assert window.open_library_item(item.id) is True
        qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)
    finally:
        # Explicit close: pytest-qt's weakref finalizer races GC, and a
        # GC'd window never runs closeEvent (leaked pipelines/threads).
        try:
            if window is not None:
                window.close()
        except Exception:
            pass
        store.close()


# -- Window controls --------------------------------------------------------------

def test_minimize_feedback_then_minimizes(shell, qtbot):
    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window.titlebar.minimizeRequested.emit()
    qtbot.waitUntil(lambda: window.isMinimized() is True, timeout=5000)
    qtbot.waitUntil(lambda: window._stack.graphicsEffect() is None,
                    timeout=5000)
    window.showNormal()


def test_maximize_restore_settles(shell, qtbot):
    window, _service = shell
    window.showNormal()
    qtbot.wait(200)
    window.titlebar.maximizeToggleRequested.emit()
    # Authoritative-first transitions: native state flips in the toggle.
    qtbot.waitUntil(lambda: window.isMaximized() is True, timeout=5000)
    qtbot.waitUntil(lambda: window._stack.graphicsEffect() is None,
                    timeout=5000)
    window.toggle_maximize_restore()
    # M4.6.8: restore dips to mask the compositor snap, then settles.
    qtbot.waitUntil(lambda: window.isMaximized() is False, timeout=5000)


def test_titlebar_press_feedback_wired(shell):
    window, _service = shell
    import inspect

    from app import titlebar as _tb

    src = inspect.getsource(_tb.TitleBar._make_control)
    assert "press_feedback" in src
    for btn in (window.titlebar._min_btn, window.titlebar._max_btn,
                window.titlebar._close_btn):
        assert btn.focusPolicy() & Qt.FocusPolicy.TabFocus


# -- Modal plate -------------------------------------------------------------------

def test_settings_modal_captures_library_plate(shell, tmp_path, qtbot):
    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    window.library_page.refresh()
    qtbot.wait(300)
    window.toggle_settings_panel()
    modal = window.settings_modal
    try:
        qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
        plate = modal._backdrop_pm
        assert plate is not None and not plate.isNull()
        # Plate covers the client area (recognizable Library shot).
        assert plate.width() >= modal.width() // 2
        assert modal.graphicsEffect() is None  # never flattened
        assert window._stack.isVisible()
    finally:
        modal.close_modal(animate=False)


def test_modal_plate_released_on_close(shell, qtbot):
    window, _service = shell
    window.toggle_settings_panel()
    modal = window.settings_modal
    qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
    modal.close_modal()
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)
    assert modal._backdrop_pm is None


def test_new_modal_action_rows(shell, qtbot):
    from PySide6.QtTest import QTest

    from app.new_panel import ActionRow

    window, _service = shell
    window.toggle_new_panel()
    modal = window.new_panel
    try:
        qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
        assert len(modal._rows) == 2
        assert all(isinstance(r, ActionRow) for r in modal._rows)
        # No layout-hosting button (the M4.6.6 row bug class).
        from PySide6.QtWidgets import QPushButton

        assert not any(isinstance(r, QPushButton) for r in modal._rows)
        for row in modal._rows:
            assert row.focusPolicy() & Qt.FocusPolicy.TabFocus
        plate = modal._backdrop_pm
        assert plate is not None and not plate.isNull()
        # Keyboard activates without dialogs (signal-level).
        fired = []
        modal._rows[0].activated.disconnect()
        modal._rows[0].activated.connect(lambda: fired.append(True))
        QTest.keyClick(modal._rows[0], Qt.Key.Key_Return)
        assert fired == [True]
    finally:
        modal.close_modal(animate=False)


# -- Focus styling ------------------------------------------------------------------

def test_no_native_focus_rect_anywhere():
    import inspect

    from app.theme import manager as _mgr

    from app.library import library_page as _lp
    from app import modal as _mo
    from app import new_panel as _np
    from app import settings_button as _fb
    from app import settings_panel as _sp
    from app import titlebar as _tb

    # Global kill-switch for the native indicator (adds no visuals).
    assert "QPushButton:focus" in inspect.getsource(_mgr.ThemeManager._stylesheet)
    assert "QToolButton:focus" in inspect.getsource(_mgr.ThemeManager._stylesheet)
    # Every styled control suppresses the native rect locally too.
    for mod in (_lp, _mo, _np, _fb, _sp, _tb):
        try:
            src = inspect.getsource(mod)
        except Exception:
            continue
        if "setStyleSheet" in src:
            assert "outline: 0" in src or "outline:0" in src, mod.__name__


def test_circular_controls_tab_focus_only(shell):
    window, _service = shell
    page = window.library_page
    for btn in (page._search_btn, page._new_btn, page._sort_btn,
                page._grid_btn, page._list_btn, page._filter_clear,
                window.settings_fab, window.settings_modal._close_btn,
                window.titlebar._min_btn):
        assert btn.focusPolicy() == Qt.FocusPolicy.TabFocus, btn


def test_focus_rings_use_token_accent(shell):
    window, _service = shell
    page = window.library_page
    token = window._theme.tokens.input_focus
    for sheet in (page._search_btn.styleSheet(), page._new_btn.styleSheet(),
                  page._sort_btn.styleSheet(),
                  window.settings_modal._close_btn.styleSheet()):
        assert "input_focus" in sheet or token in sheet


# -- Collapsible sections --------------------------------------------------------------

def _section_keys(page):
    return [r.header.key for r in
            (page._model.row_at(i) for i in range(page._model.rowCount()))
            if r is not None and r.is_header]


def test_sections_collapse_independently(shell, tmp_path, qtbot):
    window, service = shell
    folder = service.create_folder("Work")
    for i in range(3):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"),
                            folder.id if i % 2 == 0 else None)
    page = window.library_page
    page.refresh()
    qtbot.wait(300)
    full = page._model.rowCount()
    assert full > 4
    assert page.is_section_collapsed("documents") is False
    page.toggle_section("documents")
    qtbot.wait(400)
    assert page._model.rowCount() < full
    assert page.is_section_collapsed("documents") is True
    # Other sections keep their content rows.
    keys = _section_keys(page)
    assert "documents" in keys
    page.toggle_section("documents")
    qtbot.wait(400)
    assert page._model.rowCount() == full
    assert page.is_section_collapsed("documents") is False


def test_header_click_and_keyboard_toggle(shell, tmp_path, qtbot):
    from PySide6.QtTest import QTest

    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    idx = _header_row(page, "documents")
    opens = []
    page.openRequested.connect(opens.append)
    page._on_view_clicked(idx)  # header click: toggles, never opens
    assert opens == []
    assert page.is_section_collapsed("documents") is True
    page._on_activated(_header_row(page, "documents"))  # Enter toggles back
    assert page.is_section_collapsed("documents") is False
    QTest.keyClick(page._view.viewport(), Qt.Key.Key_Escape)  # no crash path


def test_collapse_state_persists_and_survives_controls(shell, tmp_path, qtbot):
    from PySide6.QtCore import QSettings

    window, service = shell
    for i in range(3):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    page = window.library_page
    qtbot.wait(300)
    page.toggle_section("documents")
    qtbot.wait(300)
    assert QSettings().value("library/sections/documents_collapsed") in (True, "true", 1)
    full_collapsed_rows = page._model.rowCount()
    # Sort / grid-list / search must not reset collapse.
    page._sort_index = 1
    page.refresh()
    assert page.is_section_collapsed("documents") is True
    page.set_list_mode(True)
    qtbot.wait(300)
    assert page.is_section_collapsed("documents") is True
    page.set_list_mode(False)
    qtbot.wait(300)
    assert page.is_section_collapsed("documents") is True
    assert page._model.rowCount() == full_collapsed_rows
    page._search.setText("d00")
    page.refresh()
    assert page.is_section_collapsed("documents") is True
    page._search.clear()
    page.refresh()


def test_collapse_expand_zero_renders(shell, tmp_path, qtbot):
    window, service = shell
    for i in range(4):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    page = window.library_page
    page.refresh()
    bar = page._view.verticalScrollBar()
    qtbot.wait(300)
    for _ in range(6):
        bar.setValue(bar.maximum())
        qtbot.wait(150)
        bar.setValue(0)
        qtbot.wait(150)
    base = window._thumbs.renders_started
    page.toggle_section("documents")
    qtbot.wait(400)
    page.toggle_section("documents")
    qtbot.wait(400)
    assert window._thumbs.renders_started == base


def test_section_chevron_animator_lifecycle(shell, tmp_path, qtbot):
    window, service = shell
    service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    page.toggle_section("documents")
    assert page._section_anim_key == "documents"
    qtbot.waitUntil(lambda: page._section_anim_key is None, timeout=5000)
    page.toggle_section("documents")
    qtbot.waitUntil(lambda: page._section_anim_key is None, timeout=5000)


# -- Pin ------------------------------------------------------------------------

def test_pin_toggle_zero_renders_and_filled_state(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    page.refresh()
    qtbot.wait(300)
    bar = page._view.verticalScrollBar()
    for _ in range(4):
        bar.setValue(bar.maximum())
        qtbot.wait(120)
        bar.setValue(0)
        qtbot.wait(120)
    base = window._thumbs.renders_started
    assert service.get_item(item.id).favorite is False
    page._toggle_pin(item.id)
    qtbot.waitUntil(lambda: page._pin_anim_id is None, timeout=5000)
    assert service.get_item(item.id).favorite is True
    assert window._thumbs.renders_started == base
    assert "pinned" in _section_keys(page)  # Pinned section appeared
    page._toggle_pin(item.id)
    qtbot.waitUntil(lambda: page._pin_anim_id is None, timeout=5000)
    assert service.get_item(item.id).favorite is False
    assert window._thumbs.renders_started == base


def test_pin_pulse_capped_and_single_animator(shell, tmp_path):
    import inspect

    from app.library import delegate as _dlg

    src = inspect.getsource(_dlg.LibraryDelegate._pin_anim_scale)
    assert "0.08" in src  # 1.0 -> 1.08 -> 1.0 cap, no bounce
    assert "sin" in src
    window, _service = shell
    assert window.library_page._pin_anim is not None


def test_pin_filled_icon_uses_accent():
    from app.icons import pixmap

    from app.theme.tokens import InkfallTokens

    t = InkfallTokens()
    pm = pixmap("pin-filled", t.accent, 16)
    assert not pm.isNull()
    assert pm.width() == 16


def test_collapsed_pinned_survives_pin(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    page._toggle_pin(item.id)
    qtbot.wait(300)
    page.toggle_section("pinned")
    qtbot.wait(300)
    assert page.is_section_collapsed("pinned") is True
    page._toggle_pin(item.id)  # unpin: no force-expand
    qtbot.wait(300)
    assert page.is_section_collapsed("pinned") is True


# -- Open benchmark -----------------------------------------------------------------

def test_open_transition_first_viewer_deferred(shell, tmp_path, qtbot):
    import time

    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=6))
    t0 = time.perf_counter()
    assert window.open_library_item(item.id) is True
    sync_ms = (time.perf_counter() - t0) * 1000
    # Transition starts synchronously (workspace visible immediately).
    assert window._stack.currentIndex() == 1
    assert sync_ms < 500  # no heavy prep on the click path
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=15000)
    window.close_document()
