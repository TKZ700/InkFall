"""Render request/result types (M2).

Backward-compatible extension of the M0 structures: every new field has a
safe default so existing ``RenderRequest(page_index=..., target_size=...,
scale=...)`` construction keeps working.

Coordinate contract (canonical)::

    PDF page coords (unrotated media-box, points)
        -> Rotated page coords (rotation applied, points)
        -> Render coords (x scale, pixels)
        -> Tile coords (tile rect in render pixels)
        -> Screen coords (UI mapping, NOT persisted)

Rotation and scale are always explicit. DPI is never hidden: render pixels
= page points x scale (72pt baseline). Later UI code translates zoom/DPI
into render scale. See ``rendering/coordinates.py`` for the math.

Pixel contract: RGBA8, 4 bytes/px, row-major, stride = width*4. The alpha
channel is preserved from rasterization (0 = transparent page background;
PDF pages have no paper color). Exactly one documented pixmap -> buffer
copy in ``renderer.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.types import DocumentId, PageId, Size

#: Initial tile size default (configurable via EngineConfig, not hardcoded).
DEFAULT_TILE_SIZE = 512

#: Allowed rotation values (degrees).
ALLOWED_ROTATIONS = (0, 90, 180, 270)


class RenderPriority(str, Enum):
    """Scheduling priority. VISIBLE always beats PREFETCH."""

    VISIBLE = "visible"
    PREFETCH = "prefetch"


@dataclass(frozen=True, slots=True)
class TileRect:
    """Tile rectangle in render pixels (origin + size)."""

    x: int = 0
    y: int = 0
    width: int = DEFAULT_TILE_SIZE
    height: int = DEFAULT_TILE_SIZE

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError("tile origin must be non-negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("tile dimensions must be positive")

    def key(self) -> tuple[int, int, int, int]:
        """Hashable identity of the tile rect."""
        return (self.x, self.y, self.width, self.height)


def _new_request_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class TileKey:
    """Immutable, hashable identity of one (clipped) tile request.

    Includes clipped ``tile_w``/``tile_h`` so edge tiles are distinct keys.
    Derives from the same fields as :meth:`RenderRequest.key` — see
    :meth:`from_request` for the ONE canonical derivation path.

    ``rotation`` is the additional (user/display) rotation applied at render
    time; the effective orientation is ``(intrinsic + rotation) % 360`` where
    ``intrinsic`` is the page's PDF ``/Rotate``. Per page this maps 1:1 to
    rendered pixels, so the key stays cache-correct.
    """

    document_id: DocumentId = field(default_factory=DocumentId)
    page_id: PageId = field(default_factory=PageId)
    generation: int = 0
    scale: float = 1.0
    rotation: int = 0
    tile_x: int = 0
    tile_y: int = 0
    tile_w: int = DEFAULT_TILE_SIZE
    tile_h: int = DEFAULT_TILE_SIZE

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        if int(self.rotation) not in ALLOWED_ROTATIONS:
            raise ValueError(f"rotation must be one of {ALLOWED_ROTATIONS}")
        if self.tile_x < 0 or self.tile_y < 0:
            raise ValueError("tile origin must be non-negative")
        if self.tile_w <= 0 or self.tile_h <= 0:
            raise ValueError("tile dimensions must be positive")

    def to_tile_rect(self) -> TileRect:
        """Return this key's tile geometry as a :class:`TileRect`."""
        return TileRect(self.tile_x, self.tile_y, self.tile_w, self.tile_h)

    def to_tuple(self) -> tuple:
        """Hashable tuple shared with :meth:`RenderRequest.key`."""
        return (
            self.document_id.value,
            self.page_id.value,
            self.scale,
            int(self.rotation),
            (self.tile_x, self.tile_y, self.tile_w, self.tile_h),
            self.generation,
        )

    @classmethod
    def from_request(cls, request: RenderRequest) -> TileKey:
        """Derive the canonical key for a render request (single path)."""
        return cls(
            document_id=request.document_id,
            page_id=request.page_id,
            generation=request.generation,
            scale=request.scale,
            rotation=int(request.rotation_degrees),
            tile_x=request.tile.x,
            tile_y=request.tile.y,
            tile_w=request.tile.width,
            tile_h=request.tile.height,
        )


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Immutable tile render request.

    Two visually identical requests from different generations are
    distinguishable via ``generation`` (part of :meth:`key`).

    ``rotation_degrees`` is the additional user/display rotation (clockwise);
    renderers compose it with the page's intrinsic PDF ``/Rotate`` as
    ``(intrinsic + rotation_degrees) % 360``.
    """

    document_id: DocumentId = field(default_factory=DocumentId)
    page_id: PageId = field(default_factory=PageId)
    page_index: int = 0
    target_size: Size = field(default_factory=lambda: Size(800.0, 600.0))
    scale: float = 1.0
    rotation_degrees: int = 0
    tile: TileRect = field(default_factory=TileRect)
    generation: int = 0
    request_id: str = field(default_factory=_new_request_id)
    priority: RenderPriority = RenderPriority.VISIBLE
    source_path: str | None = None

    def __post_init__(self) -> None:
        if self.page_index < 0:
            raise ValueError("page_index must be non-negative")
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        if int(self.rotation_degrees) not in ALLOWED_ROTATIONS:
            raise ValueError(f"rotation must be one of {ALLOWED_ROTATIONS}")
        if self.generation < 0:
            raise ValueError("generation must be non-negative")

    def key(self) -> tuple:
        """Deduplication key: doc, page, scale, rotation, tile, generation.

        Derived through the canonical :class:`TileKey` path (plus the
        request's ``page_index`` scope). ``request_id``, ``priority`` and
        ``source_path`` are excluded: they do not change the pixels.
        Different generations never dedup.
        """
        tile_key = TileKey.from_request(self)
        doc_value, page_value, scale, rotation, tile_tuple, generation = tile_key.to_tuple()
        return (
            doc_value,
            page_value,
            self.page_index,
            scale,
            rotation,
            tile_tuple,
            generation,
        )


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Immutable, application-owned render result.

    ``data`` is ``bytes`` (RGBA8, ``width_px * height_px * 4``) on success,
    ``None`` on failure/cancellation. The pixmap is always released before
    this object is constructed.
    """

    request: RenderRequest = field(default_factory=RenderRequest)
    size: Size = field(default_factory=lambda: Size(800.0, 600.0))
    scale: float = 1.0
    success: bool = True
    data: bytes | None = None
    width_px: int = 0
    height_px: int = 0
    pixel_format: str = "RGBA8"
    stride: int = 0
    generation: int = 0
    queue_wait_ms: float = 0.0
    render_ms: float = 0.0
    convert_ms: float = 0.0
    total_ms: float = 0.0
    error: str | None = None


def is_stale(result: RenderResult, expected_generation: int) -> bool:
    """True if a result must not be accepted by the consumer."""
    return result.generation != expected_generation


class CancellationToken:
    """Cooperative cancellation flag shared between scheduler and workers."""

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()

    def cancel(self) -> None:
        """Mark the associated request as cancelled (idempotent)."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """True after :meth:`cancel`."""
        return self._event.is_set()
