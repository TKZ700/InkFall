"""Group-transform candidate geometry for selection editing (M6.8.2).

Qt-free, stdlib-only (same layer rules as the rest of
``annotations/*``). This module is the single source of truth for the
*candidate* annotation a completed move / resize / rotate gesture
commits: the viewer builds one :class:`GroupTransform` from
widget-space measurements and applies it to gesture-start *originals*
through :func:`apply_group` — never to a previous preview, so
repeated application of the same transform is idempotent and a
cancelled gesture can simply drop its preview.

Shared cross-page space
-----------------------
Every page maps page-local ``pt`` to widget ``px`` as ``w = B*p + b_k``
with a **single global** ``B = bucket * display_scale`` and a per-page
offset ``b_k`` (different page sizes and gaps differ only in ``b_k``;
page rotation is folded into the layout entry once, keeping the
projection a pure scale+translate). The viewer accessor
``_region_page_pt(entry_k, 0, 0)`` therefore returns ``-b_k / B``, and
the shared-space offset is

    o_k = -_region_page_pt(entry_k, 0, 0)   (= +b_k / B)

so ``v = p + o_k == w / B`` is the widget point expressed in page-pt
units — one coherent space across pages. The group transform ``T`` is
defined once in that space and applied per annotation as

    p' = T(p + o_k) - o_k

Members keep their identity (stable ``AnnotationId``), page assignment
(``page_id`` never changes — an annotation is never reparented) and
every unrelated property; candidates are fresh instances built with
``dataclasses.replace``. Point-based kinds (ink, Line, Arrow, Polygon)
transform directly in page-local pt; RECTANGLE/ELLIPSE carry their
rotation as :attr:`~annotations.models.Shape.rotation_deg` and scale in
their own unrotated local frame (see the kind-specific notes below).
"""

from __future__ import annotations

import dataclasses
import math

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from core.types import Point, Rect, Size

__all__ = [
    "MIN_WIDTH_MM",
    "MAX_WIDTH_MM",
    "NOOP_EPS",
    "GroupTransform",
    "apply_group",
    "move_annotation",
    "resize_annotation",
    "rotate_annotation",
    "normalize_deg",
    "rotate_point",
    "scale_point",
    "mean_axis_scale",
    "scale_width_mm",
    "positive_scale_clamp",
    "geometry_changed",
    "MIN_BOX_SIDE_PT",
    "BOX_HANDLES",
    "vertex_points",
    "box_handle_points",
    "move_vertex",
    "resize_box_handle",
]

#: Resize width policy: a stroke's or shape's ``width_mm`` scales by the
#: mean axis scale and is clamped to these bounds.
MIN_WIDTH_MM = 0.1
MAX_WIDTH_MM = 50.0
_WIDTH_DEFAULT_MM = 0.5

#: Epsilon for "changed?" comparisons, in page-local pt (or mm for
#: widths): a candidate within this distance of the original is the
#: same data for commit purposes and must not open a history group.
NOOP_EPS = 1e-6

#: Strict-positivity floor for scale factors: a scale may never reach
#: zero or flip sign (no resize through the anchor).
_SCALE_FLOOR = 1e-6

_DEG2RAD = math.pi / 180.0


# ------------------------------------------------------------- primitives

def normalize_deg(angle) -> float:
    """Angle in degrees wrapped to ``(-180, 180]``; non-finite -> 0.0.

    Positive = clockwise in y-down page space (matching Qt's
    ``painter.rotate`` and the models' rotation semantics).
    """
    try:
        a = float(angle)
    except (TypeError, ValueError):
        return 0.0
    if a != a or a in (float("inf"), float("-inf")):
        return 0.0
    a = math.fmod(a, 360.0)
    if a <= -180.0:
        a += 360.0
    elif a > 180.0:
        a -= 360.0
    return a


