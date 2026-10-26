"""M5-A1 final bug-fix pass: regression tests for the six reported defects.

D1: hover controls must survive enter/leave without exceptions
    (previously _AddCell.leaveEvent raised NameError: 'tokens').
D2: shape previews render the actual shape, centered and unclipped.
D3: toolbar icons are optically centered inside real 40px cells.
D4: scroll-to-start chevron is fully visible at every reveal value.
D5: overflow boundaries morph line<->arrow from live scrollbar state.
D6: custom color picker opens/commits/collapses with animation.
"""

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import fx
from app.theme.tokens import dark_tokens, light_tokens
from app.toolshelf.store import ToolType
from tests.qt_helpers import wait_edge_settled, wait_stable, wait_until

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


# -- D1: leaveEvent must not raise --------------------------------------

def _cycle_hover(widget, qtbot):
    """Drive real Qt enter/leave delivery; raises if an override throws."""
    widget.show()
    QTest.qWait(10)
    QTest.mouseMove(widget, widget.rect().center())
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
    QApplication.processEvents()


def test_addcell_leave_event_has_no_undefined_names(qtbot):
    """Regression: _AddCell.leaveEvent raised NameError: 'tokens'."""
    import ast
    import pathlib

    from app.toolshelf.tool_popup import _AddCell

    cell = _AddCell(ToolType.PEN, "Pen", dark_tokens())
    qtbot.addWidget(cell)
    tokens_before = cell._tokens
    # Direct call: an undefined name propagates synchronously.
    # (enterEvent takes QEnterEvent; leaveEvent takes a plain QEvent.)
    from PySide6.QtGui import QEnterEvent

    enter = QEnterEvent(cell.rect().center(), cell.rect().center(),
                        cell.mapToGlobal(cell.rect().center()))
    cell.enterEvent(enter)
    cell.leaveEvent(QEvent(QEvent.Type.Leave))
    assert cell._tokens is tokens_before
    # Source-level guard: the override must not load a bare `tokens` name.
    text = pathlib.Path("app/toolshelf/tool_popup.py").read_text(
        encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef) and node.name == "leaveEvent"
                and node.lineno >= 2690 and node.lineno <= 2715):
            params = [a.arg for a in node.args.args]
            assigned = {ch.id for ch in ast.walk(node)
                        if isinstance(ch, ast.Name)
                        and isinstance(ch.ctx, ast.Store)}
            bad = [ch.lineno for ch in ast.walk(node)
                   if isinstance(ch, ast.Name)
                   and isinstance(ch.ctx, ast.Load) and ch.id == "tokens"
                   and "tokens" not in params and "tokens" not in assigned]
            assert bad == [], f"bare 'tokens' loads at {bad}"


def test_hover_controls_enter_leave_stable(qtbot, cleared_settings):
    """Swatches, visual options, library cards, slot buttons: enter/leave
    cycles raise nothing, keep theme, and leave property editing working."""
    from app.toolshelf.slot_button import SlotButton
    from app.toolshelf.tool_popup import (
        _AddCell,
        _SwatchButton,
        _VisualOption,
    )
    from tools.models import ToolInstance, ToolType

    tokens = dark_tokens()
    swatch = _SwatchButton("#e5484d", tokens)
    qtbot.addWidget(swatch)
    option = _VisualOption("solid", lambda p, r, t: None, tokens, 56, 36)
    qtbot.addWidget(option)
    card = _AddCell(ToolType.RECTANGLE, "Rectangle", tokens)
    qtbot.addWidget(card)
    slot = ToolInstance(kind=ToolType.PEN, name="Pen #1")
    button = SlotButton(slot, tokens)
    qtbot.addWidget(button)
    for widget in (swatch, option, card, button):
        assert widget._tokens is not None
    for _ in range(5):
        for widget in (swatch, option, card, button):
            _cycle_hover(widget, qtbot)
    for widget in (swatch, option, card, button):
        assert widget._tokens is not None
        assert widget.isVisible()
    # Property editing still works after the hover cycles.
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#ff0000")
    from app.toolshelf.tool_popup import PropertiesForm

    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form._color_section._emit("#00ff00")
    assert form._slot.color.lower() == "#00ff00"


