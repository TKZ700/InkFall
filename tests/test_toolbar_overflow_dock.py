"""Toolbar overflow, end-cap, live drag, zone alignment, cursor tests."""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app import fx
from app.theme.tokens import dark_tokens
from tests.qt_helpers import wait_edge_settled, wait_stable, wait_until
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
    try:
        fx.set_motion_mode("follow")
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


def _along_bar(shelf):
    if shelf._orientation() == "horizontal":
        return shelf._scroll.horizontalScrollBar()
    return shelf._scroll.verticalScrollBar()


# -- overflow edges -------------------------------------------------------

# NOTE: the start/mid/end overflow state machine is covered canonically by
# test_m5a1_final_fixes.test_boundary_full_cycle_animated (real click trips)
# and test_m5a11_fixes.test_scroll_limit_roundtrip_all_docks (all docks);
# the idle no-overflow state by test_m5a2_fixes idle test (rendered lines).
# Earlier per-file duplicates were consolidated here (M5-A2 suite pruning).

def test_overflow_arrow_click_steps(qtbot, cleared_settings):
    from PySide6.QtTest import QTest

    viewer = _make_viewer(qtbot, customs=14)
    shelf = viewer.tool_shelf
    bar = _along_bar(shelf)
    assert bar.value() == bar.minimum()
    before = bar.value()
    QTest.mouseClick(shelf._edge_next, Qt.MouseButton.LeftButton)
    assert wait_until(qtbot, lambda: bar.value() > before)
    bar.setValue(bar.maximum())
    assert wait_stable(qtbot, bar.value)
    at_end = bar.value()
    QTest.mouseClick(shelf._edge_prev, Qt.MouseButton.LeftButton)
    assert wait_until(qtbot, lambda: bar.value() < at_end)


# -- end-cap ---------------------------------------------------------------

def test_endcap_order_all_docks(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=3)
    shelf = viewer.tool_shelf
    for dock in ("left", "right", "top", "bottom"):
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        order = [shelf.layout().itemAt(i).widget()
                 for i in range(shelf.layout().count())]
        core_ids = {s.id.value for s in viewer.tool_store.core_slots}
        core_btns = [shelf._buttons[sid] for sid in core_ids]
        first_core = min(order.index(b) for b in core_btns)
        assert first_core <= 1  # core block first (no handle ahead)
        assert order[-2] is shelf._add
        assert order[-1] is shelf._handle  # move directly after Add
        div2 = order.index(shelf._edge_next)
        assert div2 == order.index(shelf._add) - 1


def test_add_move_black_circular(qtbot, cleared_settings):
    from PySide6.QtGui import QColor, QPixmap

    viewer = _make_viewer(qtbot)
    shelf = viewer.tool_shelf
    sentinel = QColor("#ff00ff")
    for cell in (shelf._add, shelf._handle):
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QRegion
        from PySide6.QtWidgets import QWidget

        canvas = QPixmap(cell.size())
        canvas.fill(sentinel)
        # DrawChildren only: no window-background fill, so unpainted
        # regions keep the sentinel (grab() leaves them undefined).
        cell.render(canvas, QPoint(), QRegion(),
                    QWidget.RenderFlag.DrawChildren)
        img = canvas.toImage()
        cx, cy = img.width() // 2, img.height() // 2
        # Medallion core painted (not sentinel).
        assert img.pixelColor(cx, cy) != sentinel, cell
        # Off-glyph probe: inside the 30px medallion (12px out) but past
        # the centered 20px mark — must be the near-black surface.
        probe = img.pixelColor(cx + 12, cy)
        assert probe != sentinel, cell
        assert probe.lightness() < 70, (cell, probe.lightness())
        # Corners paint no square button face (sentinel survives).
        corner = img.pixelColor(2, 2)
        assert corner == sentinel, (cell, corner.name())


# -- live follow + alignment --------------------------------------------------

