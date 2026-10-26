"""Shape hit-testing geometry (M6.7.2). Qt-free, stdlib-only.

Answers one question for the viewer's eraser: *does this eraser path
touch a committed* :class:`~annotations.models.Shape`'s **visible**
geometry? For every supported kind the visible outline (Line/Arrow
shaft, Rectangle/Ellipse boundary, Polygon edges) is derived per kind
and tested for proximity/intersection, and the **filled interior** of a
filled shape counts as contact too.

Shapes are always erased in full (both eraser modes), so no clipping,
fragmenting, or subtraction geometry lives here. This module is pure
geometry: it never imports Qt, never mutates its inputs, and never
touches a store or a history.
"""

from __future__ import annotations

import math

from annotations.models import ShapeKind
from annotations.transform import normalize_deg, rotate_point
from core.types import Point

__all__ = [
    "outline_polylines",
    "fill_contains",
    "segment_hits_shape",
    "shape_extent",
    "shape_rotation_deg",
]

#: Flattening resolution for an ellipse outline.
_ELLIPSE_STEPS = 72
_EPS = 1e-9


# --------------------------------------------------------------- helpers

def shape_rotation_deg(shape) -> float:
    """The shape's local rotation; only rectangle/ellipse kinds rotate.

    M6.8.2: those two kinds store their *unrotated local box* in
    ``rect`` and rotate it about its own center by ``rotation_deg``
    (positive = clockwise, y-down) — every outline/fill/extent answer
    here is rotation-aware. All other kinds (and non-shapes) are 0.0
    and behave exactly as before, so zero-rotation (default) data
    follows byte-identical code paths.
    """
    kind = getattr(shape, "kind", None)
    if kind not in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE):
        return 0.0
    return normalize_deg(getattr(shape, "rotation_deg", 0.0))


def _points(shape) -> list:
    out = []
    for p in (getattr(shape, "points", ()) or ()):
        try:
            out.append((float(p.x), float(p.y)))
        except Exception:
            continue
    return out


def _rect_ring(rect) -> tuple:
    x0, y0 = float(rect.x0), float(rect.y0)
    x1, y1 = float(rect.x1), float(rect.y1)
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def _rotate_ring(ring, rect, deg: float) -> tuple:
    """Rotate a rect-space ring about the rect's center (its pivot).

    The rotation pivots on the *stored unrotated box's* center — the
    same pivot the painter and :mod:`annotations.transform` use — so
    outline rings, fill regions and extents stay consistent with the
    rendered result. ``deg == 0`` returns the ring untouched.
    """
    if not ring or deg == 0.0:
        return ring
    cx = (float(rect.x0) + float(rect.x1)) / 2.0
    cy = (float(rect.y0) + float(rect.y1)) / 2.0
    pts = []
    for x, y in ring:
        rp = rotate_point(Point(x, y), cx, cy, deg)
        pts.append((rp.x, rp.y))
    return tuple(pts)


def _ellipse_ring(rect, steps: int = _ELLIPSE_STEPS) -> tuple:
    cx = (float(rect.x0) + float(rect.x1)) / 2.0
    cy = (float(rect.y0) + float(rect.y1)) / 2.0
    rx = abs(float(rect.x1) - float(rect.x0)) / 2.0
    ry = abs(float(rect.y1) - float(rect.y0)) / 2.0
    if rx <= 0.0 or ry <= 0.0:
        return ()
    n = max(8, int(steps))
    return tuple(
        (cx + rx * math.cos(2.0 * math.pi * i / n),
         cy + ry * math.sin(2.0 * math.pi * i / n))
        for i in range(n))


def _closed(poly) -> tuple:
    """Append the first point to make a closed ring (loop path)."""
    if len(poly) >= 2 and poly[0] != poly[-1]:
        return tuple(poly) + (poly[0],)
    return tuple(poly)


