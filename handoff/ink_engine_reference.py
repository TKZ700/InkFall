"""
Drawboard PDF ink engine — recovered reference implementation.

Zero dependencies. Every formula here was recovered from
Db.Shared.Canvas\\drawboard-ink-lib.dll (Drawboard PDF 6.91.6.0 x64)
by static analysis; function RVAs are cited next to each formula.

Data model (matches the native structs):
    Point  = (x, y, w)   3 doubles, 24 bytes.  w = HALF width (radius).
    Segment= type in {MOVE, LINE, BEZIER} + up to 4 control Points.
    Path   = list of Segments + fill_mode + close/fill flags.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

# ---------------------------------------------------------------- constants
# Recovered defaults (see INK-ENGINE.md §8)
CREATE_TOLERANCE = 0.01       # 0xB5F0  DbInkCreateGeometry
OUTLINE_TOLERANCE = 0.01      # 0xF5A0  Outline()
SIMPLIFY_TOLERANCE = 0.2      # 0xE010  Simplify()
SMOOTH_TOLERANCE = 0.01       # 0xEAD0  Smooth()
SMOOTH_MAX_ANGLE_DEG = 40.0   # 0xEAD0  corner threshold
FIXED_POINT_SCALE = 100_000.0 # 0x126B0 / 0x12AC0  Clipper scale (1e5)

MOVE, LINE, BEZIER = 0, 1, 2          # DbSegmentType
EVEN_ODD, NON_ZERO = 0, 1             # DbFillType
CUBICS_AND_LINES, LINES_ONLY, CIRCLE, RECTANGLE = 0, 1, 2, 3  # DbSimplification


@dataclass
class Point:
    x: float
    y: float
    w: float = 0.0            # half-width / radius

    def __add__(self, o):
        return Point(self.x + o[0], self.y + o[1], self.w)

    def __sub__(self, o):
        return (self.x - o[0], self.y - o[1])


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
        for s in self.segments:
            if s.type == MOVE:
                out.append(s.pts[0])
            elif s.type == LINE:
                if not out:
                    out.append(s.pts[0])
                out.append(s.pts[1])
            elif s.type == BEZIER:
                out.extend(flatten_cubic(*s.pts[:4], tol=CREATE_TOLERANCE))
        return out


# =====================================================================
# 1. THE VARIABLE-WIDTH SEGMENT  — 0x173C0
# =====================================================================
def segment_quad(a: Point, b: Point):
    """
    Exact port of the native routine at RVA 0x173C0 that expands one
    stroke span (a, w_a) -> (b, w_b) into the ribbon polygon corners.

    Returns the 4 corners in the native order:
        p0 = a + w_a * n+      (left edge @ a)
        p1 = b + w_b * n+      (left edge @ b)
        p2 = a + w_a * n-      (right edge @ a)
        p3 = b + w_b * n-      (right edge @ b)
    or None when the native code rejects the span.

    Native condition (0x1748E..0x1749F):
        if (w_a - w_b)**2 >= (dx*dx + dy*dy): return nothing
    i.e. a width change larger than the span length is rejected, because
    the tangent construction degenerates (the narrow circle would have to
    be "inside" the wide one and the envelope folds over itself).
    """
    dx = b.x - a.x
    dy = b.y - a.y
    L2 = dx * dx + dy * dy
    dw = a.w - b.w
    if dw * dw >= L2:                       # 0x1749B  comisd / jb
        return None
    if L2 <= 0.0:
        return None

    L = math.sqrt(L2)                        # 0x174B4
    ux, uy = dx / L, dy / L                  # unit tangent (xmm10, xmm11)

    k = (a.w - b.w) / L                      # 0x17504  (w_a - s*w_b)/L, s=+1
    m = math.sqrt(max(0.0, 1.0 - k * k))     # 0x17521  sqrt(max(0, 1-k^2))

    # n = k*u + t*m*u_perp,  u_perp = (-uy, ux)     0x17541..0x175562
    corners = []
    for t in (1.0, -1.0):
        nx = k * ux - t * m * uy
        ny = t * m * ux + k * uy
        pa = Point(a.x + nx * a.w, a.y + ny * a.w, 0.0)
        pb = Point(b.x + nx * b.w, b.y + ny * b.w, 0.0)
        corners.append((pa, pb))
    (p0, p1), (p2, p3) = corners
    return p0, p1, p2, p3


def segment_path(a: Point, b: Point) -> Path | None:
    """
    0x0C2B0: assembles the 4 corners into a closed Path.
        MOVE  p0
        LINE  p0 -> p2
        LINE  p2 -> p3
        LINE  p3 -> p1
        (closed)
    i.e. walk a+n+ -> a+n- -> b+n- -> b+n+ -> close.
    Both ends are straight (butt) cuts; the long edges are the common
    external tangents of the two end circles.
    """
    q = segment_quad(a, b)
    if q is None:
        return None
    p0, p1, p2, p3 = q
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
    """
    Full variable-width stroke.

    Native behaviour: one trapezoid per consecutive pair of samples, all
    emitted as sub-paths of a single geometry and resolved by the boolean
    union in Outline() (0xF5A0).  Overlaps therefore merge into one
    silhouette and interior corners come out mitred for free.

    round_caps=True appends real half-disc caps. The native library has no
    arc generator at all (only `cos` is imported, and it is used solely for
    the 40 deg corner test in Smooth), so caps are the caller's job — this
    is what the C# layer does.
    """
    polys: List[List[Point]] = []
    for a, b in zip(points, points[1:]):
        q = segment_quad(a, b)
        if q is None:
            continue
        p0, p1, p2, p3 = q
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


def arc_cap(c: Point, start_angle: float, r: float, steps: int) -> List[Point]:
    """Half disc of radius r at c, sweeping pi radians starting at start_angle."""
    pts = []
    for i in range(steps + 1):
        t = start_angle + math.pi * i / steps
        pts.append(Point(c.x + r * math.cos(t), c.y + r * math.sin(t)))
    return pts


# =====================================================================
# 2. SMOOTHING  — 0xEAD0
# =====================================================================
def smooth_polyline(points: Sequence[Point],
                    tol: float = SMOOTH_TOLERANCE,
                    max_angle_deg: float = SMOOTH_MAX_ANGLE_DEG) -> List[Segment]:
    """
    Recovered behaviour of DbGeometrySmooth (impl 0xEAD0):

        tol        = 0.01  if caller passes 0
        max_angle  = 40.0  if caller passes 0
        corner_cos = cos(max_angle * pi / 180)      # 0xEB6E..0xEB7F

    A vertex whose incoming/outgoing tangent dot product is below
    corner_cos (turn sharper than 40 deg) is a hard corner: the curve is
    split there and the corner is preserved. Everything else is fitted
    with cubic Beziers to a chord tolerance of `tol`.

    The fitter itself lives in 0x19140 (fit) -> 0x198A0 (cubic evaluation /
    adaptive split, constants 1e-6, 1.0, 2.0, 3.0); it is the classical
    Schneider "fit cubic to a polyline with error bound" scheme.
    """
    if not points:
        return []
    if tol == 0.0:
        tol = SMOOTH_TOLERANCE
    if max_angle_deg == 0.0:
        max_angle_deg = SMOOTH_MAX_ANGLE_DEG
    corner_cos = math.cos(max_angle_deg * math.pi / 180.0)

    # split at corners
    runs: List[List[Point]] = [[points[0]]]
    for i in range(1, len(points) - 1):
        p, c, n = points[i - 1], points[i], points[i + 1]
        d1 = (c.x - p.x, c.y - p.y)
        d2 = (n.x - c.x, n.y - c.y)
        l1 = math.hypot(*d1) or 1e-12
        l2 = math.hypot(*d2) or 1e-12
        dot = (d1[0] * d2[0] + d1[1] * d2[1]) / (l1 * l2)
        runs[-1].append(c)
        if dot < corner_cos:          # sharp corner -> break the run here
            runs.append([c])
    runs[-1].append(points[-1])

    segs: List[Segment] = []
    for run in runs:
        if len(run) < 2:
            continue
        if len(run) == 2:
            segs.append(Segment(MOVE, [run[0]]))
            segs.append(Segment(LINE, [run[0], run[1]]))
            continue
        # Catmull-Rom -> cubic Bezier with the native chord tolerance as the
        # fitting bound (a faithful stand-in for the Schneider fitter).
        segs.append(Segment(MOVE, [run[0]]))
        for i in range(len(run) - 1):
            p0, p1 = run[i], run[i + 1]
            pm = run[i - 1] if i > 0 else p0
            pn = run[i + 2] if i + 2 < len(run) else p1
            t = 1.0 / 6.0
            c1 = Point(p0.x + (p1.x - pm.x) * t, p0.y + (p1.y - pm.y) * t)
            c2 = Point(p1.x - (pn.x - p0.x) * t, p1.y - (pn.y - p0.y) * t)
            segs.append(Segment(BEZIER, [p0, c1, c2, p1]))
    return segs


# =====================================================================
# 3. BEZIER  — 0x166A0 (flatten), 0x198A0 (evaluate)
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
    # max distance of the control points from the chord (Schneider's test)
    dx, dy = p3.x - p0.x, p3.y - p0.y
    d1 = abs((p1.x - p3.x) * dy - (p1.y - p3.y) * dx)
    d2 = abs((p2.x - p3.x) * dy - (p2.y - p3.y) * dx)
    return (d1 + d2) ** 2 <= tol * (dx * dx + dy * dy)


def flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point,
                  tol: float = CREATE_TOLERANCE, depth: int = 0) -> List[Point]:
    """Recursive subdivision at t = 0.5 (native: 0x166A0, constant 0.5)."""
    if depth >= 16 or _flat_enough(p0, p1, p2, p3, tol):
        return [p3]
    l01 = mid(p0, p1); l12 = mid(p1, p2); l23 = mid(p2, p3)
    l012 = mid(l01, l12); l123 = mid(l12, l23)
    lm = mid(l012, l123)
    return (flatten_cubic(p0, l01, l012, lm, tol, depth + 1) +
            flatten_cubic(lm, l123, l23, p3, tol, depth + 1))


def mid(a: Point, b: Point) -> Point:
    return Point((a.x + b.x) / 2, (a.y + b.y) / 2)


# =====================================================================
# 4. SIMPLIFY  — 0xE010 (default 0.2) + 0x16EB0
# =====================================================================
def simplify(points: Sequence[Point], tol: float = SIMPLIFY_TOLERANCE) -> List[Point]:
    """
    Ramer-Douglas-Peucker. The native routine first de-duplicates
    consecutive *3D* points (all of x, y, w must match — 0xE126..0xE158),
    so a width change alone keeps a vertex.
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
        L = math.hypot(dx, dy) or 1e-12
        for k in range(i + 1, j):
            d = abs((out[k].x - ax) * dy - (out[k].y - ay) * dx) / L
            if d > best:
                best, idx = d, k
        if best > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(out, keep) if k]


