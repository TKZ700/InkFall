"""B.2.2: geometry-only smoothing pass (offscreen, deterministic).

Covers ``annotations.ink_geometry.smooth_centerline``: jitter
reduction measured against fixtures (never point counts), deviation
bounds, corners, dots, width alignment, immutability, determinism,
and a performance probe. No Qt needed except the viewer-level checks.
"""

from __future__ import annotations

import math
import time

import pytest

from annotations.ink_geometry import (
    SMOOTH_MIN_SPACING_PT,
    SMOOTH_PASSES,
    build_outline,
    smooth_centerline,
)

pytestmark = pytest.mark.usefixtures("qapp")


def _noisy_straight(n=40, dx=3.0):
    """Deterministic tablet-style jitter (no RNG).

    Sample-to-sample white noise (~0.4pt) on multi-px spacing — the
    harsh end of real pen jitter. (A large-amplitude sawtooth is an
    intentional zigzag, not jitter; smoothing must largely keep it.)
    """
    return [(i * dx, 0.4 * (((i * 2654435761) % 1024) / 1024.0 * 2 - 1))
            for i in range(n)]


def _dense_noisy_run(n=60, dx=0.2):
    """Slow-pen cluster: spacing below the resample threshold."""
    return [(i * dx, 0.4 * (((i * 2654435761 + 77) % 1024) / 1024.0 * 2 - 1))
            for i in range(n)]


def _rms(pts):
    return math.sqrt(sum(y * y for _, y in pts) / len(pts))


def _max_abs_y(pts):
    return max(abs(y) for _, y in pts)


def _dist_to_polyline(px, py, poly):
    best = float("inf")
    for i in range(len(poly) - 1):
        ax, ay = poly[i]
        bx, by = poly[i + 1]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0.0:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                             / length_sq))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        best = min(best, d)
    return best


def _max_deviation(smoothed, raw):
    return max(_dist_to_polyline(x, y, raw) for x, y in smoothed)


def test_constants_documented():
    assert SMOOTH_MIN_SPACING_PT == 0.5
    assert SMOOTH_PASSES == 2


def test_jitter_reduction_measured():
    raw = _noisy_straight()
    assert _rms(raw) > 0.15  # fixture really is noisy
    spts, _ = smooth_centerline(raw, None)
    # White-noise wiggle energy drops; the low-frequency path that
    # remains is signal, not jitter (further passes plateau here).
    assert _rms(spts) < 0.7 * _rms(raw)


def test_dense_cluster_jitter_reduced():
    raw = _dense_noisy_run()
    assert _rms(raw) > 0.15
    spts, _ = smooth_centerline(raw, None)
    assert _rms(spts) < 0.7 * _rms(raw)
    assert _max_deviation(spts, raw) <= 1.5 * SMOOTH_MIN_SPACING_PT


def test_deviation_from_raw_path_bounded():
    raw = _noisy_straight()
    spts, _ = smooth_centerline(raw, None)
    assert _max_deviation(spts, raw) <= 1.5 * SMOOTH_MIN_SPACING_PT


def test_endpoints_pinned():
    raw = _noisy_straight()
    spts, _ = smooth_centerline(raw, None)
    assert spts[0] == pytest.approx(raw[0])
    assert spts[-1] == pytest.approx(raw[-1])


def test_intentional_corner_preserved():
    raw = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    spts, _ = smooth_centerline(raw, None)
    assert spts[0] == pytest.approx((0.0, 0.0))
    assert spts[-1] == pytest.approx((10.0, 10.0))
    # The smoothed path still rounds the corner tip closely.
    assert _dist_to_polyline(10.0, 0.0, spts) < 1.0
    assert _max_deviation(spts, raw) <= 1.5 * SMOOTH_MIN_SPACING_PT


def test_curve_shape_preserved():
    raw = [(10.0 * math.cos(t), 10.0 * math.sin(t))
           for t in (i * 0.15 for i in range(21))]
    spts, _ = smooth_centerline(raw, None)
    assert spts[0] == pytest.approx(raw[0])
    assert spts[-1] == pytest.approx(raw[-1])
    assert _max_deviation(spts, raw) <= 1.5 * SMOOTH_MIN_SPACING_PT


def test_duplicates_and_irregular_spacing():
    raw = [(0.0, 0.0), (0.0, 0.0), (0.05, 0.0), (5.0, 0.0),
           (5.2, 0.1), (30.0, 0.0)]
    spts, vals = smooth_centerline(raw, [2.0] * len(raw))
    assert len(spts) == len(vals)
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in spts)
    assert spts[0] == pytest.approx((0.0, 0.0))
    assert spts[-1] == pytest.approx((30.0, 0.0))


def test_dots_and_short_strokes_bypass_identical():
    dot = [(3.0, 4.0)]
    assert smooth_centerline(dot, None) == (tuple(dot), None)
    pair = [(0.0, 0.0), (0.1, 0.0)]
    assert smooth_centerline(pair, [2.0, 2.0]) == (
        tuple(pair), (2.0, 2.0))
    tiny = [(0.0, 0.0), (0.2, 0.0), (0.4, 0.0)]
    assert smooth_centerline(tiny, None) == (tuple(tiny), None)


