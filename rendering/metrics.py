"""Renderer metrics (M2). Lock-guarded counters + latency samples.

Thread-safe: scheduler/engine record from any thread; readers get a
plain-dict snapshot. No UI, no Qt.

Samples are bounded: each latency collection keeps only the most recent
:data:`SAMPLE_LIMIT` renders. The counters remain all-time totals; the
latency summaries (count/avg/p50/p95/max) describe the recent window,
and ``snapshot()`` sorting stays bounded no matter how long the session
runs.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable

__all__ = ["RenderMetrics", "SAMPLE_LIMIT"]

#: Retained latency samples per metric. 2048 renders covers several
#: minutes of heavy scrolling; summaries remain statistically stable
#: (p50/p95) over that window while memory stays flat.
SAMPLE_LIMIT = 2048


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


class RenderMetrics:
    """Lightweight programmatic metrics for the rendering engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self.deduplicated = 0
        self.dropped_prefetch = 0
        self.queue_full = 0
        self.queue_wait_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.render_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.convert_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.total_ms: deque[float] = deque(maxlen=SAMPLE_LIMIT)
        self.active_renders = 0
        self.max_active_renders = 0

    def _latency_summary(self, vals: Iterable[float]) -> dict[str, float]:
        s = sorted(vals)
        n = len(s)
        return {
            "count": float(n),
            "avg": (sum(s) / n) if n else 0.0,
            "p50": _percentile(s, 50.0),
            "p95": _percentile(s, 95.0),
            "max": s[-1] if s else 0.0,
        }

    def snapshot(self) -> dict:
        """Return a plain-dict copy of all counters + latency summaries."""
        with self._lock:
            return {
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled,
                "deduplicated": self.deduplicated,
                "dropped_prefetch": self.dropped_prefetch,
                "queue_full": self.queue_full,
                "queue_wait_ms": self._latency_summary(self.queue_wait_ms),
                "render_ms": self._latency_summary(self.render_ms),
                "convert_ms": self._latency_summary(self.convert_ms),
                "total_ms": self._latency_summary(self.total_ms),
                "active_renders": self.active_renders,
                "max_active_renders": self.max_active_renders,
            }

    def render_started(self) -> None:
        """Record entry into the GIL-holding raster section (engine gate)."""
        with self._lock:
            self.active_renders += 1
            if self.active_renders > self.max_active_renders:
                self.max_active_renders = self.active_renders

    def render_finished(self) -> None:
        """Record exit from the raster section. Always paired; never negative."""
        with self._lock:
            self.active_renders -= 1
            if self.active_renders < 0:
                self.active_renders = 0
