"""M6.8.2 Stage 1: pure transform geometry + rotated rect/ellipse model.

Covers the pure selection-editing geometry stack end to end without a
viewer: :mod:`annotations.transform` candidate operations (move /
resize / rotate), the shared cross-page offset contract
(``o_k = -_region_page_pt(entry_k, 0, 0)``), width/min-size/no-op
policies, the appended ``Shape.rotation_deg`` field, and the
rotation-aware rect/ellipse answers in :mod:`annotations.shape_hit`
(outline, fill containment, extents) flowing through
:mod:`annotations.hit_test`. Qt-free: no QApplication is needed.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from annotations.models import (
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
    StickyNote,
    TextAssist,
)
import annotations.transform as T
import annotations.shape_hit as SH
from annotations.hit_test import (
    annotation_extent,
    point_hits_annotation,
    select_in_rect,
)
from core.types import AnnotationId, PageId, Point, Rect, Size

APPROX = pytest.approx


# --------------------------------------------------------------- builders

def make_ink(**kw) -> InkStroke:
    base = dict(
        points=(Point(0.0, 0.0), Point(10.0, 4.0), Point(20.0, 0.0)),
        color="#ff0000",
        width_mm=1.0,
        opacity=0.9,
        pressures=(0.2, 0.8, 0.5),
        times_ms=(11, 22, 33),
        pressure_enabled=True,
        pressure_profile="normal",
    )
    base.update(kw)
    return InkStroke(**base)


def make_hl(**kw) -> Highlighter:
    return Highlighter(
        points=(Point(0.0, 0.0), Point(10.0, 0.0)),
        color="#ffff00",
        opacity=0.4,
        width_mm=3.0,
        text_assist=TextAssist(char_count=7, source="page"),
        **kw)


def make_shape(kind: ShapeKind, **kw) -> Shape:
    defaults = dict(
        color="#123456",
        width_mm=0.5,
        opacity=0.8,
        fill_enabled=False,
        fill_color="#fedcba",
        fill_opacity=0.7,
        line_style="dashed",
        start_ending="dot",
        end_ending="arrow",
    )
    defaults.update(kw)
    if kind in (ShapeKind.LINE, ShapeKind.ARROW):
        defaults.setdefault("points", (Point(1.0, 2.0), Point(9.0, 14.0)))
        defaults.setdefault(
            "rect", Rect(Point(1.0, 2.0), Size(8.0, 12.0)))
    elif kind == ShapeKind.RECTANGLE:
        defaults.setdefault(
            "rect", Rect(Point(0.0, 0.0), Size(10.0, 20.0)))
    elif kind == ShapeKind.ELLIPSE:
        defaults.setdefault(
            "rect", Rect(Point(0.0, 0.0), Size(10.0, 20.0)))
    elif kind == ShapeKind.POLYGON:
        defaults.setdefault("close_path", True)
        defaults.setdefault("points", (
            Point(0.0, 0.0), Point(8.0, 2.0), Point(4.0, 10.0)))
        defaults.setdefault(
            "rect", Rect(Point(0.0, 0.0), Size(8.0, 10.0)))
    return Shape(kind=kind, **defaults)


def assert_same_beyond_geometry(original, candidate) -> None:
    """Identity + every non-transformable field survived untouched."""
    assert candidate.id == original.id
    assert candidate.page_id == original.page_id
    assert candidate.created_at == original.created_at
    if isinstance(candidate, InkStroke):
        assert candidate.pressures == original.pressures
        assert candidate.times_ms == original.times_ms
        assert candidate.pressure_enabled == original.pressure_enabled
        assert candidate.pressure_profile == original.pressure_profile
        assert candidate.color == original.color
        assert candidate.opacity == original.opacity
    elif isinstance(candidate, Highlighter):
        assert candidate.opacity == original.opacity
        assert candidate.text_assist == original.text_assist
        assert candidate.color == original.color
    elif isinstance(candidate, Shape):
        assert candidate.kind == original.kind
        assert candidate.color == original.color
        assert candidate.opacity == original.opacity
        assert candidate.fill_enabled == original.fill_enabled
        assert candidate.fill_color == original.fill_color
        assert candidate.fill_opacity == original.fill_opacity
        assert candidate.line_style == original.line_style
        assert candidate.start_ending == original.start_ending
        assert candidate.end_ending == original.end_ending
        assert candidate.close_path == original.close_path


# ------------------------------------------------------------ dataclasses

def rect_of(shape) -> tuple:
    r = shape.rect
    return (r.x0, r.y0, r.x1, r.y1)


def pts_of(annotation) -> tuple:
    return tuple((p.x, p.y) for p in annotation.points)


def flat_pts(pt_items) -> list:
    """Flatten points/pairs — pytest.approx cannot compare nested
    tuples reliably (rel fails around exact zeros), so all multi-point
    assertions go through this flat form with an absolute tolerance."""
    out = []
    for item in pt_items:
        if isinstance(item, Point):
            out.extend((item.x, item.y))
        else:
            out.extend(item)
    return out


def assert_points_close(actual, expected, tol: float = 1e-9) -> None:
    assert_components_close(flat_pts(actual), flat_pts(expected), tol)


def assert_components_close(xs, ys, tol: float = 1e-9) -> None:
    """Elementwise absolute-tolerance comparison (nested-safe)."""
    assert len(xs) == len(ys)
    for u, v in zip(xs, ys):
        assert abs(u - v) <= tol, (u, v)


def assert_components_apart(xs, ys, tol: float = 1e-6) -> None:
    assert len(xs) == len(ys)
    assert any(abs(u - v) > tol for u, v in zip(xs, ys))


def flat_boxes(corners) -> list:
    return [c for pair in corners for c in pair]


# ================================================ angle normalization

@pytest.mark.parametrize("angle, expected", [
    (0.0, 0.0), (90.0, 90.0), (270.0, -90.0), (-90.0, -90.0),
    (180.0, 180.0), (-180.0, 180.0), (190.0, -170.0), (-190.0, 170.0),
    (360.0, 0.0), (-360.0, 0.0), (540.0, 180.0), (730.0, 10.0),
    (360.5, 0.5),
])
def test_normalize_deg_wraps_to_half_open_interval(angle, expected):
    assert T.normalize_deg(angle) == APPROX(expected)


def test_normalize_deg_rejects_unusable_values():
    assert T.normalize_deg(float("nan")) == 0.0
    assert T.normalize_deg(float("inf")) == 0.0
    assert T.normalize_deg(None) == 0.0
    assert T.normalize_deg("junk") == 0.0


def test_rotation_point_is_clockwise_in_y_down_space():
    out = T.rotate_point(Point(1.0, 0.0), 0.0, 0.0, 90.0)
    assert (out.x, out.y) == APPROX((0.0, 1.0))  # +x axis -> +y (below)


# ================================================== model: rotation_deg

def test_shape_rotation_defaults_and_backwards_compatible_construction():
    # Short pre-M6.8.2 positional construction still loads unchanged
    # (base fields: id, page_id, created_at; then kind/rect/color…).
    legacy = Shape(AnnotationId(value="legacy"), PageId(), 123.0,
                   ShapeKind.RECTANGLE,
                   Rect(Point(0.0, 0.0), Size(4.0, 4.0)), "#000000")
    assert legacy.rotation_deg == 0.0
    assert SH.shape_rotation_deg(legacy) == 0.0
    assert SH.shape_extent(legacy) == (0.0, 0.0, 4.0, 4.0)


def test_shape_rotation_post_init_normalizes():
    assert make_shape(ShapeKind.RECTANGLE, rotation_deg=270.0
                      ).rotation_deg == APPROX(-90.0)
    assert make_shape(ShapeKind.RECTANGLE, rotation_deg=-180.0
                      ).rotation_deg == 180.0
    assert make_shape(ShapeKind.RECTANGLE, rotation_deg=540.0
                      ).rotation_deg == 180.0
    assert make_shape(ShapeKind.RECTANGLE, rotation_deg=float("nan")
                      ).rotation_deg == 0.0
    assert make_shape(ShapeKind.RECTANGLE, rotation_deg="junk"
                      ).rotation_deg == 0.0


def test_point_kinds_ignore_rotation():
    line = make_shape(ShapeKind.LINE)
    assert SH.shape_rotation_deg(line) == 0.0
    rotated_model = make_shape(ShapeKind.LINE, rotation_deg=90.0)
    # The field normalizes for storage, but geometry answers come from
    # points-only kinds remain rotation-free through shape_hit.
    assert SH.shape_rotation_deg(rotated_model) == 0.0
    assert SH.shape_extent(rotated_model) == SH.shape_extent(line)


# ============================================================ move

def test_move_ink_preserves_every_unrelated_field():
    ink = make_ink()
    moved = T.move_annotation(ink, 5.5, -2.25)
    assert isinstance(moved, InkStroke)
    assert_points_close(
        moved.points,
        [(5.5, -2.25), (15.5, 1.75), (25.5, -2.25)])
    assert_same_beyond_geometry(ink, moved)
    assert T.geometry_changed(ink, moved)
    # Deterministic: applying the same gesture to the same original twice.
    assert moved == T.move_annotation(ink, 5.5, -2.25)


def test_move_highlighter_preserves_translucency_and_text_assist():
    hl = make_hl()
    moved = T.move_annotation(hl, 3.0, 4.0)
    assert_points_close(moved.points, [(3.0, 4.0), (13.0, 4.0)])
    assert moved.opacity == 0.4
    assert moved.text_assist.char_count == 7
    assert_same_beyond_geometry(hl, moved)


@pytest.mark.parametrize("kind", [
    ShapeKind.LINE, ShapeKind.ARROW, ShapeKind.RECTANGLE,
    ShapeKind.ELLIPSE, ShapeKind.POLYGON,
])
def test_move_shape_kinds_translate_geometry(kind):
    base = make_shape(kind)
    moved = T.move_annotation(base, 7.0, 9.0)
    assert_same_beyond_geometry(base, moved)
    if kind in (ShapeKind.LINE, ShapeKind.ARROW, ShapeKind.POLYGON):
        assert_points_close(
            moved.points,
            [(x + 7.0, y + 9.0) for x, y in pts_of(base)])
        # rect stays the canonical (moved) bounding box
        assert rect_of(moved) == APPROX(
            (rect_of(base)[0] + 7.0, rect_of(base)[1] + 9.0,
             rect_of(base)[2] + 7.0, rect_of(base)[3] + 9.0))
    else:
        assert rect_of(moved) == APPROX(
            (rect_of(base)[0] + 7.0, rect_of(base)[1] + 9.0,
             rect_of(base)[2] + 7.0, rect_of(base)[3] + 9.0))


def test_move_rotated_rectangle_keeps_rotation_and_local_box():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0)
    moved = T.move_annotation(box, 2.0, 3.0)
    assert moved.rotation_deg == 45.0
    assert rect_of(moved) == APPROX((2.0, 3.0, 12.0, 23.0))


def test_zero_move_is_a_true_no_op():
    for annotation in (make_ink(), make_hl(), make_shape(ShapeKind.LINE),
                       make_shape(ShapeKind.RECTANGLE,
                                  rotation_deg=33.0)):
        moved = T.move_annotation(annotation, 0.0, 0.0)
        assert not T.geometry_changed(annotation, moved)


def test_unsupported_annotation_types_have_no_edit_capability():
    note = StickyNote(position=Point(1.0, 2.0), text="hi")
    assert T.move_annotation(note, 1.0, 1.0) is None
    assert T.rotate_annotation(note, (0.0, 0.0), Point(0.0, 0.0), 30.0) is None
    assert T.resize_annotation(note, (0.0, 0.0), Point(0.0, 0.0),
                               2.0, 2.0) is None


# ============================================================ resize

def test_resize_width_policy_scales_by_mean_axis_scale():
    ink = make_ink()
    scaled = T.resize_annotation(ink, (0.0, 0.0), Point(0.0, 0.0), 2.0, 1.0)
    assert scaled.width_mm == APPROX(1.0 * (2.0 + 1.0) / 2.0)
    # Uniform converse: both axes -> the exact factor.
    uniform = T.resize_annotation(ink, (0.0, 0.0), Point(0.0, 0.0), 3.0, 3.0)
    assert uniform.width_mm == APPROX(3.0)
    # Move/rotate never touch widths.
    assert T.move_annotation(ink, 4.0, 4.0).width_mm == 1.0
    rotated = T.rotate_annotation(ink, (0.0, 0.0), Point(0.0, 0.0), 45.0)
    assert rotated.width_mm == 1.0


@pytest.mark.parametrize("start, sx, sy, expected", [
    (1.0, 1000.0, 1000.0, T.MAX_WIDTH_MM),
    (1.0, 0.02, 0.02, T.MIN_WIDTH_MM),
    (30.0, 4.0, 4.0, T.MAX_WIDTH_MM),
    (0.05, 2.0, 2.0, T.MIN_WIDTH_MM),
    # Non-finite width falls back to the 0.5 mm default *before*
    # scaling: 0.5 * mean(2, 1) = 0.75, inside the clamp band.
    (float("nan"), 2.0, 1.0, 0.75),
])
def test_resize_width_policy_clamps(start, sx, sy, expected):
    assert T.scale_width_mm(start, sx, sy) == APPROX(expected)


def test_resize_rectangle_free_scale_about_anchor():
    box = make_shape(ShapeKind.RECTANGLE)
    out = T.resize_annotation(box, (0.0, 0.0), Point(0.0, 0.0), 2.0, 0.5)
    assert rect_of(out) == APPROX((0.0, 0.0, 20.0, 10.0))
    assert out.width_mm == APPROX(0.5 * (2.0 + 0.5) / 2.0)
    assert out.rotation_deg == 0.0


def test_resize_rotated_rectangle_uses_its_unrotated_local_frame():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=30.0,
                     width_mm=1.0)
    # Center anchor: local and world frames coincide for the box dims.
    out = T.resize_annotation(box, (0.0, 0.0), Point(5.0, 10.0), 2.0, 1.0)
    assert rect_of(out) == APPROX((-5.0, 0.0, 15.0, 20.0))
    assert out.rotation_deg == 30.0
    assert out.width_mm == APPROX(1.5)

    # Contravariant anchor: a world anchor maps into the shape's frame
    # by the inverse rotation about the center (documented policy).
    box2 = make_shape(ShapeKind.RECTANGLE, rotation_deg=30.0)
    anchor_world = Point(0.0, 0.0)
    rad = math.radians(-30.0)
    c, s = math.cos(rad), math.sin(rad)
    wdx, wdy = 0.0 - 5.0, 0.0 - 10.0
    alx = 5.0 + wdx * c - wdy * s
    aly = 10.0 + wdx * s + wdy * c
    out2 = T.resize_annotation(box2, (0.0, 0.0), anchor_world, 2.0, 1.0)
    expected = Rect(Point(alx + (0.0 - alx) * 2.0, aly + (0.0 - aly) * 1.0),
                    Size(20.0, 20.0))
    assert rect_of(out2) == APPROX(rect_of_rect_tuple(expected))
    assert out2.rotation_deg == 30.0


def rect_of_rect_tuple(r: Rect) -> tuple:
    return (r.x0, r.y0, r.x1, r.y1)


def test_resize_ellipse_and_point_kinds():
    ell = make_shape(ShapeKind.ELLIPSE)
    out = T.resize_annotation(ell, (0.0, 0.0), Point(0.0, 0.0), 0.5, 2.0)
    assert rect_of(out) == APPROX((0.0, 0.0, 5.0, 40.0))

    line = make_shape(ShapeKind.LINE)
    out_line = T.resize_annotation(line, (0.0, 0.0), Point(1.0, 2.0),
                                   2.0, 2.0)
    assert_points_close(out_line.points, [(1.0, 2.0), (17.0, 26.0)])
    assert rect_of(out_line) == (1.0, 2.0, 17.0, 26.0)

    poly = make_shape(ShapeKind.POLYGON)
    out_poly = T.resize_annotation(poly, (0.0, 0.0), Point(0.0, 0.0),
                                   2.0, 0.5)
    assert_points_close(out_poly.points, [(0.0, 0.0), (16.0, 1.0),
                                          (8.0, 5.0)])
    assert rect_of(out_poly) == (0.0, 0.0, 16.0, 5.0)


def test_positive_scale_clamp_policy():
    # No flip through the anchor: negative / zero scales become the
    # strict positive floor.
    fx, fy = T.positive_scale_clamp(-0.5, 0.0)
    assert fx == T._SCALE_FLOOR and fy == T._SCALE_FLOOR
    # Minimum side length honored when the base side is measurable.
    cx, cy = T.positive_scale_clamp(0.02, 0.02, base=(100.0, 20.0),
                                    min_side=4.0)
    assert cx == APPROX(0.04) and cy == APPROX(0.2)
    # Degenerate base leaves that axis unchanged.
    dx_, dy_ = T.positive_scale_clamp(0.5, 3.0, base=(0.0, -1.0),
                                      min_side=4.0)
    assert dx_ == 1.0 and dy_ == 1.0
    # Minimum requested, positive input already above it: untouched.
    gx, gy = T.positive_scale_clamp(2.0, 2.0, base=(10.0, 10.0),
                                    min_side=4.0)
    assert gx == 2.0 and gy == 2.0


def test_resize_min_side_applies_to_candidates():
    box = make_shape(ShapeKind.RECTANGLE)  # base 10 x 20
    out = T.resize_annotation(
        box, (0.0, 0.0), Point(0.0, 0.0), 0.01, 0.01,
        base_size=(10.0, 20.0), min_side=4.0)
    w = out.rect.x1 - out.rect.x0
    h = out.rect.y1 - out.rect.y0
    assert w >= 4.0 and h >= 4.0
    assert out.rect.x0 >= 0.0  # no flip past the anchor
    assert out.rect.y0 >= 0.0


def test_resize_corner_free_vs_shift_locked_aspect():
    ink = make_ink()
    # Free corner resize: independent axes.
    free = T.resize_annotation(ink, (0.0, 0.0), Point(0.0, 0.0), 2.0, 0.5)
    assert pts_of(free)[1] == APPROX((20.0, 2.0))
    # Shift-locked corner resize: one uniform factor on both axes.
    locked = T.resize_annotation(ink, (0.0, 0.0), Point(0.0, 0.0),
                                 2.0, 2.0)
    assert pts_of(locked)[1] == APPROX((20.0, 8.0))


def test_identity_resize_is_a_no_op():
    ink = make_ink()
    assert not T.geometry_changed(
        ink, T.resize_annotation(ink, (3.0, 7.0), Point(5.0, 5.0),
                                 1.0, 1.0))


# ============================================================ rotate

def test_rotate_ink_about_external_pivot_and_preserves_metadata():
    ink = make_ink()
    out = T.rotate_annotation(ink, (0.0, 0.0), Point(0.0, 0.0), 90.0)
    assert_points_close(out.points, [(0.0, 0.0), (-4.0, 10.0), (0.0, 20.0)])
    assert_same_beyond_geometry(ink, out)
    assert out.width_mm == ink.width_mm  # rotation never scales widths


def test_rotate_rect_orbits_center_and_accumulates_rotation():
    box = make_shape(ShapeKind.RECTANGLE)  # center (5, 10), size 10x20
    out = T.rotate_annotation(box, (0.0, 0.0), Point(0.0, 0.0), 90.0)
    assert (out.rect.x0 + out.rect.x1) / 2.0 == APPROX(-10.0)
    assert (out.rect.y0 + out.rect.y1) / 2.0 == APPROX(5.0)
    assert out.rect.width == APPROX(10.0) and out.rect.height == APPROX(20.0)
    assert out.rotation_deg == APPROX(90.0)

    twice = T.rotate_annotation(out, (0.0, 0.0), Point(0.0, 0.0), 37.0)
    assert twice.rotation_deg == APPROX(127.0)


def test_rotate_accumulation_wraps_at_half_turn():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=170.0)
    out = T.rotate_annotation(box, (0.0, 0.0), Point(0.0, 0.0), 20.0)
    assert out.rotation_deg == APPROX(-170.0)
    box2 = make_shape(ShapeKind.ELLIPSE, rotation_deg=90.0)
    out2 = T.rotate_annotation(box2, (0.0, 0.0), Point(0.0, 0.0), -180.0)
    assert out2.rotation_deg == APPROX(-90.0)


def test_rotate_about_own_center_keeps_local_box():
    box = make_shape(ShapeKind.ELLIPSE, rotation_deg=25.0)
    out = T.rotate_annotation(box, (0.0, 0.0), Point(5.0, 10.0), 37.0)
    assert rect_of(out) == APPROX(rect_of(box))
    assert out.rotation_deg == APPROX(62.0)


def test_rotate_polygon_rebuilds_bbox():
    poly = make_shape(ShapeKind.POLYGON)
    out = T.rotate_annotation(poly, (0.0, 0.0), Point(0.0, 0.0), 90.0)
    # y-down clockwise: (8, 2) -> (-2, 8); (4, 10) -> (-10, 4).
    assert_points_close(out.points, [(0.0, 0.0), (-2.0, 8.0), (-10.0, 4.0)])
    assert rect_of(out) == (-10.0, 0.0, 0.0, 8.0)


def test_zero_rotate_is_a_no_op():
    for annotation in (make_ink(), make_shape(ShapeKind.LINE),
                       make_shape(ShapeKind.ELLIPSE)):
        assert not T.geometry_changed(
            annotation, T.rotate_annotation(
                annotation, (0.0, 0.0), Point(1.0, 1.0), 0.0))


# =================================================== cross-page mapping

BUCKET = 3.0
SCALE = 2.0
B = BUCKET * SCALE

#: (page_x_origin, y_offset_px) widget-mapping inputs, mirroring
#: app/viewer.py::_region_page_pt. Both pages have distinct, nonzero
#: offsets on BOTH axes.
PAGE1 = (63.0, 42.0)
PAGE2 = (117.0, 300.0)
#: (scroll_doc, scroll_doc_x)
SCROLL = (12.0, 30.0)


def region_page_pt(entry, wx, wy):
    """Mirrors viewer._region_page_pt: widget (wx, wy) -> page-local pt."""
    lx = (wx / SCALE + SCROLL[1] - entry[0]) / BUCKET
    ly = (wy / SCALE + SCROLL[0] - entry[1]) / BUCKET
    return (lx, ly)


def offset_of(entry):
    """o_k = -_region_page_pt(entry_k, 0, 0)  (the corrected formula)."""
    rx, ry = region_page_pt(entry, 0.0, 0.0)
    return (-rx, -ry)


def stroke_at_plus(delta):
    """Same widget object expressed on a page whose offset differs."""
    ink = make_ink()
    return T.move_annotation(ink, delta[0], delta[1])


def widget_images(annotation, offset):
    return [(B * p.x + B * offset[0], B * p.y + B * offset[1])
            for p in annotation.points]


def box_widget_images(shape, offset):
    """Sampled world points of a rect/ellipse shape, in widget px."""
    deg = SH.shape_rotation_deg(shape)
    corners = SH._rect_ring(shape.rect)
    if deg:
        corners = SH._rotate_ring(corners, shape.rect, deg)
    return [(B * x + B * offset[0], B * y + B * offset[1])
            for x, y in corners]


def test_offset_accepts_point_or_pair_form():
    o1 = offset_of(PAGE1)
    on_page1 = make_ink()
    anchor = Point(4.0, 9.0)
    via_point = T.resize_annotation(
        on_page1, Point(o1[0], o1[1]), anchor, 2.0, 2.0)
    via_tuple = T.resize_annotation(on_page1, o1, anchor, 2.0, 2.0)
    assert via_point == via_tuple


def test_cross_page_offset_formula_with_distinct_nonzero_offsets():
    o1, o2 = offset_of(PAGE1), offset_of(PAGE2)
    # (scroll - origin) / bucket on both axes; distinct and nonzero.
    assert o1 == APPROX((11.0, 10.0))
    assert o2 == APPROX((29.0, 96.0))
    assert o1 != o2 and all(component != 0.0
                            for o in (o1, o2) for component in o)
    # b_k = B * o_k is the page's widget origin; the accessor identity
    # holds: _region_page_pt(entry, 0, 0) == -b_k / B == -o_k.
    for entry, off in ((PAGE1, o1), (PAGE2, o2)):
        assert region_page_pt(entry, 0.0, 0.0) == APPROX(
            (-off[0], -off[1]))


def test_cross_page_shared_translation_coheres():
    o1, o2 = offset_of(PAGE1), offset_of(PAGE2)
    # Same widget object: on page 2 its page-local geometry is shifted
    # by (o1 - o2) relative to page 1.
    on_page1 = make_ink()
    on_page2 = stroke_at_plus((o1[0] - o2[0], o1[1] - o2[1]))

    dx, dy = 6.0, -3.0
    out1 = T.move_annotation(on_page1, dx, dy)
    out2 = T.move_annotation(on_page2, dx, dy)
    # Translation is offset-independent: identical page-local delta on
    # both pages, and the widget images coincide exactly.
    assert_points_close(
        out2.points, [(x + dx, y + dy) for x, y in pts_of(on_page2)])
    assert_components_close(
        flat_boxes(widget_images(out1, o1)),
        flat_boxes(widget_images(out2, o2)))


def test_cross_page_shared_resize_images_coincide():
    o1, o2 = offset_of(PAGE1), offset_of(PAGE2)
    on_page1 = make_ink()
    on_page2 = stroke_at_plus((o1[0] - o2[0], o1[1] - o2[1]))

    anchor = Point(12.0, -20.0)
    sx, sy = 0.5, 2.0
    out1 = T.resize_annotation(on_page1, o1, anchor, sx, sy)
    out2 = T.resize_annotation(on_page2, o2, anchor, sx, sy)
    assert_components_close(
        flat_boxes(widget_images(out1, o1)),
        flat_boxes(widget_images(out2, o2)))
    # Page-local anchors genuinely differ (per-page pivot a_k = a - o_k).
    assert out1 != out2


def test_cross_page_shared_rotation_images_coincide():
    o1, o2 = offset_of(PAGE1), offset_of(PAGE2)
    on_page1 = make_ink()
    on_page2 = stroke_at_plus((o1[0] - o2[0], o1[1] - o2[1]))

    pivot = Point(-3.0, 8.0)
    angle = 33.0
    out1 = T.rotate_annotation(on_page1, o1, pivot, angle)
    out2 = T.rotate_annotation(on_page2, o2, pivot, angle)
    assert_components_close(
        flat_boxes(widget_images(out1, o1)),
        flat_boxes(widget_images(out2, o2)))

    # Rotation of a rect/ellipse member composes the same way.
    box1 = make_shape(ShapeKind.RECTANGLE)
    box2 = T.move_annotation(
        make_shape(ShapeKind.RECTANGLE),
        o1[0] - o2[0], o1[1] - o2[1])
    rot1 = T.rotate_annotation(box1, o1, pivot, angle)
    rot2 = T.rotate_annotation(box2, o2, pivot, angle)
    assert_components_close(
        flat_boxes(box_widget_images(rot1, o1)),
        flat_boxes(box_widget_images(rot2, o2)))
    assert rot1.rotation_deg == APPROX(angle)
    assert rot2.rotation_deg == APPROX(angle)


def test_wrong_offset_sign_breaks_image_coherence():
    """The regression guard: '+' instead of '-' for o_k diverges."""
    o1, o2 = offset_of(PAGE1), offset_of(PAGE2)
    on_page1 = make_ink()
    wrong_o2 = (+region_page_pt(PAGE2, 0.0, 0.0)[0],
                +region_page_pt(PAGE2, 0.0, 0.0)[1])
    on_page2 = stroke_at_plus((o1[0] - o2[0], o1[1] - o2[1]))
    anchor = Point(4.0, 9.0)

    out1 = T.resize_annotation(on_page1, o1, anchor, 2.0, 2.0)
    out_wrong = T.resize_annotation(on_page2, wrong_o2, anchor, 2.0, 2.0)
    # The sign error must produce a *decisively* different result —
    # not a float-noise-level mismatch.
    assert_components_apart(
        flat_boxes(widget_images(out1, o1)),
        flat_boxes(widget_images(out_wrong, o2)), tol=1.0)


# ======================================= rotated rect/ellipse geometry

def test_rotated_rectangle_outline_and_extent():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=90.0,
                     fill_enabled=True)
    polys = SH.outline_polylines(box)
    assert len(polys) == 1
    ring = polys[0]
    # Corners of (0,0)-(10,20) rotated 90 deg cw about (5, 10): the AABB
    # swaps axes.
    assert ring[0] == APPROX((15.0, 5.0))
    assert ring[2] == APPROX((-5.0, 15.0))
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    assert (min(xs), min(ys), max(xs), max(ys)) == SH.shape_extent(box)
    assert SH.shape_extent(box) == APPROX((-5.0, 5.0, 15.0, 15.0))


def test_rotated_square_45_degree_extent():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0,
                     rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)))
    half = 5.0 * math.sqrt(2.0)
    assert SH.shape_extent(box) == APPROX((5.0 - half, 5.0 - half,
                                           5.0 + half, 5.0 + half))


def test_rotated_ellipse_extent_and_zero_rotation_no_drift():
    ell = make_shape(ShapeKind.ELLIPSE,
                     rect=Rect(Point(0.0, 0.0), Size(12.0, 4.0)),
                     rotation_deg=90.0)
    # Half-extents swap: |rx*0| + |ry*1| -> hw = ry, hh = rx.
    assert SH.shape_extent(ell) == APPROX(
        (6.0 - 2.0, 2.0 - 6.0, 6.0 + 2.0, 2.0 + 6.0))
    plain = make_shape(ShapeKind.ELLIPSE,
                       rect=Rect(Point(0.0, 0.0), Size(12.0, 4.0)))
    assert SH.shape_extent(plain) == (0.0, 0.0, 12.0, 4.0)  # exact


def test_rotated_rectangle_fill_containment():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0,
                     rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)),
                     fill_enabled=True)
    # Straight beyond the unrotated corner yet inside the diamond.
    assert SH.fill_contains(box, 11.0, 5.0)
    # Inside the unrotated box yet outside the rotated diamond.
    assert not SH.fill_contains(box, 10.5, 10.5)
    # Center obviously inside both.
    assert SH.fill_contains(box, 5.0, 5.0)
    # fill containment is purely *geometric*: enablement is applied by
    # callers (hit_test/eraser), which is pre-M6.8.2 behavior kept
    # intact. What must hold is that a fill-disabled rotated shape
    # yields no *hit* at the same point.
    hollow = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0,
                        rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)))
    assert not point_hits_annotation(hollow, 11.0, 5.0)
    assert SH.fill_contains(hollow, 11.0, 5.0)  # geometry only


def test_rotated_ellipse_fill_containment():
    ell = make_shape(ShapeKind.ELLIPSE,
                     rect=Rect(Point(0.0, 0.0), Size(12.0, 4.0)),
                     rotation_deg=90.0, fill_enabled=True)
    # Rotated 90: the tall axis is now horizontal... ry=2 vertical, rx=6
    # horizontal in local frame; after 90 deg the 6-length axis is
    # vertical, the 2-length axis horizontal.
    assert SH.fill_contains(ell, 6.0, 6.0)      # +2 on the long axis
    assert not SH.fill_contains(ell, 11.0, 2.0)  # beyond the short axis
    assert SH.fill_contains(ell, 6.0, 2.0)      # center


def test_rotated_rectangle_eraser_touch():
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0,
                     rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)),
                     fill_enabled=True)
    # Segment crossing the rotated diamond's interior (fill sample).
    assert SH.segment_hits_shape(box, 6.0, 5.0, 9.0, 5.0, 0.5, 0.7)
    # Segment far from the rotated geometry.
    assert not SH.segment_hits_shape(box, 30.0, 30.0, 40.0, 40.0,
                                     0.5, 0.7)


def test_rotation_flows_through_hit_test():
    half = 0.5 * 72.0 / 25.4 / 2.0  # half outline width, page pt
    box = make_shape(ShapeKind.RECTANGLE, rotation_deg=45.0,
                     rect=Rect(Point(0.0, 0.0), Size(10.0, 10.0)),
                     fill_enabled=True)
    # Extent (AABB of the rotated geometry) is used as the broad phase;
    # annotation_extent pads it by the half outline width.
    assert annotation_extent(box) == APPROX(tuple(
        v - half if i < 2 else v + half
        for i, v in enumerate(SH.shape_extent(box))))
    # Point pick hits inside the diamond but outside the AABB diag.
    assert point_hits_annotation(box, 8.0, 5.0)   # in fill, x<10 anyway
    assert point_hits_annotation(box, 11.0, 5.0)  # fill only when rotated
    assert not point_hits_annotation(box, 13.0, 5.0)  # beyond the outline
    # Marquee: a rect far away excludes, and an overlapping one selects.
    assert select_in_rect((box,), (50.0, 50.0, 60.0, 60.0)) == ()
    assert select_in_rect((box,), (4.0, 4.0, 6.0, 6.0)) == (box,)


# ============================================ no-op + determinism audit

def test_geometry_changed_detects_field_changes():
    # Identity fields compare exactly: two *freshly built* annotations
    # are different annotations even when their geometry matches. No-op
    # comparisons always run between an original and its own candidate
    # (which preserves identity by construction), so build the no-op
    # case via dataclasses.replace.
    ink = make_ink()
    assert not T.geometry_changed(ink, dataclasses.replace(ink))
    assert T.geometry_changed(ink, dataclasses.replace(ink, width_mm=1.2))
    assert T.geometry_changed(ink, dataclasses.replace(ink, color="#00ff00"))
    assert T.geometry_changed(ink, dataclasses.replace(ink, page_id=PageId()))


def test_transform_operations_are_deterministic_from_originals():
    """Every candidate recomputes from the original, never a preview."""
    ink = make_ink()
    before = pts_of(ink)
    first = T.move_annotation(ink, 3.0, 1.0)
    second = T.move_annotation(ink, 3.0, 1.0)
    assert first == second
    # Applying a move never mutates the original: a cancelled gesture
    # can simply drop its preview, and re-running the gesture
    # recomputes the identical candidate.
    assert pts_of(ink) == before
