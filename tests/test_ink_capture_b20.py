"""M5-B.2.0: per-sample pressure/timestamp capture (no visual change).

Deterministic by construction: controller-level tests pass explicit
values; viewer-level tests use synthetic QMouseEvents and duck-typed
tablet stubs (no hardware). Rendering behavior is asserted unchanged.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from annotations.interaction import InteractionController
from annotations.models import InkStroke
from core.types import PageId, Point
from tests.helpers import committed_strokes, make_probe_pdf
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


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
    return ToolInstance(kind=ToolType.PEN, name="Pen", color="#112233",
                        width_mm=0.7, opacity=0.8)


def _page():
    return PageId()


# -- model ----------------------------------------------------------------

def test_legacy_construction_has_empty_capture_arrays():
    stroke = InkStroke(page_id=_page(),
                       points=(Point(0.0, 0.0), Point(1.0, 1.0)))
    assert stroke.pressures == ()
    assert stroke.times_ms == ()
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#000000", 0.5, 1.0)


def test_mismatched_array_lengths_raise():
    pts = (Point(0.0, 0.0), Point(1.0, 1.0), Point(2.0, 2.0))
    with pytest.raises(ValueError):
        InkStroke(points=pts, pressures=(1.0, 1.0))
    with pytest.raises(ValueError):
        InkStroke(points=pts, times_ms=(1, 2))


def test_pressure_values_validated_and_clamped():
    pts = (Point(0.0, 0.0), Point(1.0, 1.0))
    stroke = InkStroke(points=pts, pressures=(2.0, -0.5),
                       times_ms=(10, 20))
    assert stroke.pressures == (1.0, 0.0)
    with pytest.raises(ValueError):
        InkStroke(points=pts, pressures=(float("nan"), 1.0))
    with pytest.raises(ValueError):
        InkStroke(points=pts, pressures=(float("inf"), 1.0))
    with pytest.raises(ValueError):
        InkStroke(points=pts, pressures=("hard", 1.0))


# -- controller capture -----------------------------------------------------

def test_controller_captures_pressure_and_time():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 1.0, 2.0, pressure=0.25, now_ms=1000)
    controller.handle_pen_move(page, 3.0, 4.0, pressure=0.75, now_ms=1016)
    done = controller.handle_pen_release(page, 5.0, 6.0, pressure=0.5,
                                         now_ms=1032)
    stroke = done["stroke_done"]
    assert [p.x for p in stroke.points] == [1.0, 3.0, 5.0]
    assert stroke.pressures == (0.25, 0.75, 0.5)
    assert stroke.times_ms == (1000, 1016, 1032)
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#112233", 0.7, 0.8)


def test_duplicate_timestamps_bumped_not_lost():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 0.0, 0.0, now_ms=500)
    controller.handle_pen_move(page, 1.0, 0.0, now_ms=500)
    done = controller.handle_pen_release(page, 2.0, 0.0, now_ms=499)
    stroke = done["stroke_done"]
    assert len(stroke.points) == 3
    assert stroke.times_ms == (500, 501, 502)


def test_invalid_timestamps_fall_back_safely():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 0.0, 0.0, now_ms=None)
    controller.handle_pen_move(page, 1.0, 0.0, now_ms="bad")
    done = controller.handle_pen_release(page, 2.0, 0.0, now_ms=-7)
    stroke = done["stroke_done"]
    assert len(stroke.times_ms) == 3
    assert list(stroke.times_ms) == sorted(stroke.times_ms)
    assert all(isinstance(t, int) for t in stroke.times_ms)


def test_invalid_pressure_falls_back_to_full():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 0.0, 0.0, pressure=float("nan"))
    controller.handle_pen_move(page, 1.0, 0.0, pressure=None)
    done = controller.handle_pen_release(page, 2.0, 0.0, pressure=0.4)
    assert done["stroke_done"].pressures == (1.0, 1.0, 0.4)


def test_single_point_and_cancel_keep_arrays_consistent():
    controller = InteractionController(_pen_tool())
    page = _page()
    done = controller.handle_pen_release(page, 9.0, 9.0, pressure=0.6,
                                         now_ms=5)
    # Release without press still finalizes nothing (existing noop path).
    assert done == {"noop": True}
    update = controller.handle_pen_press(page, 9.0, 9.0, pressure=0.6,
                                         now_ms=5)
    live = update["stroke_update"]
    assert len(live.points) == 1
    assert tuple(live.pressures) == (0.6,)
    assert tuple(live.times_ms) == (5,)
    assert controller.cancel() == {"stroke_cancelled": True}
    # Next stroke starts fresh (no leaked samples).
    update2 = controller.handle_pen_press(page, 1.0, 1.0, pressure=0.9,
                                          now_ms=50)
    assert tuple(update2["stroke_update"].pressures) == (0.9,)
    assert tuple(update2["stroke_update"].times_ms) == (50,)


def test_release_outside_page_finalizes_consistent_arrays():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 0.0, 0.0, pressure=0.3, now_ms=10)
    controller.handle_pen_move(page, 1.0, 0.0, pressure=0.4, now_ms=20)
    done = controller.handle_pen_release(None, 0.0, 0.0, pressure=0.9,
                                         now_ms=30)
    stroke = done["stroke_done"]
    assert len(stroke.points) == 2
    assert stroke.pressures == (0.3, 0.4)
    assert stroke.times_ms == (10, 20)


def test_legacy_calls_default_to_full_pressure():
    controller = InteractionController(_pen_tool())
    page = _page()
    controller.handle_pen_press(page, 0.0, 0.0)
    controller.handle_pen_move(page, 1.0, 0.0)
    done = controller.handle_pen_release(page, 2.0, 0.0)
    stroke = done["stroke_done"]
    assert stroke.pressures == (1.0, 1.0, 1.0)
    assert len(stroke.times_ms) == 3


# -- viewer capture -----------------------------------------------------------

def _make_viewer(qtbot, tmp_path):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    pdf = make_probe_pdf(str(tmp_path / "cap.pdf"), page_width=600.0,
                         page_height=800.0)
    assert viewer.open_document(pdf) is True
    qtbot.wait(200)
    return viewer


def _select_pen(viewer):
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="Cap", color="#112233",
                                      width_mm=0.7, opacity=0.8)
    viewer.tool_shelf.refresh()
    viewer._on_tool_selected(slot)


def _mouse_click(viewer, x, y):
    viewer.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_drag(viewer, x, y):
    viewer.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _mouse_up(viewer, x, y):
    viewer.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


class _TabletStub:
    """Duck-typed stylus event (pressure/timestamp/position/type)."""

    def __init__(self, etype, x, y, pressure, stamp):
        self._etype = etype
        self._pos = QPointF(x, y)
        self._pressure = pressure
        self._stamp = stamp

    def position(self):
        return self._pos

    def type(self):
        return self._etype

    def pressure(self):
        return self._pressure

    def timestamp(self):
        return self._stamp

    def accept(self):
        pass


def test_mouse_draw_captures_full_pressure_and_monotonic_time(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    _mouse_click(viewer, 300.0, 150.0)
    _mouse_drag(viewer, 350.0, 160.0)
    _mouse_up(viewer, 400.0, 170.0)
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert len(stroke.points) == 3
    # Mouse has no pressure: predictable full-baseline fallback.
    assert stroke.pressures == (1.0, 1.0, 1.0)
    assert len(stroke.times_ms) == 3
    assert list(stroke.times_ms) == sorted(stroke.times_ms)
    # No visual-behavior change: tool snapshot identical to before.
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#112233", 0.7, 0.8)


def test_tablet_stub_captures_pressure_ramp(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletPress, 300.0, 150.0,
                                   0.2, 7000))
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletMove, 350.0, 160.0,
                                   0.55, 7016))
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletMove, 400.0, 170.0,
                                   0.9, 7032))
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletRelease, 400.0, 170.0,
                                   0.9, 7040))
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    # Release at the same position dedupes (existing behavior): 3 samples.
    assert stroke.pressures == (0.2, 0.55, 0.9)
    assert stroke.times_ms == (7000, 7016, 7032)
    # B.2.0 captures only: baseline width untouched by pressure.
    assert stroke.width_mm == 0.7


def test_tablet_zero_timestamp_uses_fallback_clock(
        qtbot, tmp_path, cleared_settings):
    viewer = _make_viewer(qtbot, tmp_path)
    _select_pen(viewer)
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletPress, 300.0, 150.0,
                                   0.5, 0))
    viewer.tabletEvent(_TabletStub(QEvent.Type.TabletRelease, 350.0, 160.0,
                                   0.5, 0))
    assert len(committed_strokes(viewer)) == 1
    stroke = committed_strokes(viewer)[0]
    assert stroke.pressures == (0.5, 0.5)
    assert len(stroke.times_ms) == 2
    assert stroke.times_ms[0] <= stroke.times_ms[1]
    assert stroke.times_ms[0] > 0  # real monotonic clock, not the zeros


def test_event_time_ms_prefers_platform_stamp():
    from app.viewer import PdfViewer

    assert PdfViewer._event_time_ms(_TabletStub(
        QEvent.Type.TabletMove, 0.0, 0.0, 1.0, 12345)) == 12345
    fallback = PdfViewer._event_time_ms(_TabletStub(
        QEvent.Type.TabletMove, 0.0, 0.0, 1.0, 0))
    assert fallback > 0
