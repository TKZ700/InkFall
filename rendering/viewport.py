"""Page-space viewport model (M3-A). Pure data, no Qt, no PyMuPDF.

Coordinate frame (rotated render pixels)::

    origin (0, 0) ............ page_width_px
      .  viewport rect            .
      .  [scroll_x, scroll_x + viewport_width_px]
      .  [scroll_y, scroll_y + viewport_height_px]
      .                           .
      ............................ (page_width_px, page_height_px)

- ``scroll_x``/``scroll_y`` are the TOP-LEFT viewport origin in rotated
  render pixels (same space as tile coordinates).
- Scroll may temporarily be NEGATIVE (overscroll); tile math clamps it.
  Tile coordinates themselves are ALWAYS non-negative integer indices —
  floating-point scroll values must never directly become tile coordinates.
- This model describes what is visible on ONE page. It introduces NO
  persisted coordinates (canonical document/page coordinates are unchanged;
  see the M2 contract in ``rendering/types.py``).
- Unrelated to ``core.types.Viewport`` (M0, document-space); that type is
  intentionally left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.types import DocumentId, PageId
from rendering.types import ALLOWED_ROTATIONS

__all__ = ["ViewportState"]


@dataclass(frozen=True, slots=True)
class ViewportState:
    """Immutable description of the visible region of one page."""

    document_id: DocumentId = field(default_factory=DocumentId)
    page_id: PageId = field(default_factory=PageId)
    page_index: int = 0
    viewport_width_px: int = 800
    viewport_height_px: int = 600
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    scale: float = 1.0
    rotation: int = 0
    generation: int = 0

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("page_index must be non-negative")
        if self.viewport_width_px <= 0 or self.viewport_height_px <= 0:
            raise ValueError("viewport dimensions must be positive")
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        if int(self.rotation) not in ALLOWED_ROTATIONS:
            raise ValueError(f"rotation must be one of {ALLOWED_ROTATIONS}")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
