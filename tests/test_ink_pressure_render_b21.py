"""B.2.1 Slice 3: pressure-sensitive outline rendering integration.

Model latch (Qt-free) + viewer helper + raster integration through the
existing overlay paint path. Raster tests reuse the MainWindow harness
pattern from ``tests/test_pen_stroke_overlay.py``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from annotations.interaction import InteractionController, PenHandler
from annotations.models import InkStroke
from app.viewport_controller import page_screen_rect
from core.types import AnnotationId, PageId, Point
from tests.helpers import committed_strokes
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _pen_tool(pressure=False, width_mm=3.0, color="#ff0000", opacity=1.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color=color,
                        width_mm=width_mm, opacity=opacity,
                        pressure=pressure)


# -- Part 1: model field -------------------------------------------------

def test_pressure_enabled_defaults_false():
    assert InkStroke().pressure_enabled is False


def test_legacy_positional_construction_keeps_default():
    stroke = InkStroke(AnnotationId(), PageId(), 0.0,
                       (Point(0.0, 0.0),), "#000000", 0.5, 1.0, (), ())
    assert stroke.pressure_enabled is False


def test_explicit_true_retained():
    stroke = InkStroke(points=(Point(0.0, 0.0),), pressure_enabled=True)
    assert stroke.pressure_enabled is True


# -- Part 2: press-time latch --------------------------------------------

def _controller(pressure):
    return InteractionController(_pen_tool(pressure=pressure))


def test_latch_enabled_stroke_survives_midstroke_reconfig():
    page = PageId()
    tool_on = _pen_tool(pressure=True)
    ctrl = InteractionController(tool_on)
    live = ctrl.handle_pen_press(page, 0.0, 0.0, 0.2, 100)["stroke_update"]
    assert live.pressure_enabled is True
    # Property edit on the SAME slot (id preserved): no cancellation,
    # but the active stroke must keep its latched mode.
    ctrl.set_tool(replace(tool_on, pressure=False))
    live2 = ctrl.handle_pen_move(page, 5.0, 0.0, 0.9, 116)["stroke_update"]
    assert live2.pressure_enabled is True
    done = ctrl.handle_pen_release(page, 10.0, 0.0, 0.9, 132)["stroke_done"]
    assert done.pressure_enabled is True
    # The completed stroke keeps its mode after further switches.
    ctrl.set_tool(_pen_tool(pressure=False))
    assert done.pressure_enabled is True


def test_next_stroke_adopts_new_setting():
    page = PageId()
    ctrl = _controller(True)
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.5, 100)
    ctrl.set_tool(_pen_tool(pressure=False))
    ctrl.handle_pen_release(page, 1.0, 0.0, 0.5, 116)
    second = ctrl.handle_pen_press(page, 2.0, 0.0, 0.5, 200)["stroke_update"]
    assert second.pressure_enabled is False


def test_reverse_transition_disabled_to_enabled():
    page = PageId()
    tool_off = _pen_tool(pressure=False)
    ctrl = InteractionController(tool_off)
    first = ctrl.handle_pen_press(page, 0.0, 0.0, 1.0, 100)["stroke_update"]
    assert first.pressure_enabled is False
    ctrl.set_tool(replace(tool_off, pressure=True))
    mid = ctrl.handle_pen_move(page, 5.0, 0.0, 1.0, 116)["stroke_update"]
    assert mid.pressure_enabled is False
    ctrl.handle_pen_release(page, 10.0, 0.0, 1.0, 132)
    second = ctrl.handle_pen_press(page, 20.0, 0.0, 0.4, 200)["stroke_update"]
    assert second.pressure_enabled is True


def test_configure_none_keeps_pending_value():
    pen = PenHandler()
    pen.configure(_pen_tool(pressure=True))
    pen.configure(None)
    stroke = pen.down(PageId(), 0.0, 0.0)["stroke_update"]
    assert stroke.pressure_enabled is True


def test_cancel_keeps_latch_until_next_stroke():
    page = PageId()
    ctrl = _controller(True)
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.5, 100)
    ctrl.set_tool(_pen_tool(pressure=False))
    ctrl.cancel()
    # Cancelled stroke is gone, but the latch still reflects the
    # stroke-start configuration until the next stroke begins.
    nxt = ctrl.handle_pen_press(page, 1.0, 1.0, 0.5, 200)["stroke_update"]
    assert nxt.pressure_enabled is False


# -- Part 3: viewer helper (needs a viewer instance; logic is Qt-free) --

def _make_viewer():
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    return PdfViewer(DocumentManager())


def _enabled_stroke(pressures=(0.2, 0.8), width_mm=3.0):
    n = len(pressures)
    return InkStroke(points=tuple(Point(float(i * 10), 0.0) for i in range(n)),
                     color="#ff0000", width_mm=width_mm, opacity=1.0,
                     pressures=tuple(pressures),
                     times_ms=tuple(100 + 16 * i for i in range(n)),
                     pressure_enabled=True)


def test_helper_none_for_disabled_stroke():
    viewer = _make_viewer()
    stroke = _enabled_stroke()
    disabled = InkStroke(points=stroke.points, color=stroke.color,
                         width_mm=stroke.width_mm, opacity=stroke.opacity,
                         pressures=stroke.pressures, times_ms=stroke.times_ms,
                         pressure_enabled=False)
    assert viewer._pressure_outline(disabled) is None
    viewer.close()


def test_helper_none_for_empty_or_missing_pressures():
    viewer = _make_viewer()
    assert viewer._pressure_outline(_enabled_stroke(pressures=())) is None
    legacy = InkStroke(points=(Point(0.0, 0.0), Point(1.0, 0.0)),
                       pressure_enabled=True)
    assert viewer._pressure_outline(legacy) is None
    viewer.close()


def test_helper_none_for_mismatched_arrays_duck_typed():
    from types import SimpleNamespace

    viewer = _make_viewer()
    bogus = SimpleNamespace(points=(Point(0.0, 0.0), Point(1.0, 0.0)),
                            pressures=(0.5,),
                            pressure_enabled=True, width_mm=1.0)
    assert viewer._pressure_outline(bogus) is None
    viewer.close()


def test_helper_min_max_widths_in_points():
    viewer = _make_viewer()
    outline = viewer._pressure_outline(_enabled_stroke(
        pressures=(0.0, 1.0), width_mm=25.4 / 72.0 * 4.0))
    assert outline is not None and outline.dots == ()
    base_pt = 4.0
    xs = [p.x for p in outline.polygon]
    ys = [p.y for p in outline.polygon]
    assert min(ys) == pytest.approx(-base_pt / 2.0)  # max pressure rail
    assert max(ys) == pytest.approx(base_pt / 2.0)
    # Minimum-pressure end rails sit at the normal-profile floor
    # (B.2.4 retune: 0.2 * base half-width; was 0.3 before the retune).
    low_x = xs[0]
    lows = sorted(p.y for p in outline.polygon
                  if abs(p.x - low_x) < 1e-9)
    assert lows[0] == pytest.approx(-base_pt * 0.2 / 2.0)
    assert lows[-1] == pytest.approx(base_pt * 0.2 / 2.0)
    viewer.close()


def test_helper_dot_for_single_point():
    viewer = _make_viewer()
    outline = viewer._pressure_outline(_enabled_stroke(pressures=(0.6,)))
    assert outline is not None
    assert outline.polygon == ()
    assert len(outline.dots) == 1
    viewer.close()


def test_helper_mouse_uniform_width():
    viewer = _make_viewer()
    outline = viewer._pressure_outline(_enabled_stroke(
        pressures=(1.0, 1.0, 1.0), width_mm=25.4 / 72.0 * 4.0))
    ys = [p.y for p in outline.polygon]
    assert min(ys) == pytest.approx(-2.0)
    assert max(ys) == pytest.approx(2.0)
    viewer.close()


def test_live_and_completed_share_geometry():
    # Release exactly at the last move point appends no new sample,
    # so the live preview and the finalized stroke carry identical
    # samples and must yield identical outlines (no release jump).
    viewer = _make_viewer()
    page = PageId()
    ctrl = _controller(True)
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.3, 100)
    live = ctrl.handle_pen_move(page, 10.0, 0.0, 0.9, 116)["stroke_update"]
    done = ctrl.handle_pen_release(page, 10.0, 0.0, 0.9, 132)["stroke_done"]
    assert [(p.x, p.y) for p in live.points] == [
        (p.x, p.y) for p in done.points]
    assert viewer._pressure_outline(live) == viewer._pressure_outline(done)
    viewer.close()


# -- Part 4: raster integration (offscreen MainWindow) -------------------

@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        keys = ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                "inkfall/m5/toolbar/active",
                "inkfall/m5/toolbar/custom_colors",
                "inkfall/m5/toolbar/presets")
        for key in keys:
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


def _open_window(qtbot, tmp_path, tool):
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / "pressure.pdf"), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool)
    qtbot.wait(200)
    return window, viewer


def _drive(viewer, page_id, samples):
    """Feed page-space (x, y, pressure) samples through the real path."""
    it = viewer._interaction
    t = 1000
    viewer._apply_intent(it.handle_pen_press(page_id, *samples[0], t))
    for sample in samples[1:]:
        t += 16
        viewer._apply_intent(it.handle_pen_move(page_id, *sample, t))
    viewer._apply_intent(it.handle_pen_release(page_id, *samples[-1], t + 16))


def _project(viewer, entry, lx, ly):
    layout = viewer._pipeline.layout
    scale = viewer._display_scale()
    vw, _vh = viewer._render_viewport()
    rect = page_screen_rect(float(entry.y_offset_px),
                            float(entry.render_width_px),
                            float(entry.render_height_px),
                            viewer._scroll_doc(), vw, scale,
                            viewer._scroll_doc_x(),
                            float(layout.total_width_px))
    bucket = float(layout.raster_scale)
    return (rect.x + lx * bucket * scale, rect.y + ly * bucket * scale)


def _is_red(color):
    return color.red() > 150 and color.green() < 150 and color.blue() < 150


def _wait_pixel(qtbot, viewer, x, y, pred, timeout=8000):
    """Poll until a probe pixel satisfies pred (survives slow paint)."""
    import time as _time

    deadline = _time.monotonic() + timeout / 1000.0
    while True:
        try:
            if bool(pred(viewer.grab().toImage().pixelColor(
                    int(x), int(y)))):
                return
        except Exception:
            pass
        if _time.monotonic() >= deadline:
            raise AssertionError(
                f"probe pixel ({x:.0f}, {y:.0f}) never satisfied predicate")
        qtbot.wait(200)


def _wait_settled(qtbot, viewer, tries=12):
    """Wait until async tile arrival stops changing the frame."""
    prev = None
    for _ in range(tries):
        qtbot.wait(200)
        img = viewer.grab().toImage()
        if prev is not None and _sample_equal(img, prev):
            return img
        prev = img
    return prev


def _sample_equal(a, b):
    if a.size() != b.size():
        return False
    step = 25
    for x in range(0, a.width(), step):
        for y in range(0, a.height(), step):
            if a.pixel(x, y) != b.pixel(x, y):
                return False
    return True


def _region_equal(a, b):
    if a.size() != b.size():
        return False
    for x in range(a.width()):
        for y in range(a.height()):
            if a.pixel(x, y) != b.pixel(x, y):
                return False
    return True


def _red_span(image, x, y0, y1):
    span = 0
    for y in range(int(y0), int(y1)):
        if _is_red(image.pixelColor(int(x), y)):
            span += 1
    return span


def _page_id(viewer):
    return viewer._pipeline.layout.entry(0).page_id


def test_enabled_ramp_paints_variable_width(qtbot, tmp_path,
                                            cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        page = _page_id(viewer)
        xs = [100.0 + 50.0 * i for i in range(9)]
        pressures = [0.05] * 4 + [1.0] * 5
        _drive(viewer, page, [(x, 250.0, p)
                              for x, p in zip(xs, pressures)])
        assert len(committed_strokes(viewer)) == 1
        viewer.update()
        entry = viewer._pipeline.layout.entry(0)
        low_x, low_y = _project(viewer, entry, 150.0, 250.0)
        high_x, high_y = _project(viewer, entry, 450.0, 250.0)
        _wait_pixel(qtbot, viewer, high_x, high_y, _is_red)
        image = viewer.grab().toImage()
        low_span = _red_span(image, low_x, low_y - 15, low_y + 15)
        high_span = _red_span(image, high_x, high_y - 15, high_y + 15)
        assert low_span >= 1, "light-pressure ink must stay visible"
        assert high_span >= low_span + 4
    finally:
        viewer.close_document()
        window.close()


def test_disabled_tool_keeps_scalar_width(qtbot, tmp_path, cleared_settings):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=False))
    try:
        viewer.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(200.0, 250.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(400.0, 250.0),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        viewer.mouseReleaseEvent(QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(400.0, 250.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        assert len(committed_strokes(viewer)) == 1
        stroke = committed_strokes(viewer)[0]
        assert stroke.pressure_enabled is False
        assert viewer._pressure_outline(stroke) is None
        viewer.update()
        _wait_pixel(qtbot, viewer, 300, 250, _is_red)
        image = viewer.grab().toImage()
        # Base width 3mm ~ 11.3px at zoom 1; legacy round polyline.
        assert _red_span(image, 300, 235, 265) >= 8
    finally:
        viewer.close_document()
        window.close()


def test_enabled_mouse_stroke_uses_base_width(qtbot, tmp_path,
                                              cleared_settings):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        viewer.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(200.0, 250.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        viewer.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, QPointF(400.0, 250.0),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        viewer.mouseReleaseEvent(QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(400.0, 250.0),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        assert len(committed_strokes(viewer)) == 1
        stroke = committed_strokes(viewer)[0]
        assert stroke.pressures == (1.0, 1.0)
        outline = viewer._pressure_outline(stroke)
        assert outline is not None and outline.dots == ()
        viewer.update()
        _wait_pixel(qtbot, viewer, 300, 250, _is_red)
        image = viewer.grab().toImage()
        # Constant 1.0 pressure -> uniform base-width capsule.
        assert _red_span(image, 300, 235, 265) >= 8
    finally:
        viewer.close_document()
        window.close()


def test_single_point_dot_renders(qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        page = _page_id(viewer)
        _drive(viewer, page, [(150.0, 250.0, 0.7)])
        assert len(committed_strokes(viewer)) == 1
        assert len(committed_strokes(viewer)[0].points) == 1
        viewer.update()
        entry = viewer._pipeline.layout.entry(0)
        cx, cy = _project(viewer, entry, 150.0, 250.0)
        _wait_pixel(qtbot, viewer, cx, cy, _is_red)
        image = viewer.grab().toImage()
        assert _is_red(image.pixelColor(int(cx), int(cy)))
    finally:
        viewer.close_document()
        window.close()


def test_color_and_opacity_preserved(qtbot, tmp_path, cleared_settings):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    def stroke_span(color, opacity):
        window, viewer = _open_window(
            qtbot, tmp_path, _pen_tool(pressure=True, width_mm=4.0,
                                       color=color, opacity=opacity))
        try:
            viewer.mousePressEvent(QMouseEvent(
                QEvent.Type.MouseButtonPress, QPointF(200.0, 250.0),
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            viewer.mouseMoveEvent(QMouseEvent(
                QEvent.Type.MouseMove, QPointF(400.0, 250.0),
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
            viewer.mouseReleaseEvent(QMouseEvent(
                QEvent.Type.MouseButtonRelease, QPointF(400.0, 250.0),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier))
            viewer.update()
            _wait_pixel(qtbot, viewer, 300, 250,
                        lambda c: c.blue() > 150)
            return viewer.grab().toImage().pixelColor(300, 250)
        finally:
            viewer.close_document()
            window.close()

    solid = stroke_span("#0000ff", 1.0)
    assert solid.blue() > 150 and solid.red() < 150
    soft = stroke_span("#0000ff", 0.5)
    assert soft.blue() > 150 and 50 < soft.red() < 200


def test_mixed_frame_pressure_does_not_leak_state(qtbot, tmp_path,
                                                  cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=False))
    try:
        page = _page_id(viewer)
        legacy_samples = [(100.0 + 25.0 * i, 250.0, 1.0) for i in range(9)]
        _drive(viewer, page, legacy_samples)
        assert len(committed_strokes(viewer)) == 1
        viewer.update()
        entry = viewer._pipeline.layout.entry(0)
        x0, y0 = _project(viewer, entry, 100.0, 250.0)
        x1, y1 = _project(viewer, entry, 300.0, 250.0)
        _wait_pixel(qtbot, viewer, (x0 + x1) / 2, y0, _is_red)
        # Settle async tiles first: only then is a pixel-exact
        # before/after comparison meaningful.
        _wait_settled(qtbot, viewer)
        region = (int(x0) - 20, int(y0) - 20, int(x1 - x0) + 40, 40)
        before = viewer.grab().toImage().copy(*region)
        # Pressure stroke well clear of the legacy region but below
        # top chrome (y=150 is proven paintable by the overlay suite),
        # painted FIRST in the same frame.
        far = InkStroke(
            page_id=page,
            points=tuple(Point(100.0 + 25.0 * i, 170.0) for i in range(9)),
            color="#0000ff", width_mm=4.0, opacity=1.0,
            pressures=(0.1, 0.3, 0.5, 0.7, 0.9, 1.0, 1.0, 1.0, 1.0),
            times_ms=tuple(1000 + 16 * i for i in range(9)),
            pressure_enabled=True)
        assert viewer._pressure_outline(far) is not None
        # M6.4: committed strokes live in the document store; insert the
        # far stroke first (same z-order as the old list insert).
        viewer._document.annotations.insert_at(far, 0, 0)
        viewer.update()
        far_entry = viewer._pipeline.layout.entry(0)
        fx, fy = _project(viewer, far_entry, 200.0, 170.0)
        _wait_pixel(qtbot, viewer, fx, fy,
                    lambda c: c.blue() > 150 and c.red() < 150)
        after = viewer.grab().toImage().copy(*region)
        assert before.size() == after.size()
        assert _region_equal(before, after)
    finally:
        viewer.close_document()
        window.close()


def test_outline_stable_across_scroll_zoom_and_cancel(qtbot, tmp_path,
                                                      cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path, _pen_tool(pressure=True))
    try:
        page = _page_id(viewer)
        _drive(viewer, page, [(100.0 + 50.0 * i, 250.0, 0.2 + 0.1 * i)
                              for i in range(5)])
        stroke = committed_strokes(viewer)[0]
        before = [(p.x, p.y) for p in stroke.points]
        outline_before = viewer._pressure_outline(stroke)
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        assert [(p.x, p.y) for p in stroke.points] == before
        viewer.set_zoom(2.0)
        assert [(p.x, p.y) for p in stroke.points] == before
        # Geometry is document-space: identical across zoom.
        assert viewer._pressure_outline(stroke) == outline_before
        viewer.set_zoom(1.0)
        viewer._scrollbar.setValue(0)
        qtbot.wait(100)
        # Cancel leaves no live stroke and repaints cleanly.
        viewer._apply_intent(viewer._interaction.handle_pen_press(
            page, 400.0, 250.0, 0.5, 5000))
        assert viewer._live_stroke is not None
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._live_stroke is None
        assert len(committed_strokes(viewer)) == 1
        viewer.update()
        entry = viewer._pipeline.layout.entry(0)
        cx, cy = _project(viewer, entry, 200.0, 250.0)
        _wait_pixel(qtbot, viewer, cx, cy, _is_red)
        image = viewer.grab().toImage()
        assert _is_red(image.pixelColor(int(cx), int(cy)))
        # Tool switch follows the production path (_on_tool_selected):
        # the cancel intent is applied before the tool is replaced.
        viewer._apply_intent(viewer._interaction.handle_pen_press(
            page, 400.0, 250.0, 0.5, 6000))
        assert viewer._live_stroke is not None
        viewer._apply_intent(viewer._interaction.cancel())
        viewer._interaction.set_tool(_pen_tool(pressure=False))
        assert viewer._live_stroke is None
    finally:
        viewer.close_document()
        window.close()


# -- Part 5: global resolver wiring (post-Slice-3 fix A) ------------------

def _resolving_controller(slot_pressure, resolver_value):
    return InteractionController(_pen_tool(pressure=slot_pressure),
                                 pressure_resolver=lambda: resolver_value)


def test_global_only_enables_stroke():
    ctrl = _resolving_controller(False, True)
    live = ctrl.handle_pen_press(PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert live.pressure_enabled is True


def test_tool_only_enables_stroke():
    ctrl = _resolving_controller(True, False)
    live = ctrl.handle_pen_press(PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert live.pressure_enabled is True


def test_both_enabled_enables_stroke():
    ctrl = _resolving_controller(True, True)
    live = ctrl.handle_pen_press(PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert live.pressure_enabled is True


def test_both_disabled_keeps_legacy_stroke():
    ctrl = _resolving_controller(False, False)
    done = ctrl.handle_pen_press(PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert done.pressure_enabled is False
    ctrl2 = InteractionController(_pen_tool(pressure=False))
    done2 = ctrl2.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert done2.pressure_enabled is False


def test_resolver_none_means_slot_flag_only():
    on = InteractionController(_pen_tool(pressure=True),
                               pressure_resolver=lambda: None)
    assert on.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_enabled is True
    off = InteractionController(_pen_tool(pressure=False),
                                pressure_resolver=lambda: None)
    assert off.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_enabled is False


def test_resolver_failure_falls_back_to_slot_flag():
    def boom():
        raise RuntimeError("settings unreadable")

    on = InteractionController(_pen_tool(pressure=True),
                               pressure_resolver=boom)
    assert on.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_enabled is True
    off = InteractionController(_pen_tool(pressure=False),
                                pressure_resolver=boom)
    assert off.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_enabled is False


def test_global_change_midstroke_latches_per_stroke():
    state = {"global": True}
    ctrl = InteractionController(_pen_tool(pressure=False),
                                 pressure_resolver=lambda: state["global"])
    page = PageId()
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.3, 100)
    state["global"] = False  # user toggles Settings mid-stroke
    mid = ctrl.handle_pen_move(page, 5.0, 0.0, 0.5, 116)["stroke_update"]
    assert mid.pressure_enabled is True
    ctrl.handle_pen_release(page, 10.0, 0.0, 0.5, 132)
    nxt = ctrl.handle_pen_press(page, 20.0, 0.0, 0.5, 200)["stroke_update"]
    assert nxt.pressure_enabled is False


def test_configure_override_param_defaults_to_slot_flag():
    pen = PenHandler()
    pen.configure(_pen_tool(pressure=True))
    assert pen.down(PageId(), 0.0, 0.0)["stroke_update"].pressure_enabled is True
    pen.configure(_pen_tool(pressure=True), pressure_override=False)
    assert pen.down(PageId(), 1.0, 1.0)["stroke_update"].pressure_enabled is True
    pen.configure(_pen_tool(pressure=False), pressure_override=True)
    assert pen.down(PageId(), 2.0, 2.0)["stroke_update"].pressure_enabled is True
    pen.configure(_pen_tool(pressure=False), pressure_override=False)
    assert pen.down(PageId(), 3.0, 3.0)["stroke_update"].pressure_enabled is False


def test_real_config_path_yields_variable_outline():
    # Slot flag off (as shipped) + global resolver on: the production
    # enablement path must produce a pressure-enabled stroke whose
    # outline varies with pressure.
    viewer = _make_viewer()
    try:
        ctrl = _resolving_controller(False, True)
        page = PageId()
        ctrl.handle_pen_press(page, 0.0, 0.0, 0.05, 100)
        ctrl.handle_pen_move(page, 10.0, 0.0, 1.0, 116)
        done = ctrl.handle_pen_release(
            page, 20.0, 0.0, 1.0, 132)["stroke_done"]
        assert done.pressure_enabled is True
        outline = viewer._pressure_outline(done)
        assert outline is not None and outline.dots == ()
        ys = sorted(p.y for p in outline.polygon if abs(p.x) < 1e-9)
        wide = sorted(p.y for p in outline.polygon if abs(p.x - 20.0) < 1e-9)
        assert ys[-1] - ys[0] < wide[-1] - wide[0]
    finally:
        viewer.close()
