"""Qt-free viewport geometry for the PDF viewer (M3-C).

Pure helpers mapping between the three pixel spaces:

- layout/render px (raster bucket integers from ``DocumentLayout``),
- logical screen px (Qt widget coordinates),
- device px (physical pixels).

Single conversion rule: ``logical = layout × display_scale`` where
``display_scale = ui_zoom / raster_bucket``, and
``device = logical × dpr`` applied EXACTLY ONCE at the final step.
All math stays float until :func:`to_int_rect` constructs the integer
rectangle Qt paints. No QWidget/QImage/QPainter/fitz imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "FloatRect",
    "IntRect",
    "display_scale_for",
    "render_viewport_size",
    "page_screen_rect",
    "tile_screen_rect",
    "apply_dpr",
    "to_int_rect",
    "scroll_fraction",
    "scroll_from_fraction",
    "split_current_stale",
]


@dataclass(frozen=True, slots=True)
class FloatRect:
    """Rectangle with float coordinates (intermediate geometry)."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class IntRect:
    """Integer rectangle ready for Qt painting."""

    x: int
    y: int
    width: int
    height: int


def display_scale_for(ui_zoom: float, raster_bucket: float) -> float:
    """Display factor mapping layout px to logical screen px."""
    if ui_zoom <= 0 or raster_bucket <= 0:
        raise ValueError("ui_zoom and raster_bucket must be positive")
    return float(ui_zoom) / float(raster_bucket)


def render_viewport_size(
    viewport_logical_w: float, viewport_logical_h: float, display_scale: float
) -> tuple[float, float]:
    """Viewport size in render px (what ``pipeline.update`` consumes)."""
    if display_scale <= 0:
        raise ValueError("display_scale must be positive")
    return (viewport_logical_w / display_scale, viewport_logical_h / display_scale)


def page_screen_rect(
    page_offset_px: float,
    page_width_px: float,
    page_height_px: float,
    scroll_y_doc: float,
    viewport_width_render_px: float,
    display_scale: float,
    scroll_x_doc: float = 0.0,
    content_width_px: float | None = None,
) -> FloatRect:
    """Logical screen rect of one page: centered if narrow, else left-aligned.

    All inputs except the output are render-px floats; the result is logical.

    M5-B.1.2: ``scroll_x_doc`` is the document-space horizontal viewport
    origin in render px and ``content_width_px`` the document content
    width in render px (``None`` reproduces the legacy viewport-only
    centering). The page origin ``max(0, (C - page_w) / 2)`` with
    ``C = max(viewport_w, content_w)`` is continuous across the
    fits/scrollable boundary: identical to the legacy formula when the
    document fits, content-centered when scrollable. Must stay in sync
    with the mirror computation in ``rendering.layout`` (page origin
    for input mapping and tile planning).
    """
    extent = float(viewport_width_render_px)
    if content_width_px is not None:
        extent = max(extent, float(content_width_px))
    x_render = max(0.0, (extent - page_width_px) / 2.0) - float(scroll_x_doc)
    return FloatRect(
        x=x_render * display_scale,
        y=(page_offset_px - scroll_y_doc) * display_scale,
        width=page_width_px * display_scale, height=page_height_px * display_scale,
    )


def tile_screen_rect(
    tile_x: int,
    tile_y: int,
    tile_w: int,
    tile_h: int,
    page_screen: FloatRect,
    display_scale: float,
) -> FloatRect:
    """Logical screen rect of a page-local tile (tile coords are render px)."""
    return FloatRect(
        x=page_screen.x + tile_x * display_scale,
        y=page_screen.y + tile_y * display_scale,
        width=tile_w * display_scale,
        height=tile_h * display_scale,
    )


def placeholder_rect(tile_rect: FloatRect) -> FloatRect:
    """Placeholder geometry for a missing tile (identical to tile geometry)."""
    return FloatRect(
        x=tile_rect.x, y=tile_rect.y, width=tile_rect.width, height=tile_rect.height
    )


def apply_dpr(rect: FloatRect, dpr: float) -> FloatRect:
    """Map logical px to device px. Applied exactly once, at the very end."""
    if dpr <= 0:
        raise ValueError("dpr must be positive")
    return FloatRect(
        x=rect.x * dpr, y=rect.y * dpr, width=rect.width * dpr, height=rect.height * dpr
    )


def to_int_rect(rect: FloatRect) -> IntRect:
    """Round a float rect to integers (single rounding point for painting)."""
    return IntRect(
        x=int(math.floor(rect.x + 0.5)),
        y=int(math.floor(rect.y + 0.5)),
        width=max(0, int(math.floor(rect.width + 0.5))),
        height=max(0, int(math.floor(rect.height + 0.5))),
    )


def snapped_tile_rect(
    tile_x: int,
    tile_y: int,
    tile_w: int,
    tile_h: int,
    page_rect: FloatRect,
    display_scale: float,
) -> IntRect:
    """Tile target in logical px with shared-edge snapping.

    Each edge is rounded independently; widths/heights are derived from
    edge differences. Two adjacent tiles therefore compute the SAME screen
    coordinate for their shared edge (right of A and left of B round the
    same float), so no 1 px white gap or doubled seam can appear.
    """
    left = int(math.floor(page_rect.x + tile_x * display_scale + 0.5))
    right = int(math.floor(page_rect.x + (tile_x + tile_w) * display_scale + 0.5))
    top = int(math.floor(page_rect.y + tile_y * display_scale + 0.5))
    bottom = int(math.floor(page_rect.y + (tile_y + tile_h) * display_scale + 0.5))
    return IntRect(x=left, y=top, width=max(0, right - left), height=max(0, bottom - top))


def scroll_fraction(scroll_y: float, total_height: float) -> float:
    """Scrollbar position as a fraction (preserved across layout rebuilds)."""
    if total_height <= 0:
        return 0.0
    return min(1.0, max(0.0, scroll_y / total_height))


def scroll_from_fraction(fraction: float, total_height: float) -> float:
    """Restore a scroll position from a stored fraction."""
    return min(1.0, max(0.0, fraction)) * total_height


def split_current_stale(wanted: set, have: dict):
    """Partition display-map keys into (keep, drop).

    Pure helper so stale/out-of-scope pruning is unit-testable.
    """
    keep = {k: v for k, v in have.items() if k in wanted}
    drop = [k for k in have if k not in wanted]
    return keep, drop
