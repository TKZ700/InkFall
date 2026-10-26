"""B.2.3 Stage 3: live incremental outline wiring + latency benchmarks.

Proves the viewer paints the cached incremental outline for live
strokes, mid-stroke width edits refeed exactly, cancels/resets leave
no stale geometry — and reports before/after timings. Timing asserts
use generous guards (loaded CI); actuals are printed for the report.
"""

from __future__ import annotations

import time

import pytest

from annotations.interaction import InteractionController
from core.types import PageId
from tests.helpers import committed_strokes
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


def _pen_tool(pressure=True, width_mm=3.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#ff0000",
                        width_mm=width_mm, opacity=1.0, pressure=pressure)


def _drive(ctrl, page, samples):
    """Feed (x, y, pressure) samples; returns (live_list, done)."""
    lives = []
    t = 1000
    lives.append(ctrl.handle_pen_press(page, *samples[0], t)["stroke_update"])
    for sample in samples[1:]:
        t += 16
        intent = ctrl.handle_pen_move(page, *sample, t)
        if "stroke_update" in intent:
            lives.append(intent["stroke_update"])
    t += 16
    done = ctrl.handle_pen_release(page, *samples[-1], t)["stroke_done"]
    return lives, done


def test_live_outline_matches_full_on_every_update():
    page = PageId()
    ctrl = InteractionController(_pen_tool(), profile_resolver=lambda: "soft")
    samples = [(float(i * 3), float((i * 37) % 11), 0.1 + 0.8 * (i % 5) / 4)
               for i in range(30)]
    ctrl.handle_pen_press(page, *samples[0], 1000)
    t = 1000
    for sample in samples[1:]:
        t += 16
        ctrl.handle_pen_move(page, *sample, t)
        pen = ctrl._pen_handler()
        live = pen.live_outline()
        assert live is not None
        full = _oracle_for(pen)
        assert live == full


def _oracle_for(pen):
    from annotations.ink_geometry import build_outline, smooth_centerline
    from annotations.interaction import PenHandler as _PH  # noqa: F401

    pts = [(p.x, p.y) for p in pen._points]
    base = float(pen._width_mm) * 72.0 / 25.4
    from annotations.ink_geometry import pressure_width
    widths = tuple(pressure_width(p, base, pen._stroke_profile)
                   for p in pen._pressures)
    spts, svals = smooth_centerline(pts, widths)
    return build_outline(spts, svals if svals is not None else widths)


def test_midstroke_width_edit_refeeds_exactly():
    from dataclasses import replace

    page = PageId()
    tool = _pen_tool(width_mm=3.0)
    ctrl = InteractionController(tool)
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.5, 1000)
    ctrl.handle_pen_move(page, 10.0, 0.0, 0.7, 1016)
    # Live width edit on the same slot (no cancel): preview must adopt
    # the new width exactly, matching a full rebuild with it.
    ctrl.set_tool(replace(tool, width_mm=6.0))
    ctrl.handle_pen_move(page, 20.0, 0.0, 0.7, 1032)
    pen = ctrl._pen_handler()
    assert pen.live_outline() == _oracle_for(pen)


def test_disabled_stroke_has_no_live_outline():
    page = PageId()
    ctrl = InteractionController(_pen_tool(pressure=False))
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.5, 1000)
    pen = ctrl._pen_handler()
    assert pen.live_outline() is None


def test_cancel_and_down_reset_live_outline():
    page = PageId()
    ctrl = InteractionController(_pen_tool())
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.5, 1000)
    ctrl.handle_pen_move(page, 10.0, 0.0, 0.5, 1016)
    assert ctrl._pen_handler().live_outline() is not None
    ctrl.cancel()
    assert ctrl._pen_handler().live_outline() is None
    ctrl.handle_pen_press(page, 50.0, 50.0, 0.5, 2000)
    outline = ctrl._pen_handler().live_outline()
    assert outline is not None
    assert outline == _oracle_for(ctrl._pen_handler())


