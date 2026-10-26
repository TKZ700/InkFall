"""Region-selection hit-testing for committed annotations. Qt-free.

Answers one question for the Selection/Lasso tools: *which committed
annotations' rendered geometry intersects this region?* Two region
shapes are supported:

* an **axis-aligned rectangle** (the Selection tool's marquee) --
  :func:`select_in_rect`; and
* a **closed polygon** (the Lasso tool's freehand loop) --
  :func:`select_in_polygon`.

The test uses each annotation's **actually rendered** geometry -- the
pen/highlighter centerline the renderer draws, at its rendered
(constant or pressure-variable) half-width, and a shape's visible
outline plus its filled interior -- never a bare bounding box. The
bounding extent is used only as a cheap broad-phase filter; the exact
test may reject a candidate whose box overlaps the region but whose
ink does not.

A companion **point pick** answers click selection (the Selection tool
and a Pan double-click): :func:`point_hits_annotation` tests one
annotation against a point, and :func:`pick_annotation` returns the
topmost hit in the viewer's established paint order (pen ink above
shapes above highlighters; later insertion wins).

Agreed M6.8.1 semantics (intersection-based):

* **Marquee**: an annotation is selected when its rendered geometry
  intersects the marquee rectangle -- the outline band (any outline
  segment within the band's half-width + ``tol`` of the rect), the
  filled interior, or containment in either direction (a filled shape
  fully containing the rect, or the rect fully containing the shape's
  outline). Complete enclosure is NOT required.
* **Lasso**: an annotation is selected when its ink *enters* the loop,
  crosses the loop boundary, or reaches it within the band's half-width
  + ``tol`` -- and, symmetrically, when the loop is fully contained by
  the annotation (a loop drawn inside a large filled shape, or inside a
  very thick stroke's band). Partially-enclosed and boundary-grazing
  geometry is selected; a disjoint annotation is not.

``tol`` is the gesture tolerance in page-local pt (the viewer converts
a fixed screen distance, so it stays zoom-correct). ``0.0`` means exact
geometry only.

This module is pure geometry: it never imports Qt, never mutates its
inputs, and never touches a store or a history.
"""

from __future__ import annotations

import math

from annotations.ink_geometry import (
    HIGHLIGHTER_BAND_ASPECT,
    pressure_widths,
    smooth_centerline,
)
from annotations.models import Highlighter, InkStroke, Shape
from annotations.shape_hit import (
    fill_contains,
    outline_polylines,
    shape_extent,
)

__all__ = [
    "annotation_extent",
    "clip_polygon_to_rect",
    "pick_annotation",
    "point_hits_annotation",
    "point_hits_shape",
    "point_hits_stroke",
    "polygon_area",
    "select_in_polygon",
    "select_in_rect",
    "stroke_centerline_geometry",
]

#: PDF points per millimetre (annotation tool widths are stored in mm).
_PT_PER_MM = 72.0 / 25.4


# --------------------------------------------------------------- helpers

def _points(annotation) -> list:
    """Page-local ``(x, y)`` pairs for a stroke/shape, skipping garbage."""
    out = []
    for p in (getattr(annotation, "points", ()) or ()):
        try:
            out.append((float(p.x), float(p.y)))
        except Exception:
            continue
    return out


def _base_width_pt(annotation) -> float:
    """Rendered full stroke width in page pt, or ``0.0`` when unusable."""
    try:
        base = abs(float(annotation.width_mm)) * _PT_PER_MM
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(base) or base <= 0.0:
        return 0.0
    return base


# ----------------------------------------------------- rendered geometry