def test_no_bare_tokens_in_event_overrides(qtbot):
    """Repo-wide guard: no enter/leave/hover override loads an undefined
    `tokens` name (the _AddCell NameError pattern)."""
    import ast
    import pathlib

    offenders = []
    for path in list(pathlib.Path("app").rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.name in (
                    "enterEvent", "leaveEvent", "hoverEnterEvent",
                    "hoverLeaveEvent")):
                params = [a.arg for a in node.args.args]
                assigned = {ch.id for ch in ast.walk(node)
                            if isinstance(ch, ast.Name)
                            and isinstance(ch.ctx, ast.Store)}
                bad = [ch.lineno for ch in ast.walk(node)
                       if isinstance(ch, ast.Name)
                       and isinstance(ch.ctx, ast.Load)
                       and ch.id == "tokens"
                       and "tokens" not in params
                       and "tokens" not in assigned]
                if bad:
                    offenders.append(f"{path}:{node.lineno} lines {bad}")
    assert offenders == []


# -- D6: custom color picker animations ----------------------------------

def _picker_popup(qtbot):
    """Real ToolPopup hosting a pen inspector, anchored to a launcher."""
    from PySide6.QtCore import QPoint, QRect

    from app.toolshelf.tool_popup import PropertiesForm, ToolPopup
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.wait(100)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1",
                                     color="#ff0000")
    shelf = viewer.tool_shelf
    launcher = next(iter(shelf._buttons.values()))
    form = PropertiesForm(pen, dark_tokens(), viewer)
    popup = ToolPopup(dark_tokens(), viewer)
    popup.set_content(form)
    popup.show_near(QRect(launcher.mapToGlobal(QPoint(0, 0)),
                          launcher.size()), viewer, animate=False)
    qtbot.addWidget(popup)
    qtbot.wait(100)
    return viewer, popup, form


def _popup_inside(host, popup):
    pg = popup.geometry()
    return (pg.left() >= 0 and pg.top() >= 0
            and pg.right() <= host.width() and pg.bottom() <= host.height())


def test_picker_open_animates_then_settles(qtbot, cleared_settings):
    """Opening tweens (mid-flight height is partial, not a snap) and
    settles with cleared constraints; popup stays anchored in-window."""
    from app import fx

    fx.set_motion_mode("full")
    viewer, popup, form = _picker_popup(qtbot)
    section = form._color_section
    wrap = section._picker_wrap
    x_before = popup.geometry().left()
    section._on_palette_add_tile()
    assert section._picker_open is True
    qtbot.wait(60)
    mid = wrap.height()
    assert wrap.isVisible()
    assert 0 < mid < 222, f"picker snapped instead of tweening: h={mid}"
    assert wait_until(qtbot, lambda: wrap.isVisible() and
                      wrap.minimumHeight() == 0
                      and wrap.maximumHeight() == 16777215)
    assert popup.geometry().left() == x_before
    assert _popup_inside(viewer, popup)


def test_picker_commit_adds_collapses_anchored(qtbot, cleared_settings):
    """Commit applies live, adds the preset, collapses animated, and
    the popup stays anchored inside the window."""
    from app import fx

    fx.set_motion_mode("full")
    viewer, popup, form = _picker_popup(qtbot)
    section = form._color_section
    wrap = section._picker_wrap
    x_before = popup.geometry().left()
    section._on_palette_add_tile()
    assert wait_until(qtbot, lambda: wrap.isVisible() and
                      wrap.minimumHeight() == 0
                      and wrap.maximumHeight() == 16777215)
    section._emit("#123456")
    n_before = len(section._presets)
    section._on_palette_add_tile()
    assert wait_until(qtbot, lambda: not wrap.isVisible() and
                      wrap.minimumHeight() == 0
                      and wrap.maximumHeight() == 16777215)
    assert len(section._presets) == n_before + 1
    assert "#123456" in [p.lower() for p in section._presets]
    assert form._slot.color.lower() == "#123456"
    assert section._picker_open is False
    assert not wrap.isVisible()
    assert wrap.minimumHeight() == 0
    assert wrap.maximumHeight() == 16777215
    assert popup.geometry().left() == x_before
    assert _popup_inside(viewer, popup)


