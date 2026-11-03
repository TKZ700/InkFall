"""Render silhouette for pressure ink (visual path only).

This module implements the *rendering* half of the researched ink engine
(``handoff/IMPLEMENT.md``) on top of InkFall's existing data model. It
never changes what is stored: strokes stay ``points`` + ``pressures``
(and the PDF stays a centreline ``/InkList``). Only the painted
silhouette is derived here.

The engine in three points (all from the research notes):

1. **``w`` is a radius.** Every sample expands to an end circle of
   radius ``w = width / 2``.
2. **Each span is the exact tangent trapezoid** between two of those
   circles. For a unit edge normal ``n`` the tangency condition is
   ``(B + w1 n - A - w0 n) . n = 0``, i.e. ``u . n = (w0 - w1) / L``,
   hence ``n = k u +/- sqrt(1 - k^2) u_perp`` with ``k = (w0 - w1) / L``.
   No approximation: both edge points sit at perpendicular distance
   exactly ``w0`` / ``w1`` from the centreline.
3. **The silhouette is the union** of those spans plus one disc per
   sample that needs it. The painter fills every ring as sub-paths of
   one path with the NonZero rule (Qt's ``WindingFill``), so overlapping
   spans merge into a single silhouette and a translucent stroke is
   blended exactly once — never twice — at the overlaps.

Why the discs: two adjacent tangent trapezoids only meet exactly when
the centreline is straight. Around a turn of angle ``theta`` the outer
rail of one span and the inner rail of the next leave a wedge of about
``w * theta^2 / 8``; at a sharp corner that wedge is a visible notch or
a "bevel flat". A filled disc of radius ``w`` at the joint closes it and
turns every join into a round one, which is what a real nib does.

Two deliberate departures from the recovered native engine, both from
the research notes:

* A span whose width change outgrows its own length (``(dw)^2 >= L^2``,
  a pressure spike on a fast flick) is **kept** with ``k`` clamped just
  inside +/-1 instead of being dropped. The native engine drops it and
  leaves a gap in the ink; the clamped cone is degenerate-but-closed and
  the joint discs cover it, so the ribbon never breaks.
* Corner preservation (``40 deg``) and the width conditioning are
  modelling choices; the constants come from the recovered engine, the
  exact branch structure is ours.

Conditioning order (research section 6) matters: map pressure to width
first, smooth the *width* channel (it is the noisy one), preserve hard
corners, then resample by arc length so the per-span kernel sees an even
density. Never smooth pressure and re-map it.

Qt-free, pure and deterministic: same input, same output.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from core.types import Point

__all__ = [
    "RESAMPLE_STEP_PT",
    "MERGE_EPS_PT",
    "CORNER_MAX_ANGLE_DEG",
    "SMOOTH_PASSES",
    "JOIN_GAP_PT",
    "JOIN_SLOPE",
    "CAP_SEGMENTS",
    "DISC_SEGMENTS",
    "Silhouette",
    "LiveSilhouette",
    "silhouette",
    "stroke_silhouette",
]

#: Maximum centreline sample spacing handed to the span kernel, in input
#: units (page pt). The kernel is exact per span, so facets come from
#: input density alone; ~2 pt is the research's "~2 px" and stays
#: sub-pixel on curves at normal zoom while keeping the ring count (and
#: the fill cost) sane.
RESAMPLE_STEP_PT = 2.0

#: Samples closer than this are merged (the survivor keeps the larger
#: width, so a pressure peak is never lost). Below feature size, above
#: tablet jitter.
MERGE_EPS_PT = 0.25

#: Turn angle above which a vertex is a *hard corner* (research section
#: 8: ``dot(t_in, t_out) < cos(40 deg)``). Hard corners are pinned: the
#: smoothing pass never cuts them, so a deliberate corner stays crisp.
CORNER_MAX_ANGLE_DEG = 40.0

#: Chaikin corner-cutting passes inside a corner-free run.
SMOOTH_PASSES = 2

#: A joint gets a filler disc when the wedge between its two spans would
#: exceed this distance (input units). Sub-pixel at 100%, still sub-pixel
#: at 400%.
JOIN_GAP_PT = 0.02

#: ... or when the width slope at the joint exceeds this (``|dw| / L``),
#: which tilts the rails by more than ~8.5 degrees.
JOIN_SLOPE = 0.15

#: Points per pi of a round cap (research section 7 asks for >= 24; the
#: chord sagitta is then ~0.9% of ``w``, i.e. sub-pixel on a 10 pt nib).
CAP_SEGMENTS = 24

#: Points per full turn of an interior joint disc. The disc is only ever
#: a join filler (the spans carry the long edges), so it can be coarser
#: than a cap.
DISC_SEGMENTS = 12

#: ``k`` is clamped just inside +/-1: at |k| = 1 the two end circles no
#: longer have a common external tangent and the native engine drops the
#: span. Clamping keeps the ribbon closed instead.
_K_LIMIT = 0.999


@dataclass(frozen=True, slots=True)
class Silhouette:
    """Painted silhouette of one stroke, in the input coordinate frame.

    ``rings`` are the sub-paths of one NonZero-filled union, in paint
    order (all wound the same way, so overlaps merge instead of
    cancelling). ``polygon`` is their concatenation: it is not a valid
    ring, but every consumer that only needs the vertex cloud (extents,
    culling, dirty rects) keeps working unchanged. ``dots`` is the
    single-sample fallback.
    """

    rings: tuple[tuple[Point, ...], ...] = ()
    dots: tuple = ()

    @property
    def polygon(self) -> tuple[Point, ...]:
        """All ring vertices as ``Point``\ s (extents/compat view).

        This is a vertex cloud, not a fillable ring: the paint path uses
        ``rings`` (one sub-path each) so the union stays a union. Read
        it for bounds, culling and dirty rects only.
        """
        if not self.rings:
            return ()
        out: list[Point] = []
        for ring in self.rings:
            for x, y in ring:
                out.append(Point(x, y))
        return tuple(out)

    @property
    def discs(self) -> tuple:
        """No separate joints: they are rings of the union."""
        return ()


def _parse_point(value) -> tuple[float, float]:
    if isinstance(value, Point):
        raw = (value.x, value.y)
    else:
        try:
            if len(value) != 2:
                raise ValueError(f"point must be a 2D pair, got {value!r}")
            raw = (value[0], value[1])
        except TypeError:
            raise ValueError(f"point must be a 2D pair, got {value!r}")
        except (IndexError, KeyError):
            raise ValueError(f"point must be a 2D pair, got {value!r}")
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        raise ValueError(f"point coordinates must be numeric, got {value!r}")
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"point coordinates must be finite, got {value!r}")
    return x, y


def _parse_width(value) -> float:
    try:
        width = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"width must be numeric, got {value!r}")
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError(f"width must be finite and positive, got {value!r}")
    return width


# -- conditioning (research section 6) -------------------------------------

def _merge_duplicates(pts, widths):
    """Drop near-duplicate samples, keeping the larger width at a spot.

    The survivor takes the peak width of the run it stands for, so a
    press-and-hold blob that never moves still shows its heaviest
    sample (same rule as ``build_outline``'s dedup).
    """
    out_p = [pts[0]]
    out_w = [widths[0]]
    for i in range(1, len(pts)):
        x, y = pts[i]
        px, py = out_p[-1]
        if math.hypot(x - px, y - py) < MERGE_EPS_PT:
            if widths[i] > out_w[-1]:
                out_w[-1] = widths[i]
            continue
        out_p.append((x, y))
        out_w.append(widths[i])
    return out_p, out_w


def _smooth_widths(widths):
    """One 3-tap (1, 2, 1) pass over the *width* channel.

    Pressure is the noisy channel (research section 6.3); the centreline
    is smoothed separately. Endpoints are pinned so a stroke keeps its
    pressed-down start and lifted end.
    """
    if len(widths) < 3:
        return list(widths)
    out = [widths[0]]
    for i in range(1, len(widths) - 1):
        out.append(0.25 * widths[i - 1] + 0.5 * widths[i]
                   + 0.25 * widths[i + 1])
    out.append(widths[-1])
    return out


def _turn_dot(ax, ay, bx, by, cx, cy) -> float:
    d1x, d1y = bx - ax, by - ay
    d2x, d2y = cx - bx, cy - by
    n1 = math.hypot(d1x, d1y)
    n2 = math.hypot(d2x, d2y)
    if n1 <= 0.0 or n2 <= 0.0:
        return 1.0
    return (d1x * d2x + d1y * d2y) / (n1 * n2)


def _corner_indices(pts):
    """Indices of hard corners (turn sharper than the 40 deg threshold)."""
    cos_limit = math.cos(CORNER_MAX_ANGLE_DEG * math.pi / 180.0)
    found = set()
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        cx, cy = pts[i + 1]
        if _turn_dot(ax, ay, bx, by, cx, cy) < cos_limit:
            found.add(i)
    return found


def _chaikin_run(pts, widths, passes):
    """Endpoint-pinned Chaikin on one corner-free run."""
    for _ in range(passes):
        if len(pts) < 3:
            break
        new_p = [pts[0]]
        new_w = [widths[0]]
        for i in range(len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            wa, wb = widths[i], widths[i + 1]
            new_p.append((ax + (bx - ax) * 0.25, ay + (by - ay) * 0.25))
            new_w.append(wa + (wb - wa) * 0.25)
            new_p.append((ax + (bx - ax) * 0.75, ay + (by - ay) * 0.75))
            new_w.append(wa + (wb - wa) * 0.75)
        new_p.append(pts[-1])
        new_w.append(widths[-1])
        pts, widths = new_p, new_w
    return pts, widths


def _resample_run(pts, widths, step):
    """Even arc-length sampling; endpoints kept, corners never dropped.

    The interpolation is linear *along the smoothed run*, so no new
    deviation is introduced; widths ride the same parameter. Runs are
    short (between hard corners), so this stays linear in the input.
    """
    if len(pts) < 2:
        return list(pts), list(widths)
    out_p = [pts[0]]
    out_w = [widths[0]]
    acc = 0.0
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        seg = math.hypot(bx - ax, by - ay)
        ca, ca_w = ax, widths[i]
        wa, wb = widths[i], widths[i + 1]
        if seg <= 0.0:
            continue
        consumed = 0.0
        while acc + (seg - consumed) >= step:
            need = step - acc
            consumed += need
            t = consumed / seg
            out_p.append((ax + (bx - ax) * t, ay + (by - ay) * t))
            out_w.append(wa + (wb - wa) * t)
            acc = 0.0
        acc += seg - consumed
    if out_p[-1] != pts[-1]:
        out_p.append(pts[-1])
        out_w.append(widths[-1])
    return out_p, out_w


def _condition(pts, widths, *, smooth_passes: int,
               resample_step: float):
    """Merge, smooth the width channel, preserve corners, resample.

    Returns ``(points, widths)`` aligned one-to-one.
    """
    pts, widths = _merge_duplicates(pts, widths)
    widths = _smooth_widths(widths)
    if len(pts) < 3:
        return pts, widths
    corners = _corner_indices(pts)
    bounds = [0]
    for i in sorted(corners):
        bounds.append(i)
    if bounds[-1] != len(pts) - 1:
        bounds.append(len(pts) - 1)
    out_p = [pts[0]]
    out_w = [widths[0]]
    for k in range(len(bounds) - 1):
        a = bounds[k]
        b = bounds[k + 1]
        run_p = list(pts[a:b + 1])
        run_w = list(widths[a:b + 1])
        if smooth_passes:
            run_p, run_w = _chaikin_run(run_p, run_w, smooth_passes)
        if resample_step > 0.0:
            run_p, run_w = _resample_run(run_p, run_w, resample_step)
        out_p.extend(run_p[1:])
        out_w.extend(run_w[1:])
    return out_p, out_w


# -- the kernel (research sections 3-5) ------------------------------------

def _span_quad(ax, ay, wa, bx, by, wb):
    """Exact common-external-tangent trapezoid of two end circles.

    Returns ``(p_left_a, p_left_b, p_right_a, p_right_b)`` or ``None``
    for a zero-length span.
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


def _disc(cx, cy, r, segments=DISC_SEGMENTS):
    """Closed ring approximating a disc of radius ``r``."""
    if segments < 3:
        segments = 3
    step = 2.0 * math.pi / segments
    return tuple(
        (cx + r * math.cos(step * i), cy + r * math.sin(step * i))
        for i in range(segments))


def _cap_arc(cx, cy, r, rail_a, rail_b, outward, segments):
    """Round cap: the half disc between the two rail vertices.

    Both rail vertices sit exactly ``r`` from the sample centre (that is
    what the tangency formula guarantees), so the arc between them
    around the outward tangent is a closed sector — a circular cap, never
    a pinched or flat one.
    """
    a0 = math.atan2(rail_a[1] - cy, rail_a[0] - cx)
    a1 = math.atan2(rail_b[1] - cy, rail_b[0] - cx)
    ccw = (a1 - a0) % (2.0 * math.pi)
    through = (outward - a0) % (2.0 * math.pi)
    sweep = ccw if through <= ccw else ccw - 2.0 * math.pi
    count = max(2, int(round(segments * abs(sweep) / math.pi)))
    return tuple(
        (cx + r * math.cos(a0 + sweep * i / count),
         cy + r * math.sin(a0 + sweep * i / count))
        for i in range(1, count))


def _signed_area(ring) -> float:
    total = 0.0
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        total += ax * by - bx * ay
    return total * 0.5


def _normalized(ring):
    """Wind every ring the same way so NonZero unions instead of cancels."""
    if len(ring) < 3:
        return tuple(ring)
    if _signed_area(ring) < 0.0:
        return tuple(reversed(ring))
    return tuple(ring)


def _joint_gap(ax, ay, bx, by, cx, cy, h) -> float:
    """Gap left between two adjacent trapezoids at ``b``.

    Consecutive spans turn by ``theta``; their rails at the shared sample
    are ``2 h sin(theta / 2)`` apart, so the outer side of the turn
    leaves a wedge of that width (the inner side merely overlaps).
    """
    dot = _turn_dot(ax, ay, bx, by, cx, cy)
    angle = math.acos(max(-1.0, min(1.0, dot)))
    return 2.0 * h * math.sin(angle * 0.5)


def silhouette(points: Sequence, widths: Sequence, *,
               cap: str = "round",
               cap_segments: int = CAP_SEGMENTS,
               resample_step: float = RESAMPLE_STEP_PT,
               smooth_passes: int = SMOOTH_PASSES) -> Silhouette:
    """Build the painted silhouette from centreline samples + full widths.

    ``widths`` are full widths in the same units as ``points``; the
    engine works in radii internally. ``cap`` is ``"round"`` (the end
    discs of the union) or ``"butt"`` (raw kernel cuts).

    Raises ``ValueError`` on empty input, length mismatch, or
    non-finite/non-positive values (same contract as
    ``ink_geometry.build_outline``).
    """
    if cap not in ("round", "butt"):
        raise ValueError(f"cap must be 'round' or 'butt', got {cap!r}")
    if points is None or widths is None:
        raise ValueError("points and widths must be sequences, got None")
    n_pts = len(points)
    n_widths = len(widths)
    if n_pts == 0:
        raise ValueError("points must not be empty")
    if n_pts != n_widths:
        raise ValueError(f"points length {n_pts} != widths length {n_widths}")
    pts = [_parse_point(p) for p in points]
    full = [_parse_width(w) for w in widths]

    try:
        step = float(resample_step)
    except (TypeError, ValueError):
        step = RESAMPLE_STEP_PT
    if not math.isfinite(step) or step <= 0.0:
        step = 0.0
    try:
        passes = int(smooth_passes)
    except (TypeError, ValueError):
        passes = 0
    if passes < 0:
        passes = 0

    pts, full = _condition(pts, full, smooth_passes=passes,
                           resample_step=step)
    if len(pts) == 1:
        from annotations.ink_geometry import OutlineDot
        x, y = pts[0]
        return Silhouette(dots=(OutlineDot(center=Point(x, y),
                                           radius=full[0] / 2.0),))

    halves = [w / 2.0 for w in full]
    rings: list = []
    for i in range(len(pts) - 1):
        quad = _span_quad(pts[i][0], pts[i][1], halves[i],
                          pts[i + 1][0], pts[i + 1][1], halves[i + 1])
        if quad is None:
            continue
        la, lb, ra, rb = quad
        rings.append(_normalized((la, lb, rb, ra)))

    # Joint fillers: only where a wedge or a rail fan would actually show.
    last = len(pts) - 1
    for i in range(1, last):
        x, y = pts[i]
        h = halves[i]
        if h <= 0.0:
            continue
        prev_len = math.hypot(x - pts[i - 1][0], y - pts[i - 1][1])
        next_len = math.hypot(pts[i + 1][0] - x, pts[i + 1][1] - y)
        gap = _joint_gap(pts[i - 1][0], pts[i - 1][1], x, y,
                         pts[i + 1][0], pts[i + 1][1], h)
        slope = 0.0
        if prev_len > 0.0:
            slope = max(slope, abs(halves[i] - halves[i - 1]) / prev_len)
        if next_len > 0.0:
            slope = max(slope, abs(halves[i + 1] - halves[i]) / next_len)
        if gap > JOIN_GAP_PT or slope > JOIN_SLOPE:
            rings.append(_normalized(_disc(x, y, h)))

    if cap == "round":
        # End caps: the sector between the end span's two rail vertices.
        ex, ey = pts[-1]
        last_quad = None
        for i in range(last - 1, -1, -1):
            last_quad = _span_quad(pts[i][0], pts[i][1], halves[i],
                                   ex, ey, halves[last])
            if last_quad is not None:
                break
        if last_quad is not None:
            la, _lb, ra, _rb = last_quad
            along = math.atan2(ey - pts[0][1], ex - pts[0][0])
            for i in range(last - 1, -1, -1):
                dx = ex - pts[i][0]
                dy = ey - pts[i][1]
                if math.hypot(dx, dy) > 0.0:
                    along = math.atan2(dy, dx)
                    break
            arc = _cap_arc(ex, ey, halves[last], la, ra, along,
                           cap_segments)
            rings.append(_normalized((la,) + arc + (ra,)))
        sx, sy = pts[0]
        first_quad = _span_quad(sx, sy, halves[0], pts[1][0], pts[1][1],
                                halves[1])
        if first_quad is not None:
            la, _lb, ra, _rb = first_quad
            along = math.atan2(sy - pts[1][1], sx - pts[1][0])
            arc = _cap_arc(sx, sy, halves[0], ra, la, along, cap_segments)
            rings.append(_normalized((ra,) + arc + (la,)))

    return Silhouette(rings=tuple(rings))


def stroke_silhouette(points: Sequence, base_width: float,
                      pressures: Sequence | None = None,
                      profile: str = "normal", *,
                      cap: str = "round") -> Silhouette:
    """Silhouette for a raw stroke: pressure map, then geometry.

    ``base_width`` is the full nib width in input units (page pt).
    ``pressures`` is one sample per point; ``None`` (or a mismatched
    length) renders a constant-width stroke. Profile names resolve
    through ``ink_geometry.PRESSURE_PROFILES`` and raise ``ValueError``
    for unknown names, exactly like ``pressure_widths``.
    """
    from annotations.ink_geometry import pressure_widths

    if points is None:
        raise ValueError("points must be a sequence, got None")
    n = len(points)
    if n == 0:
        raise ValueError("points must not be empty")
    if pressures is not None and len(pressures) == n:
        widths = pressure_widths(pressures, base_width, profile)
    else:
        base = float(base_width)
        widths = tuple(base for _ in range(n))
    return silhouette(points, widths, cap=cap)


class LiveSilhouette:
    """Incremental silhouette for the stroke currently under the pen.

    Same public surface as ``ink_geometry.LiveOutline`` (``append`` /
    ``reset`` / ``outline`` / ``len``) so the pen handler can swap it in
    unchanged. ``outline()`` returns the same object while the sample
    stream has not changed, and a fresh build otherwise; a live stroke
    is rebuilt at most once per paint, never per input event.

    Raw samples are copied, never retained by reference and never
    mutated. Qt-free.
    """

    def __init__(self, *, cap: str = "round",
                 cap_segments: int = CAP_SEGMENTS) -> None:
        if cap not in ("round", "butt"):
            raise ValueError(f"cap must be 'round' or 'butt', got {cap!r}")
        self._cap = cap
        self._cap_segments = cap_segments
        self._raw: list[tuple[float, float, float]] = []
        self._cached = None
        self._key = None
        self.fallbacks = 0

    def reset(self) -> None:
        """Forget all samples (stroke boundary)."""
        self._raw = []
        self._cached = None
        self._key = None

    def __len__(self) -> int:
        """Number of raw samples fed."""
        return len(self._raw)

    def append(self, x: float, y: float, width: float) -> None:
        """Feed one raw sample (full width); raises on bad input."""
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(f"point must be numeric, got {(x, y)!r}")
        if not (math.isfinite(fx) and math.isfinite(fy)):
            raise ValueError(f"point must be finite, got {(x, y)!r}")
        fw = _parse_width(width)
        self._raw.append((fx, fy, fw))

    def outline(self) -> Silhouette:
        """Current silhouette; rebuilds only when the samples changed."""
        if not self._raw:
            raise ValueError("no samples appended")
        key = (len(self._raw), self._raw[-1])
        if key == self._key and self._cached is not None:
            return self._cached
        pts = [(x, y) for x, y, _w in self._raw]
        widths = [w for _x, _y, w in self._raw]
        result = silhouette(pts, widths, cap=self._cap,
                            cap_segments=self._cap_segments)
        self._cached = result
        self._key = key
        return result
