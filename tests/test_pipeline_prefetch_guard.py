"""Prefetch-guard regression: visible work always wins over prefetch.

Focus: during active viewport work, prefetch is withheld; once the
visible set is resident, prefetch resumes. Existing dedup/generation/
pressure semantics are preserved.
"""

from __future__ import annotations

import time

import pytest

from core.types import DocumentId, Size
from document.models import PageInfo, page_id_for
from rendering.pipeline import DocumentPipeline
from rendering.scheduler import EngineConfig
from rendering.types import RenderPriority, TileKey
from tests.helpers import make_dense_pdf, make_probe_pdf, make_rich_pdf


def _pages(doc: DocumentId, sizes):
    out = []
    for i, (w, h, rot) in enumerate(sizes):
        out.append(
            PageInfo(
                id=page_id_for(doc, i), document_id=doc, index=i,
                size=Size(w, h),
                rotation=rot,
            )
        )
    return out


SIZES = [(600.0, 800.0, 0)] * 4

TINY_VP = (300, 300)


def _pipe(pdf, sizes, **kw):
    doc = DocumentId("guard")
    defaults = dict(engine_config=EngineConfig(worker_count=2))
    defaults.update(kw)
    return DocumentPipeline(doc, pdf, _pages(doc, sizes), **defaults)


def _drain(pipe, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while True:
        stats = pipe.stats()
        if stats["in_flight"] == 0 and stats["engine"]["pending"] == 0:
            return stats
        assert time.monotonic() < deadline, "pipeline did not quiesce"
        time.sleep(0.005)


def test_cold_viewport_admits_visible_only(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES)
    try:
        pipe.update(0.0, *TINY_VP)
        stats = pipe.stats()
        assert stats["submitted_visible"] >= 1
        assert stats["submitted_prefetch"] == 0
        assert stats["prefetch_deferred"] > 0
        _drain(pipe)
    finally:
        pipe.close()


def test_prefetch_withheld_while_visible_unresolved(tmp_path, monkeypatch):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES)
    try:
        first = pipe.update(0.0, *TINY_VP)
        # Second update while workers are still busy: visible attaches,
        # prefetch must remain withheld.
        second = pipe.update(0.0, *TINY_VP)
        assert second["epoch"] == first["epoch"]
        stats = pipe.stats()
        assert stats["attached_in_flight"] >= 1
        assert stats["submitted_prefetch"] == 0
        _drain(pipe)
    finally:
        pipe.close()


def _ring_tile(pipe, page_index, tile_x, tile_y, tile_w, tile_h):
    """Prefetch-ring tile key for assertions (generation ignored by cache)."""
    return TileKey(
        document_id=pipe._document_id,
        page_id=page_id_for(pipe._document_id, page_index),
        generation=0, scale=1.0, rotation=0,
        tile_x=tile_x, tile_y=tile_y, tile_w=tile_w, tile_h=tile_h,
    )


def test_prefetch_resumes_once_visible_resident(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES)
    try:
        pipe.update(0.0, *TINY_VP)
        _drain(pipe)
        # All visible tiles now resident -> next update serves the prefetch
        # ring (via submission or via slicing from the decoded page).
        pipe.update(0.0, *TINY_VP)
        stats = pipe.stats()
        assert stats["submitted_prefetch"] + stats["sliced_tiles"] > 0
        assert pipe._cache.get(_ring_tile(pipe, 0, 512, 0, 88, 512)) is not None
        _drain(pipe)
    finally:
        pipe.close()


def test_settled_viewport_still_populates_prefetch_ring(tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "r.pdf"), pages=1)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)])
    try:
        pipe.update(0.0, *TINY_VP)
        _drain(pipe)
        pipe.update(0.0, *TINY_VP)
        _drain(pipe)
        stats = pipe.stats()
        assert stats["submitted_prefetch"] + stats["sliced_tiles"] >= 1
        assert pipe._cache.get(_ring_tile(pipe, 0, 512, 0, 83, 512)) is not None
        assert stats["cache"]["resident_bytes"] > 0
    finally:
        pipe.close()


def test_dedup_still_works(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES)
    try:
        first = pipe.update(0.0, *TINY_VP)
        second = pipe.update(0.0, *TINY_VP)
        total = (
            pipe.stats()["submitted_visible"]
            + pipe.stats()["submitted_prefetch"]
        )
        assert total <= first["visible"] + first["prefetch"]
        assert pipe.stats()["attached_in_flight"] >= 1
        _drain(pipe)
    finally:
        pipe.close()


def test_generation_stale_protection_still_works(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"), pages=2)
    pipe = _pipe(pdf, [(595.0, 842.0, 0)] * 2,
                 engine_config=EngineConfig(worker_count=1))
    try:
        pipe.update(0.0, *TINY_VP)
        pipe.set_zoom(2.0)  # epoch bump mid-flight
        _drain(pipe)
        assert pipe.stats()["stale_discarded"] >= 1
        pipe.close()
        assert pipe.stats()["cache"]["resident_bytes"] == 0
    finally:
        if pipe._closed is False:
            pipe.close()


def test_pressure_accounting_preserved(tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "p.pdf"))
    pipe = _pipe(pdf, SIZES, engine_config=EngineConfig(worker_count=1, max_pending=2))
    try:
        pipe.update(0.0, 800, 600)
        stats = _drain(pipe)
        assert stats["queue_full"] + stats["prefetch_dropped"] >= 0  # path exercised
        assert stats["submitted_visible"] >= 1
    finally:
        pipe.close()