def rotate_point(p, px: float, py: float, angle_deg: float) -> Point:
    """Rotate one point about ``(px, py)`` by ``angle_deg`` degrees.

    Positive = clockwise in y-down space: ``(dx, dy) -> (dx*c - dy*s,
    dx*s + dy*c)``, identical to Qt's y-down ``painter.rotate``. The
    angle is applied as given (callers normalize; zero is a no-op).
    """
    a = float(angle_deg)
    if a == 0.0:
        try:
            return Point(float(p.x), float(p.y))
        except Exception:
            return Point(0.0, 0.0)
    c = math.cos(a * _DEG2RAD)
    s = math.sin(a * _DEG2RAD)
    try:
        x, y = float(p.x), float(p.y)
    except Exception:
        x, y = 0.0, 0.0
    dx = x - px
    dy = y - py
    return Point(px + dx * c - dy * s, py + dx * s + dy * c)


def scale_point(p, ax: float, ay: float, sx: float, sy: float) -> Point:
    """Scale one point about the anchor ``(ax, ay)`` (per axis)."""
    try:
        x, y = float(p.x), float(p.y)
    except Exception:
        x, y = 0.0, 0.0
    return Point(ax + (x - ax) * sx, ay + (y - ay) * sy)


def mean_axis_scale(sx, sy) -> float:
    """Mean of the absolute axis scales (the resize width policy input).

    A non-finite axis falls back to ``1.0`` for that axis; a fully
    degenerate mean (zero/non-finite) collapses to ``1.0`` so widths are
    left untouched rather than destroyed.
    """
    try:
        a = abs(float(sx))
    except (TypeError, ValueError):
        return 1.0
    try:
        b = abs(float(sy))
    except (TypeError, ValueError):
        return 1.0
    if a != a or a in (float("inf"), float("-inf")):
        a = 1.0
    if b != b or b in (float("inf"), float("-inf")):
        b = 1.0
    m = (a + b) / 2.0
    if m != m or m in (float("inf"), float("-inf")) or m <= 0.0:
        return 1.0
    return m


def scale_width_mm(width_mm, sx, sy) -> float:
    """``width_mm`` scaled by the mean axis scale, clamped to policy.

    Unrelated to geometry, this implements the documented M6.8.2 policy:
    ink and shape outlines scale by ``mean(|sx|, |sy|)`` when resized
    (never on a move or rotate), and the result is clamped to
    ``[MIN_WIDTH_MM, MAX_WIDTH_MM]``. A non-finite width falls back to
    the scalar-width default (0.5 mm) before clamping.
    """
    try:
        w = float(width_mm)
    except (TypeError, ValueError):
        w = _WIDTH_DEFAULT_MM
    if w != w or w in (float("inf"), float("-inf")):
        w = _WIDTH_DEFAULT_MM
    return max(MIN_WIDTH_MM, min(MAX_WIDTH_MM, w * mean_axis_scale(sx, sy)))


def positive_scale_clamp(sx, sy, base=(1.0, 1.0), min_side=None) -> tuple:
    """Clamp axis scales to be strictly positive, optionally min-size.

    Policy (M6.8.2, documented and tested):
    * A scale is never zero or negative — dragging a handle through the
      anchor is impossible; the floor is a strict small positive value.
    * With ``min_side`` given and a usable ``base`` dimension (finite,
      positive), each axis scale is additionally clamped so the *scaled
      side length* is at least ``min_side`` page-pt: ``s >= min_side /
      base``.
    * With ``min_side`` given and an unusable base dimension the
      geometry is already degenerate; that axis leaves its scale at
      ``1.0`` (no change) rather than inventing geometry.
    """
    out = []
    for value, base_len in ((sx, base[0]), (sy, base[1])):
        try:
            s = float(value)
        except (TypeError, ValueError):
            s = 1.0
        try:
            b = float(base_len)
        except (TypeError, ValueError):
            b = float("nan")
        if not (s > 0.0) or s != s or s in (float("inf"), float("-inf")):
            s = _SCALE_FLOOR
        if min_side is not None:
            if b > 0.0 and b == b and b not in (float("inf"), float("-inf")):
                s = max(s, float(min_side) / b)
            else:
                s = 1.0
            s = max(s, _SCALE_FLOOR)
        out.append(s)
    return (out[0], out[1])


# ---------------------------------------------------------- framed map

