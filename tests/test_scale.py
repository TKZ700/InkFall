"""M3-B scale tests: buckets, headroom, hysteresis, determinism."""

from __future__ import annotations

import pytest

from rendering.scale import (
    HEADROOM,
    MAX_RASTER_SCALE,
    MIN_RASTER_SCALE,
    RASTER_BUCKETS,
    next_bucket,
    quantize_scale,
)


def test_bucket_ladder():
    assert RASTER_BUCKETS == (0.5, 1.0, 2.0, 4.0, 8.0)
    assert (MIN_RASTER_SCALE, MAX_RASTER_SCALE) == (0.5, 8.0)
    assert HEADROOM == 1.25


def test_exact_buckets_pass_through():
    for bucket in RASTER_BUCKETS:
        assert quantize_scale(bucket) == bucket


def test_frozen_headroom_examples():
    assert quantize_scale(1.13) == 1.0
    assert quantize_scale(1.30) == 2.0


def test_headroom_boundaries():
    # Boundaries at bucket * HEADROOM: 0.625, 1.25, 2.5, 5.0 (exact hits round up).
    assert quantize_scale(0.62) == 0.5
    assert quantize_scale(0.625) == 1.0
    assert quantize_scale(1.24) == 1.0
    assert quantize_scale(1.25) == 2.0
    assert quantize_scale(2.49) == 2.0
    assert quantize_scale(2.5) == 4.0
    assert quantize_scale(4.99) == 4.0
    assert quantize_scale(5.0) == 8.0


def test_clamping():
    assert quantize_scale(0.01) == 0.5
    assert quantize_scale(100.0) == 8.0
    with pytest.raises(ValueError):
        quantize_scale(0.0)
    with pytest.raises(ValueError):
        quantize_scale(-1.0)


def test_hysteresis_holds_wiggle():
    # 1.0 -> 1.13 -> 1.0 renders zero new tiles (stays on bucket).
    assert next_bucket(1.13, 1.0) == 1.0
    assert next_bucket(0.95, 1.0) == 1.0
    assert next_bucket(1.12, 1.0) == 1.0


def test_hysteresis_transitions():
    assert next_bucket(1.30, 1.0) == 2.0
    assert next_bucket(0.60, 1.0) == 0.5
    assert next_bucket(3.0, 2.0) == 4.0
    with pytest.raises(ValueError):
        next_bucket(1.3, 1.3)


def test_deterministic_repeated():
    for zoom in (0.4, 1.13, 1.30, 2.7, 5.5, 9.0):
        assert quantize_scale(zoom) == quantize_scale(zoom)
        assert next_bucket(zoom, 2.0) == next_bucket(zoom, 2.0)


def test_no_float_fragmentation():
    seen = {quantize_scale(z / 100.0) for z in range(40, 901, 3)}
    assert seen == set(RASTER_BUCKETS)
