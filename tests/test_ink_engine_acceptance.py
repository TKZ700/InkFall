"""Acceptance contract for the recovered ink kernel (handoff §11).

Ported from ``handoff/acceptance_test.py``. Every expected number is
exact observed behaviour of the native engine (Drawboard PDF 6.91.6.0)
or exact geometry — **do not adjust the tolerances**. If an assertion
here fails, the kernel is wrong, not the test.

T1  constant width -> rectangle of exactly 2w thickness
T2  every corner sits exactly w from the centreline
T3  tapered span: tangency still exact
T4  native rejection rule: dw^2 >= L^2 drops the span
T5  quad assembly order, closed, four segments
T6  one quad per span; round caps add exactly two discs
T7  simplify: collinear collapses, default tol 0.2
T8  smoothing: 40 deg corner split, gentle curve -> cubics
T9  bezier flattening: endpoints preserved, no NaN
T10 SVG serialisation grammar
T11 full stroke -> NonZero path, no NaN, expected polygon count
"""

from __future__ import annotations

import math

import pytest

from annotations.ink_engine import (
    BEZIER,
    LINE,
    MOVE,
    Path,
    Point,
    arc_cap,
    flatten_cubic,
    path_to_svg_data,
    polygons_to_svg,
    segment_path,
    segment_quad,
    simplify,
    smooth_polyline,
    span_corners,
    stroke_polygons,
)

TOL = 1e-9


def _area(poly):
    total = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i].x, poly[i].y
        x2, y2 = poly[(i + 1) % len(poly)].x, poly[(i + 1) % len(poly)].y
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _dist_point_line(p, a, b):
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    length = math.hypot(bx - ax, by - ay)
    return abs((p.x - ax) * (by - ay) - (p.y - ay) * (bx - ax)) / length


# ---------------------------------------------------------------- T1
def test_t1_constant_width_is_exact_rectangle():
    a, b = Point(0, 0, 5.0), Point(100, 0, 5.0)
    p0, p1, p2, p3 = segment_quad(a, b)
    assert p0.x == pytest.approx(0.0, abs=TOL)
    assert p0.y == pytest.approx(5.0, abs=TOL)
    assert p1.x == pytest.approx(100.0, abs=TOL)
    assert p1.y == pytest.approx(5.0, abs=TOL)
    assert p2.x == pytest.approx(0.0, abs=TOL)
    assert p2.y == pytest.approx(-5.0, abs=TOL)
    assert p3.x == pytest.approx(100.0, abs=TOL)
    assert p3.y == pytest.approx(-5.0, abs=TOL)
    # area is exactly 2*w*L
    assert _area([p0, p2, p3, p1]) == pytest.approx(1000.0, abs=TOL)


# ---------------------------------------------------------------- T2
def test_t2_every_corner_sits_exactly_w_from_centreline():
    a, b = Point(0, 0, 5.0), Point(100, 0, 5.0)
    for p in segment_quad(a, b):
        assert _dist_point_line(p, a, b) == pytest.approx(5.0, abs=TOL)


# ---------------------------------------------------------------- T3
def test_t3_tapered_span_tangency_is_exact():
    a, b = Point(0, 0, 8.0), Point(60, 20, 2.0)
    q = segment_quad(a, b)
    length = math.hypot(60, 20)
    for name, p, radius in (("p0", q[0], 8.0), ("p1", q[1], 2.0)):
        end = a if name == "p0" else b
        assert math.hypot(p.x - end.x, p.y - end.y) == pytest.approx(
            radius, abs=TOL)
    for i in (0, 2):
        n = ((q[i].x - a.x) / a.w, (q[i].y - a.y) / a.w)
        assert (60 * n[0] + 20 * n[1]) / length == pytest.approx(
            (8.0 - 2.0) / length, abs=1e-12)


# ---------------------------------------------------------------- T4
def test_t4_native_rejection_rule_drops_the_span():
    # dw=10 over L=5: the narrow circle sits inside the wide one.
    assert segment_quad(Point(0, 0, 10), Point(3, 4, 0)) is None
    # dw=3 over L=5 is still inside the guard.
    assert segment_quad(Point(0, 0, 4), Point(3, 4, 1)) is not None


# ---------------------------------------------------------------- T5
def test_t5_quad_assembly_order():
    path = segment_path(Point(0, 0, 2), Point(50, 0, 2))
    assert isinstance(path, Path)
    assert path.segments[0].type == MOVE
    assert len(path.segments) == 4
    assert path.closed is True
    seq = [path.segments[0].pts[0]] + [s.pts[1] for s in path.segments[1:]]
    order = [(round(p.x, 6), round(p.y, 6)) for p in seq]
    assert order == [(0.0, 2.0), (0.0, -2.0), (50.0, -2.0), (50.0, 2.0)]