@dataclasses.dataclass(frozen=True, slots=True)
class GroupTransform:
    """One shared-space group transform, in page-pt units.

    A gesture sets exactly one mode (move, resize, rotate); the other
    components default to identity, so the fixed pipeline order below
    is inconsequential for the supported gestures and only matters if a
    future gesture ever combines them.

    Pipeline: translate -> scale about ``anchor`` -> rotate about
    ``pivot``. Angles normalize through :func:`normalize_deg`; scales
    are expected strictly positive (never flip through the anchor —
    clamp with :func:`positive_scale_clamp` before construction).

    The scale step is expressed in the selection frame's local axes:
    ``frame_deg == 0`` scales along the page axes (the original
    behavior), a non-zero ``frame_deg`` rotates the anchor-relative
    displacement into that frame, scales, and rotates back — so a
    resize of a rotated selection follows the rotated frame instead of
    shearing along the screen axes.
    """

    dx: float = 0.0
    dy: float = 0.0
    anchor: Point = dataclasses.field(
        default_factory=lambda: Point(0.0, 0.0))
    sx: float = 1.0
    sy: float = 1.0
    pivot: Point = dataclasses.field(
        default_factory=lambda: Point(0.0, 0.0))
    angle_deg: float = 0.0
    #: Orientation (degrees, clockwise in y-down space) of the *selection
    #: frame* the resize is relative to. ``0`` scales along the page axes
    #: (the original behavior); a non-zero value scales along the frame's
    #: own rotated axes about ``anchor`` (M6.8.2 corrective: resizing a
    #: rotated selection follows the rotated frame). Only the scale step
    #: uses it; a pure move/rotate leaves it ``0``.
    frame_deg: float = 0.0

    @property
    def scales_identity(self) -> bool:
        return self.sx == 1.0 and self.sy == 1.0

    @property
    def rotates(self) -> bool:
        return normalize_deg(self.angle_deg) != 0.0

    @property
    def translates(self) -> bool:
        return self.dx != 0.0 or self.dy != 0.0


def _page_mapper(t: GroupTransform, ox: float, oy: float):
    """Point map for one page: ``p' = T(p + o_k) - o_k`` (page pt)."""
    ax = float(t.anchor.x)
    ay = float(t.anchor.y)
    sx = float(t.sx)
    sy = float(t.sy)
    dx = float(t.dx)
    dy = float(t.dy)
    nx = float(t.pivot.x)
    ny = float(t.pivot.y)
    angle = normalize_deg(t.angle_deg)
    rad = angle * _DEG2RAD
    frame = float(getattr(t, "frame_deg", 0.0))
    frad = frame * _DEG2RAD
    fcos = math.cos(frad) if frad != 0.0 else 1.0
    fsin = math.sin(frad) if frad != 0.0 else 0.0

    def fn(p) -> Point:
        # Shared space v = p + o_k == w / B.
        vx = float(p.x) + ox + dx
        vy = float(p.y) + oy + dy
        if frad != 0.0:
            # Scale along the (rotated) selection-frame axes about the
            # shared anchor: rotate into the frame, scale axis-aligned,
            # rotate back — a pure frame-axis scale, no page-axis shear.
            ex = vx - ax
            ey = vy - ay
            lx = ex * fcos + ey * fsin
            ly = -ex * fsin + ey * fcos
            lx *= sx
            ly *= sy
            vx = ax + lx * fcos - ly * fsin
            vy = ay + lx * fsin + ly * fcos
        else:
            # Scale about the shared anchor along the page axes.
            vx = ax + (vx - ax) * sx
            vy = ay + (vy - ay) * sy
        # Rotate about the shared pivot.
        if rad != 0.0:
            c = math.cos(rad)
            s = math.sin(rad)
            ex = vx - nx
            ey = vy - ny
            vx = nx + ex * c - ey * s
            vy = ny + ex * s + ey * c
        # Back to this page's page-local pt.
        return Point(vx - ox, vy - oy)

    return fn


def _coerce_offset(offset) -> tuple:
    """Offset ``o_k`` as ``(ox, oy)``; accepts a Point or ``(x, y)``."""
    try:
        return (float(offset[0]), float(offset[1]))
    except Exception:
        pass
    try:
        return (float(offset.x), float(offset.y))
    except Exception:
        return (0.0, 0.0)


def _bbox_rect(points) -> Rect:
    """Canonical normalized bounding ``Rect`` of mapped points."""
    xs = [float(p.x) for p in points]
    ys = [float(p.y) for p in points]
    minx, miny = min(xs), min(ys)
    return Rect(
        Point(minx, miny),
        Size(max(0.0, max(xs) - minx), max(0.0, max(ys) - miny)))


