"""M3-B cache tests: identity, LRU, exact byte accounting, budget."""

from __future__ import annotations

import pytest

from core.types import DocumentId, PageId
from rendering.cache import (
    DEFAULT_CACHE_BUDGET_BYTES,
    CachedTile,
    TileCache,
    cache_identity,
)
from rendering.types import TileKey


def _key(i: int = 0, gen: int = 0, w: int = 512, h: int = 512) -> TileKey:
    return TileKey(
        document_id=DocumentId("c"),
        page_id=PageId(f"c:p{i}"),
        generation=gen,
        tile_x=0,
        tile_y=0,
        tile_w=w,
        tile_h=h,
    )


def _tile(key: TileKey, fill: int = 0xAB) -> CachedTile:
    return CachedTile(
        key=key,
        data=bytes([fill]) * (key.tile_w * key.tile_h * 4),
        width_px=key.tile_w,
        height_px=key.tile_h,
        stride=key.tile_w * 4,
    )


def test_default_budget():
    assert DEFAULT_CACHE_BUDGET_BYTES == 64 * 1024 * 1024


def test_hit_miss_stats():
    cache = TileCache(budget_bytes=10 * 1024 * 1024)
    assert cache.get(_key()) is None
    cache.put(_tile(_key()))
    assert cache.get(_key()) is not None
    stats = cache.stats()
    assert (stats["hits"], stats["misses"]) == (1, 1)
    assert abs(stats["hit_rate"] - 0.5) < 1e-9
    assert stats["resident_bytes"] == 512 * 512 * 4


def test_generation_normalized():
    cache = TileCache(budget_bytes=10 * 1024 * 1024)
    cache.put(_tile(_key(gen=10)))
    cache.put(_tile(_key(gen=11)))
    assert len(cache) == 1
    assert cache.resident_bytes == 512 * 512 * 4
    assert cache_identity(_key(gen=10)) == cache_identity(_key(gen=11))


def test_lru_eviction_order():
    cache = TileCache(budget_bytes=2 * 512 * 512 * 4)
    cache.put(_tile(_key(0)))
    cache.put(_tile(_key(1)))
    assert cache.get(_key(0)) is not None
    cache.put(_tile(_key(2)))
    assert cache.get(_key(1)) is None
    assert cache.get(_key(0)) is not None
    assert cache.get(_key(2)) is not None
    assert cache.stats()["evictions"] == 1


def test_budget_never_exceeded():
    budget = 5 * 256 * 256 * 4
    cache = TileCache(budget_bytes=budget)
    for i in range(50):
        cache.put(_tile(_key(i, w=256, h=256), fill=i % 256))
        assert cache.resident_bytes <= budget
    assert cache.stats()["evictions"] > 0


def test_edge_tile_bytes_exact():
    cache = TileCache(budget_bytes=10 * 1024 * 1024)
    cache.put(_tile(_key(w=88, h=288)))
    assert cache.resident_bytes == 88 * 288 * 4


def test_replacement_no_double_count():
    cache = TileCache(budget_bytes=10 * 1024 * 1024)
    cache.put(_tile(_key(0), fill=0x11))
    cache.put(_tile(_key(0), fill=0x22))
    assert len(cache) == 1
    assert cache.resident_bytes == 512 * 512 * 4
    assert cache.get(_key(0)).data[:1] == bytes([0x22])


def test_remove_and_clear():
    cache = TileCache(budget_bytes=10 * 1024 * 1024)
    cache.put(_tile(_key(0)))
    cache.put(_tile(_key(1)))
    assert cache.remove(_key(0)) is True
    assert cache.remove(_key(0)) is False
    assert cache.resident_bytes == 512 * 512 * 4
    cache.clear()
    assert len(cache) == 0 and cache.resident_bytes == 0


def test_rejects_non_positive_budget():
    with pytest.raises(ValueError):
        TileCache(budget_bytes=0)
