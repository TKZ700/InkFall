"""M5-B.1.2 horizontal panning tests: bounds, mapping, anchoring.

Deterministic by construction: synthetic layouts for the coordinate
math, offscreen viewer sessions with fixed geometry (1100x750) and
probe PDFs for the end-to-end channel. No native verification claimed.
"""

from __future__ import annotations

import pytest

from app.viewport_controller import page_screen_rect
from core.types import DocumentId, Size
from rendering.layout import DocumentLayout
from tests.helpers import committed_strokes, make_probe_pdf, make_sized_pdf
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")

VIEW_W, VIEW_H = 1100, 750


@pytest.fixture()
def cleared_settings():
    """Isolate the tool-shelf store (mirrors test_tool_freshness)."""
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass


def _layout_two() -> DocumentLayout:
    return DocumentLayout(
        DocumentId("h2"),
        [Size(600.0, 800.0), Size(1600.0, 800.0)],
        None, 1.0, gap_px=0,
    )


def _make_viewer(qtbot, tmp_path, pdf):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(VIEW_W, VIEW_H)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    assert viewer.open_document(pdf) is True
    qtbot.wait(200)
    return viewer


def _wide_viewer(qtbot, tmp_path):
    pdf = make_probe_pdf(str(tmp_path / "wide.pdf"),
                         page_width=1600.0, page_height=800.0)
    return _make_viewer(qtbot, tmp_path, pdf)


# -- layout coordinate math (no Qt) -------------------------------------

def test_total_width_is_widest_page():
    layout = _layout_two()
    assert layout.total_width_px == 1600
    empty = DocumentLayout(DocumentId("e"), [], None, 1.0)
    assert empty.total_width_px == 0


def test_page_x_origin_matches_legacy_when_fitting():
    # C = max(V, W) = V when the document fits: legacy centering.
    assert DocumentLayout.page_x_origin(1100.0, 600.0, 600.0) == 250.0
    assert DocumentLayout.page_x_origin(800.0, 800.0, 595.0) == 102.5


def test_page_x_origin_content_centered_when_scrollable():
    layout = _layout_two()
    assert layout.total_width_px == 1600
    # Widest page at doc x=0; narrower page centered in content width.
    assert DocumentLayout.page_x_origin(1100.0, 1600.0, 1600.0) == 0.0
    assert DocumentLayout.page_x_origin(1100.0, 1600.0, 600.0) == 500.0


def test_page_x_origin_continuous_across_boundary():
    # W = V from either side yields the same origin: no jump possible.
    just_fits = DocumentLayout.page_x_origin(1100.0, 1099.0, 600.0)
    just_scrolls = DocumentLayout.page_x_origin(1100.0, 1101.0, 600.0)
    assert just_fits == 250.0
    assert just_scrolls == 250.5
    assert abs(just_scrolls - just_fits) <= 1.0


def test_page_viewport_scroll_x_defaults_to_zero():
    layout = _layout_two()
    vs = layout.page_viewport(0, 0.0, 1100, 750, generation=3)
    assert vs.scroll_x == 0.0


def test_page_viewport_subtracts_origin_per_page():
    layout = _layout_two()
    # Widest page (index 1): page-local scroll equals doc scroll.
    vs_wide = layout.page_viewport(1, 0.0, 1100, 750, 3, scroll_x_doc=500.0)
    assert vs_wide.scroll_x == 500.0
    # Narrow page origin is 500: scroll left of it floors at 0 ...
    vs_narrow = layout.page_viewport(0, 0.0, 1100, 750, 3, scroll_x_doc=200.0)
    assert vs_narrow.scroll_x == 0.0
    # ... and scroll right of it converts to page-local correctly.
    vs_narrow2 = layout.page_viewport(0, 0.0, 1100, 750, 3, scroll_x_doc=700.0)
    assert vs_narrow2.scroll_x == 200.0


# -- pan channel ----------------------------------------------------------