def test_viewer_paints_live_incrementally(qtbot, tmp_path, cleared_settings):
    """Live paints must not run full rebuilds; done strokes still do."""
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / "live.pdf"), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    try:
        assert window.viewer.open_document(pdf) is True
        window._stack.setCurrentIndex(1)
        viewer = window.viewer
        viewer._interaction.set_tool(_pen_tool())
        calls = []
        original = viewer._pressure_outline

        def counting(stroke):
            calls.append(1)
            return original(stroke)

        viewer._pressure_outline = counting
        page = viewer._pipeline.layout.entry(0).page_id
        viewer._apply_intent(viewer._interaction.handle_pen_press(
            page, 100.0, 250.0, 0.3, 1000))
        viewer._apply_intent(viewer._interaction.handle_pen_move(
            page, 200.0, 250.0, 0.9, 1016))
        live = viewer._live_stroke
        assert live is not None
        viewer.update()
        qtbot.wait(150)
        viewer.grab()
        live_calls = len(calls)
        # The live stroke paints from the incremental builder.
        assert live_calls == 0, live_calls
        viewer._apply_intent(viewer._interaction.handle_pen_release(
            page, 200.0, 250.0, 0.9, 1032))
        viewer.update()
        qtbot.wait(150)
        viewer.grab()
        # The finalized stroke goes through the full rebuild (once per
        # paint, as before — completed strokes are not incremental).
        assert len(calls) > live_calls
        assert len(committed_strokes(viewer)) == 1
    finally:
        try:
            viewer._pressure_outline = original
        except Exception:
            pass
        viewer.close_document()
        window.close()


def _bench_stroke(n):
    import math

    return ([(i * 1.5, 1.5 * math.sin(i * 0.9) + 0.7 * math.sin(i * 2.3))
             for i in range(n)],
            [0.1 + 0.9 * ((i * 7919) % 100) / 99.0 for i in range(n)])


def test_benchmark_incremental_vs_full_rebuild():
    """Per-event append cost vs full rebuild (prints + gates).

    Lazy viewer semantics: appends run per event, outline assembly
    runs per paint (cached while clean). All three phases measured.
    """
    from annotations.ink_geometry import build_outline, smooth_centerline

    for n in (80, 300, 1000, 2000):
        pts, prs = _bench_stroke(n)
        base = 3.0 * 72.0 / 25.4
        from annotations.ink_geometry import pressure_widths
        widths = pressure_widths(prs, base, "normal")
        # BEFORE: full smooth + full outline, as the old paint path did.
        start = time.perf_counter()
        spts, svals = smooth_centerline(pts, widths)
        build_outline(spts, svals)
        full_ms = (time.perf_counter() - start) * 1000.0
        # AFTER: bounded appends per event + one assembly per paint.
        from annotations.ink_geometry import LiveOutline

        live = LiveOutline()
        worst_append = 0.0
        start = time.perf_counter()
        for i in range(n):
            tick = time.perf_counter()
            live.append(pts[i][0], pts[i][1], widths[i])
            worst_append = max(worst_append,
                               (time.perf_counter() - tick) * 1000.0)
        append_ms = (time.perf_counter() - start) * 1000.0
        tick = time.perf_counter()
        outline = live.outline()
        assembly_ms = (time.perf_counter() - tick) * 1000.0
        tick = time.perf_counter()
        live.outline()
        cached_ms = (time.perf_counter() - tick) * 1000.0
        print(f"\nn={n}: full-rebuild {full_ms:.2f}ms | append total "
              f"{append_ms:.2f}ms, worst append {worst_append:.2f}ms, "
              f"assembly {assembly_ms:.2f}ms, cached "
              f"{cached_ms:.3f}ms, fallbacks={live.fallbacks}")
        assert outline == _full_oracle(pts, widths)
        if n <= 500:
            assert worst_append < 2.0, f"n={n} worst {worst_append:.2f}ms"


def _full_oracle(pts, widths):
    from annotations.ink_geometry import build_outline, smooth_centerline

    spts, svals = smooth_centerline(pts, widths)
    return build_outline(spts, svals)


def test_benchmark_paint_cost_split():
    """Raster fill cost for the largest outline (informational)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QPainter, QPainterPath, QPolygonF

    pts, prs = _bench_stroke(2000)
    base = 3.0 * 72.0 / 25.4
    from annotations.ink_geometry import (
        LiveOutline,
        pressure_widths,
    )

    widths = pressure_widths(prs, base, "normal")
    live = LiveOutline()
    for i in range(2000):
        live.append(pts[i][0], pts[i][1], widths[i])
    outline = live.outline()
    poly = QPolygonF([QPointF(p.x, p.y) for p in outline.polygon])
    img = QImage(1200, 800, QImage.Format.Format_ARGB32_Premultiplied)
    start = time.perf_counter()
    painter = QPainter(img)
    try:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.black)
        path = QPainterPath()
        path.addPolygon(poly)
        path.setFillRule(Qt.FillRule.WindingFill)
        painter.fillPath(path, Qt.GlobalColor.black)
    finally:
        painter.end()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    print(f"\npaint 16k-vert winding fill: {elapsed_ms:.2f}ms")
