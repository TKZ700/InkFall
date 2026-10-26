"""B.2.1 Slice 2: variable-width outline builder tests (offscreen).

Covers ``annotations.ink_geometry.build_outline`` only: pure geometry,
no Qt, no viewer, no painting. Asserts geometric properties (extents,
symmetry, area, boundedness) — not exact-coordinate snapshots.
"""

from __future__ import annotations

import math

import pytest

from annotations.ink_geometry import StrokeOutline, build_outline
from core.types import Point


def _xs(outline):
    return [p.x for p in outline.polygon]


def _ys(outline):
    return [p.y for p in outline.polygon]


def _bbox(outline):
    return (min(_xs(outline)), max(_xs(outline)),
            min(_ys(outline)), max(_ys(outline)))


def _area(outline):
    pts = outline.polygon
    total = 0.0
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        total += a.x * b.y - b.x * a.y
    return abs(total) * 0.5


def _assert_finite(outline):
    for p in outline.polygon:
        assert math.isfinite(p.x) and math.isfinite(p.y)
    for dot in outline.dots:
        assert math.isfinite(dot.center.x) and math.isfinite(dot.center.y)
        assert math.isfinite(dot.radius) and dot.radius > 0


def _assert_bounded(outline, samples, max_half):
    """Every vertex lies within max_half of some centerline sample.

    Holds by construction (rails/caps are exact half-width offsets);
    guards against miter spikes and runaway arcs.
    """
    for p in outline.polygon:
        nearest = min(math.hypot(p.x - sx, p.y - sy) for sx, sy in samples)
        assert nearest <= max_half + 1e-9


def test_straight_horizontal_extents_round_cap():
    outline = build_outline([(0.0, 0.0), (10.0, 0.0)], [2.0, 2.0])
    assert isinstance(outline, StrokeOutline)
    assert outline.dots == ()
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert xmin == pytest.approx(-1.0)
    assert xmax == pytest.approx(11.0)
    assert ymin == pytest.approx(-1.0)
    assert ymax == pytest.approx(1.0)
    _assert_finite(outline)


def test_straight_horizontal_area_round_cap():
    # Rectangle 10x2 plus a full unit disc from the two semicircles.
    outline = build_outline([(0.0, 0.0), (10.0, 0.0)], [2.0, 2.0])
    assert _area(outline) == pytest.approx(20.0 + math.pi, abs=0.5)


def test_butt_cap_no_extension_exact_area():
    outline = build_outline([(0.0, 0.0), (10.0, 0.0)], [2.0, 2.0],
                            cap="butt")
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert (xmin, xmax, ymin, ymax) == pytest.approx((0.0, 10.0, -1.0, 1.0))
    assert _area(outline) == pytest.approx(20.0)
    assert len(outline.polygon) == 4


def test_vertical_stroke_symmetry():
    outline = build_outline([(5.0, 0.0), (5.0, 8.0)], [2.0, 2.0])
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert xmin == pytest.approx(4.0)
    assert xmax == pytest.approx(6.0)
    assert ymin == pytest.approx(-1.0)
    assert ymax == pytest.approx(9.0)
    for p in outline.polygon:
        mirror = Point(10.0 - p.x, p.y)
        assert any(math.hypot(m.x - mirror.x, m.y - mirror.y) < 1e-9
                   for m in outline.polygon)


def test_diagonal_stroke_mirror_symmetry():
    outline = build_outline([(0.0, 0.0), (10.0, 10.0)], [2.0, 2.0])
    assert len(outline.polygon) == 2 * 2 + 2 * (8 - 1)
    for p in outline.polygon:
        assert any(math.hypot(m.x - p.y, m.y - p.x) < 1e-9
                   for m in outline.polygon)
    _assert_finite(outline)
    _assert_bounded(outline, [(0.0, 0.0), (10.0, 10.0)], 1.0)


def test_variable_width_rails_follow_half_widths():
    pts = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    outline = build_outline(pts, [2.0, 4.0, 6.0], cap="butt")
    for x, h in ((0.0, 1.0), (5.0, 2.0), (10.0, 3.0)):
        assert any(math.isclose(p.x, x) and math.isclose(p.y, h)
                   for p in outline.polygon)
        assert any(math.isclose(p.x, x) and math.isclose(p.y, -h)
                   for p in outline.polygon)
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert (xmin, xmax, ymin, ymax) == pytest.approx((0.0, 10.0, -3.0, 3.0))


def test_single_point_dot_preserved():
    outline = build_outline([(3.0, 4.0)], [2.0])
    assert outline.polygon == ()
    assert len(outline.dots) == 1
    dot = outline.dots[0]
    assert (dot.center.x, dot.center.y) == pytest.approx((3.0, 4.0))
    assert dot.radius == pytest.approx(1.0)


def test_two_point_stroke_is_capsule():
    outline = build_outline([(0.0, 0.0), (4.0, 0.0)], [2.0, 4.0])
    assert outline.dots == ()
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert xmin == pytest.approx(-1.0)
    assert xmax == pytest.approx(6.0)
    assert ymin == pytest.approx(-2.0)
    assert ymax == pytest.approx(2.0)


def test_repeated_consecutive_points_collapse():
    dup = build_outline([(0.0, 0.0), (0.0, 0.0), (5.0, 0.0), (5.0, 0.0)],
                        [2.0, 2.0, 2.0, 2.0])
    clean = build_outline([(0.0, 0.0), (5.0, 0.0)], [2.0, 2.0])
    assert dup.polygon == clean.polygon
    assert dup.dots == ()


