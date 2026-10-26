"""Thumbnail architecture tests (M4): bounds, cancellation, raster helper."""

import os

import pytest

from app.thumbnails.service import (
    MAX_MEMORY_BYTES,
    MAX_MEMORY_ENTRIES,
    MAX_QUEUED,
    ThumbnailService,
    cache_key,
)


def test_cache_key_stable_and_revision_sensitive():
    assert cache_key("a", 1.0, 10) == cache_key("a", 1.0, 10)
    assert cache_key("a", 1.0, 10) != cache_key("a", 2.0, 10)
    assert cache_key("a", 1.0, 10) != cache_key("b", 1.0, 10)


def test_memory_cache_bounded(qapp, tmp_path):
    from PySide6.QtGui import QImage

    svc = ThumbnailService(cache_dir=str(tmp_path / "thumbs"))
    for i in range(MAX_MEMORY_ENTRIES + 40):
        svc._store_memory(f"k{i}", QImage(64, 64, QImage.Format.Format_RGBA8888))
    assert svc.memory_entries <= MAX_MEMORY_ENTRIES
    assert svc.memory_bytes <= MAX_MEMORY_BYTES


def test_queue_bounded_and_cancellable(qapp, tmp_path):
    svc = ThumbnailService(cache_dir=str(tmp_path / "thumbs"))
    # Fill the queue without pumping workers: block pump by faking inflight.
    svc._inflight.update({"busy-1", "busy-2"})
    try:
        for i in range(MAX_QUEUED + 50):
            svc._pending[f"item-{i}"] = "C:/fake.pdf"
            if len(svc._pending) > MAX_QUEUED:
                svc._pending.pop(f"item-{i}")
        assert len(svc._pending) <= MAX_QUEUED
        before = len(svc._pending)
        dropped = svc.cancel_outside({"item-0"})
        assert dropped == before - 1
        assert set(svc._pending) == {"item-0"}
    finally:
        svc._inflight.clear()
        svc._pending.clear()


def test_missing_file_fails_fast_without_work(qapp, tmp_path):
    svc = ThumbnailService(cache_dir=str(tmp_path / "thumbs"))
    failures: list[str] = []
    svc.failed.connect(failures.append)
    assert svc.request("x", "", 0.0, 0, missing=True) is None
    assert failures == ["x"]
    assert svc.pending_count == 0


def test_render_cover_preview_real_pdf(tmp_path):
    from tests.helpers import make_rich_pdf

    from document.backend import render_cover_preview

    pdf = make_rich_pdf(str(tmp_path / "cover.pdf"), pages=2)
    result = render_cover_preview(pdf)
    assert result is not None
    data, width, height, stride, page_count = result
    assert page_count == 2
    assert width > 0 and height > 0 and max(width, height) <= 256
    assert len(data) == stride * height


def test_render_cover_preview_failures(tmp_path):
    from document.backend import render_cover_preview

    assert render_cover_preview(str(tmp_path / "missing.pdf")) is None
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert render_cover_preview(str(bad)) is None


def test_thumbnail_end_to_end_off_gui_thread(qtbot, tmp_path):
    from tests.helpers import make_rich_pdf

    pdf = make_rich_pdf(str(tmp_path / "e2e.pdf"), pages=1)
    svc = ThumbnailService(cache_dir=str(tmp_path / "thumbs"))
    got: dict = {}
    svc.ready.connect(lambda item_id, image: got.update(id=item_id, image=image))
    st = os.stat(pdf)
    svc.request("doc1", pdf, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: "id" in got, timeout=15000)
    assert got["image"].width() > 0
    # Second request for the same revision hits memory (no new work).
    pending_before = svc.pending_count
    svc.request("doc1", pdf, float(st.st_mtime), int(st.st_size))
    assert svc.pending_count == pending_before
    # Disk cache file exists.
    assert list((tmp_path / "thumbs").glob("*.png"))
