"""M7.1 shape recognition, contour fitting and morph geometry.

Qt-free and standard-library-only, like the rest of ``annotations/*``.
All geometry is page-local rotated PDF points (y down) — the coordinate
system the interaction handlers, the annotation store and the paint path
already share — so recognized geometry is zoom- and scroll-independent.

Three responsibilities, deliberately kept apart:

* :func:`recognize_shape` — a staged classification of a freehand
  centerline into a supported contour. After arc-length normalization,
  the endpoint gap relative to the bounding-box diagonal decides between
  the open branch (a straight line, judged by endpoint-chord deviation)
  and the closed branch. A wide gap in the evidence band
  (:data:`CLOSE_GAP_MAX`) is still completed when the pen stopped
  part-way along an edge, so the closing chord continues the last drawn
  direction (M7.1.4.1; V/L/C/hook strokes are refused). Closed strokes then
  present shape-specific evidence in a fixed order — circle (radial
  consistency, box aspect, area), rectangle (oriented-box coverage and
  edge proximity), triangle (adaptive corner interpretation), then a
  generic polygon fallback (ordered vertex ring with discrete corner
  evidence) — with a shared normalized fit guard acting only as the
  false-positive limiter. A candidate is never required to pass every gate
  of every other shape, and a materially ambiguous reading returns
  ``None`` so the freehand stroke is kept. The polygon fallback is the
  *last* stage on purpose: it can never pre-empt the specialized circle,
  rectangle or triangle readings.
* the ``_fit_*`` helpers — turn a classified centerline into clean
  page-local contour geometry (``ShapeCandidate.contour``).
* :func:`prepare_morph` / :func:`morph_points` — normalized geometry
  interpolation between the freehand centerline and the fitted contour,
  used by the viewer's conversion animation.

Nothing here builds annotations or touches Qt: the viewer owns committing
(``Shape`` for the pen, ``Highlighter`` for the highlighter) through the
existing store/history path, so there is no second annotation model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from core.types import Point

__all__ = [
    "HOLD_MS",
    "MORPH_MS",
    "MIN_SNAP_PT",
    "MIN_STROKE_PT",
    "MIN_SEGMENT_PT",
    "MIN_SAMPLES",
    "MAX_WORK_POINTS",
    "MORPH_SAMPLES",
    "CLOSE_RATIO",
    "CLOSE_GAP_MAX",
    "CLOSE_TANGENT_DEG",
    "CLOSE_EDGE_MEAN_RATIO",
    "CHAIN_MERGE_RATIO",
    "CHAIN_MAX_SIDES",
    "CHAIN_MIN_SIDES",
    "CHAIN_MAX_TURN_DEG",
    "ShapeCandidate",
    "recognize_shape",
    "prepare_morph",
    "morph_translation",
    "morph_points",
]

#: Stationary hold (ms) at the end of an active stroke before the viewer
#: evaluates recognition. Centralized here so it can be tuned in one
#: place; the viewer owns the single-shot timer that measures it.
HOLD_MS = 320

#: Geometry-morph duration (ms) for the conversion animation. The viewer
#: clamps it through ``app.fx`` (reduced motion resolves instantly).
MORPH_MS = 160

#: Minimum pointer-endpoint separation (pt) for an "Ink to Line" snap; a
#: shorter stroke is a degenerate tap and keeps its freehand geometry.
MIN_SNAP_PT = 1.0

#: Minimum arc length (pt) of an *open* (line) candidate. Closed shapes
#: use :data:`MIN_CLOSED_PERIMETER_PT` instead, because their perimeter is
#: shared between all sides.
MIN_STROKE_PT = 20.0

#: Minimum usable dimension (pt) of a recognized shape (shortest edge /
#: shortest fitted axis / shortest line). Small enough for a 2 mm glyph
#: drawn with a pen; still far above a jitter scribble.
MIN_SEGMENT_PT = 6.0

#: Minimum captured samples before recognition is attempted. A hold on a
#: dot or a two-sample flick can never convert.
MIN_SAMPLES = 6

#: Working sample count: strokes are resampled to this many equal
#: arc-length samples before classification (bounded cost, uniform
#: spacing independent of input sampling).
MAX_WORK_POINTS = 128

#: Equal-arc-length sample count per morph side. Both sides are resampled
#: to exactly this count so interpolation is per-index and stable.
MORPH_SAMPLES = 96

#: Closed-shape minimum perimeter: an absolute floor plus a multiple of
#: the tool thickness (a thick band cannot describe a tiny loop).
MIN_CLOSED_PERIMETER_PT = 40.0
MIN_CLOSED_PERIMETER_THICKNESS = 8.0

#: Line minimum length as a multiple of the tool thickness (on top of
#: :data:`MIN_STROKE_PT`).
MIN_LINE_LENGTH_THICKNESS = 4.0

#: Closed shapes must be drawn *once* around: the drawn arc length may not
#: exceed the hull perimeter by more than this factor. A genuine
#: rectangle/circle/triangle (even with a closure gap, rounded corners or
#: an overshoot) stays near 1.0; a figure-eight, a double-drawn loop or a
#: coiled scribble reaches ~2 and is kept freehand.
SIMPLE_STROKE_MAX_RATIO = 1.25

#: Closure test, M7.1.3 convention: the distance between the stroke's
#: first and last sample divided by the **diagonal of the stroke's own
#: bounding box**. This follows the reference classifier (Veditor:
#: ``closure = endDist / boxDiagonal``); it is *not* the old gap/drawn-arc
#: ratio, and the two are not interchangeable. Using the stroke's overall
#: size as the denominator makes the decision independent of how much arc
#: was drawn and of sampling density: a ring that stops about 42 degrees
#: short (endpoints ~0.36 diagonals apart) is still "closed", while a C,
#: a hook or a half-drawn loop (>= ~0.5) is not. The gap is
#: classification input only: closed candidates are fitted from drawn
#: samples, never from an invented closing chord.
CLOSE_RATIO = 0.36

#: Evidence-based closure band (M7.1.4.1). A stroke whose endpoint gap sits
#: between :data:`CLOSE_RATIO` and this ceiling is still completed into a
#: closed candidate, but only when the *shape* of the gap proves the intent:
#: the pen must have stopped part-way along an edge, so the final drawn
#: direction continues into the gap chord (within
#: :data:`CLOSE_TANGENT_DEG`) and the implied closing edge must be a
#: plausible boundary edge of the fitted candidate (no longer than
#: :data:`CLOSE_EDGE_MEAN_RATIO` times that candidate's mean edge length).
#:
#: This is deliberately *not* a raised :data:`CLOSE_RATIO`. A V, an L, a
#: hook, an S-curve or a C has endpoints whose tangents meet the gap chord
#: at a corner or a reversal, so it never qualifies; neither does a stroke
#: that abandons an entire side (the chord would be perpendicular to the
#: last drawn edge). Round outlines keep the established
#: :data:`CLOSE_RATIO` convention unchanged — only straight-sided
#: candidates are eligible in this band.
CLOSE_GAP_MAX = 0.65
CLOSE_TANGENT_DEG = 55.0
CLOSE_EDGE_MEAN_RATIO = 1.5

#: Multi-stroke chain merge (M7.1.4.1). Recognition normally sees one raw
#: pen stroke. When a dwell produces no closed single-stroke candidate, the
#: viewer may offer the recent raw ``InkStroke`` chain (sides captured as
#: separate strokes) and re-run this module on the merged ring. Seams must
#: be genuinely connected: the two stroke endpoints may sit at most this
#: fraction of the merged diagonal apart, and the merged ring may not carry
#: more than :data:`CHAIN_MAX_SIDES` raw strokes (a plausible hand-drawn
#: polygon, never an arbitrary pile of ink). The merged ring must still pass
#: the full closed-shape evidence below, so proximity alone never accepts a
#: shape.
CHAIN_MERGE_RATIO = 0.08
CHAIN_MAX_SIDES = 8
CHAIN_MIN_SIDES = 3
CHAIN_MAX_TURN_DEG = 150.0

#: Shared false-positive guard for closed candidates (M7.1.3). This is
#: deliberately *one* normalized check applied after each shape's own
#: evidence has already accepted it, not a global classifier: the robust
#: (worst-decile) distance from the drawn samples to the candidate
#: contour must stay within a fraction of the bounding-box diagonal, and
#: no single sample may sit absurdly far away. The absolute and
#: thickness-derived floors keep small or thick strokes from being judged
#: by a fraction of a tiny diagonal.
FIT_ERROR_RATIO = 0.06
FIT_ERROR_MIN_PT = 1.5
FIT_ERROR_THICKNESS_FRAC = 0.5
FIT_WORST_RATIO = 0.25
FIT_WORST_MIN_PT = 8.0

#: Corner analysis. A corner needs this much *multiscale* turning; the
#: non-maximum suppression radius is a fraction of the ring; a "strong"
#: corner turns this much and is used only as structural evidence against
#: reading a many-cornered polygon as a circle.
CORNER_ANGLE_DEG = 38.0
CORNER_SUPPRESS_FRAC = 0.07
STRONG_CORNER_DEG = 60.0

#: Adaptive corner simplification (M7.1.3, after the reference
#: classifier): Douglas-Peucker is widened through this sequence of
#: tolerances — each a fraction of the bounding-box diagonal, with a small
#: absolute floor for tiny glyphs — and the smallest vertex count reached
#: (never below three) is kept. Corners closer than the merge/closing
#: fractions are averaged/dropped, because resampling often straddles one
#: true corner with two nearby samples. The fit error of the simplified
#: polygon against the drawn samples is the guard that stops a scribble
#: from collapsing into a false triangle.
ADAPTIVE_TOLERANCES = (0.03, 0.045, 0.06, 0.08, 0.10)
ADAPTIVE_MIN_PT = 0.75
CORNER_CLOSE_RATIO = 0.08
CORNER_MERGE_RATIO = 0.06
ADAPTIVE_FLAT_RATIO = 0.01
ADAPTIVE_MAX_FIT_ERROR = 0.20

#: Convex-hull simplification budget (fraction of the equivalent radius,
#: with an absolute floor): jitter vertices flatter than this are dropped
#: before the triangle search, so noisy input cannot inflate the hull
#: beyond the search limit.
HULL_SIMPLIFY_RATIO = 0.015
HULL_SIMPLIFY_MIN_PT = 1.0
TRI_MAX_VERTICES = 24

#: Generic polygon fallback (M7.1.4). A closed outline that is not
#: convincingly a circle, rectangle or triangle is committed as one
#: ordered vertex ring, independent of its everyday name (trapezoid,
#: parallelogram, pentagon, hexagon, ...); there is no per-name kind and
#: no per-name fitter.
#:
#: Vertex bounds: 4..8. The specialized stages already own 3 (triangle)
#: and the rectangle stage owns four-corner box shapes, so the fallback
#: starts at four and covers the common hand-drawn quads to octagons.
#: Nine or more fitted vertices is scribble/loop territory (turns get
#: small, vertex counting becomes unstable under jitter) and the stroke
#: keeps its freehand geometry. The search itself is bounded regardless:
#: one RDP pass per existing adaptive tolerance on the fixed-size
#: recognition ring, then this vertex filter (the triangle stage's own
#: :data:`TRI_MAX_VERTICES` hull budget is untouched). The bounds are
#: centralized here.
POLY_MIN_VERTICES = 4
POLY_MAX_VERTICES = 8

#: Polygon fit gate, normalized by the stroke's bounding-box diagonal
#: (same scale contract as the shared fit guard, but stricter: the
#: fallback must follow the drawn contour closely because it may snap
#: *any* simple outline).
POLY_MAX_FIT_ERROR = 0.10

#: A polygon must enclose a meaningful share of its own hull. A closed
#: scribble, a folded loop or a star-shaped re-entrant path that wanders
#: back across itself enclose little area relative to the hull and are
#: kept freehand. The self-intersection test below is the primary guard;
#: this is the area sanity check behind it.
POLY_MIN_AREA_SHARE = 0.50

#: Discrete corner evidence (M7.1.4 clarification). A smooth circle or
#: oval can be approximated by a 4-8 sided polygon inside a permissive
#: fit tolerance, so fit error and area alone must not decide. A polygon
#: must show *concentrated* turning at its fitted vertices: every fitted
#: vertex needs a local drawn turn of at least
#: :data:`POLY_VERTEX_TURN_DEG`, and the windows around the fitted
#: vertices must jointly capture at least
#: :data:`POLY_TURN_CONCENTRATION` of the stroke's total turning. On a
#: circle/oval the turning is spread uniformly (or over two smooth ends),
#: so narrow vertex windows capture far less; a rounded polygon still
#: concentrates each vertex's turn in a short arc and passes without
#: needing razor-sharp corners. The window is a fraction of the sample
#: ring on each side of the nearest sample to a fitted vertex.
POLY_VERTEX_TURN_DEG = 30.0
POLY_TURN_CONCENTRATION = 0.70
POLY_TURN_WINDOW_FRAC = 0.03

#: Rectangle evidence (M7.1.3). Recognition is rotation-aware: the box is
#: the stroke hull's minimum-area rectangle, and both measures are taken
#: against *that* box. "Coverage" is the stroke's enclosed area divided
#: by the box area (a rectangle ~1.0, a circle pi/4 ~0.785, a triangle
#: ~0.5), and the edge fraction is the share of samples within a band of
#: 9 % of the diagonal from the box boundary. The soft pair is the
#: reference classifier's second chance for shaky rectangles that
#: over-split into 4-6 corners; a hand-drawn circle's coverage tops out
#: near pi/4 = 0.785 (plus a little wobble), so the relaxed coverage stays
#: above it and a round outline cannot take this path.
RECT_EDGE_BAND_RATIO = 0.09
RECT_MIN_EDGE_FRACTION = 0.62
RECT_MIN_COVERAGE = 0.84
RECT_SOFT_EDGE_FRACTION = 0.45
RECT_SOFT_COVERAGE = 0.83

#: Triangle evidence. The best triangle inscribed in the hull must cover
#: this share of the hull (a triangle ~1.0, a parallelogram ~0.5, a
#: circle ~0.41). The threshold sits above a symmetric trapezoid's share
#: (base/(base+top), ~0.67 when the top is half the base) so an ordinary
#: quadrilateral is not squeezed into a triangle; when adaptive
#: simplification already yields exactly three vertices the threshold is
#: relaxed, since the corners then come from the stroke itself. Every
#: edge must beat :data:`MIN_SEGMENT_PT`.
TRI_MIN_HULL_AREA_FRAC = 0.68
TRI_ADAPTIVE_MIN_HULL_AREA_FRAC = 0.55

#: Circle evidence (M7.1.3.1). Hold-to-shape recognizes *circles* only: a
#: committed circle has one radius (equal horizontal/vertical half-axes)
#: and zero rotation, so a stretched oval can never be emitted. The gates
#: tolerate hand-drawn imperfection while refusing ovals and polygons:
#: (1) radial consistency — the spread of sample radii about the robust
#: fit (std / mean) — rejects elongated and cornered outlines, because an
#: oval's radii vary along its axes while a circle's do not; (2) the
#: bounding-box aspect is the explicit elongation guard, so a contour much
#: longer than wide stays freehand even when its mean radius could be
#: fitted; (3) the enclosed area must sit near ``pi * r^2`` under the
#: robust radius, rejecting a stroke that only traverses part of a round
#: outline; and (4) the number of strong corners stays that of a sloppy
#: seam, not a polygon. The shared normalized fit guard then confirms the
#: radius and centre. The trim fraction bounds how much of the sample set
#: the robust radius ignores at each end.
CIRCLE_MAX_RADIAL_CV = 0.10
CIRCLE_MIN_BBOX_ASPECT = 0.82
CIRCLE_MIN_AREA_RATIO = 0.75
CIRCLE_MAX_AREA_RATIO = 1.25
CIRCLE_MAX_STRONG_CORNERS = 4
CIRCLE_TRIM_FRAC = 0.10
CIRCLE_STEPS = 48

#: Line candidate. ``LINE_MAX_BOW_RATIO`` bounds the sagitta relative to
#: the chord (accepts a modest hand wobble, rejects a visible arc),
#: ``LINE_MIN_CHORD_RATIO`` is chord / arc length and acts only as a
#: *surplus-arc* guard (a stroke drawn twice, or a loop, adds arc without
#: shortening the chord). It is deliberately loose: ordinary hand tremor
#: adds arc length while barely moving the chord, and a tight ratio
#: rejected normal lines without helping arc rejection — a gentle visible
#: arc already scores ~0.985 here and is caught by the bow and bend gates.
#: ``LINE_MAX_BEND_DEG`` is the angle between the end tangents (an arc
#: bends them, a bow bends them a little, an S-curve not at all), and the
#: double-sided gate rejects an S-curve by requiring that the smoothed
#: residual profile not have significant lobes on *both* sides of the
#: chord. The noise floors scale with tool thickness.
LINE_MAX_BOW_RATIO = 0.075
LINE_MIN_CHORD_RATIO = 0.90
LINE_MAX_BEND_DEG = 60.0
LINE_NOISE_PT = 1.5
LINE_THICKNESS_FRAC = 0.5
LINE_DOUBLE_SIDED_MIN_PT = 2.5
LINE_DOUBLE_SIDED_RATIO = 0.045


@dataclass(frozen=True, slots=True)
class ShapeCandidate:
    """One classified shape in page-local points.

    ``kind`` is ``"line"``, ``"rectangle"``, ``"circle"``,
    ``"triangle"`` or ``"polygon"``. ``contour`` is the fitted ordered
    vertex list *without* a repeated closing vertex; for a polygon it
    holds only the fitted corners (never the resampled recognition
    samples). ``closed`` says whether the contour forms a ring (the
    painter/highlighter sweep must then close it). ``meta`` carries the
    box parameters for box-shaped kinds — ``(cx, cy, half_a, half_b,
    rotation_deg)``; a circle commits equal half-axes and zero rotation —
    and is empty otherwise. ``confidence`` is a 0..1 quality score (used
    for tie-breaking and diagnostics, never for user-facing gating).
    """

    kind: str
    contour: tuple
    closed: bool = False
    confidence: float = 0.0
    meta: tuple = ()


# --------------------------------------------------------------- parsing


def _floats(points: Sequence) -> list:
    """Coerce a centerline to finite ``(x, y)`` float pairs.

    Malformed/non-finite samples are skipped rather than raising: a
    hostile sample must never convert or crash an in-progress gesture.
    """
    out: list = []
    for value in points or ():
        try:
            if isinstance(value, Point):
                x, y = float(value.x), float(value.y)
            else:
                x, y = float(value[0]), float(value[1])
        except Exception:
            continue
        if math.isfinite(x) and math.isfinite(y):
            out.append((x, y))
    return out


def _cumulative(points: Sequence) -> list:
    """Cumulative arc length at every vertex (``[0.0, ...]``)."""
    out = [0.0]
    for i in range(1, len(points)):
        px, py = points[i - 1]
        qx, qy = points[i]
        out.append(out[-1] + math.hypot(qx - px, qy - py))
    return out


def _perimeter(points: Sequence, closed: bool) -> float:
    if len(points) < 2:
        return 0.0
    total = _cumulative(points)[-1]
    if closed:
        total += math.hypot(points[0][0] - points[-1][0],
                            points[0][1] - points[-1][1])
    return total


def _resample_open(points: Sequence, n: int) -> tuple:
    """``n`` points equally spaced along an open polyline by arc length."""
    if n <= 1:
        return tuple(points[:1])
    if len(points) < 2:
        return tuple([tuple(points[0])] * n if points else ())
    seg = _cumulative(points)
    total = seg[-1]
    if total <= 0.0:
        return tuple(tuple(points[0]) for _ in range(n))
    out: list = []
    j = 0
    last = len(seg) - 1
    for i in range(n):
        target = total * i / (n - 1)
        while j < last - 1 and seg[j + 1] < target:
            j += 1
        p0, p1 = points[j], points[j + 1]
        s0, s1 = seg[j], seg[j + 1]
        span = s1 - s0
        t = 0.0 if span <= 0.0 else (target - s0) / span
        out.append((p0[0] + (p1[0] - p0[0]) * t,
                    p0[1] + (p1[1] - p0[1]) * t))
    return tuple(out)


def _resample_closed(points: Sequence, n: int) -> tuple:
    """``n`` points equally spaced around a closed ring by arc length."""
    if len(points) < 2:
        return tuple([tuple(points[0])] * max(n, 1) if points else ())
    ring = list(points) + [points[0]]
    out = _resample_open(ring, n + 1)
    return tuple(out[:-1])


def _centroid(points: Sequence) -> tuple:
    n = float(len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n)


def _point_segment_distance(px, py, x0, y0, x1, y1) -> float:
    dx, dy = x1 - x0, y1 - y0
    span = dx * dx + dy * dy
    if span <= 0.0:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / span
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _ring_distances(points: Sequence, ring: Sequence,
                    closed: bool = True) -> list:
    """Distance from every input point to the polyline ``ring`` (pt)."""
    if len(ring) < 2:
        return []
    segs = list(zip(ring, ring[1:]))
    if closed:
        segs.append((ring[-1], ring[0]))
    out: list = []
    for px, py in points:
        best = float("inf")
        for (x0, y0), (x1, y1) in segs:
            d = _point_segment_distance(px, py, x0, y0, x1, y1)
            if d < best:
                best = d
        out.append(best)
    return out


def _ring_stats(points: Sequence, ring: Sequence, closed: bool = True
                ) -> tuple:
    """``(robust, worst)`` residual from ``points`` to ``ring``.

    ``robust`` averages the worst decile instead of taking the single
    worst sample: one stray sample (a pressure spike, a coalesced move)
    must not veto an otherwise clean fit, while a *systematic* mismatch —
    every corner of a blob sitting off the candidate — still shows up at
    full strength.
    """
    dists = _ring_distances(points, ring, closed)
    if not dists:
        return (float("inf"), float("inf"))
    dists.sort()
    count = max(3, len(dists) // 10)
    worst = dists[-count:]
    return (sum(worst) / float(len(worst)), dists[-1])


def _fit_quality(points: Sequence, contour: Sequence, closed: bool,
                 diag: float, thickness: float):
    """Shared, *post-classification* fit guard for a closed candidate.

    The shape-specific evidence in the classifiers decides *what* the
    stroke is; this guard only rejects a primitive that does not follow
    the drawn path. ``diag`` is the stroke's bounding-box diagonal, so the
    measure is size-normalized and directly comparable across shapes.
    Returns ``(score, robust_ratio, worst_ratio)`` or ``None``.
    """
    robust, worst = _ring_stats(points, contour, closed)
    if not (math.isfinite(robust) and math.isfinite(worst)) or diag <= 0.0:
        return None
    limit = max(FIT_ERROR_MIN_PT, FIT_ERROR_THICKNESS_FRAC * thickness,
                FIT_ERROR_RATIO * diag)
    worst_limit = max(FIT_WORST_MIN_PT, FIT_WORST_RATIO * diag)
    if robust > limit or worst > worst_limit:
        return None
    return (max(0.0, 1.0 - robust / limit), robust / diag, worst / diag)


def _moving_average(values: Sequence, window: int) -> list:
    """Centred moving average with clamped ends (no phase shift)."""
    n = len(values)
    if n <= 1 or window <= 1:
        return list(values)
    half = max(1, int(window) // 2)
    out: list = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        chunk = values[lo:hi]
        out.append(sum(chunk) / float(len(chunk)))
    return out


def _polygon_area(ring: Sequence) -> float:
    """Absolute shoelace area of an ordered vertex ring (pt^2)."""
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        total += x0 * y1 - x1 * y0
    return abs(total) * 0.5


def _convex_hull(points: Sequence) -> tuple:
    """Monotone-chain convex hull (counter-clockwise, no repeat)."""
    ordered = sorted(set(points))
    if len(ordered) <= 2:
        return tuple(ordered)

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list = []
    for p in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0.0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0.0:
            upper.pop()
        upper.append(p)
    return tuple(lower[:-1] + upper[:-1])


def _simplify_convex(ring: Sequence, tolerance: float) -> tuple:
    """Drop hull vertices flatter than ``tolerance`` (noise removal).

    Removing the flattest removable vertex repeatedly keeps genuine
    corners (their removal distance is large) and removes jitter along an
    edge, so a shaky triangle still presents three vertices to the
    triangle search instead of dozens.
    """
    pts = list(ring)
    limit = max(0.0, float(tolerance))
    changed = True
    while changed and len(pts) > 3:
        changed = False
        best_index = None
        best_distance = None
        for i in range(len(pts)):
            ax, ay = pts[i - 1]
            bx, by = pts[i]
            cx, cy = pts[(i + 1) % len(pts)]
            d = _point_segment_distance(bx, by, ax, ay, cx, cy)
            if d <= limit and (best_distance is None or d < best_distance):
                best_distance = d
                best_index = i
        if best_index is not None:
            pts.pop(best_index)
            changed = True
    return tuple(pts)


def _polygon_centroid(ring: Sequence) -> tuple:
    """Area centroid of an ordered vertex ring.

    Used instead of the sample mean for circle fitting: a stroke that
    stops short of closing is a point *set* whose mean is pulled toward
    the drawn side, while the area centroid of the implied polygon is
    nearly unbiased (the missing wedge is small in area even when its arc
    is long), so an imperfectly closed circle still fits cleanly.
    """
    n = len(ring)
    if n < 3:
        return _centroid(ring)
    area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) <= 1e-12:
        return _centroid(ring)
    return (cx / (3.0 * area), cy / (3.0 * area))


def _direction(a: Sequence, b: Sequence):
    """Unit vector from ``a`` to ``b``, or ``None`` when degenerate."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = math.hypot(dx, dy)
    if span <= 0.0:
        return None
    return (dx / span, dy / span)