def test_picker_repeated_toggle_stable(qtbot, cleared_settings):
    """Rapid open/close cycles leave one wrap, sane constraints, and a
    valid anchored popup (no stale heights/widgets/anchors)."""
    from PySide6.QtWidgets import QWidget as _QW

    from app import fx

    fx.set_motion_mode("full")
    viewer, popup, form = _picker_popup(qtbot)
    section = form._color_section
    wrap = section._picker_wrap
    for _ in range(3):
        section._on_palette_add_tile()
        qtbot.wait(40)
        section._on_palette_add_tile()
        qtbot.wait(40)
    assert wait_until(qtbot, lambda: wrap.minimumHeight() == 0
                      and wrap.maximumHeight() == 16777215)
    assert len([c for c in section.findChildren(_QW) if c is wrap]) == 1
    assert wrap.minimumHeight() == 0
    assert wrap.maximumHeight() == 16777215
    assert _popup_inside(viewer, popup)


def test_picker_reduced_motion_synchronous(qtbot, cleared_settings):
    """Reduced motion completes open/collapse synchronously."""
    from app import fx

    fx.set_motion_mode("reduced")
    _viewer, _popup, form = _picker_popup(qtbot)
    section = form._color_section
    wrap = section._picker_wrap
    section._on_palette_add_tile()
    qtbot.wait(50)
    assert section._picker_open is True
    assert wrap.isVisible()
    assert wrap.maximumHeight() == 16777215
    section._emit("#654321")
    section._on_palette_add_tile()
    qtbot.wait(50)
    assert section._picker_open is False
    assert not wrap.isVisible()
    assert "#654321" in [p.lower() for p in section._presets]


def test_picker_popup_width_stays_compact(qtbot, cleared_settings):
    """Inspector popup holds ~340-380px with the picker open (the hex
    edit's default hint must not inflate it)."""
    from app import fx

    fx.set_motion_mode("full")
    _viewer, popup, form = _picker_popup(qtbot)
    section = form._color_section
    wrap = section._picker_wrap
    section._on_palette_add_tile()
    assert wait_until(qtbot, lambda: wrap.isVisible() and
                      wrap.minimumHeight() == 0
                      and wrap.maximumHeight() == 16777215)
    assert 340 <= popup.geometry().width() <= 380, (
        popup.geometry().getRect())


# -- D5: boundaries morph back from live scroll state ----------------------

def _overflow_viewer(qtbot, customs=14):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    for i in range(customs):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"C{i}")
    viewer.tool_shelf.refresh()
    qtbot.addWidget(viewer)
    shelf = viewer.tool_shelf
    assert wait_until(qtbot, lambda: shelf._edge_next._reveal_target is None
                      and shelf._edge_prev._reveal_target is None)
    return viewer


def _along_bar(shelf):
    if shelf._orientation() == "horizontal":
        return shelf._scroll.horizontalScrollBar()
    return shelf._scroll.verticalScrollBar()


