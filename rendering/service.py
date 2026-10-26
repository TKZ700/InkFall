"""Lazy renderer service boundary (M2).

The application may own a renderer, but no worker threads start until the
first ``submit``. Importing this module (or creating the application) is
free of background work.
"""

from __future__ import annotations

import threading

from rendering.engine import RendererEngine
from rendering.scheduler import EngineConfig

__all__ = ["get_engine", "shutdown_engine"]

_lock = threading.Lock()
_engine: RendererEngine | None = None


def get_engine(config: EngineConfig | None = None) -> RendererEngine:
    """Return the shared engine, creating it lazily on first call."""
    global _engine
    with _lock:
        if _engine is None:
            _engine = RendererEngine(config)
        return _engine


def shutdown_engine() -> None:
    """Shut down and drop the shared engine (idempotent)."""
    global _engine
    with _lock:
        engine, _engine = _engine, None
    if engine is not None:
        engine.shutdown()