def test_reversal_stays_finite_and_bounded():
    raw = [(0.0, 0.0), (10.0, 0.0), (1.0, 0.5)]
    spts, _ = smooth_centerline(raw, None)
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in spts)
    # Degenerate V: any smoothing cuts the turnaround, but never by
    # more than a fraction of the longest raw arm.
    longest = max(math.hypot(raw[i + 1][0] - raw[i][0],
                             raw[i + 1][1] - raw[i][1])
                  for i in range(len(raw) - 1))
    for x, y in spts:
        nearest = min(math.hypot(x - sx, y - sy) for sx, sy in raw)
        assert nearest <= 0.5 * longest


def test_widths_aligned_and_ramps_kept():
    raw = [(float(i), 0.0) for i in range(10)]
    widths = [1.0 + 0.5 * i for i in range(10)]
    spts, svals = smooth_centerline(raw, widths)
    assert svals is not None
    assert len(spts) == len(svals)
    assert list(svals) == sorted(svals)  # ramp monotonicity kept
    assert svals[0] == pytest.approx(widths[0])
    assert svals[-1] == pytest.approx(widths[-1])
    assert all(w > 0.0 and math.isfinite(w) for w in svals)


def test_none_widths_round_trip():
    raw = _noisy_straight()
    spts, svals = smooth_centerline(raw, None)
    assert svals is None
    assert len(spts) >= 2


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        smooth_centerline(None, None)
    with pytest.raises(ValueError):
        smooth_centerline([], None)
    with pytest.raises(ValueError):
        smooth_centerline([(0.0, 0.0)], [1.0, 2.0])
    with pytest.raises(ValueError):
        smooth_centerline([(0.0, float("nan"))], None)
    with pytest.raises(ValueError):
        smooth_centerline([(0.0, 0.0)], [0.0])


def test_bad_parameters_bypass_without_raising():
    raw = _noisy_straight()
    for kwargs in ({"min_spacing": 0.0}, {"min_spacing": -1.0},
                   {"passes": -1}, {"min_spacing": float("nan")}):
        spts, _ = smooth_centerline(raw, None, **kwargs)
        assert tuple(spts) == tuple(raw)


def test_no_mutation_and_determinism():
    raw = [[0.0, 0.0], [4.0, 1.0], [8.0, -1.0], [12.0, 0.5]]
    widths = [2.0, 3.0, 2.5, 2.0]
    first = smooth_centerline(raw, widths)
    assert raw == [[0.0, 0.0], [4.0, 1.0], [8.0, -1.0], [12.0, 0.5]]
    assert widths == [2.0, 3.0, 2.5, 2.0]
    assert smooth_centerline(raw, widths) == first


def test_smoothed_outline_builds_and_stays_bounded():
    raw = _noisy_straight()
    widths = [2.0] * len(raw)
    spts, svals = smooth_centerline(raw, widths)
    outline = build_outline(spts, svals)
    assert outline.dots == ()
    assert len(outline.polygon) > 0
    for p in outline.polygon:
        assert math.isfinite(p.x) and math.isfinite(p.y)


def test_viewer_pressure_path_uses_smoothing():
    from document.manager import DocumentManager

    from app.viewer import PdfViewer
    from core.types import PageId, Point
    from annotations.ink_geometry import build_outline, pressure_widths
    from annotations.models import InkStroke

    viewer = PdfViewer(DocumentManager())
    try:
        raw = _noisy_straight()
        pts = tuple(Point(x, y) for x, y in raw)
        stroke = InkStroke(points=pts, color="#ff0000", width_mm=3.0,
                           pressures=tuple(0.5 for _ in pts),
                           times_ms=tuple(range(len(pts))),
                           pressure_enabled=True)
        outline = viewer._pressure_outline(stroke)
        assert outline is not None and outline.dots == ()
        widths = pressure_widths(tuple(0.5 for _ in pts),
                                 3.0 * 72.0 / 25.4)
        raw_outline = build_outline([(x, y) for x, y in raw], widths)
        smooth_max = max(abs(p.y) for p in outline.polygon)
        raw_max = max(abs(p.y) for p in raw_outline.polygon)
        assert smooth_max < raw_max  # rails calm down at paint time
        assert smooth_max <= max(widths) / 2.0 + 1.0
    finally:
        viewer.close()


def test_perf_2000_samples_reported():
    raw = [(i * 0.8,
            2.0 * math.sin(i * 1.7) + 1.0 * math.sin(i * 0.9 + 0.4))
           for i in range(2000)]
    widths = [2.0 + (i % 7) * 0.1 for i in range(2000)]
    start = time.perf_counter()
    spts, svals = smooth_centerline(raw, widths)
    outline = build_outline(spts, svals)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    print(f"\nsmooth+outline 2000 samples: {elapsed_ms:.2f} ms "
          f"({len(spts)} smoothed pts, {len(outline.polygon)} verts)")
    assert elapsed_ms < 100.0  # generous guard; target < 5 ms reported


def test_perf_typical_stroke_well_within_budget():
    raw = [(i * 3.0,
            2.0 * math.sin(i * 1.7) + 1.0 * math.sin(i * 0.9 + 0.4))
           for i in range(300)]
    widths = [2.0 + (i % 7) * 0.1 for i in range(300)]
    start = time.perf_counter()
    spts, svals = smooth_centerline(raw, widths)
    build_outline(spts, svals)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    print(f"\nsmooth+outline 300 samples: {elapsed_ms:.2f} ms")
    assert elapsed_ms < 5.0