def stroke_centerline_geometry(annotation):
    """Rendered ``(centers, half_widths)`` in page pt, or ``None``.

    Mirrors the viewer's paint/eraser geometry exactly: the smoothed
    centerline the renderer draws, with a constant half-width for a
    Pressure-OFF pen/highlighter stroke and pressure-variable
    half-widths (via ``pressure_widths``) for a Pressure-ON
    ``InkStroke`` and for a ``Highlighter`` that carries per-sample
    pressures. ``None`` when there is no drawable geometry (no points, or
    a non-positive/unusable width).
    """
    raw = _points(annotation)
    if not raw:
        return None
    base = _base_width_pt(annotation)
    if base <= 0.0:
        return None
    half = base / 2.0
    pressures = getattr(annotation, "pressures", ())
    if isinstance(annotation, InkStroke):
        pressure_on = (bool(getattr(annotation, "pressure_enabled", False))
                       and len(pressures) == len(raw))
        profile = getattr(annotation, "pressure_profile", "normal")
    elif isinstance(annotation, Highlighter):
        # M7.1.4: the highlighter always uses the default curve; a mouse
        # stroke stores all-1.0 pressures, which maps to the nominal width.
        pressure_on = bool(pressures) and len(pressures) == len(raw)
        profile = "normal"
    else:
        pressure_on = False
        profile = "normal"
    if pressure_on:
        if isinstance(annotation, Highlighter):
            half = half * HIGHLIGHTER_BAND_ASPECT
        try:
            widths = pressure_widths(tuple(pressures), base, profile)
            spts, swidths = smooth_centerline(raw, widths)
        except ValueError:
            return (tuple(raw), (half,) * len(raw))
        if swidths is None or len(swidths) != len(spts):
            return (tuple(spts), (half,) * len(spts))
        halves = tuple(max(0.0, float(w)) / 2.0 for w in swidths)
        if isinstance(annotation, Highlighter):
            halves = tuple(h * HIGHLIGHTER_BAND_ASPECT for h in halves)
        return (tuple(spts), halves)
    try:
        spts, _ = smooth_centerline(raw, None)
    except ValueError:
        spts = tuple(raw)
    if isinstance(annotation, Highlighter):
        # The painted nib is a vertical rectangle: its visible half-height
        # is ``aspect`` times the half-width, so a circular hit radius that
        # covers the whole band is the band half-height.
        half = half * HIGHLIGHTER_BAND_ASPECT
    return (tuple(spts), (half,) * len(spts))


def annotation_extent(annotation):
    """Page-pt ``(minx, miny, maxx, maxy)`` of rendered geometry, or None.

    For a ``Shape`` this is ``shape_hit``'s visible-outline extent
    (rect-based kinds use the box, point-based kinds the point bbox);
    for a stroke it is the raw-point bounding box. Both are padded by
    the outline half-width so a thick stroke's frame encloses its ink.
    A highlighter is padded asymmetrically: ``half`` horizontally and the
    vertical band half-height (``aspect * half``) vertically, matching the
    centred rectangular nib the painter sweeps.
    """
    if annotation is None:
        return None
    half = _base_width_pt(annotation) / 2.0
    half_y = half
    if isinstance(annotation, Highlighter):
        half_y = half * HIGHLIGHTER_BAND_ASPECT
    if isinstance(annotation, Shape):
        try:
            extent = shape_extent(annotation)
        except Exception:
            return None
        if extent is None:
            return None
    else:
        pts = _points(annotation)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        extent = (min(xs), min(ys), max(xs), max(ys))
    return (extent[0] - half, extent[1] - half_y,
            extent[2] + half, extent[3] + half_y)


# ------------------------------------------------ rectangle primitives

def _rect_contains(rect, x: float, y: float) -> bool:
    minx, miny, maxx, maxy = rect
    return minx <= x <= maxx and miny <= y <= maxy


def _grow_rect(rect, pad: float) -> tuple:
    return (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)


def _rects_overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _rect_inside(outer, inner) -> bool:
    """True when ``inner`` lies fully within ``outer``."""
    return (outer[0] <= inner[0] and inner[2] <= outer[2]
            and outer[1] <= inner[1] and inner[3] <= outer[3])


def _segment_intersects_rect(x0: float, y0: float, x1: float, y1: float,
                             rect) -> bool:
    """True when the segment touches or crosses the axis-aligned rect.

    Touching counts: an endpoint on the rect boundary (or a segment
    lying along an edge) is contact. Inclusive bounds keep that
    deterministic.
    """
    minx, miny, maxx, maxy = rect
    # Bbox quick reject.
    if (max(x0, x1) < minx or min(x0, x1) > maxx
            or max(y0, y1) < miny or min(y0, y1) > maxy):
        return False
    # An endpoint inside (bounds inclusive) is contact.
    if _rect_contains(rect, x0, y0) or _rect_contains(rect, x1, y1):
        return True
    # Both endpoints strictly beyond one side -> no crossings.
    if (x0 < minx and x1 < minx) or (x0 > maxx and x1 > maxx):
        return False
    if (y0 < miny and y1 < miny) or (y0 > maxy and y1 > maxy):
        return False
    # Otherwise contact means a true edge crossing.
    edges = (((minx, miny), (maxx, miny)), ((maxx, miny), (maxx, maxy)),
             ((maxx, maxy), (minx, maxy)), ((minx, maxy), (minx, miny)))
    for (ea, eb) in edges:
        if _segments_cross(x0, y0, x1, y1, ea[0], ea[1], eb[0], eb[1]):
            return True
    return False