# ---------------------------------------------------------------- T6
def test_t6_one_quad_per_span_and_two_cap_discs():
    samples = [Point(0, 0, 1), Point(10, 0, 2), Point(20, 5, 3),
               Point(30, 5, 4)]
    assert len(stroke_polygons(samples)) == 3
    assert len(stroke_polygons(samples, round_caps=True)) == 5


# ---------------------------------------------------------------- T7
def test_t7_simplify_collinear_and_default_tolerance():
    line = [Point(i, 0, 2) for i in range(11)]
    assert len(simplify(line)) == 2
    # 0.1 deviation is below the 0.2 tolerance -> dropped
    assert len(simplify([Point(0, 0, 1), Point(5, 0.1, 1),
                         Point(10, 0, 1)])) == 2
    # 1.0 deviation is above it -> kept
    assert len(simplify([Point(0, 0, 1), Point(5, 1.0, 1),
                         Point(10, 0, 1)])) == 3


# ---------------------------------------------------------------- T8
def test_t8_smoothing_gentle_curve_and_corner_split():
    gentle = [Point(x, 20 * math.sin(x / 40.0), 2)
              for x in range(0, 200, 8)]
    beziers = sum(1 for s in smooth_polyline(gentle) if s.type == BEZIER)
    assert beziers > 5
    corner = [Point(0, 0, 2), Point(50, 0, 2), Point(50, 50, 2),
              Point(100, 50, 2)]
    moves = sum(1 for s in smooth_polyline(corner) if s.type == MOVE)
    assert moves >= 2


# ---------------------------------------------------------------- T9
def test_t9_flatten_preserves_endpoints_without_nan():
    p0, p1 = Point(0, 0, 0), Point(0, 100, 0)
    p2, p3 = Point(100, 100, 0), Point(100, 0, 0)
    flat = flatten_cubic(p0, p1, p2, p3, tol=0.01)
    assert (flat[-1].x, flat[-1].y) == (100.0, 0.0)
    assert all(not math.isnan(p.x) and not math.isnan(p.y) for p in flat)


# ---------------------------------------------------------------- T10
def test_t10_svg_grammar():
    first = segment_path(Point(0, 0, 2), Point(10, 0, 2))
    data = path_to_svg_data([first.segments[0], first.segments[1]])
    assert data.startswith("M ")
    assert "L " in data
    assert data.endswith("Z ")
    assert ", " in data


# ---------------------------------------------------------------- T11
def test_t11_full_stroke_nonzero_no_nan_polygon_count():
    pts = [Point(60, 200, 1), Point(140, 120, 7), Point(240, 190, 14),
           Point(340, 120, 6), Point(430, 200, 1.5), Point(520, 140, 10)]
    polys = stroke_polygons(pts, round_caps=True)
    svg = polygons_to_svg(polys)
    assert 'fill-rule="nonzero"' in svg
    assert "nan" not in svg.lower()
    assert len(polys) == len(pts) - 1 + 2


# ------------------------------------------------- shipped deviation (§5)
def test_guard_rejects_but_render_path_stays_continuous():
    """The native guard drops the span; the shipped quad never does.

    Handoff §5: dropping leaves a gap on fast pressure flicks, so the
    renderer clamps the tangent slope instead. The quad must stay
    finite and keep both end radii.

    Note the handoff proposes recursive midpoint subdivision here, but
    the guard predicate ``(dw)^2 >= (L)^2`` is scale invariant: halving
    the span halves the width delta too, so the ratio never converges
    and the recursion always bottoms out at the depth cap (measured:
    791 ring vertices for one such span). Clamping is the O(1)
    equivalent, and a clamped span is exactly the limiting cone the
    subdivision would converge to.
    """
    a, b = Point(0, 0, 10.0), Point(3, 4, 0.5)
    assert segment_quad(a, b) is None          # native behaviour
    corners = span_corners(a.x, a.y, a.w, b.x, b.y, b.w)
    assert corners is not None
    p0, p1, p2, p3 = corners
    for x, y in corners:
        assert math.isfinite(x) and math.isfinite(y)
    # Both end radii are still honoured exactly.
    assert math.hypot(p0[0] - a.x, p0[1] - a.y) == pytest.approx(10.0,
                                                               abs=TOL)
    assert math.hypot(p2[0] - a.x, p2[1] - a.y) == pytest.approx(10.0,
                                                               abs=TOL)
    assert math.hypot(p1[0] - b.x, p1[1] - b.y) == pytest.approx(0.5,
                                                               abs=TOL)
    assert math.hypot(p3[0] - b.x, p3[1] - b.y) == pytest.approx(0.5,
                                                               abs=TOL)
