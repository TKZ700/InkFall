"""M4.6 redesign tests: chrome removal, sections, toasts, palette rules."""

from pathlib import Path

import pytest

from app.main_window import MainWindow
from document.manager import DocumentManager
from library.service import LibraryService
from library.store import LibraryStore

pytestmark = pytest.mark.usefixtures("qapp")

APP = Path(__file__).resolve().parent.parent / "app"


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


def test_no_menu_bar_shortcuts_remain(shell):
    window, _service = shell
    from PySide6.QtWidgets import QMenuBar

    assert window.findChild(QMenuBar) is None
    seqs = {a.shortcut().toString() for a in window.actions()}
    for expected in ("Ctrl+O", "Ctrl+I", "Ctrl+T", "Ctrl+L", "F11"):
        assert expected in seqs, f"missing shortcut {expected}"


def test_single_new_action(shell):
    window, _service = shell
    btn = window.library_page._new_btn
    assert btn is not None
    assert not hasattr(window, "_sidebar")  # sidebar never reintroduced
    # M4.6.5: circular icon-only New (paired with Search), no menu/button.
    assert btn.text() == ""
    assert btn.width() == btn.height() == 38
    assert not btn.icon().isNull()
    assert not hasattr(window.library_page, "_new_menu")


def test_section_headers_structure(shell, tmp_path):
    window, service = shell
    folder = service.create_folder("Work")
    service.import_path(_make_pdf(tmp_path, "a.pdf"), folder.id)
    service.import_path(_make_pdf(tmp_path, "b.pdf"))
    window.library_page.refresh()
    model = window.library_page._model
    rows = [model.row_at(i) for i in range(model.rowCount())]
    headers = [r for r in rows if r is not None and r.is_header]
    eyebrows = [h.header.eyebrow for h in headers]
    # M4.6.3: Folders section renamed to Categories (folder-model backed).
    assert "Categories" in eyebrows and "Documents" in eyebrows
    # M4.6.7: headers are keyboard-navigable collapse toggles —
    # selectable for focus, but clicks/keys never open anything.
    from PySide6.QtCore import Qt

    for i, r in enumerate(rows):
        if r is not None and r.is_header:
            assert model.flags(model.index(i, 0)) & Qt.ItemFlag.ItemIsSelectable
            assert model.flags(model.index(i, 0)) & Qt.ItemFlag.ItemIsEnabled


def test_recent_section_visibility_rules(shell, tmp_path, qtbot):
    window, service = shell
    window.library_page.refresh()

    def eyebrows():
        model = window.library_page._model
        return [model.row_at(i).header.eyebrow
                for i in range(model.rowCount())
                if (r := model.row_at(i)) is not None and r.is_header]

    assert "Recent" not in eyebrows()  # no history: hidden
    item = service.import_path(_make_pdf(tmp_path))
    assert window.open_library_item(item.id) is True
    # M4.6.7: recency lands with the deferred open finish.
    qtbot.waitUntil(lambda: service.get_item(item.id).last_opened_ts is not None,
                    timeout=15000)
    window.show_library()
    window.library_page._search.clear()
    window.library_page.refresh()
    assert "Recent" in eyebrows()
    model = window.library_page._model
    recent_docs = [model.row_at(i) for i in range(model.rowCount())
                   if (r := model.row_at(i)) is not None
                   and not r.is_header and not r.is_folder]
    assert len(recent_docs) >= 1
    window.close_document()


def test_sort_menu_options(shell):
    window, _service = shell
    from app.library.library_page import _SORT_OPTIONS
    from library.service import SortKey

    assert len(_SORT_OPTIONS) == 5
    window.library_page._sort_index = 1
    window.library_page._sync_sort_label()
    assert "Recently modified" in window.library_page._sort_btn.text()
    assert _SORT_OPTIONS[1][1] == SortKey.MODIFIED


