"""M5-B.1 pen stroke capture + overlay.

Part 1 is the coordinate-validation gate: it proves the rotated
page-local PDF-point mapping stays aligned with PDF content across
scroll, zoom, and raster-bucket changes using ONLY existing transforms
(DocumentLayout, page_screen_rect, rotated_render_size). No frozen file
is modified by anything in this feature.
"""

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from app.main_window import MainWindow
from app.viewport_controller import page_screen_rect
from core.types import DocumentId, Size
from document.models import page_id_for
from rendering.coordinates import rotated_render_size
from rendering.layout import DocumentLayout
from tests.helpers import committed_strokes, make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

_A4 = Size(595.0, 842.0)


def _three_page(intrinsics, bucket):
    return DocumentLayout(
        DocumentId(), [_A4, _A4, _A4],
        intrinsic_rotations=intrinsics, raster_scale=bucket)


def _screen_of(entry, lx_pt, ly_pt, bucket, scroll_doc, vw_render,
               ui_zoom):
    """Forward projection, mirroring page_screen_rect exactly."""
    scale = ui_zoom / bucket
    rect = page_screen_rect(entry.y_offset_px, entry.render_width_px,
                            entry.render_height_px, scroll_doc, vw_render,
                            scale)
    return (rect.x + lx_pt * bucket * scale,
            rect.y + ly_pt * bucket * scale)


def _stored_of(layout, sx, sy, scroll_doc, vw_render, ui_zoom):
    """Inverse projection: the exact math _event_to_page_pt will use."""
    bucket = layout.raster_scale
    scale = ui_zoom / bucket
    doc_y = sy / scale + scroll_doc
    pages = layout.pages_in(doc_y, doc_y + 1.0)
    assert pages, "test point must hit a page, not a gap"
    entry = layout.entry(pages[0])
    x_off = max(0.0, (vw_render - entry.render_width_px) / 2.0)
    lx = (sx / scale - x_off) / bucket
    ly = (doc_y - entry.y_offset_px) / bucket
    assert 0.0 <= lx <= entry.render_width_px / bucket
    assert 0.0 <= ly <= entry.render_height_px / bucket
    return pages[0], lx, ly


def test_stored_point_invariant_across_scroll_zoom_bucket():
    """Same content maps to the same stored point under any view."""
    lay1 = _three_page([0, 0, 0], 1.0)
    lay2 = _three_page([0, 0, 0], 2.0)
    entry = lay1.entry(1)
    # Content point: page-local rotated pt on page 1.
    px, py = 100.0, 200.0
    s1 = _screen_of(entry, px, py, 1.0, 0.0, 800.0, 1.0)
    e2 = lay2.entry(1)
    s2 = _screen_of(e2, px, py, 2.0, 173.5, 800.0, 1.7)
    got1 = _stored_of(lay1, *s1, 0.0, 800.0, 1.0)
    got2 = _stored_of(lay2, *s2, 173.5, 800.0, 1.7)
    assert got1[0] == got2[0] == 1
    assert abs(got1[1] - px) < 1e-6 and abs(got1[2] - py) < 1e-6
    assert abs(got2[1] - px) < 1e-6 and abs(got2[2] - py) < 1e-6


def test_rotation_applied_exactly_once():
    """Layout build applies rotation; the mapping adds none."""
    lay = _three_page([0, 90, 0], 2.0)
    exp_w, exp_h = rotated_render_size(_A4, 2.0, 90)
    entry = lay.entry(1)
    assert (entry.render_width_px, entry.render_height_px) == (exp_w, exp_h)
    assert exp_w == 1684 and exp_h == 1190  # swapped, doubled
    assert entry.y_offset_px == 842 * 2 + 16  # rotated height + gap
    # Round-trip on the rotated page is exact with no extra rotation.
    # (Rotated page is 842x595pt: keep the point inside those bounds.)
    px, py = 700.0, 300.0
    s = _screen_of(entry, px, py, 2.0, 1500.0, 900.0, 1.25)
    idx, lx, ly = _stored_of(lay, *s, 1500.0, 900.0, 1.25)
    assert idx == 1
    assert abs(lx - px) < 1e-6 and abs(ly - py) < 1e-6
    # Stored bounds derive from the rotated entry dims, not unrotated pt.
    assert entry.render_width_px / 2.0 == 842.0
    assert entry.render_height_px / 2.0 == 595.0


