"""Focused toolbar UX correction checks (M6.9 / M6.9.1 / M6.9.3).

Covers only the corrected behaviours: library category navigation keeps
the popup open, adding from a category is not pre-empted by dismissal,
library icons share one monochrome background-free treatment, property
popups survive internal edits, outside clicks dismiss, child palettes do
not, and the selected-tool three-dot affordance. M6.9.3 rebuilds that
affordance: ONE integrated control whose shared capsule wraps the
selected tool's own circular button and paints a separate, solid black
circular three-dot button of its own beneath it — a real second button,
not a badge. The checks pin the geometry (dots below the tool, both
inside the shared capsule, inside the toolbar), that the options face is
a genuinely circular, opaque, near-black disc of the documented diameter
carrying three separate light dots centred inside it, that the original
43px custom pitch is untouched — so the options pointer target stays
inside the selected slot's own cell plus the inter-slot gap while the
painted face may only graze the next slot's *empty* padding, never the
next tool's icon and never the Custom|Add boundary — the two independent
hit targets (including a click over the painted overflow still reaching
the neighbouring tool), that a selection never flashes through a wrong
position (and that an ineligible Selection/Pan switch never exposes the
capsule at all, not even mid-animation), rapid switching stability, popup
toggle without capsule movement, and Selection/Pan exclusion. The header
trash removes the right instance (including the active fallback,
persistence and capsule/popup cleanup).
"""

from __future__ import annotations

from collections import Counter

import pytest
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSettings,
    Qt,
)
from PySide6.QtGui import QPixmap, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.theme.tokens import dark_tokens
from app.toolshelf.capsule import (
    CAPSULE_DOTS_D,
    CAPSULE_MERGE,
    CAPSULE_TOOL_D,
    CapsuleOptionsButton,
    ToolCapsule,
    capsule_advance,
    capsule_size,
)
from app.toolshelf.shelf import (
    MAX_VISIBLE_CUSTOMS,
    MIN_VISIBLE_CUSTOMS,
    SEPARATION_ACTIVE,
    SEPARATION_NEIGHBOUR,
    _SPACING,
)
from app.toolshelf.slot_button import CELL
from app.toolshelf.store import ADDABLE_KINDS, ToolShelfStore
from app.toolshelf.surfaces import (
    blend_over,
    hover_wash,
    selected_surface,
)
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def cleared_settings():
    keys = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
            "inkfall/m5/toolbar/active",
            "inkfall/m5/toolbar/custom_colors",
            "inkfall/m5/toolbar/presets")
    for key in keys:
        QSettings().remove(key)
    yield
    for key in keys:
        QSettings().remove(key)


def _make_viewer(qtbot):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    QApplication.processEvents()
    return viewer


def _open_library(viewer):
    viewer._on_add_requested()
    QApplication.processEvents()
    popup = viewer._tool_popup
    assert popup is not None and popup.isVisible()
    return popup, popup._content


def _open_properties(viewer, slot):
    assert viewer._open_properties_popup(slot, viewer.tool_shelf.dots)
    QApplication.processEvents()
    popup = viewer._tool_popup
    assert popup is not None and popup.isVisible()
    return popup, popup._content


def _add_editable(viewer, kind, name):
    slot = viewer.tool_store.add_slot(kind, name=name)
    viewer._refresh_shelf()
    return slot


def _core_slot(viewer, kind):
    return next(s for s in viewer.tool_store.slots if s.kind == kind)


def _capsule(viewer):
    return viewer.tool_shelf.capsule


def _dots(viewer):
    return viewer.tool_shelf.dots


def _settle(viewer):
    """Force the shared geometry to its final resting place."""
    viewer.tool_shelf._sync_capsule(animate=False)
    QApplication.processEvents()


def _tool_circle(viewer):
    """Tool circle ``(x, y, w, h)`` in shelf coordinates (active slot).

    Includes the active tile's paint-only separation offset: while its
    capsule is shown the selected tool rises a few pixels, and the
    capsule's top end is centred on that *lifted* circle (one
    authoritative calculation, ``ToolShelf._capsule_geometry``).
    """
    shelf = viewer.tool_shelf
    button = shelf._buttons[viewer.tool_store.active_id]
    tl = button.mapTo(shelf, QPoint(0, 0))
    ox, oy = button.content_offset
    cx = tl.x() + button.width() / 2.0 + ox
    cy = tl.y() + button.height() / 2.0 + oy
    half = CAPSULE_TOOL_D / 2.0
    return (cx - half, cy - half, CAPSULE_TOOL_D, CAPSULE_TOOL_D)


def _render(widget):
    """The widget's own paint on a transparent pixmap, as a QImage."""
    pixmap = QPixmap(widget.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap, QPoint(0, 0), QRegion(),
                  QWidget.RenderFlag.DrawChildren)
    return pixmap.toImage()


def _ink(widget):
    """Non-transparent pixels of a widget rendered WITHOUT its window
    background, as ``(x, y)`` — only what the control itself paints."""
    image = _render(widget)
    pixels = []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > 8:
                pixels.append((x, y))
    return pixels


def _runs(xs):
    """Contiguous runs of sorted integers, as ``(first, last)`` pairs."""
    runs = []
    for x in sorted(set(xs)):
        if runs and x == runs[-1][1] + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    return [tuple(run) for run in runs]


def _bounds(pixels):
    xs = [p[0] for p in pixels]
    ys = [p[1] for p in pixels]
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1,
                 max(ys) - min(ys) + 1)


def _near(color, target, tol: int = 2) -> bool:
    """Rendered-pixel colour match (alpha included)."""
    return (color.alpha() == target.alpha()
            and abs(color.red() - target.red()) <= tol
            and abs(color.green() - target.green()) <= tol
            and abs(color.blue() - target.blue()) <= tol)


def _accent_count(image) -> int:
    """Blue-dominant pixels: an accent stroke would show up here."""
    return sum(1 for y in range(image.height())
               for x in range(image.width())
               if image.pixelColor(x, y).blue()
               > image.pixelColor(x, y).red() + 20)


def _slot_cell(viewer, slot):
    """Interactive cell of ``slot``''s button, in shelf coordinates."""
    shelf = viewer.tool_shelf
    button = shelf._buttons[slot.id.value]
    return QRect(button.mapTo(shelf, QPoint(0, 0)), button.size())


