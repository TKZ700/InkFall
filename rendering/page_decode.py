"""Bounded decoded-page LRU (Path F prototype). No Qt, no PyMuPDF.

Stores raw full-page RGBA8 payload bytes only — never pixmaps, pages, or
document objects. A decoded page is the raster of one page at one bucket
scale; individual tiles are sliced from it by the pipeline and inserted
into the normal :class:`TileCache` under their normal identities, so the
viewer never knows a tile's provenance.

Identity is ``(document, page, scale, rotation)`` — generation is
deliberately EXCLUDED (it never discriminates: every epoch bump in the
pipeline coincides with a scale or rotation change, so including it could
only retain duplicate rasters). Freshness is enforced at delivery (the
pipeline only stores decodes from the current epoch) and by clearing on
epoch change.

Memory is bounded by BYTES, not entry count: entries evict LRU until the
budget fits, and a single entry larger than the budget is REFUSED (the
caller falls back to per-tile rendering) rather than stored.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

__all__ = [
    "PAGE_DECODE_MAX_ENTRIES",
    "PAGE_DECODE_BUDGET_BYTES",
    "DecodedPage",
    "PageDecodeCache",
    "decode_key",
    "slice_tile",
]

#: Hard cap on resident decoded pages (scroll-back working set, not docs).
PAGE_DECODE_MAX_ENTRIES = 2

#: Byte budget: 2 entries x worst case 2.0x page (1191x1685x4 ~ 8.0 MiB).
PAGE_DECODE_BUDGET_BYTES = 16 * 1024 * 1024


def decode_key(
    document_value: str, page_value: str, scale: float, rotation: int
) -> tuple:
    """Cache identity of a decoded raster (generation excluded by design)."""
    return (document_value, page_value, float(scale), int(rotation))


@dataclass(frozen=True, slots=True)
class DecodedPage:
    """One resident full-page RGBA8 raster plus its producing generation."""

    key: tuple
    data: bytes
    width_px: int
    height_px: int
    stride: int
    generation: int

    @property
    def payload_bytes(self) -> int:
        """Exact resident size (authoritative for accounting)."""
        return len(self.data)


def slice_tile(page: DecodedPage, tile_x: int, tile_y: int, tile_w: int, tile_h: int) -> bytes:
    """Copy one tile region out of a decoded page (raises on bad geometry).

    Requires a tightly packed RGBA8 buffer (stride == width * 4); anything
    else raises and the caller falls back to per-tile rendering.
    """
    if page.stride != page.width_px * 4:
        raise ValueError("decoded page is not tightly packed RGBA8")
    if tile_x < 0 or tile_y < 0 or tile_w <= 0 or tile_h <= 0:
        raise ValueError("invalid tile geometry")
    if tile_x + tile_w > page.width_px or tile_y + tile_h > page.height_px:
        raise ValueError("tile lies outside the decoded page")
    arr = np.frombuffer(page.data, dtype=np.uint8).reshape(
        page.height_px, page.width_px, 4
    )
    return arr[tile_y : tile_y + tile_h, tile_x : tile_x + tile_w].tobytes()


class PageDecodeCache:
    """Bounded LRU of decoded full-page rasters with strict byte budget."""

    def __init__(
        self,
        max_entries: int = PAGE_DECODE_MAX_ENTRIES,
        budget_bytes: int = PAGE_DECODE_BUDGET_BYTES,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if budget_bytes <= 0:
            raise ValueError("budget_bytes must be positive")
        self._max_entries = int(max_entries)
        self._budget = int(budget_bytes)
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, DecodedPage] = OrderedDict()
        self._resident_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.rejected = 0

    def get(self, key: tuple, generation: int) -> DecodedPage | None:
        """Return the resident raster if present AND from this generation."""
        with self._lock:
            page = self._entries.get(key)
            if page is None or page.generation != int(generation):
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return page

    def put(self, page: DecodedPage) -> bool:
        """Store a raster; False (rejected) if it cannot fit the budget.

        A single entry larger than the whole budget is never stored. Evicts
        LRU entries until the budget AND entry cap both hold.
        """
        size = page.payload_bytes
        with self._lock:
            if size > self._budget:
                self.rejected += 1
                return False
            old = self._entries.pop(page.key, None)
            if old is not None:
                self._resident_bytes -= old.payload_bytes
            self._entries[page.key] = page
            self._resident_bytes += size
            while (len(self._entries) > self._max_entries) or (
                len(self._entries) > 1 and self._resident_bytes > self._budget
            ):
                _victim_key, victim = self._entries.popitem(last=False)
                self._resident_bytes -= victim.payload_bytes
                self.evictions += 1
            return True

    def clear(self) -> None:
        """Drop all entries and reset accounting."""
        with self._lock:
            self._entries.clear()
            self._resident_bytes = 0

    def stats(self) -> dict:
        """Plain-dict snapshot (bytes/count/budget/hits/misses/evictions)."""
        with self._lock:
            return {
                "decoded_pages": len(self._entries),
                "decoded_bytes": self._resident_bytes,
                "budget_bytes": self._budget,
                "max_entries": self._max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "rejected": self.rejected,
            }
