"""M5-B.2.8: incremental live-outline assembly + live paint memos.

Proves the B.2.8 caches reuse only splice-verified geometry:
per-prefix ``==`` against the full-rebuild oracle (the real gate),
``is``-shared stable prefixes (reuse proof, not timing), tail growth
bounded by the incremental window constants, invalidation on every
mutation, and viewer pixel equivalence through the memoized paths.
"""

from __future__ import annotations

import math
import random

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter

from annotations.ink_geometry import (
    LiveOutline,
    build_outline,
    smooth_centerline,
)
from annotations.interaction import InteractionController
from core.types import PageId
from tests.helpers import committed_strokes
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

# Tail bound from the incremental mechanism itself: the rails window
# re-derives _RAIL_WINDOW + 10 smoothed entries plus 2 context points,
# the smooth vein re-derives _OVERLAP_KEPT kept entries (each Chaikin
# pass at most doubles: 2 ** passes), caps contribute cap_segments.
# Anything near the full polygon length means a silent full rebuild.
_OVERLAP_KEPT = LiveOutline._OVERLAP_KEPT
_RAIL_WINDOW = LiveOutline._RAIL_WINDOW
_TAIL_BOUND = (_OVERLAP_KEPT * (2 ** 2) + _RAIL_WINDOW + 10 + 2) * 2 + 16


def _oracle(pts, widths, cap="round"):
    spts, svals = smooth_centerline(pts, widths)
    return build_outline(spts, svals if svals is not None else widths,
                         cap=cap)


def _random_stroke(rng, n):
    pts, widths = [], []
    x, y = 0.0, 0.0
    for _ in range(n):
        r = rng.random()
        if r < 0.08 and pts:
            x, y = pts[rng.randrange(len(pts))]
        elif r < 0.15 and pts:
            x += rng.uniform(-1e-10, 1e-10)
            y += rng.uniform(-1e-10, 1e-10)
        elif r < 0.25:
            x += rng.uniform(-0.2, 0.2)
            y += rng.uniform(-0.2, 0.2)
        else:
            x += rng.uniform(-4.0, 6.0)
            y += rng.uniform(-4.0, 4.0)
        pts.append((x, y))
        widths.append(rng.choice([0.5, 1.0, 2.0, 4.0, 8.0]))
    return pts, widths


@pytest.mark.parametrize("cap", ["round", "butt"])
@pytest.mark.parametrize("seed", [7, 1234, 99991])
@pytest.mark.parametrize("n", [5, 17, 40, 120])
def test_cached_assembly_matches_oracle_every_prefix(cap, seed, n):
    rng = random.Random(seed)
    pts, widths = _random_stroke(rng, n)
    live = LiveOutline(cap=cap)
    for k in range(1, n + 1):
        live.append(pts[k - 1][0], pts[k - 1][1], widths[k - 1])
        got = live.outline()
        want = _oracle(pts[:k], widths[:k], cap)
        assert got == want, f"cap={cap} seed={seed} n={n} k={k}"


def test_stable_prefix_shared_and_tail_bounded():
    pts = [(60.0 + i * 0.25, 400.0 + (i % 11) * 1.0) for i in range(300)]
    widths = [2.0] * 300
    live = LiveOutline()
    prev = None
    max_new = 0
    for k in range(1, 301):
        live.append(pts[k - 1][0], pts[k - 1][1], widths[k - 1])
        cur = live.outline()
        assert cur == _oracle(pts[:k], widths[:k]), f"k={k}"
        if prev is not None and len(prev.polygon) >= 100:
            # Shared Point OBJECTS (by identity) between consecutive
            # pulls: only the tail window is freshly wrapped. Polygon
            # order places the right rail after the rebuilt end cap, so
            # a leading-run count would undercount sharing — count by
            # identity instead. A silent full rebuild shares ~0.
            prev_ids = {id(p) for p in prev.polygon}
            new = sum(1 for p in cur.polygon if id(p) not in prev_ids)
            assert new <= _TAIL_BOUND, (
                f"k={k} new={new} cur={len(cur.polygon)}")
            assert new < len(cur.polygon) // 2, (f"k={k} new={new}")
            max_new = max(max_new, new)
            # The stable head is also a leading run in polygon order
            # (left rail comes first): spot-check the first vertex.
            assert cur.polygon[0] is prev.polygon[0], f"k={k}"
        prev = cur
    assert live.fallbacks == 0
    assert max_new <= _TAIL_BOUND, max_new


