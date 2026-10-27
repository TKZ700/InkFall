"""Horizontal scrollbar visibility regression tests.

Root cause fixed here: both overlay scrollbars shared one opacity fade,
so any vertical scroll, wheel, hover, or document open flashed the
horizontal bar to full opacity even when the document fit the viewport
(its QSS groove + min-width handle still painted a full-width empty bar).
The horizontal bar is now hidden entirely — not faded, disabled, or left
with a zero-length thumb — until its range is non-zero, recomputed on
every range sync (resize, zoom, rotation, page ops, document switch).

Visibility is asserted with ``isVisible()``; ranges come from the live
``maximum()`` values, so no pixel math is hard-coded.
"""

from __future__ import annotations

import pytest

from tests.helpers import make_probe_pdf, make_sized_pdf

pytestmark = pytest.mark.usefixtures("qapp")

VIEW_W, VIEW_H = 1100, 750


@pytest.fixture()
def viewer(qtbot):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    widget = PdfViewer(DocumentManager())
    widget.resize(VIEW_W, VIEW_H)
    widget.show()
    qtbot.addWidget(widget)
    yield widget


def _open(viewer, path: str) -> None:
    assert viewer.open_document(path) is True


def test_narrow_document_hides_horizontal_bar(viewer, tmp_path, qtbot) -> None:
    """A PDF narrower than the viewport keeps the horizontal bar hidden."""
    _open(viewer, make_probe_pdf(str(tmp_path / "narrow.pdf"),
                                 page_width=600.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() == 0
    assert not viewer._hscrollbar.isVisible()


def test_wide_document_shows_horizontal_bar(viewer, tmp_path, qtbot) -> None:
    """A PDF wider than the viewport shows the horizontal bar."""
    _open(viewer, make_probe_pdf(str(tmp_path / "wide.pdf"),
                                 page_width=1600.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() > 0
    assert viewer._hscrollbar.isVisible()


def test_zoom_in_reveals_and_fit_width_hides_again(
        viewer, tmp_path, qtbot) -> None:
    """Zooming past fit shows the bar; returning to fit-width hides it."""
    _open(viewer, make_probe_pdf(str(tmp_path / "fit.pdf"),
                                 page_width=600.0, page_height=800.0))
    qtbot.wait(200)
    assert not viewer._hscrollbar.isVisible()

    viewer.set_zoom(2.0)
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() > 0
    assert viewer._hscrollbar.isVisible()

    viewer.apply_view_mode("width")
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() == 0
    assert not viewer._hscrollbar.isVisible()


def test_resize_updates_visibility_across_the_boundary(
        viewer, tmp_path, qtbot) -> None:
    """Shrinking below content width shows the bar; widening hides it."""
    _open(viewer, make_probe_pdf(str(tmp_path / "doc.pdf"),
                                 page_width=1200.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.isVisible()  # 1200 > 1100

    viewer.resize(1400, VIEW_H)
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() == 0
    assert not viewer._hscrollbar.isVisible()

    viewer.resize(900, VIEW_H)
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() > 0
    assert viewer._hscrollbar.isVisible()


def test_rotation_updates_visibility(viewer, tmp_path, qtbot) -> None:
    """Rotating the only (wide) page to portrait removes the overflow."""
    _open(viewer, make_probe_pdf(str(tmp_path / "rot.pdf"),
                                 page_width=1600.0, page_height=800.0))
    qtbot.wait(300)
    assert viewer._hscrollbar.isVisible()

    viewer.rotate_page(0, 90)
    qtbot.wait(400)
    assert viewer._hscrollbar.maximum() == 0
    assert not viewer._hscrollbar.isVisible()

    viewer.rotate_page(0, -90)
    qtbot.wait(400)
    assert viewer._hscrollbar.maximum() > 0
    assert viewer._hscrollbar.isVisible()


def test_document_switch_leaves_no_stale_visibility(
        viewer, tmp_path, qtbot) -> None:
    """Opening a narrow document after a wide one hides the bar again."""
    _open(viewer, make_probe_pdf(str(tmp_path / "wide.pdf"),
                                 page_width=1600.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.isVisible()

    _open(viewer, make_probe_pdf(str(tmp_path / "narrow.pdf"),
                                 page_width=600.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() == 0
    assert not viewer._hscrollbar.isVisible()

    _open(viewer, make_probe_pdf(str(tmp_path / "wide2.pdf"),
                                 page_width=1600.0, page_height=800.0))
    qtbot.wait(200)
    assert viewer._hscrollbar.isVisible()


def test_mixed_page_widths_reflect_the_widest_page(
        viewer, tmp_path, qtbot) -> None:
    """The bar tracks the widest page in the document, then re-syncs."""
    _open(viewer, make_sized_pdf(str(tmp_path / "mixed.pdf"),
                                 sizes=[(600.0, 800.0), (1400.0, 800.0)]))
    qtbot.wait(200)
    assert viewer._hscrollbar.maximum() > 0
    assert viewer._hscrollbar.isVisible()


def test_vertical_scrollbar_behavior_unchanged(viewer, tmp_path, qtbot) -> None:
    """The vertical bar keeps its existing fade chrome and range math."""
    _open(viewer, make_probe_pdf(str(tmp_path / "tall.pdf"),
                                 page_width=600.0, page_height=1600.0))
    qtbot.wait(200)
    # The vertical bar is overlay chrome that fades: the widget stays a
    # visible child while idle (opacity effect gates presentation), and
    # its range math is untouched (a 1600px page must overflow the
    # 750px viewport at any display scale).
    assert viewer._scrollbar.isVisible()
    vertical_max = viewer._scrollbar.maximum()
    assert vertical_max > 0
    viewer._scrollbar.setValue(vertical_max // 2)
    qtbot.wait(100)
    assert viewer._scrollbar.maximum() == vertical_max
    # Panning/scrolling must not conjure the horizontal bar on a
    # document that fits horizontally.
    assert not viewer._hscrollbar.isVisible()
