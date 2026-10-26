"""Stateless tile rasterizer (M2). May import PyMuPDF (approved module).

Pipeline::

    page -> rotation+scale matrix -> clip rect -> pixmap -> app bytes

No Qt, no QPainter, no QGraphicsView. Usable without a GUI.

Rotation convention: clockwise degrees in {0,90,180,270}, matching PDF
display rotation. Tiles live in ROTATED render-pixel space (a 1000x1400pt
page at scale 1, rot 90 spans 1400x1000px). The tile is inverse-rotated
into unrotated page coordinates for the raster clip; the PyMuPDF matrix
(``prerotate``) applies the forward rotation. Content orientation is
covered by an end-to-end fixture test (dark-rectangle probe).

Intrinsic page rotation (``page.rotation``) is composed since M3-B via the
``intrinsic_rotation`` parameter: effective rotation is
``(intrinsic + request.rotation_degrees) % 360``. Callers that pass the
default ``0`` get exact M2 behavior.

Memory: exactly ONE documented copy — ``bytes(pix.samples)`` — then the
pixmap reference is dropped before the result is built. No pixmap, page,
or document object ever leaves this module.
"""

from __future__ import annotations

import time

import pymupdf

from core.types import Size
from document.errors import InvalidPageIndexError
from rendering.coordinates import (
    clip_tile_to_page,
    rotated_render_size,
    tile_to_page_clip,
)
from rendering.errors import RenderCancelledError, RenderFailedError
from rendering.types import CancellationToken, RenderRequest, RenderResult

__all__ = ["render_tile", "render_tile_raw", "PIXEL_FORMAT", "BYTES_PER_PIXEL"]

#: Public pixel contract.
PIXEL_FORMAT = "RGBA8"
BYTES_PER_PIXEL = 4


def render_tile(
    doc: pymupdf.Document,
    request: RenderRequest,
    page_width_pt: float,
    page_height_pt: float,
    token: CancellationToken | None = None,
    intrinsic_rotation: int = 0,
) -> RenderResult:
    """Render one tile to application-owned RGBA8 bytes (single copy)."""
    data, w, h, render_ms, convert_ms = render_tile_raw(
        doc, request, page_width_pt, page_height_pt, token,
        alpha=True, intrinsic_rotation=intrinsic_rotation,
    )
    assert data is not None
    return RenderResult(
        request=request,
        size=Size(float(w), float(h)),
        scale=request.scale,
        success=True,
        data=data,
        width_px=w,
        height_px=h,
        pixel_format=PIXEL_FORMAT,
        stride=w * BYTES_PER_PIXEL,
        generation=request.generation,
    )


def render_tile_raw(
    doc: pymupdf.Document,
    request: RenderRequest,
    page_width_pt: float,
    page_height_pt: float,
    token: CancellationToken | None = None,
    alpha: bool = True,
    intrinsic_rotation: int = 0,
) -> tuple[bytes | None, int, int, float, float]:
    """Internal raster core with selectable alpha (benchmark comparison).

    ``alpha=True`` is the RGBA8 contract path. ``alpha=False`` (RGB) exists
    ONLY for the M2 RGB-vs-RGBA benchmark; the public contract stays RGBA8.
    ``intrinsic_rotation`` is the page's PDF ``/Rotate`` (clockwise) that the
    caller has neutralized from the page object via ``set_rotation(0)``; the
    caller must therefore pass UNROTATED media dimensions. The effective
    rotation is ``(intrinsic_rotation + request.rotation_degrees) % 360``.
    The default ``0`` preserves exact M2 behavior.
    Returns ``(data, width_px, height_px, render_ms, convert_ms)``.
    """
    if token is not None and token.is_cancelled:
        raise RenderCancelledError("request cancelled before rasterization")

    effective = (int(intrinsic_rotation) + int(request.rotation_degrees)) % 360
    if effective not in (0, 90, 180, 270):
        raise RenderFailedError(f"unsupported effective rotation {effective}")

    full_w, full_h = rotated_render_size(
        Size(page_width_pt, page_height_pt), request.scale, effective
    )
    clipped = clip_tile_to_page(request.tile, full_w, full_h)
    if clipped is None:
        raise RenderFailedError(f"tile {request.tile.key()} lies outside {full_w}x{full_h}px page")

    # Tile coords are in ROTATED render space; the raster clip must be in
    # UNROTATED page coords (matrix handles rotation). Invert rotation.
    cx0, cy0, cx1, cy1 = _rotated_tile_to_page_clip(
        clipped, page_width_pt, page_height_pt, request.scale, effective
    )
    # Clamp to the media box (float safety at edges).
    cx0 = max(0.0, min(cx0, page_width_pt))
    cy0 = max(0.0, min(cy0, page_height_pt))
    cx1 = max(0.0, min(cx1, page_width_pt))
    cy1 = max(0.0, min(cy1, page_height_pt))
    if cx1 <= cx0 or cy1 <= cy0:
        raise RenderFailedError("empty page-space clip after clamping")

    try:
        page = doc[request.page_index]
    except IndexError as e:
        raise InvalidPageIndexError(f"Invalid page index {request.page_index!r}") from e
    except Exception as e:
        raise RenderFailedError(f"Cannot access page {request.page_index}: {e}") from e

    matrix = pymupdf.Matrix(request.scale, request.scale)
    matrix.prerotate(effective)
    clip = pymupdf.Rect(cx0, cy0, cx1, cy1)

    t0 = time.perf_counter()
    try:
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=alpha)
    except Exception as e:
        raise RenderFailedError(f"Rasterization failed (page {request.page_index}): {e}") from e
    render_ms = (time.perf_counter() - t0) * 1000.0

    if token is not None and token.is_cancelled:
        # Drop the raster work; never deliver it as success.
        pix = None  # type: ignore[assignment]
        raise RenderCancelledError("request cancelled after rasterization")

    t0 = time.perf_counter()
    try:
        w, h = int(pix.width), int(pix.height)
        # THE documented copy: pixmap samples -> application-owned bytes.
        data = bytes(pix.samples)
    except Exception as e:
        raise RenderFailedError(f"Pixel conversion failed: {e}") from e
    finally:
        pix = None  # type: ignore[assignment]  # release raster memory now
    convert_ms = (time.perf_counter() - t0) * 1000.0

    expected = w * h * (4 if alpha else 3)
    if len(data) != expected:
        raise RenderFailedError(f"pixel buffer size {len(data)} != expected {expected}")
    return data, w, h, render_ms, convert_ms


def _rotated_tile_to_page_clip(
    tile, page_w: float, page_h: float, scale: float, rotation: int
) -> tuple[float, float, float, float]:
    """Inverse-rotate a rotated-space tile into unrotated page points.

    Rotation is clockwise (PDF convention). Returns (x0, y0, x1, y1).
    """
    tx0, ty0 = tile.x / scale, tile.y / scale
    tx1, ty1 = (tile.x + tile.width) / scale, (tile.y + tile.height) / scale
    if rotation == 0:
        return tile_to_page_clip(tile, scale)
    if rotation == 90:
        # rotated(x,y) = (H - py, px)  =>  px in [ty0,ty1], py in [H-tx1, H-tx0]
        return (ty0, page_h - tx1, ty1, page_h - tx0)
    if rotation == 180:
        return (page_w - tx1, page_h - ty1, page_w - tx0, page_h - ty0)
    if rotation == 270:
        return (page_w - ty1, tx0, page_w - ty0, tx1)
    raise ValueError(f"unsupported rotation {rotation}")
