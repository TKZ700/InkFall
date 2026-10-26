"""M3-C viewer tests (offscreen Qt): open/scroll/zoom/paint/close lifecycle."""

from __future__ import annotations

import threading

import pytest

from app.main_window import MainWindow
from tests.helpers import make_mixed_pdf, make_probe_pdf, make_rich_pdf

pytestmark = pytest.mark.usefixtures("qapp")


def _open_window(qtbot, pdf: str, width: int = 800, height: int = 600):
    window = MainWindow()
    window.resize(width, height)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    assert window.viewer.has_document
    # M4 shell hosts the viewer on a stacked page: make it the visible
    # page so paint events are delivered (harness only, assertions unchanged).
    window._stack.setCurrentIndex(1)
    return window


def _render_threads() -> list:
    return [t for t in threading.enumerate() if t.name.startswith("inkfall-render")]


def test_open_pdf_gives_page_geometry(qtbot, tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "a.pdf"), pages=3)
    window = _open_window(qtbot, pdf)
    try:
        viewer = window.viewer
        assert viewer._pipeline.layout.total_height_px > 0
        assert viewer._pipeline.layout.page_count == 3
        assert viewer._scrollbar.maximum() > 0
    finally:
        viewer.close_document()


def test_tile_becomes_displayed(qtbot, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "b.pdf"))
    window = _open_window(qtbot, pdf)
    try:
        viewer = window.viewer
        qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=15000)
        assert viewer.total_painted_tiles >= 1
    finally:
        viewer.close_document()


def test_scroll_changes_document_offset(qtbot, tmp_path):
    pdf = make_rich_pdf(str(tmp_path / "c.pdf"), pages=4)
    window = _open_window(qtbot, pdf)
    try:
        viewer = window.viewer
        assert viewer._scroll_doc() == 0.0
        viewer._scrollbar.setValue(viewer._scrollbar.maximum())
        assert viewer._scroll_doc() > 0.0
    finally:
        viewer.close_document()


def test_zoom_changes_raster_bucket(qtbot, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "d.pdf"))
    window = _open_window(qtbot, pdf)
    try:
        viewer = window.viewer
        assert viewer._pipeline.raster_scale == 1.0
        viewer.set_zoom(4.0)
        assert viewer._pipeline.raster_scale == 4.0
        assert viewer.set_zoom(100.0) == 8.0  # clamped
        assert viewer.set_zoom(0.01) == 0.25  # clamped
    finally:
        viewer.close_document()


def test_missing_tiles_are_placeholders_not_crashes(qtbot, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "e.pdf"))
    window = _open_window(qtbot, pdf)
    try:
        viewer = window.viewer
        viewer.repaint()
        qtbot.waitUntil(lambda: viewer.total_painted_tiles + viewer.total_painted_placeholders > 0,
                        timeout=15000)
        assert isinstance(viewer.painted_tiles, int)
        assert isinstance(viewer.painted_placeholders, int)
    finally:
        viewer.close_document()


def test_close_cleans_rendering_resources(qtbot, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "f.pdf"))
    window = _open_window(qtbot, pdf)
    viewer = window.viewer
    qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=15000)
    viewer.close_document()
    assert not viewer.has_document
    assert viewer._pipeline is None
    assert viewer._display == {}  # transient display map released
    assert _render_threads() == []
    assert viewer.isEnabled()  # GUI stays alive after close
    viewer.close_document()  # idempotent


def test_reopen_another_pdf(qtbot, tmp_path):
    pdf1 = make_probe_pdf(str(tmp_path / "g1.pdf"))
    pdf2 = make_mixed_pdf(str(tmp_path / "g2.pdf"), pages=4)
    window = _open_window(qtbot, pdf1)
    try:
        viewer = window.viewer
        qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=15000)
        assert viewer.open_document(pdf2) is True
        assert viewer._pipeline.layout.page_count == 4
        qtbot.waitUntil(lambda: viewer.total_painted_tiles > 1, timeout=15000)
    finally:
        viewer.close_document()
    assert _render_threads() == []


def test_open_missing_file_reports_error(qtbot):
    window = MainWindow()
    window.show()
    qtbot.addWidget(window)
    try:
        assert window.viewer.open_document("/nonexistent/missing.pdf") is False
        assert not window.viewer.has_document
    finally:
        window.viewer.close_document()


def test_smoke_three_page_pdf(qtbot, tmp_path):
    """Full smoke: open 3-page PDF, tiles reach the UI, clean close."""
    pdf = make_rich_pdf(str(tmp_path / "smoke.pdf"), pages=3)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    viewer = window.viewer
    try:
        assert viewer.open_document(pdf) is True
        window._stack.setCurrentIndex(1)  # M4 shell: viewer page must be visible to paint
        assert viewer._scrollbar.maximum() > 0  # page geometry present
        qtbot.waitUntil(lambda: viewer.total_painted_tiles > 0, timeout=15000)
        assert viewer._pipeline.stats()["engine"]["failed"] == 0
    finally:
        viewer.close_document()
    assert _render_threads() == []
