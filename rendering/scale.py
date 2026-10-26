"""Raster scale quantization (M3-B). Pure policy, no Qt, no PyMuPDF.

Three levels stay separate:

- continuous UI zoom (any positive float; drives layout + display),
- raster scale (quantized bucket; drives TileKey.scale + rendering),
- cache key (raster scale only — see ``rendering/cache.py``).

Buckets ``{0.5, 1.0, 2.0, 4.0, 8.0}`` with 25% upward headroom plus a
+-12% hysteresis band around the CURRENT bucket: while a current bucket is
held, UI zoom must leave the band before a new bucket is selected. This
prevents pinch-zoom jitter from thrashing the cache (a 1.0 -> 1.13 -> 1.0
wiggle renders zero new tiles). ``quantize_scale`` is stateless (no memory
of the current bucket); :func:`next_bucket` applies hysteresis given the
current bucket. Both are deterministic: repeated calls are identical.
"""

from __future__ import annotations

__all__ = [
    "RASTER_BUCKETS",
    "HEADROOM",
    "HYSTERESIS",
    "MIN_RASTER_SCALE",
    "MAX_RASTER_SCALE",
    "quantize_scale",
    "next_bucket",
]

#: Quantized raster scales (powers of two covering 50%..800%).
RASTER_BUCKETS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)

#: Headroom factor: UI zoom maps to the lowest bucket whose
#: ``bucket * HEADROOM`` still covers it. This favors lower-resolution
#: rasters (memory/bandwidth win) while capping worst-case upscale blur at
#: 25% linear. Exact thresholds are pinned by tests — do not change
#: silently; report any change with benchmark evidence.
HEADROOM = 1.25

#: Hysteresis half-band: stay on the current bucket while UI zoom is within
#: +-12% of it.
HYSTERESIS = 0.12

MIN_RASTER_SCALE = RASTER_BUCKETS[0]
MAX_RASTER_SCALE = RASTER_BUCKETS[-1]


def _check_zoom(ui_zoom: float) -> float:
    zoom = float(ui_zoom)
    if not zoom > 0:
        raise ValueError("ui_zoom must be positive")
    return zoom


def quantize_scale(ui_zoom: float) -> float:
    """Map UI zoom to the lowest bucket whose headroom covers it.

    Boundaries sit at ``bucket * HEADROOM`` (0.625, 1.25, 2.5, 5.0), so
    1.13 stays on 1.0 while 1.30 moves to 2.0. Values below/above the
    ladder clamp to the ends.
    """
    zoom = _check_zoom(ui_zoom)
    for bucket in RASTER_BUCKETS:
        if zoom < bucket * HEADROOM:
            return bucket
    return MAX_RASTER_SCALE


def next_bucket(ui_zoom: float, current: float) -> float:
    """Apply hysteresis: keep ``current`` while zoom stays in its band.

    ``current`` must be a valid bucket (values are validated, not snapped:
    an unknown current raises). Returns the (possibly unchanged) bucket.
    """
    zoom = _check_zoom(ui_zoom)
    current_f = float(current)
    if current_f not in RASTER_BUCKETS:
        raise ValueError(f"current must be one of {RASTER_BUCKETS}")
    lo = current_f * (1.0 - HYSTERESIS)
    hi = current_f * (1.0 + HYSTERESIS)
    if lo <= zoom <= hi:
        return current_f
    return quantize_scale(zoom)