def _native_fill_ring(shape) -> tuple:
    """The fill region of a shape, or ``()`` when it has none."""
    if not bool(getattr(shape, "fill_enabled", False)):
        return ()
    kind = getattr(shape, "kind", None)
    deg = shape_rotation_deg(shape)
    pts = _points(shape)
    if kind == ShapeKind.RECTANGLE:
        return _rotate_ring(_rect_ring(shape.rect), shape.rect, deg)
    if kind == ShapeKind.ELLIPSE:
        return _rotate_ring(_ellipse_ring(shape.rect), shape.rect, deg)
    if kind == ShapeKind.POLYGON:
        if len(pts) >= 3 and bool(getattr(shape, "close_path", False)):
            return tuple(pts)
    return ()


def outline_polylines(shape) -> tuple:
    """The shape's visible outline as one or more open polylines.

    Rotation-aware for rectangle/ellipse (M6.8.2): their rings rotate
    about the stored box's center by ``shape_rotation_deg``; zero
    rotation keeps the exact pre-M6.8.2 geometry.
    """
    kind = getattr(shape, "kind", None)
    deg = shape_rotation_deg(shape)
    pts = _points(shape)
    if kind in (ShapeKind.LINE, ShapeKind.ARROW):
        return (tuple(pts),) if len(pts) >= 2 else ()
    if kind == ShapeKind.RECTANGLE:
        return (_closed(_rotate_ring(_rect_ring(shape.rect), shape.rect,
                                     deg)),)
    if kind == ShapeKind.ELLIPSE:
        ring = _rotate_ring(_ellipse_ring(shape.rect), shape.rect, deg)
        return (_closed(ring),) if ring else ()
    if kind == ShapeKind.POLYGON:
        if len(pts) >= 2:
            if bool(getattr(shape, "close_path", False)):
                return (_closed(tuple(pts)),)
            return (tuple(pts),)
        return ()
    return ()


def shape_extent(shape):
    """Conservative ``(minx, miny, maxx, maxy)`` of the visible outline.

    Rotation-aware for rectangle/ellipse (M6.8.2): the extent is the
    AABB of the *rendered* (possibly rotated) geometry — for a
    rectangle the exact rotated corners, for an ellipse the exact
    rotated-ellipse half-extents ``sqrt(rx^2 c^2 + ry^2 s^2)``. Zero
    rotation (all pre-M6.8.2 data) returns the stored box exactly as
    before, with no float drift. Point-based kinds use the point bbox.
    """
    kind = getattr(shape, "kind", None)
    if kind in (ShapeKind.LINE, ShapeKind.ARROW, ShapeKind.POLYGON):
        pts = _points(shape)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))
    rect = getattr(shape, "rect", None)
    if rect is None:
        return None
    deg = shape_rotation_deg(shape)
    if deg == 0.0:
        return (float(rect.x0), float(rect.y0),
                float(rect.x1), float(rect.y1))
    cx = (float(rect.x0) + float(rect.x1)) / 2.0
    cy = (float(rect.y0) + float(rect.y1)) / 2.0
    if kind == ShapeKind.RECTANGLE:
        ring = _rotate_ring(_rect_ring(rect), rect, deg)
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (min(xs), min(ys), max(xs), max(ys))
    if kind == ShapeKind.ELLIPSE:
        rx = abs(float(rect.x1) - float(rect.x0)) / 2.0
        ry = abs(float(rect.y1) - float(rect.y0)) / 2.0
        if rx <= 0.0 or ry <= 0.0:
            return None
        rad = deg * math.pi / 180.0
        c = abs(math.cos(rad))
        s = abs(math.sin(rad))
        hw = math.sqrt(rx * rx * c * c + ry * ry * s * s)
        hh = math.sqrt(rx * rx * s * s + ry * ry * c * c)
        return (cx - hw, cy - hh, cx + hw, cy + hh)
    return None


# ------------------------------------------------------------- geometry

