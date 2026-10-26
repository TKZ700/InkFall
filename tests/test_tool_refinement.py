"""M5-A1 refinement tests: capabilities, dblclick popups, Add, docking."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from app import fx
from app.theme.tokens import dark_tokens
from app.toolshelf.store import ADDABLE_KINDS, default_tool_config
from tools.models import (
    ToolInstance,
    ToolType,
    editable_properties,
    has_editable_properties,
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


def _dblclick(widget, qtbot):
    pos = QPoint(26, 26)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPointF(pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    widget.mouseDoubleClickEvent(event)
    qtbot.wait(10)


# -- capability model ------------------------------------------------------

def test_capability_model_matches_spec():
    assert editable_properties(ToolType.PAN) == ()
    assert editable_properties(ToolType.SELECTION) == ()
    assert editable_properties(ToolType.LASSO) == ()
    assert set(editable_properties(ToolType.PEN)) == {
        "color", "width_mm", "opacity"}
    assert set(editable_properties(ToolType.HIGHLIGHTER)) == {
        "color", "width_mm", "opacity", "straight_line"}
    assert editable_properties(ToolType.ERASER) == ("eraser_mode", "width_mm")
    assert set(editable_properties(ToolType.RECTANGLE)) == {
        "color", "width_mm", "opacity", "fill_enabled", "fill_color",
        "fill_opacity", "line_style", "square"}
    assert set(editable_properties(ToolType.ELLIPSE)) == {
        "color", "width_mm", "opacity", "fill_enabled", "fill_color",
        "fill_opacity", "line_style", "circle"}
    # M6.7.2: Polygon always connects and always closes internally, so
    # neither is a user-editable property (no toggle, no hidden setting).
    assert set(editable_properties(ToolType.POLYGON)) == {
        "color", "width_mm", "opacity", "fill_enabled", "fill_color",
        "fill_opacity", "line_style", "snap_angle"}
    assert set(editable_properties(ToolType.LINE)) == {
        "color", "width_mm", "opacity", "line_style", "start_ending",
        "end_ending", "snap_angle", "connect_lines"}
    assert set(editable_properties(ToolType.ARROW)) == {
        "color", "width_mm", "opacity", "line_style", "start_ending",
        "end_ending", "snap_angle", "connect_lines"}
    assert editable_properties(ToolType.NOTE) == ("color",)
    assert not hasattr(ToolType, "TEXT")
    for kind in ToolType:
        assert isinstance(editable_properties(kind), tuple)
    for kind in ToolType:
        assert "blend_mode" not in editable_properties(kind)


def test_has_editable_properties_guards_pan_selection_lasso():
    pan = ToolInstance(kind=ToolType.PAN, name="Pan")
    select = ToolInstance(kind=ToolType.SELECTION, name="Select")
    lasso = ToolInstance(kind=ToolType.LASSO, name="Lasso")
    pen = ToolInstance(kind=ToolType.PEN, name="Pen #1")
    assert has_editable_properties(pan) is False
    assert has_editable_properties(select) is False
    assert has_editable_properties(lasso) is False
    assert has_editable_properties(pen) is True


def test_add_defaults_valid_for_every_kind():
    from PySide6.QtGui import QColor

    assert len(ADDABLE_KINDS) == 10
    assert ToolType.PAN not in ADDABLE_KINDS
    assert ToolType.SELECTION not in ADDABLE_KINDS
    assert ToolType.LASSO in ADDABLE_KINDS
    for kind in ADDABLE_KINDS:
        slot = default_tool_config(kind)
        assert slot.kind == kind  # ToolInstance __post_init__ validated
        assert slot.width_mm > 0 and 0.0 <= slot.opacity <= 1.0
        # Every new tool must paint a valid visible glyph immediately.
        color = QColor(slot.color)
        assert color.isValid() and color.alpha() > 0
        if kind in (ToolType.RECTANGLE, ToolType.ELLIPSE, ToolType.POLYGON):
            assert slot.fill_enabled is False  # fill opt-in, never surprise
            assert QColor(slot.fill_color).isValid()


# -- double-click properties -------------------------------------------------

def test_dblclick_pen_opens_popup(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer.tool_shelf.refresh()
    button = viewer.tool_shelf._buttons[pen.id.value]
    _dblclick(button, qtbot)
    assert viewer._tool_popup is not None
    assert viewer._tool_popup.isVisible() is True
    viewer._close_tool_popup()


def test_dblclick_pan_selection_open_nothing(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    for kind in (ToolType.PAN, ToolType.SELECTION):
        slot = next(s for s in viewer.tool_store.slots if s.kind == kind)
        button = viewer.tool_shelf._buttons[slot.id.value]
        _dblclick(button, qtbot)
        assert viewer._tool_popup is None
        assert viewer.tool_store.active_id is not None


def test_live_edit_applies_immediately_no_save_cancel(qtbot, cleared_settings):
    """Live inspector: every change applies at once; no Save/Cancel."""
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    viewer.tool_shelf.refresh()
    button = viewer.tool_shelf._buttons[pen.id.value]
    _dblclick(button, qtbot)
    form = viewer._tool_popup._content
    assert isinstance(form, PropertiesForm)
    # No Save/Cancel buttons anywhere in the popup; live `changed` signal.
    names = {b.text() for b in viewer._tool_popup.findChildren(QPushButton)}
    assert "Save" not in names and "Cancel" not in names
    assert hasattr(form, "changed")
    # A preset click applies to the store immediately (no commit step).
    form._color_section._emit("#00ff00")
    assert viewer.tool_store.find(pen.id.value).color.lower() == "#00ff00"
    # Closing keeps the applied change (no revert, no cancel path).
    viewer._tool_popup.close_popup(animate=False)
    viewer._on_tool_popup_closed()
    assert viewer.tool_store.find(pen.id.value).color.lower() == "#00ff00"


def test_live_preview_updates(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000",
                                     width_mm=0.5)
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    before = form._preview._slot
    assert before is not None and before.color.lower() == "#ff0000"
    form._color_section._emit("#00ff00")
    assert form._preview._slot.color.lower() == "#00ff00"


# -- Add flow ------------------------------------------------------------------

def test_add_cell_exists_and_opens_picker(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    assert shelf._add is not None
    assert shelf._add not in shelf._buttons.values()
    fired = []
    shelf.addRequested.connect(lambda b: fired.append(b))
    shelf._add.click()
    assert len(fired) == 1
    viewer._close_tool_popup()


def test_add_grid_covers_every_kind(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import ADD_GRID_META, AddToolGrid

    assert {kind for kind, _label in ADD_GRID_META} == set(ADDABLE_KINDS)
    viewer = _make_viewer(qtbot)
    grid = AddToolGrid(dark_tokens(), viewer)
    qtbot.addWidget(grid)
    from app.toolshelf.tool_popup import _AddCell

    assert len(grid.findChildren(_AddCell)) == 10


def test_add_creates_activates_persists(qtbot, cleared_settings, tmp_path):
    viewer = _make_viewer(qtbot)
    before = len(viewer.tool_store.slots)
    viewer._on_add_requested()
    assert viewer._tool_popup is not None
    viewer._on_add_tool_chosen(ToolType.HIGHLIGHTER)
    assert len(viewer.tool_store.slots) == before + 1
    assert viewer.tool_store.active().kind == ToolType.HIGHLIGHTER
    assert viewer._tool_popup is None
    # Persisted round-trip keeps the new slot.
    from PySide6.QtCore import QSettings

    settings = QSettings(str(tmp_path / "add.ini"), QSettings.Format.IniFormat)
    viewer.tool_store.persist(settings)
    settings.sync()
    from app.toolshelf.store import ToolShelfStore

    revived = ToolShelfStore.restore(settings)
    assert any(s.kind == ToolType.HIGHLIGHTER for s in revived.slots)


# -- repositioning ---------------------------------------------------------------

def test_handle_exists_and_is_not_a_tool(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    assert handle is not None
    assert handle not in viewer.tool_shelf._buttons.values()
    assert handle.accessibleName() == "Reposition toolbar"


def test_short_click_does_nothing_destructive(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    active_before = viewer.tool_store.active_id
    dock_before = viewer.tool_store.dock
    pos = QPoint(26, 26)
    press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(pos),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    handle.mousePressEvent(press)
    handle.mouseReleaseEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    assert viewer._docking is False
    assert viewer._dock_targets == []
    assert viewer.tool_store.active_id == active_before
    assert viewer.tool_store.dock == dock_before


def test_hold_drag_release_snaps_and_persists(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    assert viewer.tool_store.dock == "left"
    pos = QPoint(26, 26)
    handle.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    handle._on_hold_timeout()
    assert viewer._docking is True
    assert len(viewer._dock_targets) == 4
    viewer._on_reposition_moved(viewer.mapToGlobal(QPoint(viewer.width() // 2, 20)))
    assert viewer._dock_preview == "top"
    handle.mouseReleaseEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    assert viewer._docking is False
    assert viewer._dock_targets == []
    assert viewer.tool_store.dock == "top"
    assert viewer.tool_shelf._orientation() == "horizontal"


def test_docking_all_sides(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    w, h = viewer.width(), viewer.height()
    corners = {"top": (w // 2, 10), "bottom": (w // 2, h - 10),
               "left": (10, h // 2), "right": (w - 10, h // 2)}
    for dock, (x, y) in corners.items():
        viewer._on_reposition_started()
        viewer._on_reposition_moved(viewer.mapToGlobal(QPoint(x, y)))
        assert viewer._dock_preview == dock
        viewer._on_reposition_finished()
        assert viewer.tool_store.dock == dock
        assert viewer.tool_shelf.pos().x() >= 0
        assert viewer.tool_shelf.pos().y() >= 0


def test_reposition_ui_causes_no_render_submissions(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    # Drain show/resize events before installing the spy: a pending
    # initial resize would otherwise count as a submission.
    qtbot.wait(50)
    calls = []
    viewer._request_update = lambda: calls.append(1)  # type: ignore[method-assign]
    viewer._on_reposition_started()
    viewer._on_reposition_moved(viewer.mapToGlobal(QPoint(50, 50)))
    viewer._on_reposition_finished()
    viewer._on_add_requested()
    viewer._close_tool_popup()
    assert calls == []


# -- lasso icon --------------------------------------------------------------------

def test_lasso_icon_redesigned_and_distinct():
    from app.icons import icon_names, pixmap

    assert "lasso" in icon_names() and "pan" in icon_names()
    lasso = pixmap("lasso", "#a7aebb", 22)
    pan = pixmap("pan", "#a7aebb", 22)
    assert not lasso.isNull() and not pan.isNull()

    def _bytes(pm) -> bytes:
        from PySide6.QtGui import QImage

        return bytes(pm.toImage().convertToFormat(
            QImage.Format.Format_ARGB32).constBits())

    assert _bytes(lasso) != _bytes(pan)


# -- core/custom separation ----------------------------------------------------

def test_is_core_tool_role_check():
    from tools.models import CORE_KINDS, is_core_tool

    assert set(CORE_KINDS) == {ToolType.PAN, ToolType.SELECTION}
    assert is_core_tool(ToolInstance(kind=ToolType.PAN, name="Pan")) is True
    assert is_core_tool(ToolInstance(kind=ToolType.SELECTION, name="Select")) is True
    assert is_core_tool(ToolInstance(kind=ToolType.LASSO, name="Lasso")) is False
    for kind in (ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER,
                  ToolType.LINE, ToolType.POLYGON, ToolType.NOTE):
        assert is_core_tool(ToolInstance(kind=kind, name="x")) is False


def test_core_tools_pinned_first_with_boundaries(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer.tool_store.add_slot(ToolType.HIGHLIGHTER, name="HL #1")
    shelf = viewer.tool_shelf
    shelf.refresh()
    kinds = [s.kind for s in viewer.tool_store.slots]
    assert kinds[:2] == [ToolType.PAN, ToolType.SELECTION]
    assert viewer.tool_store.core_slots[0].kind == ToolType.PAN
    assert all(s.kind not in (ToolType.PAN, ToolType.SELECTION)
               for s in viewer.tool_store.custom_slots)
    # Exactly TWO morphing boundaries, named, no static divider widgets.
    assert shelf._edge_prev is not None
    assert shelf._edge_next is not None
    assert (shelf._edge_prev.accessibleName()
            == "Boundary between core and custom tools")
    assert (shelf._edge_next.accessibleName()
            == "Boundary between custom tools and Add")
    from app.toolshelf import shelf as shelf_mod

    assert not hasattr(shelf_mod, "_DividerCell")
    assert shelf.findChildren(QWidget, "") is not None
    for child in shelf.findChildren(QWidget):
        assert child.__class__.__name__ != "_DividerCell"
    layout = shelf.layout()
    order = [layout.itemAt(i).widget() for i in range(layout.count())]
    prev_idx = order.index(shelf._edge_prev)
    buttons = shelf._buttons
    core_ids = {s.id.value for s in viewer.tool_store.core_slots}
    last_core_idx = max(order.index(buttons[sid]) for sid in core_ids)
    assert last_core_idx < prev_idx
    # Customs live in the bounded scroll viewport between the two
    # boundaries; the end-cap cluster is boundary2, Add, then the move
    # handle (adjacent end-cap, never stacked).
    assert order[prev_idx + 1] is shelf._scroll
    assert order[prev_idx + 2] is shelf._edge_next
    assert order[prev_idx + 3] is shelf._add
    assert order[-2] is shelf._add and order[-1] is shelf._handle
    inner = shelf._scroll._inner_layout
    inner_buttons = [inner.itemAt(i).widget()
                     for i in range(inner.count())]
    assert {b for b in buttons.values()
            if b not in order} == set(inner_buttons)
    custom_ids = {s.id.value for s in viewer.tool_store.custom_slots}
    assert {buttons[sid] for sid in custom_ids} == set(inner_buttons)


def test_core_menu_offers_no_mutations(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    shelf = viewer.tool_shelf
    for kind in (ToolType.PAN, ToolType.SELECTION):
        slot = next(s for s in viewer.tool_store.slots if s.kind == kind)
        labels = [a.text() for a in shelf.build_slot_menu(slot).actions()]
        assert labels == []
    pen = next(s for s in viewer.tool_store.slots if s.kind == ToolType.PEN)
    labels = [a.text() for a in shelf.build_slot_menu(pen).actions()]
    assert any("Edit" in label for label in labels)
    assert "Duplicate" in labels and "Remove" in labels


def test_add_grid_excludes_core(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import ADD_GRID_META, AddToolGrid, _AddCell

    viewer = _make_viewer(qtbot)
    assert ToolType.PAN not in {k for k, _ in ADD_GRID_META}
    assert ToolType.SELECTION not in {k for k, _ in ADD_GRID_META}
    assert ToolType.LASSO in {k for k, _ in ADD_GRID_META}
    assert not hasattr(ToolType, "TEXT")
    grid = AddToolGrid(dark_tokens(), viewer)
    qtbot.addWidget(grid)
    assert len(grid.findChildren(_AddCell)) == len(ADDABLE_KINDS) == 10


# -- live inspector containment --------------------------------------------------

def test_popup_stays_inside_window_all_docks(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    for dock in ("left", "right", "top", "bottom"):
        viewer.tool_shelf.set_dock(dock)
        viewer._on_tool_edit_requested(pen)
        popup = viewer._tool_popup
        assert popup is not None and popup.isVisible()
        geo, host = popup.geometry(), viewer.rect()
        assert geo.left() >= 0 and geo.top() >= 0
        assert geo.right() <= host.width() and geo.bottom() <= host.height()
        assert popup._nub_side in ("top", "bottom", "left", "right")
        viewer._close_tool_popup()


def test_esc_closes_keeping_changes(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    viewer.tool_shelf.refresh()
    _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
    viewer._tool_popup._content._color_section._emit("#00ff00")
    QTest.keyClick(viewer._tool_popup, Qt.Key.Key_Escape)
    qtbot.wait(350)
    assert viewer._tool_popup is None
    assert viewer.tool_store.find(pen.id.value).color.lower() == "#00ff00"


def test_outside_click_closes_keeping_changes(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    viewer.tool_shelf.refresh()
    _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
    popup = viewer._tool_popup
    assert popup is not None
    popup._content._color_section._emit("#00ff00")
    # Click a viewer corner far from the popup (never consumes the click).
    corner = QPoint(30, viewer.height() - 30)
    if popup.geometry().contains(corner):
        corner = QPoint(viewer.width() - 30, 30)
    QTest.mouseClick(viewer, Qt.MouseButton.LeftButton, pos=corner)
    qtbot.wait(350)
    assert viewer._tool_popup is None
    assert viewer.tool_store.find(pen.id.value).color.lower() == "#00ff00"


def test_live_change_persists_debounced(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    viewer.tool_shelf.refresh()
    calls = []
    orig = viewer.tool_store.persist

    def _spy(settings=None):
        calls.append(1)
        return orig(settings)

    viewer.tool_store.persist = _spy  # type: ignore[method-assign]
    try:
        _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
        viewer._tool_popup._content._color_section._emit("#00ff00")
        qtbot.wait(100)
        assert calls == []  # debounced: no immediate disk write
        qtbot.wait(400)
        assert len(calls) >= 1
    finally:
        viewer.tool_store.persist = orig  # type: ignore[method-assign]
    viewer._close_tool_popup()


# -- inline color ------------------------------------------------------------------

def test_inline_color_ring_hex_and_preset(qtbot, cleared_settings):
    from PySide6.QtTest import QTest
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    # Advanced picker is collapsed by default; presets are the normal UI.
    assert section._picker_wrap.isHidden() is True
    # Preset swatch click applies (QML grid: click the cell center).
    idx = next(i for i, b in enumerate(section._swatches)
               if str(b.property("swatch_color")).lower() == "#2b6cff")
    QTest.mouseClick(section._grid_view, Qt.MouseButton.LeftButton,
                     pos=section._swatch_center(idx))
    assert form._slot.color.lower() == "#2b6cff"
    # Square picker opens only on request (no dialog).
    section._on_palette_add_tile()
    assert section._picker_wrap.isHidden() is False
    qtbot.wait(900)  # QML card Ready before pressing
    before = form._slot.color.lower()
    # Press two distinct points of the SV square (200-space): the live
    # color must track. View coords: square is 200 wide, centered.
    view = section._ring._view
    x0 = (view.width() - 200) / 2
    QTest.mousePress(section._ring, Qt.MouseButton.LeftButton,
                     pos=QPoint(int(x0 + 150), int(12 + 50)))
    QTest.mouseRelease(section._ring, Qt.MouseButton.LeftButton,
                       pos=QPoint(int(x0 + 150), int(12 + 50)))
    QTest.mousePress(section._ring, Qt.MouseButton.LeftButton,
                     pos=QPoint(int(x0 + 30), int(12 + 160)))
    QTest.mouseRelease(section._ring, Qt.MouseButton.LeftButton,
                       pos=QPoint(int(x0 + 30), int(12 + 160)))
    assert form._slot.color.lower() != before
    # Hex entry applies valid input, ignores garbage.
    section._hex.setText("#123456")
    section._hex.editingFinished.emit()
    assert form._slot.color.lower() == "#123456"
    section._hex.setText("not-a-color")
    section._hex.editingFinished.emit()
    assert form._slot.color.lower() == "#123456"


# -- visual-first property controls ---------------------------------------------------

def test_size_visual_and_numeric_parity(qtbot, cleared_settings):
    """Locked M5-A1: direct slider + preview + readout; no icon tiles."""
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", width_mm=0.5)
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._size_section
    # No generic icon preset tiles for Size.
    assert section._options == []
    assert section._slider is not None
    assert section._preview_dot is not None
    # Slider writes the same width_mm field (ends of range).
    section._slider.setValue(0)
    assert abs(form._slot.width_mm - section._min_w) < 1e-9
    section._slider.setValue(100)
    assert abs(form._slot.width_mm - section._max_w) < 1e-9
    # Numeric editor (Custom) writes the identical model value.
    form._size_section._custom_btn.click()
    qtbot.wait(900)
    form._size_section._bridge.commit("0.8")
    assert abs(form._slot.width_mm - 0.8) < 1e-9
    assert "mm" in section._readout.text()


def test_opacity_visual_bars(qtbot, cleared_settings):
    """Locked M5-A1: direct opacity slider + bar preview; no icon tiles."""
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", opacity=1.0)
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._opacity_section
    assert section._options == []
    assert section._slider is not None
    assert section._bar is not None
    section._slider.setValue(100)
    assert abs(form._slot.opacity - 1.0) < 1e-9
    form._opacity_section._custom_btn.click()
    qtbot.wait(900)
    section._bridge.commit("40")
    assert abs(form._slot.opacity - 0.4) < 1e-9
    assert "%" in section._readout.text()


def test_blend_mode_gone_everywhere(qtbot, cleared_settings):
    """Blend Mode is not part of the design: no field, UI, or persistence."""
    from app.toolshelf.tool_popup import PropertiesForm

    assert not hasattr(ToolInstance(kind=ToolType.PEN, name="x"), "blend_mode")
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    assert getattr(form, "_blend_section", None) is None
    assert "blend_mode" not in editable_properties(ToolType.PEN)
    assert "blend_mode" not in editable_properties(ToolType.HIGHLIGHTER)


# -- real-mouse reorder / trash / docking -----------------------------------------------

def _hold_press(widget, qtbot, hold_ms: int = 450):
    from PySide6.QtTest import QTest

    QTest.mousePress(widget, Qt.MouseButton.LeftButton,
                     pos=widget.rect().center())
    qtbot.wait(hold_ms)


def _drag_to(widget, qtbot, target_global, steps: int = 6):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    # Step in GLOBAL coordinates (re-mapped per step): the dragged
    # widget may itself move under the cursor (live-follow toolbar).
    start_global = widget.mapToGlobal(widget.rect().center())
    for i in range(1, steps + 1):
        gx = start_global.x() + (target_global.x() - start_global.x()) * i / steps
        gy = start_global.y() + (target_global.y() - start_global.y()) * i / steps
        QTest.mouseMove(widget, pos=widget.mapFromGlobal(QPoint(int(gx), int(gy))))
        qtbot.wait(20)


def test_qtest_reorder_custom_slot(qtbot, cleared_settings, tmp_path):
    from PySide6.QtTest import QTest
    from app.toolshelf.store import ToolShelfStore

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    a = store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    b = store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    shelf = viewer.tool_shelf
    btn_b = shelf._buttons[b.id.value]
    btn_a = shelf._buttons[a.id.value]
    _hold_press(btn_b, qtbot)
    above_a = shelf.mapToGlobal(QPoint(
        btn_a.x() + btn_a.width() // 2, btn_a.y() - 12))
    _drag_to(btn_b, qtbot, above_a)
    assert viewer._reordering is True
    assert viewer._reorder_trash is not None
    assert viewer._reorder_trash.isVisible() is True
    assert viewer._reorder_indicator is not None
    QTest.mouseRelease(btn_b, Qt.MouseButton.LeftButton,
                       pos=btn_b.mapFromGlobal(above_a))
    qtbot.wait(100)
    names = [s.name for s in store.slots]
    assert names == ["Pan", "Select", "Pen B", "Pen A"]
    assert viewer._reorder_trash is None and viewer._reorder_ghost is None
    # Order persisted: round-trip through an ini keeps the swapped customs.
    from PySide6.QtCore import QSettings

    settings = QSettings(str(tmp_path / "reorder.ini"),
                         QSettings.Format.IniFormat)
    store.persist(settings)
    settings.sync()
    revived = ToolShelfStore.restore(settings)
    assert [s.name for s in revived.slots] == names


def test_qtest_trash_deletes_custom(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    a = store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn = viewer.tool_shelf._buttons[a.id.value]
    _hold_press(btn, qtbot)
    first = btn.rect().center() + QPoint(0, 30)
    from PySide6.QtTest import QTest as _QTest

    _QTest.mouseMove(btn, pos=first)
    qtbot.wait(50)
    assert viewer._reorder_trash is not None
    trash_center = viewer.mapToGlobal(viewer._reorder_trash.geometry().center())
    _drag_to(btn, qtbot, trash_center)
    assert viewer._reorder_over_trash is True
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton,
                       pos=btn.mapFromGlobal(trash_center))
    qtbot.wait(100)
    assert store.find(a.id.value) is None
    assert [s.kind for s in store.slots[:2]] == [ToolType.PAN, ToolType.SELECTION]


def test_qtest_reorder_esc_cancels(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    a = store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    b = store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    before = [s.id.value for s in store.slots]
    btn = viewer.tool_shelf._buttons[b.id.value]
    _hold_press(btn, qtbot)
    QTest.mouseMove(btn, pos=btn.rect().center() + QPoint(0, 80))
    qtbot.wait(50)
    assert viewer._reordering is True
    QTest.keyClick(btn, Qt.Key.Key_Escape)
    qtbot.wait(100)
    assert viewer._reordering is False
    assert viewer._reorder_trash is None
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert [s.id.value for s in store.slots] == before


def test_qtest_core_hold_does_nothing(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    before = [s.id.value for s in store.slots]
    for kind in (ToolType.PAN, ToolType.SELECTION):
        slot = next(s for s in store.slots if s.kind == kind)
        button = viewer.tool_shelf._buttons[slot.id.value]
        _hold_press(button, qtbot)
        QTest.mouseMove(button, pos=button.rect().center() + QPoint(0, 80))
        qtbot.wait(50)
        assert viewer._reordering is False
        assert viewer._reorder_trash is None
        QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
        qtbot.wait(50)
    assert [s.id.value for s in store.slots] == before


# -- mouse-grab audit ------------------------------------------------------------

def _grabber():
    from PySide6.QtWidgets import QWidget

    return QWidget.mouseGrabber()


def test_no_stale_grab_after_handle_release(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    _hold_press(handle, qtbot)
    assert _grabber() is handle  # hold engages the drag grab
    QTest.mouseMove(handle, pos=handle.rect().center() + QPoint(60, 0))
    qtbot.wait(50)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert _grabber() is None


def test_no_stale_grab_after_handle_esc(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    _hold_press(handle, qtbot)
    assert _grabber() is handle
    QTest.keyClick(handle, Qt.Key.Key_Escape)
    qtbot.wait(50)
    assert _grabber() is None
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert _grabber() is None


def test_no_stale_grab_after_reorder_release(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn = viewer.tool_shelf._buttons[
        next(s.id.value for s in store.slots if s.name == "Pen B")]
    _hold_press(btn, qtbot)
    _drag_to(btn, qtbot, btn.mapToGlobal(btn.rect().center() + QPoint(0, 60)))
    assert viewer._reordering is True
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    qtbot.wait(100)
    assert viewer._reordering is False
    assert _grabber() is None


def test_no_stale_grab_after_reorder_esc(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    store.add_slot(ToolType.PEN, name="Pen B", color="#222222")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn = viewer.tool_shelf._buttons[
        next(s.id.value for s in store.slots if s.name == "Pen B")]
    _hold_press(btn, qtbot)
    _drag_to(btn, qtbot, btn.mapToGlobal(btn.rect().center() + QPoint(0, 60)))
    assert viewer._reordering is True
    QTest.keyClick(btn, Qt.Key.Key_Escape)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    qtbot.wait(100)
    assert viewer._reordering is False
    assert _grabber() is None


def test_no_stale_grab_after_refresh_mid_hold(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    _hold_press(handle, qtbot)
    assert _grabber() is handle
    # Rebuild mid-gesture (dock/add/remove path): detaching the
    # grabbing widget must release the grab (Qt hide-release).
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    assert _grabber() is None
    # Shelf stays functional after the rebuild.
    assert viewer.tool_shelf._handle is not None
    QTest.mouseRelease(viewer.tool_shelf._handle, Qt.MouseButton.LeftButton)
    qtbot.wait(50)


def test_qtest_dock_all_sides_real_mouse(qtbot, cleared_settings, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtTest import QTest

    from app.toolshelf.store import ToolShelfStore

    viewer = _make_viewer(qtbot)
    qtbot.wait(100)
    w, h = viewer.width(), viewer.height()
    targets = {"top": (w // 2, 30), "right": (w - 30, h // 2),
               "bottom": (w // 2, h - 30), "left": (30, h // 2)}
    for dock, (x, y) in targets.items():
        handle = viewer.tool_shelf._handle
        _hold_press(handle, qtbot)
        assert viewer._docking is True
        assert len(viewer._dock_targets) == 4
        target_global = viewer.mapToGlobal(QPoint(x, y))
        _drag_to(handle, qtbot, target_global)
        assert viewer._dock_preview == dock
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton,
                           pos=handle.mapFromGlobal(target_global))
        qtbot.wait(100)
        assert viewer.tool_store.dock == dock
        # Dock persisted: ini round-trip keeps the committed side.
        settings = QSettings(str(tmp_path / f"dock-{dock}.ini"),
                             QSettings.Format.IniFormat)
        viewer.tool_store.persist(settings)
        settings.sync()
        assert ToolShelfStore.restore(settings).dock == dock
        assert viewer._dock_targets == []
    assert viewer.tool_shelf._orientation() == "vertical"


def test_qtest_dock_esc_cancels(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    qtbot.wait(100)
    assert viewer.tool_store.dock == "left"
    handle = viewer.tool_shelf._handle
    _hold_press(handle, qtbot)
    assert viewer._docking is True
    _drag_to(handle, qtbot,
             viewer.mapToGlobal(QPoint(viewer.width() // 2, 30)))
    assert viewer._dock_preview == "top"
    QTest.keyClick(handle, Qt.Key.Key_Escape)
    qtbot.wait(100)
    assert viewer._docking is False
    assert viewer._dock_targets == []
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert viewer.tool_store.dock == "left"
    assert viewer.tool_shelf._orientation() == "vertical"


# -- M5-A1 visual-refinement acceptance ----------------------------------------------

def test_add_library_no_text_polygon_exists(qtbot, cleared_settings):
    from app.icons import icon_names
    from app.toolshelf.tool_popup import ADD_GRID_META, AddToolGrid, _AddCell

    kinds = {k for k, _ in ADD_GRID_META}
    assert ToolType.POLYGON in kinds
    assert not hasattr(ToolType, "TEXT")
    assert all(k in ADDABLE_KINDS for k in kinds)
    assert "polygon" in icon_names()
    viewer = _make_viewer(qtbot)
    grid = AddToolGrid(dark_tokens(), viewer)
    qtbot.addWidget(grid)
    cells = grid.findChildren(_AddCell)
    assert len(cells) == len(ADDABLE_KINDS) == 10
    for cell in cells:
        assert cell._kind != getattr(ToolType, "TEXT", None)
    poly = next(c for c in cells if c._kind == ToolType.POLYGON)
    assert poly._label() == "Polygon"


def test_numeric_editors_hidden_until_custom(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", width_mm=0.5)
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    assert form._size_section._num_wrap.isHidden() is True
    assert form._opacity_section._num_wrap.isHidden() is True
    # Visual presets are primary; Custom reveals the numeric editor.
    form._size_section._custom_btn.click()
    assert form._size_section._num_wrap.isHidden() is False
    qtbot.wait(900)
    form._size_section._bridge.commit("0.8")
    assert abs(form._slot.width_mm - 0.8) < 1e-9


def test_custom_colors_persist_inside_popup(qtbot, cleared_settings):
    from PySide6.QtCore import QSettings

    from app.toolshelf.tool_popup import PropertiesForm, _ColorSection

    QSettings().remove(_ColorSection.CUSTOM_KEY)
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#123456")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    section._emit("#abcdef")
    section._on_add_custom_collapse()
    assert "#abcdef" in section.custom_colors()
    # A fresh section reloads the saved swatch (survives popup close).
    form2 = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form2)
    assert "#abcdef" in form2._color_section.custom_colors()
    QSettings().remove(_ColorSection.CUSTOM_KEY)


def test_no_external_color_dialog():
    import app.toolshelf.tool_popup as popup_mod
    import inspect

    source = inspect.getsource(popup_mod)
    assert "QColorDialog" not in source


def test_popup_is_translucent_child_overlay(qtbot, cleared_settings):
    from PySide6.QtCore import Qt

    from app.toolshelf.tool_popup import ToolPopup

    viewer = _make_viewer(qtbot)
    popup = ToolPopup(dark_tokens(), viewer)
    qtbot.addWidget(popup)
    assert bool(popup.testAttribute(
        Qt.WidgetAttribute.WA_TranslucentBackground)) is True
    assert popup.graphicsEffect() is None
    # Corner pixels stay transparent (no black square around the radius).
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer._on_tool_edit_requested(pen)
    popup = viewer._tool_popup
    assert popup.parent() is viewer
    grab = popup.grab()
    assert grab.toImage().pixelColor(1, 1).alpha() < 40
    viewer._close_tool_popup()


def test_popup_labels_have_transparent_backgrounds(qtbot, cleared_settings):
    from PySide6.QtWidgets import QLabel

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer._on_tool_edit_requested(pen)
    for label in viewer._tool_popup.findChildren(QLabel):
        style = label.styleSheet() or ""
        assert "background-color: #" not in style.replace(" ", "")
        assert "background:#" not in style.replace(" ", "")
    viewer._close_tool_popup()


def test_dock_zones_are_regions_without_text(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    viewer._on_reposition_started()
    try:
        assert len(viewer._dock_targets) == 4
        rects = viewer._dock_zone_rects()
        assert set(rects) == {"top", "left", "right", "bottom"}
        for zone in viewer._dock_targets:
            assert zone.text() == "" if hasattr(zone, "text") else True
            assert zone.property("dock_zone") is True
            dock = zone.property("dock_target")
            assert dock in rects
        # Top zone hugs the top edge, left zone hugs the left edge.
        assert rects["top"].y() <= 12
        assert rects["left"].x() <= 12
        assert rects["top"].width() > 120 and rects["left"].height() > 120
        viewer._highlight_dock_target("top")
        active = next(z for z in viewer._dock_targets
                      if z.property("dock_target") == "top")
        from PySide6.QtCore import Qt as _Qt

        assert bool(active.testAttribute(
            _Qt.WidgetAttribute.WA_TranslucentBackground)) is True
        # Translucent hint: no opaque fill in any stylesheet.
        assert "surface_raised" not in (active.styleSheet() or "")
    finally:
        viewer._on_reposition_cancelled()


def test_trash_is_small_icon_beside_toolbar(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    a = store.add_slot(ToolType.PEN, name="Pen A", color="#111111")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn = viewer.tool_shelf._buttons[a.id.value]
    _hold_press(btn, qtbot)
    from PySide6.QtTest import QTest

    QTest.mouseMove(btn, pos=btn.rect().center() + QPoint(0, 40))
    qtbot.wait(50)
    trash = viewer._reorder_trash
    assert trash is not None and trash.isVisible()
    assert trash.width() <= 48 and trash.height() <= 48
    assert (trash.text() or "") == ""
    shelf_geo = viewer.tool_shelf.geometry()
    gap = trash.geometry().x() - (shelf_geo.x() + shelf_geo.width())
    near_right = 0 <= gap <= 32
    gap_left = shelf_geo.x() - (trash.geometry().x() + trash.width())
    near_left = 0 <= gap_left <= 32
    assert near_right or near_left
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    qtbot.wait(50)


def test_shape_icons_use_configured_color(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    line = store.add_slot(ToolType.LINE, name="Line #1", color="#e5484d")
    poly = store.add_slot(ToolType.POLYGON, name="Poly #1", color="#2b6cff")
    viewer.tool_shelf.refresh()
    from app.toolshelf.slot_button import _SHAPE_ICONS

    for slot in (line, poly):
        button = viewer.tool_shelf._buttons[slot.id.value]
        assert slot.kind in _SHAPE_ICONS
        grab = button.grab()
        assert not grab.isNull()
        # At least one pixel carries significant red (line) / blue (poly).
        image = grab.toImage()
        hits = 0
        for x in range(0, image.width(), 2):
            for y in range(0, image.height(), 2):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() < 60:
                    continue
                if slot.kind == ToolType.LINE and pixel.red() > 150:
                    hits += 1
                if slot.kind == ToolType.POLYGON and pixel.blue() > 150:
                    hits += 1
        assert hits > 0


def test_titlebar_controls_have_no_focus_border(qtbot):
    from app.titlebar import TitleBar

    bar = TitleBar(dark_tokens())
    qtbot.addWidget(bar)
    for btn in (bar._min_btn, bar._max_btn, bar._close_btn):
        style = btn.styleSheet() or ""
        assert "input_focus" not in style
        assert bar._tokens.input_focus not in style


def test_toolbar_is_thin_with_circular_halos():
    import inspect

    from app.toolshelf.slot_button import CELL
    from app.toolshelf import shelf as shelf_mod
    from app.toolshelf import slot_button as slot_mod

    assert CELL == 40
    assert shelf_mod._MARGIN <= 6 and shelf_mod._SPACING <= 4
    assert shelf_mod.HOLD_MS <= 300
    slot_source = inspect.getsource(slot_mod.SlotButton.paintEvent)
    assert "drawEllipse" in slot_source
    assert "drawRoundedRect(rect, 10, 10)" not in slot_source


# -- M5-A1 final-polish acceptance ----------------------------------------------

def test_tool_switch_closes_old_popup(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    hl = viewer.tool_store.add_slot(ToolType.HIGHLIGHTER, name="HL #1")
    viewer.tool_shelf.refresh()
    _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
    assert viewer._tool_popup is not None
    viewer.tool_shelf.select_slot(hl.id.value)
    qtbot.wait(50)
    assert viewer._tool_popup is None


def test_popup_never_overlaps_launcher_all_docks(qtbot, cleared_settings):
    from PySide6.QtCore import QRect as _QRect

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    for dock in ("left", "right", "top", "bottom"):
        viewer.tool_shelf.set_dock(dock)
        viewer._position_shelf()
        button = viewer.tool_shelf._buttons[pen.id.value]
        viewer._on_tool_edit_requested(pen)
        qtbot.wait(250)  # let the slide/fade settle on final geometry
        popup = viewer._tool_popup
        assert popup is not None and popup.isVisible()
        geo = popup.geometry()
        host = viewer.rect()
        assert geo.left() >= 0 and geo.top() >= 0
        assert geo.right() <= host.width()
        assert geo.bottom() <= host.height()
        launcher = _QRect(viewer.mapFromGlobal(
            button.mapToGlobal(button.rect().topLeft())),
            button.rect().size())
        assert not geo.intersects(launcher)
        viewer._close_tool_popup()


def test_custom_picker_collapses_after_add(qtbot, cleared_settings):
    from PySide6.QtCore import QSettings

    from app.toolshelf.tool_popup import PropertiesForm, _ColorSection

    QSettings().remove(_ColorSection.CUSTOM_KEY)
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#123456")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    section._on_palette_add_tile()
    assert section._picker_wrap.isHidden() is False
    section._ring.set_hsv(200, 0.7, 0.8)
    section._emit("#41b6c8")
    section._on_palette_add_tile()  # open tile commits + collapses
    assert "#41b6c8" in section.custom_colors()
    assert section._picker_wrap.isHidden() is True
    assert form._slot.color.lower() == "#41b6c8"
    QSettings().remove(_ColorSection.CUSTOM_KEY)


def test_new_tool_defaults_are_visible_colors():
    from PySide6.QtGui import QColor

    from app.toolshelf.store import default_tool_config

    for kind in ADDABLE_KINDS:
        slot = default_tool_config(kind)
        color = QColor(slot.color)
        assert color.isValid()
        assert color.alpha() == 255
        # Not transparent/white-invisible: some saturation or darkness.
        h, s, v, _a = color.getHsv()
        assert s > 0 or v < 250


def test_pen_highlighter_shelf_icons_show_color(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    pen = store.add_slot(ToolType.PEN, name="Pen #1", color="#e5484d")
    hl = store.add_slot(ToolType.HIGHLIGHTER, name="HL #1", color="#2b6cff")
    viewer.tool_shelf.refresh()
    for slot, channel in ((pen, "red"), (hl, "blue")):
        button = viewer.tool_shelf._buttons[slot.id.value]
        image = button.grab().toImage()
        hits = 0
        for x in range(0, image.width(), 2):
            for y in range(0, image.height(), 2):
                pixel = image.pixelColor(x, y)
                if pixel.alpha() < 60:
                    continue
                if channel == "red" and pixel.red() > 150:
                    hits += 1
                if channel == "blue" and pixel.blue() > 150:
                    hits += 1
        assert hits > 0


def test_trash_has_no_black_square(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    a = viewer.tool_store.add_slot(ToolType.PEN, name="Pen A")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    btn = viewer.tool_shelf._buttons[a.id.value]
    _hold_press(btn, qtbot)
    QTest.mouseMove(btn, pos=btn.rect().center() + QPoint(0, 40))
    qtbot.wait(50)
    trash = viewer._reorder_trash
    assert trash is not None and trash.isVisible()
    assert (trash.text() or "") == ""
    corner = trash.grab().toImage().pixelColor(1, 1)
    assert corner.alpha() < 60
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
    qtbot.wait(50)


def test_undo_redo_hud_shell(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    hud = viewer.undo_hud
    assert hud is not None and hud.isVisible()
    assert hud._undo_btn.isEnabled() is False
    assert hud._redo_btn.isEnabled() is False
    assert hud.can_undo() is False and hud.can_redo() is False
    # Bottom-left placement with shelf margin language.
    assert hud.geometry().x() <= 12
    assert hud.geometry().bottom() >= viewer.height() - 12 - hud.height()
    # UI-only: hover/clicks submit no renders.
    calls = []
    viewer._request_update = lambda: calls.append(1)  # type: ignore[method-assign]
    hud._undo_btn.setEnabled(True)
    from PySide6.QtTest import QTest

    QTest.mouseClick(hud._undo_btn, Qt.MouseButton.LeftButton)
    QTest.mouseClick(hud._redo_btn, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    assert calls == []
    # Theme switches apply without errors.
    from app.theme.tokens import light_tokens

    viewer.update_chrome_tokens(light_tokens())
    viewer.update_chrome_tokens(dark_tokens())


def test_toolbar_ui_causes_no_render_submissions(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot)
    store = viewer.tool_store
    pen = store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000")
    viewer.tool_shelf.refresh()
    qtbot.wait(50)
    calls = []
    viewer._request_update = lambda: calls.append(1)  # type: ignore[method-assign]
    # Live inspector edit + reorder drag + dock drag: all UI-only.
    _dblclick(viewer.tool_shelf._buttons[pen.id.value], qtbot)
    viewer._tool_popup._content._color_section._emit("#00ff00")
    viewer._close_tool_popup()
    button = viewer.tool_shelf._buttons[pen.id.value]
    _hold_press(button, qtbot, hold_ms=400)
    QTest.mouseMove(button, pos=button.rect().center() + QPoint(0, 40))
    QTest.mouseRelease(button, Qt.MouseButton.LeftButton)
    qtbot.wait(50)
    handle = viewer.tool_shelf._handle
    _hold_press(handle, qtbot)
    viewer._on_reposition_finished()
    assert calls == []


# -- M5-A1 second pass: unified palette, contrast-safe icons, Inter -----


def test_unified_palette_has_no_split(qtbot, cleared_settings):
    """ONE palette: no Default/My Colors split; store is the only source."""
    from PySide6.QtWidgets import QLabel

    from app.toolshelf.tool_popup import PropertiesForm
    from app.toolshelf.store import get_color_presets

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#ff0000")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    texts = [lbl.text().lower() for lbl in section.findChildren(QLabel)]
    assert not any("my color" in t or "default color" in t for t in texts)
    presets = get_color_presets()
    swatch_colors = [str(b.property("swatch_color")).lower()
                     for b in section._swatches]
    assert sorted(swatch_colors) == sorted(p.lower() for p in presets)
    # Exactly one circular + tile: the QML cell trailing the presets
    # opens the picker (no second palette).
    assert section._preset_bridge is not None
    assert len(section._preset_bridge.colors) == len(presets)


def test_palette_add_tile_opens_picker(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    assert section._picker_wrap.isHidden() is True
    # Click the trailing QML add tile (index == preset count).
    from app.toolshelf.store import get_color_presets

    add_at = section._swatch_center(len(get_color_presets()))
    QTest.mouseClick(section._grid_view, Qt.MouseButton.LeftButton,
                     pos=add_at)
    assert section._picker_wrap.isHidden() is False


def test_picker_commit_adds_selects_collapses(qtbot, cleared_settings):
    from app.toolshelf.store import get_color_presets
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#123456")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    section = form._color_section
    assert "#41b6c8" not in get_color_presets()
    section._on_palette_add_tile()
    section._emit("#41b6c8")
    section._on_palette_add_tile()
    assert "#41b6c8" in get_color_presets()
    assert "#41b6c8" in section.custom_colors()
    assert form._slot.color.lower() == "#41b6c8"
    assert section._picker_wrap.isHidden() is True


def test_icon_contrast_treatment():
    from app.icons import contrast_ratio, ensure_contrast_fg, tool_pixmap

    # Near-background fills shift (bounded, hue-preserving); visible
    # colors pass through untouched.
    assert ensure_contrast_fg("#ffd60a", "#181C23").lower() == "#ffd60a"
    shifted = ensure_contrast_fg("#000000", "#181C23")
    assert shifted.lower() != "#000000"
    assert contrast_ratio(shifted, "#181C23") >= 2.2
    shifted_light = ensure_contrast_fg("#ffffff", "#FFFFFF")
    assert shifted_light.lower() != "#ffffff"
    assert contrast_ratio(shifted_light, "#FFFFFF") >= 2.2
    for name in ("selection", "pen-nib", "marker", "highlighter"):
        assert not tool_pixmap(name, "#000000", "#181C23", 20).isNull()
        assert not tool_pixmap(name, "#ffffff", "#FFFFFF", 20).isNull()


def test_line_samples_contrast_safe(qtbot, cleared_settings):
    from app.icons import contrast_ratio
    from app.theme.tokens import light_tokens
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    for tokens, color in ((dark_tokens(), "#111111"),
                          (light_tokens(), "#ffffff")):
        line = viewer.tool_store.add_slot(ToolType.LINE, name="Line",
                                          color=color)
        form = PropertiesForm(line, tokens, viewer)
        qtbot.addWidget(form)
        sample = form._line_section._base_color().name()
        assert contrast_ratio(sample, tokens.surface_raised) >= 2.2


def test_inter_bundled_and_first():
    import pathlib

    from PySide6.QtGui import QFontDatabase

    from app.fonts import INTER_FAMILY, bundled_font_dir, load_bundled_fonts

    font_dir = bundled_font_dir()
    assert font_dir is not None
    for name in ("Inter-Regular.ttf", "Inter-Medium.ttf",
                 "Inter-SemiBold.ttf"):
        assert (pathlib.Path(str(font_dir)) / name).is_file()
    assert load_bundled_fonts() == INTER_FAMILY
    assert INTER_FAMILY in QFontDatabase.families()
    from app.theme.multilingual import LATIN_STACK, font_for
    from app.theme.tokens import dark_tokens

    assert LATIN_STACK[0] == "Inter"
    assert dark_tokens().font_family.split(",")[0].strip() == "Inter"
    assert font_for("Add Tool", "body", dark_tokens()).family() == "Inter"
    # RTL fallback untouched.
    assert font_for("فصل اول", "body", dark_tokens()).family() == "Vazirmatn"


def test_dock_zones_match_toolbar_footprint(qtbot, cleared_settings):
    """Left/right zones are vertical strips, top/bottom horizontal —
    regardless of the shelf's current orientation (single source)."""
    viewer = _make_viewer(qtbot)
    for dock in ("top", "left"):
        viewer.tool_shelf.set_dock(dock)
        rects = viewer._dock_zone_rects()
        assert set(rects) == {"top", "bottom", "left", "right"}
        for side in ("left", "right"):
            assert rects[side].width() < rects[side].height(), (dock, side)
        for side in ("top", "bottom"):
            assert rects[side].width() > rects[side].height(), (dock, side)
        # No center highlight: every zone hugs its own edge (the right /
        # bottom strips stop at the scrollbar inset, never mid-canvas).
        assert rects["left"].left() == 0
        assert rects["right"].left() > viewer.width() // 2
        assert rects["top"].top() == 0
        assert rects["bottom"].top() > viewer.height() // 2


