"""Rendering package: async tile engine + viewing pipeline (M2/M3-B). No Qt inside rendering/*."""

from rendering.cache import (
    DEFAULT_CACHE_BUDGET_BYTES,
    CachedTile,
    TileCache,
    cache_identity,
)
from rendering.coordinates import (
    clip_tile_to_page,
    render_page_size,
    rotated_page_size,
    rotated_render_size,
    tile_to_page_clip,
)
from rendering.engine import RendererEngine
from rendering.errors import QueueFullError, RenderCancelledError, RenderFailedError
from rendering.layout import DocumentLayout, PageLayoutEntry, effective_rotation, unrotated_size
from rendering.metrics import RenderMetrics
from rendering.pipeline import DocumentPipeline, VISIBLE_HEADROOM, superseded_prefetch
from rendering.scale import (
    HEADROOM,
    HYSTERESIS,
    MAX_RASTER_SCALE,
    MIN_RASTER_SCALE,
    RASTER_BUCKETS,
    next_bucket,
    quantize_scale,
)
from rendering.scheduler import EngineConfig, Scheduler
from rendering.service import get_engine, shutdown_engine
from rendering.tile_grid import (
    MAX_PREFETCH_TILES,
    page_render_size,
    plan_requests,
    prefetch_tiles,
    visible_tile_range,
    visible_tiles,
)
from rendering.types import (
    DEFAULT_TILE_SIZE,
    CancellationToken,
    RenderPriority,
    RenderRequest,
    RenderResult,
    TileKey,
    TileRect,
    is_stale,
)
from rendering.viewport import ViewportState

__all__ = [
    "DEFAULT_TILE_SIZE",
    "DEFAULT_CACHE_BUDGET_BYTES",
    "HEADROOM",
    "HYSTERESIS",
    "MAX_PREFETCH_TILES",
    "MAX_RASTER_SCALE",
    "MIN_RASTER_SCALE",
    "RASTER_BUCKETS",
    "CachedTile",
    "CancellationToken",
    "DocumentLayout",
    "DocumentPipeline",
    "EngineConfig",
    "PageLayoutEntry",
    "QueueFullError",
    "RendererEngine",
    "RenderCancelledError",
    "RenderFailedError",
    "RenderMetrics",
    "RenderPriority",
    "RenderRequest",
    "RenderResult",
    "Scheduler",
    "TileCache",
    "TileKey",
    "TileRect",
    "VISIBLE_HEADROOM",
    "ViewportState",
    "cache_identity",
    "clip_tile_to_page",
    "effective_rotation",
    "get_engine",
    "is_stale",
    "next_bucket",
    "page_render_size",
    "plan_requests",
    "prefetch_tiles",
    "quantize_scale",
    "render_page_size",
    "rotated_page_size",
    "rotated_render_size",
    "shutdown_engine",
    "superseded_prefetch",
    "tile_to_page_clip",
    "unrotated_size",
    "visible_tile_range",
    "visible_tiles",
]
