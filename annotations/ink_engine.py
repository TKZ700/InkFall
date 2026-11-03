"""Recovered variable-width ink kernel (Drawboard PDF 6.91.6.0 reference).

Ported from the research handoff's ``ink_engine_reference.py``. Every
formula here is observed behaviour of the native engine; the exact
numbers are pinned by ``tests/test_ink_engine_acceptance.py`` and must
not be retuned.

Data model (matches the native structs)::

    Point   = (x, y, w)   three doubles.  w is a RADIUS (half width).
    Segment = type in {MOVE, LINE, BEZIER} + up to four control Points.
    Path    = segments + fill rule + close/fill flags.

The core is :func:`span_corners`: one stroke span expands to the exact
trapezoid bounded by the two common external tangents of its two end
circles. For a unit edge normal ``n``::

    (B + w1*n - A - w0*n) . n = 0   ->   u.n = (w0 - w1) / L

so ``n = k*u +/- sqrt(1-k^2)*u_perp`` and both edge points sit at
perpendicular distance exactly ``w0`` / ``w1`` from the centreline. No
approximation anywhere.

Spans are filled with the NonZero rule (all spans wound the same way),
so overlaps merge into one silhouette and joins come out mitred with no
separate join step. Painting lives in the Qt layer; this module is
pure and Qt-free.

Two deliberate departures from the native library, both from the
research notes:

* ``(w0 - w1)^2 >= L^2`` is *clamped* here instead of dropping the span
  (:func:`span_corners`). The native engine drops it, which leaves a
  gap in the ink on fast pressure flicks. :func:`segment_quad` keeps the
  native rejection so the reference behaviour stays testable.
* The pressure -> width curve and the corner-splitting branch structure
  are modelling choices (they live in NativeAOT C# and were not
  recovered). Everything else is observed behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Sequence

__all__ = [
    "CREATE_TOLERANCE",
    "OUTLINE_TOLERANCE",
    "SIMPLIFY_TOLERANCE",
    "SMOOTH_TOLERANCE",
    "SMOOTH_MAX_ANGLE_DEG",
    "FIXED_POINT_SCALE",
    "MOVE",
    "LINE",
    "BEZIER",
    "EVEN_ODD",
    "NON_ZERO",
    "CUBICS_AND_LINES",
    "LINES_ONLY",
    "CIRCLE",
    "RECTANGLE",
    "Point",
    "Segment",
    "Path",
    "span_corners",
    "segment_quad",
    "segment_path",
    "stroke_polygons",
    "arc_cap",
    "cap_points",
    "cubic_at",
    "flatten_cubic",
    "mid",
    "simplify",
    "smooth_polyline",
    "path_to_svg_data",
    "polygons_to_svg",
]

# ---------------------------------------------------------------- constants
# Recovered defaults (IMPLEMENT.md §8)
CREATE_TOLERANCE = 0.01       # 0xB5F0  DbInkCreateGeometry
OUTLINE_TOLERANCE = 0.01      # 0xF5A0  Outline()
SIMPLIFY_TOLERANCE = 0.2      # 0xE010  Simplify()
SMOOTH_TOLERANCE = 0.01       # 0xEAD0  Smooth()
SMOOTH_MAX_ANGLE_DEG = 40.0   # 0xEAD0  corner threshold
FIXED_POINT_SCALE = 100_000.0  # 0x126B0 / 0x12AC0  Clipper scale (1e5)

MOVE, LINE, BEZIER = 0, 1, 2            # DbSegmentType
EVEN_ODD, NON_ZERO = 0, 1               # DbFillType
CUBICS_AND_LINES, LINES_ONLY, CIRCLE, RECTANGLE = 0, 1, 2, 3

#: Tangent slope is clamped just inside +/-1 when the width change
#: outgrows the span, so the quad stays finite instead of folding.
_K_LIMIT = 0.999


@dataclass
class Point:
    """One sample: ``w`` is a radius (half width), never a thickness."""

    x: float
    y: float
    w: float = 0.0

    def __add__(self, other):
        return Point(self.x + other[0], self.y + other[1], self.w)

    def __sub__(self, other):
        return (self.x - other[0], self.y - other[1])


@dataclass
class Segment:
    type: int
    pts: List[Point] = field(default_factory=list)


@dataclass
class Path:
    segments: List[Segment] = field(default_factory=list)
    fill_mode: int = EVEN_ODD
    closed: bool = True
    fill: bool = True

    def points(self) -> List[Point]:
        """Flatten to a point sequence (MOVE anchors + LINE endpoints)."""
        out: List[Point] = []
        for segment in self.segments:
            if segment.type == MOVE:
                out.append(segment.pts[0])
            elif segment.type == LINE:
                if not out:
                    out.append(segment.pts[0])
                out.append(segment.pts[1])
            elif segment.type == BEZIER:
                out.extend(flatten_cubic(*segment.pts[:4],
                                         tol=CREATE_TOLERANCE))
        return out


# =====================================================================
# THE VARIABLE-WIDTH SPAN  — 0x173C0
# =====================================================================
def span_corners(ax: float, ay: float, wa: float,
                 bx: float, by: float, wb: float):
    """Exact tangent trapezoid corners as ``(x, y)`` tuples.

    ``wa`` / ``wb`` are radii. Returns ``(p0, p1, p2, p3)`` in the
    native order::

        p0 = a + wa*n+     p1 = b + wb*n+
        p2 = a + wa*n-     p3 = b + wb*n-

    where ``n+/- = k*u +/- sqrt(1-k^2)*u_perp`` and
    ``k = (wa - wb) / L``. When ``|k|`` reaches 1 the two end circles
    no longer have a common external tangent (the native engine rejects
    the span and leaves a gap); the slope is clamped instead, so the
    quad degenerates to a closed cone and the ink stays continuous.

    ``None`` only for a zero-length span.
    """
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return None
    length = math.sqrt(length_sq)
    ux, uy = dx / length, dy / length
    k = (wa - wb) / length
    if k > _K_LIMIT:
        k = _K_LIMIT
    elif k < -_K_LIMIT:
        k = -_K_LIMIT
    m = math.sqrt(max(0.0, 1.0 - k * k))
    n1x, n1y = k * ux - m * uy, m * ux + k * uy
    n2x, n2y = k * ux + m * uy, -m * ux + k * uy
    return (
        (ax + n1x * wa, ay + n1y * wa),
        (bx + n1x * wb, by + n1y * wb),
        (ax + n2x * wa, ay + n2y * wa),
        (bx + n2x * wb, by + n2y * wb),
    )


def segment_quad(a: Point, b: Point):
    """Native span expansion (reference behaviour, including the guard).

    Returns the four corners in native order or ``None`` when the
    native code rejects the span::

        if (w_a - w_b)**2 >= (dx*dx + dy*dy): return nothing

    A width change larger than the span length makes the tangent
    construction degenerate (the narrow circle sits inside the wide
    one), so the native engine drops the span. The renderer uses
    :func:`span_corners` instead, which clamps and keeps the ink joined.
    """
    dx = b.x - a.x
    dy = b.y - a.y
    length_sq = dx * dx + dy * dy
    dw = a.w - b.w
    if dw * dw >= length_sq:                # 0x1749B  comisd / jb
        return None
    if length_sq <= 0.0:
        return None
    corners = span_corners(a.x, a.y, a.w, b.x, b.y, b.w)
    if corners is None:                     # pragma: no cover - guarded above
        return None
    return tuple(Point(x, y, 0.0) for x, y in corners)


def segment_path(a: Point, b: Point):
    """Assemble one span's four corners into a closed Path.

    ``MOVE p0, LINE p0->p2, LINE p2->p3, LINE p3->p1`` — walk
    ``a+n+ -> a+n- -> b+n- -> b+n+`` and close. Both ends are straight
    (butt) cuts; the long edges are the common external tangents.
    """
    quad = segment_quad(a, b)
    if quad is None:
        return None
    p0, p1, p2, p3 = quad
    return Path(
        segments=[
            Segment(MOVE, [p0]),
            Segment(LINE, [p0, p2]),
            Segment(LINE, [p2, p3]),
            Segment(LINE, [p3, p1]),
        ],
        closed=True,
    )


def stroke_polygons(points: Sequence[Point], round_caps: bool = False,
                    cap_steps: int = 24):
    """One trapezoid per span, optionally plus two half-disc caps.

    This is the native behaviour: spans are separate sub-paths of one
    geometry and the boolean union resolves them. Overlaps therefore
    merge into one silhouette and interior corners come out mitred.

    The native library has no arc generator (only ``cos``, used for the
    40 degree corner test), so caps are the caller's job.
    """
    polys: List[List[Point]] = []
    for a, b in zip(points, points[1:]):
        quad = segment_quad(a, b)
        if quad is None:
            continue
        p0, p1, p2, p3 = quad
        polys.append([p0, p2, p3, p1])

    if round_caps and len(points) >= 1:
        first, last = points[0], points[-1]
        if first.w > 0:
            if len(points) >= 2:
                ang = math.atan2(-(last.y - first.y), -(last.x - first.x))
            else:
                ang = 0.0
            polys.append(arc_cap(first, ang, first.w, cap_steps))
        if last.w > 0:
            if len(points) >= 2:
                ang = math.atan2(last.y - points[-2].y, last.x - points[-2].x)
            else:
                ang = math.pi
            polys.append(arc_cap(last, ang, last.w, cap_steps))
    return polys


def arc_cap(c: Point, start_angle: float, r: float,
            steps: int) -> List[Point]:
    """Half disc of radius ``r`` at ``c``, sweeping pi from start_angle."""
    pts = []
    for i in range(steps + 1):
        t = start_angle + math.pi * i / steps
        pts.append(Point(c.x + r * math.cos(t), c.y + r * math.sin(t)))
    return pts


def cap_points(cx: float, cy: float, r: float,
               ax: float, ay: float, bx: float, by: float,
               through: float, steps: int):
    """Interior points of the cap arc from ``a`` to ``b`` via ``through``.

    Both endpoints lie on the circle ``(cx, cy, r)``; the arc is the one
    that passes through the outward tangent direction ``through``. For
    a constant-width span that is exactly a semicircle; for a tapered
    span it is the shorter sector, which is why a tapered end cap meets
    both rail vertices without a tab.

    Endpoints are excluded — the caller supplies the exact rail
    vertices — and ``steps`` is interpreted per pi radians.
    """
    a0 = math.atan2(ay - cy, ax - cx)
    a1 = math.atan2(by - cy, bx - cx)
    ccw = (a1 - a0) % (2.0 * math.pi)
    to_through = (through - a0) % (2.0 * math.pi)
    sweep = ccw if to_through <= ccw else ccw - 2.0 * math.pi
    count = max(1, int(round(steps * abs(sweep) / math.pi)))
    return [(cx + r * math.cos(a0 + sweep * i / count),
             cy + r * math.sin(a0 + sweep * i / count))
            for i in range(1, count)]


# =====================================================================
# BEZIER  — 0x166A0 (flatten), 0x198A0 (evaluate)
# =====================================================================
def cubic_at(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3 * mt * mt * t
    c = 3 * mt * t * t
    d = t * t * t
    return Point(a * p0.x + b * p1.x + c * p2.x + d * p3.x,
                 a * p0.y + b * p1.y + c * p2.y + d * p3.y)


def _flat_enough(p0, p1, p2, p3, tol) -> bool:
    # Max distance of the control points from the chord (Schneider's test)
    dx, dy = p3.x - p0.x, p3.y - p0.y
    d1 = abs((p1.x - p3.x) * dy - (p1.y - p3.y) * dx)
    d2 = abs((p2.x - p3.x) * dy - (p2.y - p3.y) * dx)
    return (d1 + d2) ** 2 <= tol * (dx * dx + dy * dy)


def mid(a: Point, b: Point) -> Point:
    return Point((a.x + b.x) / 2, (a.y + b.y) / 2)


def flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point,
                  tol: float = CREATE_TOLERANCE, depth: int = 0
                  ) -> List[Point]:
    """Recursive subdivision at t = 0.5 (native 0x166A0, constant 0.5)."""
    if depth >= 16 or _flat_enough(p0, p1, p2, p3, tol):
        return [p3]
    l01 = mid(p0, p1)
    l12 = mid(p1, p2)
    l23 = mid(p2, p3)
    l012 = mid(l01, l12)
    l123 = mid(l12, l23)
    lm = mid(l012, l123)
    return (flatten_cubic(p0, l01, l012, lm, tol, depth + 1)
            + flatten_cubic(lm, l123, l23, p3, tol, depth + 1))


# =====================================================================
# SIMPLIFY  — 0xE010 (default 0.2) + 0x16EB0
# =====================================================================
def simplify(points: Sequence[Point],
             tol: float = SIMPLIFY_TOLERANCE) -> List[Point]:
    """Ramer-Douglas-Peucker after a 3-D de-duplication.

    Consecutive points are dropped only when ``x``, ``y`` *and* ``w``
    all match (native 0xE126..0xE158), so a width change alone keeps a
    vertex.
    """
    if tol == 0.0:
        tol = SIMPLIFY_TOLERANCE
    out: List[Point] = []
    for p in points:
        if out and p.x == out[-1].x and p.y == out[-1].y and p.w == out[-1].w:
            continue
        out.append(p)
    if len(out) < 3:
        return out
    keep = [False] * len(out)
    keep[0] = keep[-1] = True
    stack = [(0, len(out) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        best, idx = -1.0, -1
        ax, ay = out[i].x, out[i].y
        bx, by = out[j].x, out[j].y
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1e-12
        for k in range(i + 1, j):
            d = abs((out[k].x - ax) * dy - (out[k].y - ay) * dx) / length
            if d > best:
                best, idx = d, k
        if best > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(out, keep) if k]


# =====================================================================
# SMOOTH  — 0xEAD0
# =====================================================================
def smooth_polyline(points: Sequence[Point],
                    tol: float = SMOOTH_TOLERANCE,
                    max_angle_deg: float = SMOOTH_MAX_ANGLE_DEG
                    ) -> List[Segment]:
    """Corner-preserving smoothing (DbGeometrySmooth, impl 0xEAD0).

    A vertex whose incoming/outgoing tangent dot product is below
    ``cos(max_angle_deg)`` turns sharper than the threshold, so the
    curve is split there and the corner is preserved. Everything else
    is emitted as cubics with the native chord tolerance.

    The native fitter (0x19140 -> 0x198A0) is Schneider's error-bounded
    scheme; the Catmull-Rom tangents below are the faithful stand-in
    the reference implementation uses.
    """
    if not points:
        return []
    if tol == 0.0:
        tol = SMOOTH_TOLERANCE
    if max_angle_deg == 0.0:
        max_angle_deg = SMOOTH_MAX_ANGLE_DEG
    corner_cos = math.cos(max_angle_deg * math.pi / 180.0)

    runs: List[List[Point]] = [[points[0]]]
    for i in range(1, len(points) - 1):
        p, c, n = points[i - 1], points[i], points[i + 1]
        d1 = (c.x - p.x, c.y - p.y)
        d2 = (n.x - c.x, n.y - c.y)
        l1 = math.hypot(*d1) or 1e-12
        l2 = math.hypot(*d2) or 1e-12
        dot = (d1[0] * d2[0] + d1[1] * d2[1]) / (l1 * l2)
        runs[-1].append(c)
        if dot < corner_cos:          # sharper than 40 deg -> hard corner
            runs.append([c])
    runs[-1].append(points[-1])

    segments: List[Segment] = []
    for run in runs:
        if len(run) < 2:
            continue
        if len(run) == 2:
            segments.append(Segment(MOVE, [run[0]]))
            segments.append(Segment(LINE, [run[0], run[1]]))
            continue
        segments.append(Segment(MOVE, [run[0]]))
        for i in range(len(run) - 1):
            p0, p1 = run[i], run[i + 1]
            pm = run[i - 1] if i > 0 else p0
            pn = run[i + 2] if i + 2 < len(run) else p1
            t = 1.0 / 6.0
            c1 = Point(p0.x + (p1.x - pm.x) * t,
                       p0.y + (p1.y - pm.y) * t)
            c2 = Point(p1.x - (pn.x - p0.x) * t,
                       p1.y - (pn.y - p0.y) * t)
            segments.append(Segment(BEZIER, [p0, c1, c2, p1]))
    return segments


# =====================================================================
# SVG OUTPUT  — 0x144A0
# =====================================================================
def dp(value: float) -> str:
    """``std::ostream << double`` uses 6 significant digits by default."""
    return f"{value:.6g}"


def path_to_svg_data(segments: Sequence[Segment]) -> str:
    """Emit the native serializer's tokens: ``M/L/C`` then ``Z ``."""
    out: List[str] = []
    if segments and segments[0].type != MOVE:
        first = segments[0].pts[0]
        out.append("M %s, %s " % (dp(first.x), dp(first.y)))
    for segment in segments:
        if segment.type == MOVE:
            out.append("M %s, %s " % (dp(segment.pts[0].x),
                                      dp(segment.pts[0].y)))
        elif segment.type == LINE:
            out.append("L %s, %s " % (dp(segment.pts[1].x),
                                      dp(segment.pts[1].y)))
        elif segment.type == BEZIER:
            out.append("C %s, %s , %s, %s , %s, %s " % (
                dp(segment.pts[1].x), dp(segment.pts[1].y),
                dp(segment.pts[2].x), dp(segment.pts[2].y),
                dp(segment.pts[3].x), dp(segment.pts[3].y)))
    out.append("Z ")
    return "".join(out)


def polygons_to_svg(polys: Sequence[Sequence[Point]], w=800, h=400,
                    fill="#111", background="#fff") -> str:
    """One ``<path>`` with every polygon as a sub-path, NonZero fill.

    That is exactly equivalent to the union the native ``Outline()``
    performs, so the render matches without needing a clipper.
    """
    data = []
    for poly in polys:
        if len(poly) < 2:
            continue
        data.append("M %s, %s " % (dp(poly[0].x), dp(poly[0].y)))
        for p in poly[1:]:
            data.append("L %s, %s " % (dp(p.x), dp(p.y)))
        data.append("Z ")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
            f'height="{h}" viewBox="0 0 {w} {h}">\n'
            f'<rect width="100%" height="100%" fill="{background}"/>\n'
            f'<path d="{"".join(data)}" fill="{fill}" '
            f'fill-rule="nonzero"/>\n</svg>\n')