def test_duplicate_keeps_peak_width():
    outline = build_outline([(0.0, 0.0), (0.0, 0.0), (10.0, 0.0)],
                            [2.0, 6.0, 2.0], cap="butt")
    assert any(math.isclose(p.x, 0.0) and math.isclose(p.y, 3.0)
               for p in outline.polygon)


def test_all_duplicates_become_peak_dot():
    outline = build_outline([(1.0, 1.0), (1.0, 1.0)], [2.0, 6.0])
    assert outline.polygon == ()
    assert len(outline.dots) == 1
    assert outline.dots[0].radius == pytest.approx(3.0)


def test_very_short_segment_stays_finite():
    outline = build_outline([(0.0, 0.0), (1e-6, 0.0)], [2.0, 2.0])
    assert len(outline.polygon) > 0
    _assert_finite(outline)
    _assert_bounded(outline, [(0.0, 0.0), (1e-6, 0.0)], 1.0)


def test_acute_turn_bounded_and_finite():
    pts = [(0.0, 0.0), (10.0, 0.0), (15.0, 5.0)]
    outline = build_outline(pts, [2.0, 2.0, 2.0])
    _assert_finite(outline)
    _assert_bounded(outline, pts, 1.0)
    assert _area(outline) > 0


def test_near_reversal_no_spike():
    pts = [(0.0, 0.0), (10.0, 0.0), (1.0, 0.5)]
    outline = build_outline(pts, [2.0, 2.0, 2.0])
    _assert_finite(outline)
    _assert_bounded(outline, pts, 1.0)


def test_exact_backtrack_uses_fallback_tangent():
    pts = [(0.0, 0.0), (5.0, 0.0), (0.0, 0.0)]
    outline = build_outline(pts, [2.0, 2.0, 2.0])
    _assert_finite(outline)
    _assert_bounded(outline, pts, 1.0)
    assert _area(outline) >= 0


def test_zigzag_sharp_turns_bounded():
    pts = [(float(i), 0.0 if i % 2 == 0 else 0.5) for i in range(10)]
    outline = build_outline(pts, [3.0] * 10)
    _assert_finite(outline)
    _assert_bounded(outline, pts, 1.5)


def test_minimum_and_maximum_widths():
    tiny = build_outline([(0.0, 0.0), (10.0, 0.0)], [1e-6, 1e-6])
    _assert_finite(tiny)
    huge = build_outline([(0.0, 0.0), (10.0, 0.0)], [1e6, 1e6])
    _assert_finite(huge)
    xmin, xmax, ymin, ymax = _bbox(huge)
    assert (xmin, xmax, ymin, ymax) == pytest.approx(
        (-5e5, 10.0 + 5e5, -5e5, 5e5))


def test_invalid_inputs_raise_value_error():
    with pytest.raises(ValueError):
        build_outline([], [])
    with pytest.raises(ValueError):
        build_outline(None, [1.0])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0)], [1.0, 1.0])  # length mismatch
    with pytest.raises(ValueError):
        build_outline([(0.0, float("nan"))], [1.0])
    with pytest.raises(ValueError):
        build_outline([(0.0, float("inf"))], [1.0])
    with pytest.raises(ValueError):
        build_outline([("a", "b")], [1.0])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0, 0.0)], [1.0])  # not a 2D pair
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0)], [0.0])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0)], [-2.0])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0)], [float("nan")])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0)], ["wide"])
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0), (1.0, 0.0)], [1.0, 1.0], cap="miter")
    with pytest.raises(ValueError):
        build_outline([(0.0, 0.0), (1.0, 0.0)], [1.0, 1.0], cap_segments=0)


def test_deterministic_output():
    args = ([(0.0, 0.0), (4.0, 1.0), (7.0, -2.0)], [2.0, 3.0, 2.5])
    assert build_outline(*args) == build_outline(*args)
    dot_args = ([(1.0, 2.0)], [3.0])
    assert build_outline(*dot_args) == build_outline(*dot_args)


def test_inputs_not_mutated():
    pts = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]]
    widths = [2.0, 3.0, 2.0]
    build_outline(pts, widths)
    assert pts == [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]]
    assert widths == [2.0, 3.0, 2.0]


def test_point_objects_accepted():
    outline = build_outline([Point(0.0, 0.0), Point(10.0, 0.0)], [2.0, 2.0])
    xmin, xmax, ymin, ymax = _bbox(outline)
    assert (xmin, xmax, ymin, ymax) == pytest.approx((-1.0, 11.0, -1.0, 1.0))
    assert all(isinstance(p, Point) for p in outline.polygon)


def test_round_cap_vertex_count_and_butt_count():
    three = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    widths = [2.0, 2.0, 2.0]
    capped = build_outline(three, widths, cap="round", cap_segments=8)
    assert len(capped.polygon) == 2 * 3 + 2 * (8 - 1)
    few = build_outline(three, widths, cap="round", cap_segments=4)
    assert len(few.polygon) == 2 * 3 + 2 * (4 - 1)
    butt = build_outline(three, widths, cap="butt")
    assert len(butt.polygon) == 2 * 3


def test_cap_policy_constant_documented():
    from annotations.ink_geometry import CAP_POLICIES
    assert set(CAP_POLICIES) == {"round", "butt"}