# 1 ------------------------------------------------------------------------

def test_category_navigation_keeps_library_open(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    popup, grid = _open_library(viewer)

    QTest.mouseClick(grid._tabs[1], Qt.MouseButton.LeftButton)
    qtbot.wait(30)
    assert viewer._tool_popup is popup and popup.isVisible()
    assert grid.current_tab() == "Shapes"

    # Keyboard category navigation is internal too.
    QTest.keyClick(grid._tabs[1], Qt.Key.Key_Right)
    qtbot.wait(30)
    assert viewer._tool_popup is popup and popup.isVisible()
    assert grid.current_tab() == "Other"


# 2 ------------------------------------------------------------------------

def test_add_from_category_is_not_preempted_by_dismissal(
        qtbot, cleared_settings):
    from app.toolshelf.tool_popup import _AddCell

    viewer = _make_viewer(qtbot)
    popup, grid = _open_library(viewer)
    before = len(viewer.tool_store.slots)
    cell = grid._stack.currentWidget().findChildren(_AddCell)[0]

    # The press that starts the add is internal: it must not close first.
    QTest.mousePress(cell, Qt.MouseButton.LeftButton)
    assert viewer._tool_popup is popup and popup.isVisible()
    QTest.mouseRelease(cell, Qt.MouseButton.LeftButton)
    qtbot.wait(40)

    assert len(viewer.tool_store.slots) == before + 1
    assert viewer.tool_store.slots[-1].kind == cell._kind
    # Adding completed, then the library closed deliberately.
    assert viewer._tool_popup is None


# 3 ------------------------------------------------------------------------

def test_library_icons_share_one_monochrome_treatment(
        qtbot, cleared_settings, monkeypatch):
    """Every kind renders one neutral glyph, no colored tile/backing."""
    import app.toolshelf.tool_popup as popup_mod
    from app.toolshelf.tool_popup import _AddCell

    tokens = dark_tokens()
    neutral = {c.lower() for c in (
        tokens.text_primary, tokens.text_secondary, tokens.text_muted)}
    calls: list[tuple] = []
    tool_calls: list[tuple] = []
    real_icon = popup_mod._icon_pixmap
    real_tool = popup_mod._tool_pixmap

    def spy_icon(name, color, size=20):
        calls.append((name, str(color).lower(), int(size)))
        return real_icon(name, color, size)

    def spy_tool(name, color, *args, **kwargs):
        tool_calls.append((name, str(color)))
        return real_tool(name, color, *args, **kwargs)

    monkeypatch.setattr(popup_mod, "_icon_pixmap", spy_icon)
    monkeypatch.setattr(popup_mod, "_tool_pixmap", spy_tool)

    for kind in ADDABLE_KINDS:
        calls.clear()
        cell = _AddCell(kind, kind.value.title(), tokens)
        qtbot.addWidget(cell)
        cell.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cell.show()
        cell.clearFocus()
        QApplication.processEvents()
        # Artwork contract: theme-neutral ink only, fixed 24px tool glyph
        # (the 12px add plus), and never the color-tinted tool renderer.
        glyphs = [(n, c, s) for n, c, s in calls if s == 24]
        assert glyphs, f"{kind} drew no 24px glyph"
        for _name, color, _size in calls:
            assert color in neutral, (kind, color)
        assert not tool_calls, (kind, tool_calls)
        image = cell.grab().toImage()
        pixels = []
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                if color.alpha() > 8:
                    pixels.append((color.red(), color.green(),
                                   color.blue()))
        assert pixels, f"{kind} rendered nothing"
        background = Counter(pixels).most_common(1)[0][0]
        for pixel in pixels:
            if max(abs(pixel[i] - background[i]) for i in range(3)) <= 8:
                continue
            # Neutral grays barely spread; a tool color (yellow pen,
            # accent badge, tinted tile) spreads far wider.
            assert max(pixel) - min(pixel) <= 32, (kind, pixel, background)


# 4 ------------------------------------------------------------------------

def test_property_change_keeps_popup_open(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    popup, form = _open_properties(viewer, pen)

    form._color_section._emit("#00ff00")
    qtbot.wait(30)
    assert viewer._tool_popup is popup and popup.isVisible()
    assert form._slot.color.lower() == "#00ff00"
    stored = viewer.tool_store.find(pen.id.value)
    assert stored is not None and stored.color.lower() == "#00ff00"

    # A second internal edit still leaves it open (no one-shot pass).
    form._opacity_section._emit(0.6)
    qtbot.wait(30)
    assert viewer._tool_popup is popup and popup.isVisible()


# 5 ------------------------------------------------------------------------

def test_outside_click_dismisses_popup(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    popup, _form = _open_properties(viewer, pen)
    assert not popup.geometry().contains(QPoint(4, 4))

    QTest.mouseClick(viewer, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
    qtbot.wait(320)
    assert viewer._tool_popup is None


# 6 ------------------------------------------------------------------------

def test_child_palette_does_not_dismiss_parent(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    popup, form = _open_properties(viewer, pen)

    swatch = form._color_section._swatches[2]
    swatch.click()
    qtbot.wait(30)
    assert viewer._tool_popup is popup and popup.isVisible()


# 7 ------------------------------------------------------------------------
# Selected-tool capsule (M6.9.3): ONE integrated control — a shared
# outer capsule wrapping the selected tool circle and painting a separate,
# solid black three-dot circle directly beneath it. The options face is
# part of the shared surface; its pointer target is the band of that face
# the slot's own pitch can offer, so a click over the visual overflow
# still reaches the neighbouring tool. No position flash.

def test_vertical_capsule_wraps_tool_and_dots(qtbot, cleared_settings):
    """Default left dock: one shared capsule wraps the tool circle AND
    an options circle of the SAME diameter, and stays inside the toolbar."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "vertical"

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    assert cap.isVisible() and dots.isVisible()
    assert cap.parent() is shelf and dots.parent() is shelf

    capsule = cap.geometry()
    tool = QRect(*[int(round(v)) for v in _tool_circle(viewer)])
    # The shared outer form is completely inside the toolbar...
    assert shelf.rect().contains(capsule), (capsule, shelf.rect())
    # ...and it wraps BOTH inner circles.
    assert capsule.contains(tool), (capsule, tool)
    assert capsule.contains(dots.geometry()), (capsule, dots.geometry())
    # The options button is the tool button's own diameter, never a badge.
    assert CAPSULE_DOTS_D == CAPSULE_TOOL_D, (CAPSULE_DOTS_D, CAPSULE_TOOL_D)


def test_capsule_is_one_continuous_backdrop_below_the_tiles(
        qtbot, cleared_settings):
    """M6.9.4: ONE continuous capsule, painted UNDER the slot tiles.

    Two regressions are pinned here. First, the contour used to be the
    union of the tool circle and the options circle, which is a peanut —
    a big circle with a small tail — so the capsule must hold its width
    through the neck between the two controls. Second, the body used to be
    filled with the winding rule on a difference path, which re-filled the
    hole punched for the tool and painted an opaque shape straight over
    the selected tool's icon. That subtraction is gone: the capsule is a
    backdrop kept below the tiles, so the slot button paints the selected
    halo and glyph over it and no fill rule can cover anything.
    """
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    shelf = viewer.tool_shelf
    cap = _capsule(viewer)
    dots = _dots(viewer)
    scroll = shelf._scroll

    # The widget IS the capsule: one rounded outline of the documented
    # size, no extra lobes tacked on.
    assert (cap.width(), cap.height()) == capsule_size(False)

    # It is a BACKDROP: it sits below the tiles that paint over it — the
    # custom viewport and everything inside it...
    children = shelf.children()
    assert children.index(cap) < children.index(scroll), children.index(cap)
    # ...while the options pointer target rides above them, so the face it
    # does not paint still receives the clicks.
    assert children.index(dots) > children.index(scroll), children.index(dots)
    assert cap.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # The body is opaque across the capsule — including straight over the
    # selected tool's own circle, where the tile above it paints the halo
    # and the glyph.
    image = _render(cap)
    tc = cap.tool_center
    for dx, dy in ((0.0, 0.0), (0.0, -13.0), (0.0, 9.0),
                   (-13.0, 0.0), (13.0, 0.0)):
        px = int(round(tc.x() + dx))
        py = int(round(tc.y() + dy))
        assert image.pixelColor(px, py).alpha() == 255, (dx, dy)

    def _span(row):
        xs = [x for x in range(image.width())
              if image.pixelColor(x, row).alpha() > 128]
        return (min(xs), max(xs)) if xs else None

    # ONE continuous capsule through the neck, not two circles fused: the
    # old union contour pinched to ~15px of half-width between the
    # controls, where a capsule keeps the full width.
    neck = int((tc.y() + cap.dots_center.y()) / 2.0)
    for row in (int(tc.y()), neck):
        span = _span(row)
        assert span is not None, row
        assert span[1] - span[0] + 1 >= cap.width() - 2, (row, span)


def test_dots_are_below_the_tool_not_beside_it(qtbot, cleared_settings):
    """In the default left toolbar the dots sit on the tool's axis,
    beneath the tool — never to its side."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "vertical"

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    x, y, w, h = _tool_circle(viewer)
    tool_cx, tool_cy = x + w / 2.0, y + h / 2.0

    # Same axis. (The pointer target is the disc's own pitch-sized band,
    # so its across extent is the disc's: their centres share the x.)
    assert abs(dots.geometry().center().x() - tool_cx) <= 1, (
        dots.geometry(), tool_cx)

    # The options circle itself (painted by the shared capsule) is centred
    # below the tool's circle, on that same axis.
    disc_cy = cap.pos().y() + cap.dots_center.y()
    disc_cx = cap.pos().x() + cap.dots_center.x()
    assert abs(disc_cx - tool_cx) <= 0.5, (disc_cx, tool_cx)
    assert disc_cy >= tool_cy + CAPSULE_TOOL_D / 2.0, (disc_cy, tool_cy)

    # Directly below, not beside: the two circles overlap by exactly the
    # documented CAPSULE_MERGE (the slot's own pitch has no room for two
    # tangent circles without reaching into the neighbouring cell).
    overlap = (tool_cy + CAPSULE_TOOL_D / 2.0) - (disc_cy - CAPSULE_DOTS_D / 2.0)
    assert overlap == CAPSULE_MERGE, (overlap, CAPSULE_MERGE, disc_cy, tool_cy)
    assert disc_cy - tool_cy == capsule_advance()


def test_custom_slot_pitch_stays_the_original_43px(qtbot, cleared_settings):
    """The selected-tool control is slot-local: custom slots keep the
    toolbar's original pitch (CELL + _SPACING == 43px), the Custom|Add
    boundary follows the last custom immediately (no 16px reservation),
    and selecting a tool never moves a single slot."""
    viewer = _make_viewer(qtbot)
    slots = [_add_editable(viewer, ToolType.PEN, "Pen A"),
             _add_editable(viewer, ToolType.HIGHLIGHTER, "HL"),
             _add_editable(viewer, ToolType.ERASER, "Er")]
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "vertical"
    assert CELL + _SPACING == 43

    viewer._shelf.select_slot(slots[0].id.value)
    _settle(viewer)

    tops = [_slot_cell(viewer, slot).top() for slot in slots]
    steps = [later - earlier for earlier, later in zip(tops, tops[1:])]
    assert steps == [CELL + _SPACING] * (len(slots) - 1), tops

    # The boundary after the customs sits one plain inter-slot gap below
    # the last custom cell — the pre-correction rhythm, not a reserved
    # pocket for the options control.
    last = _slot_cell(viewer, slots[-1])
    edge = shelf._edge_next
    assert edge is not None
    assert edge.geometry().top() == last.bottom() + 1 + _SPACING, (
        edge.geometry(), last)

    before = {sid: b.geometry() for sid, b in shelf._buttons.items()}
    viewer._shelf.select_slot(slots[1].id.value)
    QApplication.processEvents()
    viewer._shelf.select_slot(_core_slot(viewer, ToolType.PAN).id.value)
    QApplication.processEvents()
    after = {sid: b.geometry() for sid, b in shelf._buttons.items()}
    assert after == before


def test_bounded_viewport_scrolls_at_the_original_pitch(
        qtbot, cleared_settings):
    """Scrolling and the tools-visible count survive the restored pitch:
    overflow scrolls inside the bounded viewport instead of growing the
    toolbar, and a smaller budget shrinks the visible count — never a
    slot's pitch (asserted natively here so it cannot depend on the
    offscreen window geometry)."""
    viewer = _make_viewer(qtbot)
    for i in range(16):
        _add_editable(viewer, ToolType.PEN, f"P{i}")
    shelf = viewer.tool_shelf
    viewer._shelf.refresh()
    QApplication.processEvents()

    inner = shelf._scroll._inner_layout
    assert inner.count() == 16, "custom tools were dropped"
    cap = shelf._visible_custom_cap()
    assert MIN_VISIBLE_CUSTOMS <= cap <= MAX_VISIBLE_CUSTOMS, cap
    visible = min(16, cap)
    shelf.fit_to_available(shelf._natural_along(visible))
    assert shelf._fit_visible == visible, (shelf._fit_visible, visible)

    # The viewport is bounded: with 16 customs and at most 12 visible,
    # the overflow scrolls rather than enlarging the toolbar.
    bar = (shelf._scroll.horizontalScrollBar()
           if shelf._orientation() == "horizontal"
           else shelf._scroll.verticalScrollBar())
    assert bar.maximum() > 0

    # A tighter budget drops visible tools by whole 43px pitches; a
    # looser one never invents slots.
    shelf.fit_to_available(
        shelf._natural_along(visible) - 4 * (CELL + _SPACING))
    assert shelf._fit_visible == visible - 4, shelf._fit_visible
    shelf.fit_to_available(shelf._natural_along(visible) + 10_000)
    assert shelf._fit_visible == visible, shelf._fit_visible
    assert inner.count() == 16


def test_options_button_is_a_full_circle_the_size_of_the_tool(
        qtbot, cleared_settings):
    """The options button is a genuine second button: the SAME diameter as
    the tool button, an opaque near-black face painted as part of the
    shared capsule, and three light dots centred inside that face — never
    a small oval, a faint translucent ring or a badge-sized dot."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    # A second button, not a badge and not a shrunken workaround: the tool
    # button's own diameter.
    assert CAPSULE_DOTS_D == CAPSULE_TOOL_D, (CAPSULE_DOTS_D, CAPSULE_TOOL_D)

    # The disc belongs to the shared capsule; the pointer target sits on
    # it (and, by construction, inside this slot's own pitch).
    assert dots.parent() is cap.parent()
    cx, cy = int(cap.dots_center.x()), int(cap.dots_center.y())
    assert 0 <= cx - CAPSULE_DOTS_D // 2
    assert cx + CAPSULE_DOTS_D // 2 <= cap.width()
    assert 0 <= cy - CAPSULE_DOTS_D // 2
    assert cy + CAPSULE_DOTS_D // 2 <= cap.height()
    assert dots.geometry().contains(
        cap.pos() + QPoint(cx, cy)), (dots.geometry(), cap.pos())

    image = _render(cap)
    tokens = dark_tokens()
    base = selected_surface(tokens)
    # The face is OPAQUE near-black: the button reads as a black disc, not
    # as a faint outline on a translucent oval. (It shares the body's own
    # surface, exactly like the reference's single solid pill.) Measured
    # just off the dots' own row, inside the disc.
    face = image.pixelColor(cx, cy + CAPSULE_DOTS_D // 4)
    assert face.alpha() == 255, ("the face is translucent", face)
    assert max(face.red(), face.green(), face.blue()) <= 24, (
        "the face is not near-black", face)
    assert _near(face, base, 2), ("the face is not the shared surface", face)

    # ...and that disc really is CAPSULE_DOTS_D across. The face shares the
    # body's colour, so the disc is measured where it is drawn distinct:
    # pressed fills it with the shared ink wash (the same state language
    # the tool slots use), and that fill traces the same 30px circle.
    lifted = blend_over(base, hover_wash(tokens, pressed=True))
    dots.setDown(True)
    try:
        pressed = _render(cap)
    finally:
        dots.setDown(False)
    filled = [(x, y) for y in range(pressed.height())
              for x in range(pressed.width())
              if _near(pressed.pixelColor(x, y), lifted, 2)]
    assert filled, "the pressed options face did not lift its fill"
    disc = _bounds(filled)
    assert abs(disc.width() - CAPSULE_DOTS_D) <= 2, disc
    assert abs(disc.height() - CAPSULE_DOTS_D) <= 2, disc
    assert abs(disc.center().x() - cx) <= 1, (disc, cx)
    assert abs(disc.center().y() - cy) <= 1, (disc, cy)

    # Fills only: no state of this control draws an outline, an accent ring
    # or any circular stroke — the removed pressed rim would have been
    # accent-coloured, so the whole capsule must be accent-free.
    for state, label in ((image, "idle"), (pressed, "pressed")):
        accent = [(x, y) for y in range(state.height())
                  for x in range(state.width())
                  if state.pixelColor(x, y).blue()
                  > state.pixelColor(x, y).red() + 20]
        assert not accent, ("a ring was stroked on the options control",
                            label, accent[:4])

    # The three dots are painted in a light ink inside that circle, as a
    # horizontal cluster of three separate dots, centred on the disc.
    light = [(x, y) for y in range(image.height())
             for x in range(image.width())
             if min(image.pixelColor(x, y).red(),
                    image.pixelColor(x, y).green(),
                    image.pixelColor(x, y).blue()) >= 180]
    assert light, "no dots painted on the black face"
    cluster = _bounds(light)
    assert cluster.width() > cluster.height(), cluster
    mean_x = sum(x + 0.5 for x, y in light) / len(light)
    mean_y = sum(y + 0.5 for x, y in light) / len(light)
    assert abs(mean_x - cx) <= 1, (mean_x, cluster)
    assert abs(mean_y - cy) <= 1, (mean_y, cluster)
    # The cluster lives inside the disc, with real button left around it:
    # the dots are on the face, never on the tool's own background.
    assert 8 <= cluster.width() < CAPSULE_DOTS_D, cluster
    # Three separate dots on their centre row, not one smeared dash: the
    # bright cores are split by dark pixels between them.
    cy_row = cluster.center().y()
    runs = _runs([x for x, y in light if y == cy_row])
    assert len(runs) == 3, (runs, cluster)


def test_selected_tool_is_a_filled_disc_never_a_border(
        qtbot, cleared_settings):
    """Selected tools answer with ONE filled language.

    A selected tool reads as an opaque near-black disc — for a tool that
    owns the capsule and for a tool that never gets one (Pan / Selection)
    alike. The removed accent selection edge used to rim that disc, so the
    disc's own rim must now be the fill, never an accent stroke.
    """
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    pan = _core_slot(viewer, ToolType.PAN)
    shelf = viewer.tool_shelf
    tokens = dark_tokens()
    base = selected_surface(tokens)

    for slot, wants_capsule in ((pan, False), (pen, True)):
        viewer._shelf.select_slot(slot.id.value)
        _settle(viewer)
        assert _capsule(viewer).isVisible() is wants_capsule

        button = shelf._buttons[slot.id.value]
        assert button.isChecked()
        image = _render(button)
        cx = cy = CELL // 2

        # One opaque near-black disc of the tool button's own diameter.
        disc_px = [(x, y) for y in range(image.height())
                   for x in range(image.width())
                   if _near(image.pixelColor(x, y), base, 2)]
        assert disc_px, ("no filled selected disc", slot.name)
        disc = _bounds(disc_px)
        assert abs(disc.width() - CAPSULE_TOOL_D) <= 2, (slot.name, disc)
        assert abs(disc.height() - CAPSULE_TOOL_D) <= 2, (slot.name, disc)

        # The rim is that fill — not an accent ring (which is exactly what
        # the old accent-wash + accent-edge selection drew here). Sampled
        # on the disc's own axis, outside every 20-24px glyph.
        for dx, dy in ((0, -14), (-14, 0), (14, 0)):
            px = image.pixelColor(cx + dx, cy + dy)
            assert px.alpha() > 0, (slot.name, dx, dy)
            assert px.blue() <= px.red() + 20, (
                "the selected tool still wears an accent border",
                slot.name, dx, dy, px)


def test_hover_and_pressed_are_fills_never_borders(qtbot, cleared_settings):
    """Hover / pressed / open answer with a filled wash, never a rim.

    The three-dot options control and the tool slots share one rule from
    :mod:`app.toolshelf.surfaces`: the same restrained ink wash over the
    same disc. The contour is identical in every state.
    """
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    other = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    tokens = dark_tokens()
    base = selected_surface(tokens)
    rest = _render(cap)
    cx, cy = int(cap.dots_center.x()), int(cap.dots_center.y())

    dots.setDown(True)
    try:
        pressed = _render(cap)
    finally:
        dots.setDown(False)

    lifted = blend_over(base, hover_wash(tokens, pressed=True))
    assert _near(rest.pixelColor(cx, cy + 12), base), "the disc is not filled"
    assert _near(pressed.pixelColor(cx, cy + 12), lifted), (
        "pressing the options control painted no FILL")
    # Same silhouette in both states: nothing was stroked in or out.
    column = [y for y in range(rest.height())
              if rest.pixelColor(cx, y).alpha() > 8]
    assert column == [y for y in range(pressed.height())
                      if pressed.pixelColor(cx, y).alpha() > 8]
    for state, label in ((rest, "rest"), (pressed, "pressed")):
        accent = [1 for y in range(state.height())
                  for x in range(state.width())
                  if state.pixelColor(x, y).blue()
                  > state.pixelColor(x, y).red() + 20]
        assert not accent, ("a rim was stroked", label)

    # The open state is the same fill rule (no ring): the face takes the
    # stronger wash while the popup it owns is open.
    dots.set_open(True)
    try:
        opened = _render(cap)
    finally:
        dots.set_open(False)
    assert _near(opened.pixelColor(cx, cy + 12), lifted), (
        "the open state did not use the shared fill")

    # A tool slot answers with the same language: pressed paints the wash
    # and no outline around it.
    btn = viewer.tool_shelf._buttons[other.id.value]
    assert not btn.isChecked()
    idle = _render(btn)
    btn.setDown(True)
    try:
        image = _render(btn)
    finally:
        btn.setDown(False)
    wash = hover_wash(tokens, pressed=True)
    washed = [(x, y) for y in range(image.height())
              for x in range(image.width())
              if abs(image.pixelColor(x, y).alpha() - wash.alpha()) <= 4
              and abs(image.pixelColor(x, y).red() - wash.red()) <= 4]
    assert len(washed) > 200, len(washed)
    # A filled circular highlight of the tool button's own diameter.
    disc = _bounds(washed)
    assert abs(disc.width() - CAPSULE_TOOL_D) <= 3, disc
    assert abs(disc.height() - CAPSULE_TOOL_D) <= 3, disc
    # The state adds FILL, not ink: the only blue-dominant pixels on this
    # tile are the tool's own coloured glyph, present identically at rest.
    # The removed hover rim used to add a whole accent ring here.
    assert _accent_count(image) <= _accent_count(idle) + 8, (
        "the hovered slot drew a border")


def test_local_separation_is_paint_only_and_restores(
        qtbot, cleared_settings):
    """The requested local separation moves CONTENT, never a cell.

    While the capsule is shown, the selected tool rises and the slot
    immediately after it drops — both inside their own unchanged cells.
    No other tool is affected, the 43px pitch is intact, every hit target
    stays put, and switching to a tool without a capsule restores the
    original rhythm.
    """
    viewer = _make_viewer(qtbot)
    slots = [_add_editable(viewer, ToolType.PEN, "Pen A"),
             _add_editable(viewer, ToolType.HIGHLIGHTER, "HL"),
             _add_editable(viewer, ToolType.ERASER, "Er")]
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "vertical"

    cells = {s.id.value: _slot_cell(viewer, s) for s in slots}
    viewer._shelf.select_slot(slots[0].id.value)
    _settle(viewer)

    offsets = {sid: b.content_offset for sid, b in shelf._buttons.items()}
    assert offsets[slots[0].id.value] == (0.0, -SEPARATION_ACTIVE), offsets
    assert offsets[slots[1].id.value] == (0.0, SEPARATION_NEIGHBOUR), offsets
    # The tool two cells down is untouched: only the adjacent pair moves.
    assert offsets[slots[2].id.value] == (0.0, 0.0), offsets

    # Cells and hit targets are exactly where they were: no pitch change,
    # no reserved space, no reflow.
    assert {s.id.value: _slot_cell(viewer, s) for s in slots} == cells
    tops = [cells[s.id.value].top() for s in slots]
    assert [b - a for a, b in zip(tops, tops[1:])] == [CELL + _SPACING] * 2

    # The separation is what keeps the capsule's lower contour clear of
    # the neighbour's own ink (measured with the offset applied).
    cap = _capsule(viewer)
    btn = shelf._buttons[slots[1].id.value]
    ink = _ink(btn)
    tl = btn.mapTo(shelf, QPoint(0, 0))
    ink_top = min(tl.y() + y for _x, y in ink)
    assert cap.geometry().bottom() + 1 <= ink_top, (cap.geometry(), ink_top)

    # Selecting a tool that owns no capsule restores every offset and
    # every cell smoothly (and instantly when the control is dropped).
    viewer._shelf.select_slot(_core_slot(viewer, ToolType.PAN).id.value)
    _settle(viewer)
    for button in shelf._buttons.values():
        assert button.content_offset == (0.0, 0.0), button.slot.name
    assert {s.id.value: _slot_cell(viewer, s) for s in slots} == cells


def test_focus_ring_is_keyboard_only(qtbot, cleared_settings):
    """A mouse click leaves no ring; keyboard focus keeps the affordance.

    Clicking a tool focuses it, and the specification is explicit that the
    selected and hovered states wear no outline, accent ring or circular
    stroke — so the ring is gated to keyboard-driven focus. Accessibility
    is not lost: Tab focus still shows it.
    """
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    hl = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    shelf = viewer.tool_shelf
    button = shelf._buttons[hl.id.value]
    before = _accent_count(_render(button))

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert button.hasFocus(), "the click did not focus the tile"
    assert not button.keyboard_focused, "a mouse click drew a focus ring"
    assert _accent_count(_render(button)) <= before + 8, (
        "a mouse click drew an accent ring")

    dots = _dots(viewer)
    dots.setFocus(Qt.FocusReason.MouseFocusReason)
    QApplication.processEvents()
    assert dots.hasFocus() and not dots.keyboard_focused
    assert _accent_count(_render(_capsule(viewer))) == 0, (
        "the options control drew a mouse-driven ring")

    dots.clearFocus()
    dots.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    assert dots.keyboard_focused, "keyboard focus lost its affordance"
    button.clearFocus()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    assert button.keyboard_focused, "keyboard focus lost its affordance"


def test_capsule_and_dots_clear_the_next_slot_hit_target(
        qtbot, cleared_settings):
    """The slot-local control keeps the CLICKABLE dots out of the
    neighbouring slot's interactive cell (and out of the morphing
    boundary after the last custom), while the toolbar keeps its original
    43px custom pitch — no reserved space, no reflow on selection."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    second = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "vertical"

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)
    cap = _capsule(viewer)
    dots = _dots(viewer)

    cell = _slot_cell(viewer, second)
    capsule = cap.geometry()
    # The interactive target never reaches into the neighbour's cell.
    assert not dots.geometry().intersects(cell), (dots.geometry(), cell)
    assert dots.geometry().bottom() < cell.top(), (dots.geometry(), cell)
    # Every clickable pixel of the control lives inside the selected
    # slot's own pitch (one cell plus the inter-slot gap): the original
    # 43px custom rhythm.
    selected = _slot_cell(viewer, pen)
    assert cell.top() - selected.top() == CELL + _SPACING, (cell, selected)
    assert dots.geometry().bottom() + 1 <= selected.top() + CELL + _SPACING
    # The painted options face is deliberately taller than that band, so
    # the shared contour does reach into the neighbour's empty top padding
    # — strictly as a BACKDROP: the neighbouring tile owns that cell and
    # paints its own glyph above the capsule, so its icon is never covered
    # (see test_capsule_is_one_continuous_backdrop_below_the_tiles). What
    # keeps the contact invisible is the local separation: the neighbour's
    # own content drops while the capsule is shown. Measured, not assumed.
    assert cell.top() <= capsule.bottom(), (capsule, cell)
    assert capsule.bottom() < cell.top() + CELL // 2, (capsule, cell)
    btn = shelf._buttons[second.id.value]
    assert btn.content_offset == (0.0, SEPARATION_NEIGHBOUR), (
        btn.content_offset)
    ink = _ink(btn)
    assert ink, "the neighbouring tool painted nothing to measure"
    tl = btn.mapTo(shelf, QPoint(0, 0))
    ink_top = min(tl.y() + y for _x, y in ink)
    assert capsule.bottom() + 1 <= ink_top, (capsule, ink_top)
    children = shelf.children()
    assert children.index(cap) < children.index(shelf._scroll)

    # The same guarantee for the last custom slot: the Custom|Add
    # boundary's own separator line and scroll chevron are painted by the
    # boundary itself, above the backdrop, and the backdrop stays inside
    # that boundary's own cell.
    viewer._shelf.select_slot(second.id.value)
    _settle(viewer)
    edge = shelf._edge_next
    assert edge is not None
    assert not dots.geometry().intersects(edge.geometry()), (
        dots.geometry(), edge.geometry())
    assert cap.geometry().bottom() < edge.geometry().bottom(), (
        cap.geometry(), edge.geometry())

    # The rhythm is static: selecting tools never moves a slot.
    before = {sid: b.geometry() for sid, b in shelf._buttons.items()}
    viewer._shelf.select_slot(pen.id.value)
    QApplication.processEvents()
    viewer._shelf.select_slot(_core_slot(viewer, ToolType.PAN).id.value)
    QApplication.processEvents()
    after = {sid: b.geometry() for sid, b in shelf._buttons.items()}
    assert after == before

def test_capsule_never_swallows_a_click(qtbot, cleared_settings):
    """The capsule never swallows a click: the tool circle resolves to
    the tool button, the lower circle to the dots button."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    other = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    shelf = viewer.tool_shelf
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    # The shared capsule visual is mouse-transparent by construction.
    assert cap.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    x, y, w, h = _tool_circle(viewer)
    hit_tool = shelf.childAt(QPoint(int(x + w / 2), int(y + h / 2)))
    assert hit_tool is shelf._buttons[pen.id.value], hit_tool
    hit_dots = shelf.childAt(dots.geometry().center())
    assert hit_dots is dots, hit_dots

    # The painted disc is taller than the clickable band the slot's own
    # pitch offers: that overflow is strictly visual. A click over it —
    # inside the next slot's own cell — must still reach the neighbouring
    # tool, never this slot's options button.
    overflow = QPoint(dots.geometry().center().x(),
                      dots.geometry().bottom() + 2)
    hit_over = shelf.childAt(overflow)
    assert hit_over is shelf._buttons[other.id.value], hit_over

    # Clicking the lower circle opens the SELECTED tool's properties and
    # never changes the selection.
    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    assert viewer._tool_popup is not None
    assert viewer._tool_popup_slot.id.value == pen.id.value
    assert viewer.tool_store.active_id == pen.id.value
    viewer._close_tool_popup()
    QApplication.processEvents()

    # Clicking the tool circle still selects that tool: the capsule did
    # not steal the underlying hit target.
    QTest.mouseClick(shelf._buttons[other.id.value],
                     Qt.MouseButton.LeftButton)
    qtbot.wait(30)
    assert viewer.tool_store.active_id == other.id.value
    _settle(viewer)
    assert _capsule(viewer).isVisible() and _dots(viewer).isVisible()
    # The whole control moved to the newly selected slot.
    moved = _capsule(viewer).geometry()
    ox, oy, ow, oh = _tool_circle(viewer)
    assert moved.contains(QPoint(int(ox + ow / 2), int(oy + oh / 2)))


def test_selection_never_shows_wrong_position_capsule(
        qtbot, cleared_settings):
    """Selecting an eligible tool must place the capsule at its final
    target directly: no interim position, no stale slot, no flash."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    shelf = viewer.tool_shelf
    cap = _capsule(viewer)
    dots = _dots(viewer)
    assert not cap.isVisible()

    events = []

    class _Spy(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.Move, QEvent.Type.Show):
                events.append((obj, event.type().name,
                               obj.pos().x(), obj.pos().y(),
                               obj.isVisible()))
            return False

    spy = _Spy()
    cap.installEventFilter(spy)
    dots.installEventFilter(spy)

    viewer._shelf.select_slot(pen.id.value)
    QApplication.processEvents()

    assert cap.isVisible() and dots.isVisible()
    final = {cap: (cap.pos().x(), cap.pos().y()),
             dots: (dots.pos().x(), dots.pos().y())}
    shows = [e for e in events if e[1] == "Show"]
    assert shows, events
    for obj, kind, px, py, visible in events:
        if kind in ("Move", "Show"):
            # Positioned at its final target BEFORE being shown, and
            # never moved to any other visible location.
            assert (px, py) == final[obj], (kind, px, py, final[obj])
        if kind == "Move":
            assert not visible, ("transport while visible", px, py)
    cap.removeEventFilter(spy)
    dots.removeEventFilter(spy)


def test_rapid_switching_leaves_one_capsule_at_current_slot(
        qtbot, cleared_settings):
    """Rapid tool changes never leave duplicates or a stale capsule."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    highlighter = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    shelf = viewer.tool_shelf
    cap = _capsule(viewer)
    dots = _dots(viewer)

    for _ in range(8):
        viewer._shelf.select_slot(pen.id.value)
        viewer._shelf.select_slot(highlighter.id.value)
    qtbot.wait(220)

    assert len(shelf.findChildren(ToolCapsule)) == 1
    assert len(shelf.findChildren(CapsuleOptionsButton)) == 1
    assert cap.isVisible() and dots.isVisible()
    assert viewer.tool_store.active_id == highlighter.id.value

    x, y, w, h = _tool_circle(viewer)
    assert cap.geometry().contains(QPoint(int(x + w / 2), int(y + h / 2)))
    assert shelf.rect().contains(cap.geometry())


def test_opening_and_closing_properties_does_not_move_capsule(
        qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)
    cap = _capsule(viewer)
    dots = _dots(viewer)
    before = (cap.geometry(), dots.geometry())

    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(80)
    assert viewer._tool_popup is not None
    assert (cap.geometry(), dots.geometry()) == before

    # A live property edit keeps the popup open and the capsule put.
    form = viewer._tool_popup._content
    form._color_section._emit("#00ff00")
    qtbot.wait(40)
    assert viewer._tool_popup is not None
    assert (cap.geometry(), dots.geometry()) == before

    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(80)
    assert viewer._tool_popup is None
    assert (cap.geometry(), dots.geometry()) == before


def test_capsule_absent_for_selection_and_pan(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    cap = _capsule(viewer)
    dots = _dots(viewer)
    pan = _core_slot(viewer, ToolType.PAN)
    selection = _core_slot(viewer, ToolType.SELECTION)

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)
    assert cap.isVisible() and dots.isVisible()

    viewer._shelf.select_slot(pan.id.value)
    assert not cap.isVisible() and not dots.isVisible()
    qtbot.wait(220)
    assert not cap.isVisible() and not dots.isVisible()

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)
    assert cap.isVisible() and dots.isVisible()

    viewer._shelf.select_slot(selection.id.value)
    assert not cap.isVisible() and not dots.isVisible()
    qtbot.wait(220)
    assert not cap.isVisible() and not dots.isVisible()

    # The exclusion never removes the core tools from the toolbar.
    kinds = [s.kind for s in viewer.tool_store.slots]
    assert ToolType.PAN in kinds and ToolType.SELECTION in kinds


def test_ineligible_selection_never_exposes_the_capsule(
        qtbot, cleared_settings):
    """Switching to Selection/Pan must not show the dots for even one
    frame: not the previous tool's capsule, and never a stale entrance
    animation that is then hidden again (no timers involved)."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    pan = _core_slot(viewer, ToolType.PAN)
    selection = _core_slot(viewer, ToolType.SELECTION)
    cap = _capsule(viewer)
    dots = _dots(viewer)

    # (a) From a settled eligible selection: gone in the same frame.
    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)
    assert cap.isVisible() and dots.isVisible()
    viewer._shelf.select_slot(pan.id.value)
    assert not cap.isVisible() and not dots.isVisible()
    QApplication.processEvents()
    assert not cap.isVisible() and not dots.isVisible()
    qtbot.wait(200)
    assert not cap.isVisible() and not dots.isVisible()

    # (b) From a capsule that is still animating in: superseded, never
    # finished into view and then hidden.
    viewer._shelf.select_slot(pen.id.value)
    assert cap.isVisible()  # exposed immediately; fade-in still running
    viewer._shelf.select_slot(selection.id.value)
    assert not cap.isVisible() and not dots.isVisible()
    qtbot.wait(200)
    assert not cap.isVisible() and not dots.isVisible()

    # (c) From hidden, an ineligible selection shows nothing at all.
    shows: list[object] = []

    class _Spy(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Show:
                shows.append(obj)
            return False

    spy = _Spy()
    for widget in (cap, dots):
        widget.installEventFilter(spy)
    try:
        assert viewer.tool_store.active_id == selection.id.value
        viewer._shelf.select_slot(pan.id.value)
        viewer._shelf.select_slot(selection.id.value)
        QApplication.processEvents()
        assert not shows, shows
        assert not cap.isVisible() and not dots.isVisible()
    finally:
        for widget in (cap, dots):
            widget.removeEventFilter(spy)

    # (d) Rapid eligible <-> ineligible switching settles hidden.
    for _ in range(6):
        viewer._shelf.select_slot(pen.id.value)
        viewer._shelf.select_slot(pan.id.value)
    QApplication.processEvents()
    qtbot.wait(200)
    assert viewer.tool_store.active_id == pan.id.value
    assert not cap.isVisible() and not dots.isVisible()


def test_horizontal_capsule_wraps_tool_and_dots(qtbot, cleared_settings):
    """Horizontal dock: same integrated form (dots after the tool along
    the axis — the 50px cross thickness has no room beneath), still
    entirely inside the shelf's visible bounds."""
    viewer = _make_viewer(qtbot)
    viewer.tool_shelf.set_dock("top")
    QApplication.processEvents()
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    shelf = viewer.tool_shelf
    assert shelf._orientation() == "horizontal"

    viewer._shelf.select_slot(pen.id.value)
    _settle(viewer)

    cap = _capsule(viewer)
    dots = _dots(viewer)
    assert cap.isVisible() and dots.isVisible()
    capsule = cap.geometry()
    assert shelf.rect().contains(capsule), (capsule, shelf.rect())
    tool = QRect(*[int(round(v)) for v in _tool_circle(viewer)])
    assert capsule.contains(tool), (capsule, tool)
    assert capsule.contains(dots.geometry()), (capsule, dots.geometry())


def test_capsule_click_toggles_its_properties_popup(
        qtbot, cleared_settings):
    """First click on the lower circle opens the slot's popup; a second
    closes that same popup; repeated clicks never duplicate popups."""
    from app.toolshelf.tool_popup import ToolPopup

    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    qtbot.wait(220)
    dots = _dots(viewer)
    assert dots.isVisible()
    active = viewer.tool_store.active_id

    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    assert viewer._tool_popup is not None
    assert viewer._tool_popup_slot.id.value == pen.id.value
    # Opening properties never changes selection or slot values.
    assert viewer.tool_store.active_id == active

    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    assert viewer._tool_popup is None

    # Toggle back open and confirm a single visible popup instance.
    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    visible = [p for p in viewer.findChildren(ToolPopup) if p.isVisible()]
    assert viewer._tool_popup is not None and len(visible) == 1
    assert viewer._tool_popup_slot.id.value == pen.id.value


def test_removing_slot_with_open_popup_cleans_up(qtbot, cleared_settings):
    """Removing the selected slot while its popup is open leaves no
    popup or capsule bound to the deleted instance."""
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    viewer._shelf.select_slot(pen.id.value)
    qtbot.wait(220)
    cap = _capsule(viewer)
    dots = _dots(viewer)
    assert cap.isVisible() and dots.isVisible()

    QTest.mouseClick(dots, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    assert viewer._tool_popup is not None

    viewer._on_tool_remove_requested(pen)
    qtbot.wait(220)

    assert viewer.tool_store.find(pen.id.value) is None
    assert viewer._tool_popup is None
    assert viewer._tool_popup_slot is None
    # Fallback is a core tool (no editable properties): capsule hidden.
    assert not cap.isVisible() and not dots.isVisible()
    assert _capsule(viewer) is cap  # same single instance, no orphan
    assert _dots(viewer) is dots


# 8 ------------------------------------------------------------------------

def test_trash_removes_correct_instance_and_persists(
        qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    duplicate = viewer.tool_store.duplicate_slot(pen.id.value)
    viewer._refresh_shelf()
    viewer._shelf.select_slot(pen.id.value)
    popup, form = _open_properties(viewer, duplicate)

    QTest.mouseClick(form._trash, Qt.MouseButton.LeftButton)
    qtbot.wait(80)

    assert viewer.tool_store.find(duplicate.id.value) is None
    assert viewer.tool_store.find(pen.id.value) is not None
    assert viewer.tool_store.active_id == pen.id.value
    assert viewer._tool_popup is None
    restored = ToolShelfStore.restore()
    assert [s.id.value for s in restored.slots] == [
        s.id.value for s in viewer.tool_store.slots]


# 9 ------------------------------------------------------------------------

def test_removing_active_slot_falls_back_without_stale_reference(
        qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = _add_editable(viewer, ToolType.PEN, "Pen A")
    highlighter = _add_editable(viewer, ToolType.HIGHLIGHTER, "HL")
    viewer._shelf.select_slot(highlighter.id.value)
    popup, form = _open_properties(viewer, highlighter)

    QTest.mouseClick(form._trash, Qt.MouseButton.LeftButton)
    # The fallback is a core tool (no editable properties), so the old
    # capsule is dropped in the same frame — no lingering exit fade.
    qtbot.wait(120)

    assert viewer.tool_store.find(highlighter.id.value) is None
    assert viewer.tool_store.find(pen.id.value) is not None
    active = viewer.tool_store.active()
    assert active is not None
    assert viewer.tool_store.find(active.id.value) is active
    assert viewer._interaction.active_kind == active.kind
    assert viewer._tool_popup is None
    # The fallback (core Pan) has no editable properties: no stale capsule.
    assert viewer.tool_shelf.capsule.isVisible() is False
    assert viewer.tool_shelf.dots.isVisible() is False
