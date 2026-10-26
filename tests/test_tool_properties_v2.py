"""M5-A1 completion tests: fill/line/drawer/constraints/scroll/chrome.

Covers the Drawboard-style property architecture: shape Outline/Fill
groups, shared Line/Arrow style system with independent endings,
Square/Circle toggles, grouped Add library, bounded scrolling shelf,
inset fading scrollbar, and the UI-only split Undo/Redo capsule.
All popup interactions use the live single-inspector path.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import fx
from app.theme.tokens import dark_tokens, light_tokens
from app.toolshelf.store import ADDABLE_KINDS, default_tool_config
from tools.models import (
    LINE_ENDINGS,
    LINE_STYLES,
    ToolInstance,
    ToolType,
    editable_properties,
)

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
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
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    return viewer


def _form(qtbot, viewer, slot):
    from app.toolshelf.tool_popup import PropertiesForm

    form = PropertiesForm(slot, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    return form


# -- fill ---------------------------------------------------------------

def test_shape_fill_enable_disable_and_opacity(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    rect = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R")
    assert rect.fill_enabled is False
    form = _form(qtbot, viewer, rect)
    fill = form._fill_section
    assert fill is not None
    # None tile is the disabled state; the shared circular preset strip
    # (same implementation as the stroke picker) selects fill colors.
    assert fill._none_option.isChecked() is True
    swatches = fill._palette._swatches
    QTest.mouseClick(fill._palette._grid_view, Qt.MouseButton.LeftButton,
                     pos=fill._palette._swatch_center(2))
    assert form._slot.fill_enabled is True
    assert form._slot.fill_color.lower() == str(
        swatches[2].property("swatch_color")).lower()
    QTest.mouseClick(fill._none_option, Qt.MouseButton.LeftButton)
    assert form._slot.fill_enabled is False
    # Fill opacity slider writes the same model value as Custom editor.
    QTest.mouseClick(fill._palette._grid_view, Qt.MouseButton.LeftButton,
                     pos=fill._palette._swatch_center(0))
    assert fill._opacity._options == []  # no generic tiles for opacity
    fill._opacity._slider.setValue(20)
    assert abs(form._slot.fill_opacity - 0.2) < 1e-9
    assert fill._opacity._num_wrap.isHidden() is True  # numeric secondary
    fill._opacity._custom_btn.click()
    assert fill._opacity._num_wrap.isHidden() is False
    qtbot.wait(900)
    fill._opacity._bridge.commit("70")
    assert abs(form._slot.fill_opacity - 0.7) < 1e-9


def test_fill_live_applies_to_store(qtbot, cleared_settings):
    import dataclasses

    viewer = _make_viewer(qtbot)
    rect = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R")
    updated = dataclasses.replace(rect, fill_enabled=True,
                                  fill_color="#e5484d", fill_opacity=0.5,
                                  line_style="dashed", square=True)
    viewer._on_tool_live_changed(updated)
    stored = viewer.tool_store.find(rect.id.value)
    assert stored.fill_enabled is True and stored.fill_color == "#e5484d"
    assert abs(stored.fill_opacity - 0.5) < 1e-9
    assert stored.line_style == "dashed" and stored.square is True


# -- line style / endings / drawer ---------------------------------------

def test_line_drawer_style_and_independent_endings(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L")
    form = _form(qtbot, viewer, line)
    section = form._line_section
    assert section is not None
    assert section.is_drawer_open() is False  # drawer closed by default
    QTest.mouseClick(section._header, Qt.MouseButton.LeftButton)
    assert section.is_drawer_open() is True
    assert set(section._style_options) == set(LINE_STYLES)
    assert set(section._start_options) == set(LINE_ENDINGS)
    assert set(section._end_options) == set(LINE_ENDINGS)
    QTest.mouseClick(section._style_options["dotted"], Qt.LeftButton)
    assert form._slot.line_style == "dotted"
    # Start and End are independent persisted fields.
    QTest.mouseClick(section._start_options["arrow"], Qt.LeftButton)
    assert form._slot.start_ending == "arrow"
    assert form._slot.end_ending == "none"
    QTest.mouseClick(section._end_options["bar"], Qt.LeftButton)
    assert form._slot.end_ending == "bar"
    assert form._slot.start_ending == "arrow"
    # Drawer collapses inside the same popup (no new window).
    QTest.mouseClick(section._header, Qt.MouseButton.LeftButton)
    assert section.is_drawer_open() is False


def test_arrow_reuses_line_system(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    # Real Add path: model-owned defaults (arrow head on by default).
    viewer._on_add_tool_chosen(ToolType.ARROW)
    arrow = viewer.tool_store.active()
    assert arrow.kind == ToolType.ARROW
    assert arrow.end_ending == "arrow"  # sensible visible default
    form = _form(qtbot, viewer, arrow)
    section = form._line_section
    assert section is not None and section.is_drawer_open() is False
    QTest.mouseClick(section._header, Qt.MouseButton.LeftButton)
    QTest.mouseClick(section._style_options["dashed"], Qt.LeftButton)
    assert form._slot.line_style == "dashed"


def test_shape_style_row_without_endings(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    rect = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R")
    form = _form(qtbot, viewer, rect)
    section = form._line_section
    assert section is not None
    assert section.is_drawer_open() is True  # compact: always visible
    assert section._header is None  # no drawer chrome for shapes
    assert not section._start_options and not section._end_options
    QTest.mouseClick(section._style_options["dashed"], Qt.LeftButton)
    assert form._slot.line_style == "dashed"


# -- square / circle ------------------------------------------------------

def test_square_circle_toggles_persist(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    rect = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R")
    form = _form(qtbot, viewer, rect)
    assert form._constraint is not None
    QTest.mouseClick(form._constraint._option, Qt.MouseButton.LeftButton)
    assert form._slot.square is True
    viewer._on_tool_live_changed(form._slot)
    assert viewer.tool_store.find(rect.id.value).square is True
    ell = viewer.tool_store.add_slot(ToolType.ELLIPSE, name="E")
    eform = _form(qtbot, viewer, ell)
    QTest.mouseClick(eform._constraint._option, Qt.MouseButton.LeftButton)
    assert eform._slot.circle is True
    poly = viewer.tool_store.add_slot(ToolType.POLYGON, name="P")
    pform = _form(qtbot, viewer, poly)
    assert pform._constraint is None  # polygon has no constraint toggle


# -- grouped library -------------------------------------------------------

def test_add_library_grouped(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    from app.toolshelf.store import ADDABLE_GROUPS
    from app.toolshelf.tool_popup import AddToolGrid, _AddCell

    assert [name for name, _k in ADDABLE_GROUPS] == [
        "Freehand", "Shapes", "Other"]
    flat = [k for _name, kinds in ADDABLE_GROUPS for k in kinds]
    assert set(flat) == set(ADDABLE_KINDS) and len(flat) == 10
    assert ToolType.LASSO in flat
    assert ToolType.PAN not in flat and ToolType.SELECTION not in flat
    viewer = _make_viewer(qtbot)
    grid = AddToolGrid(dark_tokens(), viewer)
    qtbot.addWidget(grid)
    grid.show()
    assert [t.text() for t in grid._tabs] == ["Freehand", "Shapes", "Other"]
    assert grid.current_tab() == "Freehand"
    assert len(grid.findChildren(_AddCell)) == 10
    # Tab switching shows the right membership per category.
    expected = {
        "Freehand": {ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER,
                      ToolType.LASSO},
        "Shapes": {ToolType.LINE, ToolType.ARROW, ToolType.RECTANGLE,
                    ToolType.ELLIPSE, ToolType.POLYGON},
        "Other": {ToolType.NOTE},
    }
    by_tab = dict(zip(grid._tab_names,
                      [kinds for _n, kinds in ADDABLE_GROUPS]))
    assert {n: set(k) for n, k in by_tab.items()} == expected
    for tab in grid._tabs[1:]:
        QTest.mouseClick(tab, Qt.MouseButton.LeftButton)
        qtbot.wait(50)
    assert grid.current_tab() == "Other"
    assert grid._stack.currentIndex() == 2
    page = grid._stack.currentWidget()
    assert {c._kind for c in page.findChildren(_AddCell)} == {ToolType.NOTE}
    lasso_cells = [c for c in grid.findChildren(_AddCell)
                   if c._kind == ToolType.LASSO]
    assert len(lasso_cells) == 1 and lasso_cells[0]._label() == "Lasso"


# -- scroll overflow ---------------------------------------------------------

def test_toolbar_scrolls_after_threshold(qtbot, cleared_settings):
    from app.toolshelf.shelf import MAX_VISIBLE_CUSTOMS, MIN_VISIBLE_CUSTOMS
    from app.toolshelf.slot_button import CELL

    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    cap = shelf._visible_custom_cap()
    assert MIN_VISIBLE_CUSTOMS <= cap <= MAX_VISIBLE_CUSTOMS
    for i in range(cap + 4):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"P{i}")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    inner = shelf._scroll._inner_layout
    assert inner.count() == cap + 4
    # Bounded: viewport shows the cap, overflow scrolls.
    bar = (shelf._scroll.horizontalScrollBar()
           if shelf._orientation() == "horizontal"
           else shelf._scroll.verticalScrollBar())
    assert bar.maximum() > 0
    along = (shelf.width() if shelf._orientation() == "horizontal"
             else shelf.height())
    # Growth stops past the cap: four more customs change nothing.
    for i in range(4):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"Q{i}")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    along2 = (shelf.width() if shelf._orientation() == "horizontal"
              else shelf.height())
    assert along2 == along
    assert shelf._scroll._inner_layout.count() == cap + 8
    # Core + divider + Add stay fixed and reachable.
    assert shelf._handle.isVisible() and shelf._add.isVisible()
    core_ids = {s.id.value for s in viewer.tool_store.custom_slots}
    assert core_ids <= set(viewer.tool_shelf._buttons)


def test_visible_cap_adaptive_bounds(qtbot, cleared_settings):
    from app.toolshelf.shelf import MAX_VISIBLE_CUSTOMS, MIN_VISIBLE_CUSTOMS

    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    # Narrow canvas clamps to the floor; wide canvas to the ceiling.
    viewer.resize(500, 400)
    shelf.refresh()
    qtbot.wait(100)
    assert shelf._visible_custom_cap() == MIN_VISIBLE_CUSTOMS
    viewer.resize(1600, 1000)
    shelf.set_dock("top")
    qtbot.wait(100)
    assert shelf._visible_custom_cap() == MAX_VISIBLE_CUSTOMS
    # Mid canvas lands strictly inside the band.
    viewer.resize(1100, 750)
    shelf.set_dock("left")
    qtbot.wait(100)
    mid = shelf._visible_custom_cap()
    assert MIN_VISIBLE_CUSTOMS <= mid <= MAX_VISIBLE_CUSTOMS


# -- scrollbar ---------------------------------------------------------------

def test_scrollbar_inset_and_fade(qtbot, cleared_settings):
    from app.viewer import _SCROLLBAR_INSET_X, _SCROLLBAR_INSET_Y

    viewer = _make_viewer(qtbot)
    bar = viewer._scrollbar
    assert bar.x() + bar.width() == viewer.width() - _SCROLLBAR_INSET_X
    assert bar.y() == _SCROLLBAR_INSET_Y
    assert _SCROLLBAR_INSET_X > 6  # clears the native resize zone
    assert viewer._scrollbar_fx.opacity() == 0.0  # idle: faded out
    viewer._poke_scrollbar()
    qtbot.wait(300)
    assert viewer._scrollbar_fx.opacity() > 0.9
    # Scroll mechanics untouched: value changes still flow.
    bar.setRange(0, 100)
    bar.setValue(40)
    assert bar.value() == 40


# -- HUD capsule ---------------------------------------------------------------

def test_undo_redo_split_capsule(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    hud = viewer.undo_hud
    assert hud.width() == 80 and hud.height() == 40
    assert hud._undo_btn.isEnabled() is False
    assert hud._redo_btn.isEnabled() is False
    # One cohesive object: square faces must not show through.
    assert hud._undo_btn.styleSheet().count("transparent") >= 1
    calls = []
    viewer._request_update = lambda: calls.append(1)  # type: ignore[method-assign]
    hud._undo_btn.setEnabled(True)
    hud._redo_btn.setEnabled(True)
    QTest.mouseClick(hud._undo_btn, Qt.MouseButton.LeftButton)
    QTest.mouseClick(hud._redo_btn, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert calls == []  # UI-only: no renders submitted
    viewer.update_chrome_tokens(light_tokens())
    viewer.update_chrome_tokens(dark_tokens())


# -- icons ----------------------------------------------------------------------

def test_selection_lasso_icons_distinct(qtbot, cleared_settings):
    from app.icons import icon_names, pixmap

    assert "selection" in icon_names() and "lasso" in icon_names()
    app = QApplication.instance()
    sel = pixmap("selection", "#2b6cff", 22).toImage()
    las = pixmap("lasso", "#2b6cff", 22).toImage()

    def _bytes(img):
        from PySide6.QtCore import QBuffer, QIODevice

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    assert _bytes(sel) != _bytes(las)
    assert _bytes(sel) != _bytes(pixmap("pan", "#2b6cff", 22).toImage())
    viewer = _make_viewer(qtbot)
    kinds = [s.kind for s in viewer.tool_store.slots]
    assert kinds[:2] == [ToolType.PAN, ToolType.SELECTION]
