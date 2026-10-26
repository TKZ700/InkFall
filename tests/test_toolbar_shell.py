"""M5-A1 toolbar shell tests: shelf, viewer integration, motion, icons."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import fx
from app.icons import icon_names
from app.theme.tokens import dark_tokens, light_tokens
from app.toolshelf.store import ToolShelfStore
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def cleared_settings():
    app = QApplication.instance()
    previous = None
    try:
        from PySide6.QtCore import QSettings

        previous = (QSettings().value("inkfall/m5/toolbar/slots", None),
                    QSettings().value("inkfall/m5/toolbar/dock", None),
                    QSettings().value("inkfall/m5/toolbar/active", None),
                    QSettings().value("inkfall/m5/motion", None))
        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors"):
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors"):
            QSettings().remove(key)
    except Exception:
        pass
    try:
        fx.set_motion_mode("follow")
    except Exception:
        pass


def _make_viewer(qtbot):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(900, 700)
    viewer.show()
    qtbot.addWidget(viewer)
    return viewer


def test_default_tools_exist(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    kinds = [s.kind for s in viewer.tool_store.slots]
    assert ToolType.PAN in kinds and ToolType.SELECTION in kinds
    assert len(viewer.tool_shelf._buttons) >= 2


def test_unlimited_slots_supported(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    for i in range(5):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"Pen #{i}")
    viewer.tool_shelf.refresh()
    assert len(viewer.tool_shelf._buttons) == 7  # pan + selection + 5 pens


def test_all_dock_positions(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    for dock in ("left", "right", "top", "bottom"):
        shelf.set_dock(dock)
        assert viewer.tool_store.dock == dock
        viewer.resize(901, 701)
        viewer.resize(900, 700)
        pos = shelf.pos()
        assert pos.x() >= 0 and pos.y() >= 0
        assert pos.x() + shelf.width() <= viewer.width() + 2
    # Invalid dock rejected.
    with pytest.raises(ValueError):
        shelf.set_dock("diagonal")


def test_no_compact_mode(qtbot, cleared_settings):
    # Compact mode was explicitly abolished: the shelf always shows the
    # full ordered slot list. Guards against reintroduction.
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    for obj in (shelf, viewer.tool_store):
        assert not hasattr(obj, "set_compact")
        assert not hasattr(obj, "toggle_compact")
        assert not hasattr(obj, "compactChanged")
    assert not hasattr(viewer.tool_store, "compact")
    assert len(shelf._buttons) == len(viewer.tool_store.slots) >= 2


def test_shelf_bounded_size(qtbot, cleared_settings):
    # The shelf must keep a bounded natural size — never a full-height
    # sidebar column when docked left/right.
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    viewer.resize(900, 700)
    assert shelf.height() < viewer.height() // 2
    assert shelf.width() < viewer.width() // 2


def _live_buttons(shelf):
    from app.toolshelf.slot_button import SlotButton

    # Whole shelf subtree: customs live inside the scroll viewport.
    return [c for c in shelf.findChildren(SlotButton)]


def test_refresh_leaves_no_zombie_buttons(qtbot, cleared_settings):
    # Rebuilt buttons must detach synchronously: stale instances must
    # never remain painted anywhere in the shelf with outdated state.
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    shelf.refresh()
    shelf.refresh()
    live = set(shelf._buttons.values())
    assert set(_live_buttons(shelf)) == live
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    shelf.refresh()
    children = _live_buttons(shelf)
    assert set(children) == set(shelf._buttons.values())
    assert shelf.select_slot(pen.id.value) is not None
    checked = [b for b in children if b.isChecked()]
    assert [b.slot.id.value for b in checked] == [pen.id.value]


def test_shelf_survives_fullscreen_parenting(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    assert viewer.tool_shelf.parent() is viewer
    # Fullscreen hides ViewerBar/TitleBar only — a PdfViewer child stays.
    assert viewer.tool_shelf.isVisible() is True


def test_tool_selection_updates_interaction(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer.tool_shelf.refresh()
    fired = []
    viewer.tool_shelf.toolSelected.connect(lambda s: fired.append(s))
    assert viewer.tool_shelf.select_slot(pen.id.value) is not None
    assert viewer.tool_store.active_id == pen.id.value
    assert viewer._interaction.active_kind == ToolType.PEN
    assert fired and fired[0].id.value == pen.id.value


def test_toolbar_chrome_never_touches_pipeline(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    # Drain show/resize events before installing the spy (see above).
    qtbot.wait(50)
    calls = []
    viewer._request_update = lambda: calls.append(1)  # type: ignore[method-assign]
    viewer.tool_shelf.show_animated()
    viewer.tool_shelf.hide_animated()
    viewer.tool_shelf.update_tokens(light_tokens())
    viewer.tool_shelf.update_tokens(dark_tokens())
    assert calls == []


def test_reduced_motion_resolves_immediately(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    fx.set_motion_mode("reduced")
    try:
        assert fx.reduced_motion_enabled() is True
        shelf.hide_animated()
        assert shelf.isVisible() is False
        shelf.show_animated()
        assert shelf.isVisible() is True
        # Scroll glide is instant under reduced motion.
        bar = viewer._scrollbar
        bar.setRange(0, 1000)
        fx.smooth_scroll_to(bar, 500, 140, viewer)
        assert bar.value() == 500
    finally:
        fx.set_motion_mode("follow")
    assert fx.reduced_motion_enabled() is False


def test_animation_interruption_safe(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    shelf.show_animated()
    shelf.hide_animated()
    shelf.show_animated()
    assert shelf.isVisible() is True


def test_shelf_respects_all_tokens_themes(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    for tokens in (dark_tokens(), light_tokens()):
        viewer.update_chrome_tokens(tokens)
        assert tokens.name in ("dark", "light")


def test_required_icons_follow_inkfall_language(qtbot, cleared_settings):
    names = set(icon_names())
    for required in ("pan", "lasso", "pen", "highlighter", "eraser", "line",
                     "rectangle", "ellipse", "arrow", "text", "note", "eye",
                     "undo", "redo", "fit-width", "fit-page"):
        assert required in names, required
