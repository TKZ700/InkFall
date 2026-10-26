"""M5-A2 focused regression: presets, palette, icons, reorder, assist UI.

§1 unified stroke/fill presets · §2 circular palette in container ·
§4 single Line Style header · §3 property-reflecting icons ·
§7 reorder drop position + persist · §5/§6 assist UI + persistence.
(§8: no counter exists — recorded, no test.)
Drawing behavior for snap/connect/straight is engine-phase work;
these tests cover UI + configuration + persistence only.
"""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.theme.tokens import dark_tokens, light_tokens
from tests.qt_helpers import wait_stable, wait_until
from tools.models import ToolType

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


def _make_viewer(qtbot, customs=0):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    for i in range(customs):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"P{i}")
    viewer.tool_shelf.refresh()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    return viewer


def _popup(qtbot, viewer, slot, tokens=None):
    from app.toolshelf.tool_popup import PropertiesForm

    form = PropertiesForm(slot, dark_tokens() if tokens is None else tokens,
                          viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(120)
    return form


def _grab_distance(a, b) -> int:
    ia = a.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    ib = b.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    total = 0
    for y in range(ia.height()):
        for x in range(ia.width()):
            pa, pb = ia.pixelColor(x, y), ib.pixelColor(x, y)
            total += (abs(pa.red() - pb.red())
                      + abs(pa.green() - pb.green())
                      + abs(pa.blue() - pb.blue()))
    return total


# -- §1 unified presets ----------------------------------------------------

def test_fill_uses_full_shared_presets(qtbot, cleared_settings):
    from app.toolshelf.store import get_color_presets

    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, slot)
    n = len(get_color_presets())
    assert n > 6  # the old fill snapshot only showed six
    assert len(form._fill_section._palette._swatches) == n
    assert len(form._color_section._swatches) == n
    # Exactly one add tile: QML cell trailing the presets opens the
    # same add workflow (clicked coordinates below in later tests).
    strip = form._fill_section._palette
    assert strip._preset_bridge is not None
    assert len(strip._preset_bridge.colors) == n  # fill shares the store


def test_stroke_fill_values_independent(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(
        ToolType.RECTANGLE, name="R1", color="#1b1e26",
        fill_enabled=True, fill_color="#ffd60a")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, slot)
    for sw in form._fill_section._palette._swatches:
        if str(sw.property("swatch_color")).lower() == "#e5484d":
            sw.click()
            break
    assert form._slot.fill_color.lower() == "#e5484d"
    assert form._slot.fill_enabled is True
    assert form._slot.color.lower() == "#1b1e26"
    for sw in form._color_section._swatches:
        if str(sw.property("swatch_color")).lower() == "#2b6cff":
            sw.click()
            break
    assert form._slot.color.lower() == "#2b6cff"
    assert form._slot.fill_color.lower() == "#e5484d"


def test_preset_add_remove_sync_both_sides(qtbot, cleared_settings):
    from app.toolshelf.store import get_color_presets

    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, slot)
    n = len(get_color_presets())
    form._color_section._color = "#123456"
    assert form._color_section._commit_live_color() is True
    assert len(get_color_presets()) == n + 1
    assert len(form._fill_section._palette._swatches) == n + 1
    form._fill_section._palette._remove_preset("#123456")
    assert len(get_color_presets()) == n
    assert len(form._color_section._swatches) == n