def test_page_identity_resolves_via_page_id_for():
    """Stroke page binding uses the deterministic page_id_for mapping."""
    from core.types import DocumentId as _D

    doc = _D()
    lay = DocumentLayout(doc, [_A4, _A4], [0, 0], raster_scale=1.0)
    assert lay.entry(1).page_id == page_id_for(doc, 1)
    by_id = {lay.entry(i).page_id: lay.entry(i) for i in range(len(lay))}
    assert by_id[page_id_for(doc, 0)].index == 0
    assert by_id[page_id_for(doc, 1)].index == 1


def test_page_identity_resolves_via_page_id_for():
    """Stroke page binding uses the deterministic page_id_for mapping."""
    from core.types import DocumentId as _D

    doc = _D()
    lay = DocumentLayout(doc, [_A4, _A4], [0, 0], raster_scale=1.0)
    assert lay.entry(1).page_id == page_id_for(doc, 1)
    by_id = {lay.entry(i).page_id: lay.entry(i) for i in range(len(lay))}
    assert by_id[page_id_for(doc, 0)].index == 0
    assert by_id[page_id_for(doc, 1)].index == 1


# -- Part 2: viewer integration (offscreen Qt) -------------------------------

@pytest.fixture()
def cleared_settings():
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


def _pen_tool():
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#ff0000",
                        width_mm=1.0, opacity=0.9)


def _open_pen_window(qtbot, tmp_path, tool=None):
    pdf = make_probe_pdf(str(tmp_path / "pen.pdf"), page_width=600.0,
                         page_height=800.0)
    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer._interaction.set_tool(tool or _pen_tool())
    qtbot.wait(200)
    return window, viewer