# ------------------------------------------------------------- dispatch

def _apply_free_points(annotation, points, t, ox, oy):
    """Ink/Highlighter-like candidates: map every point; width policy."""
    new_points = tuple(_page_mapper(t, ox, oy)(p) for p in points)
    kw = {}
    if not t.scales_identity:
        kw["width_mm"] = scale_width_mm(
            annotation.width_mm, t.sx, t.sy)
    return dataclasses.replace(annotation, points=new_points, **kw)


def _apply_point_shape(shape, t, ox, oy):
    """Line/Arrow/Polygon candidates: map points, rebuild ``rect``."""
    new_points = tuple(_page_mapper(t, ox, oy)(p) for p in shape.points)
    kw = {}
    if not t.scales_identity:
        kw["width_mm"] = scale_width_mm(shape.width_mm, t.sx, t.sy)
    return dataclasses.replace(
        shape, points=new_points, rect=_bbox_rect(new_points), **kw)


def _apply_box_shape(shape, t, ox, oy):
    """Rectangle/Ellipse candidates: local-frame scale + rotation.

    ``rect`` stores the *unrotated local box* and its center is the
    rotation pivot, so (single-op gestures; see :class:`GroupTransform`):
    * scale applies to the stored box about the shared anchor mapped
      contravariantly into the shape's own frame (linear part
      ``R(-r) S R(r)`` — no shear, the shape's ``rotation_deg`` is
      preserved);
    * rotation orbits the box center about the shared pivot and
      accumulates ``rotation_deg``;
    * translation moves the box origin; ``rotation_deg`` untouched.
    """
    rect = shape.rect
    if rect is None:
        return None
    try:
        x0, y0 = float(rect.x0), float(rect.y0)
        x1, y1 = float(rect.x1), float(rect.y1)
    except Exception:
        return None
    if not (x1 - x0 > 0.0) or not (y1 - y0 > 0.0):
        return None
    deg0 = normalize_deg(getattr(shape, "rotation_deg", 0.0))
    sx, sy = float(t.sx), float(t.sy)
    angle = normalize_deg(t.angle_deg)

    # 1) Local-frame scale about the per-page anchor mapped into the
    #    shape's unrotated frame (inverse rotation about the center).
    if not t.scales_identity:
        cx0 = (x0 + x1) / 2.0
        cy0 = (y0 + y1) / 2.0
        akx = float(t.anchor.x) - ox
        aky = float(t.anchor.y) - oy
        if deg0 != 0.0:
            inv = -deg0 * _DEG2RAD
            c = math.cos(inv)
            s = math.sin(inv)
            wdx = akx - cx0
            wdy = aky - cy0
            alx = cx0 + wdx * c - wdy * s
            aly = cy0 + wdx * s + wdy * c
        else:
            alx, aly = akx, aky
        nx0 = alx + (x0 - alx) * sx
        ny0 = aly + (y0 - aly) * sy
        nx1 = alx + (x1 - alx) * sx
        ny1 = aly + (y1 - aly) * sy
        rect = _bbox_rect((Point(nx0, ny0), Point(nx1, ny1)))

    # 2) Move the world center by the translate, then orbit it about
    #    the shared pivot; rebase the (unchanged-dims) box there.
    rect_center_x = (rect.x0 + rect.x1) / 2.0 + float(t.dx)
    rect_center_y = (rect.y0 + rect.y1) / 2.0 + float(t.dy)
    if angle != 0.0:
        moved = rotate_point(
            Point(rect_center_x, rect_center_y),
            float(t.pivot.x) - ox, float(t.pivot.y) - oy, angle)
        rect_center_x = moved.x
        rect_center_y = moved.y
    rect = dataclasses.replace(
        rect,
        origin=Point(
            rect_center_x - rect.width / 2.0,
            rect_center_y - rect.height / 2.0),
    )
    kw = {}
    if not t.scales_identity:
        kw["width_mm"] = scale_width_mm(shape.width_mm, sx, sy)
    kw["rotation_deg"] = normalize_deg(deg0 + angle)
    return dataclasses.replace(shape, rect=rect, **kw)


