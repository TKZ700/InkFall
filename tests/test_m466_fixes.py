"""M4.6.6 fixes: single-click open, hover restraint, modal backdrop,
sort anchoring, workspace/fullscreen/startup transitions, scroll continuity.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor

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
    qtbot.wait(400)
    yield window, service
    # Deterministic teardown (see test_m467_polish): never rely on the
    # weakref-only pytest-qt finalizer for closeEvent side effects.
    try:
        window.close()
    except Exception:
        pass
    store.close()


def _make_pdf(tmp_path, name="doc.pdf", pages=2):
    from tests.helpers import make_rich_pdf

    return make_rich_pdf(str(tmp_path / name), pages=pages)


def _row_of(page, item_id):
    # Imports don't auto-refresh the page model; sync first.
    page.refresh()
    model = page._model
    for r in range(model.rowCount()):
        row = model.row_at(r)
        if row is not None and not row.is_folder and row.item is not None:
            if row.item.id == item_id:
                return model.index(r, 0)
    raise AssertionError(f"row for {item_id} not found")


def _import(page, service, tmp_path, name="doc.pdf", pages=2, folder_id=None):
    """Import then refresh the page model (imports don't auto-refresh)."""
    item = service.import_path(_make_pdf(tmp_path, name, pages), folder_id)
    page.refresh()
    return item


def _click_at(page, qtbot, item_id):
    """Click a document card by id (M4.6.7 async open).

    The deferred open finish rebuilds the model underfoot, so the
    index is (re-)resolved after every event-processing pause — a
    stale QModelIndex would silently address the wrong row.
    """
    from PySide6.QtTest import QTest

    viewport = page._view.viewport()
    qtbot.wait(200)  # let the refreshed model lay out first
    idx = _row_of(page, item_id)
    QTest.mouseMove(viewport, page._view.visualRect(idx).center())
    qtbot.wait(100)  # deferred open work may reset the model here
    idx = _row_of(page, item_id)  # re-resolve: never click stale
    page._view.setCurrentIndex(idx)
    page._on_view_clicked(idx)


# -- Single-click open ----------------------------------------------------------

def test_single_click_opens_document(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    _click_at(page, qtbot, item.id)
    qtbot.waitUntil(lambda: window.in_workspace, timeout=5000)
    # M4.6.7: document preparation is deferred past the transition.
    qtbot.waitUntil(lambda: window.viewer.has_document, timeout=10000)


def test_double_click_emits_single_open(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    opens = []
    page.openRequested.connect(opens.append)
    _click_at(page, qtbot, item.id)
    page._on_activated(_row_of(page, item.id))  # double-click second fire
    page._on_activated(_row_of(page, item.id))
    qtbot.wait(200)
    assert opens == [item.id]


def test_rapid_a_then_b_opens_both(shell, tmp_path, qtbot):
    window, service = shell
    a = service.import_path(_make_pdf(tmp_path, "a.pdf"))
    b = service.import_path(_make_pdf(tmp_path, "b.pdf"))
    page = window.library_page
    qtbot.wait(300)
    opens = []
    page.openRequested.connect(opens.append)
    for doc in (a, b):
        _click_at(page, qtbot, doc.id)
    assert opens == [a.id, b.id]


def test_enter_opens_selected(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    qtbot.wait(300)
    opens = []
    page.openRequested.connect(opens.append)
    page._on_activated(_row_of(page, item.id))
    assert opens == [item.id]


def test_category_click_filters_without_open(shell, tmp_path, qtbot):
    window, service = shell
    folder = service.create_folder("Work")
    item = service.import_path(_make_pdf(tmp_path), folder.id)
    _ = item
    page = window.library_page
    qtbot.wait(300)
    opens = []
    page.openRequested.connect(opens.append)
    target = None
    page.refresh()
    for r in range(page._model.rowCount()):
        row = page._model.row_at(r)
        if row is not None and row.is_folder and row.folder is not None:
            if row.folder.id == folder.id:
                target = page._model.index(r, 0)
    assert target is not None
    page._on_view_clicked(target)
    assert opens == []
    assert page.selected_category_id == folder.id


def test_pin_click_toggles_without_open(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    page = window.library_page
    service.toggle_favorite(item.id)  # pin always painted for favorites
    idx = _row_of(page, item.id)
    page._view.viewport().repaint()  # synchronously repopulate pin geometry
    qtbot.wait(100)
    opens = []
    page.openRequested.connect(opens.append)
    rect = page._delegate._pin_rects.get(item.id)
    assert rect is not None
    viewport = page._view.viewport()
    QCursor.setPos(viewport.mapToGlobal(rect.center()))
    page._on_view_clicked(idx)
    assert opens == []
    assert service.get_item(item.id).favorite is False


# -- Hover restraint ------------------------------------------------------------

def test_hover_wash_capped_moderate():
    import inspect

    from app.library import delegate as _dlg

    src = inspect.getsource(_dlg.LibraryDelegate._hover_lift)
    assert "underline" not in src.lower()
    assert "0.11" in src  # M4.6.12 moderate wash target (~28/255), never selection
    assert "0.30" not in src


def test_hover_only_lifts_current_item(shell, tmp_path, qtbot):
    window, service = shell
    a = service.import_path(_make_pdf(tmp_path, "a.pdf"))
    b = service.import_path(_make_pdf(tmp_path, "b.pdf"))
    page = window.library_page
    qtbot.wait(300)
    page._hover_id = a.id
    page._hover_progress = 1.0
    delegate = page._delegate

    lift_a, wash_a = delegate._hover_lift(service.get_item(a.id), False)
    lift_b, wash_b = delegate._hover_lift(service.get_item(b.id), False)
    assert (lift_a, wash_a) == (2, pytest.approx(0.11))
    assert (lift_b, wash_b) == (0, 0.0)
    page._hover_id = None


def test_single_hover_animator_no_card_widgets(shell):
    from PySide6.QtCore import QVariantAnimation
    from PySide6.QtWidgets import QWidget

    window, _service = shell
    page = window.library_page
    assert page._hover_anim is not None
    # View-level animators only (hover, section chevron, pin pulse,
    # category press, header hover, category hover — all singletons);
    # QPropertyAnimation subclasses (scroll glides, tweens) are
    # page-level too. Never per-card.
    plain = [a for a in page.findChildren(QVariantAnimation)
             if type(a) is QVariantAnimation]
    # M4.6.11: six view-level singletons plus the press
    # micro-interaction singleton, plus one lightweight hover
    # animator per shared chrome button (ModalButton) — never per-card.
    singletons = {page._hover_anim, page._press_anim, page._section_anim,
                  page._pin_anim, page._cat_anim, page._hdr_anim,
                  page._cat_hover_anim}
    assert singletons <= set(plain)
    from app.modal import ModalButton

    for anim in plain:
        if anim in singletons:
            assert anim.parent() is page
        else:
            assert isinstance(anim.parent(), ModalButton)
    assert page._view.viewport().findChildren(QWidget) == []


def test_selected_title_has_no_bar_or_tint():
    import inspect

    from app.library.delegate import LibraryDelegate

    titles = inspect.getsource(LibraryDelegate._paint_titles)
    assert "underline" not in titles.lower()
    assert "accent" not in titles.lower()
    assert "fillRect" not in titles
    ring = inspect.getsource(LibraryDelegate._draw_thumb)
    assert "accent" in ring  # ring-only selection lives here


# -- Modal backdrop -------------------------------------------------------------

def test_overlay_translucent_no_whole_overlay_effect(shell, qtbot):
    window, _service = shell
    window.toggle_settings_panel()
    modal = window.settings_modal
    try:
        assert modal.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert modal.graphicsEffect() is None  # never flattened to black
        assert window._stack.isVisible()  # Library stays visible behind
        qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
        assert modal.graphicsEffect() is None
    finally:
        modal.close_modal(animate=False)


def test_modal_escape_closes_and_releases(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    closed = []
    window.settings_modal.closed.connect(lambda: closed.append(True))
    window.toggle_settings_panel()
    modal = window.settings_modal
    qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
    QTest.keyClick(modal, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)
    assert closed == [True]


def test_new_modal_same_foundation(shell, qtbot):
    window, _service = shell
    window.toggle_new_panel()
    modal = window.new_panel
    try:
        assert modal.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert modal.graphicsEffect() is None
        assert window._stack.isVisible()
        qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
        center = modal._panel.geometry().center()
        expect = modal.rect().center()
        assert abs(center.x() - expect.x()) <= 2
        assert abs(center.y() - expect.y()) <= 2
    finally:
        modal.close_modal(animate=False)


def test_rapid_modal_toggle_never_sticks(shell, qtbot):
    window, _service = shell
    modal = window.settings_modal
    for _ in range(3):
        window.toggle_settings_panel()
        qtbot.wait(60)
        modal.close_modal(animate=False)
    window.toggle_settings_panel()
    qtbot.waitUntil(lambda: modal._dim >= 0.99, timeout=5000)
    assert modal.isVisible() is True
    modal.close_modal()
    qtbot.waitUntil(lambda: modal.isVisible() is False, timeout=5000)


# -- Sort anchoring -------------------------------------------------------------

def test_sort_menu_anchored_not_at_origin(shell, qtbot):
    window, _service = shell
    page = window.library_page
    page._on_sort_menu()
    menu = page._sort_menu
    try:
        assert menu is not None and menu.isVisible()
        anchor = page._sort_btn.mapToGlobal(page._sort_btn.rect().bottomLeft())
        # Qt may clamp x to stay on-screen; y must match the anchor row.
        assert abs(menu.pos().y() - anchor.y()) < 80
        assert not (menu.pos().x() < 60 and menu.pos().y() < 60)
    finally:
        menu.close()


def test_sort_menu_animated_from_button(shell, qtbot):
    window, _service = shell
    page = window.library_page
    page._on_sort_menu()
    menu = page._sort_menu
    try:
        assert menu.graphicsEffect() is not None  # entrance playing
        qtbot.waitUntil(lambda: menu.graphicsEffect() is None, timeout=5000)
    finally:
        menu.close()


def test_sort_menu_selection_applies(shell, qtbot):
    window, _service = shell
    page = window.library_page
    before = page._sort_btn.text()
    page._on_sort_menu()
    menu = page._sort_menu
    menu.actions()[2].trigger()
    qtbot.wait(200)
    assert page._sort_index == 2
    assert page._sort_btn.text() != before


# -- Workspace transitions ------------------------------------------------------

def test_open_transition_animates_and_settles(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    assert window._stack.currentIndex() == 1
    assert window._stack.widget(1).graphicsEffect() is not None
    qtbot.waitUntil(lambda: window._stack.widget(1).graphicsEffect() is None,
                    timeout=5000)
    window.close_document()


def test_back_transition_preserves_state(shell, tmp_path, qtbot):
    window, service = shell
    folder = service.create_folder("Work")
    item = service.import_path(_make_pdf(tmp_path), folder.id)
    page = window.library_page
    page.select_category(folder.id)
    page.set_list_mode(True)
    bar = page._view.verticalScrollBar()
    qtbot.wait(400)
    fraction = bar.value() / max(1, bar.maximum())
    assert window.open_library_item(item.id) is True
    qtbot.wait(400)
    window.show_library()
    qtbot.waitUntil(lambda: window._stack.widget(0).graphicsEffect() is None,
                    timeout=5000)
    assert page.selected_category_id == folder.id
    assert page._list_mode is True
    after = bar.value() / max(1, bar.maximum())
    assert abs(after - fraction) < 0.15


def test_fullscreen_two_phases(shell, tmp_path, qtbot):
    window, service = shell
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    qtbot.wait(400)
    window.toggle_fullscreen()
    # Fullscreen flips authoritatively inside the toggle (no geometry
    # tween across the state boundary: tween frames sized to the
    # screen rect could synthesize a hostile Maximized flip whose
    # notification murders the tween that caused it).
    qtbot.waitUntil(lambda: window.isFullScreen() is True, timeout=5000)
    assert window.titlebar.isVisible() is False
    window.toggle_fullscreen()
    qtbot.waitUntil(lambda: window.isFullScreen() is False, timeout=5000)
    qtbot.waitUntil(lambda: window.titlebar.isVisible() is True, timeout=5000)
    window.close_document()


def test_startup_entrance_helper():
    from PySide6.QtWidgets import QLabel

    from app import fx

    label = QLabel("probe")
    group = fx.startup_entrance(label, 60, label)
    assert group is not None
    assert label.graphicsEffect() is not None


# -- Search ---------------------------------------------------------------------

def test_search_escape_clears_then_collapses(shell, qtbot):
    from PySide6.QtTest import QTest

    window, _service = shell
    page = window.library_page
    page._expand_search()
    qtbot.wait(400)
    assert page._search.isVisible()
    page._search.setText("zzz")
    qtbot.wait(400)
    QTest.keyClick(page._search, Qt.Key.Key_Escape)
    qtbot.wait(200)
    assert page._search.text() == ""  # first Esc clears, stays open
    assert page._search.isVisible()
    QTest.keyClick(page._search, Qt.Key.Key_Escape)
    qtbot.wait(500)
    assert page._search_btn.isVisible()  # empty + Esc collapses


# -- Grid/List + scrolling ------------------------------------------------------

def test_grid_list_toggle_zero_renders_when_cached(shell, tmp_path, qtbot):
    window, service = shell
    for i in range(4):
        service.import_path(_make_pdf(tmp_path, f"d{i}.pdf"))
    page = window.library_page
    bar = page._view.verticalScrollBar()
    qtbot.wait(300)
    for _ in range(6):
        bar.setValue(bar.maximum())
        qtbot.wait(150)
        bar.setValue(0)
        qtbot.wait(150)
    base = window._thumbs.renders_started
    page.set_list_mode(True)
    qtbot.wait(400)
    page.set_list_mode(False)
    qtbot.wait(400)
    assert window._thumbs.renders_started == base


def test_scroll_moves_every_frame_without_leak(shell, tmp_path, qtbot):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=10))
    assert window.open_library_item(item.id) is True
    viewer = window.viewer
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    vbar = viewer._scrollbar
    vbar.setValue(1000)
    qtbot.wait(300)
    seen = []

    def probe():
        seen.append(vbar.value())

    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.timeout.connect(probe)
    timer.start(5)
    pos = QPoint(100, 100)
    for _ in range(4):
        event = QWheelEvent(QPointF(pos), QPointF(pos), QPoint(0, 0), QPoint(0, -240),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        viewer.wheelEvent(event)
        qtbot.wait(50)
    timer.stop()
    distinct = sum(1 for a, b in zip(seen, seen[1:]) if a != b)
    assert distinct >= 3  # continuous interpolation, not a single jump
    qtbot.wait(600)
    assert vbar.property("_inkfall_glide") is None  # no timer leak
    window.close_document()


def test_directional_scroll_stays_bounded(shell, tmp_path, qtbot):
    import random

    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    window, service = shell
    item = service.import_path(_make_pdf(tmp_path, pages=30))
    assert window.open_library_item(item.id) is True
    viewer = window.viewer
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    vbar = viewer._scrollbar
    calls = []
    orig = viewer._pipeline.update

    def counting(scroll, vw, vh, *args, **kwargs):
        calls.append(scroll)
        return orig(scroll, vw, vh, *args, **kwargs)

    viewer._pipeline.update = counting
    try:
        random.seed(466)
        pos = QPoint(100, 100)
        for direction in (-480, 480):  # down, then up into cold pages
            for p in random.sample(range(30), 2):
                vbar.setValue(int(vbar.maximum() * p / 29))
                qtbot.wait(200)
                for _ in range(4):
                    event = QWheelEvent(QPointF(pos), QPointF(pos), QPoint(0, 0),
                                        QPoint(0, direction), Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier,
                                        Qt.ScrollPhase.NoScrollPhase, False)
                    viewer.wheelEvent(event)
                    qtbot.wait(70)
        stats = viewer._pipeline.stats()["engine"]
        assert stats.get("failed", 0) == 0
        assert len(calls) < 1500  # bounded, no update storm
    finally:
        viewer._pipeline.update = orig
    window.close_document()