def test_hover_lift_helpers():
    from PySide6.QtWidgets import QGraphicsDropShadowEffect, QPushButton

    button = QPushButton("probe")
    fx.install_hover_lift(button)
    fx.install_hover_lift(button)  # idempotent across token refreshes
    fx.hover_lift(button)
    assert isinstance(button.graphicsEffect(), QGraphicsDropShadowEffect)
    fx.hover_sink(button)
    fx.reduced_motion_enabled()  # contract probe, never raises


# -- M5-A1 correction pass: final glyphs, centering, no text buttons ------


def test_no_add_color_text_buttons(qtbot, cleared_settings):
    """The add affordance is ONLY the circular + tile (no text button)."""
    from PySide6.QtWidgets import QPushButton

    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#ff0000")
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    banned = ("add color", "add custom color", "add to my colors",
              "+ add color")
    for button in form.findChildren(QPushButton):
        text = (button.text() or "").strip().lower()
        assert not any(b == text for b in banned), text
    # Source guard: no QPushButton is ever constructed with such text.
    import inspect

    import app.toolshelf.tool_popup as popup_mod

    source = inspect.getsource(popup_mod)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from ", "import ", "#")):
            continue
        if "QPushButton(" in line and "Add color" in line:
            raise AssertionError(line)
        if ".setText(" in line and "Add color" in line:
            raise AssertionError(line)


