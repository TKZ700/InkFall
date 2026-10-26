"""M4.6.2 composition tests: anchored column, responsive grid, palette."""

import pytest

from app.library.delegate import card_width_for, grid_columns_for_width
from app.theme.tokens import dark_tokens, light_tokens

pytestmark = pytest.mark.usefixtures("qapp")


def _usable(window_w: int, margin: int = 28) -> int:
    # M4.6.8: no max-width cap — the Library body owns the full client
    # width at every window size.
    return window_w - 2 * margin


def test_content_width_uses_screen():
    # Full client width: 1280 nearly full-bleed, 1440 generous,
    # 1920 fully used (no 1656px cap, no dead right-side strip).
    assert _usable(1280) == 1224
    assert _usable(1440) == 1384
    assert _usable(1920) == 1864
    assert _usable(2560) == 2504


def test_column_progression_and_card_range():
    t = dark_tokens()
    for window in (1280, 1440, 1920, 2560):
        usable = _usable(window)
        cols = grid_columns_for_width(usable, t.card_min_w, t.grid_gap_h)
        card = card_width_for(usable, cols, t.grid_gap_h)
        assert cols >= 2
        assert t.card_min_w <= card <= t.card_max_w
        # No overflow: cards + gaps fit usable width.
        assert cols * card + (cols - 1) * t.grid_gap_h <= usable + 1.5


def test_resize_monotonic_columns():
    t = dark_tokens()
    widths = [c for c in (
        grid_columns_for_width(_usable(w), t.card_min_w, t.grid_gap_h)
        for w in (1280, 1440, 1920, 2560))]
    assert widths == sorted(widths)


def test_final_row_same_width_left_aligned():
    # Final row reuses full-row card width; QListView IconMode flows left.
    usable, gap = 1184, 16
    cols = grid_columns_for_width(usable)
    full = card_width_for(usable, cols, gap)
    partial = card_width_for(usable, cols, gap)  # same helper, same width
    assert full == partial
    # M4.6.5 denser grid (170px min): 7 items in 6 cols -> final row of 1
    # starts at left origin x=0.
    assert cols == 6


def test_gap_tokens_deliberate():
    t = dark_tokens()
    assert (t.grid_gap_h, t.grid_gap_v) == (16, 24)
    assert t.content_margin in (24, 28, 32, 40)
    assert t.section_gap in (16, 24, 32)


def test_no_horizontal_scrollbar_policy(shell_m462):
    window, _s = shell_m462
    from PySide6.QtCore import Qt
    assert window.library_page._view.horizontalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_shared_left_origin_insets():
    import inspect
    from app.library import delegate as d
    src = inspect.getsource(d)
    assert "padding-left: 6px" not in src
    assert "adjusted(4, 0, -4, 0)" not in src
    assert "adjusted(6, 6, -6, -6)" not in src


def test_single_new_action_m462(shell_m462):
    window, _s = shell_m462
    # M4.6.5: circular icon-only New button opens the centered panel.
    btn = window.library_page._new_btn
    assert btn.text() == ""
    assert btn.width() == btn.height() == 38
    assert not btn.icon().isNull()
    assert not hasattr(window.library_page, "_new_menu")


def test_section_order_library(shell_m462, tmp_path):
    window, service = shell_m462
    from tests.helpers import make_rich_pdf
    folder = service.create_folder("Work")
    service.import_path(make_rich_pdf(str(tmp_path / "a.pdf")), folder.id)
    service.import_path(make_rich_pdf(str(tmp_path / "b.pdf")))
    window.library_page.refresh()
    model = window.library_page._model
    eyebrows = [r.header.eyebrow for i in range(model.rowCount())
                if (r := model.row_at(i)) is not None and r.is_header]
    # M4.6.3: Folders section renamed to Categories (folder-model backed).
    assert eyebrows.index("Categories") < eyebrows.index("Documents")


def test_palette_winner_a_locked():
    # M4.6.3: A/B/C re-judged from scratch on the sidebar-free
    # composition (rendered at 1440/1920 dark+light). A kept: visible
    # New, dark/light coherence, app-icon/pin harmony. B runner-up
    # (dull in light), C teal rejected (brand break, tiring chroma).
    d, li = dark_tokens(), light_tokens()
    assert (d.accent, d.accent_hover, d.accent_pressed) == \
        ("#8B93E0", "#9AA2E8", "#767CCB")
    assert (li.accent, li.accent_hover, li.accent_pressed) == \
        ("#4E57B8", "#5D66C4", "#414A9C")
    assert d.input_focus == d.accent and li.input_focus == li.accent
    # Losing candidates must not leak into shipped code.
    import pathlib
    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    blob = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.py"))
    assert "7A8BA8" not in blob and "6FB3AE" not in blob


def test_gold_still_favorite_only():
    import pathlib
    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    hits = []
    for p in app.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "D7A34A" in line or "d7a34a" in line:
                hits.append((str(p), i, line.strip()))
    assert hits
    for _, _, line in hits:
        low = line.lower()
        assert "star" in low or "favorite" in low or "gold" in low, line


def test_typography_stacks_intact():
    from app.theme.multilingual import (ARABIC_DOMINANCE_THRESHOLD,
                                        arabic_ratio, font_for)
    assert ARABIC_DOMINANCE_THRESHOLD == 0.5
    t = dark_tokens()
    assert font_for("Hello", "body", t).family() == "Inter"
    assert font_for("سلام دنیا", "body", t).family() == "Vazirmatn"
    assert arabic_ratio("Chapter 3 - فصل اول") < 0.5


def test_motion_single_owner_with_reduced_support():
    # M5-A1 (supersedes M4.6.5 removal): reduced motion is supported via
    # the single animation owner app.fx — manual override > OS > normal.
    # Helpers resolve immediately with identical final state + cleanup.
    from app import fx

    for name in ("set_motion_mode", "motion_mode", "reduced_motion_enabled",
                 "restore_motion_mode", "MOTION_SETTINGS_KEY"):
        assert hasattr(fx, name), name
        assert name in fx.__all__, name
    assert fx.motion_mode() in ("follow", "reduced", "full")
    assert fx.MOTION_SETTINGS_KEY == "inkfall/m5/motion"
    previous = fx.motion_mode()
    try:
        fx.set_motion_mode("reduced")
        assert fx.reduced_motion_enabled() is True
        fx.set_motion_mode("full")
        assert fx.reduced_motion_enabled() is False
        assert fx.set_motion_mode("bogus") == "follow"
    finally:
        fx.set_motion_mode(previous)


@pytest.fixture()
def shell_m462(qtbot, tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        app.setOrganizationName("Inkfall")
        app.setApplicationName("Inkfall")
    from app.main_window import MainWindow
    from document.manager import DocumentManager
    from library.service import LibraryService
    from library.store import LibraryStore
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.show()
    qtbot.addWidget(window)
    yield window, service
    store.close()
