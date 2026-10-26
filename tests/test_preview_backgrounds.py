"""Preview paper and read-mode background tests.

Covers the shared white backing for page previews / Library covers and
the main viewer's mode-aware page and unloaded-tile background.  These
are offscreen pixel checks plus one fixture-integrity (PDF bytes) check;
real compositor appearance remains a manual acceptance item.
"""

from __future__ import annotations

import hashlib
import os

import pymupdf
import pytest
from PySide6.QtGui import QColor, QImage

from app.previews import PREVIEW_PAPER, flatten_on_white

pytestmark = pytest.mark.usefixtures("qapp")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_blank_pdf(path: str, width: float = 200.0,
                    height: float = 260.0) -> str:
    """A page with no painted content: fully transparent pixels."""
    doc = pymupdf.open()
    try:
        doc.new_page(width=width, height=height)
        doc.save(path)
    finally:
        doc.close()
    return path


def _make_filled_pdf(path: str, color=(0.2, 0.4, 0.8),
                     width: float = 200.0, height: float = 260.0) -> str:
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        page.draw_rect(page.rect, color=None, fill=color, width=0)
        doc.save(path)
    finally:
        doc.close()
    return path


def _preview_rgba(path: str, side: int = 64) -> QImage:
    from document.backend import render_cover_preview

    result = render_cover_preview(path, side)
    assert result is not None
    data, width, height, stride, _page_count = result
    image = QImage(data, width, height, stride,
                   QImage.Format.Format_RGBA8888).copy()
    assert not image.isNull()
    return image


