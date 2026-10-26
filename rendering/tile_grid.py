"""Deterministic tile-grid planning (M3-A). Pure functions only.

No I/O, no renderer calls, no scheduler calls, no futures, no bitmaps.
No PyMuPDF, no Qt. All coordinates are integers once they leave the
floating-point scroll origin: ``floor`` maps scroll to tile indices, and
every emitted tile is clipped to the rotated page bounds.

Hard cap: total planned tiles never exceed ``visible + 8`` (prefetch is
truncated in deterministic row-major order), so the future scheduler can
never be flooded by planning.
"""

from __future__ import annotations

import math

from core.types import Size
from rendering.coordinates import clip_tile_to_page, rotated_render_size
from rendering.types import (
    DEFAULT_TILE_SIZE,
    RenderPriority,
    RenderRequest,
    TileKey,
    TileRect,
)
from rendering.viewport import ViewportState

__all__ = [
    "MAX_PREFETCH_TILES",
    "page_render_size",
    "plan_requests",
    "prefetch_tiles",
    "visible_tile_range",
    "visible_tiles",
]

#: Hard cap: prefetch never plans more than this many tiles per call, so
#: total planned output is at most ``visible + MAX_PREFETCH_TILES``.
MAX_PREFETCH_TILES = 8


def _check_tile_size(tile_size: int) -> int:
    size = int(tile_size)
    if size <= 0:
        raise ValueError("tile_size must be positive")
    return size


def page_render_size(
    page_width_pt: float, page_height_pt: float, scale: float, rotation: int
) -> tuple[int, int]:
    """Full-page render dimensions in pixels AFTER rotation.

    Delegates to the existing M2 :func:`rotated_render_size` logic — the
    single source of truth for the coordinate contract.
    """
    return rotated_render_size(Size(page_width_pt, page_height_pt), scale, rotation)


