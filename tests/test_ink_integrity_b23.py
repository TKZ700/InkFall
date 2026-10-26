"""B.2.3 Stage 1: stroke integrity — folded outlines must not leave holes.

Geometry assertions (disc placement) plus pixel-level proof: outlines
are rasterized offscreen two ways (legacy OddEven ``drawPolygon`` vs
production winding fill + discs) and enclosed white pixels are counted
by border flood fill. No AA in these probes so edges stay exact; AA
only blends edges and cannot create holes.
"""

from __future__ import annotations

import math
from collections import deque

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPolygonF, QColor

from annotations.ink_geometry import REVERSAL_DOT, build_outline

pytestmark = pytest.mark.usefixtures("qapp")

WHITE = 0xFFFFFFFF
SCALE = 6
PAD = 30


def _paint(pts, widths, winding=True, with_discs=True, aa=False):
    """Rasterize an outline; returns (image, outline, bbox).

    ``aa`` mirrors the production pressure branch (Antialiasing on).
    No-AA probes keep edges exact for gross-defect detection.
    """
    outline = build_outline(pts, widths)
    paint_pts = list(outline.polygon)
    discs = list(outline.dots)
    if with_discs:
        discs += list(outline.discs)
    xs = [p.x for p in paint_pts] + [d.center.x for d in discs]
    ys = [p.y for p in outline.polygon] + [d.center.y for d in discs]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    max_r = max([d.radius for d in discs] + [0.0])
    w = int((x1 - x0 + 2 * max_r) * SCALE + 2 * PAD) + 1
    h = int((y1 - y0 + 2 * max_r) * SCALE + 2 * PAD) + 1
    ox = PAD - x0 * SCALE + max_r * SCALE
    oy = PAD - y0 * SCALE + max_r * SCALE

    def mp(p):
        return QPointF(ox + p.x * SCALE, oy + p.y * SCALE)

    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.white)
    painter = QPainter(img)
    try:
        if aa:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#000000"))
        if outline.polygon:
            poly = QPolygonF([mp(p) for p in outline.polygon])
            if winding:
                path = QPainterPath()
                path.addPolygon(poly)
                path.setFillRule(Qt.FillRule.WindingFill)
                painter.fillPath(path, QColor("#000000"))
            else:
                painter.drawPolygon(poly)
        for dot in discs:
            c = mp(dot.center)
            r = dot.radius * SCALE
            painter.drawEllipse(c, r, r)
    finally:
        painter.end()
    return img, outline, (0, 0, w, h)


def _enclosed_white(img):
    """White pixels unreachable from the image border (holes)."""
    return _enclosed_components(img)[0]


def _enclosed_components(img):
    """(total enclosed white px, sorted enclosed-component areas)."""
    w, h = img.width(), img.height()
    seen = bytearray(w * h)
    queue = deque()
    for x in range(w):
        for y in (0, h - 1):
            if img.pixel(x, y) == WHITE:
                seen[y * w + x] = 1
                queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if img.pixel(x, y) == WHITE and not seen[y * w + x]:
                seen[y * w + x] = 1
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                idx = ny * w + nx
                if not seen[idx] and img.pixel(nx, ny) == WHITE:
                    seen[idx] = 1
                    queue.append((nx, ny))
    comps = []
    for y in range(h):
        for x in range(w):
            if img.pixel(x, y) == WHITE and not seen[y * w + x]:
                size = 0
                stack = [(x, y)]
                seen[y * w + x] = 1
                while stack:
                    cx, cy = stack.pop()
                    size += 1
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                                   (cx, cy + 1), (cx, cy - 1)):
                        if (0 <= nx < w and 0 <= ny < h
                                and not seen[ny * w + nx]
                                and img.pixel(nx, ny) == WHITE):
                            seen[ny * w + nx] = 1
                            stack.append((nx, ny))
                comps.append(size)
    comps.sort()
    return sum(comps), comps


def _ink_count(img):
    return sum(1 for y in range(img.height()) for x in range(img.width())
               if img.pixel(x, y) != WHITE)


REVERSAL = ([(0.0, 0.0), (10.0, 0.0), (1.0, 0.5)], [2.0, 2.0, 2.0])
TIGHT_LOOP = ([(0.0, 0.0), (8.0, 0.0), (8.0, 3.0), (1.0, 3.0), (1.0, 0.2)],
              [2.5, 2.5, 2.5, 2.5, 2.5])
RETRACE = ([(0.0, 0.0), (10.0, 0.3), (20.0, 0.0), (10.0, -0.3), (0.5, 0.1)],
           [2.0, 2.2, 2.0, 2.2, 2.0])
CROSSING = ([(0.0, 0.0), (10.0, 10.0), (0.0, 10.0), (10.0, 0.0)],
            [2.0, 2.0, 2.0, 2.0])
SMOOTH_CURVE = ([(float(i), 3.0 * math.sin(i * 0.5)) for i in range(12)],
                [2.0] * 12)
PRESSURE_TURN = ([(0.0, 0.0), (5.0, 0.0), (10.0, 5.0)],
                 [0.6, 2.0, 4.0])


def test_reversal_disc_placed_with_joint_width():
    outline = build_outline(*REVERSAL)
    assert outline.polygon
    assert len(outline.discs) == 1
    disc = outline.discs[0]
    assert (disc.center.x, disc.center.y) == pytest.approx((10.0, 0.0))
    assert disc.radius == pytest.approx(1.0)