def _polyline_hits_rect(points, rect) -> bool:
    """True when any polyline segment touches the rect (or lone point)."""
    for i in range(len(points) - 1):
        if _segment_intersects_rect(points[i][0], points[i][1],
                                    points[i + 1][0], points[i + 1][1],
                                    rect):
            return True
    if len(points) == 1:
        return _rect_contains(rect, points[0][0], points[0][1])
    return False


# ------------------------------------------------ polygon primitives

def clip_polygon_to_rect(polygon, rect) -> tuple:
    """Clip a (possibly concave) polygon to an axis-aligned rect.

    Sutherland–Hodgman against each rect half-plane in turn. Used by
    the viewer to clip a widget-space lasso loop to one page's
    displayed bounds before converting that page's coordinates. The
    result is a new vertex list (a subset of the input plus boundary
    intersections); ``()`` when nothing survives. Degenerate output
    consumes: callers re-check the vertex count.
    """
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    r = (min(rect[0], rect[2]), min(rect[1], rect[3]),
         max(rect[0], rect[2]), max(rect[1], rect[3]))

    def clip_edge(points, inside, intersect):
        if not points:
            return ()
        out = []
        prev = points[-1]
        prev_in = inside(prev)
        for curr in points:
            curr_in = inside(curr)
            if curr_in != prev_in:
                out.append(intersect(prev, curr))
            if curr_in:
                out.append(curr)
            prev = curr
            prev_in = curr_in
        return tuple(out)

    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    def clip_axis(points, index, bound, keep_less):
        def inside(point):
            value = point[index]
            return value <= bound if keep_less else value >= bound

        def intersect(a, b):
            va = a[index]
            vb = b[index]
            denom = vb - va
            t = 0.0 if denom == 0.0 else (bound - va) / denom
            return lerp(a, b, t)

        return clip_edge(points, inside, intersect)

    points = tuple((float(x), float(y))
                   for x, y in zip(xs, ys))
    # Order: left, right, top, bottom. Always keep the half-plane that
    # lies INSIDE the rect: x >= left, x <= right, y >= top, y <= bottom.
    points = clip_axis(points, 0, r[0], False)
    points = clip_axis(points, 0, r[2], True)
    points = clip_axis(points, 1, r[1], False)
    points = clip_axis(points, 1, r[3], True)
    return points


