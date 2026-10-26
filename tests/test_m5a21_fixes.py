"""M5-A2.1 acceptance remediation: dedupe, true morph, editors, picker, icon.

Covers the six user-acceptance defects; drawing behavior for the new
assist flags remains engine-phase work (config + persistence only).
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from app.theme.tokens import dark_tokens, light_tokens
from tests.qt_helpers import wait_edge_settled, wait_stable, wait_until
from tools.models import ToolInstance, ToolType

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


# -- Fix 1: exactly one Selection ------------------------------------------

def test_legacy_selection_plus_lasso_stays_single():
    from app.toolshelf.store import migrate_core_selection

    sel = ToolInstance(kind=ToolType.SELECTION, name="Select")
    las = ToolInstance(kind=ToolType.LASSO, name="Lasso")
    pen = ToolInstance(kind=ToolType.PEN, name="Pen #1")
    out = migrate_core_selection([sel, las, pen])
    assert [s.kind for s in out] == [
        ToolType.SELECTION, ToolType.LASSO, ToolType.PEN]
    assert out[1].name == "Lasso"  # custom lasso preserved, not converted


def test_pure_legacy_lasso_still_migrates():
    from app.toolshelf.store import migrate_core_selection

    las = ToolInstance(kind=ToolType.LASSO, name="Select")
    out = migrate_core_selection([las])
    assert len(out) == 1 and out[0].kind == ToolType.SELECTION
    assert out[0].id.value == las.id.value  # identity preserved


def test_dedupe_core_collapses_exact_dupes_idempotent():
    from app.toolshelf.store import dedupe_core_slots

    sel = ToolInstance(kind=ToolType.SELECTION, name="Select")
    dup = ToolInstance(kind=ToolType.SELECTION, name="Select")
    las = ToolInstance(kind=ToolType.LASSO, name="Lasso")
    pen = ToolInstance(kind=ToolType.PEN, name="Pen #1")
    out = dedupe_core_slots([sel, dup, las, pen])
    assert [s.kind for s in out] == [
        ToolType.SELECTION, ToolType.LASSO, ToolType.PEN]
    assert out[0].id.value == sel.id.value  # first identity wins
    assert [s.id.value for s in dedupe_core_slots(out)] == [
        s.id.value for s in out]


def test_store_restore_roundtrip_single_selection(cleared_settings):
    from PySide6.QtCore import QSettings

    from app.toolshelf.store import ToolShelfStore

    sel = ToolInstance(kind=ToolType.SELECTION, name="Select")
    las = ToolInstance(kind=ToolType.LASSO, name="Lasso")
    pen = ToolInstance(kind=ToolType.PEN, name="Pen #1")
    store = ToolShelfStore(slots=[sel, las, pen])
    assert sum(1 for s in store.slots
               if s.kind == ToolType.SELECTION) == 1
    settings = QSettings("InkFallTest", "m5a21dedupe")
    settings.clear()
    store.persist(settings)
    first = ToolShelfStore.restore(settings)
    second = ToolShelfStore.restore(settings)
    for restored in (first, second):
        kinds = [s.kind for s in restored.slots]
        assert kinds.count(ToolType.SELECTION) == 1
        assert kinds.count(ToolType.PAN) == 1
        assert ToolType.LASSO in kinds  # custom lasso survives
        assert ToolType.PEN in kinds
    settings.clear()


def test_toolbar_shows_one_selection_button(qtbot, cleared_settings):
    from app.toolshelf.slot_button import SlotButton

    viewer = _make_viewer(qtbot, customs=3)
    selections = [b for b in viewer.tool_shelf.findChildren(SlotButton)
                  if b._slot.kind == ToolType.SELECTION]
    assert len(selections) == 1


# -- Fix 2: single-path morph ----------------------------------------------

def test_morph_geometry_collinear_to_chevron():
    from app.toolshelf.shelf import _morph_path_points

    for horizontal in (True, False):
        for forward in (True, False):
            a, t, b, w = _morph_path_points(0.0, horizontal, forward,
                                            20.0, 7.0)
            # Reveal 0: straight separator (collinear, full span, 1px).
            assert w == 1.0
            if horizontal:
                assert a[0] == t[0] == b[0]
                assert (a[1], t[1], b[1]) == (7.0 - 12.0, 7.0, 7.0 + 12.0)
            else:
                assert a[1] == t[1] == b[1]
            a1, t1, b1, w1 = _morph_path_points(1.0, horizontal, forward,
                                               20.0, 7.0)
            assert w1 == 2.0
            # Reveal 1: compact centered chevron (arms +-5, tip +-5).
            if horizontal:
                assert t1[0] == 20.0 + (5.0 if forward else -5.0)
                assert (a1[1], b1[1]) == (7.0 - 5.0, 7.0 + 5.0)
                assert a1[0] == b1[0] == 20.0
            else:
                assert t1[1] == 7.0 + (5.0 if forward else -5.0)
            # Mid: tip strictly between, arms symmetric about center.
            am, tm, bm, _w = _morph_path_points(0.5, horizontal, forward,
                                               20.0, 7.0)
            if horizontal:
                assert abs(tm[0] - 20.0) == pytest.approx(2.5)
                assert abs((am[1] + bm[1]) / 2.0 - 7.0) < 1e-9
            else:
                assert abs(tm[1] - 7.0) == pytest.approx(2.5)


def _edge_ink_rows(pm):
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    bg = img.pixelColor(0, 0)
    rows = {}
    for y in range(img.height()):
        n = 0
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if max(abs(c.red() - bg.red()), abs(c.green() - bg.green()),
                   abs(c.blue() - bg.blue())) >= 12:
                n += 1
        rows[y] = n
    return rows


def test_morph_no_stationary_underlay(qtbot, cleared_settings):
    """Mid-morph: one bent path — no full-span straight line remains."""
    viewer = _make_viewer(qtbot, customs=14)
    shelf = viewer.tool_shelf
    edge = shelf._edge_next  # vertical dock: 40x14
    edge._reveal = 0.5
    edge._reveal_target = 0.5
    edge.update()
    qtbot.wait(80)
    rows = _edge_ink_rows(edge.grab())
    # A stationary separator would paint ~24px on the center row; the
    # bent path spreads ink across rows instead.
    assert rows[7] < 20
    assert sum(1 for n in rows.values() if n > 0) >= 3
    edge._reveal = 0.0
    edge._reveal_target = 0.0
    edge.update()
    qtbot.wait(80)
    rows0 = _edge_ink_rows(edge.grab())
    assert rows0[7] >= 20  # rest state: full clean line
    # No chevron arms below the line zone (tip would reach y>=9).
    assert sum(rows0[y] for y in rows0 if y >= 9) <= 4


def test_morph_roundtrip_all_docks_rendered(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=14)
    shelf = viewer.tool_shelf
    for dock in ("left", "right", "top", "bottom"):
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        bar = (shelf._scroll.horizontalScrollBar()
               if shelf._orientation() == "horizontal"
               else shelf._scroll.verticalScrollBar())
        bar.setValue(bar.minimum())
        assert wait_edge_settled(qtbot, shelf._edge_prev, 0.0)
        assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
        bar.setValue(bar.maximum())
        assert wait_edge_settled(qtbot, shelf._edge_prev, 1.0)
        assert wait_edge_settled(qtbot, shelf._edge_next, 0.0)


# -- Fix 4: container contrast ----------------------------------------------

@pytest.mark.parametrize("tokens", [dark_tokens(), light_tokens()],
                         ids=["dark", "light"])
def test_palette_container_meets_24(qtbot, cleared_settings, tokens):
    from app.icons import contrast_ratio
    from app.toolshelf.tool_popup import _palette_surface

    assert contrast_ratio(_palette_surface(tokens),
                          str(tokens.surface_raised)) >= 2.4


# -- Fix 3: numeric editors (QML drawer; bridge is the driver) --------------

def _open_size_editor(qtbot, section):
    section._custom_btn.click()
    qtbot.wait(900)
    assert section._editor is not None
    assert section._bridge is not None
    return section._bridge


def test_size_spin_bound_to_kind_range(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(line, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    section = form._size_section
    bridge = _open_size_editor(qtbot, section)
    assert (bridge.minimum, bridge.maximum) == (
        section._min_w, section._max_w)
    bridge.commit("20")
    qtbot.wait(80)
    assert float(form._slot.width_mm) <= section._max_w + 1e-9
    assert float(form._slot.width_mm) >= section._min_w - 1e-9


def test_opacity_allows_zero(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    section = form._opacity_section
    section._custom_btn.click()
    qtbot.wait(900)
    assert section._bridge.minimum == 0
    section._bridge.commit("0")
    assert form._slot.opacity == 0.0
    assert section._slider.minimum() == 0


def test_escape_restores_pre_edit_value(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    line = viewer.tool_store.add_slot(ToolType.LINE, name="L1",
                                      width_mm=0.5)
    viewer.tool_shelf.refresh()
    form = PropertiesForm(line, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    section = form._size_section
    bridge = _open_size_editor(qtbot, section)
    # Type uncommitted text (no Enter): model untouched so far.
    section._editor.setFocus()
    qtbot.wait(200)
    QTest.keyClicks(section._editor, "1.1")
    qtbot.wait(60)
    assert form._slot.width_mm == pytest.approx(0.5)
    # Real Escape key across the QWidget/QML boundary discards it.
    QTest.keyClick(section._editor, Qt.Key.Key_Escape)
    qtbot.wait(60)
    assert form._slot.width_mm == pytest.approx(0.5)
    assert bridge.displayText.startswith("0.5")


def test_focus_selects_all_for_replacement(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm
    from PySide6.QtCore import QObject

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    bridge = _open_size_editor(qtbot, form._size_section)
    bridge.requestFocus()
    qtbot.wait(300)
    root = form._size_section._editor.rootObject()
    field = root.findChild(QObject, "numericField")
    assert field is not None
    assert field.property("selectedText") != ""


# -- Fix 5: square picker gradients ----------------------------------------

def test_hue_slider_renders_full_range(qtbot, cleared_settings):
    """QML hue slider shows the rainbow stops (red..violet..red)."""
    from app.toolshelf.tool_popup import _QmlColorCard

    card = _QmlColorCard(dark_tokens())
    qtbot.addWidget(card)
    card.resize(260, 340)
    card.show()
    qtbot.wait(1500)
    assert card._view is not None
    fb = card._view.grabFramebuffer()
    vw = fb.width()
    # Hue bar geometry in view coords: 200 wide centered, y ~= 222..236.
    x0 = (vw - 200) / 2
    scale = fb.width() / card._view.width()
    left = fb.pixelColor(int((x0 + 4) * scale), int(229 * scale))
    mid = fb.pixelColor(int((x0 + 100) * scale), int(229 * scale))
    right = fb.pixelColor(int((x0 + 196) * scale), int(229 * scale))
    assert left.red() > 180 and left.green() < 120  # red stop
    assert mid.green() > 120  # cyan/green middle
    assert right.red() > 180 and right.blue() < 150  # wraps to red


def test_identical_moves_emit_nothing(qtbot, cleared_settings):
    """No redundant emissions: same values in -> silence out."""
    from app.qml_bridge.color_provider import SquarePickerBridge

    bridge = SquarePickerBridge()
    picks, hues = [], []
    bridge.picked.connect(picks.append)
    bridge.huePicked.connect(hues.append)
    bridge.setHSV(100, 0.5, 0.5)
    bridge.moveSV(100, 100, 200, 200)  # identical: silent
    bridge.moveHue(100 / 359 * 200, 200)  # ~hue 100: silent
    assert picks == [] and hues == []
    bridge.moveSV(150, 50, 200, 200)  # real change: emits once
    assert len(picks) == 1


def test_picker_drag_end_to_end(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    section = form._color_section
    section._on_palette_add_tile()
    qtbot.wait(250)
    ring = section._ring
    ring.set_hsv(200, 1.0, 1.0)  # saturated start: SV moves change color
    before = form._slot.color
    # Square-space sweep: vertical line down the middle (varying V).
    for i in range(12):
        ring._bridge.moveSV(100, 10 + i * 15, 200, 200)
    qtbot.wait(80)
    assert form._slot.color.lower() != before.lower()
    # Commit keeps the dragged color and collapses cleanly.
    section._on_add_custom_collapse()
    qtbot.wait(250)
    assert section._picker_open is False


def test_picker_wrap_rounded(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    form.show()
    qtbot.addWidget(form)
    qtbot.wait(100)
    style = form._color_section._picker_wrap.styleSheet()
    assert "border-radius" in style
    assert "background-color" in style


# -- Fix 6: selection redesign -------------------------------------------------

def test_selection_arrow_marquee_design():
    from app import icons as icon_mod
    from app.icons import ink_center_offset, pixmap, tool_pixmap

    body = icon_mod._PATHS["selection"]
    # Genuinely new construction: cursor-arrow + marquee corner cross.
    assert "stroke-dasharray" in body  # marquee retained
    assert "M15 12.5" in body  # arrow tip at the marquee corner
    assert "M10.1 12.1" not in body  # M5-A1.1 hand silhouette gone
    assert "width='11' height='11'" not in body  # M5-A1 block gone
    for color in ("#B4BAC7", "#5B626E"):
        pm = pixmap("selection", color, 20)
        assert not pm.isNull()
        dx, dy = ink_center_offset(pm, ("sel21", color))
        assert abs(dx) <= 2.0 and abs(dy) <= 2.0
        # Near-white arrow fill: ink distinctly brighter than the
        # outline ink itself (not just a light outline).
        Line0 = QColor(color).lightness()
        img = pm.toImage()
        bright = sum(
            1 for x in range(pm.width()) for y in range(pm.height())
            if img.pixelColor(x, y).alpha() > 8
            and img.pixelColor(x, y).lightness() > Line0 + 40)
        assert bright > 5  # near-white arrow fill + fringe present
    # Near-background configured colors stay legible via the outline.
    for bg, color in (("#181C23", "#181C23"), ("#FFFFFF", "#FFFFFF")):
        pm = tool_pixmap("selection", color, bg, 20)
        assert not pm.isNull()


def test_selection_distinct_from_pan_lasso_rendered():
    from PySide6.QtGui import QImage

    from app.icons import pixmap

    def alpha_bytes(name):
        img = pixmap(name, "#B4BAC7", 20).toImage().convertToFormat(
            QImage.Format.Format_ARGB32)
        return bytes(img.constBits())

    assert alpha_bytes("selection") != alpha_bytes("pan")
    assert alpha_bytes("selection") != alpha_bytes("lasso")
    assert alpha_bytes("selection") != alpha_bytes("pen-nib")
