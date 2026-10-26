"""Lightweight diagnostics primitives (M0).

Provides generic timing and RSS helpers. Renderer metrics, FPS, queue
latency etc. are intentionally left for later milestones.
Stdlib only.
"""

from __future__ import annotations

import ctypes
import os
import time
from types import TracebackType


class Timer:
    """Simple elapsed-time helper usable as context manager.

    Example:
        with Timer() as t:
            do_work()
        print(t.elapsed)
    """

    def __init__(self) -> None:
        self._start: float = 0.0
        self.elapsed: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.elapsed = time.perf_counter() - self._start

    def reset(self) -> None:
        """Reset elapsed time to zero."""
        self._start = time.perf_counter()
        self.elapsed = 0.0


def measure(func):  # type: ignore[no-untyped-def]
    """Decorator variant could be added later; kept minimal for M0."""
    return func


def get_rss_mb() -> float:
    """Return current process RSS in MiB, or 0.0 if unavailable.

    Linux: reads /proc/self/status (VmRSS).
    Windows: uses GetProcessMemoryInfo via ctypes.
    No third-party dependencies (e.g. psutil) by design.
    """
    try:
        if os.name == "nt":
            return _windows_rss_mb()
        return _linux_rss_mb()
    except Exception:
        return 0.0


def _linux_rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    kb = float(parts[1])
                    return kb / 1024.0
    except OSError:
        pass
    return 0.0


def _windows_rss_mb() -> float:
    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_Counters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return 0.0
        return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
    except Exception:
        return 0.0
