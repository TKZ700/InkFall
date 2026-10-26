"""M7.1 Phase B Part 2: pure geometry for live closed-shape adjustment.

Covers deterministic handle selection, immutable anchors, rectangle local
frames and metadata, circle radial resizing, triangle/polygon vertex moves,
activation-grandfathered floors, structural validation (self-intersection,
duplicates, orientation, area share), absolute-delta idempotence and the
exact morph-translation rebase. Qt-free: only the geometry module is used.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from annotations.shape_adjust import (
    CLOSED_KINDS,
    adjusted_closed,
    begin_closed_adjustment,
    centroid,
    rebase_closed_candidate,
)
from annotations.shape_recognize import (
    MIN_CLOSED_PERIMETER_PT,
    MIN_SEGMENT_PT,
    ShapeCandidate,
    morph_translation,
    prepare_morph,
)


# -- geometry fixtures ------------------------------------------------------

def _rect(cx=100.0, cy=100.0, half_a=40.0, half_b=25.0, deg=0.0):
    radians = math.radians(deg)
    ux, uy = math.cos(radians), math.sin(radians)
    vx, vy = -uy, ux
    contour = (
        (cx + half_a * ux + half_b * vx, cy + half_a * uy + half_b * vy),
        (cx + half_a * ux - half_b * vx, cy + half_a * uy - half_b * vy),
        (cx - half_a * ux - half_b * vx, cy - half_a * uy - half_b * vy),
        (cx - half_a * ux + half_b * vx, cy - half_a * uy + half_b * vy),
    )
    return ShapeCandidate(kind="rectangle", contour=contour, closed=True,
                          confidence=0.9,
                          meta=(cx, cy, half_a, half_b, deg))


def _circle(cx=100.0, cy=100.0, radius=20.0, steps=48):
    contour = tuple(
        (cx + radius * math.cos(2.0 * math.pi * i / steps),
         cy + radius * math.sin(2.0 * math.pi * i / steps))
        for i in range(steps)
    )
    return ShapeCandidate(kind="circle", contour=contour, closed=True,
                          confidence=0.9,
                          meta=(cx, cy, radius, radius, 0.0))


def _triangle(points=((0.0, 0.0), (100.0, 0.0), (50.0, 80.0))):
    return ShapeCandidate(kind="triangle", contour=tuple(points),
                          closed=True, confidence=0.9)


def _polygon(points=((0.0, 0.0), (80.0, 0.0), (80.0, 80.0),
                     (40.0, 40.0), (0.0, 80.0))):
    return ShapeCandidate(kind="polygon", contour=tuple(points),
                          closed=True, confidence=0.9)


def _corners(candidate):
    return tuple((float(x), float(y)) for x, y in candidate.contour)


def _assert_points(actual, expected):
    """Point-wise comparison (``pytest.approx`` rejects nested tuples)."""
    assert len(actual) == len(expected)
    for point, target in zip(actual, expected):
        assert point == pytest.approx(target)


def _rotation_corners(meta):
    cx, cy, half_a, half_b, deg = meta
    radians = math.radians(deg)
    ux, uy = math.cos(radians), math.sin(radians)
    vx, vy = -uy, ux
    return (
        (cx + half_a * ux + half_b * vx, cy + half_a * uy + half_b * vy),
        (cx + half_a * ux - half_b * vx, cy + half_a * uy - half_b * vy),
        (cx - half_a * ux - half_b * vx, cy - half_a * uy - half_b * vy),
        (cx - half_a * ux + half_b * vx, cy - half_a * uy + half_b * vy),
    )


# -- selection and freezing -------------------------------------------------

def test_nearest_handle_selection_and_lowest_index_tie_break():
    rect = _rect(deg=20.0)
    near_second = begin_closed_adjustment(rect, rect.contour[1], 1.0)
    assert near_second.moving_index == 1
    assert near_second.anchor == pytest.approx(rect.contour[3])

    # Exactly between corners 0 and 1: equal distance, lowest index wins.
    midpoint = ((rect.contour[0][0] + rect.contour[1][0]) / 2.0,
                (rect.contour[0][1] + rect.contour[1][1]) / 2.0)
    tied = begin_closed_adjustment(rect, midpoint, 1.0)
    assert tied.moving_index == 0

    tri = _triangle()
    tri_adj = begin_closed_adjustment(tri, tri.contour[2], 1.0)
    assert tri_adj.moving_index == 2

    poly = _polygon()
    poly_adj = begin_closed_adjustment(poly, poly.contour[1], 1.0)
    assert poly_adj.moving_index == 1


def test_circle_has_no_vertex_handle_and_rejects_non_circle_meta():
    circle = _circle()
    adjustment = begin_closed_adjustment(circle, (0.0, 0.0), 1.0)
    assert adjustment.kind == "circle"
    assert adjustment.moving_index == -1

    oval = ShapeCandidate(kind="circle", contour=circle.contour,
                          closed=True, meta=(100.0, 100.0, 20.0, 10.0, 0.0))
    assert begin_closed_adjustment(oval, (0.0, 0.0), 1.0) is None


def test_handle_selection_is_frozen_for_the_gesture():
    rect = _rect()
    adjustment = begin_closed_adjustment(rect, rect.contour[1], 1.0)
    assert adjustment.moving_index == 1
    # Moving the pointer next to another corner must not reselect it.
    adjusted_closed(adjustment, (rect.contour[2][0] + 1, rect.contour[2][1]))
    assert adjustment.moving_index == 1


# -- rectangles -------------------------------------------------------------

def test_rotated_rectangle_moves_selected_corner_keeps_diagonal_anchor():
    base = _rect(deg=25.0)
    selected = 0
    anchor_index = 2
    adjustment = begin_closed_adjustment(base, base.contour[selected], 1.0)
    assert adjustment.anchor == pytest.approx(base.contour[anchor_index])

    delta = (12.0, -7.0)
    result = adjusted_closed(
        adjustment, (adjustment.origin[0] + delta[0],
                     adjustment.origin[1] + delta[1]))
    assert result is not None
    points = _corners(result)
    assert points[anchor_index] == pytest.approx(adjustment.anchor)
    assert points[selected] == pytest.approx(
        (base.contour[selected][0] + delta[0],
         base.contour[selected][1] + delta[1]))
    # Rotation and ring ordering are preserved, and meta reproduces contour.
    assert result.meta[4] == pytest.approx(25.0)
    _assert_points(_corners(result), _rotation_corners(result.meta))


def test_rectangle_width_and_height_resize_independently():
    base = _rect(half_a=40.0, half_b=25.0)
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    # Page delta (+10, -10) maps to local +u (width) and -v (height).
    pointer = (adjustment.origin[0] + 10.0, adjustment.origin[1] - 10.0)
    result = adjusted_closed(adjustment, pointer)
    assert result is not None
    _cx, _cy, half_a, half_b, _deg = result.meta
    assert half_a == pytest.approx(45.0)
    assert half_b == pytest.approx(20.0)


def test_rectangle_rejects_axis_crossing_and_short_side():
    base = _rect(half_a=40.0, half_b=25.0)
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    # Cross the local axis through the anchor (mirror attempt).
    assert adjusted_closed(adjustment, (-90.0, 0.0)) is None
    # Collapse the short side below 6 pt while staying on the same side.
    assert adjusted_closed(adjustment, (330.0, 76.0)) is None


def test_rectangle_grandfathers_short_side_and_perimeter():
    # Short side 5 pt (< 6) and perimeter 30 pt (< 40).
    base = _rect(half_a=5.0, half_b=2.5)
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    assert adjustment.short_side_floor == pytest.approx(5.0)
    assert adjustment.perimeter_floor == pytest.approx(30.0)
    # Shrinking either dimension fails; growing is allowed.
    assert adjusted_closed(adjustment, adjustment.origin) is base
    shrink = (adjustment.origin[0], adjustment.origin[1] - 1.0)
    assert adjusted_closed(adjustment, shrink) is None
    grown = adjusted_closed(
        adjustment, (adjustment.origin[0] + 3.0,
                     adjustment.origin[1] + 2.0))
    assert grown is not None
    assert grown.meta[2] > 5.0 and grown.meta[3] > 2.5


def test_rectangle_absolute_delta_is_idempotent_and_origin_restores_base():
    base = _rect(deg=-15.0)
    adjustment = begin_closed_adjustment(base, base.contour[1], 1.0)
    radians = math.radians(-15.0)
    ux, uy = math.cos(radians), math.sin(radians)
    vx, vy = -uy, ux
    # A valid local delta: +u widens, -v deepens the selected diagonal.
    pointer = (adjustment.origin[0] + 10.0 * ux - 5.0 * vx,
               adjustment.origin[1] + 10.0 * uy - 5.0 * vy)
    first = adjusted_closed(adjustment, pointer)
    second = adjusted_closed(adjustment, pointer)
    assert first is not None
    assert _corners(first) == _corners(second)
    assert adjusted_closed(adjustment, adjustment.origin) is base
    third = adjusted_closed(adjustment, pointer)
    assert _corners(third) == _corners(first)


# -- circles ----------------------------------------------------------------

def test_circle_keeps_center_circularity_and_zero_rotation():
    base = _circle(radius=20.0)
    adjustment = begin_closed_adjustment(base, (120.0, 100.0), 1.0)
    result = adjusted_closed(adjustment, (126.0, 100.0))
    assert result is not None
    cx, cy, half_a, half_b, deg = result.meta
    assert (cx, cy) == pytest.approx((100.0, 100.0))
    assert half_a == pytest.approx(26.0)
    assert half_b == pytest.approx(half_a)
    assert deg == 0.0
    radii = {round(math.hypot(x - cx, y - cy), 9)
             for x, y in _corners(result)}
    assert len(radii) == 1


def test_circle_radial_delta_preserves_baseline_and_ignores_tangential_motion():
    base = _circle(radius=20.0)
    adjustment = begin_closed_adjustment(base, (120.0, 100.0), 1.0)
    # Zero radial change preserves r0, even when the recognition origin was
    # off the fitted circumference: the formula uses distances to the fixed
    # centre, not the origin's radius.
    off = begin_closed_adjustment(base, (130.0, 100.0), 1.0)
    assert adjusted_closed(off, (130.0, 100.0)) is base
    off_tangent = (100.0 + 30.0 * math.cos(0.7),
                   100.0 + 30.0 * math.sin(0.7))
    assert adjusted_closed(off, off_tangent).meta[2] == pytest.approx(20.0)

    # Same distance from the centre, different angle: radius unchanged.
    tangential = (100.0 + 20.0 * math.cos(0.7),
                  100.0 + 20.0 * math.sin(0.7))
    assert adjusted_closed(adjustment, tangential).meta[2] == pytest.approx(20.0)

    # Outward and inward radial deltas.
    assert adjusted_closed(adjustment, (127.0, 100.0)).meta[2] == pytest.approx(27.0)
    assert adjusted_closed(adjustment, (117.0, 100.0)).meta[2] == pytest.approx(17.0)


def test_circle_radius_floor_and_activation_grandfathering():
    base = _circle(radius=20.0)
    adjustment = begin_closed_adjustment(base, (120.0, 100.0), 1.0)
    standard = max(MIN_SEGMENT_PT / 2.0,
                   MIN_CLOSED_PERIMETER_PT
                   / (2.0 * 48.0 * math.sin(math.pi / 48.0)))
    assert adjustment.radius_floor == pytest.approx(standard)
    assert adjusted_closed(adjustment, (100.0 + standard - 0.5, 100.0)) is None
    assert adjusted_closed(adjustment, (100.0 + standard + 0.5, 100.0)) is not None

    small = _circle(radius=5.0)
    small_adj = begin_closed_adjustment(small, (105.0, 100.0), 1.0)
    assert small_adj.radius_floor == pytest.approx(5.0)
    assert adjusted_closed(small_adj, (104.9, 100.0)) is None
    assert adjusted_closed(small_adj, (105.1, 100.0)) is not None


# -- triangles --------------------------------------------------------------

def test_triangle_moves_one_vertex_and_keeps_other_two():
    base = _triangle()
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    result = adjusted_closed(adjustment, (-10.0, 5.0))
    assert result is not None
    points = _corners(result)
    assert points[0] == pytest.approx((-10.0, 5.0))
    assert points[1] == base.contour[1]
    assert points[2] == base.contour[2]
    assert len(points) == 3


def test_triangle_rejects_edge_floor_orientation_and_degenerate_frames():
    base = _triangle()
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    # Onto the neighbouring vertex: edge below the 6 pt floor.
    assert adjusted_closed(adjustment, base.contour[1]) is None
    # Cross the opposite edge: orientation flips.
    assert adjusted_closed(adjustment, (50.0, 120.0)) is None
    # Collinear frame: zero area.
    assert adjusted_closed(adjustment, (25.0, 120.0)) is None
    # Unusable pointer values never produce geometry.
    assert adjusted_closed(adjustment, (float("nan"), 0.0)) is None
    assert adjusted_closed(adjustment, None) is None


def test_triangle_grandfathers_a_below_standard_edge():
    base = _triangle(((0.0, 0.0), (5.0, 0.0), (2.0, 40.0)))
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    assert min(adjustment.edge_floors) == pytest.approx(5.0)
    # Shrinking the short edge below its activation length is rejected.
    assert adjusted_closed(adjustment, (1.0, 0.0)) is None
    # Moving the long-side vertex is still allowed.
    assert adjusted_closed(adjustment, (-1.0, 1.0)) is not None


# -- polygons ---------------------------------------------------------------

def test_polygon_moves_one_vertex_preserves_count_order_and_concavity():
    base = _polygon()
    adjustment = begin_closed_adjustment(base, base.contour[3], 1.0)
    result = adjusted_closed(adjustment, (45.0, 50.0))
    assert result is not None
    points = _corners(result)
    assert len(points) == 5
    assert points[3] == pytest.approx((45.0, 50.0))
    assert points[0] == base.contour[0]
    assert points[1] == base.contour[1]
    assert points[2] == base.contour[2]
    assert points[4] == base.contour[4]
    # The concave arrow stays simple and its orientation is unchanged.
    assert result.kind == "polygon"
    assert result.closed is True


def test_polygon_rejects_self_intersection_and_duplicate_vertices():
    base = _polygon()
    adjustment = begin_closed_adjustment(base, base.contour[1], 1.0)
    # Pull a vertex across the opposite edge: self-intersection.
    assert adjusted_closed(adjustment, (10.0, 70.0)) is None
    # Move onto another vertex: duplicate/zero-length edge.
    assert adjusted_closed(adjustment, base.contour[2]) is None


def test_polygon_area_share_floor_and_grandfathering():
    # Concave C-shape: its area is well below half its hull at activation.
    base = _polygon(((0.0, 0.0), (100.0, 0.0), (100.0, 10.0),
                     (10.0, 10.0), (10.0, 90.0), (100.0, 90.0),
                     (100.0, 100.0), (0.0, 100.0)))
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    assert 0.0 < adjustment.area_share_floor < 0.50
    # A frame below the activation share is rejected; the base is valid.
    narrowed = replace(adjustment,
                       area_share_floor=adjustment.area_share_floor + 0.01)
    assert adjusted_closed(narrowed, (-2.0, 2.0)) is None
    assert adjusted_closed(adjustment, (0.0, 0.0)) is base


def test_polygon_rejects_orientation_flip_and_non_finite():
    base = _polygon()
    adjustment = begin_closed_adjustment(base, base.contour[0], 1.0)
    assert adjusted_closed(adjustment, (200.0, 200.0)) is None
    assert adjusted_closed(adjustment, (float("inf"), 0.0)) is None


# -- rebase and shared contract ---------------------------------------------

def test_rebase_translates_contours_and_box_metadata_without_mutation():
    for candidate in (_rect(), _circle(), _triangle(), _polygon()):
        original = _corners(candidate)
        original_meta = tuple(candidate.meta)
        rebased = rebase_closed_candidate(candidate, (7.0, -3.0))
        assert rebased is not None
        assert _corners(candidate) == original
        assert tuple(candidate.meta) == original_meta
        assert _corners(rebased)[0] == pytest.approx(
            (original[0][0] + 7.0, original[0][1] - 3.0))
        if candidate.kind in ("rectangle", "circle"):
            assert rebased.meta[0] == pytest.approx(candidate.meta[0] + 7.0)
            assert rebased.meta[1] == pytest.approx(candidate.meta[1] - 3.0)
            assert rebased.meta[2:] == pytest.approx(candidate.meta[2:])


def test_morph_translation_is_the_exact_prepare_morph_translation():
    base = _polygon()
    shift = (13.5, -6.25)
    fitted = tuple((x + shift[0], y + shift[1]) for x, y in base.contour)
    raw = base.contour
    offset = morph_translation(raw, fitted, closed=True)
    assert offset == pytest.approx((-shift[0], -shift[1]))

    prepared = prepare_morph(raw, fitted, closed=True)
    preview = centroid(prepared[0])
    fitted_preview = centroid(prepared[1])
    assert preview == pytest.approx(fitted_preview)
    # Applying the reported offset to the fitted side aligns the inputs'
    # centroid convention, which is what the viewer rebases by.
    expected = (centroid(raw)[0] - centroid(fitted)[0],
                centroid(raw)[1] - centroid(fitted)[1])
    assert offset == pytest.approx(expected)


def test_closed_adjustment_rejects_unsupported_and_malformed_inputs():
    line = ShapeCandidate(kind="line", contour=((0.0, 0.0), (10.0, 0.0)),
                          closed=False)
    assert begin_closed_adjustment(line, (0.0, 0.0), 1.0) is None
    assert rebase_closed_candidate(line, (0.0, 0.0)) is None

    open_rect = ShapeCandidate(kind="rectangle",
                               contour=_rect().contour, closed=False,
                               meta=_rect().meta)
    assert begin_closed_adjustment(open_rect, (0.0, 0.0), 1.0) is None

    bad_rect = ShapeCandidate(kind="rectangle",
                              contour=_rect().contour[:3], closed=True,
                              meta=_rect().meta)
    assert begin_closed_adjustment(bad_rect, (0.0, 0.0), 1.0) is None

    assert begin_closed_adjustment(_rect(), None, 1.0) is None
    assert begin_closed_adjustment(_rect(),
                                   (float("nan"), 0.0), 1.0) is None
    assert adjusted_closed(None, (0.0, 0.0)) is None
    assert CLOSED_KINDS == {"rectangle", "circle", "triangle", "polygon"}


def test_zero_delta_returns_the_rebased_candidate_for_every_kind():
    for candidate in (_rect(deg=30.0), _circle(), _triangle(), _polygon()):
        adjustment = begin_closed_adjustment(candidate, candidate.contour[0],
                                             1.0)
        assert adjustment is not None
        assert adjusted_closed(adjustment, adjustment.origin) is candidate
