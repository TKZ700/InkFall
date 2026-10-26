"""B.2.1 Slice 1: pure pressure-to-width mapping tests (offscreen).

Covers ``annotations/ink_geometry.py`` only. No Qt, no viewer, no
painting — this slice introduces no visible behavior change.
"""

from __future__ import annotations

import math

import pytest

from annotations.ink_geometry import (
    MIN_WIDTH_RATIO,
    RESPONSE_EXPONENT,
    pressure_width,
    pressure_widths,
)

BASE = 0.7  # pen default width_mm (tests use it as an arbitrary base)

# B.2.4 deliberately changed the default ("normal") curve from the
# B.2.1 (0.3, 0.8) mapping to the linear (0.2, 1.0) candidate: the old
# curve's working band (0.1-0.6 pressure) spanned only ~1.8x in width,
# which hardware-feel review judged too subtle. The new curve spans
# ~2.4x across the same band while keeping a nonzero floor so light
# touches stay visible. Tests below pin the NEW normal curve; the
# retained MIN_WIDTH_RATIO/RESPONSE_EXPONENT constants document the
# superseded B.2.1 defaults only.
NORMAL_FLOOR = 0.2
NORMAL_EXPONENT = 1.0


def test_constants_are_conservative_defaults():
    assert MIN_WIDTH_RATIO == 0.3
    assert RESPONSE_EXPONENT == 0.8


def test_zero_pressure_gives_minimum_width():
    assert pressure_width(0.0, BASE) == pytest.approx(BASE * NORMAL_FLOOR)


def test_full_pressure_gives_exactly_base_width():
    assert pressure_width(1.0, BASE) == pytest.approx(BASE)


def test_mid_pressure_matches_formula():
    p = 0.5
    expected = BASE * (NORMAL_FLOOR + (1.0 - NORMAL_FLOOR) * p ** NORMAL_EXPONENT)
    assert pressure_width(p, BASE) == pytest.approx(expected)


def test_widths_bounded_on_dense_grid():
    low = BASE * NORMAL_FLOOR
    for i in range(1001):
        w = pressure_width(i / 1000.0, BASE)
        assert math.isfinite(w)
        assert low <= w <= BASE


def test_monotonic_nondecreasing():
    prev = 0.0
    for i in range(1001):
        w = pressure_width(i / 1000.0, BASE)
        assert w >= prev
        prev = w


def test_sequence_maps_one_to_one_in_order():
    pressures = [0.0, 0.25, 0.5, 0.75, 1.0]
    widths = pressure_widths(pressures, BASE)
    assert isinstance(widths, tuple)
    assert len(widths) == len(pressures)
    assert list(widths) == [pressure_width(p, BASE) for p in pressures]


def test_empty_and_missing_sequences_yield_empty():
    # Documented fallback: no samples -> caller keeps full-base-width
    # legacy rendering (nothing to map).
    assert pressure_widths((), BASE) == ()
    assert pressure_widths([], BASE) == ()
    assert pressure_widths(None, BASE) == ()


def test_missing_single_pressure_uses_full_width():
    assert pressure_width(None, BASE) == pytest.approx(BASE)


def test_out_of_range_pressures_clamp():
    assert pressure_width(-2.0, BASE) == pytest.approx(BASE * NORMAL_FLOOR)
    assert pressure_width(1.5, BASE) == pytest.approx(BASE)
    assert pressure_width(float("-inf"), BASE) == pytest.approx(BASE * NORMAL_FLOOR)
    assert pressure_width(float("inf"), BASE) == pytest.approx(BASE)


def test_non_finite_and_garbage_pressures_never_raise():
    # NaN / non-numeric follow the "never lose ink" policy: full width.
    assert pressure_width(float("nan"), BASE) == pytest.approx(BASE)
    assert pressure_width("hard", BASE) == pytest.approx(BASE)
    mixed = pressure_widths([0.0, float("nan"), "x", None, 2.0, 1.0], BASE)
    assert len(mixed) == 6
    assert all(math.isfinite(w) for w in mixed)
    assert mixed[0] == pytest.approx(BASE * NORMAL_FLOOR)
    assert mixed[1] == pytest.approx(BASE)
    assert mixed[4] == pytest.approx(BASE)
    assert mixed[5] == pytest.approx(BASE)


def test_deterministic():
    first = pressure_widths([0.1, 0.4, 0.9], BASE)
    second = pressure_widths([0.1, 0.4, 0.9], BASE)
    assert first == second
    assert pressure_width(0.33, BASE) == pressure_width(0.33, BASE)


def test_input_sequence_not_mutated():
    pressures = [0.0, 0.5, 1.0]
    snapshot = list(pressures)
    pressure_widths(pressures, BASE)
    assert pressures == snapshot


def test_invalid_base_width_raises():
    for bad in (0.0, -0.5, float("nan"), float("inf"), float("-inf"),
                "wide", None):
        with pytest.raises(ValueError):
            pressure_width(0.5, bad)
        with pytest.raises(ValueError):
            pressure_widths([0.5], bad)


def test_units_preserved_and_positive():
    for base in (0.3, 0.5, 2.0, 5.0):
        widths = pressure_widths([0.0, 0.5, 1.0], base)
        assert widths[0] == pytest.approx(base * NORMAL_FLOOR)
        assert widths[2] == pytest.approx(base)
        assert all(w > 0.0 for w in widths)