def test_selection_highlighter_final_glyphs():
    """Final glyphs: marquee+arrow selection, chisel-tip highlighter.

    M5-A2.1 revision (supersedes the M5-A1.1 hand-pointer art): Selection
    is a dashed rectangular marquee with a cursor-arrow pointer whose
    tip crosses the marquee corner; the highlighter is a broad
    chisel-tip marker leaning lower-left -> upper-right.
    """
    from app import icons as icon_mod
    from app.icons import tool_pixmap

    # Selection: dashed marquee rect + cursor-arrow path with a light
    # fill — not the old filled block, cursor tail, or hand silhouette.
    assert "stroke-dasharray" in icon_mod._PATHS["selection"]
    assert "M15 12.5" in icon_mod._PATHS["selection"]
    assert "M10.1 12.1" not in icon_mod._PATHS["selection"]
    assert "width='11' height='11'" not in icon_mod._PATHS["selection"]
    assert "L15.2 18.1" not in icon_mod._PATHS["selection"]
    # Highlighter: rotated marker group with translucent cap, broad
    # barrel, flat chisel tip and light section separators — not the old
    # evenodd grip-band groove, and distinct from the pen nib.
    assert "rotate(45)" in icon_mod._PATHS["highlighter"]
    assert "M-3.5 5.2H3.5L2.25 9H-2.25Z" in icon_mod._PATHS["highlighter"]
    assert "fill-rule='evenodd'" not in icon_mod._PATHS["highlighter"]
    assert "M8.2 12.0" not in icon_mod._PATHS["highlighter"]
    assert icon_mod._PATHS["highlighter"] != icon_mod._PATHS["pen-nib"]
    # No leftover exploration variants.
    for name in ("selection-v1", "selection-v2", "highlighter-v1",
                 "highlighter-v2"):
        assert name not in icon_mod._PATHS
    for name in ("pan", "selection", "lasso", "pen-nib", "marker",
                 "highlighter", "eraser", "line", "arrow", "rectangle",
                 "ellipse", "polygon", "note"):
        for bg in ("#181C23", "#F4F5F7"):
            assert not tool_pixmap(name, "#e8b93c", bg, 20).isNull(), name
    # Silhouette inequality at toolbar size: pen vs highlighter and the
    # nav trio are pairwise distinct.
    from PySide6.QtGui import QImage

    def _bytes(name):
        img = tool_pixmap(name, "#e8b93c", "#181C23", 20).toImage().convertToFormat(
            QImage.Format.Format_ARGB32)
        return bytes(img.constBits())

    assert _bytes("pen-nib") != _bytes("highlighter")
    assert _bytes("pan") != _bytes("selection")
    assert _bytes("selection") != _bytes("lasso")
    assert _bytes("pan") != _bytes("lasso")