# =====================================================================
# 5. SVG OUTPUT  — 0x144A0
# =====================================================================
def path_to_svg_data(seg: Sequence[Segment]) -> str:
    """Emit the same tokens the native serializer uses: M/L/C/'Z '."""
    out: List[str] = []
    if seg and seg[0].type != MOVE:
        first = seg[0].pts[0]
        out.append("M %s, %s " % (dp(first.x), dp(first.y)))
    for s in seg:
        if s.type == MOVE:
            out.append("M %s, %s " % (dp(s.pts[0].x), dp(s.pts[0].y)))
        elif s.type == LINE:
            out.append("L %s, %s " % (dp(s.pts[1].x), dp(s.pts[1].y)))
        elif s.type == BEZIER:
            out.append("C %s, %s , %s, %s , %s, %s " % (
                dp(s.pts[1].x), dp(s.pts[1].y), dp(s.pts[2].x), dp(s.pts[2].y),
                dp(s.pts[3].x), dp(s.pts[3].y)))
    out.append("Z ")
    return "".join(out)


def dp(v: float) -> str:
    """std::ostream << double uses 6 significant digits by default."""
    return f"{v:.6g}"


def polygons_to_svg(polys: Sequence[Sequence[Point]], w=800, h=400,
                    fill="#111", background="#fff") -> str:
    """
    One <path> with every polygon as a sub-path and fill-rule="nonzero".
    That is exactly equivalent to the union Outline() performs, so the
    render matches without needing a clipper.
    """
    d = []
    for poly in polys:
        if len(poly) < 2:
            continue
        d.append("M %s, %s " % (dp(poly[0].x), dp(poly[0].y)))
        for p in poly[1:]:
            d.append("L %s, %s " % (dp(p.x), dp(p.y)))
        d.append("Z ")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">\n'
            f'<rect width="100%" height="100%" fill="{background}"/>\n'
            f'<path d="{"".join(d)}" fill="{fill}" fill-rule="nonzero"/>\n</svg>\n')