def test_reversal_heavy_prefix_sharing_and_discs():
    rng = random.Random(31337)
    pts, widths = _random_stroke(rng, 150)
    live = LiveOutline()
    for k in range(1, 151):
        live.append(pts[k - 1][0], pts[k - 1][1], widths[k - 1])
        cur = live.outline()
        assert cur == _oracle(pts[:k], widths[:k]), f"k={k}"


def test_short_stroke_transitions():
    live = LiveOutline()
    with pytest.raises(ValueError):
        live.outline()
    live.append(0.0, 0.0, 2.0)
    assert live.outline() == _oracle([(0.0, 0.0)], [2.0])
    live.append(0.0, 0.0, 2.0)  # exact duplicate
    assert live.outline() == _oracle([(0.0, 0.0), (0.0, 0.0)], [2.0, 2.0])
    pts = [(float(i), 0.0) for i in range(40)]
    for i, (x, y) in enumerate(pts[2:], start=2):
        live.append(x, y, 2.0)
        assert live.outline() == _oracle(
            [(0.0, 0.0), (0.0, 0.0)] + pts[2:i + 1],
            [2.0] * (i + 1)), f"i={i}"


def test_invalidation_on_reset_refeed_and_restart():
    from annotations.interaction import PenHandler

    tool = ToolInstance(kind=ToolType.PEN, name="P", color="#1b1e26",
                        width_mm=3.0, opacity=1.0, pressure=True)
    pen = PenHandler(ToolType.PEN)
    pen.configure(tool)
    page = PageId()
    pts = [(float(i * 2.0), float((i % 5) * 1.5)) for i in range(30)]
    pen.down(page, pts[0][0], pts[0][1], 0.5, 0)
    for i in range(1, 30):
        pen.move(page, pts[i][0], pts[i][1], 0.5, i * 8)
    first = pen.live_outline()
    assert first is not None
    # Mid-stroke width edit refeeds: geometry matches a fresh oracle
    # built with the new base width.
    pen.configure(ToolInstance(kind=ToolType.PEN, name="P",
                               color="#1b1e26", width_mm=6.0, opacity=1.0,
                               pressure=True))
    refed = pen.live_outline()
    assert refed is not None and refed != first
    from annotations.ink_geometry import pressure_widths
    base6 = 6.0 * 72.0 / 25.4
    assert refed == _oracle(
        pts, list(pressure_widths((0.5,) * 30, base6, "normal")))
    # Cancel then restart: no stale state leaks into the new stroke.
    pen.cancel()
    assert pen.live_outline() is None
    pen.down(page, 500.0, 500.0, 0.5, 9999)
    fresh = pen.live_outline()
    assert fresh is not None and fresh != first
    base6 = 6.0 * 72.0 / 25.4
    # Single-sample strokes bypass smoothing (documented precondition).
    assert fresh == _oracle(
        [(500.0, 500.0)],
        list(pressure_widths((0.5,), base6, "normal")))
    pen.up(page, 510.0, 510.0, 0.5, 10007)
    assert pen.live_outline() is None


def _pen_tool(pressure, width_mm=3.0, opacity=1.0):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#1b1e26",
                        width_mm=width_mm, opacity=opacity,
                        pressure=pressure)