def test_toast_queues_and_clears(shell, qtbot):
    window, _service = shell
    window.toast.show_message("Imported 2 PDFs")
    assert window.toast.pending_count() >= 1
    qtbot.waitUntil(lambda: window.toast.pending_count() == 0, timeout=8000)


def test_toast_themes(shell):
    window, _service = shell
    window._toggle_theme()
    assert window.toast._label.text() is not None


def test_grid_card_geometry_consistent(shell, tmp_path):
    """Shared alignment grid: every doc row reports identical grid size."""
    window, service = shell
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        service.import_path(_make_pdf(tmp_path, name))
    window.library_page.refresh()
    delegate = window.library_page._delegate
    view = window.library_page._view
    from PySide6.QtWidgets import QStyleOptionViewItem

    sizes = set()
    model = window.library_page._model
    for i in range(model.rowCount()):
        row = model.row_at(i)
        if row is None or row.is_header or row.is_folder:
            continue
        opt = QStyleOptionViewItem()
        opt.widget = view
        size = delegate.sizeHint(opt, model.index(i, 0))
        sizes.add((size.width(), size.height()))
    assert len(sizes) == 1  # one shared cell geometry


def test_library_populates_on_launch(shell, tmp_path):
    """Fresh window shows documents without any manual refresh call."""
    from document.manager import DocumentManager
    from library.service import LibraryService
    from library.store import LibraryStore

    from tests.helpers import make_rich_pdf

    from app.main_window import MainWindow

    pdf = make_rich_pdf(str(tmp_path / "launch.pdf"), pages=1)
    window, service = shell
    service.import_path(pdf)
    window2 = MainWindow(DocumentManager(), library_service=service)
    try:
        docs = [window2.library_page._model.row_at(i)
                for i in range(window2.library_page._model.rowCount())]
        assert any(r is not None and not r.is_header and not r.is_folder for r in docs)
    finally:
        window2.close()


def test_library_body_uses_client_width(shell):
    # M4.6.8 live-width truth: no max-width cap on the Library body —
    # the body fills the page width (maximized leaves no dead strip).
    from PySide6.QtWidgets import QWidget
    window, _service = shell
    page = window.library_page
    body = page._body
    assert isinstance(body, QWidget)
    assert body.maximumWidth() >= 32767  # no effective cap
    assert body.width() == page.width()


def test_viewer_bar_height(shell):
    window, _service = shell
    assert window._viewer_bar.height() == 48
    assert window._viewer_bar.minimumHeight() <= 48


def test_palette_contrast(shell):
    from app.theme.tokens import dark_tokens, light_tokens

    def lum(h):
        h = h.strip("#").split("(")[0]
        if "," in h:  # rgba() washes: skip (translucent by design)
            return None
        r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def cr(a, b):
        la, lb = lum(a), lum(b)
        x, y = sorted([la, lb])
        return (y + 0.05) / (x + 0.05)

    for t in (dark_tokens(), light_tokens()):
        assert cr(t.text_primary, t.background) >= 10
        assert cr(t.text_secondary, t.background) >= 4.5
        assert cr(t.text_primary, t.surface) >= 10


def test_gold_reserved_for_favorites():
    """Gold hex may only appear in favorite-star contexts."""
    hits = []
    for path in list(APP.rglob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for gold in ("D7A34A", "d7a34a"):
                if gold in line:
                    hits.append((path.relative_to(APP.parent).as_posix(), i, line.strip()))
    assert hits, "gold must exist somewhere (favorites)"
    for rel, _i, line in hits:
        assert "star" in line.lower() or "favorite" in line.lower() or "gold" in line.lower(), \
            f"gold outside favorite context: {rel}: {line}"


def test_no_accent_in_body_copy_paths():
    """Accent hex must not leak into card fills or body text styles."""
    from app.theme.tokens import dark_tokens

    t = dark_tokens()
    assert t.card_background in ("#00000000", "transparent")
    assert t.card_border in ("#00000000", "transparent")