def _ring_contains(ring, x: float, y: float) -> bool:
    """Ray-casting point-in-polygon (boundary counts as inside)."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + _EPS) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def fill_contains(shape, x: float, y: float) -> bool:
    """True when ``(x, y)`` lies inside the shape's fill region.

    Rotation-aware for rectangle/ellipse (M6.8.2): the query point is
    mapped into the shape's unrotated local frame (inverse rotation
    about the stored box's center) before the axis-aligned test, so the
    *rendered* rotated fill is measured exactly. Zero rotation takes
    the identical pre-M6.8.2 code path.
    """
    kind = getattr(shape, "kind", None)
    if kind == ShapeKind.RECTANGLE:
        rect = shape.rect
        lx, ly = _into_local_frame(shape, rect, x, y)
        return (float(rect.x0) <= lx <= float(rect.x1)
                and float(rect.y0) <= ly <= float(rect.y1))
    if kind == ShapeKind.ELLIPSE:
        rect = shape.rect
        lx, ly = _into_local_frame(shape, rect, x, y)
        cx = (float(rect.x0) + float(rect.x1)) / 2.0
        cy = (float(rect.y0) + float(rect.y1)) / 2.0
        rx = abs(float(rect.x1) - float(rect.x0)) / 2.0
        ry = abs(float(rect.y1) - float(rect.y0)) / 2.0
        if rx <= 0.0 or ry <= 0.0:
            return False
        return ((lx - cx) / rx) ** 2 + ((ly - cy) / ry) ** 2 <= 1.0
    if kind == ShapeKind.POLYGON:
        ring = _native_fill_ring(shape)
        return bool(ring) and _ring_contains(ring, x, y)
    return False


def _into_local_frame(shape, rect, x: float, y: float) -> tuple:
    """World point -> the shape's unrotated local frame (rect kinds)."""
    deg = shape_rotation_deg(shape)
    if deg == 0.0:
        return (x, y)
    rp = rotate_point(
        Point(float(x), float(y)),
        (float(rect.x0) + float(rect.x1)) / 2.0,
        (float(rect.y0) + float(rect.y1)) / 2.0,
        -deg)
    return (rp.x, rp.y)


def _point_seg_distance(px, py, x0, y0, x1, y1) -> float:
    dx = x1 - x0
    dy = y1 - y0
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _segments_cross(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1) -> bool:
    def orient(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = orient(bx0, by0, bx1, by1, ax0, ay0)
    d2 = orient(bx0, by0, bx1, by1, ax1, ay1)
    d3 = orient(ax0, ay0, ax1, ay1, bx0, by0)
    d4 = orient(ax0, ay0, ax1, ay1, bx1, by1)
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _seg_seg_distance(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1) -> float:
    if _segments_cross(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
        return 0.0
    return min(
        _point_seg_distance(ax0, ay0, bx0, by0, bx1, by1),
        _point_seg_distance(ax1, ay1, bx0, by0, bx1, by1),
        _point_seg_distance(bx0, by0, ax0, ay0, ax1, ay1),
        _point_seg_distance(bx1, by1, ax0, ay0, ax1, ay1))


def segment_hits_shape(shape, x0: float, y0: float, x1: float, y1: float,
                       reach: float, half: float) -> bool:
    """Does the eraser touch the visible shape (outline OR filled region)?

    Contact with the rendered outline **or** the rendered fill region
    counts. ``reach`` is the eraser radius plus antialias tolerance;
    ``half`` is the outline half-width, in page units.
    """
    limit = half + reach
    for poly in outline_polylines(shape):
        if len(poly) == 1:
            if _point_seg_distance(poly[0][0], poly[0][1],
                                   x0, y0, x1, y1) <= limit:
                return True
            continue
        for i in range(len(poly) - 1):
            if _seg_seg_distance(x0, y0, x1, y1,
                                 poly[i][0], poly[i][1],
                                 poly[i + 1][0], poly[i + 1][1]) <= limit:
                return True
    if bool(getattr(shape, "fill_enabled", False)) and _native_fill_ring(shape):
        for px, py in ((x0, y0), (x1, y1),
                       ((x0 + x1) / 2.0, (y0 + y1) / 2.0)):
            if fill_contains(shape, px, py):
                return True
    return False