def test_live_follow_tracks_cursor(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=2)
    shelf = viewer.tool_shelf
    handle = shelf._handle
    QTest.mousePress(handle, Qt.MouseButton.LeftButton,
                     pos=handle.rect().center())
    qtbot.wait(450)
    assert viewer._docking is True
    p1 = (shelf.x() + shelf.width() // 2, shelf.y() + shelf.height() // 2)
    target = viewer.mapToGlobal(QPoint(viewer.width() - 150, 150))
    QTest.mouseMove(handle, pos=handle.mapFromGlobal(target))

    def _moved_far():
        p = (shelf.x() + shelf.width() // 2,
             shelf.y() + shelf.height() // 2)
        return abs(p[0] - p1[0]) + abs(p[1] - p1[1]) > 100

    assert wait_until(qtbot, _moved_far)
    p2 = (shelf.x() + shelf.width() // 2, shelf.y() + shelf.height() // 2)
    moved = abs(p2[0] - p1[0]) + abs(p2[1] - p1[1])
    assert moved > 100  # toolbar physically followed the pointer
    # Shelf center tracks the cursor (within one cell tolerance).
    local = viewer.mapFromGlobal(target)
    assert abs(p2[0] - local.x()) < 60 and abs(p2[1] - local.y()) < 60
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    assert wait_until(qtbot, lambda: viewer._docking is False)


def test_zone_preview_commit_share_anchor(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=2)
    for dock in ("top", "left", "right", "bottom"):
        # Commit first so the shelf carries the dock's own size; then
        # the shared anchor must reproduce the committed position and
        # the zone highlight must center on it.
        viewer.tool_shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (viewer.tool_shelf.width(),
                                           viewer.tool_shelf.height()))
        px, py, sw, sh = viewer._shelf_placement(dock)
        pos = viewer.tool_shelf.pos()
        assert (pos.x(), pos.y()) == (px, py)
        r = viewer._dock_zone_rects()[dock]
        zcx, zcy = r.x() + r.width() // 2, r.y() + r.height() // 2
        pcx, pcy = px + sw // 2, py + sh // 2
        if dock in ("top", "bottom"):
            assert zcx == pcx  # along-edge anchor shared exactly
        else:
            assert zcy == pcy


def test_esc_restores_original_dock(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=2)
    shelf = viewer.tool_shelf
    shelf.set_dock("left")
    assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
    before_dock = viewer.tool_store.dock
    before_pos = (shelf.x(), shelf.y())
    handle = shelf._handle
    QTest.mousePress(handle, Qt.MouseButton.LeftButton,
                     pos=handle.rect().center())
    qtbot.wait(450)
    target = viewer.mapToGlobal(QPoint(viewer.width() - 150, 150))
    QTest.mouseMove(handle, pos=handle.mapFromGlobal(target))
    assert wait_until(
        qtbot, lambda: (shelf.x(), shelf.y()) != before_pos)  # followed live
    QTest.keyClick(handle, Qt.Key.Key_Escape)
    QTest.mouseRelease(handle, Qt.MouseButton.LeftButton)
    assert wait_until(
        qtbot, lambda: viewer.tool_store.dock == before_dock
        and (shelf.x(), shelf.y()) == before_pos)


# -- cursor ---------------------------------------------------------------------

def test_selection_glyph_filled_tailless(qtbot, cleared_settings):
    from PySide6.QtGui import QColor

    from app import icons as icon_mod
    from app.icons import pixmap

    sel = pixmap("selection", "#e8eaf2", 22).toImage()
    las = pixmap("lasso", "#e8eaf2", 22).toImage()
    pan = pixmap("pan", "#e8eaf2", 22).toImage()

    # Marquee + filled arrow: dashed rect plus a light fill core that
    # is distinctly brighter than the outline ink itself (capped so
    # very light inks still discriminate white fill from outline).
    assert "stroke-dasharray" in icon_mod._PATHS["selection"]
    outline_l = QColor("#e8eaf2").lightness()
    bright_at = min(outline_l + 40, 245)
    bright = sum(
        1 for y in range(sel.height()) for x in range(sel.width())
        if sel.pixelColor(x, y).alpha() > 8
        and sel.pixelColor(x, y).lightness() > bright_at)
    assert bright > 5
    lasso_bright = sum(
        1 for y in range(las.height()) for x in range(las.width())
        if las.pixelColor(x, y).alpha() > 8
        and las.pixelColor(x, y).lightness() > bright_at)
    assert bright > lasso_bright  # lasso has no light fill core

    def _bytes(img):
        from PySide6.QtCore import QBuffer, QIODevice

        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    assert _bytes(sel) != _bytes(pan) and _bytes(sel) != _bytes(las)
    # Core slot paints the new cursor (no fallback dot).
    viewer = _make_viewer(qtbot)
    slot = next(s for s in viewer.tool_store.slots
                if s.kind == ToolType.SELECTION)
    btn = viewer.tool_shelf._buttons[slot.id.value]
    img = btn.grab().toImage()
    painted = sum(1 for y in range(img.height())
                  for x in range(img.width())
                  if img.pixelColor(x, y).alpha() > 0)
    assert painted > 40


# -- morph behavioral (separator -> arrow -> separator) -------------------------


def _render_on_dark(widget):
    """Render a widget onto a known dark fill (grab() leaves unpainted
    memory undefined, which breaks color counting)."""
    from PySide6.QtGui import QColor, QPixmap

    canvas = QPixmap(widget.size())
    canvas.fill(QColor("#181C23"))
    widget.render(canvas)
    return canvas.toImage()


def _accent_stats(image):
    """(count, farthest) of accent-chevron pixels: blue-dominant ink that
    is neither the dark fill nor a white blend."""
    pts = []
    for x in range(image.width()):
        for y in range(image.height()):
            pixel = image.pixelColor(x, y)
            if (pixel.blue() > 150 and pixel.blue() > pixel.red() + 30
                    and pixel.red() < 220):
                pts.append((x, y))
    return len(pts), pts


def test_boundary_morph_paints_directional_chevron(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=14)
    shelf = viewer.tool_shelf
    shelf.set_dock("left")  # vertical shelf: chevrons point up/down
    assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
    bar = _along_bar(shelf)
    assert bar.maximum() > 0
    # Forward boundary shows a chevron pointing toward remaining content
    # (down = past center); the idle backward boundary shows line only.
    img = _render_on_dark(shelf._edge_next)
    count, pts = _accent_stats(img)
    assert count > 4, count
    cy = img.height() // 2
    assert max(y for _x, y in pts) > cy  # tip side reaches past center
    idle = _render_on_dark(shelf._edge_prev)
    idle_count, _ = _accent_stats(idle)
    assert idle_count <= 2, idle_count
    # At the end the roles swap: forward collapses, backward chevrons up.
    bar.setValue(bar.maximum())
    assert wait_edge_settled(qtbot, shelf._edge_next, 0.0)
    assert wait_edge_settled(qtbot, shelf._edge_prev, 1.0)
    end_count, _ = _accent_stats(_render_on_dark(shelf._edge_next))
    assert end_count <= 2, end_count
    back_img = _render_on_dark(shelf._edge_prev)
    back_count, back_pts = _accent_stats(back_img)
    assert back_count > 4, back_count
    assert min(y for _x, y in back_pts) < back_img.height() // 2


def test_wheel_scroll_is_animated_not_jump(qtbot, cleared_settings, monkeypatch):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    import app.fx as fx_mod

    viewer = _make_viewer(qtbot, customs=14)
    shelf = viewer.tool_shelf
    calls = []
    real = fx_mod.smooth_scroll_to

    def _spy(bar, target, duration, parent, *args, **kwargs):
        calls.append((bar, target))
        return real(bar, target, duration, parent, *args, **kwargs)

    monkeypatch.setattr(fx_mod, "smooth_scroll_to", _spy)
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0),
                        QPoint(0, -120), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    shelf._scroll.wheelEvent(event)
    assert len(calls) == 1  # wheel routes through fx glide, not setValue


# -- narrow-resize robustness ----------------------------------------------------


def _shelf_children_inside(shelf):
    """Every layout child must sit inside the shelf rect (no escape)."""
    rect = shelf.rect()
    bad = []
    layout = shelf.layout()
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if widget is None:
            continue
        geo = widget.geometry()
        if not rect.contains(geo):
            bad.append((widget, geo))
    return bad


def _assert_shelf_healthy(viewer, shelf):
    shelf_rect = shelf.geometry()
    assert shelf_rect.left() >= 0 and shelf_rect.top() >= 0
    assert shelf_rect.right() <= viewer.width()
    assert shelf_rect.bottom() <= viewer.height()
    assert _shelf_children_inside(shelf) == []
    assert shelf._add.isVisible() and shelf._handle.isVisible()
    add_geo = shelf._add.geometry()
    assert shelf.rect().contains(add_geo)
    handle_geo = shelf._handle.geometry()
    assert shelf.rect().contains(handle_geo)
    bar = _along_bar(shelf)
    assert bar.minimum() <= bar.value() <= bar.maximum()
    assert shelf._edge_prev.has_more() == (bar.value() > bar.minimum())
    assert shelf._edge_next.has_more() == (bar.value() < bar.maximum())


def test_narrow_resize_never_escapes(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot, customs=16)
    shelf = viewer.tool_shelf
    bar = _along_bar(shelf)
    # Resize while scrolled to beginning / middle / end.
    for value in (bar.minimum(), bar.maximum() // 2, bar.maximum()):
        bar.setValue(value)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        for width in (1100, 700, 450, 300, 700, 1100):
            viewer.resize(width, 750)
            assert wait_stable(qtbot, lambda: (shelf.width(),
                                               shelf.height()))
            _assert_shelf_healthy(viewer, shelf)
    # Resize during an active overflow state + orientation flips +
    # repeated grow/shrink cycles.
    for width, height in ((300, 750), (1100, 750), (300, 500),
                          (500, 300), (1100, 750)):
        viewer.resize(width, height)
        assert wait_stable(qtbot, lambda: (viewer.width(),
                                           viewer.height()))
    for dock in ("left", "top", "left", "bottom"):
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        _assert_shelf_healthy(viewer, shelf)
    for width in (1100, 320, 1100, 320, 1100):
        viewer.resize(width, 750)
        assert wait_stable(qtbot, lambda: (viewer.width(), viewer.height()))
        _assert_shelf_healthy(viewer, shelf)