def test_horizontal_pan_moves_x_only(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    assert viewer._hscrollbar.maximum() == 500
    viewer._apply_intent({"pan_by": (60.0, 0.0)})
    assert viewer._hscrollbar.value() == 60
    assert viewer._scrollbar.value() == 0


def test_vertical_pan_unchanged(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._apply_intent({"pan_by": (0.0, 40.0)})
    assert viewer._scrollbar.value() == 40
    assert viewer._hscrollbar.value() == 0


def test_diagonal_pan_moves_both(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._apply_intent({"pan_by": (60.0, 40.0)})
    assert viewer._hscrollbar.value() == 60
    assert viewer._scrollbar.value() == 40


def test_horizontal_clamps_both_ends(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._apply_intent({"pan_by": (10_000.0, 0.0)})
    assert viewer._hscrollbar.value() == viewer._hscrollbar.maximum() == 500
    viewer._apply_intent({"pan_by": (-10_000.0, 0.0)})
    assert viewer._hscrollbar.value() == 0


def test_gesture_flows_end_to_end_into_horizontal_channel(
        qtbot, tmp_path, cleared_settings):
    from annotations.interaction import InteractionController
    from tools.models import ToolInstance

    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._interaction = InteractionController(
        ToolInstance(kind=ToolType.PAN, name="Pan"))
    assert viewer._interaction.handle_press(500.0, 300.0) == {"noop": True}
    intent = viewer._interaction.handle_move(440.0, 300.0)
    assert intent == {"pan_by": (60.0, 0.0)}
    viewer._apply_intent(intent)
    assert viewer._hscrollbar.value() == 60
    assert viewer._scrollbar.value() == 0


# -- reachability / bounds -------------------------------------------------

def test_wide_page_edges_reachable(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    layout = viewer._pipeline.layout
    bucket = float(layout.raster_scale)
    width_pt = float(layout.entry(0).render_width_px) / bucket
    y = 400.0  # mid-page in widget px (scale 1.0, no vertical scroll yet)

    viewer._hscrollbar.setValue(0)
    left = viewer._event_to_page_pt(2.0, y)
    assert left is not None and abs(left[1]) < 5.0

    viewer._hscrollbar.setValue(viewer._hscrollbar.maximum())
    right = viewer._event_to_page_pt(float(VIEW_W - 2), y)
    assert right is not None and abs(right[1] - width_pt) < 5.0

    viewer._hscrollbar.setValue(viewer._hscrollbar.maximum() // 2)
    mid = viewer._event_to_page_pt(float(VIEW_W // 2), y)
    assert mid is not None and abs(mid[1] - width_pt / 2.0) < 5.0


def test_narrow_document_has_no_horizontal_travel(
        qtbot, tmp_path, cleared_settings):
    pdf = make_probe_pdf(str(tmp_path / "narrow.pdf"),
                         page_width=600.0, page_height=800.0)
    viewer = _make_viewer(qtbot, tmp_path, pdf)
    assert viewer._hscrollbar.maximum() == 0
    viewer._apply_intent({"pan_by": (500.0, 0.0)})
    assert viewer._hscrollbar.value() == 0
    assert viewer._scroll_doc_x() == 0.0


def test_mixed_width_pages_share_one_system(
        qtbot, tmp_path, cleared_settings):
    pdf = make_sized_pdf(str(tmp_path / "mixed.pdf"),
                         [(600.0, 800.0), (1600.0, 800.0)])
    viewer = _make_viewer(qtbot, tmp_path, pdf)
    layout = viewer._pipeline.layout
    assert layout.total_width_px == 1600
    assert viewer._hscrollbar.maximum() == 500
    # Narrow page (index 0, y 0..800): mapping resolves to its page id
    # with in-range coordinates while scrolled right.
    viewer._hscrollbar.setValue(500)
    mapped = viewer._event_to_page_pt(600.0, 400.0)
    assert mapped is not None
    assert mapped[0] == layout.entry(0).page_id
    assert 0.0 <= mapped[1] <= 600.0


def test_gutter_positions_rejected_not_clamped(
        qtbot, tmp_path, cleared_settings):
    pdf = make_probe_pdf(str(tmp_path / "narrow.pdf"),
                         page_width=600.0, page_height=800.0)
    viewer = _make_viewer(qtbot, tmp_path, pdf)
    # Page starts at x=250 (centered 600 in 1100); x=100 is gutter.
    assert viewer._event_to_page_pt(100.0, 300.0) is None
    assert viewer._event_to_page_pt(300.0, 300.0) is not None


# -- zoom / resize anchoring -----------------------------------------------

def test_zoom_preserves_viewport_center_anchor(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._hscrollbar.setValue(200)
    scale_before = viewer._display_scale()
    vw_before = float(VIEW_W) / scale_before
    anchor = viewer._scroll_doc_x() + vw_before / 2.0
    old_w = float(viewer._pipeline.layout.total_width_px)

    viewer.set_zoom(2.0)

    layout = viewer._pipeline.layout
    new_w = float(layout.total_width_px)
    ratio = new_w / old_w
    scale_after = viewer._display_scale()
    vw_after = float(VIEW_W) / scale_after
    expected = min(max(anchor * ratio - vw_after / 2.0, 0.0),
                   max(0.0, new_w - vw_after))
    assert viewer._scroll_doc_x() == pytest.approx(expected, abs=2.0)
    # Viewport center still shows the same document point (scaled).
    center_now = viewer._scroll_doc_x() + vw_after / 2.0
    assert center_now == pytest.approx(anchor * ratio, abs=2.0)
    hmax = viewer._hscrollbar.maximum()
    assert 0 <= viewer._hscrollbar.value() <= hmax


def test_zoom_out_to_fit_settles_and_centers(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._hscrollbar.setValue(500)
    viewer.set_zoom(0.25)
    assert viewer._hscrollbar.maximum() == 0
    assert viewer._hscrollbar.value() == 0
    assert viewer._scroll_doc_x() == 0.0
    # Page is viewport-centered again (no silent offset retained).
    layout = viewer._pipeline.layout
    scale = viewer._display_scale()
    vw_render, _vh = viewer._render_viewport()
    rect = page_screen_rect(float(layout.entry(0).y_offset_px),
                            float(layout.entry(0).render_width_px),
                            float(layout.entry(0).render_height_px),
                            viewer._scroll_doc(), vw_render, scale,
                            scroll_x_doc=viewer._scroll_doc_x(),
                            content_width_px=float(layout.total_width_px))
    page_logical_w = float(layout.entry(0).render_width_px) * scale
    assert rect.x == pytest.approx((VIEW_W - page_logical_w) / 2.0)


def test_resize_crossing_boundary_settles_and_restores(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    assert viewer._hscrollbar.maximum() == 500
    viewer.resize(2000, VIEW_H)
    qtbot.wait(100)
    assert viewer._hscrollbar.maximum() == 0
    assert viewer._hscrollbar.value() == 0
    viewer.resize(VIEW_W, VIEW_H)
    qtbot.wait(100)
    assert viewer._hscrollbar.maximum() == 500


def test_resize_preserves_center_anchor(qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._hscrollbar.setValue(200)
    anchor = 200.0 + 1100.0 / 2.0  # scale 1.0: doc point under center
    viewer.resize(900, VIEW_H)
    qtbot.wait(100)
    # max = 1600-900 = 700; anchor 750 -> s' = 750-450 = 300.
    assert viewer._hscrollbar.maximum() == 700
    assert viewer._hscrollbar.value() == 300
    viewer.resize(VIEW_W, VIEW_H)
    qtbot.wait(100)


# -- consistency: paint <-> input <-> planning -------------------------------

def test_round_trip_stable_at_nonzero_scroll(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    viewer._hscrollbar.setValue(500)
    layout = viewer._pipeline.layout
    entry = layout.entry(0)
    bucket = float(layout.raster_scale)
    scale = viewer._display_scale()
    vw_render, _vh = viewer._render_viewport()
    rect = page_screen_rect(float(entry.y_offset_px),
                            float(entry.render_width_px),
                            float(entry.render_height_px),
                            viewer._scroll_doc(), vw_render, scale,
                            scroll_x_doc=viewer._scroll_doc_x(),
                            content_width_px=float(layout.total_width_px))
    width_pt = float(entry.render_width_px) / bucket
    # Fractions chosen inside the visible [500, 1600] band at s=500.
    for frac in (0.35, 0.5, 0.65, 0.8, 0.95):
        lx = width_pt * frac
        sx = rect.x + lx * bucket * scale
        sy = rect.y + 400.0 * scale
        mapped = viewer._event_to_page_pt(sx, sy)
        assert mapped is not None
        assert mapped[0] == entry.page_id
        assert mapped[1] == pytest.approx(lx, abs=1e-6)


def test_visible_plan_covers_scrolled_region(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    pipe = viewer._pipeline
    layout = pipe.layout
    vw, vh = viewer._render_viewport()
    planned = pipe.visible_plan(0.0, int(vw), int(vh), scroll_x_doc=500.0)
    page0 = [k for k in planned if k.page_id == layout.entry(0).page_id]
    assert page0, "scrolled viewport must plan tiles for the wide page"
    xs = [(k.tile_x, k.tile_x + k.tile_w) for k in page0]
    assert min(x for x, _ in xs) <= 500.0 + 256.0
    assert max(x for _, x in xs) >= 1600.0
    # Per-page subtraction: narrow page in a wide doc plans its own band.
    pdf = make_sized_pdf(str(tmp_path / "mixed.pdf"),
                         [(600.0, 800.0), (1600.0, 800.0)])
    viewer2 = _make_viewer(qtbot, tmp_path, pdf)
    pipe2 = viewer2._pipeline
    layout2 = pipe2.layout
    vs = layout2.page_viewport(0, 0.0, 1100, 750, 1, scroll_x_doc=500.0)
    assert vs.scroll_x == 0.0  # 500 - origin(500) floors at 0
    planned2 = pipe2.visible_plan(0.0, 1100, 750, scroll_x_doc=500.0)
    narrow = [k for k in planned2 if k.page_id == layout2.entry(0).page_id]
    assert narrow, "narrow page must still be planned while scrolled"
    assert max(x for _, x in [(k.tile_x, k.tile_x + k.tile_w)
                              for k in narrow]) >= 600.0


def test_pen_stroke_aligned_while_scrolled(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P1", color="#112233",
                                      width_mm=0.7, opacity=1.0)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(slot)
    viewer._hscrollbar.setValue(500)
    y = 400.0
    start = viewer._event_to_page_pt(100.0, y)
    end = viewer._event_to_page_pt(300.0, y)
    assert start is not None and end is not None
    viewer._apply_intent(viewer._interaction.handle_pen_press(
        start[0], start[1], start[2]))
    viewer._apply_intent(viewer._interaction.handle_pen_move(
        end[0], end[1], end[2]))
    viewer._apply_intent(viewer._interaction.handle_pen_release(
        end[0], end[1], end[2]))
    assert len(committed_strokes(viewer)) == 1
    pts = committed_strokes(viewer)[0].points
    assert pts[0].x == pytest.approx(start[1])
    assert pts[-1].x == pytest.approx(end[1])
    # Stored span equals the on-screen span (200 logical px at scale 1).
    assert (pts[-1].x - pts[0].x) == pytest.approx(200.0)


def test_vertical_range_coexists_with_horizontal(
        qtbot, tmp_path, cleared_settings):
    viewer = _wide_viewer(qtbot, tmp_path)
    assert viewer._scrollbar.maximum() == 50  # 800 - 750, unchanged formula
    assert viewer._hscrollbar.maximum() == 500
    viewer._apply_intent({"pan_by": (60.0, 40.0)})
    viewer._sync_scrollbar(preserve_fraction=True)
    assert viewer._scrollbar.value() == 40
    assert viewer._hscrollbar.value() == 60