def _center(image: QImage) -> QColor:
    return image.pixelColor(image.width() // 2, image.height() // 2)


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ---------------------------------------------------------------------------
# Shared preview layer
# ---------------------------------------------------------------------------


def test_backgroundless_page_gets_white_preview_paper(tmp_path) -> None:
    pdf = _make_blank_pdf(str(tmp_path / "blank.pdf"))
    raw = _preview_rgba(pdf)

    # The render contract keeps transparency ...
    assert _center(raw).alpha() < 16
    # ... and the display copy is white paper, not an invisible hole.
    white = flatten_on_white(raw)
    pixel = _center(white)
    assert (pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()) == (
        255, 255, 255, 255)
    assert PREVIEW_PAPER == QColor("#ffffff")


def test_opaque_page_colours_are_unchanged_by_preview_paper(tmp_path) -> None:
    pdf = _make_filled_pdf(str(tmp_path / "filled.pdf"))
    raw = _preview_rgba(pdf)
    white = flatten_on_white(raw)
    raw_pixel = _center(raw)
    flat_pixel = _center(white)
    assert raw_pixel.alpha() == 255
    assert (raw_pixel.red(), raw_pixel.green(), raw_pixel.blue()) == (
        flat_pixel.red(), flat_pixel.green(), flat_pixel.blue())
    assert abs(raw_pixel.red() - 51) <= 2
    assert abs(raw_pixel.green() - 102) <= 2
    assert abs(raw_pixel.blue() - 204) <= 2


def test_library_cover_service_uses_white_for_fresh_and_cached(qtbot,
                                                               tmp_path) -> None:
    from app.thumbnails.service import ThumbnailService

    pdf = _make_blank_pdf(str(tmp_path / "library.pdf"))
    digest = _sha256(pdf)
    cache_dir = tmp_path / "thumbs"
    service = ThumbnailService(cache_dir=str(cache_dir))
    got: dict = {}
    service.ready.connect(
        lambda item_id, image: got.update(id=item_id, image=image))
    try:
        stat = os.stat(pdf)
        assert service.request(
            "doc", pdf, float(stat.st_mtime), int(stat.st_size)) is None
        qtbot.waitUntil(lambda: "image" in got, timeout=15000)

        fresh = got["image"]
        assert fresh is not None
        pixel = _center(fresh)
        assert (pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()) == (
            255, 255, 255, 255)
        assert list(cache_dir.glob("*.png")), "preview was not cached"

        # Same-service memory hit returns the same white-backed image.
        cached = service.request(
            "doc", pdf, float(stat.st_mtime), int(stat.st_size))
        assert cached is not None
        assert _center(cached).name() == "#ffffff"

        # A new service reading the on-disk cache is equally white.
        service2 = ThumbnailService(cache_dir=str(cache_dir))
        try:
            disk = service2.request(
                "doc", pdf, float(stat.st_mtime), int(stat.st_size))
            assert disk is not None
            assert _center(disk).name() == "#ffffff"
        finally:
            service2.shutdown()
    finally:
        service.shutdown()

    # Previewing/caching never rewrites the document.
    assert _sha256(pdf) == digest


def test_page_navigator_preview_uses_white_paper(qapp, monkeypatch) -> None:
    import app.page_navigator as page_navigator

    class _DummyDecoder:
        def prewarm(self) -> None:
            pass

        def submit(self, *_args, **_kwargs):
            raise AssertionError("this test supplies the finished result")

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(
        page_navigator, "ProcessDecoder", lambda _config: _DummyDecoder())
    engine = page_navigator.PageThumbEngine()
    try:
        rgba = bytes((0, 0, 0, 0)) * (8 * 8)
        key = ("doc", 0, 1.0, 8, 8)
        engine._on_job_done(key, (rgba, 8, 8, 32), None)
        image = engine.cached(key)
        assert image is not None
        pixel = image.pixelColor(4, 4)
        assert (pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()) == (
            255, 255, 255, 255)
    finally:
        engine.shutdown()


# ---------------------------------------------------------------------------
# Main viewer paper and unloaded tiles
# ---------------------------------------------------------------------------


def _open_blank_viewer(qtbot, tmp_path):
    from app.main_window import MainWindow

    pdf = _make_blank_pdf(str(tmp_path / "viewer-blank.pdf"))
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    qtbot.waitUntil(
        lambda: viewer._pipeline is not None
        and viewer._pipeline.layout.page_count == 1,
        timeout=15000,
    )
    return window, viewer, pdf


def _page_center(viewer) -> tuple[int, int]:
    from app.viewport_controller import page_screen_rect

    layout = viewer._pipeline.layout
    entry = layout.entry(0)
    scale = viewer._display_scale()
    vw, _vh = viewer._render_viewport()
    rect = page_screen_rect(
        float(entry.y_offset_px),
        float(entry.render_width_px),
        float(entry.render_height_px),
        viewer._scroll_doc(),
        vw,
        scale,
        scroll_x_doc=viewer._scroll_doc_x(),
        content_width_px=float(layout.total_width_px),
    )
    left = max(0, int(rect.x))
    right = min(viewer.width(), int(rect.x + rect.width))
    top = max(0, int(rect.y))
    bottom = min(viewer.height(), int(rect.y + rect.height))
    if right <= left or bottom <= top:
        return viewer.width() // 2, viewer.height() // 2
    # Zoom/scroll can push parts of the page outside the viewport; the
    # centre of the visible page intersection is always a valid pixel.
    return (left + right) // 2, (top + bottom) // 2


def test_unloaded_tiles_match_each_read_mode(qtbot, tmp_path) -> None:
    from app.viewer import _PAGE_FILL, _appearance_solid

    _window, viewer, pdf = _open_blank_viewer(qtbot, tmp_path)
    digest = _sha256(pdf)
    try:
        viewer._timer.stop()
        expected = {"light": "#ffffff", "dark": "#000000",
                    "inverted": "#000000"}
        for mode, name in expected.items():
            viewer.set_pdf_appearance(mode)
            viewer._display.clear()
            viewer._timer.stop()
            image = viewer.grab().toImage()
            x, y = _page_center(viewer)
            pixel = image.pixelColor(x, y)
            assert pixel.name() == name, (mode, pixel.name())
            assert pixel.name() == viewer._pdf_page_fill().name()
            assert viewer._pdf_page_fill().name() == \
                _appearance_solid(_PAGE_FILL, mode).name()
            assert viewer.painted_placeholders > 0

        # A live mode switch while the page is unloaded repaints at once.
        viewer.set_pdf_appearance("dark")
        viewer._display.clear()
        viewer._timer.stop()
        assert viewer.grab().toImage().pixelColor(
            *_page_center(viewer)).name() == "#000000"
        viewer.set_pdf_appearance("light")
        viewer._display.clear()
        viewer._timer.stop()
        assert viewer.grab().toImage().pixelColor(
            *_page_center(viewer)).name() == "#ffffff"
    finally:
        viewer.close_document()

    assert _sha256(pdf) == digest


def test_loaded_tiles_replace_placeholders_without_stale_colour(qtbot,
                                                                tmp_path) -> None:
    _window, viewer, _pdf = _open_blank_viewer(qtbot, tmp_path)
    try:
        viewer.set_pdf_appearance("light")
        viewer._timer.stop()
        viewer._display.clear()
        placeholder = viewer.grab().toImage().pixelColor(*_page_center(viewer))
        assert placeholder.name() == "#ffffff"

        viewer._timer.start()
        qtbot.waitUntil(lambda: bool(viewer._display), timeout=15000)
        viewer._timer.stop()
        loaded = viewer.grab().toImage()
        assert viewer.painted_tiles > 0
        # A transparent page tile composites onto the same white paper,
        # so the placeholder -> loaded transition is seamless.
        assert loaded.pixelColor(*_page_center(viewer)).name() == "#ffffff"

        # Zoom invalidates tiles: the replacement placeholder still uses
        # the active mode's paper rather than a fixed widget colour.
        viewer.set_zoom(2.0)
        viewer._display.clear()
        viewer._timer.stop()
        assert viewer.grab().toImage().pixelColor(
            *_page_center(viewer)).name() == "#ffffff"
    finally:
        viewer.close_document()