def apply_group(annotation, offset, t: GroupTransform):
    """One candidate annotation for the shared transform, or ``None``.

    ``offset`` is page ``k``'s ``o_k`` (a Point or ``(x, y)`` pair) —
    the *negated* ``_region_page_pt(entry_k, 0, 0)`` — so the same
    ``GroupTransform`` composes correctly on every page (see the module
    docstring). Returns ``None`` for anything this milestone cannot
    edit (e.g. a :class:`~annotations.models.StickyNote`) or for
    geometry that cannot be measured; supported types keep their
    identity, ``page_id``, ``created_at`` and every unrelated field.
    """
    if annotation is None or not isinstance(t, GroupTransform):
        return None
    ox, oy = _coerce_offset(offset)
    if isinstance(annotation, InkStroke):
        if not annotation.points:
            return None
        return _apply_free_points(annotation, annotation.points, t, ox, oy)
    if isinstance(annotation, Highlighter):
        if not annotation.points:
            return None
        return _apply_free_points(annotation, annotation.points, t, ox, oy)
    if isinstance(annotation, Shape):
        kind = annotation.kind
        if kind in (ShapeKind.LINE, ShapeKind.ARROW, ShapeKind.POLYGON):
            if not annotation.points:
                return None
            return _apply_point_shape(annotation, t, ox, oy)
        if kind in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE):
            return _apply_box_shape(annotation, t, ox, oy)
    return None


# ------------------------------------------------------ gesture helpers

def move_annotation(annotation, dx, dy):
    """Candidate for a completed group move by ``(dx, dy)`` page-pt.

    Translation is offset-independent (the per-page offset cancels), so
    no page offset is needed: every member of a group — on any page —
    receives the identical page-pt delta and relative positions are
    preserved exactly. Returns ``None`` for unsupported types.
    """
    return apply_group(annotation, (0.0, 0.0), GroupTransform(
        dx=float(dx), dy=float(dy)))


def resize_annotation(annotation, offset, anchor, sx, sy, *,
                      base_size=None, min_side=None, frame_deg=0.0):
    """Candidate for a completed group resize, or ``None``.

    ``offset`` is this annotation page's ``o_k`` (``-region_pt(0,0)``);
    ``anchor`` is the shared-space anchor (the *opposite* grip/corner of
    the group frame). ``sx``/``sy`` are positive per-axis scales along
    the frame's local axes (``frame_deg``, degrees clockwise in y-down
    space; ``0`` = page axes); with ``min_side`` (and the group frame's
    ``base_size`` dimensions) they are clamped through
    :func:`positive_scale_clamp` so no side can degenerate and no scale
    can flip through the anchor. Width policy: see
    :func:`scale_width_mm`. Rectangle/Ellipse members scale in their own
    unrotated local frame; point members scale along the frame axes.
    """
    base = (1.0, 1.0)
    if base_size is not None:
        try:
            base = (float(base_size[0]), float(base_size[1]))
        except Exception:
            base = (1.0, 1.0)
    if min_side is not None:
        sx, sy = positive_scale_clamp(sx, sy, base, min_side)
    anchor_pt = anchor if isinstance(anchor, Point) else Point(
        float(anchor[0]), float(anchor[1]))
    return apply_group(annotation, offset, GroupTransform(
        anchor=anchor_pt, sx=sx, sy=sy, frame_deg=float(frame_deg)))


def rotate_annotation(annotation, offset, pivot, angle_deg):
    """Candidate for a completed group rotation, or ``None``.

    ``offset`` is this annotation page's ``o_k``; ``pivot`` is the
    group frame's center in shared space. Point members rotate about
    the per-page pivot; Rectangle/Ellipse members orbit their box
    center and accumulate ``rotation_deg`` (normalized to
    ``(-180, 180]``). Widths are untouched by rotation.
    """
    if isinstance(pivot, Point):
        pivot_pt = pivot
    else:
        try:
            pivot_pt = Point(float(pivot[0]), float(pivot[1]))
        except Exception:
            return None
    return apply_group(annotation, offset, GroupTransform(
        pivot=pivot_pt, angle_deg=normalize_deg(angle_deg)))


#: Smallest editable box side, page pt: a handle drag can never collapse
#: a Rectangle/Ellipse into a degenerate box.
MIN_BOX_SIDE_PT = 1.0