def test_glyph_optical_centering():
    """Ink-center offsets come from painted bounds, per glyph."""
    from app.icons import ink_center_offset, tool_pixmap

    for name in ("pan", "selection", "lasso", "pen-nib", "marker",
                 "highlighter", "eraser", "line", "arrow", "rectangle",
                 "ellipse", "polygon", "note", "plus"):
        pix = tool_pixmap(name, "#e8b93c", "#181C23", 20)
        dx, dy = ink_center_offset(pix)
        assert abs(dx) <= 8.0 and abs(dy) <= 8.0, (name, dx, dy)
        # Applying the offset centers the ink within half a pixel.
        img = pix.toImage()
        xs = [x for x in range(pix.width()) for y in range(pix.height())
              if img.pixelColor(x, y).alpha() > 8]
        ys = [y for x in range(pix.width()) for y in range(pix.height())
              if img.pixelColor(x, y).alpha() > 8]
        if not xs:
            continue
        ink_cx = (min(xs) + max(xs) + 1) / 2.0 + dx
        ink_cy = (min(ys) + max(ys) + 1) / 2.0 + dy
        assert abs(ink_cx - pix.width() / 2.0) <= 0.6, (name, ink_cx)
        assert abs(ink_cy - pix.height() / 2.0) <= 0.6, (name, ink_cy)
    # The diagonal pen nib genuinely needs correction (guards a
    # viewBox-center-only regression).
    dx, _dy = ink_center_offset(tool_pixmap("pen-nib", "#e8b93c",
                                            "#181C23", 20))
    assert abs(dx) >= 1.0, dx


