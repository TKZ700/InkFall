"""Core foundation: IDs, geometry, coordinates.

Stdlib only. No NumPy, no UI, no PyMuPDF.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class DocumentId:
    """Unique identifier for a document."""

    value: str = field(default_factory=_new_id)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PageId:
    """Unique identifier for a page."""

    value: str = field(default_factory=_new_id)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AnnotationId:
    """Unique identifier for an annotation. Stable across sessions."""

    value: str = field(default_factory=_new_id)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ToolId:
    """Unique identifier for a tool definition/instance."""

    value: str = field(default_factory=_new_id)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Point:
    """2D point in an unspecified coordinate space.

    Callers must know which space this is in (screen/viewport/document/page).
    Document/page coordinates are canonical for persisted data.
    """

    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Size:
    """2D size (width x height). Values must be non-negative."""

    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError("Size dimensions must be non-negative")


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle defined by origin + size."""

    origin: Point
    size: Size

    @property
    def width(self) -> float:
        return self.size.width

    @property
    def height(self) -> float:
        return self.size.height

    @property
    def x0(self) -> float:
        return self.origin.x

    @property
    def y0(self) -> float:
        return self.origin.y

    @property
    def x1(self) -> float:
        return self.origin.x + self.size.width

    @property
    def y1(self) -> float:
        return self.origin.y + self.size.height

    def contains(self, point: Point) -> bool:
        """Return True if point lies inside (inclusive) the rect."""
        return self.x0 <= point.x <= self.x1 and self.y0 <= point.y <= self.y1


@dataclass(frozen=True, slots=True)
class Viewport:
    """Visible region mapping document space to screen.

    Coordinate flow (canonical order)::

        Screen -> Viewport -> Document -> Page

    Only Document/Page coordinates may be persisted. Screen coordinates
    must never be stored (zoom/scroll/DPI dependent).
    """

    size: Size
    scale: float = 1.0
    offset: Point = field(default_factory=lambda: Point(0.0, 0.0))
    rotation_degrees: float = 0.0

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError("Viewport scale must be positive")

    def document_to_viewport(self, point: Point) -> Point:
        """Map a document-space point into viewport space."""
        return Point(
            (point.x - self.offset.x) * self.scale,
            (point.y - self.offset.y) * self.scale,
        )

    def viewport_to_document(self, point: Point) -> Point:
        """Map a viewport-space point back into document space."""
        return Point(
            point.x / self.scale + self.offset.x,
            point.y / self.scale + self.offset.y,
        )


@dataclass(frozen=True, slots=True)
class Transform:
    """Scale + rotation + translation mapping between spaces."""

    scale: float = 1.0
    rotation_degrees: float = 0.0
    translation: Point = field(default_factory=lambda: Point(0.0, 0.0))

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError("Transform scale must be positive")

    def apply(self, point: Point) -> Point:
        """Apply scale then translation (rotation stored for future M1+ use)."""
        return Point(
            point.x * self.scale + self.translation.x,
            point.y * self.scale + self.translation.y,
        )