def _click_to_end(qtbot, edge, bar, want_max=True):
    """Drive the shelf with real animated page-steps until the scroll
    settles at the wanted end (a single step need not cover max)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    target = bar.maximum() if want_max else bar.minimum()
    shelf = edge.parentWidget()
    for _ in range(8):
        if bar.value() == target:
            break
        QTest.mouseClick(edge, Qt.MouseButton.LeftButton)
        assert wait_stable(qtbot, bar.value)
    assert wait_until(qtbot, lambda: bar.value() == target
                      and shelf._edge_prev._reveal_target is None
                      and shelf._edge_next._reveal_target is None)
    assert bar.value() == target


def _edge_state(shelf):
    bar = _along_bar(shelf)
    return (bar.value(), bar.minimum(), bar.maximum(),
            shelf._edge_prev._has_more, shelf._edge_prev._reveal,
            shelf._edge_next._has_more, shelf._edge_next._reveal)


def test_boundary_full_cycle_animated(qtbot, cleared_settings):
    """Complete rendered cycle per orientation: plain line -> overflow ->
    arrow morphs in -> animated scroll -> end reached -> arrow morphs
    out -> plain line. Both boundaries, both directions."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    for dock in ("left", "top"):
        viewer = _overflow_viewer(qtbot)
        shelf = viewer.tool_shelf
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        bar = _along_bar(shelf)
        assert bar.maximum() > 0, f"{dock}: no overflow staged"
        # Start: prev is a line, next is an arrow.
        assert shelf._edge_prev._has_more is False
        assert shelf._edge_next._has_more is True
        assert wait_edge_settled(qtbot, shelf._edge_prev, 0.0)
        assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
        # Animated trip to the end (real page-step clicks, not setValue).
        _click_to_end(qtbot, shelf._edge_next, bar, want_max=True)
        assert bar.value() == bar.maximum()
        assert shelf._edge_prev._has_more is True
        assert shelf._edge_next._has_more is False
        assert shelf._edge_prev._reveal == 1.0
        assert shelf._edge_next._reveal == 0.0
        # Animated trip back: mirror terminal.
        _click_to_end(qtbot, shelf._edge_prev, bar, want_max=False)
        assert bar.value() == bar.minimum()
        assert shelf._edge_prev._has_more is False
        assert shelf._edge_next._has_more is True
        assert shelf._edge_prev._reveal == 0.0
        assert shelf._edge_next._reveal == 1.0


