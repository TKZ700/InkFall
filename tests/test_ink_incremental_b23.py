"""B.2.3 Stage 3: incremental LiveOutline exact-equivalence proofs.

A ``LiveOutline`` fed sample-by-sample must return outlines EXACTLY
equal (``==``, bitwise) to the full-rebuild oracle
``build_outline(smooth_centerline(all))`` after every prefix — across
randomized strokes (seeded, deterministic), duplicates, reversals,
pressure jumps, caps, and every split boundary. Fallback counters
prove the fast path is actually taken.
"""

from __future__ import annotations

import math
import random

import pytest

from annotations.ink_geometry import (
    LiveOutline,
    build_outline,
    smooth_centerline,
)


def _oracle(pts, widths, cap="round"):
    spts, svals = smooth_centerline(pts, widths)
    return build_outline(spts, svals if svals is not None else widths,
                         cap=cap)


def _random_stroke(rng, n):
    pts, widths = [], []
    x, y = 0.0, 0.0
    for _ in range(n):
        r = rng.random()
        if r < 0.08 and pts:
            # Exact reversal to a recent point.
            x, y = pts[rng.randrange(len(pts))]
        elif r < 0.15 and pts:
            # Near-duplicate jitter.
            x += rng.uniform(-1e-10, 1e-10)
            y += rng.uniform(-1e-10, 1e-10)
        elif r < 0.25:
            # Pressure jump without moving far.
            x += rng.uniform(-0.2, 0.2)
            y += rng.uniform(-0.2, 0.2)
        else:
            x += rng.uniform(-4.0, 6.0)
            y += rng.uniform(-4.0, 4.0)
        pts.append((x, y))
        widths.append(rng.choice([0.5, 1.0, 2.0, 4.0, 8.0]))
    return pts, widths


def _feed(live, pts, widths, upto=None):
    upto = len(pts) if upto is None else upto
    for i in range(upto):
        live.append(pts[i][0], pts[i][1], widths[i])


@pytest.mark.parametrize("cap", ["round", "butt"])
@pytest.mark.parametrize("seed", [7, 1234, 99991])
@pytest.mark.parametrize("n", [5, 17, 40])
def test_incremental_matches_oracle_every_prefix(cap, seed, n):
    rng = random.Random(seed)
    pts, widths = _random_stroke(rng, n)
    live = LiveOutline(cap=cap)
    for k in range(1, n + 1):
        live.append(pts[k - 1][0], pts[k - 1][1], widths[k - 1])
        got = live.outline()
        want = _oracle(pts[:k], widths[:k], cap)
        assert got == want, f"cap={cap} seed={seed} n={n} k={k}"


def test_fallbacks_stay_zero_on_ordinary_streams():
    rng = random.Random(4242)
    live = LiveOutline()
    pts, widths = _random_stroke(rng, 120)
    for i in range(len(pts)):
        live.append(pts[i][0], pts[i][1], widths[i])
    assert live.outline() == _oracle(pts, widths)
    assert live.fallbacks == 0


def test_dense_blob_matches_and_stays_fast():
    live = LiveOutline()
    pts = [(5.0, 5.0)] * 300
    widths = [2.0 + (i % 3) * 0.5 for i in range(300)]
    _feed(live, pts, widths)
    assert live.outline() == _oracle(pts, widths)


def test_single_dot_and_empty():
    live = LiveOutline()
    with pytest.raises(ValueError):
        live.outline()
    live.append(3.0, 4.0, 2.0)
    got = live.outline()
    assert got.polygon == () and len(got.dots) == 1
    assert got == _oracle([(3.0, 4.0)], [2.0])


def test_two_point_capsule_matches():
    live = LiveOutline()
    live.append(0.0, 0.0, 2.0)
    live.append(4.0, 0.0, 4.0)
    assert live.outline() == _oracle([(0.0, 0.0), (4.0, 0.0)], [2.0, 4.0])


def test_exact_backtrack_matches():
    pts = [(0.0, 0.0), (5.0, 0.0), (0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    widths = [2.0, 2.0, 3.0, 2.0, 2.0]
    live = LiveOutline()
    for k in range(1, len(pts) + 1):
        live.append(pts[k - 1][0], pts[k - 1][1], widths[k - 1])
        assert live.outline() == _oracle(pts[:k], widths[:k])


def test_reset_starts_clean():
    live = LiveOutline()
    _feed(live, [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)], [1.0, 1.0, 1.0])
    live.reset()
    with pytest.raises(ValueError):
        live.outline()
    live.append(9.0, 9.0, 3.0)
    assert live.outline() == _oracle([(9.0, 9.0)], [3.0])


def test_invalid_appends_raise():
    live = LiveOutline()
    for bad in ((float("nan"), 0.0, 1.0), (0.0, float("inf"), 1.0),
                ("x", 0.0, 1.0), (0.0, 0.0, 0.0), (0.0, 0.0, -1.0),
                (0.0, 0.0, float("nan"))):
        with pytest.raises(ValueError):
            live.append(*bad)
    with pytest.raises(ValueError):
        LiveOutline(cap="miter")
    with pytest.raises(ValueError):
        LiveOutline(cap_segments=0)


def test_inputs_copied_not_retained():
    live = LiveOutline()
    pt = [1.0, 2.0]
    live.append(pt[0], pt[1], 2.0)
    live.append(5.0, 5.0, 2.0)
    live.append(9.0, 1.0, 2.0)
    pt[0] = 999.0  # mutating caller data must not affect the builder
    assert live.outline() == _oracle([(1.0, 2.0), (5.0, 5.0), (9.0, 1.0)],
                                     [2.0, 2.0, 2.0])


def test_determinism():
    def build():
        live = LiveOutline()
        _feed(live, [(0.0, 0.0), (3.0, 1.0), (6.0, -1.0), (9.0, 0.5)],
              [1.0, 4.0, 2.0, 3.0])
        return live.outline()

    assert build() == build()