def _press(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _move(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _release(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def test_press_move_release_produces_one_valid_stroke(qtbot, tmp_path,
                                                      cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        _press(viewer, 300.0, 150.0)
        _move(viewer, 350.0, 160.0)
        _move(viewer, 400.0, 170.0)
        _release(viewer, 400.0, 170.0)
        assert len(committed_strokes(viewer)) == 1
        assert viewer._live_stroke is None
        stroke = committed_strokes(viewer)[0]
        assert len(stroke.points) == 3
        assert (stroke.color, stroke.width_mm, stroke.opacity) == (
            "#ff0000", 1.0, 0.9)
        # Stored points equal the mapped inputs (no drift at capture).
        mapped = [viewer._event_to_page_pt(x, y)
                  for x, y in ((300.0, 150.0), (350.0, 160.0),
                               (400.0, 170.0))]
        assert all(m is not None for m in mapped)
        assert [m[0] for m in mapped] == [stroke.page_id] * 3
        for (point, m) in zip(stroke.points, mapped):
            assert abs(point.x - m[1]) < 1e-9
            assert abs(point.y - m[2]) < 1e-9
    finally:
        viewer.close_document()


def test_live_stroke_rendered_before_release_and_kept_after(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        _press(viewer, 300.0, 300.0)
        _move(viewer, 500.0, 300.0)
        live = viewer._live_stroke
        assert live is not None and len(live.points) == 2
        assert _pixel_is_stroke_red(viewer, 400.0, 300.0)
        _release(viewer, 500.0, 300.0)
        assert viewer._live_stroke is None
        assert len(committed_strokes(viewer)) == 1
        assert _pixel_is_stroke_red(viewer, 400.0, 300.0)
    finally:
        viewer.close_document()


def _pixel_is_stroke_red(viewer, x, y) -> bool:
    try:
        image = viewer.grab().toImage()
        color = image.pixelColor(int(x), int(y))
        return color.red() > 150 and color.green() < 150 \
            and color.blue() < 150
    except Exception:
        return False


def test_stored_coordinates_survive_scroll_and_zoom(qtbot, tmp_path,
                                                    cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        _press(viewer, 300.0, 150.0)
        _move(viewer, 400.0, 170.0)
        _release(viewer, 400.0, 170.0)
        before = [(p.x, p.y) for p in committed_strokes(viewer)[0].points]
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        assert [(p.x, p.y) for p in committed_strokes(viewer)[0].points] == before
        viewer.set_zoom(2.0)
        viewer.set_zoom(4.0)  # raster bucket changes 1.0 -> 4.0
        assert viewer._pipeline.layout.raster_scale == 4.0
        after = [(p.x, p.y) for p in committed_strokes(viewer)[0].points]
        assert after == before
        # The stored content reprojects onto itself under the new view:
        # screen pos of a stored point maps back to the same stored pt.
        layout = viewer._pipeline.layout
        entry = layout.entry(1) if layout.page_count > 1 else layout.entry(0)
        scale = viewer._display_scale()
        vw_render, _vh = viewer._render_viewport()
        rect = page_screen_rect(float(entry.y_offset_px),
                                float(entry.render_width_px),
                                float(entry.render_height_px),
                                viewer._scroll_doc(), vw_render, scale,
                                scroll_x_doc=viewer._scroll_doc_x(),
                                content_width_px=float(
                                    layout.total_width_px))
        lx, ly = after[0]
        bucket = layout.raster_scale
        sx = rect.x + lx * bucket * scale
        sy = rect.y + ly * bucket * scale
        assert viewer._is_pen_active()
        remapped = viewer._event_to_page_pt(sx, sy)
        assert remapped is not None
        assert remapped[0] == committed_strokes(viewer)[0].page_id
        assert abs(remapped[1] - lx) < 1e-6
        assert abs(remapped[2] - ly) < 1e-6
    finally:
        viewer.close_document()


def test_single_point_gesture_is_safe(qtbot, tmp_path, cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        _press(viewer, 320.0, 220.0)
        _release(viewer, 320.0, 220.0)
        assert len(committed_strokes(viewer)) == 1
        assert len(committed_strokes(viewer)[0].points) == 1
        viewer.repaint()
        QTest.qWait(100)
    finally:
        viewer.close_document()


def test_tool_switch_discards_live_stroke_without_creating(qtbot, tmp_path,
                                                           cleared_settings):
    from tools.models import ToolInstance as _TI
    from tools.models import ToolType as _TT

    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        _press(viewer, 300.0, 150.0)
        _move(viewer, 350.0, 160.0)
        assert viewer._live_stroke is not None
        viewer._on_tool_selected(_TI(kind=_TT.PAN, name="Pan"))
        assert viewer._live_stroke is None
        assert committed_strokes(viewer) == []
        # Pan still works after the switch (scrollbar moves on drag).
        before = viewer._scrollbar.value()
        _press(viewer, 400.0, 400.0)
        _move(viewer, 400.0, 300.0)
        _release(viewer, 400.0, 300.0)
        assert viewer._scrollbar.value() != before
        assert committed_strokes(viewer) == []
    finally:
        viewer.close_document()


def test_pan_behavior_unchanged(qtbot, tmp_path, cleared_settings):
    from tools.models import ToolInstance as _TI
    from tools.models import ToolType as _TT

    window, viewer = _open_pen_window(
        qtbot, tmp_path, tool=_TI(kind=_TT.PAN, name="Pan"))
    try:
        before = viewer._scrollbar.value()
        _press(viewer, 400.0, 400.0)
        _move(viewer, 400.0, 300.0)
        _release(viewer, 400.0, 300.0)
        assert viewer._scrollbar.value() != before
        assert committed_strokes(viewer) == []
        assert viewer._live_stroke is None
    finally:
        viewer.close_document()


def test_annotation_drawing_leaves_tiles_cache_untouched(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        viewer._timer.stop()
        pipeline = viewer._pipeline
        calls: list = []
        orig_update = pipeline.update
        orig_zoom = pipeline.set_zoom
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(
                pipeline, "update",
                lambda *a, **k: (calls.append("update"),
                                 orig_update(*a, **k)))
            monkeypatch.setattr(
                pipeline, "set_zoom",
                lambda *a, **k: (calls.append("set_zoom"),
                                 orig_zoom(*a, **k)))
            epoch_before = pipeline.epoch
            display_before = dict(viewer._display)
            _press(viewer, 300.0, 150.0)
            _move(viewer, 350.0, 160.0)
            _move(viewer, 400.0, 170.0)
            _release(viewer, 400.0, 170.0)
            assert calls == []
            assert pipeline.epoch == epoch_before
            assert viewer._display.keys() == display_before.keys()
        finally:
            monkeypatch.undo()
            viewer._timer.start()
    finally:
        viewer.close_document()


def test_interrupted_gesture_leaves_no_invalid_or_duplicate_strokes(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_pen_window(qtbot, tmp_path)
    try:
        # Overlapping press/move/release at identical coords: deduped.
        _press(viewer, 300.0, 150.0)
        _move(viewer, 300.0, 150.0)
        _move(viewer, 350.0, 150.0)
        _release(viewer, 350.0, 150.0)
        assert len(committed_strokes(viewer)) == 1
        pts = [(p.x, p.y) for p in committed_strokes(viewer)[0].points]
        assert len(pts) == 2
        assert pts[0] != pts[1]
        # Interrupted (cancelled) gesture completes nothing.
        _press(viewer, 300.0, 250.0)
        _move(viewer, 350.0, 250.0)
        viewer._apply_intent(viewer._interaction.cancel())
        assert viewer._live_stroke is None
        assert len(committed_strokes(viewer)) == 1
    finally:
        viewer.close_document()
