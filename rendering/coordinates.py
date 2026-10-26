"""Tile coordinate math (M2). Pure functions, no PyMuPDF, no Qt.

Contract::

    PDF page coords  (unrotated media-box, points, origin top-left)
        -> Rotated page coords (rotation r in {0,90,180,270}, points)
        -> Render coords      (x scale, pixels: page_pt * scale)
        -> Tile coords        (TileRect in render pixels, clipped to page)
        -> Screen coords      (UI mapping; NOT handled here)

The renderer applies rotation via the PyMuPDF matrix (``prerotate``), so
the raster clip is expressed in UNROTATED page coordinates: the tile rect
is mapped back through scale only, and rotation is left to the matrix.
``rotated_render_size`` gives the full-page pixel dimensions AFTER
rotation (used to validate 90/270 dimension swaps and to clip edge tiles).
"""

from __future__ import annotations

from core.types import Size
from rendering.types import ALLOWED_ROTATIONS, TileRect

__all__ = [
    "rotated_page_size",
    "rotated_render_size",
    "render_page_size",
    "tile_to_page_clip",
    "clip_tile_to_page",
]


def _check_rotation(rotation: int) -> int:
    r = int(rotation)
    if r not in ALLOWED_ROTATIONS:
        raise ValueError(f"rotation must be one of {ALLOWED_ROTATIONS}")
    return r


def rotated_page_size(page_size: Size, rotation: int) -> Size:
    """Page size in points AFTER rotation (90/270 swap w/h)."""
    _check_rotation(rotation)
    if int(rotation) in (90, 270):
        return Size(page_size.height, page_size.width)
    return Size(page_size.width, page_size.height)


def render_page_size(page_size: Size, scale: float) -> tuple[int, int]:
    """Full-page render dimensions in pixels (unrotated)."""
    if scale <= 0:
        raise ValueError("scale must be positive")
    w = max(1, int(round(page_size.width * scale)))
    h = max(1, int(round(page_size.height * scale)))
    return (w, h)


def rotated_render_size(page_size: Size, scale: float, rotation: int) -> tuple[int, int]:
    """Full-page render dimensions in pixels AFTER rotation.

    Example: 1000x1400pt @ scale 1, rot 90 -> (1400, 1000).
    """
    r_page = rotated_page_size(page_size, rotation)
    return render_page_size(r_page, scale)


def tile_to_page_clip(tile: TileRect, scale: float) -> tuple[float, float, float, float]:
    """Map a tile rect (render px) to an unrotated page-space clip (points).

    Returns ``(x0, y0, x1, y1)`` in page points. Rotation is NOT applied
    here — the raster matrix handles it. Scale inversion only.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    return (
        tile.x / scale,
        tile.y / scale,
        (tile.x + tile.width) / scale,
        (tile.y + tile.height) / scale,
    )


def clip_tile_to_page(tile: TileRect, full_width_px: int, full_height_px: int) -> TileRect | None:
    """Clip a tile to the full-page render bounds. None if fully outside."""
    x0 = max(0, tile.x)
    y0 = max(0, tile.y)
    x1 = min(full_width_px, tile.x + tile.width)
    y1 = min(full_height_px, tile.y + tile.height)
    if x1 <= x0 or y1 <= y0:
        return None
    return TileRect(x0, y0, x1 - x0, y1 - y0)