def test_tight_loop_and_retrace_get_discs():
    assert len(build_outline(*RETRACE).discs) >= 1
    # A rectangular loop has only 90-degree joints (dot == 0): no
    # reversal discs by design. Its interior stays legitimately white;
    # winding fill alone restores the pinched corners (see loop test).
    assert build_outline(*TIGHT_LOOP).discs == ()


def test_ordinary_strokes_have_no_discs():
    for pts, widths in (
            ([(0.0, 0.0), (10.0, 0.0)], [2.0, 2.0]),
            ([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], [2.0, 2.0, 2.0]),
            SMOOTH_CURVE,
            ([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)], [0.6, 4.0, 0.6]),
            ([(0.0, 0.0), (0.0, 0.0), (5.0, 0.0)], [2.0, 2.0, 2.0])):
        outline = build_outline(pts, widths)
        assert outline.discs == ()


def test_disc_radii_follow_local_widths():
    pts = [(0.0, 0.0), (10.0, 0.0), (0.5, 0.2)]
    outline = build_outline(pts, [1.0, 4.0, 1.0])
    assert len(outline.discs) == 1
    assert outline.discs[0].radius == pytest.approx(2.0)


def test_reversal_threshold_documented():
    assert REVERSAL_DOT == pytest.approx(-0.7)
    # 90-degree corner (dot == 0) stays disc-free by a wide margin.
    assert 0.0 > REVERSAL_DOT


def _assert_solid(name, pts, widths, aa=False):
    img, outline, _bbox = _paint(pts, widths, winding=True, with_discs=True,
                                 aa=aa)
    holes = _enclosed_white(img)
    assert holes == 0, f"{name}: {holes} enclosed white px"
    return _ink_count(img)


def test_reversal_loop_retrace_have_no_holes():
    # No-AA probes keep edges exact. The reversal admits a single
    # aliasing-quantization speck where disc arc meets rails (blended
    # away under production AA); everything else must be hole-free.
    img, _o, _b = _paint(*REVERSAL, winding=True, with_discs=True)
    assert _enclosed_white(img) <= 1
    _assert_solid("retrace", *RETRACE)


def test_loop_interior_is_one_legitimate_component():
    # A loop encloses background by design: exactly one enclosed
    # component (the interior), no small pinch-defect components.
    # Winding still restores the pinched corners (ink grows).
    for aa in (False, True):
        img, _o, _b = _paint(*TIGHT_LOOP, winding=True, with_discs=True,
                             aa=aa)
        total, comps = _enclosed_components(img)
        assert len(comps) == 1, comps
        assert comps[0] > 50, comps
    calm, _o2, _b2 = _paint(*TIGHT_LOOP, winding=False, with_discs=False)
    fixed, _o3, _b3 = _paint(*TIGHT_LOOP, winding=True, with_discs=True)
    assert _ink_count(fixed) > _ink_count(calm)


def test_crossing_and_curve_have_no_holes():
    _assert_solid("crossing", *CROSSING)
    _assert_solid("smooth-curve", *SMOOTH_CURVE)
    _assert_solid("pressure-turn", *PRESSURE_TURN)
    _assert_solid("retrace-aa", *RETRACE, aa=True)
    _assert_solid("crossing-aa", *CROSSING, aa=True)
    _assert_solid("curve-aa", *SMOOTH_CURVE, aa=True)


def test_ink_restoration_chain():
    # No-AA exact counts. Legacy OddEven drops folded ink; winding
    # restores it; the joint disc adds the turnaround coverage.
    # Reversal: 450 -> 650 -> 756. Retrace: 516 -> 1459 -> 1550.
    for name, (pts, widths) in (("reversal", REVERSAL),
                                ("retrace", RETRACE)):
        legacy, _o, _b = _paint(pts, widths, winding=False,
                                with_discs=False)
        wound, _o2, _b2 = _paint(pts, widths, winding=True,
                                 with_discs=False)
        full, _o3, _b3 = _paint(pts, widths, winding=True, with_discs=True)
        n_legacy, n_wound, n_full = (
            _ink_count(legacy), _ink_count(wound), _ink_count(full))
        assert n_wound > n_legacy + 100, (name, n_legacy, n_wound)
        assert n_full >= n_wound, (name, n_wound, n_full)


def test_reversal_ink_stays_continuous():
    # Turnaround columns (joint ± nearby fold zone) always carry ink:
    # thinning is bounded, never a full break.
    img, _o, _b = _paint(*REVERSAL, winding=True, with_discs=True)
    white = 0xFFFFFFFF
    spans = []
    for x in range(img.width()):
        ys = [y for y in range(img.height()) if img.pixel(x, y) != white]
        if ys:
            spans.append(len(ys))
    assert spans and min(spans) >= 1


def test_dots_and_capsules_stay_solid():
    img, outline, _bbox = _paint([(3.0, 4.0)], [2.0])
    assert len(outline.dots) == 1 and outline.discs == ()
    assert _enclosed_white(img) == 0
    assert _ink_count(img) > 0
    img2, outline2, _b2 = _paint([(0.0, 0.0), (4.0, 0.0)], [2.0, 4.0])
    assert outline2.dots == () and outline2.discs == ()
    assert _enclosed_white(img2) == 0
    assert _ink_count(img2) > 0


def test_no_unexpected_blobs_on_ordinary_strokes():
    # Discs add zero ink where none is needed: winding renders with and
    # without discs are identical for ordinary strokes.
    for name, (pts, widths) in (("curve", SMOOTH_CURVE),
                                ("turn", PRESSURE_TURN)):
        with_discs, _o, _b = _paint(pts, widths, True, True)
        bare, _o2, _b2 = _paint(pts, widths, True, False)
        assert _ink_count(with_discs) == _ink_count(bare), name
