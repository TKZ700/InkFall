"""Partial-erasure geometry (M6.6). Qt-free, stdlib-only.

Splits a committed Pen/Highlighter centerline into the fragments that
survive an eraser's swept path. This module is pure geometry: it never
imports Qt, never mutates its inputs, and never touches a store, a
history, or an annotation's identity. The viewer owns all of that.

Model
-----
An eraser gesture is a sequence of straight segments; together they
form the *swept path*. For each eraser segment the eraser covers a
round-capped region of radius ``reach`` around it. A centerline point
is erased when its distance to the swept path is at most::

    local_half_width + reach

The caller folds the antialias tolerance into ``reach`` and supplies a
per-sample half-width consistent with the renderer, so the clipped
region matches what the user sees (including pressure-variable width).

Convexity
---------
For a single eraser segment the distance from a point moving along a
straight stroke segment to that eraser segment is convex in the stroke
parameter (distance to a convex set is convex; an affine path preserves
convexity). Subtracting the linear interpolated half-width keeps it
convex, so ``{t : g(t) <= 0}`` is a single interval, found exactly by a
bounded ternary search for the minimum plus two bisections. Several
eraser segments contribute a union of such intervals, merged per stroke
segment. No fixed sampling is relied on, so thin/sparse crossings are
never skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["Fragment", "erase_stroke"]

#: Below this an interval removes no representable geometry; treat the
#: stroke as untouched rather than emitting a no-op replacement.
_MIN_INTERVAL = 1e-7

#: Fragments shorter than this (in page units) are genuinely degenerate
#: (zero-length) and dropped; everything else is kept, so small valid
#: handwriting remnants survive.
_MIN_FRAGMENT_LENGTH = 1e-6

_TERNARY_ITERATIONS = 60
_BISECT_ITERATIONS = 48


@dataclass(frozen=True, slots=True)
class Fragment:
    """One surviving piece of a clipped centerline.

    ``points`` is a tuple of ``(x, y)`` positions; ``pressures`` is a
    parallel tuple (empty when the source captured no pressure).
    """

    points: tuple
    pressures: tuple = ()


def _point_seg_distance(px: float, py: float, x0: float, y0: float,
                        x1: float, y1: float) -> float:
    """Distance from a point to a segment (exact, endpoints included)."""
    dx = x1 - x0
    dy = y1 - y0
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / length2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    cx = x0 + t * dx
    cy = y0 + t * dy
    return math.hypot(px - cx, py - cy)


def _dist_to_path(px: float, py: float, erasers) -> float:
    """Minimum distance from a point to the union of eraser segments."""
    best = math.inf
    for ex0, ey0, ex1, ey1 in erasers:
        d = _point_seg_distance(px, py, ex0, ey0, ex1, ey1)
        if d < best:
            best = d
    return best


def _single_interval(p0, p1, hw0, hw1, ex0, ey0, ex1, ey1, reach):
    """Erased sub-interval of one stroke segment vs one eraser segment.

    Returns ``(a, b)`` with ``0 <= a <= b <= 1``, or ``None`` when the
    segment never enters the eraser's reach. ``g(t)`` is convex, so the
    sublevel set is one interval.
    """
    ax0, ay0 = p0
    ax1, ay1 = p1

    def g(t: float) -> float:
        x = ax0 + (ax1 - ax0) * t
        y = ay0 + (ay1 - ay0) * t
        hw = hw0 + (hw1 - hw0) * t
        return _point_seg_distance(x, y, ex0, ey0, ex1, ey1) - (hw + reach)

    # Ternary search for the minimum of the convex g on [0, 1].
    lo, hi = 0.0, 1.0
    for _ in range(_TERNARY_ITERATIONS):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if g(m1) < g(m2):
            hi = m2
        else:
            lo = m1
    tmin = (lo + hi) / 2.0
    if g(tmin) > 0.0:
        return None

    # Left boundary: g(0) may already be <= 0 (the vertex is inside).
    if g(0.0) <= 0.0:
        a = 0.0
    else:
        lo2, hi2 = 0.0, tmin
        for _ in range(_BISECT_ITERATIONS):
            mid = (lo2 + hi2) / 2.0
            if g(mid) > 0.0:
                lo2 = mid
            else:
                hi2 = mid
        a = hi2
    # Right boundary: g(1) may already be <= 0.
    if g(1.0) <= 0.0:
        b = 1.0
    else:
        lo2, hi2 = tmin, 1.0
        for _ in range(_BISECT_ITERATIONS):
            mid = (lo2 + hi2) / 2.0
            if g(mid) <= 0.0:
                lo2 = mid
            else:
                hi2 = mid
        b = lo2
    if b < a:
        a, b = b, a
    return (a, b)


def _segment_intervals(p0, p1, hw0, hw1, erasers, reach):
    """Merged erased intervals for one stroke segment (list, sorted)."""
    raw = []
    for eraser in erasers:
        ex0, ey0, ex1, ey1 = eraser
        iv = _single_interval(p0, p1, hw0, hw1, ex0, ey0, ex1, ey1, reach)
        if iv is not None and iv[1] - iv[0] > _MIN_INTERVAL:
            raw.append(iv)
    if not raw:
        return []
    raw.sort()
    merged = [raw[0]]
    for lo, hi in raw[1:]:
        a, b = merged[-1]
        if lo <= b:
            merged[-1] = (a, max(b, hi))
        else:
            merged.append((lo, hi))
    return [(max(0.0, a), min(1.0, b)) for a, b in merged if b > a]


def _in_intervals(t: float, intervals) -> bool:
    for a, b in intervals:
        if a - _MIN_INTERVAL <= t <= b + _MIN_INTERVAL:
            return True
    return False


def erase_stroke(points, pressures, halfwidths, erasers,
                 reach) -> tuple:
    """Clip a centerline into surviving fragments.

    Parameters
    ----------
    points:
        Sequence of ``(x, y)`` canonical samples (the geometry the user
        sees; the caller passes the raw centerline it stores).
    pressures:
        Parallel sequence of per-sample pressures, or an empty sequence
        when the source captured none.
    halfwidths:
        Parallel sequence of non-negative local half-widths (as rendered,
        so pressure-variable pen widths clip correctly).
    erasers:
        Sequence of ``(x0, y0, x1, y1)`` eraser path segments (page
        units, same coordinate system as ``points``).
    reach:
        Eraser radius plus antialias tolerance, in page units.

    Returns
    -------
    ``(changed, fragments)`` where ``fragments`` is a tuple of
    :class:`Fragment`:

    * ``(False, ())`` — nothing was erased; keep the original untouched
      (no replacement command).
    * ``(True, ())`` — the whole stroke lies inside the erased region;
      the caller uses the existing delete command.
    * ``(True, (frag, ...))`` — the surviving pieces, in original
      stroke direction and order.
    """
    try:
        pts = [(float(x), float(y)) for x, y in points]
    except (TypeError, ValueError):
        return (False, ())
    n = len(pts)
    if n == 0:
        return (False, ())
    try:
        hws = [max(0.0, float(h)) for h in halfwidths]
    except (TypeError, ValueError):
        return (False, ())
    if len(hws) != n:
        return (False, ())
    try:
        erasers = [(float(a), float(b), float(c), float(d))
                   for a, b, c, d in erasers]
    except (TypeError, ValueError):
        return (False, ())
    if not erasers:
        return (False, ())
    try:
        reach = float(reach)
    except (TypeError, ValueError):
        return (False, ())
    if not math.isfinite(reach) or reach < 0.0:
        return (False, ())

    prs = ()
    if pressures:
        try:
            prs = tuple(float(p) for p in pressures)
        except (TypeError, ValueError):
            prs = ()
        if len(prs) != n:
            prs = ()

    # A single-point stroke has no segment: it is either fully inside the
    # eraser's reach or untouched.
    if n == 1:
        d = _dist_to_path(pts[0][0], pts[0][1], erasers)
        if d <= hws[0] + reach:
            return (True, ())
        return (False, ())

    # Precompute each eraser segment's bounding box once so the per-stroke
    # prefilter below is a handful of comparisons.
    eraser_boxes = [
        ((ex0, ex1) if ex0 <= ex1 else (ex1, ex0),
         (ey0, ey1) if ey0 <= ey1 else (ey1, ey0))
        for (ex0, ey0, ex1, ey1) in erasers
    ]

    seg_intervals = []
    changed = False
    for i in range(n - 1):
        # Cheap per-segment prefilter: only stroke segments whose (padded)
        # bounding box overlaps an eraser segment can be clipped. Keeps
        # the cost proportional to the local crossing, not the stroke
        # length, so long strokes stay interactive.
        pad = (hws[i] if hws[i] >= hws[i + 1] else hws[i + 1]) + reach
        ax0, ay0 = pts[i]
        ax1, ay1 = pts[i + 1]
        sx0, sx1 = (ax0, ax1) if ax0 <= ax1 else (ax1, ax0)
        sy0, sy1 = (ay0, ay1) if ay0 <= ay1 else (ay1, ay0)
        near = [
            erasers[k]
            for k, ((bx0, bx1), (by0, by1)) in enumerate(eraser_boxes)
            if (bx0 - pad <= sx1 and bx1 + pad >= sx0
                and by0 - pad <= sy1 and by1 + pad >= sy0)
        ]
        if not near:
            seg_intervals.append(None)
            continue
        ivs = _segment_intervals(pts[i], pts[i + 1], hws[i], hws[i + 1],
                                 near, reach)
        seg_intervals.append(ivs)
        if ivs:
            changed = True
    if not changed:
        return (False, ())

    def _row(x, y, i, t):
        pressure = None
        if prs:
            pressure = prs[i] + (prs[i + 1] - prs[i]) * t
        return (x, y, pressure)

    fragments = []
    current = []
    for i in range(n - 1):
        p0, p1 = pts[i], pts[i + 1]
        ivs = seg_intervals[i]
        if not ivs:
            # Fast path: the segment is wholly kept. Append its endpoint
            # (or start the fragment) without building cut intervals.
            if current:
                if prs:
                    current.append((p1[0], p1[1], prs[i + 1]))
                else:
                    current.append((p1[0], p1[1], None))
            elif prs:
                current = [(p0[0], p0[1], prs[i]), (p1[0], p1[1], prs[i + 1])]
            else:
                current = [(p0[0], p0[1], None), (p1[0], p1[1], None)]
            continue
        cuts = [0.0, 1.0]
        for a, b in ivs:
            cuts.append(a)
            cuts.append(b)
        cuts = sorted(set(round(c, 12) for c in cuts))
        for k in range(len(cuts) - 1):
            u, v = cuts[k], cuts[k + 1]
            if v - u <= 0.0:
                continue
            mid = (u + v) / 2.0
            erased = _in_intervals(mid, ivs)
            x0 = p0[0] + (p1[0] - p0[0]) * u
            y0 = p0[1] + (p1[1] - p0[1]) * u
            x1 = p0[0] + (p1[0] - p0[0]) * v
            y1 = p0[1] + (p1[1] - p0[1]) * v
            if erased:
                if current:
                    fragments.append(current)
                    current = []
            else:
                if current:
                    current.append(_row(x1, y1, i, v))
                else:
                    current = [_row(x0, y0, i, u), _row(x1, y1, i, v)]
    if current:
        fragments.append(current)

    out = []
    for rows in fragments:
        clean = []
        for row in rows:
            if clean and abs(row[0] - clean[-1][0]) <= _MIN_FRAGMENT_LENGTH \
                    and abs(row[1] - clean[-1][1]) <= _MIN_FRAGMENT_LENGTH:
                continue
            clean.append(row)
        if len(clean) < 2:
            continue
        length = 0.0
        for j in range(len(clean) - 1):
            length += math.hypot(clean[j + 1][0] - clean[j][0],
                                 clean[j + 1][1] - clean[j][1])
        if length <= _MIN_FRAGMENT_LENGTH:
            continue
        frag_points = tuple((row[0], row[1]) for row in clean)
        frag_pressures = tuple(row[2] for row in clean) if prs else ()
        out.append(Fragment(points=frag_points, pressures=frag_pressures))

    # Guard against an interval so thin the result equals the original
    # (no representable change): keep the original untouched.
    if len(out) == 1 and len(out[0].points) == n:
        same = all(
            abs(out[0].points[j][0] - pts[j][0]) <= _MIN_FRAGMENT_LENGTH
            and abs(out[0].points[j][1] - pts[j][1]) <= _MIN_FRAGMENT_LENGTH
            for j in range(n))
        if same:
            return (False, ())

    return (True, tuple(out))