def polygon_area(polygon) -> float:
    """Unsigned shoelace area of the (implicitly closed) polygon."""
    if not polygon or len(polygon) < 3:
        return 0.0
    area = 0.0
    n = len(polygon)
    for i in range(n):
        x0, y0 = polygon[i]
        x1, y1 = polygon[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return abs(area / 2.0)


def _point_in_polygon(polygon, x: float, y: float) -> bool:
    """Strict interior ray-casting (boundary contact tested separately)."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _segments_cross(ax0: float, ay0: float, ax1: float, ay1: float,
                    bx0: float, by0: float, bx1: float, by1: float) -> bool:
    def orient(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = orient(bx0, by0, bx1, by1, ax0, ay0)
    d2 = orient(bx0, by0, bx1, by1, ax1, ay1)
    d3 = orient(ax0, ay0, ax1, ay1, bx0, by0)
    d4 = orient(ax0, ay0, ax1, ay1, bx1, by1)
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _point_seg_distance(px: float, py: float, x0: float, y0: float,
                        x1: float, y1: float) -> float:
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


def _segment_hits_loop(points, polygon, pad: float) -> bool:
    """Ink segment within ``pad`` of any loop edge (incl. crossing)."""
    if len(points) < 2:
        return False
    n = len(polygon)
    ax, ay = points[0]
    bx, by = points[1]
    # Per-edge bbox quick reject: the segment's bbox grown by ``pad``
    # must overlap the loop edge's bbox before exact tests run (long
    # centerlines against long loops otherwise degenerate into O(S*V)
    # exact evaluations; NaN-safe since all inputs are finite floats).
    sbx0 = min(ax, bx) - pad
    sbx1 = max(ax, bx) + pad
    sby0 = min(ay, by) - pad
    sby1 = max(ay, by) + pad
    for i in range(n):
        cx0, cy0 = polygon[i]
        cx1, cy1 = polygon[(i + 1) % n]
        # Bbox reject (exact when separated; crossing impossible).
        if (max(cx0, cx1) < sbx0 or min(cx0, cx1) > sbx1
                or max(cy0, cy1) < sby0 or min(cy0, cy1) > sby1):
            continue
        # A proper crossing counts regardless of pad.
        if _segments_cross(ax, ay, bx, by, cx0, cy0, cx1, cy1):
            return True
        if pad > 0.0:
            if (_point_seg_distance(ax, ay, cx0, cy0, cx1, cy1) <= pad
                    or _point_seg_distance(bx, by, cx0, cy0, cx1, cy1)
                    <= pad):
                return True
            # Pad reach from the loop side: a loop vertex within ``pad``
            # of the segment (covers a tiny loop fully inside a thick
            # ink band). Restrict to the segment's bbox window.
            for px, py in polygon:
                if (sbx0 <= px <= sbx1 and sby0 <= py <= sby1
                        and _point_seg_distance(px, py, ax, ay, bx,
                                                by) <= pad):
                    return True
    return False


def _point_hits_loop(point, polygon, pad: float) -> bool:
    """Ink point inside the loop or within ``pad`` of the boundary."""
    px, py = point
    if _point_in_polygon(polygon, px, py):
        return True
    if pad <= 0.0:
        return False
    n = len(polygon)
    for i in range(n):
        cx0, cy0 = polygon[i]
        cx1, cy1 = polygon[(i + 1) % n]
        if _point_seg_distance(px, py, cx0, cy0, cx1, cy1) <= pad:
            return True
    return False


def _polygon_bbox(polygon):
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _region_hits_stroke(centers, halves, rect, polygon, tol: float) -> bool:
    """One stroke's rendered band against the rect or the loop.

    ``rect`` and ``polygon`` are mutually exclusive. The band is each
    smoothed segment at its rendered (pressure-variable) half-width,
    grown by ``tol``.
    """
    if polygon is not None:
        for i in range(len(centers) - 1):
            ax, ay = centers[i]
            bx, by = centers[i + 1]
            pad = max(halves[i], halves[i + 1]) + tol
            # Ink enters the loop (endpoint inside)...
            if (_point_in_polygon(polygon, ax, ay)
                    or _point_in_polygon(polygon, bx, by)):
                return True
            # ...or reaches / crosses the boundary within pad...
            if _segment_hits_loop(((ax, ay), (bx, by)), polygon, pad):
                return True
        # ...or a lone centre sample.
        if len(centers) == 1:
            return _point_hits_loop(centers[0], polygon, halves[0] + tol)
        return False
    if rect is None:
        return False
    for i in range(len(centers) - 1):
        ax, ay = centers[i]
        bx, by = centers[i + 1]
        limit = max(halves[i], halves[i + 1]) + tol
        if _segment_intersects_rect(ax, ay, bx, by, _grow_rect(rect, limit)):
            return True
    if len(centers) == 1:
        limit = halves[0] + tol
        cx, cy = centers[0]
        gx0, gy0, gx1, gy1 = _grow_rect(rect, limit)
        return gx0 <= cx <= gx1 and gy0 <= cy <= gy1
    return False


def _region_hits_shape(shape, rect, polygon, tol: float) -> bool:
    """One shape's rendered geometry against the rect or the loop.

    The visible outline band (half-width + ``tol``) and the filled
    interior both count; containment in either direction counts.
    """
    half = _base_width_pt(shape) / 2.0
    reach = half + tol
    if polygon is not None:
        for poly in outline_polylines(shape):
            if len(poly) == 1:
                if _point_hits_loop(poly[0], polygon, reach):
                    return True
                continue
            for i in range(len(poly) - 1):
                ax, ay = poly[i]
                bx, by = poly[i + 1]
                if (_point_in_polygon(polygon, ax, ay)
                        or _point_in_polygon(polygon, bx, by)):
                    return True
                if _segment_hits_loop(((ax, ay), (bx, by)), polygon, reach):
                    return True
        # Filled interior: any loop vertex inside the fill.
        if bool(getattr(shape, "fill_enabled", False)):
            for px, py in polygon:
                try:
                    if fill_contains(shape, px, py):
                        return True
                except Exception:
                    continue
        return False
    if rect is None:
        return False
    # Outline band vs the rect.
    for poly in outline_polylines(shape):
        if len(poly) == 1:
            px, py = poly[0]
            gx0, gy0, gx1, gy1 = _grow_rect(rect, reach)
            if gx0 <= px <= gx1 and gy0 <= py <= gy1:
                return True
            continue
        for i in range(len(poly) - 1):
            if _segment_intersects_rect(poly[i][0], poly[i][1],
                                        poly[i + 1][0], poly[i + 1][1],
                                        _grow_rect(rect, reach)):
                return True
    # Filled interior: any fill sample point inside the rect (corners +
    # centre cover most cases) plus reverse containment (a fill region
    # larger than the rect but fully containing it has no sample inside).
    if bool(getattr(shape, "fill_enabled", False)):
        minx, miny, maxx, maxy = rect
        samples = ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy),
                   ((minx + maxx) / 2.0, (miny + maxy) / 2.0))
        for px, py in samples:
            try:
                if fill_contains(shape, px, py):
                    return True
            except Exception:
                continue
        # A filled rect/ellipse whose own extent sits fully inside the
        # marquee: the interior definitely intersects the rect.
        try:
            extent = shape_extent(shape)
            if extent is not None and _rect_inside(rect, extent):
                return True
        except Exception:
            pass
    return False


def _region_hits_annotation(annotation, rect, polygon, tol: float) -> bool:
    """Dispatch to the rendered-geometry test for the given region."""
    if annotation is None:
        return False
    if isinstance(annotation, Shape):
        return _region_hits_shape(annotation, rect, polygon, tol)
    if isinstance(annotation, (InkStroke, Highlighter)):
        geometry = stroke_centerline_geometry(annotation)
        if geometry is None:
            return False
        centers, halves = geometry
        return _region_hits_stroke(centers, halves, rect, polygon, tol)
    # Unknown kinds are simply not selectable rather than mis-hit.
    return False


def _extent_hits_region(extent, rect, poly_bbox, pad: float) -> bool:
    """Broad-phase: the extent's bbox overlaps the region's bbox."""
    if poly_bbox is not None:
        lx0, ly0, lx1, ly1 = poly_bbox
        poly_bbox = (lx0 - pad, ly0 - pad, lx1 + pad, ly1 + pad)
        return _rects_overlap(extent, poly_bbox)
    if rect is None:
        return False
    return _rects_overlap(extent, _grow_rect(rect, pad))


# --------------------------------------------------------- point pick

def _layer_rank(annotation) -> int:
    """Paint-order layer rank (higher paints on top), or ``-1``.

    Mirrors the viewer's fixed per-page layer order
    ``highlighters < shapes < pen ink`` (see ``PdfViewer._paint_body``):
    pen ink is drawn last, so it is "topmost" above a shape that overlies
    it, and a shape above a highlighter. Unknown kinds are unselectable.
    """
    if isinstance(annotation, InkStroke):
        return 2
    if isinstance(annotation, Shape):
        return 1
    if isinstance(annotation, Highlighter):
        return 0
    return -1


def point_hits_stroke(centers, halves, x: float, y: float,
                      tol: float = 0.0) -> bool:
    """A stroke's rendered band contains ``(x, y)``.

    ``centers``/``halves`` are :func:`stroke_centerline_geometry`'s
    smoothed centerline and per-sample half-widths; the point selects
    when it is within ``half_width + tol`` of any centerline segment.
    """
    if not centers:
        return False
    tol = max(0.0, float(tol))
    if len(centers) == 1:
        cx, cy = centers[0]
        return math.hypot(cx - x, cy - y) <= halves[0] + tol
    for i in range(len(centers) - 1):
        ax, ay = centers[i]
        bx, by = centers[i + 1]
        limit = max(halves[i], halves[i + 1]) + tol
        if _point_seg_distance(x, y, ax, ay, bx, by) <= limit:
            return True
    return False


def point_hits_shape(shape, x: float, y: float, tol: float = 0.0) -> bool:
    """A shape's outline band or filled interior contains ``(x, y)``."""
    reach = _base_width_pt(shape) / 2.0 + max(0.0, float(tol))
    for poly in outline_polylines(shape):
        if len(poly) == 1:
            px, py = poly[0]
            if math.hypot(px - x, py - y) <= reach:
                return True
            continue
        for i in range(len(poly) - 1):
            if _point_seg_distance(x, y, poly[i][0], poly[i][1],
                                   poly[i + 1][0], poly[i + 1][1]) <= reach:
                return True
    if bool(getattr(shape, "fill_enabled", False)):
        try:
            if fill_contains(shape, x, y):
                return True
        except Exception:
            pass
    return False


def point_hits_annotation(annotation, x: float, y: float,
                          tol: float = 0.0) -> bool:
    """True when the annotation's *rendered* geometry contains ``(x, y)``.

    Same rendered-geometry contract as the region tests: a stroke's
    centerline band (constant or pressure-variable half-widths) or a
    shape's visible outline band plus its filled interior. Never a bare
    bounding box.
    """
    if annotation is None:
        return False
    if isinstance(annotation, Shape):
        return point_hits_shape(annotation, x, y, tol)
    if isinstance(annotation, (InkStroke, Highlighter)):
        geometry = stroke_centerline_geometry(annotation)
        if geometry is None:
            return False
        centers, halves = geometry
        return point_hits_stroke(centers, halves, x, y, tol)
    return False


def pick_annotation(annotations, x: float, y: float, tol: float = 0.0):
    """Topmost annotation whose rendered geometry contains ``(x, y)``.

    ``annotations`` is one page's annotations in store (insertion) order.
    Pick order matches the render order exactly: pen ink is on top of
    shapes, which are on top of highlighters, and within a layer the
    later-inserted (later-painted) annotation wins. Returns the
    annotation, or ``None`` when nothing is hit.
    """
    tol = max(0.0, float(tol))
    best = None
    best_key = None
    for index, annotation in enumerate(annotations or ()):
        rank = _layer_rank(annotation)
        if rank < 0:
            continue
        if not point_hits_annotation(annotation, x, y, tol):
            continue
        key = (rank, index)
        if best_key is None or key > best_key:
            best_key = key
            best = annotation
    return best


# ----------------------------------------------------------- selection

def select_in_rect(annotations, rect, tol: float = 0.0) -> tuple:
    """Annotations whose rendered geometry intersects ``rect`` (ordered).

    ``rect`` is ``(minx, miny, maxx, maxy)`` in page-local pt.
    ``tol`` is the pick tolerance in page pt. Returns annotations in
    caller order (document order). Broad phase: the annotation's
    rendered extent (already padded by its half-width) must overlap the
    rect. Narrow phase: actual rendered geometry -- the stroke
    centerline band (constant or pressure-variable half-widths) or the
    shape outline band plus its filled interior.
    """
    tol = max(0.0, float(tol))
    rect = (min(rect[0], rect[2]), min(rect[1], rect[3]),
            max(rect[0], rect[2]), max(rect[1], rect[3]))
    hits = []
    for annotation in annotations or ():
        extent = annotation_extent(annotation)
        if extent is None:
            continue
        if not _extent_hits_region(extent, rect, None, tol):
            continue
        if _region_hits_annotation(annotation, rect, None, tol):
            hits.append(annotation)
    return tuple(hits)


def select_in_polygon(annotations, polygon, tol: float = 0.0) -> tuple:
    """Annotations whose ink enters or crosses ``polygon`` (ordered).

    ``polygon`` is a sequence of page-local ``(x, y)`` pairs forming an
    implicitly closed loop; ``tol`` is the pick tolerance in page pt.
    Returns annotations in caller order. Intersection-based semantics:
    ink inside the loop, crossing the boundary, or within the band's
    half-width + ``tol`` of the boundary selects; full enclosure is not
    required. Fewer than three points, or a zero-area loop, selects
    nothing.
    """
    polygon = tuple(polygon or ())
    if len(polygon) < 3 or polygon_area(polygon) <= 0.0:
        return ()
    tol = max(0.0, float(tol))
    bounds = _polygon_bbox(polygon)
    hits = []
    for annotation in annotations or ():
        extent = annotation_extent(annotation)
        if extent is None:
            continue
        if not _extent_hits_region(extent, None, bounds, tol):
            continue
        if _region_hits_annotation(annotation, None, polygon, tol):
            hits.append(annotation)
    return tuple(hits)