def _angle_between(u: Sequence, v: Sequence) -> float:
    """Magnitude of the angle between two unit vectors (degrees)."""
    dot = u[0] * v[0] + u[1] * v[1]
    cross = u[0] * v[1] - u[1] * v[0]
    return abs(math.degrees(math.atan2(cross, dot)))


def _triangle_area(a: Sequence, b: Sequence, c: Sequence) -> float:
    return abs((b[0] - a[0]) * (c[1] - a[1])
               - (b[1] - a[1]) * (c[0] - a[0])) * 0.5


def _min_area_rect(hull: Sequence):
    """Minimum-area enclosing rectangle of a convex hull.

    Returns ``(area, cx, cy, ux, uy, half_a, half_b)`` where ``(ux, uy)``
    is the unit direction of the ``half_a`` side, or ``None``. Rotating
    calipers over the hull edges: for a drawn rectangle the best
    orientation is its own edges, so slightly non-parallel sides and
    rounded corners are absorbed instead of being rejected by an
    exact-corner test.
    """
    n = len(hull)
    if n < 3:
        return None
    best = None
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        edge = math.hypot(bx - ax, by - ay)
        if edge <= 0.0:
            continue
        ux, uy = (bx - ax) / edge, (by - ay) / edge
        vx, vy = -uy, ux
        us = [p[0] * ux + p[1] * uy for p in hull]
        vs = [p[0] * vx + p[1] * vy for p in hull]
        umin, umax = min(us), max(us)
        vmin, vmax = min(vs), max(vs)
        width = umax - umin
        height = vmax - vmin
        area = width * height
        if area <= 0.0:
            continue
        if best is not None and area >= best[0]:
            continue
        cu = (umin + umax) * 0.5
        cv = (vmin + vmax) * 0.5
        best = (area,
                cu * ux + cv * vx, cu * uy + cv * vy,
                ux, uy, width * 0.5, height * 0.5)
    return best


