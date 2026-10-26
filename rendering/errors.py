"""Renderer-level errors (M2). All derive from RenderError."""

from core.errors import RenderError


class RenderCancelledError(RenderError):
    """A render request was cancelled before producing a result."""


class QueueFullError(RenderError):
    """Bounded queue refused a VISIBLE request (PREFETCH is dropped instead)."""


class RenderFailedError(RenderError):
    """Rasterization/conversion failed (invalid doc/page, backend error)."""
