"""Bounded LRU tile cache (M3-B). GUI-independent, no Qt, no PyMuPDF.

Stores raw RGBA8 payload bytes only — no Qt images, no fitz objects.
Cache identity is the 7 pixel-affecting ``TileKey`` fields; generation
represents freshness (not pixels) and is NORMALIZED OUT of lookup/insert
via :func:`cache_identity`, so visually identical tiles never duplicate
across epochs. Stale-epoch rejection happens at delivery/completion, not
in the cache.

The budget covers tile payload bytes exactly (``w * h * 4``) and is
enforced on every insertion by evicting least-recently-used entries until
``resident_bytes <= budget``. This is the renderer payload budget — not
total process RSS (see module docs on the distinction).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from rendering.renderer import BYTES_PER_PIXEL
from rendering.types import TileKey

__all__ = ["CachedTile", "TileCache", "DEFAULT_CACHE_BUDGET_BYTES", "cache_identity"]

#: Initial payload budget: 64 MiB (64 full 512px RGBA tiles). A starting
#: value for benchmarking, NOT a claim of optimality (see M3-B report).
DEFAULT_CACHE_BUDGET_BYTES = 64 * 1024 * 1024


def cache_identity(key: TileKey) -> tuple:
    """Cache identity: the 7 pixel-affecting fields, generation excluded."""
    doc, page, scale, rotation, tile_tuple, _generation = key.to_tuple()
    return (doc, page, scale, rotation, tile_tuple)


@dataclass(frozen=True, slots=True)
class CachedTile:
    """One resident tile: identity + application-owned RGBA8 payload."""

    key: TileKey
    data: bytes
    width_px: int
    height_px: int
    stride: int

    @property
    def payload_bytes(self) -> int:
        """Exact payload size (authoritative for accounting)."""
        return len(self.data)


class TileCache:
    """Deterministic LRU cache with strict payload-byte accounting."""

    def __init__(self, budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES) -> None:
        if budget_bytes <= 0:
            raise ValueError("budget_bytes must be positive")
        self._budget = budget_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, CachedTile] = OrderedDict()
        self._resident_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.inserts = 0

    @property
    def budget_bytes(self) -> int:
        """Configured payload budget."""
        return self._budget

    @property
    def resident_bytes(self) -> int:
        """Currently resident payload bytes (always <= budget)."""
        with self._lock:
            return self._resident_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: TileKey) -> CachedTile | None:
        """Return the resident tile, promoting it to most-recently-used."""
        identity = cache_identity(key)
        with self._lock:
            tile = self._entries.get(identity)
            if tile is None:
                self.misses += 1
                return None
            self._entries.move_to_end(identity)
            self.hits += 1
            return tile

    def put(self, tile: CachedTile) -> None:
        """Insert (or replace) a tile, evicting LRU entries until in budget.

        Replacement of an existing identity adjusts accounting instead of
        double-counting. A single tile larger than the budget is still
        stored (it becomes the sole resident); oversize payloads are the
        caller's concern, never silent drops.
        """
        identity = cache_identity(tile.key)
        size = tile.payload_bytes
        with self._lock:
            old = self._entries.pop(identity, None)
            if old is not None:
                self._resident_bytes -= old.payload_bytes
            self._entries[identity] = tile
            self._resident_bytes += size
            self.inserts += 1
            while len(self._entries) > 1 and self._resident_bytes > self._budget:
                _victim_key, victim = self._entries.popitem(last=False)
                self._resident_bytes -= victim.payload_bytes
                self.evictions += 1

    def remove(self, key: TileKey) -> bool:
        """Remove one entry. Returns True if present."""
        identity = cache_identity(key)
        with self._lock:
            tile = self._entries.pop(identity, None)
            if tile is None:
                return False
            self._resident_bytes -= tile.payload_bytes
            return True

    def clear(self) -> None:
        """Drop all entries and reset byte accounting."""
        with self._lock:
            self._entries.clear()
            self._resident_bytes = 0

    def stats(self) -> dict:
        """Plain-dict snapshot: hits/misses/evictions/bytes/count/budget."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
                "inserts": self.inserts,
                "resident_bytes": self._resident_bytes,
                "resident_tiles": len(self._entries),
                "budget_bytes": self._budget,
            }

    @staticmethod
    def payload_bytes_for(width_px: int, height_px: int) -> int:
        """Exact RGBA8 payload size for tile dimensions."""
        return width_px * height_px * BYTES_PER_PIXEL