def _rect_ring(cx, cy, ux, uy, half_a, half_b) -> tuple:
    """Corners of a rotated box, in ring order."""
    vx, vy = -uy, ux
    return (
        (cx + half_a * ux + half_b * vx, cy + half_a * uy + half_b * vy),
        (cx + half_a * ux - half_b * vx, cy + half_a * uy - half_b * vy),
        (cx - half_a * ux - half_b * vx, cy - half_a * uy - half_b * vy),
        (cx - half_a * ux + half_b * vx, cy - half_a * uy + half_b * vy),
    )


# ------------------------------------------------------- corner analysis


def _turn_angles(ring: Sequence, k: int | None = None) -> list:
    """Turn magnitude (degrees, 0..180) at every ring vertex.

    ``k`` is the half-window in samples. It defaults to a fraction of the
    ring so the incoming/outgoing directions are estimated *along the
    edges* on either side of a corner: a resampled corner rarely lands
    exactly on a sample, and a too-small window would split its 90-degree
    turn between two neighbours and under-measure it.
    """
    n = len(ring)
    if k is None:
        k = max(2, int(round(n / 32.0)))
    k = max(1, min(int(k), max(1, n // 3)))
    out: list = []
    for i in range(n):
        ax, ay = ring[(i - k) % n]
        bx, by = ring[i]
        cx, cy = ring[(i + k) % n]
        v1 = (bx - ax, by - ay)
        v2 = (cx - bx, cy - by)
        if (v1[0] * v1[0] + v1[1] * v1[1]) <= 0.0:
            out.append(0.0)
            continue
        if (v2[0] * v2[0] + v2[1] * v2[1]) <= 0.0:
            out.append(0.0)
            continue
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        cross = v1[0] * v2[1] - v1[1] * v2[0]
        out.append(abs(math.degrees(math.atan2(cross, dot))))
    return out


def _turn_profile(ring: Sequence) -> list:
    """Multiscale corner profile: per vertex, the strongest turn.

    A small window measures a sharp corner at full strength; a larger
    window measures a *rounded* corner at full strength (a short window
    only sees the chord of the arc and under-reports). Taking the maximum
    per vertex over both windows therefore detects sharp and rounded
    corners without rounding genuine ones away — and without needing a
    second, independently tuned detector.
    """
    n = len(ring)
    if n < 8:
        return [0.0] * n
    small = _turn_angles(ring, max(2, int(round(n / 32.0))))
    large = _turn_angles(ring, max(3, int(round(n / 16.0))))
    return [max(a, b) for a, b in zip(small, large)]


def _corner_peaks(ring: Sequence, turns: Sequence | None = None) -> list:
    """Indices of confident corners (suppressed, plateau-merged)."""
    n = len(ring)
    if n < 8:
        return []
    profile = list(turns) if turns is not None else _turn_profile(ring)
    suppress = max(2, int(round(n * CORNER_SUPPRESS_FRAC)))
    # Candidates: local maxima above the threshold.
    peaks: list = []
    for i in range(n):
        value = profile[i]
        if value < CORNER_ANGLE_DEG:
            continue
        window_ok = True
        for d in (1, 2):
            if profile[(i - d) % n] > value or profile[(i + d) % n] > value:
                window_ok = False
                break
        if window_ok:
            peaks.append((value, i))
    peaks.sort(reverse=True)
    accepted: list = []
    for _value, index in peaks:
        conflict = False
        for other in accepted:
            delta = abs(other - index)
            delta = min(delta, n - delta)
            if delta < suppress:
                conflict = True
                break
        if not conflict:
            accepted.append(index)
    accepted.sort()
    return accepted


def _bounding_box(points: Sequence) -> tuple:
    """``(minx, miny, maxx, maxy, diagonal)`` of the samples."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    return (minx, miny, maxx, maxy, math.hypot(maxx - minx, maxy - miny))


def _dedupe_adjacent(points: Sequence) -> list:
    """Drop consecutive duplicate samples (zero-length segments).

    Resampling already tolerates them, but removing them here keeps
    direction estimates and corner evidence from looking at a zero
    vector when a pen reports the same coordinate twice.
    """
    out: list = []
    for p in points:
        if out and math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) <= 1e-9:
            continue
        out.append((float(p[0]), float(p[1])))
    return out


def _rdp(points: Sequence, eps: float) -> list:
    """Ramer-Douglas-Peucker simplification; endpoints are kept."""
    if len(points) <= 2:
        return [(float(p[0]), float(p[1])) for p in points]
    first, last = points[0], points[-1]
    index = 0
    max_dist = -1.0
    for i in range(1, len(points) - 1):
        d = _point_segment_distance(points[i][0], points[i][1],
                                    first[0], first[1], last[0], last[1])
        if d > max_dist:
            max_dist = d
            index = i
    if max_dist > eps:
        left = _rdp(points[:index + 1], eps)
        right = _rdp(points[index:], eps)
        return left[:-1] + right
    return [(float(first[0]), float(first[1])),
            (float(last[0]), float(last[1]))]


def _merge_corners(corners: Sequence, diag: float) -> tuple:
    """Average corners that are closer than a fraction of the diagonal.

    A resampled corner frequently straddles two neighbouring samples; the
    reference classifier merges those candidates before counting vertices.
    A duplicated closing vertex is dropped first.
    """
    pts = [(float(p[0]), float(p[1])) for p in corners]
    if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0],
                                   pts[0][1] - pts[-1][1]
                                   ) < CORNER_CLOSE_RATIO * diag:
        pts.pop()
    merged: list = []
    for p in pts:
        if merged and math.hypot(
                p[0] - merged[-1][0],
                p[1] - merged[-1][1]) < CORNER_MERGE_RATIO * diag:
            q = merged[-1]
            merged[-1] = ((p[0] + q[0]) * 0.5, (p[1] + q[1]) * 0.5)
        else:
            merged.append(p)
    if len(merged) > 1 and math.hypot(merged[0][0] - merged[-1][0],
                                      merged[0][1] - merged[-1][1]
                                      ) < CORNER_MERGE_RATIO * diag:
        merged.pop()
    return tuple(merged)


def _drop_flat_vertices(corners: Sequence, diag: float) -> tuple:
    """Remove nearly collinear vertices (including the forced start point).

    RDP always keeps the first/last sample, so a stroke that begins in the
    middle of an edge contributes a flat extra vertex. Dropping it here
    (at a smaller tolerance than the simplification itself) is what lets
    a clean triangle report three corners instead of four.
    """
    pts = [(float(p[0]), float(p[1])) for p in corners]
    limit = ADAPTIVE_FLAT_RATIO * diag
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a = pts[i - 1]
            b = pts[i]
            c = pts[(i + 1) % len(pts)]
            if _point_segment_distance(b[0], b[1], a[0], a[1],
                                       c[0], c[1]) <= limit:
                pts.pop(i)
                changed = True
                break
    return tuple(pts)


def _polygon_fit_error(points: Sequence, poly: Sequence) -> float:
    """Mean distance from samples to a closed polygon's boundary (pt)."""
    if len(poly) < 2 or not points:
        return float("inf")
    total = 0.0
    count = len(poly)
    for p in points:
        best = float("inf")
        for i in range(count):
            q0 = poly[i]
            q1 = poly[(i + 1) % count]
            d = _point_segment_distance(p[0], p[1], q0[0], q0[1], q1[0], q1[1])
            if d < best:
                best = d
        total += best
    return total / float(len(points))


def _segments_cross(a0: Sequence, a1: Sequence, b0: Sequence,
                    b1: Sequence) -> bool:
    """True when two segments cross at a point interior to both.

    Strict orientation signs ignore shared endpoints and collinear
    touches, so adjacent polygon edges are never reported as crossings.
    """
    def orient(o, p, q) -> float:
        return ((p[0] - o[0]) * (q[1] - o[1])
                - (p[1] - o[1]) * (q[0] - o[0]))

    d1 = orient(a0, a1, b0)
    d2 = orient(a0, a1, b1)
    d3 = orient(b0, b1, a0)
    d4 = orient(b0, b1, a1)
    return (((d1 > 0.0) != (d2 > 0.0))
            and ((d3 > 0.0) != (d4 > 0.0)))


def _ring_self_intersects(ring: Sequence) -> bool:
    """Whether an ordered vertex ring crosses itself (non-adjacent edges)."""
    n = len(ring)
    if n < 4:
        return False
    for i in range(n):
        a0, a1 = ring[i], ring[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            b0, b1 = ring[j], ring[(j + 1) % n]
            if _segments_cross(a0, a1, b0, b1):
                return True
    return False


def _polygon_min_edge(ring: Sequence) -> float:
    """Shortest edge length of an ordered ring (0 for degenerate input)."""
    n = len(ring)
    if n < 2:
        return 0.0
    return min(math.hypot(ring[(i + 1) % n][0] - ring[i][0],
                          ring[(i + 1) % n][1] - ring[i][1])
               for i in range(n))


def _adaptive_corners(ring: Sequence, diag: float) -> tuple:
    """Stable vertex count from Douglas-Peucker across several tolerances.

    The reference classifier widens the tolerance until the vertex count
    stops shrinking and treats the result as corner evidence rather than
    as a hard "exactly four corners" test. The scan starts at the flattest
    vertex so the closure gap - a chord, not a corner - does not become
    the first split. Returns an ordered vertex ring; empty when the
    simplification is unusable or still has fewer than three vertices.
    """
    if len(ring) < 4 or diag <= 0.0:
        return ()
    turns = _turn_profile(ring)
    start = min(range(len(ring)), key=lambda i: turns[i])
    rotated = list(ring[start:]) + list(ring[:start])
    closed = rotated + [rotated[0]]
    best = _drop_flat_vertices(
        _merge_corners(_rdp(closed, max(ADAPTIVE_TOLERANCES[0] * diag,
                                        ADAPTIVE_MIN_PT)), diag),
        diag)
    best_count = len(best)
    for factor in ADAPTIVE_TOLERANCES[1:]:
        if best_count <= 3:
            break
        candidate = _drop_flat_vertices(
            _merge_corners(_rdp(closed, max(factor * diag,
                                            ADAPTIVE_MIN_PT)), diag), diag)
        if 3 <= len(candidate) < best_count:
            best, best_count = candidate, len(candidate)
    if len(best) < 3:
        return ()
    if _polygon_fit_error(ring, best) / diag > ADAPTIVE_MAX_FIT_ERROR:
        return ()
    return best


def _oriented_edge_fraction(points: Sequence, cx: float, cy: float,
                            ux: float, uy: float, half_a: float,
                            half_b: float, band: float) -> float:
    """Share of samples within ``band`` of a rotated box's boundary."""
    if not points:
        return 0.0
    vx, vy = -uy, ux
    count = 0
    for p in points:
        du = (p[0] - cx) * ux + (p[1] - cy) * uy
        dv = (p[0] - cx) * vx + (p[1] - cy) * vy
        outside_u = max(abs(du) - half_a, 0.0)
        outside_v = max(abs(dv) - half_b, 0.0)
        if outside_u > 0.0 or outside_v > 0.0:
            distance = math.hypot(outside_u, outside_v)
        else:
            distance = min(half_a - abs(du), half_b - abs(dv))
        if distance <= band:
            count += 1
    return count / float(len(points))


# -------------------------------------------------------------- fitting


def _fit_line(points: Sequence, raw_ends: Sequence, thickness: float,
              arc: float | None = None) -> ShapeCandidate | None:
    """Straight-line candidate built on the stroke's own chord.

    The committed segment is exactly the recorded start/end pair, so a
    recognized line never shortens or shifts what the user drew. The
    chord is then used as evidence, in three independent ways:

    * sagitta (``LINE_MAX_BOW_RATIO``) — a modest hand wobble passes, a
      visible arc does not;
    * chord / arc length (``LINE_MIN_CHORD_RATIO``) — a loose surplus-arc
      guard: it catches loops and double-drawn strokes but deliberately
      tolerates the sub-percent arc surplus ordinary tremor adds, because
      a visible arc is rejected by the gates below anyway;
    * end-tangent bend (``LINE_MAX_BEND_DEG``) — an arc turns the start
      and end tangents by its sweep angle, while a bowed line only
      spreads them by twice the bow angle. The tangents are estimated at
      two window sizes and the smaller bend is used, so a short pen-down
      hook does not masquerade as curvature.

    An S-curve survives all three (small sagitta, chord == arc, parallel
    end tangents), so the smoothed residual profile is checked for
    significant lobes on *both* sides of the chord; that is what keeps
    ordinary handwriting curves freehand.
    """
    if len(points) < 2:
        return None
    start = tuple(points[0])
    end = tuple(points[-1])
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    if chord < MIN_SEGMENT_PT:
        return None
    if arc is None:
        arc = _perimeter(points, False)
    if arc <= 0.0 or chord / arc < LINE_MIN_CHORD_RATIO:
        return None
    ux, uy = (end[0] - start[0]) / chord, (end[1] - start[1]) / chord
    vx, vy = -uy, ux
    devs = [(p[0] - start[0]) * vx + (p[1] - start[1]) * vy
            for p in points]
    bow = max(max(devs), -min(devs))
    limit = max(LINE_NOISE_PT, LINE_THICKNESS_FRAC * thickness,
                LINE_MAX_BOW_RATIO * chord)
    if bow > limit:
        return None
    smooth = _moving_average(devs, max(3, len(devs) // 16))
    floor = max(LINE_DOUBLE_SIDED_MIN_PT, LINE_DOUBLE_SIDED_RATIO * chord)
    if max(smooth) > floor and -min(smooth) > floor:
        return None
    n = len(points)
    bends = []
    for window in (max(2, n // 16), max(2, n // 8)):
        t0 = _direction(points[0], points[window])
        t1 = _direction(points[-1 - window], points[-1])
        if t0 is not None and t1 is not None:
            bends.append(_angle_between(t0, t1))
    # Multiscale estimate: a tip hook inflates the short-window angle while
    # the larger window measures the stroke's intended direction. Taking
    # the smaller reading keeps ordinary fast pen-down hooks from rejecting
    # a straight line; a real arc bends both windows by its sweep angle.
    if bends and min(bends) > LINE_MAX_BEND_DEG:
        return None
    ends = (tuple(raw_ends[0]), tuple(raw_ends[-1]))
    seg = math.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1])
    if seg < MIN_SEGMENT_PT:
        return None
    confidence = max(0.0, 1.0 - (bow / limit if limit > 0.0 else 1.0))
    return ShapeCandidate(kind="line", contour=ends, closed=False,
                          confidence=confidence)


def _fit_rectangle(ring: Sequence, hull: Sequence, stroke_area: float,
                   diag: float, thickness: float) -> ShapeCandidate | None:
    """Rectangle from the stroke's oriented minimum-area box.

    Primary evidence is rotation-aware box *coverage* - the stroke's
    enclosed area divided by the box area (rectangles ~1.0, circles
    ~pi/4 = 0.785, triangles ~0.5) - plus the share of drawn samples that
    lie on the box boundary. Both survive bowed sides, moderately rounded
    corners, uneven lengths and a short closure gap, and neither needs an
    exact corner count. The old "box may exceed the hull by at most 15 %"
    gate is gone: it vetoed ordinary shaky rectangles while these two
    measures already separate rectangles from circles and triangles.

    The soft pair is the reference classifier's second chance: a shaky
    rectangle can over-split into 4-6 corners and miss the strict edge
    band on one side while still filling its box, which no circle
    (pi/4) or triangle (~0.5) can do at the relaxed coverage.
    """
    box = _min_area_rect(hull)
    if box is None:
        return None
    box_area, cx, cy, ux, uy, half_a, half_b = box
    if min(half_a, half_b) * 2.0 < MIN_SEGMENT_PT:
        return None
    if box_area <= 0.0 or stroke_area <= 0.0:
        return None
    coverage = stroke_area / box_area
    contour = _rect_ring(cx, cy, ux, uy, half_a, half_b)
    quality = _fit_quality(ring, contour, True, diag, thickness)
    if quality is None:
        return None
    edge_fraction = _oriented_edge_fraction(
        ring, cx, cy, ux, uy, half_a, half_b,
        max(RECT_EDGE_BAND_RATIO * diag, FIT_ERROR_MIN_PT,
            FIT_ERROR_THICKNESS_FRAC * thickness))
    primary = (coverage >= RECT_MIN_COVERAGE
               and edge_fraction >= RECT_MIN_EDGE_FRACTION)
    if not primary:
        corners = _adaptive_corners(ring, diag)
        secondary = (4 <= len(corners) <= 6
                     and coverage >= RECT_SOFT_COVERAGE
                     and edge_fraction >= RECT_SOFT_EDGE_FRACTION)
        if not secondary:
            return None
    return ShapeCandidate(
        kind="rectangle", contour=contour, closed=True,
        confidence=quality[0],
        meta=(cx, cy, half_a, half_b, math.degrees(math.atan2(uy, ux))))


def _fit_triangle(ring: Sequence, hull: Sequence, hull_area: float,
                  diag: float, thickness: float) -> ShapeCandidate | None:
    """Triangle from the best three-corner interpretation of the path.

    Corner evidence is adaptive, as in the reference classifier: when
    Douglas-Peucker across its tolerance sequence returns exactly three
    vertices, those vertices come from the stroke and become the
    candidate. Otherwise the largest triangle inscribed in the hull is
    used, which tolerates a heavily rounded or displaced vertex because
    the vertex is *chosen*, not detected.

    The hull-area share is the polygon-count guard: a quad, pentagon or
    circle tops out near or below half the hull area, while a triangle
    with rounded corners stays well above it.
    """
    if len(hull) < 3 or hull_area <= 0.0:
        return None
    corners = _adaptive_corners(ring, diag)
    if len(corners) == 3:
        verts = corners
        min_share = TRI_ADAPTIVE_MIN_HULL_AREA_FRAC
    else:
        simple = _simplify_convex(
            hull, max(HULL_SIMPLIFY_RATIO * 0.5 * diag, HULL_SIMPLIFY_MIN_PT))
        count = len(simple)
        if count < 3 or count > TRI_MAX_VERTICES:
            return None
        best_area = 0.0
        best_triple = None
        for i in range(count):
            for j in range(i + 1, count):
                for k in range(j + 1, count):
                    area = _triangle_area(simple[i], simple[j], simple[k])
                    if area > best_area:
                        best_area = area
                        best_triple = (i, j, k)
        if best_triple is None or best_area <= 0.0:
            return None
        verts = tuple(simple[i] for i in best_triple)
        min_share = TRI_MIN_HULL_AREA_FRAC
    if _triangle_area(*verts) < min_share * hull_area:
        return None
    edges = [math.hypot(verts[(i + 1) % 3][0] - verts[i][0],
                        verts[(i + 1) % 3][1] - verts[i][1])
             for i in range(3)]
    if min(edges) < MIN_SEGMENT_PT:
        return None
    quality = _fit_quality(ring, verts, True, diag, thickness)
    if quality is None:
        return None
    return ShapeCandidate(kind="triangle", contour=verts, closed=True,
                          confidence=quality[0])


# ------------------------------------------------- generic polygon fitting


def _polygon_candidates(ring: Sequence, diag: float,
                        turns: Sequence | None = None) -> list:
    """Ordered-vertex candidates from a bounded simplification sweep.

    Instead of trusting one Douglas-Peucker vertex count, the closed ring
    is simplified at every existing adaptive tolerance and each plausible
    ring (4..8 vertices) is kept, measured against the drawn samples and
    ranked by normalized fit error; ties prefer fewer vertices. The scan
    starts at the flattest sample so the stroke's closure gap (a chord,
    not a corner) never becomes the forced start vertex.

    Candidates with a too-short edge, a duplicate vertex or a
    self-intersecting edge pair are discarded. Returns
    ``[(error_pt, vertex_count, ring), ...]`` sorted best-first; empty
    when no tolerance yields a usable polygon.
    """
    if len(ring) < 8 or diag <= 0.0:
        return []
    profile = list(turns) if turns is not None else _turn_profile(ring)
    start = min(range(len(ring)), key=lambda i: profile[i])
    rotated = list(ring[start:]) + list(ring[:start])
    closed = rotated + [rotated[0]]
    best_by_count: dict = {}
    for factor in ADAPTIVE_TOLERANCES:
        raw = _rdp(closed, max(factor * diag, ADAPTIVE_MIN_PT))
        poly = _drop_flat_vertices(_merge_corners(raw, diag), diag)
        count = len(poly)
        if not (POLY_MIN_VERTICES <= count <= POLY_MAX_VERTICES):
            continue
        if len({(round(p[0], 6), round(p[1], 6)) for p in poly}) != count:
            continue
        if _polygon_min_edge(poly) < MIN_SEGMENT_PT:
            continue
        if _ring_self_intersects(poly):
            continue
        error = _polygon_fit_error(ring, poly)
        if not math.isfinite(error) or error / diag > POLY_MAX_FIT_ERROR:
            continue
        current = best_by_count.get(count)
        if current is None or error < current[0]:
            best_by_count[count] = (error, poly)
    ranked = [(error, count, poly)
              for count, (error, poly) in best_by_count.items()]
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked


def _polygon_corner_evidence(ring: Sequence, turns: Sequence,
                             poly: Sequence) -> bool:
    """Discrete corner evidence for a fitted polygon (M7.1.4).

    A smooth circle or oval can fit an inscribed 4-8-gon within a
    permissive tolerance, so geometry alone is not enough. A polygon's
    turning is *concentrated* at its vertices, while a round outline's is
    spread along the whole contour (or over two smooth ends of an oval).

    Two scale-independent checks:

    * every fitted vertex must have a local drawn turn of at least
      :data:`POLY_VERTEX_TURN_DEG` in the samples around it. The
      multiscale profile is used so a rounded (split) corner still reads
      at full strength; this gate rejects corners that DP placed on a
      low-curvature flank, such as the long sides of an oval;
    * the windows around all fitted vertices must capture at least
      :data:`POLY_TURN_CONCENTRATION` of the stroke's total *fine-grained*
      turning (single-step turns, so a sharp corner is counted once rather
      than smeared across a multiscale plateau). This is the decisive
      round-outline rejection: with 4..8 disjoint windows a uniform
      (circle) turn profile can capture at most 8 * (2w + 1) / n = 0.56
      of the total, well below the threshold, while a polygon spends
      almost all of its turning inside its corner windows.

    Rounded polygons pass: their fillet is short compared with the window
    and still reports most of the corner angle, and they do not need
    razor-sharp vertices. The test reuses the already-computed turn
    profile plus one cheap single-step turn pass.
    """
    n = len(ring)
    if n < 8 or not poly or not turns or len(turns) != n:
        return False
    fine = _turn_angles(ring, 1)
    total = sum(fine)
    if not math.isfinite(total) or total <= 0.0:
        return False
    window = max(1, int(round(n * POLY_TURN_WINDOW_FRAC)))
    covered: set = set()
    for px, py in poly:
        index = min(range(n), key=lambda i: ((ring[i][0] - px) ** 2
                                             + (ring[i][1] - py) ** 2))
        local = 0.0
        for delta in range(-window, window + 1):
            j = (index + delta) % n
            covered.add(j)
            if turns[j] > local:
                local = turns[j]
        if local < POLY_VERTEX_TURN_DEG:
            return False
    captured = sum(fine[i] for i in covered)
    return captured / total >= POLY_TURN_CONCENTRATION


def _classify_polygon(ring: Sequence, diag: float, hull_area: float,
                      turns: Sequence, thickness: float
                      ) -> ShapeCandidate | None:
    """Generic polygon fallback for closed, clearly polygonal outlines.

    Runs only after the circle, rectangle and triangle stages declined the
    stroke, so it can never pre-empt a specialized reading; open strokes
    and V shapes never reach it (the line branch owns them). It accepts
    any simple outline whose ordered corners survive the candidate sweep,
    the area sanity check, the discrete corner-evidence test and the
    shared normalized fit guard — a trapezoid, parallelogram, pentagon or
    hexagon is the same ``"polygon"`` kind with its own vertices.

    Poor fits, ambiguous contours and circle/oval-like contours that only
    missed the circle gates all return ``None``: the freehand stroke is
    preserved. The candidate carries exactly the fitted vertices, not the
    resampled recognition samples.
    """
    if len(ring) < 8 or diag <= 0.0 or hull_area <= 0.0:
        return None
    stroke_area = _polygon_area(ring)
    if stroke_area < POLY_MIN_AREA_SHARE * hull_area:
        return None
    for _error, _count, poly in _polygon_candidates(ring, diag, turns):
        if _polygon_area(poly) < POLY_MIN_AREA_SHARE * hull_area:
            continue
        if not _polygon_corner_evidence(ring, turns, poly):
            continue
        quality = _fit_quality(ring, poly, True, diag, thickness)
        if quality is None:
            continue
        return ShapeCandidate(kind="polygon", contour=poly, closed=True,
                              confidence=quality[0])
    return None


# ------------------------------------------------ circle fitting helpers


def _circle_contour(cx: float, cy: float, radius: float):
    """Validated ``(geometry, contour)`` for one circle.

    Geometry is ``(cx, cy, radius, radius, 0.0)`` — the box-shaped meta the
    viewer commits as an equal-half-axis annotation — and the contour is
    the closed ``CIRCLE_STEPS``-gon used for measuring, morphing and
    committing. Numerically unusable parameters return ``None``.
    """
    try:
        cx = float(cx)
        cy = float(cy)
        radius = float(radius)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (cx, cy, radius)):
        return None
    if radius <= 0.0:
        return None
    contour = []
    for i in range(CIRCLE_STEPS):
        phi = 2.0 * math.pi * i / CIRCLE_STEPS
        contour.append((cx + radius * math.cos(phi),
                        cy + radius * math.sin(phi)))
    return ((cx, cy, radius, radius, 0.0), tuple(contour))


def _circle_radii(ring: Sequence, cx: float, cy: float) -> list:
    """Trimmed, ascending sample distances to ``(cx, cy)``.

    The extreme :data:`CIRCLE_TRIM_FRAC` of samples is dropped at both
    ends so a pressure spike, a coalesced move or an unevenly sampled seam
    cannot drag the fit or its consistency measure.
    """
    radii = sorted(math.hypot(p[0] - cx, p[1] - cy) for p in ring)
    count = len(radii)
    trim = int(count * CIRCLE_TRIM_FRAC)
    if trim > 0 and count - 2 * trim >= 3:
        radii = radii[trim:count - trim]
    return radii


def _circle_fit(ring: Sequence):
    """Robust circle fit: area centroid plus trimmed-mean radius.

    The centre is the *area* centroid of the implied polygon rather than
    the sample mean: a stroke that stops short of closing is a point set
    whose mean is pulled toward the drawn side, while the area centroid of
    the ring is nearly unbiased for the missing wedge. The radius is the
    mean distance to that centre over the trimmed sample set, so an
    imperfect hand circle, uneven sampling or a modest endpoint gap still
    yields the radius the user drew. Returns ``(cx, cy, radius)`` or
    ``None``.
    """
    if len(ring) < 5:
        return None
    cx, cy = _polygon_centroid(ring)
    if not (math.isfinite(cx) and math.isfinite(cy)):
        return None
    radii = _circle_radii(ring, cx, cy)
    if not radii:
        return None
    radius = sum(radii) / float(len(radii))
    if not math.isfinite(radius) or radius <= 0.0:
        return None
    return (cx, cy, radius)


def _circle_radial_cv(ring: Sequence, cx: float, cy: float) -> float:
    """Spread of sample radii about their own mean (std / mean).

    Measured over the same trimmed sample set as the fit, so a lone stray
    sample is not what decides whether the outline is round. A circle's
    radii are near-constant; an oval's vary along its axes and a cornered
    outline's vary between faces and corners.
    """
    radii = _circle_radii(ring, cx, cy)
    if not radii:
        return float("inf")
    mean_r = sum(radii) / float(len(radii))
    if mean_r <= 0.0:
        return float("inf")
    variance = math.sqrt(sum((r - mean_r) ** 2 for r in radii)
                         / float(len(radii)))
    return variance / mean_r


def _classify_circle(ring: Sequence, diag: float, stroke_area: float,
                     turns: Sequence, peaks: Sequence,
                     thickness: float):
    """Circle evidence and candidate geometry (M7.1.3.1).

    Recognition is circle-only: the candidate always commits *equal*
    horizontal and vertical half-axes and zero rotation, so a stretched
    oval can never be emitted. The evidence is shape-level, not the
    success of a general conic fit:

    * radial consistency (std / mean of the robust sample radii) rejects
      elongated and cornered outlines: an oval's radii vary along its
      axes, a circle's do not, and a polygon's vary between faces and
      corners;
    * the drawn bounding-box aspect is the explicit elongation guard, so
      a contour much longer than wide stays freehand even when its mean
      radius could be fitted;
    * the enclosed area must sit near ``pi * r^2`` under the robust
      radius, rejecting a stroke that traverses only part of a round
      outline (box-filling shapes are separated by the radial spread and
      by the rectangle branch);
    * the strong-corner count is a structural guard against regular
      polygons (a hexagon has six hard turns, a sloppy circle at most a
      seam).

    The shared normalized fit guard then confirms that the circle follows
    the drawn samples.
    """
    if len(ring) < 5 or diag <= 0.0 or stroke_area <= 0.0:
        return None
    strong = sum(1 for index in peaks
                 if turns[index % len(turns)] >= STRONG_CORNER_DEG)
    if strong > CIRCLE_MAX_STRONG_CORNERS:
        return None
    fit = _circle_fit(ring)
    if fit is None:
        return None
    cx, cy, radius = fit
    if radius < MIN_SEGMENT_PT / 2.0:
        return None
    minx, miny, maxx, maxy, _bbox_diag = _bounding_box(ring)
    width = maxx - minx
    height = maxy - miny
    if width <= 0.0 or height <= 0.0:
        return None
    if min(width, height) / max(width, height) < CIRCLE_MIN_BBOX_ASPECT:
        return None
    area_ratio = stroke_area / (math.pi * radius * radius)
    if not (CIRCLE_MIN_AREA_RATIO <= area_ratio <= CIRCLE_MAX_AREA_RATIO):
        return None
    if _circle_radial_cv(ring, cx, cy) > CIRCLE_MAX_RADIAL_CV:
        return None
    built = _circle_contour(cx, cy, radius)
    if built is None:
        return None
    geometry, contour = built
    quality = _fit_quality(ring, contour, True, diag, thickness)
    if quality is None:
        return None
    return ShapeCandidate(kind="circle", contour=contour, closed=True,
                          confidence=quality[0], meta=geometry)


def _direction(a: Sequence, b: Sequence):
    """Unit direction ``a -> b``, or ``None`` for a degenerate pair."""
    dx = float(b[0]) - float(a[0])
    dy = float(b[1]) - float(a[1])
    span = math.hypot(dx, dy)
    if not math.isfinite(span) or span <= 0.0:
        return None
    return (dx / span, dy / span)


def _closing_evidence(ring: Sequence, diag: float) -> bool:
    """True when a gap in the bridged band still implies a closing edge.

    See :data:`CLOSE_GAP_MAX`. The decisive test is directional: the pen
    must have stopped *part-way along an edge*, so the last drawn direction
    continues into the gap chord. A V/L/hook/S/C ends at a corner or a
    reversal relative to that chord and is refused, as is a stroke that
    abandoned an entire side. The stroke must also have covered most of
    the outline, so a short two-sided path can never be bridged.
    """
    n = len(ring)
    if n < 8 or not math.isfinite(diag) or diag <= 0.0:
        return False
    gap = math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1])
    if not math.isfinite(gap) or gap <= 0.0 or gap > CLOSE_GAP_MAX * diag:
        return False
    drawn = _perimeter(ring, False)
    if drawn <= 0.0 or gap > 0.55 * drawn:
        return False
    window = max(2, min(n - 1, n // 32))
    tail = _direction(ring[-1 - window], ring[-1])
    chord = _direction(ring[-1], ring[0])
    if tail is None or chord is None:
        return False
    dot = tail[0] * chord[0] + tail[1] * chord[1]
    dot = -1.0 if dot < -1.0 else (1.0 if dot > 1.0 else dot)
    return math.degrees(math.acos(dot)) <= CLOSE_TANGENT_DEG


def _mean_edge_length(contour: Sequence) -> float:
    """Mean boundary edge length of a closed contour, or ``0.0``."""
    n = len(contour)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        ax, ay = contour[i]
        bx, by = contour[(i + 1) % n]
        total += math.hypot(bx - ax, by - ay)
    return total / float(n)


def _bridged_candidate_ok(candidate: ShapeCandidate, gap: float) -> bool:
    """Closing-edge plausibility for an evidence-bridged candidate.

    The inferred closing edge must be a boundary edge the shape can
    afford: no longer than :data:`CLOSE_EDGE_MEAN_RATIO` times the
    candidate's mean edge. Round candidates keep the established
    :data:`CLOSE_RATIO` convention and are never bridged.
    """
    if candidate is None or not candidate.closed:
        return False
    if candidate.kind == "circle":
        return False
    mean = _mean_edge_length(candidate.contour)
    if mean <= 0.0:
        return False
    return gap <= CLOSE_EDGE_MEAN_RATIO * mean


# ---------------------------------------------------------- recognition


def recognize_shape(points: Sequence, thickness_pt: float = 1.0
                    ) -> ShapeCandidate | None:
    """Classify a freehand centerline, or ``None`` when unconvinced.

    Staged pipeline (M7.1.3.1), after the reference classifier's
    ``fitStroke()``:

    1. Parse, drop duplicate samples, fail on too few samples or a stroke
       too short for its tool thickness, then resample by arc length to
       :data:`MAX_WORK_POINTS` so classification does not depend on event
       density.
    2. Measure the stroke's bounding box and its diagonal; the closure
       ratio is the endpoint gap divided by that diagonal.
    3. Open branch: a line judged by endpoint-chord deviation and
       straightness (sagitta, chord/arc, end-tangent bend, S-curve
       lobes); the committed segment is the recorded start/end pair.
    4. Closed branch: reject strokes drawn more than once around, build
       the hull, then ask each supported shape for its own evidence in a
       fixed order — circle (radial consistency, box aspect, area),
       rectangle (oriented-box coverage, edge proximity), triangle
       (adaptive corner interpretation), then the generic polygon
       fallback (ordered corner ring with discrete corner evidence). A
       shared normalized fit guard confirms each accepted candidate, and
       a stroke that both a circle and a rectangle explain equally well
       stays freehand.

    Polygon fallback (M7.1.4): a closed outline that is not convincingly
    a specialized shape becomes one generic ``"polygon"`` candidate with
    its own ordered vertices (trapezoid, parallelogram, pentagon, hexagon,
    ...). It runs last, is scale-normalized, rejects self-intersecting
    rings, too-short edges, poor fits and contours whose turning is not
    concentrated at the fitted corners — so circles, ovals and scribbles
    stay freehand. Vertex count is bounded by :data:`POLY_MIN_VERTICES` ..
    :data:`POLY_MAX_VERTICES`.

    Circle-only (M7.1.3.1): generalized ellipses are no longer recognized;
    a circle candidate always carries equal half-axes, so an oval stays
    freehand.

    Bridged closure (M7.1.4.1): a single stroke whose endpoint gap exceeds
    :data:`CLOSE_RATIO` but stays within :data:`CLOSE_GAP_MAX` is completed
    into a closed shape only when the gap is evidence of a pen that stopped
    part-way along an edge: the final drawn direction must continue into
    the gap chord (:data:`CLOSE_TANGENT_DEG`) and the implied closing edge
    must be a plausible edge of the fitted candidate
    (:data:`CLOSE_EDGE_MEAN_RATIO`). Round outlines are never bridged. This
    is additional evidence, not a higher global closure tolerance, so V
    shapes, L corners, hooks, S-curves, C shapes and scribbles keep their
    freehand geometry.

    Closed candidates are fitted from drawn samples only: the closure gap
    is classification input, never a fitted chord.

    Args:
        points: page-local centerline samples (pairs or ``Point``).
        thickness_pt: effective tool thickness (pt) — recognition slack
            scales with it, so a thick highlighter is not judged by pen
            tolerances.

    Returns:
        :class:`ShapeCandidate`, or ``None`` (never raises on ordinary
        input; malformed samples are skipped).
    """
    try:
        thickness = float(thickness_pt)
        if not math.isfinite(thickness) or thickness < 0.0:
            thickness = 0.0
    except (TypeError, ValueError):
        thickness = 0.0
    raw = _dedupe_adjacent(_floats(points))
    if len(raw) < MIN_SAMPLES:
        return None
    work = _resample_open(raw, MAX_WORK_POINTS)
    if len(work) < MIN_SAMPLES:
        return None
    length = _perimeter(work, False)
    if length < max(MIN_STROKE_PT, MIN_LINE_LENGTH_THICKNESS * thickness):
        return None
    _minx, _miny, _maxx, _maxy, diag = _bounding_box(work)
    if diag < MIN_SEGMENT_PT:
        return None
    gap = math.hypot(work[0][0] - work[-1][0], work[0][1] - work[-1][1])
    closure = gap / diag
    bridged = False
    if closure > CLOSE_RATIO:
        # M7.1.4.1: a gap wider than the reference closure bound is still
        # completed when the pen stopped part-way along an edge, so the
        # closing chord continues the last drawn direction and is a
        # plausible edge of the fitted shape. Otherwise the open branch
        # owns the stroke exactly as before.
        if closure <= CLOSE_GAP_MAX and _closing_evidence(work, diag):
            bridged = True
        else:
            try:
                return _fit_line(work, (raw[0], raw[-1]), thickness, length)
            except Exception:
                return None
    perimeter = length + gap
    if perimeter < max(MIN_CLOSED_PERIMETER_PT,
                       MIN_CLOSED_PERIMETER_THICKNESS * thickness):
        return None
    hull = _convex_hull(work)
    if len(hull) < 3:
        return None
    hull_perimeter = _perimeter(hull, True)
    if hull_perimeter <= 0.0:
        return None
    if length > SIMPLE_STROKE_MAX_RATIO * hull_perimeter:
        # Drawn more than once around (loop, coil, figure-eight): the
        # hull may look rectangular or circular, but the *stroke* is not
        # a single outline, so no conversion.
        return None
    simple = _simplify_convex(
        hull, max(HULL_SIMPLIFY_RATIO * 0.5 * diag, HULL_SIMPLIFY_MIN_PT))
    if len(simple) < 3:
        return None
    # The triangle share is measured against the *full* hull: simplifying a
    # smooth outline to three vertices (its stop condition) must not make a
    # circle look like a triangle that fills its own hull.
    hull_area = _polygon_area(hull)
    if hull_area <= 0.0:
        return None
    # Cyclic analysis runs on the *open* sample ring: the single
    # wrap-around step is the stroke's own closure gap, so no invented
    # closing chord is ever measured against the fitted contour.
    turns = _turn_profile(work)
    peaks = _corner_peaks(work, turns)
    stroke_area = _polygon_area(work)
    circle = None
    rectangle = None
    # Circle evidence is evaluated first: a circle hugs its bounding-box
    # edges closely enough to pass an edge-proximity test, so rectangle
    # evidence must not get first refusal. M7.1.4.1: round outlines keep the
    # established CLOSE_RATIO closure convention, so a bridged stroke never
    # takes the circle branch (a wide arc gap is a C, not a circle).
    if not bridged:
        try:
            circle = _classify_circle(
                work, diag, stroke_area, turns, peaks, thickness)
        except Exception:
            circle = None
    try:
        rectangle = _fit_rectangle(work, hull, stroke_area, diag, thickness)
    except Exception:
        rectangle = None
    if bridged:
        if not _bridged_candidate_ok(rectangle, gap):
            rectangle = None
    if circle is not None and rectangle is not None:
        # Both readings explain the stroke: it fills its oriented box like
        # a rectangle *and* follows a circle closely (a very rounded
        # rectangle or squircle). Preserve the freehand stroke, as the
        # reference classifier does when its evidence is ambiguous.
        return None
    if circle is not None:
        return circle
    if rectangle is not None:
        return rectangle
    try:
        triangle = _fit_triangle(work, hull, hull_area, diag, thickness)
    except Exception:
        triangle = None
    if bridged and not _bridged_candidate_ok(triangle, gap):
        triangle = None
    if triangle is not None:
        return triangle
    # Generic polygon fallback (M7.1.4): only reached when the specialized
    # circle/rectangle/triangle readings declined, so those keep absolute
    # priority. Shares the already-computed hull, turns and scale.
    try:
        polygon = _classify_polygon(work, diag, hull_area, turns, thickness)
    except Exception:
        polygon = None
    if bridged and not _bridged_candidate_ok(polygon, gap):
        polygon = None
    return polygon


# ---------------------------------------------------------------- morph


def _align_ring(reference: Sequence, ring: Sequence) -> tuple:
    """Best cyclic shift + direction of ``ring`` against ``reference``."""
    n = len(ring)
    if n == 0:
        return ()
    forward = list(ring)
    backward = list(reversed(ring))
    best = None
    best_cost = None
    for base in (forward, backward):
        for offset in range(n):
            cost = 0.0
            for i in range(n):
                rx, ry = reference[i]
                qx, qy = base[(i + offset) % n]
                cost += (rx - qx) ** 2 + (ry - qy) ** 2
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best = [base[(i + offset) % n] for i in range(n)]
    return tuple(best) if best is not None else ()


def _morph_sides(raw: Sequence, fitted: Sequence, *, closed: bool,
                  samples: int) -> tuple:
    """Normalized ``(raw_samples, fitted_samples, translation)``.

    Single implementation behind :func:`prepare_morph` and
    :func:`morph_translation`, so the translation a caller rebases by is
    exactly the one the morph animation applied. ``translation`` is
    ``(dx, dy)`` in page points (raw centroid minus fitted centroid).
    Raises ``ValueError`` on unusable input.
    """
    raw_pts = _floats(raw)
    fit_pts = _floats(fitted)
    if len(raw_pts) < 2 or len(fit_pts) < 2:
        raise ValueError("morph sides need at least two points")
    count = max(8, int(samples))
    if closed:
        raw_s = _resample_closed(raw_pts, count)
        fit_s = _align_ring(raw_s, _resample_closed(fit_pts, count))
    else:
        raw_s = _resample_open(raw_pts, count)
        fit_s = _resample_open(fit_pts, count)
        forward_cost = sum((rx - qx) ** 2 + (ry - qy) ** 2
                           for (rx, ry), (qx, qy) in zip(raw_s, fit_s))
        back = tuple(reversed(fit_s))
        back_cost = sum((rx - qx) ** 2 + (ry - qy) ** 2
                        for (rx, ry), (qx, qy) in zip(raw_s, back))
        if back_cost < forward_cost:
            fit_s = back
    rcx, rcy = _centroid(raw_s)
    fcx, fcy = _centroid(fit_s)
    dx, dy = rcx - fcx, rcy - fcy
    fit_s = tuple((qx + dx, qy + dy) for qx, qy in fit_s)
    return (tuple(raw_s), tuple(fit_s), (dx, dy))


def prepare_morph(raw: Sequence, fitted: Sequence, *, closed: bool = False,
                  samples: int = MORPH_SAMPLES) -> tuple:
    """Normalize two centerlines for index-wise interpolation.

    Both sides are resampled to the same number of equal arc-length
    samples; a closed fitted ring is rotated/reversed into its best
    correspondence with the raw stroke, and the fitted side is finally
    translated so the two centroids coincide (the fitted shape appears
    where the user drew it). Because both sides start and end at the same
    arc-length parameters, blending per index produces a continuous
    morph with no positional jump at ``t == 0``.

    Returns:
        ``(raw_samples, fitted_samples)``, equal-length tuples of
        ``(x, y)`` floats. Raises ``ValueError`` on unusable input.
    """
    raw_s, fit_s, _offset = _morph_sides(raw, fitted, closed=closed,
                                         samples=samples)
    return (raw_s, fit_s)


def morph_translation(raw: Sequence, fitted: Sequence, *,
                      closed: bool = False,
                      samples: int = MORPH_SAMPLES) -> tuple:
    """The exact translation :func:`prepare_morph` applies (page points).

    The morph preview the user sees is the fitted contour translated so
    its centerline centroid coincides with the raw stroke's. Live shape
    adjustment must rebase by that same translation or the adjusted
    preview and the released annotation would differ. This helper returns
    the exact ``(dx, dy)`` used by :func:`prepare_morph` (same resampling
    count, same centroid convention), not an approximation. Unusable
    input yields ``(0.0, 0.0)`` — the same no-op the viewer's morph
    offset has always used.
    """
    try:
        _raw, _fit, offset = _morph_sides(raw, fitted, closed=closed,
                                          samples=samples)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    return (float(offset[0]), float(offset[1]))


def morph_points(morph: Sequence, t: float) -> tuple:
    """Interpolate between prepared morph sides at phase ``t`` (0..1).

    ``t == 0`` is exactly the raw sample set and ``t == 1`` exactly the
    fitted one (translated), so the caller's start and end frames match
    the freehand preview and the committed geometry.
    """
    try:
        raw_s, fit_s = morph[0], morph[1]
    except Exception:
        return ()
    if not raw_s or not fit_s:
        return ()
    try:
        phase = float(t)
    except (TypeError, ValueError):
        phase = 1.0
    if phase != phase:
        phase = 1.0
    phase = 0.0 if phase < 0.0 else (1.0 if phase > 1.0 else phase)
    return tuple((rx + (qx - rx) * phase, ry + (qy - ry) * phase)
                 for (rx, ry), (qx, qy) in zip(raw_s, fit_s))
