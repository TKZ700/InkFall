"""Pressure-aware ink geometry (M5-B.2.1). Qt-free pure utilities.

Slice 1: pressure-to-width mapping only. No outline generation, no
smoothing, no resampling, no timestamps, no velocity — those land in
later slices. Nothing here may import Qt (see
``tests/test_import_boundaries.py``); painting lives in ``app/viewer.py``.

Slice 2: ``build_outline`` converts centerline samples + full widths
into a filled outline (``StrokeOutline``). Still pure and Qt-free;
still no smoothing, resampling, velocity, or timestamp use.

Mapping formula (B.2.1 audit recommendation, conservative defaults)::

    width = base_width * (floor + (1 - floor) * clamp(p) ** exponent)

with ``(floor, exponent)`` from ``PRESSURE_PROFILES[profile]``
(B.2.3; retuned B.2.4 — ``"normal"`` is now the linear ``(0.2, 1.0)``
candidate, no longer the original ``(0.3, 0.8)`` curve).

Properties, all covered by ``tests/test_ink_geometry_b21.py``:

* Units: output carries the input ``base_width`` units (mm in InkFall).
* ``p = 0`` yields exactly ``base_width * floor`` (nonzero, so
  light touches stay visible); ``p = 1`` yields exactly ``base_width``.
* Widths never leave ``[base_width * floor, base_width]``.
* Monotonically nondecreasing in pressure.
* Missing data (``None`` pressure, empty/``None`` sequence) falls back to
  full ``base_width`` rendering: strokes without pressure data keep the
  legacy appearance. Note ``pressure_widths`` maps present samples only,
  so an empty/``None`` sequence yields an empty tuple and the caller
  keeps full-``base_width`` rendering.
* Defensive, never raises on pressure input: non-finite (NaN, inf) and
  non-numeric pressures map to full ``base_width`` ("never lose ink",
  same policy as ``PenHandler._clamp_pressure``); out-of-range values
  are clamped to ``[0, 1]``.
* Invalid ``base_width`` (non-positive, non-finite, non-numeric) raises
  ``ValueError`` — a caller bug, not device noise, so it must surface.
* Pure and deterministic: no timestamps, no velocity, no randomness,
  inputs are never mutated.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from core.types import Point

__all__ = [
    "MIN_WIDTH_RATIO",
    "RESPONSE_EXPONENT",
    "SMOOTH_MIN_SPACING_PT",
    "SMOOTH_PASSES",
    "SMOOTH_MAX_CUT_PT",
    "PRESSURE_PROFILES",
    "DEFAULT_PRESSURE_PROFILE",
    "REVERSAL_DOT",
    "HIGHLIGHTER_BAND_ASPECT",
    "OutlineDot",
    "StrokeOutline",
    "LiveOutline",
    "LiveCenterline",
    "build_outline",
    "highlighter_band_hulls",
    "pressure_width",
    "pressure_widths",
    "smooth_centerline",
]

#: Light-pressure floor of the original B.2.1 curve, retained for
#: compatibility (no current profile uses it since the B.2.4 retune).
MIN_WIDTH_RATIO = 0.3

#: Sub-linear response exponent of the original B.2.1 curve, retained
#: for compatibility (no current profile uses it since the B.2.4
#: retune). Must stay positive to preserve monotonicity.
RESPONSE_EXPONENT = 0.8

#: Named pressure-response profiles (B.2.4): ``(minimum-width ratio,
#: response exponent)`` per profile. ``normal`` is the B.2.4 linear
#: candidate ``(0.20, 1.00)`` — width = 20% + 80% x pressure — chosen
#: for a ~2.4x working-band response (was ~1.8x under the B.2.1 curve).
#: ``soft`` answers light pressure sooner; ``hard`` shifts thickening
#: toward firm pressure. Ordering soft >= normal >= hard holds for
#: every pressure in [0, 1] — hence ``hard`` shares ``normal``'s 0.20
#: floor with a steeper exponent instead of a higher floor; each curve
#: is monotonic with values in [floor, 1.0] times the base width.
PRESSURE_PROFILES: dict[str, tuple[float, float]] = {
    "soft": (0.3, 0.4),
    "normal": (0.2, 1.0),
    "hard": (0.2, 1.8),
}

#: Fallback profile for unknown persisted/forwarded values.
DEFAULT_PRESSURE_PROFILE = "normal"


def _profile_params(profile: str) -> tuple[float, float]:
    """Resolve a profile name to ``(floor, exponent)`` or raise."""
    try:
        params = PRESSURE_PROFILES[profile]
    except (KeyError, TypeError):
        raise ValueError(
            f"unknown pressure profile {profile!r}; "
            f"expected one of {sorted(PRESSURE_PROFILES)}")
    return params


def _check_base_width(base_width: float) -> float:
    """Validate the configured width; return it as float or raise."""
    try:
        value = float(base_width)
    except (TypeError, ValueError):
        raise ValueError(f"base_width must be numeric, got {base_width!r}")
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"base_width must be finite and positive, got {value!r}")
    return value


def _clamp_pressure(pressure) -> float:
    """Normalize one sample into ``[0, 1]``; missing/garbage → ``1.0``."""
    try:
        value = float(pressure)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 1.0 if value != value or value > 0 else 0.0
    return max(0.0, min(1.0, value))


def pressure_width(pressure, base_width: float,
                     profile: str = DEFAULT_PRESSURE_PROFILE) -> float:
    """Map one normalized pressure sample to a width in base_width units.

    ``pressure=None`` (missing data) yields full ``base_width``.
    ``profile`` selects a ``PRESSURE_PROFILES`` entry;
    unknown profiles raise ``ValueError``.
    """
    base = _check_base_width(base_width)
    floor, exponent = _profile_params(profile)
    clamped = _clamp_pressure(pressure)
    width = base * (floor + (1.0 - floor) * clamped ** exponent)
    low = base * floor
    return max(low, min(base, width))


def pressure_widths(
    pressures: Sequence | None, base_width: float,
    profile: str = DEFAULT_PRESSURE_PROFILE,
) -> tuple[float, ...]:
    """Map each sample to a width; one output per input, order preserved.

    ``None`` or empty input yields ``()`` — the caller then renders the
    stroke at full ``base_width`` (legacy appearance). The input sequence
    is only read, never mutated.
    """
    base = _check_base_width(base_width)
    _profile_params(profile)
    if not pressures:
        return ()
    return tuple(pressure_width(p, base, profile) for p in pressures)


# -- Slice 2: variable-width outline builder ------------------------------

#: Consecutive samples closer than this are duplicates (same epsilon as
#: ``PenHandler._same`` in ``annotations/interaction.py``).
DEDUP_EPS = 1e-9

#: Supported open-stroke cap policies for ``build_outline``.
CAP_POLICIES: tuple[str, ...] = ("round", "butt")

#: Interior joints sharper than this (segment-direction dot product)
#: count as turnarounds: offset rails fold there, so a filler disc is
#: emitted to keep ink continuous under winding fill. cos(135°) ≈
#: -0.7; ordinary corners and curves never reach it.
REVERSAL_DOT = -0.7


@dataclass(frozen=True, slots=True)
class OutlineDot:
    """Filled-disc fallback for a dot stroke (single/collapsed sample)."""

    center: Point
    radius: float  #: half of the sample full width, input units.


@dataclass(frozen=True, slots=True)
class StrokeOutline:
    """Filled outline of one stroke in the input coordinate frame.

    ``polygon`` is an implicitly closed loop (last vertex connects back
    to the first): left rail forward, end cap, right rail backward,
    start cap. Empty when the stroke collapsed to ``dots``. ``dots``
    is the single-point fallback (never combined with ``polygon``).
    ``discs`` are joint fillers accompanying ``polygon``: one per
    near-reversal joint, painted as filled discs so folded rails can
    never leave a fill-rule hole. Empty for ordinary strokes.
    """

    polygon: tuple[Point, ...] = ()
    dots: tuple[OutlineDot, ...] = ()
    discs: tuple[OutlineDot, ...] = ()


def _parse_vertex(value) -> tuple[float, float]:
    """Coerce one centerline sample to a finite ``(x, y)`` pair."""
    if isinstance(value, Point):
        raw = (value.x, value.y)
    else:
        try:
            if len(value) != 2:
                raise ValueError(
                    f"point must be a 2D pair, got {value!r}")
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
    return (x, y)


def _parse_half_width(value) -> float:
    """Coerce one full width to a finite positive half-width."""
    try:
        width = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"width must be numeric, got {value!r}")
    if not math.isfinite(width) or width <= 0.0:
        raise ValueError(f"width must be finite and positive, got {value!r}")
    return width * 0.5


def _unit_tangent(ax: float, ay: float, bx: float, by: float,
                  fallback: tuple[float, float] | None) -> tuple[float, float]:
    """Unit direction a→b; zero-length falls back (never NaN output)."""
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length > 0.0:
        return (dx / length, dy / length)
    if fallback is not None:
        return fallback
    return (1.0, 0.0)


def _dedup_consecutive(pts: list[tuple[float, float]],
                       halves: list[float]
                       ) -> tuple[list[tuple[float, float]], list[float]]:
    """Collapse consecutive near-duplicates, keeping peak half-widths."""
    kept_pts: list[tuple[float, float]] = []
    kept_h: list[float] = []
    for pt, h in zip(pts, halves):
        if kept_pts:
            px, py = kept_pts[-1]
            if math.hypot(pt[0] - px, pt[1] - py) < DEDUP_EPS:
                if h > kept_h[-1]:
                    kept_h[-1] = h
                continue
        kept_pts.append(pt)
        kept_h.append(h)
    return kept_pts, kept_h


def _joint_reversal(ax: float, ay: float, bx: float, by: float,
                    cx: float, cy: float) -> bool:
    """True when the joint at b reverses past REVERSAL_DOT."""
    d1x, d1y = bx - ax, by - ay
    d2x, d2y = cx - bx, cy - by
    n1 = math.hypot(d1x, d1y)
    n2 = math.hypot(d2x, d2y)
    if n1 <= 0.0 or n2 <= 0.0:
        return False
    return (d1x * d2x + d1y * d2y) / (n1 * n2) < REVERSAL_DOT


def _blend_pair(ax: float, ay: float, bx: float, by: float,
                va: float | None, vb: float | None, t: float,
                max_cut: float):
    """One Chaikin blend from a toward b (position + optional width).

    Shared by ``_chaikin`` and the incremental builder so both compute
    bit-identical blends.
    """
    seg = math.hypot(bx - ax, by - ay)
    if seg > 0.0:
        t = min(t, max_cut / seg)
    px, py = ax + (bx - ax) * t, ay + (by - ay) * t
    if va is None or vb is None:
        return (px, py), None
    return (px, py), va + (vb - va) * t


def _rails_and_caps(kept_pts: list[tuple[float, float]],
                    kept_h: list[float],
                    prev_pt: tuple[float, float] | None = None,
                    prev_tangent: tuple[float, float] | None = None,
                    ) -> tuple[list, list, list, list]:
    """Rails, tangents, and reversal discs for deduped samples.

    Returns ``(left, right, tangents, discs)`` with ``discs`` as
    ``(joint_index, OutlineDot)`` relative to ``kept_pts``. With
    ``prev_pt``/``prev_tangent`` unset this is the whole-stroke
    computation; set, the window's first tangent uses the carried
    context exactly as a full build would (for incremental rebuilds).
    """
    m = len(kept_pts)
    discs: list[tuple[int, OutlineDot]] = []
    # Joint 0 is evaluable only with a carried previous segment
    # (incremental windows); whole-stroke builds start at joint 1,
    # preserving exact legacy behavior.
    disc_first = 0 if prev_pt is not None else 1
    for i in range(disc_first, m - 1):
        if i == 0:
            assert prev_pt is not None
            ax, ay = prev_pt
        else:
            ax, ay = kept_pts[i - 1]
        bx, by = kept_pts[i]
        cx, cy = kept_pts[i + 1]
        if _joint_reversal(ax, ay, bx, by, cx, cy):
            discs.append((i, OutlineDot(center=Point(bx, by),
                                        radius=kept_h[i])))
    tangents: list[tuple[float, float]] = []
    for i in range(m):
        if i == 0 and prev_pt is None:
            t = _unit_tangent(kept_pts[0][0], kept_pts[0][1],
                              kept_pts[1][0], kept_pts[1][1], None)
        elif i == 0:
            assert prev_pt is not None
            t = _unit_tangent(prev_pt[0], prev_pt[1],
                              kept_pts[1][0], kept_pts[1][1],
                              prev_tangent)
        elif i == m - 1:
            t = _unit_tangent(kept_pts[m - 2][0], kept_pts[m - 2][1],
                              kept_pts[m - 1][0], kept_pts[m - 1][1],
                              tangents[-1])
        else:
            t = _unit_tangent(kept_pts[i - 1][0], kept_pts[i - 1][1],
                              kept_pts[i + 1][0], kept_pts[i + 1][1],
                              tangents[-1])
        tangents.append(t)

    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for (x, y), h, (tx, ty) in zip(kept_pts, kept_h, tangents):
        nx, ny = -ty, tx
        left.append((x + nx * h, y + ny * h))
        right.append((x - nx * h, y - ny * h))
    return left, right, tangents, discs


def _cap_arc(cx: float, cy: float, radius: float, start_angle: float,
             segments: int) -> list[tuple[float, float]]:
    """Semicircle interior points sweeping clockwise (see build_outline).

    Endpoints are excluded: the caller supplies the exact rail vertices.
    """
    pts = []
    for k in range(1, segments):
        angle = start_angle - math.pi * k / segments
        pts.append((cx + radius * math.cos(angle),
                    cy + radius * math.sin(angle)))
    return pts


def _end_cap_points(last_pt, last_tangent, last_h, cap, cap_segments):
    """Round end-cap interior arc (endpoints excluded); [] unless round.

    Shared by ``build_outline`` and the incremental builder so both
    compute bit-identical caps.
    """
    if cap != "round":
        return []
    ex, ey = last_pt
    etx, ety = last_tangent
    return _cap_arc(ex, ey, last_h, math.atan2(etx, -ety), cap_segments)


def _start_cap_points(first_pt, first_tangent, first_h, cap, cap_segments):
    """Round start-cap interior arc (endpoints excluded); [] unless round."""
    if cap != "round":
        return []
    sx, sy = first_pt
    stx, sty = first_tangent
    return _cap_arc(sx, sy, first_h, math.atan2(-stx, sty), cap_segments)


def build_outline(points: Sequence, widths: Sequence,
                  *, cap: str = "round",
                  cap_segments: int = 8) -> StrokeOutline:
    """Build a filled variable-width outline from centerline samples.

    Coordinate system: unit-agnostic — outputs are in the input frame
    (InkFall passes ``InkStroke.points`` page-local rotated PDF pt with
    widths in the same units). Centerline sample order is preserved
    along the rails. Inputs are only read, never mutated.

    Strategy (robustness over elaboration): offset rails are built
    through per-sample normals with *shared vertices* — no
    segment-intersection math exists, so miters cannot blow up, joins
    cannot gap, and output stays finite and bounded for any finite
    input.     Joins are effectively beveled (rails cut sharp corners);
    caps are ``"round"`` (semicircle, ``cap_segments`` interior points)
    or ``"butt"`` (flat chord). A stroke collapsing to one distinct
    sample becomes an ``OutlineDot`` with the peak sampled half-width.
    Interior joints sharper than ``REVERSAL_DOT`` additionally yield
    filler ``discs`` (same radius convention); rails are otherwise
    unchanged, so ordinary strokes are bit-identical with or without
    this handling.

    Raises ``ValueError`` on: ``None``/empty input, length mismatch,
    non-2D/non-numeric/non-finite points, non-positive/non-finite
    widths, unknown ``cap``, or ``cap_segments < 1``.
    """
    if cap not in CAP_POLICIES:
        raise ValueError(f"cap must be one of {CAP_POLICIES}, got {cap!r}")
    if not isinstance(cap_segments, int) or cap_segments < 1:
        raise ValueError(
            f"cap_segments must be a positive int, got {cap_segments!r}")
    if points is None or widths is None:
        raise ValueError("points and widths must be sequences, got None")
    try:
        n_points = len(points)
        n_widths = len(widths)
    except TypeError:
        raise ValueError("points and widths must be sized sequences")
    if n_points == 0:
        raise ValueError("points must not be empty")
    if n_points != n_widths:
        raise ValueError(
            f"points length {n_points} != widths length {n_widths}")

    parsed = [_parse_vertex(p) for p in points]
    halves = [_parse_half_width(w) for w in widths]

    # Deduplicate consecutive samples (paired with their half-widths);
    # the survivor keeps the peak width so a press-and-hold blob that
    # never moves still reflects its heaviest sample.
    kept_pts, kept_h = _dedup_consecutive(parsed, halves)

    if len(kept_pts) == 1:
        x, y = kept_pts[0]
        return StrokeOutline(
            dots=(OutlineDot(center=Point(x, y), radius=kept_h[0]),))

    m = len(kept_pts)
    # Turnaround joints: consecutive segment directions reversing past
    # REVERSAL_DOT fold the rails (winding fill still pinches there),
    # so each gets a filler disc at the joint with its half-width.
    # Local decision on kept samples only — appending samples can only
    # change the status of the last joints (proven by the incremental
    # equivalence tests).
    left, right, tangents, indexed_discs = _rails_and_caps(
        kept_pts, kept_h)
    discs = [dot for _idx, dot in indexed_discs]

    loop = list(left)
    # End cap: L_end (normal side) sweeping clockwise through the
    # forward tangent to R_end. Clockwise quarter-turns map n -> t
    # -> -n for n = perp(t), so start at angle(n) and sweep -180°.
    loop.extend(_end_cap_points(kept_pts[-1], tangents[-1], kept_h[-1],
                                cap, cap_segments))
    loop.extend(reversed(right))
    # Start cap: R_0 sweeping clockwise through the backward
    # tangent (-t) to L_0; start at angle(-n) and sweep -180°.
    loop.extend(_start_cap_points(kept_pts[0], tangents[0], kept_h[0],
                                  cap, cap_segments))

    return StrokeOutline(
        polygon=tuple(Point(x, y) for x, y in loop),
        discs=tuple(discs))


# -- B.2.2: geometry-only smoothing pass ---------------------------------

#: Minimum sample spacing for smoothing, in input units (doc pt in
#: InkFall: 1 pt renders as ``ui_zoom`` screen px, so 0.5 pt is subpixel
#: at zoom 1 — below feature size, above typical tablet jitter).
SMOOTH_MIN_SPACING_PT = 0.5

#: Chaikin corner-cutting passes (endpoints pinned). Two passes settle
#: sample-to-sample tablet jitter (sub-pixel noise on multi-px sample
#: spacing) while corner cuts stay O(spacing); measured on the B.2.2
#: jitter fixture (see tests/test_ink_smoothing_b22.py).
SMOOTH_PASSES = 2

#: Maximum displacement of any single Chaikin blend, in input units.
#: Caps corner rounding on long intentional arms (a 25% cut of a 10pt
#: arm would visibly blunt deliberate corners) while leaving jitter
#: smoothing untouched (typical cuts are well below this). Clamped
#: blends stay on their raw segment, so all boundedness guarantees
#: hold.
SMOOTH_MAX_CUT_PT = 1.0


def _resample(pts: list[tuple[float, float]],
              vals: list[float] | None,
              min_spacing: float
              ) -> tuple[list[tuple[float, float]], list[float] | None]:
    """Decimate by arc length: first/last kept, closer samples dropped.

    Decimate-only (never subdivides): output never exceeds input
    length, so downstream point counts stay bounded. Kept samples
    retain their exact positions and widths — no interpolation, so
    width alignment is trivially preserved.
    """
    kept_pts = [pts[0]]
    kept_vals = [vals[0]] if vals is not None else None
    last = len(pts) - 1
    for i in range(1, len(pts)):
        px, py = kept_pts[-1]
        qx, qy = pts[i]
        if i != last and math.hypot(qx - px, qy - py) < min_spacing:
            continue
        kept_pts.append((qx, qy))
        if vals is not None and kept_vals is not None:
            kept_vals.append(vals[i])
    return kept_pts, kept_vals


def _chaikin(pts: list[tuple[float, float]],
             vals: list[float] | None,
             max_cut: float = SMOOTH_MAX_CUT_PT,
             ) -> tuple[list[tuple[float, float]], list[float] | None]:
    """One endpoint-pinned Chaikin pass; widths ride the same blends.

    Each blend is capped at ``max_cut`` displacement so long
    intentional arms keep sharp tips; the clamp stays on-segment.
    """
    if len(pts) < 3:
        return pts, vals

    out_pts = [pts[0]]
    out_vals = [vals[0]] if vals is not None else None
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        va = vals[i] if vals is not None else None
        vb = vals[i + 1] if vals is not None else None
        (qx, qy), qw = _blend_pair(ax, ay, bx, by, va, vb, 0.25,
                                   max_cut)
        (rx, ry), rw = _blend_pair(bx, by, ax, ay, vb, va, 0.25,
                                   max_cut)
        out_pts.extend(((qx, qy), (rx, ry)))
        if vals is not None and out_vals is not None:
            out_vals.extend((qw, rw))
    out_pts.append(pts[-1])
    if vals is not None and out_vals is not None:
        out_vals.append(vals[-1])
    return out_pts, out_vals


def smooth_centerline(points: Sequence, widths: Sequence | None = None, *,
                      min_spacing: float = SMOOTH_MIN_SPACING_PT,
                      passes: int = SMOOTH_PASSES
                      ) -> tuple[tuple, tuple | None]:
    """Smooth a centerline for rendering; raw inputs never mutated.

    Resamples by arc length (``min_spacing`` input units) then applies
    ``passes`` endpoint-pinned Chaikin passes. Every output point is a
    convex combination of raw samples, so deviation from the raw path
    stays bounded and no loops or overshoot can appear. ``widths``
    (parallel full widths, e.g. from ``pressure_widths``) are carried
    alongside positions and stay aligned; pass ``None`` for uniform
    strokes and receive ``None`` back.

    Bypass (inputs returned as tuples, unchanged): fewer than 3
    samples, total length below ``min_spacing`` (dots and short
    strokes), or invalid ``min_spacing``/``passes`` (non-positive
    spacing or negative passes bypass smoothing rather than raising —
    rendering must never lose ink to a bad parameter).

    Raises ``ValueError`` on: ``None``/empty points, length mismatch
    with ``widths``, or non-finite coordinates/widths.
    """
    if points is None:
        raise ValueError("points must be a sequence, got None")
    try:
        n = len(points)
    except TypeError:
        raise ValueError("points must be a sized sequence")
    if n == 0:
        raise ValueError("points must not be empty")
    parsed = [_parse_vertex(p) for p in points]
    parsed_vals: list[float] | None = None
    if widths is not None:
        try:
            m = len(widths)
        except TypeError:
            raise ValueError("widths must be a sized sequence or None")
        if m != n:
            raise ValueError(
                f"points length {n} != widths length {m}")
        parsed_vals = []
        for w in widths:
            try:
                value = float(w)
            except (TypeError, ValueError):
                raise ValueError(f"width must be numeric, got {w!r}")
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"width must be finite and positive, got {w!r}")
            parsed_vals.append(value)
    try:
        spacing = float(min_spacing)
        n_passes = int(passes)
    except (TypeError, ValueError):
        return (tuple(parsed),
                tuple(parsed_vals) if parsed_vals is not None else None)
    if not math.isfinite(spacing) or spacing <= 0.0 or n_passes < 0:
        return (tuple(parsed),
                tuple(parsed_vals) if parsed_vals is not None else None)
    if n < 3:
        return (tuple(parsed),
                tuple(parsed_vals) if parsed_vals is not None else None)
    total = sum(math.hypot(parsed[i + 1][0] - parsed[i][0],
                           parsed[i + 1][1] - parsed[i][1])
                for i in range(n - 1))
    if total < spacing:
        return (tuple(parsed),
                tuple(parsed_vals) if parsed_vals is not None else None)
    work_pts, work_vals = _resample(parsed, parsed_vals, spacing)
    for _ in range(n_passes):
        if len(work_pts) < 3:
            break
        work_pts, work_vals = _chaikin(work_pts, work_vals)
    return (tuple(work_pts),
            tuple(work_vals) if work_vals is not None else None)


    work_pts, work_vals = _resample(parsed, parsed_vals, spacing)
    for _ in range(n_passes):
        if len(work_pts) < 3:
            break
        work_pts, work_vals = _chaikin(work_pts, work_vals)
    return (tuple(work_pts),
            tuple(work_vals) if work_vals is not None else None)


# -- B.2.3: append-only incremental outline builder -----------------------

class LiveOutline:
    """Incremental variable-width outline for one live stroke (B.2.3).

    Accepts raw ``(x, y, full-width)`` samples in order and returns a
    ``StrokeOutline`` matching ``build_outline(smooth_centerline(all))``
    exactly (proven by the equivalence tests across every split).
    Per-append cost is bounded by a constant-size tail recompute;
    only a full derive (small strokes, fallbacks) touches all samples.

    How exactness holds: decimation decisions are prefix-stable (drops
    never change state, so only the provisional final point can be
    retracted); Chaikin blends are per-segment deterministic (only the
    tail is recomputed, spliced by verified exact match); rail joints
    need ±1 neighbors (recomputed window with carried seed tangent);
    caps pin the stable start and rebuild the trivial end per call.
    Any splice that fails verification falls back to an exact full
    derive — output is never approximate, only occasionally slower.

    Raw inputs are copied, never retained by reference and never
    mutated. Qt-free. ``reset()`` starts a new stroke.
    """

    #: Overlap (kept samples) re-derived on each extension.
    _OVERLAP_KEPT = 20
    #: Smoothed tail window re-railed on each extension.
    _RAIL_WINDOW = 10
    #: Below this effective-kept length, derive everything directly.
    _SMALL_FULL = 32
    #: Backward-search cap for splices (true splice always lies in the
    #: tail; anything beyond falls back to an exact full derive).
    _SEARCH_CAP = 64
    #: Alignment run length for splice search (8 exact triples cannot
    #: coincide by chance; identical runs splice harmlessly — dedup
    #: normalizes them downstream).
    _SPLICE_RUN = 8
    #: Search window (smoothed outputs) for the splice alignment.
    _SPLICE_WINDOW = 200

    def __init__(self, *, min_spacing: float = SMOOTH_MIN_SPACING_PT,
                 passes: int = SMOOTH_PASSES,
                 cap: str = "round", cap_segments: int = 8) -> None:
        if cap not in CAP_POLICIES:
            raise ValueError(
                f"cap must be one of {CAP_POLICIES}, got {cap!r}")
        if not isinstance(cap_segments, int) or cap_segments < 1:
            raise ValueError(
                f"cap_segments must be a positive int, got {cap_segments!r}")
        self._min_spacing = min_spacing
        self._passes = passes
        self._cap = cap
        self._cap_segments = cap_segments
        self.reset()

    def reset(self) -> None:
        """Forget all samples (stroke boundary)."""
        self._raw: list[tuple[float, float, float]] = []
        self._total = 0.0
        self._kept: list[tuple[float, float, float]] = []
        self._pending: tuple[float, float, float] | None = None
        self._S: list[tuple[float, float, float]] = []
        self._D: list[tuple[float, float, float]] = []
        self._T: list[tuple[float, float]] = []
        self._L: list[tuple[float, float]] = []
        self._R: list[tuple[float, float]] = []
        self._discs: list[tuple[int, OutlineDot]] = []
        self._start_cap: list[tuple[float, float]] | None = None
        self._derived_key = None
        self._cached = None
        self._dirty = True
        self.fallbacks = 0
        # B.2.8: cached assembly prefixes for outline(). _head_L covers
        # the first _head_len rail entries in polygon order,
        # _head_R_rev covers the same entries in reverse (polygon)
        # order, _head_discs the leading (index, dot) pairs. Valid only
        # while the covered _L/_R/_discs prefixes are verbatim
        # unchanged — maintained by _truncate_heads/_drop_heads on every
        # rails mutation, so outline() rebuilds just the tail window.
        self._head_L: tuple = ()
        self._head_R_rev: tuple = ()
        self._head_discs: tuple = ()
        self._head_len = 0

    def __len__(self) -> int:
        """Number of raw samples fed (not smoothed outputs)."""
        return len(self._raw)

    def _count_fallback(self) -> None:
        # Steady-state misses only: startup derivations (tiny lists)
        # always take the exact full path by design and must not pollute
        # the fast-path statistic.
        if len(self._raw) > 64:
            self.fallbacks += 1

    def _params_ok(self) -> bool:
        try:
            spacing = float(self._min_spacing)
            n_passes = int(self._passes)
        except (TypeError, ValueError):
            return False
        return (math.isfinite(spacing) and spacing > 0.0
                and n_passes >= 0)

    def _active(self) -> bool:
        return (self._params_ok() and len(self._raw) >= 3
                and self._total >= float(self._min_spacing))

    def append(self, x: float, y: float, width: float) -> None:
        """Feed one raw sample; raises ``ValueError`` on bad input."""
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(f"point must be numeric, got {(x, y)!r}")
        if not (math.isfinite(fx) and math.isfinite(fy)):
            raise ValueError(f"point must be finite, got {(x, y)!r}")
        fw = _parse_half_width(width) * 2.0
        if self._raw:
            px, py, _pw = self._raw[-1]
            self._total += math.hypot(fx - px, fy - py)
        self._raw.append((fx, fy, fw))
        # Decimation runs from the first sample (cheap exact filter),
        # so the kept stream mirrors a full run at all times; only the
        # heavier smooth/rail stages wait for the active phase.
        self._extend_decimate((fx, fy, fw))
        if not self._active():
            self._dirty = True
            return
        key = self._eff_key()
        if key == self._derived_key:
            return  # decimation dropped the sample: state unchanged
        self._extend_smooth()
        self._extend_rails()
        self._derived_key = key
        self._dirty = True

    def _eff_key(self):
        eff_len = len(self._kept) + (1 if self._pending is not None else 0)
        tail = self._pending if self._pending is not None else (
            self._kept[-1] if self._kept else None)
        return (eff_len, tail)

    def _extend_decimate(self, newest) -> None:
        """Exact streaming decimation (see class docs)."""
        spacing = float(self._min_spacing)
        if self._pending is not None:
            qx, qy, qw = self._pending
            if not self._kept:
                self._kept.append(self._pending)
            elif math.hypot(qx - self._kept[-1][0],
                            qy - self._kept[-1][1]) >= spacing:
                self._kept.append(self._pending)
            # else: interior within spacing — dropped, as a full run would.
        self._pending = newest

    def _effective_kept(self):
        eff = list(self._kept)
        if self._pending is not None:
            if not eff or self._pending[:2] != eff[-1][:2]:
                eff.append(self._pending)
        return eff

    def _pend_active(self):
        """Whether the provisional point extends the kept sequence."""
        pend = self._pending
        return (pend is not None
                and (not self._kept or pend[:2] != self._kept[-1][:2]))

    def _eff_tail(self, k):
        """Last ``k`` entries of the effective kept sequence (bounded).

        Exact tail of what ``_effective_kept`` would return, without
        copying the full history: only the kept tail plus the
        provisional point are materialized. The full copy stays
        available via ``_effective_kept`` for the rare full-derive
        paths below.
        """
        tail = self._kept[-k:]
        pend = self._pending
        if pend is not None and (not tail or pend[:2] != tail[-1][:2]):
            tail = (tail + [pend])[-k:]
        return tail

    def _smooth_vein(self, vein):
        """Run the module smooth stages over one vein (exact stages)."""
        pts = [(x, y) for x, y, _w in vein]
        vals = [w for _x, _y, w in vein]
        spts, svals = smooth_centerline(
            pts, vals, min_spacing=self._min_spacing,
            passes=self._passes)
        if svals is None:
            return None
        return [(x, y, w) for (x, y), w in zip(spts, svals)]

    def _extend_smooth(self) -> None:
        # B.3.7: the common path needs only the vein (bounded tail),
        # never the full effective list — the O(history) copy lives
        # only in _derive_smooth_full (small/fallback paths).
        n_eff = len(self._kept) + (1 if self._pend_active() else 0)
        if n_eff < 3 or n_eff < self._SMALL_FULL:
            self._derive_smooth_full()
            return
        vein = self._eff_tail(self._OVERLAP_KEPT)
        vein_len = sum(math.hypot(vein[i + 1][0] - vein[i][0],
                                 vein[i + 1][1] - vein[i][1])
                       for i in range(len(vein) - 1))
        if len(vein) < 3 or vein_len < float(self._min_spacing):
            # Tight recent cluster: staged pipeline would bypass on
            # the vein while the oracle smooths — derive directly.
            self._derive_smooth_full()
            return
        R = self._smooth_vein(vein)
        if R is None or len(R) < 2:
            self._derive_smooth_full()
            return
        # Alignment search: R re-derives history ending in new outputs.
        # Each Chaikin pass taints one head output with the vein's
        # pinned start (R[0], then one more per pass), so matching
        # starts after the first (2**passes - 1) entries. Find the
        # latest S_old index where an 8-triple run matches exactly.
        # Identical runs splice harmlessly: dedup collapses them to
        # the same downstream content.
        try:
            skip = (1 << int(self._passes)) - 1
        except (TypeError, ValueError):
            skip = 1
        if skip < 0:
            skip = 0
        old = self._S
        run = self._SPLICE_RUN
        lo = max(0, len(old) - self._SPLICE_WINDOW)
        splice = -1
        if len(R) - skip >= run:
            for s in range(len(old) - 1, lo - 1, -1):
                if old[s:s + run] == R[skip:skip + run]:
                    splice = s
                    break
        if splice < 0:
            self._count_fallback()
            self._derive_smooth_full()
            return
        # B.3.7: in-place tail splice — identical content to
        # ``old[:splice] + R[skip:]`` without copying the history
        # head. The search above already completed, so mutating the
        # list it read is safe; nothing else aliases this list.
        del self._S[splice:]
        self._S.extend(R[skip:])

    def _derive_smooth_full(self) -> None:
        eff = self._effective_kept()
        pts = [(x, y) for x, y, _w in eff]
        vals = [w for _x, _y, w in eff]
        spts, svals = smooth_centerline(
            pts, vals, min_spacing=self._min_spacing,
            passes=self._passes)
        self._S = [(x, y, w) for (x, y), w in zip(spts, svals)
                   ] if svals is not None else []

    def _drop_heads(self) -> None:
        """Forget cached assembly prefixes (rails fully re-derived)."""
        self._head_L = ()
        self._head_R_rev = ()
        self._head_discs = ()
        self._head_len = 0

    def _truncate_heads(self, keep: int) -> None:
        """Keep only the first ``keep`` covered rail entries.

        Mirrors the ``_discs`` filter in ``_rails_tail`` exactly: rails
        ``[:keep]`` are carried over verbatim, so cached prefixes
        covering them stay valid; anything beyond is rebuilt by
        ``outline()``. Backward disc scan touches only dropped entries.
        """
        h = self._head_len
        new_h = min(h, max(0, keep))
        if new_h < h:
            self._head_L = self._head_L[:new_h]
            self._head_R_rev = self._head_R_rev[h - new_h:]
            discs = self._head_discs
            while discs and discs[-1][0] >= keep - 2:
                discs = discs[:-1]
            self._head_discs = discs
            self._head_len = new_h

    def _extend_rails(self) -> None:
        # Recompute dedup + rails over a bounded tail window. The
        # window carries 2 context points; the splice search below
        # uses the FULL recomputed head (context included): a merged
        # run-continuation peak mismatches and forces the exact
        # fallback instead of silently keeping a stale width.
        W = self._RAIL_WINDOW
        S = self._S
        if len(S) < 3:
            self._count_fallback()
            self._rails_full()
            return
        c0 = max(0, len(S) - (W + 10))
        seg = S[c0:]
        kd, kh = _dedup_consecutive(
            [(x, y) for x, y, _w in seg],
            [w / 2.0 for _x, _y, w in seg])
        if len(kd) < 3:
            self._count_fallback()
            self._rails_full()
            return
        stable = [(x, y, h * 2.0) for (x, y), h in zip(kd, kh)]
        run = self._SPLICE_RUN
        lo = max(0, len(self._D) - 1 - self._SPLICE_WINDOW)
        splice = -1
        if len(stable) >= run:
            for s in range(len(self._D) - 1, lo - 1, -1):
                if self._D[s:s + run] == stable[:run]:
                    splice = s
                    break
        if splice < 0:
            self._count_fallback()
            self._rails_full()
            return
        # B.3.7: in-place tail splice (same content, no history copy).
        del self._D[splice:]
        self._D.extend(stable)
        self._rails_tail(splice)

    def _rails_full(self) -> None:
        # Exact full derive: dedup the smoothed list first so this is
        # correct no matter which state the caller left behind.
        # Everything is recomputed, so cached assembly prefixes die.
        self._drop_heads()
        full_d, full_h = _dedup_consecutive(
            [(x, y) for x, y, _w in self._S],
            [w / 2.0 for _x, _y, w in self._S])
        self._D = [(x, y, h * 2.0) for (x, y), h in zip(full_d, full_h)]
        if len(self._D) < 2:
            # Dot/empty handled by outline(); keep rail state clear.
            self._L, self._R, self._T = [], [], []
            self._discs = []
            self._start_cap = None
            return
        pts = [(x, y) for x, y, _w in self._D]
        halves = [w / 2.0 for _x, _y, w in self._D]
        left, right, tangents, discs = _rails_and_caps(pts, halves)
        self._L, self._R, self._T = left, right, tangents
        self._discs = discs
        if self._D:
            (sx, sy, sw) = self._D[0]
            self._start_cap = _start_cap_points(
                (sx, sy), tangents[0], sw / 2.0, self._cap,
                self._cap_segments)
        else:
            self._start_cap = None

    def _rails_tail(self, splice_at: int) -> None:
        # Recompute rails over the tail with carried seed context;
        # the head must verify exactly (stable region), else fall
        # back to an exact full derive. Joint splice_at - 1 is
        # recomputed (its next segment may be new); earlier joints
        # keep their stored discs.
        W = self._RAIL_WINDOW
        a = max(2, splice_at - 1)
        if len(self._D) <= W + 2 or a < 2 or a >= len(self._D):
            self._count_fallback()
            self._rails_full()
            return
        win = self._D[a - 1:]
        wpts = [(x, y) for x, y, _w in win]
        whalves = [w / 2.0 for _x, _y, w in win]
        left_w, right_w, T_w, discs_w = _rails_and_caps(
            wpts, whalves, prev_pt=(self._D[a - 2][0], self._D[a - 2][1]),
            prev_tangent=self._T[a - 2])
        if (len(left_w) < 3 or left_w[1] != self._L[a]
                or right_w[1] != self._R[a] or T_w[1] != self._T[a]):
            self._count_fallback()
            self._rails_full()
            return
        # B.3.7: in-place tail splices — identical content to the
        # head-copy form without copying rail history. The verify
        # above already completed, so mutating afterwards is safe.
        del self._L[a + 1:]
        self._L.extend(left_w[2:])
        del self._R[a + 1:]
        self._R.extend(right_w[2:])
        del self._T[a + 1:]
        self._T.extend(T_w[2:])
        # B.2.8: rails [:a + 1] carried over verbatim — cached assembly
        # prefixes covering them stay valid; the tail is rebuilt.
        self._truncate_heads(a + 1)
        self._discs = [(i, d) for i, d in self._discs if i < a - 1] + [
            (a - 1 + i, d) for i, d in discs_w]
        if self._start_cap is None and self._D:
            (sx, sy, sw) = self._D[0]
            self._start_cap = _start_cap_points(
                (sx, sy), self._T[0], sw / 2.0, self._cap,
                self._cap_segments)

    def outline(self) -> StrokeOutline:
        """Current outline; exact-match with the full rebuild.

        Results are cached until the next state-changing ``append``,
        so repeated paints of an unchanged stroke are free. B.2.8: a
        dirty rebuild reuses cached assembly prefixes (``_head_*``,
        covering splice-verified rail entries) and wraps only the tail
        window into new ``Point``\\ s — output is ``==``-identical to a
        full rebuild, but per-pull allocation is O(tail), not O(stroke).
        """
        if not self._raw:
            raise ValueError("no samples appended")
        if not self._dirty and self._cached is not None:
            return self._cached
        if not self._active():
            pts = [(x, y) for x, y, _w in self._raw]
            vals = [w for _x, _y, w in self._raw]
            result = build_outline(pts, vals, cap=self._cap,
                                   cap_segments=self._cap_segments)
            self._drop_heads()
            self._cached = result
            self._dirty = False
            return result
        if len(self._D) == 1:
            (x, y, w) = self._D[0]
            result = StrokeOutline(dots=(OutlineDot(
                center=Point(x, y), radius=w / 2.0),))
            self._drop_heads()
            self._cached = result
            self._dirty = False
            return result
        if not self._D or not self._L:
            pts = [(x, y) for x, y, _w in self._raw]
            vals = [w for _x, _y, w in self._raw]
            result = build_outline(pts, vals, cap=self._cap,
                                   cap_segments=self._cap_segments)
            self._drop_heads()
            self._cached = result
            self._dirty = False
            return result
        (ex, ey, ew) = self._D[-1]
        end_cap = _end_cap_points((ex, ey), self._T[-1], ew / 2.0,
                                  self._cap, self._cap_segments)
        # Stable head (splice-verified, shared Point objects) + fresh
        # tail. The right rail runs backward in polygon order, so its
        # cached head is stored pre-reversed.
        h = min(self._head_len, len(self._L), len(self._R))
        l_head = self._head_L[:h]
        r_head = self._head_R_rev[self._head_len - h:]
        tail_l = tuple(Point(x, y) for x, y in self._L[h:])
        tail_r = tuple(Point(x, y) for x, y in self._R[h:])
        mid_r = tail_r[::-1]
        cap_end = tuple(Point(x, y) for x, y in end_cap)
        cap_start = tuple(Point(x, y) for x, y in (
            self._start_cap or ()))
        result = StrokeOutline(
            polygon=l_head + tail_l + cap_end + mid_r + r_head
            + cap_start,
            discs=tuple(d for _idx, d in self._head_discs)
            + tuple(d for _idx, d in self._discs[len(
                self._head_discs):]))
        # Repopulate heads to full coverage for the next pull. Heads
        # remain a verbatim prefix of _L/_R/_discs until the next rails
        # mutation truncates or drops them.
        self._head_L = l_head + tail_l
        self._head_R_rev = mid_r + r_head
        self._head_discs = tuple(self._discs)
        self._head_len = len(self._L)
        self._cached = result
        self._dirty = False
        return result


class LiveCenterline:
    """Incremental widthless smoothed centerline for one live stroke.

    B.2.10: reproduces ``smooth_centerline(raw, None)`` exactly at
    every prefix (proven by the per-prefix equivalence tests), with
    bounded per-append work. This is the smooth-half twin of
    ``LiveOutline`` without the outline/rail/disc/cap pipeline: the
    Pressure-OFF paint path needs only smoothed points for its
    ``drawPolyline``, so carrying rails would be pure overhead.

    How exactness holds: the same streaming decimation as
    ``LiveOutline`` (drops never change state, so only the
    provisional final point can be retracted) and the same Chaikin
    vein re-derive with the same head-taint skip (``2**passes - 1``)
    and 8-triple exact-match splice over the same trailing window.
    Any splice failure falls back to an exact full derive — output
    is never approximate, only occasionally slower.

    Raw inputs are copied, never retained by reference and never
    mutated. Qt-free. ``reset()`` starts a new stroke. ``smoothed()``
    returns a cached tuple until the next state-changing ``append``,
    so repeated paints of an unchanged stroke are free.
    """

    def __init__(self, *,
                 min_spacing: float = SMOOTH_MIN_SPACING_PT,
                 passes: int = SMOOTH_PASSES) -> None:
        self._min_spacing = min_spacing
        self._passes = passes
        self.reset()

    def reset(self) -> None:
        """Forget all samples (stroke boundary)."""
        self._raw: list[tuple[float, float]] = []
        self._total = 0.0
        self._kept: list[tuple[float, float]] = []
        self._pending: tuple[float, float] | None = None
        self._S: list[tuple[float, float]] = []
        self._cached: tuple | None = None
        self._dirty = True
        self.fallbacks = 0

    def __len__(self) -> int:
        """Number of raw samples fed."""
        return len(self._raw)

    def _count_fallback(self) -> None:
        # Same convention as LiveOutline: startup derivations (tiny
        # lists) always take the exact full path by design and must
        # not pollute the fast-path statistic.
        if len(self._raw) > 64:
            self.fallbacks += 1

    def _params_ok(self) -> bool:
        try:
            spacing = float(self._min_spacing)
            n_passes = int(self._passes)
        except (TypeError, ValueError):
            return False
        return (math.isfinite(spacing) and spacing > 0.0
                and n_passes >= 0)

    def _active(self) -> bool:
        return (self._params_ok() and len(self._raw) >= 3
                and self._total >= float(self._min_spacing))

    def append(self, x: float, y: float) -> None:
        """Feed one raw sample; raises ``ValueError`` on bad input."""
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            raise ValueError(f"point must be numeric, got {(x, y)!r}")
        if not (math.isfinite(fx) and math.isfinite(fy)):
            raise ValueError(f"point must be finite, got {(x, y)!r}")
        if self._raw:
            px, py = self._raw[-1]
            self._total += math.hypot(fx - px, fy - py)
        self._raw.append((fx, fy))
        # Exact streaming decimation, mirroring LiveOutline: drops
        # never change state; only the provisional final point can
        # be retracted by the next sample.
        spacing = float(self._min_spacing)
        if self._pending is not None:
            qx, qy = self._pending
            if not self._kept:
                self._kept.append(self._pending)
            elif math.hypot(qx - self._kept[-1][0],
                            qy - self._kept[-1][1]) >= spacing:
                self._kept.append(self._pending)
            # else: interior within spacing — dropped, as a full run
            # would.
        self._pending = (fx, fy)
        if not self._active():
            self._cached = None
            self._dirty = True
            return
        # B.3.7: bounded tail only — see LiveOutline._eff_tail. The
        # full copy lives only in _derive_full (small/fallback paths).
        n_eff = len(self._kept) + (1 if (
            self._pending is not None and (
                not self._kept
                or self._pending != self._kept[-1])) else 0)
        if n_eff < 3 or n_eff < LiveOutline._SMALL_FULL:
            self._derive_full()
            return
        tail = self._kept[-LiveOutline._OVERLAP_KEPT:]
        if self._pending is not None and (
                not tail or self._pending != tail[-1]):
            tail = (tail + [self._pending])[-LiveOutline._OVERLAP_KEPT:]
        vein = tail
        vein_len = sum(math.hypot(vein[i + 1][0] - vein[i][0],
                                 vein[i + 1][1] - vein[i][1])
                       for i in range(len(vein) - 1))
        if len(vein) < 3 or vein_len < float(self._min_spacing):
            # Tight recent cluster: staged pipeline would bypass on
            # the vein while the oracle smooths — derive directly.
            self._derive_full(eff)
            return
        spts, _ = smooth_centerline(
            vein, None, min_spacing=self._min_spacing,
            passes=self._passes)
        R = list(spts)
        if len(R) < 2:
            self._derive_full()
            return
        # Same alignment search as LiveOutline._extend_smooth: each
        # Chaikin pass taints one head output with the vein's pinned
        # start, so matching starts after the first (2**passes - 1)
        # entries; find the latest old index where an 8-triple run
        # matches exactly.
        try:
            skip = (1 << int(self._passes)) - 1
        except (TypeError, ValueError):
            skip = 1
        if skip < 0:
            skip = 0
        old = self._S
        run = LiveOutline._SPLICE_RUN
        lo = max(0, len(old) - LiveOutline._SPLICE_WINDOW)
        splice = -1
        if len(R) - skip >= run:
            for s in range(len(old) - 1, lo - 1, -1):
                if old[s:s + run] == R[skip:skip + run]:
                    splice = s
                    break
        if splice < 0:
            self._count_fallback()
            self._derive_full()
            return
        # B.3.7: in-place tail splice (same content, no history copy).
        del self._S[splice:]
        self._S.extend(R[skip:])
        self._cached = None
        self._dirty = True

    def _derive_full(self) -> None:
        eff = list(self._kept)
        if self._pending is not None:
            if not eff or self._pending != eff[-1]:
                eff.append(self._pending)
        spts, _ = smooth_centerline(
            eff, None, min_spacing=self._min_spacing,
            passes=self._passes)
        self._S = list(spts)
        self._cached = None
        self._dirty = True

    def smoothed(self) -> tuple:
        """Current smoothed centerline; exact-match with the full run.

        Results are cached until the next state-changing ``append``,
        so repeated paints of an unchanged stroke are free. Inactive
        strokes (bypass rules) return the raw samples as a tuple —
        exactly what ``smooth_centerline`` returns.
        """
        if not self._dirty and self._cached is not None:
            return self._cached
        if not self._active():
            result = tuple(self._raw)
        else:
            result = tuple(self._S)
        self._cached = result
        self._dirty = False
        return result


# -- M8: rectangular highlighter brush footprint ---------------------------

#: Highlighter band height as a multiple of the effective thickness. The
#: highlighter cursor draws the same vertical rectangle (see
#: ``app/cursors.py``); this constant is the single source of truth for the
#: shared aspect ratio. Zoom and display scaling are applied later by the
#: painter, so stored band geometry is zoom-independent page-local pt.
HIGHLIGHTER_BAND_ASPECT = 2.6


def _footprint_corners(x: float, y: float, half_width: float,
                       height: float) -> tuple:
    """Four corners of the brush footprint anchored at ``(x, y)``.

    The footprint is the highlighter cursor's vertical rectangle: centred
    on the nib's contact point both horizontally and vertically, so the
    band expands and contracts symmetrically around the pointer as
    pressure changes. Page coordinates are y-down.
    """
    half_height = height / 2.0
    return (
        (x - half_width, y - half_height),
        (x + half_width, y - half_height),
        (x + half_width, y + half_height),
        (x - half_width, y + half_height),
    )


def _convex_hull(points: Sequence) -> tuple:
    """Monotone-chain convex hull with a deterministic winding.

    The ring is returned without repeating the first vertex. Its winding
    is a pure function of the sorted vertex set, so every hull produced by
    :func:`highlighter_band_hulls` shares the same orientation: overlap
    regions in a winding-fill path then count twice instead of cancelling,
    which is what keeps self-crossing strokes from punching holes.
    """
    ordered = sorted(set(points))
    if len(ordered) <= 2:
        return tuple(ordered)

    def cross(o, a, b) -> float:
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


def highlighter_band_hulls(
        points: Sequence,
        thickness_pt: float,
        *,
        aspect: float = HIGHLIGHTER_BAND_ASPECT,
        widths: Sequence | None = None,
) -> tuple:
    """Sweep the highlighter's rectangular brush along a centerline.

    The brush is the vertical, borderless rectangle the highlighter cursor
    draws: at centerline point ``(x, y)`` it covers
    ``[x - t/2, x + t/2] x [y - aspect*t/2, y + aspect*t/2]`` in
    page-local PDF points. ``(x, y)`` is the nib's contact point (and the
    pointer hotspot), the centre of the rectangle, so the band grows and
    shrinks symmetrically around the pointer. Sweeping that rectangle
    along one segment is exactly the
    convex hull of the two endpoint footprints, so a polyline yields one
    hull per segment; their union is the visibly continuous band (adjacent
    hulls always share the footprint at the joint point, so no gaps or
    seams can appear).

    M7.1.4: ``widths`` optionally supplies the per-sample effective
    thickness (already pressure-mapped and smoothed by the caller). Each
    footprint then keeps the same rectangular nib and the same
    ``aspect`` height ratio but uses its own thickness, so a pressure
    taper changes the band's *width* without changing its character.
    ``None`` — or a sequence that does not match ``points`` — keeps the
    constant ``thickness_pt`` band, which is what legacy annotations and
    mouse input use. Per-sample non-finite/non-positive values fall back
    to ``thickness_pt`` rather than breaking the band.

    This pure geometry is shared by the painter and, through
    :data:`HIGHLIGHTER_BAND_ASPECT`, by the cursor: the cursor keeps its
    approved screen-space size clamps, but the aspect ratio and anchoring
    originate here.

    Args:
        points: page-local ``(x, y)`` centerline samples (pairs or
            ``core.types.Point``).
        thickness_pt: nominal effective thickness in PDF points (finite,
            > 0).
        aspect: band height / thickness (finite, > 0).
        widths: optional per-sample effective thicknesses parallel to
            ``points``.

    Returns:
        One hull ring per non-degenerate segment, each a tuple of
        ``(x, y)`` floats in a consistent winding. Empty input yields
        ``()``; one point (or one point repeated) yields a single
        footprint ring.

    Raises:
        ValueError: non-finite/non-positive ``thickness_pt`` or ``aspect``,
            or a malformed point (same rules as ``smooth_centerline``).
    """
    try:
        thickness = float(thickness_pt)
        ratio = float(aspect)
    except (TypeError, ValueError):
        raise ValueError(
            "thickness_pt and aspect must be numeric, got "
            f"{thickness_pt!r}, {aspect!r}")
    if (not math.isfinite(thickness) or thickness <= 0.0
            or not math.isfinite(ratio) or ratio <= 0.0):
        raise ValueError(
            "thickness_pt and aspect must be finite and positive")
    if points is None:
        raise ValueError("points must be a sequence, got None")
    try:
        n = len(points)
    except TypeError:
        raise ValueError("points must be a sized sequence")
    if n == 0:
        return ()
    per_point: list | None = None
    if widths is not None:
        try:
            if len(widths) == n:
                per_point = []
                for value in widths:
                    try:
                        width = float(value)
                    except (TypeError, ValueError):
                        width = thickness
                    if not math.isfinite(width) or width <= 0.0:
                        width = thickness
                    per_point.append(width)
        except TypeError:
            per_point = None
    parsed: list = []
    for index, value in enumerate(points):
        vertex = _parse_vertex(value)
        if parsed and parsed[-1][0] == vertex:
            continue
        width = per_point[index] if per_point is not None else thickness
        parsed.append((vertex, width))
    if len(parsed) == 1:
        x, y = parsed[0][0]
        width = parsed[0][1]
        return (_convex_hull(_footprint_corners(
            x, y, width / 2.0, width * ratio)),)
    hulls: list = []
    for (start, start_w), (end, end_w) in zip(parsed, parsed[1:]):
        x0, y0 = start
        x1, y1 = end
        hulls.append(_convex_hull(
            _footprint_corners(x0, y0, start_w / 2.0, start_w * ratio)
            + _footprint_corners(x1, y1, end_w / 2.0, end_w * ratio)))
    return tuple(hulls)