def visible_tile_range(
    viewport: ViewportState,
    page_width_px: int,
    page_height_px: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> tuple[int, int, int, int] | None:
    """Inclusive ``(ix0, iy0, ix1, iy1)`` tile-index range, or None if empty.

    Floor-based: ``ix0 = floor(scroll_x / tile)`` … ``ix1 =
    floor((scroll_x + viewport_w - 1) / tile)``, clamped to the page grid.
    A viewport completely outside the page yields None. Indices are never
    negative.
    """
    size = _check_tile_size(tile_size)
    if page_width_px <= 0 or page_height_px <= 0:
        return None
    x0 = viewport.scroll_x
    y0 = viewport.scroll_y
    x1 = viewport.scroll_x + viewport.viewport_width_px
    y1 = viewport.scroll_y + viewport.viewport_height_px
    if x1 <= 0 or y1 <= 0 or x0 >= page_width_px or y0 >= page_height_px:
        return None
    tiles_x = max(1, math.ceil(page_width_px / size))
    tiles_y = max(1, math.ceil(page_height_px / size))
    ix0 = max(0, math.floor(x0 / size))
    iy0 = max(0, math.floor(y0 / size))
    ix1 = min(tiles_x - 1, math.floor((x1 - 1) / size))
    iy1 = min(tiles_y - 1, math.floor((y1 - 1) / size))
    if ix1 < ix0 or iy1 < iy0:
        return None
    return (ix0, iy0, ix1, iy1)


def _grid_cell_keys(
    viewport: ViewportState,
    page_width_px: int,
    page_height_px: int,
    tile_size: int,
    ix0: int,
    iy0: int,
    ix1: int,
    iy1: int,
) -> list[TileKey]:
    """Build clipped keys for grid cells in row-major order, skipping outside."""
    keys: list[TileKey] = []
    for iy in range(iy0, iy1 + 1):
        for ix in range(ix0, ix1 + 1):
            cell = TileRect(ix * tile_size, iy * tile_size, tile_size, tile_size)
            clipped = clip_tile_to_page(cell, page_width_px, page_height_px)
            if clipped is None:
                continue
            keys.append(
                TileKey(
                    document_id=viewport.document_id,
                    page_id=viewport.page_id,
                    generation=viewport.generation,
                    scale=viewport.scale,
                    rotation=int(viewport.rotation),
                    tile_x=clipped.x,
                    tile_y=clipped.y,
                    tile_w=clipped.width,
                    tile_h=clipped.height,
                )
            )
    return keys


def visible_tiles(
    viewport: ViewportState,
    page_width_px: int,
    page_height_px: int,
    tile_size: int = DEFAULT_TILE_SIZE,
) -> tuple[TileKey, ...]:
    """Clipped visible tile keys in deterministic row-major order."""
    size = _check_tile_size(tile_size)
    box = visible_tile_range(viewport, page_width_px, page_height_px, size)
    if box is None:
        return ()
    return tuple(_grid_cell_keys(viewport, page_width_px, page_height_px, size, *box))


def prefetch_tiles(
    viewport: ViewportState,
    page_width_px: int,
    page_height_px: int,
    tile_size: int = DEFAULT_TILE_SIZE,
    margin_tiles: int = 1,
    visible: tuple[TileKey, ...] | None = None,
) -> tuple[TileKey, ...]:
    """One-ring prefetch around the visible range (default margin 1).

    Excludes visible tiles and out-of-bounds cells, preserves row-major
    order, and truncates to ``MAX_PREFETCH_TILES`` so planning output is at
    most ``visible + 8``. ``margin_tiles=0`` returns no prefetch.
    """
    size = _check_tile_size(tile_size)
    if margin_tiles < 0:
        raise ValueError("margin_tiles must be non-negative")
    if margin_tiles == 0:
        return ()
    box = visible_tile_range(viewport, page_width_px, page_height_px, size)
    if box is None:
        return ()
    if visible is None:
        visible = visible_tiles(viewport, page_width_px, page_height_px, size)
    seen = set(visible)
    ix0, iy0, ix1, iy1 = box
    tiles_x = max(1, math.ceil(page_width_px / size))
    tiles_y = max(1, math.ceil(page_height_px / size))
    ring = _grid_cell_keys(
        viewport,
        page_width_px,
        page_height_px,
        size,
        max(0, ix0 - margin_tiles),
        max(0, iy0 - margin_tiles),
        min(tiles_x - 1, ix1 + margin_tiles),
        min(tiles_y - 1, iy1 + margin_tiles),
    )
    out = [key for key in ring if key not in seen]
    return tuple(out[:MAX_PREFETCH_TILES])


def plan_requests(
    viewport: ViewportState,
    page_size: Size,
    tile_size: int = DEFAULT_TILE_SIZE,
    prefetch_margin: int = 1,
    source_path: str | None = None,
) -> tuple[tuple[RenderRequest, ...], tuple[RenderRequest, ...]]:
    """Plan ``(visible, prefetch)`` render requests for a viewport.

    Pure function: identical input always yields identical output. Carries
    document/page/index, scale (= zoom), rotation, clipped tile geometry,
    generation, priority and source path into every request. Creates no
    futures and touches no renderer/scheduler.
    """
    size = _check_tile_size(tile_size)
    page_w_px, page_h_px = page_render_size(
        page_size.width, page_size.height, viewport.scale, int(viewport.rotation)
    )
    visible_keys = visible_tiles(viewport, page_w_px, page_h_px, size)
    prefetch_keys = prefetch_tiles(
        viewport, page_w_px, page_h_px, size, prefetch_margin, visible_keys
    )

    def _to_request(key: TileKey, priority: RenderPriority) -> RenderRequest:
        rect = key.to_tile_rect()
        return RenderRequest(
            document_id=key.document_id,
            page_id=key.page_id,
            page_index=viewport.page_index,
            target_size=Size(float(rect.width), float(rect.height)),
            scale=key.scale,
            rotation_degrees=key.rotation,
            tile=rect,
            generation=key.generation,
            priority=priority,
            source_path=source_path,
        )

    visible = tuple(_to_request(key, RenderPriority.VISIBLE) for key in visible_keys)
    prefetch = tuple(_to_request(key, RenderPriority.PREFETCH) for key in prefetch_keys)
    return (visible, prefetch)