#: Editable box handle names (clockwise, y-down, north-west first).
BOX_HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

_POINT_KINDS = (ShapeKind.LINE, ShapeKind.ARROW, ShapeKind.POLYGON)
_BOX_KINDS = (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE)


def vertex_points(shape):
    """Editable defining points of a Line/Arrow/Polygon, or ``None``.

    Lines and arrows expose their two endpoints (start then end, order
    preserved); polygons expose every ordered vertex. Box kinds have no
    free vertices — they use :func:`box_handle_points`.
    """
    if not isinstance(shape, Shape) or shape.kind not in _POINT_KINDS:
        return None
    points = tuple(shape.points or ())
    if len(points) < 2:
        return None
    return points


def box_handle_points(shape):
    """Page-local handle positions of a Rectangle/Ellipse box.

    Returns ``{name: Point}`` for the eight :data:`BOX_HANDLES` (corners
    and edge midpoints). Handles are rotated about the box centre by the
    shape's ``rotation_deg`` (clockwise, y-down), so they follow the
    rendered shape exactly; the canonical unrotated ``rect`` is unchanged.
    ``None`` for other kinds or a degenerate box.
    """
    if not isinstance(shape, Shape) or shape.kind not in _BOX_KINDS:
        return None
    try:
        rect = shape.rect
        x0, y0 = float(rect.x0), float(rect.y0)
        x1, y1 = float(rect.x1), float(rect.y1)
    except Exception:
        return None
    if not (x1 > x0) or not (y1 > y0):
        return None
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    mx, my = cx, cy
    angle = normalize_deg(getattr(shape, "rotation_deg", 0.0))
    local = {
        "nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0), "e": (x1, my),
        "se": (x1, y1), "s": (mx, y1), "sw": (x0, y1), "w": (x0, my),
    }
    out = {}
    for name, (px, py) in local.items():
        if angle == 0.0:
            out[name] = Point(px, py)
        else:
            out[name] = rotate_point(Point(px, py), cx, cy, angle)
    return out


def move_vertex(shape, index: int, target):
    """Candidate with one defining point moved to ``target`` (page pt).

    Point order, ``close_path`` and every unrelated field are preserved;
    the bounding ``rect`` is re-derived. Only Line/Arrow/Polygon shapes
    and an in-range index are accepted, so no other kind can acquire a
    free vertex through this path.
    """
    if not isinstance(shape, Shape) or shape.kind not in _POINT_KINDS:
        return None
    points = tuple(shape.points or ())
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None
    if not 0 <= index < len(points):
        return None
    try:
        moved = Point(float(target.x), float(target.y))
    except Exception:
        try:
            moved = Point(float(target[0]), float(target[1]))
        except Exception:
            return None
    new_points = list(points)
    new_points[index] = moved
    new_points = tuple(new_points)
    return dataclasses.replace(
        shape, points=new_points, rect=_bbox_rect(new_points))