def test_reopen_shows_persisted_stroke_and_fill(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(
        ToolType.RECTANGLE, name="R1", color="#1b1e26",
        fill_enabled=True, fill_color="#ffd60a")
    viewer.tool_shelf.refresh()
    viewer.tool_store.update_slot(slot.id.value, color="#e5484d",
                                  fill_color="#2b6cff")
    form = PropertiesForm(viewer.tool_store.find(slot.id.value),
                          dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    checked = [str(s.property("swatch_color")).lower()
               for s in form._color_section._swatches if s.isChecked()]
    assert checked == ["#e5484d"]
    fchecked = [str(s.property("swatch_color")).lower()
                for s in form._fill_section._palette._swatches
                if s.isChecked()]
    assert fchecked == ["#2b6cff"]


# -- §2 container ----------------------------------------------------------

@pytest.mark.parametrize("tokens", [dark_tokens(), light_tokens()],
                         ids=["dark", "light"])
def test_palette_container_contrast_and_circular(qtbot, cleared_settings,
                                                  tokens):
    from PySide6.QtGui import QColor, QImage

    from app.icons import contrast_ratio
    from app.toolshelf.tool_popup import _palette_surface
    from app.toolshelf.tool_popup import _palette_well, _swatch_outline

    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, slot, tokens)
    strip = form._color_section
    qtbot.wait(900)  # QML grid Ready
    bg, edge = _palette_well(tokens)
    popup = str(tokens.surface_raised)
    prev = _palette_surface(tokens)
    # Darker step past the M5-A2.1 container + boundary distinct from
    # both popup and fill (two-part rule, not a single floor).
    assert contrast_ratio(bg, prev) >= 1.5
    assert contrast_ratio(edge, popup) >= 2.0
    assert contrast_ratio(edge, bg) >= 1.6
    if popup.lower() == "#ffffff":
        assert contrast_ratio(bg, popup) >= 2.5
    # Rendered proof: QML well is a rounded rect (transparent corners),
    # swatch centers show preset colors, low-contrast swatches outlined.
    assert strip._grid_view is not None
    img = strip._grid_view.grabFramebuffer()
    assert img.pixelColor(1, 1).alpha() < 32  # no square tile behind
    pad = img.pixelColor(4, img.height() - 4)
    well = QColor(bg)
    assert max(abs(pad.red() - well.red()),
               abs(pad.green() - well.green()),
               abs(pad.blue() - well.blue())) < 40
    center = strip._swatch_center(0)
    assert center is not None
    first = strip._preset_bridge.colors[0]
    got = img.pixelColor(center.x(), center.y())
    want = QColor(first)
    assert max(abs(got.red() - want.red()),
               abs(got.green() - want.green()),
               abs(got.blue() - want.blue())) < 40
    # Visibility rule: black/white/gray never vanish on the well.
    for sw in ("#ffffff", "#111111", "#6b7280"):
        assert contrast_ratio(sw, bg) >= 1.8 or \
            _swatch_outline(sw, tokens, bg) != "transparent"


# -- §4 single header ------------------------------------------------------

def test_single_line_style_header(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, line)
    section_labels = [w for w in form.findChildren(QLabel)
                      if w.objectName() == "tool-popup-section"
                      and w.text() == "Line Style"]
    meta_labels = [w for w in form.findChildren(QLabel)
                   if w.objectName() == "tool-popup-meta"
                   and w.text() == "Line Style"]
    assert len(section_labels) == 0
    assert len(meta_labels) == 1  # the section's own header + drawer
    assert form._line_section is not None


def test_shape_keeps_single_unlabeled_section(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, slot)
    assert form._line_section is not None
    assert form._line_section._head_label is None  # compact, endings off
    assert sum(1 for w in form.findChildren(QLabel)
               if w.text() == "Line Style") == 0


# -- §3 icons --------------------------------------------------------------

def _cell_grab(qtbot, kind, **fields):
    from app.toolshelf.slot_button import SlotButton
    from tools.models import ToolInstance

    slot = ToolInstance(kind=kind, name=kind.value, **fields)
    btn = SlotButton(slot, dark_tokens())
    qtbot.addWidget(btn)
    btn.resize(40, 40)
    btn.show()
    qtbot.wait(80)
    return btn.grab()


def test_shape_icon_shows_fill_and_unfill(qtbot, cleared_settings):
    filled = _cell_grab(qtbot, ToolType.RECTANGLE, color="#e5484d",
                        fill_enabled=True, fill_color="#2b6cff")
    img = filled.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    center = img.pixelColor(20, 20)
    assert abs(center.blue() - 255) < 90 and center.red() < 120
    plain = _cell_grab(qtbot, ToolType.RECTANGLE, color="#e5484d")
    img2 = plain.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    c2 = img2.pixelColor(20, 20)
    assert abs(c2.red() - 24) > 40 or abs(c2.blue() - 35) > 40  # no fill


def test_line_style_and_endings_change_icon(qtbot, cleared_settings):
    solid = _cell_grab(qtbot, ToolType.LINE, color="#111111")
    dashed = _cell_grab(qtbot, ToolType.LINE, color="#111111",
                        line_style="dashed")
    dotted = _cell_grab(qtbot, ToolType.LINE, color="#111111",
                        line_style="dotted")
    assert _grab_distance(solid, dashed) > 500
    assert _grab_distance(solid, dotted) > 500
    arrow = _cell_grab(qtbot, ToolType.ARROW, color="#111111",
                       end_ending="arrow")
    bare = _cell_grab(qtbot, ToolType.ARROW, color="#111111")
    assert _grab_distance(arrow, bare) > 300


def test_icon_updates_on_live_property_change(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    slot = viewer.tool_store.add_slot(
        ToolType.RECTANGLE, name="R1", color="#e5484d")
    viewer.tool_shelf.refresh()
    qtbot.wait(100)
    button = viewer.tool_shelf._buttons[slot.id.value]
    before = button.grab()
    viewer._on_tool_live_changed(
        viewer.tool_store.find(slot.id.value))
    viewer.tool_store.update_slot(slot.id.value, fill_enabled=True,
                                  fill_color="#2b6cff",
                                  line_style="dashed")
    viewer._on_tool_live_changed(
        viewer.tool_store.find(slot.id.value))
    qtbot.wait(80)
    assert _grab_distance(before, button.grab()) > 500


def test_near_background_icon_stays_legible(qtbot, cleared_settings):
    from app.icons import contrast_ratio

    grab = _cell_grab(qtbot, ToolType.RECTANGLE, color="#181C23",
                      fill_enabled=True, fill_color="#181C23")
    img = grab.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    bg = QColor("#181C23")
    best = 1.0
    for y in range(10, 30):
        for x in range(10, 30):
            p = img.pixelColor(x, y)
            if p.alpha() < 128:
                continue
            lum = (lambda c: (lambda s: s / 12.92 if s <= 0.03928
                              else ((s + 0.055) / 1.055) ** 2.4)(c / 255.0))
            la = (0.2126 * lum(p.red()) + 0.7152 * lum(p.green())
                  + 0.0722 * lum(p.blue()))
            lb = (0.2126 * lum(bg.red()) + 0.7152 * lum(bg.green())
                  + 0.0722 * lum(bg.blue()))
            hi, lo = (la, lb) if la >= lb else (lb, la)
            best = max(best, (hi + 0.05) / (lo + 0.05))
    assert best >= 2.0
    assert contrast_ratio("#000000", "#000000") == 1.0  # helper sane


# -- §7 reorder ------------------------------------------------------------

def test_insertion_index_viewport_and_scroll(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=10)
    shelf = viewer.tool_shelf
    for dock in ("left", "top"):
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        bar = (shelf._scroll.horizontalScrollBar()
               if shelf._orientation() == "horizontal"
               else shelf._scroll.verticalScrollBar())
        for value in (bar.minimum(), bar.maximum()):
            bar.setValue(value)
            assert wait_stable(qtbot, bar.value)
            buttons = viewer._custom_buttons_in_order()
            for bi in (0, 3, len(buttons) - 1):
                b = buttons[bi]
                pt = b.mapTo(shelf, b.rect().center())
                got = viewer._reorder_insertion_index(
                    shelf.mapToGlobal(pt))
                assert got in (bi, bi + 1), (dock, value, bi, got)


def test_drag_reorders_and_persists(qtbot, cleared_settings):
    from PySide6.QtCore import QSettings

    from app.toolshelf.store import ToolShelfStore

    viewer = _make_viewer(qtbot, customs=6)
    names0 = [t.name for t in viewer.tool_store.custom_slots]
    buttons = viewer._custom_buttons_in_order()
    b0 = buttons[0]
    QTest.mousePress(b0, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, b0.rect().center())
    qtbot.wait(450)
    target = viewer._custom_buttons_in_order()[3]
    glob = target.mapToGlobal(target.rect().center())
    pt = b0.mapFromGlobal(glob)
    QTest.mouseMove(b0, pt)
    assert wait_until(qtbot, lambda: viewer._reorder_index is not None)
    QTest.mouseRelease(b0, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, pt)
    assert wait_stable(qtbot, lambda: (viewer.tool_shelf.width(),
                                       viewer.tool_shelf.height()))
    names = [t.name for t in viewer.tool_store.custom_slots]
    assert names[:4] == [names0[1], names0[2], names0[3], names0[0]]
    settings = QSettings("InkFallTest", "m5a2reorder")
    settings.clear()
    viewer.tool_store.persist(settings)
    restored = ToolShelfStore.restore(settings)
    assert [t.name for t in restored.custom_slots] == names
    settings.clear()


def test_home_drop_is_noop_and_click_selects(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=6)
    before = [t.name for t in viewer.tool_store.custom_slots]
    buttons = viewer._custom_buttons_in_order()
    b2 = buttons[2]
    QTest.mousePress(b2, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, b2.rect().center())
    qtbot.wait(450)
    QTest.mouseRelease(b2, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, b2.rect().center())
    assert wait_stable(qtbot, lambda: (viewer.tool_shelf.width(),
                                       viewer.tool_shelf.height()))
    assert [t.name for t in viewer.tool_store.custom_slots] == before
    QTest.mouseClick(viewer._custom_buttons_in_order()[1],
                     Qt.MouseButton.LeftButton)
    qtbot.wait(100)
    assert (viewer.tool_store.active_id
            == viewer._custom_buttons_in_order()[1]._slot.id.value)


# -- §5/§6 assist UI + persistence -----------------------------------------

def test_assist_fields_default_and_old_dicts(qtbot, cleared_settings):
    from app.toolshelf.store import _slot_from_dict
    from tools.models import ToolInstance

    slot = ToolInstance(kind=ToolType.LINE, name="L")
    assert (slot.snap_angle, slot.connect_endpoints,
            slot.connect_intersections, slot.close_path,
            slot.straight_line) == (False,) * 5
    old = {"kind": "line", "name": "Old", "color": "#111111"}
    loaded = _slot_from_dict(old)
    assert loaded is not None
    assert (loaded.snap_angle, loaded.close_path,
            loaded.straight_line) == (False, False, False)


def test_assist_sections_per_kind(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    cases = {
        # M6.7.2: ONE Connect Lines toggle (Line/Arrow only). Polygon
        # always connects and always closes internally, so it exposes
        # neither control.
        ToolType.LINE: (True, 1, False, False),
        ToolType.ARROW: (True, 1, False, False),
        ToolType.POLYGON: (True, 0, False, False),
        ToolType.HIGHLIGHTER: (False, 0, True, False),
        ToolType.PEN: (False, 0, False, False),
    }
    for kind, (snap, ntoggles, straight, _x) in cases.items():
        slot = viewer.tool_store.add_slot(kind, name=kind.value)
        viewer.tool_shelf.refresh()
        form = _popup(qtbot, viewer, slot)
        assert (form._snap is not None) == snap, kind
        if ntoggles:
            assert form._connect is not None, kind
            assert len(form._connect._toggles) == ntoggles, kind
        else:
            assert form._connect is None, kind
        assert (form._straight is not None) == straight, kind
        form.close()


def test_assist_toggle_persist_reopen(qtbot, cleared_settings):
    from PySide6.QtCore import QSettings

    from app.toolshelf.store import ToolShelfStore
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L1")
    hl = viewer.tool_store.add_slot(ToolType.HIGHLIGHTER, name="H1")
    viewer.tool_shelf.refresh()
    form = _popup(qtbot, viewer, line)
    form._snap._option.click()
    form._connect._toggles[0]._option.click()
    assert form._slot.snap_angle is True
    assert form._slot.connect_endpoints is True
    viewer.tool_store.update_slot(
        line.id.value, snap_angle=True, connect_endpoints=True)
    viewer.tool_store.update_slot(hl.id.value, straight_line=True)
    settings = QSettings("InkFallTest", "m5a2assist")
    settings.clear()
    viewer.tool_store.persist(settings)
    restored = ToolShelfStore.restore(settings)
    rl = next(s for s in restored.slots if s.name == "L1")
    rh = next(s for s in restored.slots if s.name == "H1")
    assert (rl.snap_angle, rl.connect_endpoints) == (True, True)
    assert rh.straight_line is True
    form2 = PropertiesForm(rl, dark_tokens(), viewer)
    form2.show()
    qtbot.addWidget(form2)
    qtbot.wait(80)
    assert form2._snap._option.isChecked() is True
    assert form2._connect._toggles[0]._option.isChecked() is True
    settings.clear()


def test_assist_settings_independent_per_tool(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    a = viewer.tool_store.add_slot(ToolType.LINE, name="A")
    b = viewer.tool_store.add_slot(ToolType.LINE, name="B")
    viewer.tool_shelf.refresh()
    viewer.tool_store.update_slot(a.id.value, snap_angle=True)
    fa = _popup(qtbot, viewer, viewer.tool_store.find(a.id.value))
    fb = _popup(qtbot, viewer, viewer.tool_store.find(b.id.value))
    assert fa._snap._option.isChecked() is True
    assert fb._snap._option.isChecked() is False