def _open(qtbot, tmp_path, tool, name="c28.pdf"):
    from app.main_window import MainWindow
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / name), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool)
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=20000)
    vr = viewer.rect()
    vw, vh = vr.width(), vr.height()

    def _paint(dirty):
        img = QImage(vw, vh, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        try:
            viewer._paint_body(p, QRect(dirty) if dirty is not None else None)
        finally:
            p.end()
        return img

    for _ in range(30):
        _paint(None)
        if viewer.painted_placeholders == 0 and viewer.painted_tiles > 0:
            qtbot.wait(150)
            _paint(None)
            if viewer.painted_placeholders == 0:
                break
        qtbot.wait(150)
    return window, viewer, _paint


def _paint_dirty_onto(viewer, base, dirty):
    img = base.copy()
    p = QPainter(img)
    try:
        # Production paintEvents arrive pre-clipped by Qt; replicate
        # the clip or the unconditional page fill erases surviving
        # pixels outside dirty (harness artifact, not paint behavior).
        p.setClipRect(QRect(dirty))
        viewer._paint_body(p, QRect(dirty))
    finally:
        p.end()
    return img


def test_off_long_stroke_live_completed_identity(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(False))
    try:
        page = viewer._pipeline.layout.entry(0).page_id
        it = viewer._interaction
        calls = []
        viewer.update = lambda *a: calls.append(tuple(a))
        pts = [(60.0 + i * 0.9, 140.0 + (i % 9) * 2.0) for i in range(600)]
        viewer._apply_intent(it.handle_pen_press(page, pts[0][0], pts[0][1],
                                                 0.6, 1000))
        for i in range(1, 600):
            viewer._apply_intent(it.handle_pen_move(page, pts[i][0],
                                                    pts[i][1], 0.6,
                                                    1000 + i * 8))
        live_full = _paint(None)
        # The OFF live path memoized its smoothing without changing it.
        assert viewer._live_smooth_memo is not None
        viewer._apply_intent(it.handle_pen_release(page, pts[-1][0],
                                                   pts[-1][1], 0.6, 99999))
        done_full = _paint(None)
        assert live_full.bits().tobytes() == done_full.bits().tobytes()
        done_rect = calls[-1][0] if calls and calls[-1] else None
        assert done_rect is not None
        assert _paint_dirty_onto(viewer, live_full,
                                 done_rect).bits().tobytes() == \
            done_full.bits().tobytes()
        # Memos released with the stroke: no dead-stroke pinning.
        assert viewer._live_extents_memo is None
        assert viewer._live_smooth_memo is None
        assert committed_strokes(viewer)[0].pressure_enabled is False
    finally:
        viewer.close_document()
        window.close()


def test_viewer_memos_hit_turnover_and_clear(qtbot, tmp_path):
    window, viewer, _paint = _open(qtbot, tmp_path, _pen_tool(True),
                                   name="c28b.pdf")
    try:
        page = viewer._pipeline.layout.entry(0).page_id
        it = viewer._interaction
        pts = [(300.0 + i * 1.5, 200.0 + (i % 7) * 2.0) for i in range(60)]
        viewer._apply_intent(it.handle_pen_press(page, pts[0][0], pts[0][1],
                                                 0.5, 5000))
        for i in range(1, 20):
            viewer._apply_intent(it.handle_pen_move(page, pts[i][0],
                                                    pts[i][1], 0.5,
                                                    5000 + i * 8))
        full_a = _paint(None)
        first_outline = it.live_outline()
        assert viewer._live_extents_memo is not None
        assert viewer._live_extents_memo[0] is first_outline
        # Second paint without a move: identical memo object reused.
        full_b = _paint(None)
        assert viewer._live_extents_memo[0] is first_outline
        assert full_a.bits().tobytes() == full_b.bits().tobytes()
        # A move installs a new outline object: memo turns over and the
        # dirty repaint still matches a full repaint pixel for pixel.
        viewer._apply_intent(it.handle_pen_move(page, pts[20][0],
                                                pts[20][1], 0.5, 5160))
        second_outline = it.live_outline()
        assert second_outline is not first_outline
        full_c = _paint(None)
        assert viewer._live_extents_memo[0] is second_outline
        tail = viewer._live_update_rect(viewer._live_stroke)
        assert _paint_dirty_onto(viewer, full_b,
                                 tail).bits().tobytes() == \
            full_c.bits().tobytes()
        # Cancel clears memos; close clears memos.
        viewer._apply_intent(it.cancel())
        assert viewer._live_extents_memo is None
        assert viewer._live_smooth_memo is None
        viewer._apply_intent(it.handle_pen_press(page, pts[0][0], pts[0][1],
                                                 0.5, 9000))
        viewer._apply_intent(it.handle_pen_move(page, pts[1][0], pts[1][1],
                                                0.5, 9008))
        _paint(None)
        assert viewer._live_extents_memo is not None
        viewer.close_document()
        assert viewer._live_extents_memo is None
        assert viewer._live_smooth_memo is None
    finally:
        try:
            viewer.close_document()
        except Exception:
            pass
        window.close()