def resize_box_handle(shape, handle: str, pointer, *,
                      aspect_lock: bool = False):
    """Candidate Rectangle/Ellipse after dragging one box handle.

    The pointer is mapped into the shape's unrotated local frame (the
    inverse of ``rotation_deg`` about the box centre), the corresponding
    edges are moved with a minimum side of :data:`MIN_BOX_SIDE_PT` (and
    never through the opposite edge), and the resulting box is re-anchored
    in page space so a rotated shape neither jumps nor shears. Widths and
    every unrelated field stay untouched.

    ``aspect_lock`` keeps the box square for recognized circles: corner
    handles scale uniformly about the opposite corner, edge handles act as
    a radius drag about the box centre. General (non-circular) ellipses
    use the free per-axis box handles.
    """
    if (not isinstance(shape, Shape) or shape.kind not in _BOX_KINDS
            or handle not in BOX_HANDLES):
        return None
    try:
        rect = shape.rect
        x0, y0 = float(rect.x0), float(rect.y0)
        x1, y1 = float(rect.x1), float(rect.y1)
        px = float(pointer.x)
        py = float(pointer.y)
    except Exception:
        return None
    if not (x1 > x0) or not (y1 > y0):
        return None
    angle = normalize_deg(getattr(shape, "rotation_deg", 0.0))
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    if angle != 0.0:
        local = rotate_point(Point(px, py), cx, cy, -angle)
        qx, qy = float(local.x), float(local.y)
    else:
        qx, qy = px, py

    left = handle in ("nw", "w", "sw")
    right = handle in ("ne", "e", "se")
    top = handle in ("nw", "n", "ne")
    bottom = handle in ("sw", "s", "se")
    corner = (left or right) and (top or bottom)
    floor = MIN_BOX_SIDE_PT

    if aspect_lock:
        if corner:
            # Anchor the opposite corner and scale uniformly.
            ax = x1 if left else x0
            ay = y1 if top else y0
            size = max(floor, abs(qx - ax), abs(qy - ay))
            sx = -1.0 if left else 1.0
            sy = -1.0 if top else 1.0
            nx0, nx1 = sorted((ax, ax + sx * size))
            ny0, ny1 = sorted((ay, ay + sy * size))
        else:
            # Radius handle: uniform scale about the (fixed) box centre.
            old_w, old_h = x1 - x0, y1 - y0
            if handle in ("e", "w"):
                target = max(floor, 2.0 * abs(qx - cx))
                scale = target / old_w if old_w > 0.0 else 1.0
            else:
                target = max(floor, 2.0 * abs(qy - cy))
                scale = target / old_h if old_h > 0.0 else 1.0
            new_w = max(floor, old_w * scale)
            new_h = max(floor, old_h * scale)
            nx0, nx1 = cx - new_w / 2.0, cx + new_w / 2.0
            ny0, ny1 = cy - new_h / 2.0, cy + new_h / 2.0
    else:
        nx0, ny0, nx1, ny1 = x0, y0, x1, y1
        if left:
            nx0 = min(qx, nx1 - floor)
        elif right:
            nx1 = max(qx, nx0 + floor)
        if top:
            ny0 = min(qy, ny1 - floor)
        elif bottom:
            ny1 = max(qy, ny0 + floor)

    # Re-anchor the (possibly moved) local box in page space: the local
    # midpoint rotates about the old centre, so a rotated box keeps its
    # orientation and never jumps.
    lmx, lmy = (nx0 + nx1) / 2.0, (ny0 + ny1) / 2.0
    if angle != 0.0:
        centred = rotate_point(Point(lmx, lmy), cx, cy, angle)
        mx, my = float(centred.x), float(centred.y)
    else:
        mx, my = lmx, lmy
    width = max(floor, nx1 - nx0)
    height = max(floor, ny1 - ny0)
    new_rect = Rect(
        Point(mx - width / 2.0, my - height / 2.0),
        Size(width, height))
    return dataclasses.replace(shape, rect=new_rect)


# ------------------------------------------------------ no-op predicate

def geometry_changed(original, candidate, eps: float = NOOP_EPS) -> bool:
    """True when a commit would write data meaningfully different.

    Float fields (points, rect coordinates, widths, angles, opacity…)
    compare within ``eps`` page-pt; every other field (identity,
    colors, pressure samples, timestamps, flags, strings) compares
    exactly. A candidate equal to its original is a no-op and must not
    open a history group.
    """
    if original is candidate:
        return False
    if original is None or candidate is None:
        return True
    if type(original) is not type(candidate):
        return True
    for f in dataclasses.fields(original):
        a = getattr(original, f.name)
        b = getattr(candidate, f.name)
        if isinstance(a, Point):
            if (abs(a.x - b.x) > eps or abs(a.y - b.y) > eps):
                return True
        elif isinstance(a, Rect):
            for name in ("x0", "y0", "x1", "y1"):
                if abs(getattr(a, name) - getattr(b, name)) > eps:
                    return True
        elif isinstance(a, tuple):
            if len(a) != len(b):
                return True
            for a_item, b_item in zip(a, b):
                if isinstance(a_item, Point):
                    if (abs(a_item.x - b_item.x) > eps
                            or abs(a_item.y - b_item.y) > eps):
                        return True
                elif isinstance(a_item, float):
                    if abs(a_item - b_item) > eps:
                        return True
                elif a_item != b_item:
                    return True
        elif isinstance(a, float):
            if abs(a - b) > eps:
                return True
        elif a != b:
            return True
    return False
