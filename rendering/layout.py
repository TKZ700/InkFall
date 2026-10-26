"""Multi-page document layout (M3-B). Pure geometry, no bitmaps, no Qt.

Pages are stacked vertically with a uniform inter-page gap. Each page's
render size is computed for one quantized raster scale and one user
rotation, composed per page with that page's intrinsic PDF rotation::

    effective_rotation = (intrinsic_rotation + user_rotation) % 360

Page offsets are prefix sums of ``render_height + gap``; visible-page
lookup is ``bisect`` over the offset table — O(log N) regardless of page
count. The layout object is immutable: zoom/rotation changes rebuild it
(O(N) scalar math, sub-ms for hundreds of pages) instead of mutating it.

Holds no rendered pixels: ~7 numbers per page (a 600-page layout is tens
of kilobytes). Only pages intersecting the viewport (plus a small page
prefetch margin) ever reach tile planning.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from core.types import DocumentId, PageId
from document.models import page_id_for
from rendering.coordinates import rotated_render_size
from rendering.types import ALLOWED_ROTATIONS, Size
from rendering.viewport import ViewportState

__all__ = ["PageLayoutEntry", "DocumentLayout", "effective_rotation", "unrotated_size"]


def effective_rotation(intrinsic_rotation: int, user_rotation: int) -> int:
    """Compose intrinsic PDF rotation with user/display rotation (clockwise).

    Both inputs must be in {0, 90, 180, 270}; the result is normalized to
    the same set, e.g. (90, 270) -> 0, (270, 270) -> 180.
    """
    total = (int(intrinsic_rotation) + int(user_rotation)) % 360
    if total not in ALLOWED_ROTATIONS:
        raise ValueError(f"effective rotation {total} not in {ALLOWED_ROTATIONS}")
    return total


def unrotated_size(width: float, height: float, rotation: int) -> tuple[float, float]:
    """Derotate PyMuPDF-rect dimensions back to unrotated media dimensions.

    PyMuPDF pre-rotates ``page.rect`` by the intrinsic ``/Rotate`` (verified
    experimentally: a 600x800 page with /Rotate 90 reports rect 800x600).
    Since layout/raster math is defined in unrotated media space, callers
    holding rect dimensions must normalize them first. 90/270 swap w/h.
    """
    if int(rotation) in (90, 270):
        return (float(height), float(width))
    return (float(width), float(height))


@dataclass(frozen=True, slots=True)
class PageLayoutEntry:
    """Geometry of one page in the document stack (render pixels)."""

    index: int
    page_id: PageId
    render_width_px: int
    render_height_px: int
    y_offset_px: int
    width_pt: float = 0.0
    height_pt: float = 0.0
    intrinsic_rotation: int = 0
    effective_rotation: int = 0


class DocumentLayout:
    """Immutable vertical stack layout for one raster bucket + user rotation."""

    def __init__(
        self,
        document_id: DocumentId,
        page_sizes_pt: list[Size],
        intrinsic_rotations: list[int] | None,
        raster_scale: float,
        user_rotation: int = 0,
        gap_px: int = 16,
    ) -> None:
        if raster_scale <= 0:
            raise ValueError("raster_scale must be positive")
        if int(user_rotation) not in ALLOWED_ROTATIONS:
            raise ValueError(f"user_rotation must be one of {ALLOWED_ROTATIONS}")
        if gap_px < 0:
            raise ValueError("gap_px must be non-negative")
        if intrinsic_rotations is not None and len(intrinsic_rotations) != len(page_sizes_pt):
            raise ValueError("intrinsic_rotations must match page_sizes_pt length")
        intrinsics = list(intrinsic_rotations) if intrinsic_rotations is not None else [0] * len(page_sizes_pt)

        entries: list[PageLayoutEntry] = []
        offsets: list[int] = []
        y = 0
        for i, (size_pt, intrinsic) in enumerate(zip(page_sizes_pt, intrinsics)):
            eff = effective_rotation(intrinsic, user_rotation)
            w_px, h_px = rotated_render_size(size_pt, raster_scale, eff)
            entries.append(
                PageLayoutEntry(
                    index=i,
                    page_id=page_id_for(document_id, i),
                    render_width_px=w_px,
                    render_height_px=h_px,
                    y_offset_px=y,
                    width_pt=float(size_pt.width),
                    height_pt=float(size_pt.height),
                    intrinsic_rotation=int(intrinsic),
                    effective_rotation=eff,
                )
            )
            offsets.append(y)
            y += h_px + gap_px

        self._document_id = document_id
        self._entries = tuple(entries)
        self._offsets = offsets
        self._bottoms = [off + e.render_height_px for off, e in zip(offsets, entries)]
        self._raster_scale = float(raster_scale)
        self._user_rotation = int(user_rotation)
        self._gap_px = gap_px
        self._total_height_px = max(0, y - gap_px) if entries else 0
        # M5-B.1.2: document content width = widest page (render px).
        # Horizontal scroll range derives from this; pages are at doc x=0.
        self._total_width_px = max(
            (e.render_width_px for e in entries), default=0)

    @property
    def document_id(self) -> DocumentId:
        """Document this layout was built for."""
        return self._document_id

    @property
    def raster_scale(self) -> float:
        """Quantized raster scale this layout was built for."""
        return self._raster_scale

    @property
    def user_rotation(self) -> int:
        """User rotation this layout was built for."""
        return self._user_rotation

    @property
    def page_count(self) -> int:
        """Number of laid-out pages."""
        return len(self._entries)

    @property
    def total_height_px(self) -> int:
        """Stack height in render pixels (no trailing gap)."""
        return self._total_height_px

    @property
    def total_width_px(self) -> int:
        """Content width in render pixels (widest page; M5-B.1.2)."""
        return self._total_width_px

    @staticmethod
    def page_x_origin(viewport_width_px: float, content_width_px: float,
                      page_width_px: float) -> float:
        """Page doc-space X origin shared by paint, input, and planning.

        ``max(0, (C - page_w) / 2)`` with ``C = max(viewport_w,
        content_w)``: identical to the legacy viewport-centering formula
        when the document fits, content-centered when scrollable, and
        continuous across the boundary. Must stay in sync with the mirror
        computation in ``app.viewport_controller.page_screen_rect``.
        """
        extent = max(float(viewport_width_px), float(content_width_px))
        return max(0.0, (extent - float(page_width_px)) / 2.0)

    def entry(self, index: int) -> PageLayoutEntry:
        """Return the layout entry for a page index (raises IndexError)."""
        return self._entries[index]

    def pages_in(self, y0: float, y1: float) -> tuple[int, ...]:
        """Indices of pages intersecting [y0, y1) in document pixels.

        O(log N) via bisect. Empty viewport or no intersection yields ().
        """
        if y1 <= y0 or not self._entries:
            return ()
        # First entry whose bottom edge exceeds y0.
        lo = bisect.bisect_right(self._bottoms, y0)
        # First entry whose top edge reaches y1; everything before intersects.
        hi = bisect.bisect_left(self._offsets, y1)
        lo = max(0, min(lo, len(self._entries)))
        hi = max(0, min(hi, len(self._entries)))
        if hi <= lo:
            return ()
        return tuple(range(lo, hi))

    def page_viewport(
        self,
        index: int,
        scroll_y_doc: float,
        viewport_width_px: int,
        viewport_height_px: int,
        generation: int,
        scroll_x_doc: float = 0.0,
    ) -> ViewportState:
        """Map a document-space scroll to a page-local ViewportState.

        M5-B.1.2: ``scroll_x_doc`` is the document-space horizontal
        viewport origin in render px. Each page converts it to a
        page-local scroll by subtracting its shared X origin
        (``page_x_origin``); the floor at 0.0 covers scroll positions
        left of a narrower page's origin (tile math clamps the rest).
        Default 0.0 preserves legacy behavior.
        """
        entry = self._entries[index]
        origin = DocumentLayout.page_x_origin(
            viewport_width_px, self._total_width_px, entry.render_width_px)
        return ViewportState(
            document_id=self._document_id,
            page_id=entry.page_id,
            page_index=index,
            viewport_width_px=viewport_width_px,
            viewport_height_px=viewport_height_px,
            scroll_x=max(0.0, float(scroll_x_doc) - origin),
            scroll_y=float(scroll_y_doc - entry.y_offset_px),
            scale=self._raster_scale,
            rotation=entry.effective_rotation,
            generation=generation,
        )

    def __len__(self) -> int:
        return len(self._entries)