def test_move_handle_has_zero_dots(qtbot, cleared_settings):
    """Move = app icon on a circular surface; no dots/ellipsis/grips."""
    from PySide6.QtWidgets import QLabel

    viewer = _make_viewer(qtbot)
    handle = viewer.tool_shelf._handle
    assert handle.text() == ""
    for label in handle.findChildren(QLabel):
        text = (label.text() or "")
        assert "·" not in text and "..." not in text, text
    assert "..." not in (handle.accessibleName() or "")
    # Rest-state paint: render onto a sentinel — outside the 30px
    # medallion nothing may paint (dots/grips would survive as
    # non-sentinel clusters). grab() leaves unpainted regions as
    # uninitialized memory, so render() onto a known fill is required.
    from PySide6.QtGui import QColor, QPixmap

    sentinel = QColor("#ff00ff")
    canvas = QPixmap(handle.size())
    canvas.fill(sentinel)
    # DrawChildren only: no window-background fill, so unpainted
    # regions keep the sentinel (grab() leaves them undefined).
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QRegion
    from PySide6.QtWidgets import QWidget

    handle.render(canvas, QPoint(), QRegion(),
                  QWidget.RenderFlag.DrawChildren)
    img = canvas.toImage()
    cx, cy = img.width() / 2.0, img.height() / 2.0
    strays = 0
    for x in range(img.width()):
        for y in range(img.height()):
            # Outside the medallion + its antialiased edge (r=15 + 3px).
            if (x - cx) ** 2 + (y - cy) ** 2 <= 18 ** 2:
                continue
            if img.pixelColor(x, y) != sentinel:
                strays += 1
    assert strays == 0, strays
    # Structural: the handle paints no text, no points, no rects —
    # medallion circle + app mark only.
    import inspect

    from app.toolshelf import shelf as shelf_mod

    source = inspect.getsource(shelf_mod._HandleCell.paintEvent)
    assert "drawText" not in source
    assert "drawPoints" not in source and "drawPoint" not in source
    assert "drawRect" not in source


def test_inter_registration_is_clean():
    """Bundled Inter registers without errors (no silent fallback)."""
    from PySide6.QtGui import QFontDatabase

    from app.fonts import (INTER_FAMILY, bundled_family_available,
                           load_bundled_fonts)

    assert load_bundled_fonts() == INTER_FAMILY
    assert getattr(load_bundled_fonts, "load_errors", None) == []
    assert INTER_FAMILY in QFontDatabase.families()
    assert bundled_family_available() is True