def test_boundary_does_not_restart_reveal_per_tick(qtbot, cleared_settings):
    """One animated scroll must flip each edge with a single glide —
    never a restart per scrollbar tick (the mid-morph stranding)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    viewer = _overflow_viewer(qtbot)
    shelf = viewer.tool_shelf
    counts = {"prev": 0, "next": 0}
    orig_prev, orig_next = (shelf._edge_prev._glide_to,
                            shelf._edge_next._glide_to)

    def wrap(which, orig):
        def inner(target):
            counts[which] += 1
            return orig(target)
        return inner

    shelf._edge_prev._glide_to = wrap("prev", orig_prev)
    shelf._edge_next._glide_to = wrap("next", orig_next)
    QTest.mouseClick(shelf._edge_next, Qt.MouseButton.LeftButton)
    # Settle condition mirrors the assertion: exactly one glide per edge,
    # both landed (the step can reach the far end, so no reveal value is
    # presumed — only rest + the glide counts).
    assert wait_until(qtbot, lambda: counts == {"prev": 1, "next": 1}
                      and shelf._edge_prev._reveal_target is None
                      and shelf._edge_next._reveal_target is None)
    assert counts == {"prev": 1, "next": 1}, counts


def test_boundary_interrupted_glide_settles(qtbot, cleared_settings):
    """Clicking back mid-glide lands on exact start state, settled."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    viewer = _overflow_viewer(qtbot)
    shelf = viewer.tool_shelf
    bar = _along_bar(shelf)
    QTest.mouseClick(shelf._edge_next, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    QTest.mouseClick(shelf._edge_prev, Qt.MouseButton.LeftButton)
    assert wait_stable(qtbot, bar.value)
    assert wait_edge_settled(qtbot, shelf._edge_prev, 0.0)
    assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
    assert bar.value() == bar.minimum()
    assert shelf._edge_prev._has_more is False
    assert shelf._edge_next._has_more is True
    assert shelf._edge_prev._reveal == 0.0
    assert shelf._edge_next._reveal == 1.0


def test_boundary_resize_mid_glide_stays_live(qtbot, cleared_settings):
    """Resizing mid-glide keeps states live-consistent and settled,
    and the trip can still finish at the new end."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    viewer = _overflow_viewer(qtbot)
    shelf = viewer.tool_shelf
    QTest.mouseClick(shelf._edge_next, Qt.MouseButton.LeftButton)
    qtbot.wait(60)
    viewer.resize(900, 700)
    bar = _along_bar(shelf)
    assert wait_stable(qtbot, lambda: (viewer.width(), viewer.height()))
    assert wait_stable(qtbot, bar.value)
    assert wait_edge_settled(qtbot, shelf._edge_prev,
                             1.0 if shelf._edge_prev.has_more() else 0.0)
    assert wait_edge_settled(qtbot, shelf._edge_next,
                             1.0 if shelf._edge_next.has_more() else 0.0)
    value, minimum, maximum, p_has, p_rev, n_has, n_rev = _edge_state(shelf)
    assert p_has == (value > minimum)
    assert n_has == (value < maximum)
    assert p_rev in (0.0, 1.0) and n_rev in (0.0, 1.0)
    assert p_rev == (1.0 if p_has else 0.0)
    assert n_rev == (1.0 if n_has else 0.0)
    _click_to_end(qtbot, shelf._edge_next, _along_bar(shelf), want_max=True)
    value, minimum, maximum, p_has, p_rev, n_has, n_rev = _edge_state(shelf)
    assert value == maximum
    assert (p_has, n_has) == (True, False)
    assert (p_rev, n_rev) == (1.0, 0.0)


def test_boundary_cycle_all_docks(qtbot, cleared_settings):
    """Animated end-trip terminates correctly on all four docks."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    for dock in ("left", "right", "top", "bottom"):
        viewer = _overflow_viewer(qtbot)
        shelf = viewer.tool_shelf
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        bar = _along_bar(shelf)
        assert bar.maximum() > 0, f"{dock}: no overflow staged"
        _click_to_end(qtbot, shelf._edge_next, bar, want_max=True)
        assert bar.value() == bar.maximum(), dock
        assert shelf._edge_next._has_more is False, dock
        assert shelf._edge_next._reveal == 0.0, dock
        assert shelf._edge_prev._has_more is True, dock
        assert shelf._edge_prev._reveal == 1.0, dock


def test_exactly_two_boundaries_no_third_widget(qtbot, cleared_settings):
    """The shelf carries exactly two morphing boundaries — no static
    divider, no permanent arrow widget — with customs inside the
    scroll viewport."""
    from app.toolshelf.shelf import _OverflowEdge
    from app.toolshelf.slot_button import SlotButton

    viewer = _overflow_viewer(qtbot)
    shelf = viewer.tool_shelf
    edges = shelf.findChildren(_OverflowEdge)
    assert len(edges) == 2
    assert shelf._edge_prev in edges and shelf._edge_next in edges
    assert not hasattr(shelf, "_divider") and not hasattr(shelf, "_divider2")
    assert "DividerCell" not in type(shelf).__name__
    try:
        from app.toolshelf import shelf as shelf_mod

        assert not hasattr(shelf_mod, "_DividerCell")
    except ImportError:
        pass
    customs = [b for b in shelf.findChildren(SlotButton)
               if b.parentWidget() is not shelf]
    assert len(customs) == 14


# -- D4: scroll-to-start arrow fully visible ------------------------------

def test_overflow_chevron_never_touches_widget_edge(qtbot):
    """Single morphing path: insideness at every reveal, and the tip
    provably travels (bending geometry, not a fading overlay).

    Regression history: the old s = 3 + 4*reveal chevron reached
    half-extent 7 + 1px pen on a 7px budget (tip cut at reveal >= 0.75);
    M5-A2.1 replaced line-plus-overlay with one interpolated path whose
    tip travels 0 -> 5px (5 + 1px pen = 6 <= 7px budget).
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtWidgets import QWidget

    from app.toolshelf.shelf import _OverflowEdge

    for horizontal in (True, False):
        W, H = (14, 40) if horizontal else (40, 14)
        for forward in (False, True):
            edge = _OverflowEdge(forward, horizontal, dark_tokens())
            qtbot.addWidget(edge)
            edge.resize(W, H)
            tips = []
            for reveal in (0.0, 0.25, 0.5, 0.75, 1.0):
                edge._reveal = reveal
                img = QImage(W, H, QImage.Format.Format_ARGB32)
                img.fill(QColor(0, 0, 0, 0))
                edge.render(img, QPoint(0, 0), edge.rect(),
                            QWidget.RenderFlag.DrawChildren)
                x0, y0, x1, y1, n = W, H, -1, -1, 0
                for y in range(H):
                    for x in range(W):
                        if img.pixelColor(x, y).alpha() > 8:
                            n += 1
                            x0, x1 = min(x0, x), max(x1, x)
                            y0, y1 = min(y0, y), max(y1, y)
                assert n > 0, "boundary paints nothing"
                assert x0 > 0 and x1 < W - 1 and y0 > 0 and y1 < H - 1, (
                    f"morph touches edge h={horizontal} fwd={forward} "
                    f"r={reveal}: x={x0}..{x1} y={y0}..{y1}")
                # Tip = extreme ink along the boundary axis.
                if horizontal:
                    tips.append(x1 if forward else x0)
                else:
                    tips.append(y1 if forward else y0)
            # The tip travels monotonically into the arrow (>= 3px over
            # the full morph); a fade/slide overlay would not move ink.
            if forward:
                assert all(b >= a for a, b in zip(tips, tips[1:])), tips
                assert tips[-1] - tips[0] >= 3, tips
            else:
                assert all(b <= a for a, b in zip(tips, tips[1:])), tips
                assert tips[0] - tips[-1] >= 3, tips


# -- D3: optical centering in actual buttons -------------------------------

def test_slot_glyphs_centered_on_true_cell_center(qtbot):
    """Every toolbar glyph's ink bbox sits on the true 40px cell center.

    Regression: QRect.center() truncates on even-sized rects (34px at
    x=3 reports 19, true 20.0), parking all glyphs 1px up-left of the
    circular halo. Anchors must use float centers; threshold ±0.75px
    admits only odd-span rasterization, never truncation.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtWidgets import QWidget

    from app.toolshelf.shelf import _AddCell as ShelfAdd, _HandleCell
    from app.toolshelf.slot_button import SlotButton
    from tools.models import ToolInstance

    shape_colors = {
        ToolType.LINE: "#f76b15", ToolType.RECTANGLE: "#e5484d",
        ToolType.ELLIPSE: "#2b6cff", ToolType.ARROW: "#8e4ec6",
        ToolType.POLYGON: "#30d158",
    }
    kinds = [ToolType.PAN, ToolType.SELECTION, ToolType.PEN,
             ToolType.HIGHLIGHTER, ToolType.ERASER, ToolType.LINE,
             ToolType.ARROW, ToolType.RECTANGLE, ToolType.ELLIPSE,
             ToolType.POLYGON, ToolType.LASSO, ToolType.NOTE]
    for tokens in (dark_tokens(), light_tokens()):
        for kind in kinds:
            color = shape_colors.get(kind, "#e5484d")
            if kind == ToolType.HIGHLIGHTER:
                color = "#ffd60a"
            slot = ToolInstance(kind=kind, name=kind.value, color=color,
                                width_mm=0.9, opacity=0.9)
            btn = SlotButton(slot, tokens)
            qtbot.addWidget(btn)
            btn.resize(40, 40)
            img = QImage(40, 40, QImage.Format.Format_ARGB32)
            img.fill(QColor(0, 0, 0, 0))
            btn.render(img, QPoint(0, 0), btn.rect(),
                       QWidget.RenderFlag.DrawChildren)
            x0, y0, x1, y1, n = 40, 40, -1, -1, 0
            for y in range(40):
                for x in range(40):
                    if img.pixelColor(x, y).alpha() > 8:
                        n += 1
                        x0, x1 = min(x0, x), max(x1, x)
                        y0, y1 = min(y0, y), max(y1, y)
            assert n > 0, f"{kind} painted nothing"
            mid_x, mid_y = (x0 + x1 + 1) / 2.0, (y0 + y1 + 1) / 2.0
            assert abs(mid_x - 20.0) <= 0.75, (
                f"{kind} x-shifted: {mid_x}")
            if kind == ToolType.HIGHLIGHTER:
                # Two-part composition (M5-A1.1 chisel marker): a centered
                # marker glyph plus an intentional low stroke-wash strip,
                # so the whole-cell ink bbox sits slightly below center.
                # The truncation regression is covered by the glyph anchor
                # itself (float-centered, asserted below).
                assert 19.5 <= mid_y <= 21.5, (
                    f"{kind} composition drifted: {mid_y}")
                from app.icons import ink_center_offset as _ico
                glyph = btn._glyph("marker", btn._glyph_color(slot), 22,
                                   btn._shelf_bg())
                dx, dy = _ico(glyph)
                assert abs(dx) <= 0.75 and abs(dy) <= 0.75, (
                    f"marker glyph off-center: {(dx, dy)}")
            else:
                assert abs(mid_y - 20.0) <= 0.75, (
                    f"{kind} y-shifted: {mid_y}")
        for label, make in (("add", lambda: ShelfAdd(tokens)),
                            ("move", lambda: _HandleCell(tokens))):
            cell = make()
            qtbot.addWidget(cell)
            cell.resize(40, 40)
            img = QImage(40, 40, QImage.Format.Format_ARGB32)
            img.fill(QColor(0, 0, 0, 0))
            cell.render(img, QPoint(0, 0), cell.rect(),
                        QWidget.RenderFlag.DrawChildren)
            x0, y0, x1, y1, n = 40, 40, -1, -1, 0
            for y in range(40):
                for x in range(40):
                    if img.pixelColor(x, y).alpha() > 8:
                        n += 1
                        x0, x1 = min(x0, x), max(x1, x)
                        y0, y1 = min(y0, y), max(y1, y)
            assert n > 0, f"{label} painted nothing"
            mid_x, mid_y = (x0 + x1 + 1) / 2.0, (y0 + y1 + 1) / 2.0
            assert abs(mid_x - 20.0) <= 0.75, f"{label} x-shifted"
            assert abs(mid_y - 20.0) <= 0.75, f"{label} y-shifted"


# -- D2: shape previews -------------------------------------------------

def _render_preview_ink(preview, width=320, height=36):
    """Render a _Preview to transparency; return (image, bounds dict)."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtWidgets import QWidget

    preview.resize(width, height)
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    preview.render(img, QPoint(0, 0), preview.rect(),
                   QWidget.RenderFlag.DrawChildren)
    top, bot, left, right, n = None, None, None, None, 0
    for y in range(height):
        for x in range(width):
            if img.pixelColor(x, y).alpha() > 0:
                n += 1
                top = y if top is None else min(top, y)
                bot = y if bot is None else max(bot, y)
                left = x if left is None else min(left, x)
                right = x if right is None else max(right, x)
    return img, {"n": n, "top": top, "bottom": bot,
                 "left": left, "right": right}


def _shape_slot(kind, **kw):
    from tools.models import ToolInstance

    base = dict(name="X", color="#e5484d", width_mm=1.0, opacity=0.9)
    base.update(kw)
    return ToolInstance(kind=kind, **base)


def test_shape_previews_render_centered_unclipped(qtbot):
    """Rectangle/Ellipse/Polygon/Line/Arrow previews: real presence
    (not slivers), centered, with clip-free margins."""
    from app.toolshelf.tool_popup import _Preview

    cases = [
        (ToolType.RECTANGLE, dict(fill_enabled=True, fill_color="#e5484d",
                                  fill_opacity=0.3)),
        (ToolType.ELLIPSE, {}),
        (ToolType.POLYGON, {}),
        (ToolType.LINE, dict(line_style="dashed", start_ending="dot",
                             end_ending="arrow")),
        (ToolType.ARROW, dict(start_ending="none", end_ending="arrow")),
        (ToolType.RECTANGLE, dict(name="thick", width_mm=4.0)),
    ]
    for kind, kw in cases:
        pv = _Preview()
        qtbot.addWidget(pv)
        pv.set_slot(_shape_slot(kind, **kw))
        _img, b = _render_preview_ink(pv)
        assert b["n"] > 100, f"{kind} preview nearly empty: {b}"
        # Unclipped: ink keeps a margin from every preview edge.
        assert b["top"] >= 4, f"{kind} clips top: {b}"
        assert b["bottom"] <= 31, f"{kind} clips bottom: {b}"
        assert b["left"] >= 4, f"{kind} clips left: {b}"
        assert b["right"] <= 315, f"{kind} clips right: {b}"
        # Centered: ink midpoint near the preview center.
        mid_y = (b["top"] + b["bottom"]) / 2.0
        assert abs(mid_y - 18) <= 3, f"{kind} off-center: {b}"
        mid_x = (b["left"] + b["right"]) / 2.0
        assert abs(mid_x - 160) <= 6, f"{kind} off-center: {b}"
        # Closed shapes have real presence, never slivers.
        if kind in (ToolType.RECTANGLE, ToolType.ELLIPSE):
            assert (b["bottom"] - b["top"]) >= 14, (
                f"{kind} preview is a sliver: {b}")


def test_arrow_preview_uses_configured_endings(qtbot):
    """Arrow/Line previews honor start/end endings and line style."""
    from app.toolshelf.tool_popup import _Preview

    def ink_of(slot):
        pv = _Preview()
        qtbot.addWidget(pv)
        pv.set_slot(slot)
        _img, b = _render_preview_ink(pv)
        return b

    headed = ink_of(_shape_slot(ToolType.ARROW, start_ending="none",
                                end_ending="arrow"))
    bare = ink_of(_shape_slot(ToolType.ARROW, start_ending="none",
                              end_ending="none"))
    assert headed["n"] > bare["n"] + 20, (headed, bare)
    dotted = ink_of(_shape_slot(ToolType.LINE, line_style="dotted",
                                start_ending="none", end_ending="none"))
    solid = ink_of(_shape_slot(ToolType.LINE, line_style="solid",
                               start_ending="none", end_ending="none"))
    assert dotted["n"] < solid["n"], (dotted, solid)
    filled = ink_of(_shape_slot(ToolType.RECTANGLE, fill_enabled=True,
                                fill_color="#e5484d", fill_opacity=0.5))
    hollow = ink_of(_shape_slot(ToolType.RECTANGLE, fill_enabled=False))
    assert filled["n"] > hollow["n"] * 2, (filled, hollow)


def test_shape_preview_updates_on_property_change(qtbot, cleared_settings):
    """Changing a property updates the corresponding preview at once."""
    from app.toolshelf.tool_popup import _Preview, PropertiesForm

    viewer = _make_viewer(qtbot)
    rect = viewer.tool_store.add_slot(ToolType.RECTANGLE, name="R",
                                      color="#e5484d", width_mm=1.0)
    form = PropertiesForm(rect, dark_tokens(), viewer)
    qtbot.addWidget(form)
    assert form._preview._slot.color.lower() == "#e5484d"
    form._apply(color="#2b6cff")
    assert form._preview._slot.color.lower() == "#2b6cff"
    form._apply(width_mm=3.0)
    assert abs(form._preview._slot.width_mm - 3.0) < 1e-9
    # Render-level: a width change visibly changes the preview.
    thin = _Preview()
    qtbot.addWidget(thin)
    thin.set_slot(_shape_slot(ToolType.RECTANGLE, width_mm=1.0))
    _img_t, bt = _render_preview_ink(thin)
    thick = _Preview()
    qtbot.addWidget(thick)
    thick.set_slot(_shape_slot(ToolType.RECTANGLE, width_mm=4.0))
    _img_k, bk = _render_preview_ink(thick)
    assert bk["n"] > bt["n"], (bt, bk)
