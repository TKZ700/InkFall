"""B.2.3 Stage 2: Soft/Normal/Hard pressure-response profiles.

Curve tables, latch isolation, Settings persistence + UI, and
rendering-path integration. B.2.4 retuned all three curves (normal is
now the linear (0.2, 1.0) candidate — see
test_normal_matches_retuned_linear_formula); the per-tool Pressure
toggle stays independent.
"""

from __future__ import annotations

import pytest

from annotations.ink_geometry import (
    DEFAULT_PRESSURE_PROFILE,
    PRESSURE_PROFILES,
    pressure_width,
    pressure_widths,
)
from annotations.interaction import InteractionController, PenHandler
from annotations.models import InkStroke
from core.types import AnnotationId, PageId, Point
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")

PRESSURES = [0.0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0]

EXPECTED = {
    "soft": [0.300, 0.511, 0.579, 0.668, 0.760, 0.830, 0.924, 1.000],
    "normal": [0.200, 0.240, 0.280, 0.360, 0.480, 0.600, 0.800, 1.000],
    "hard": [0.200, 0.204, 0.213, 0.244, 0.321, 0.430, 0.677, 1.000],
}


def _pen_tool(pressure=False):
    return ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#ff0000",
                        width_mm=3.0, opacity=1.0, pressure=pressure)


# -- curve tables ----------------------------------------------------------

def test_profile_table_declared():
    assert set(PRESSURE_PROFILES) == {"soft", "normal", "hard"}
    assert DEFAULT_PRESSURE_PROFILE == "normal"


def test_exact_width_ratios_per_profile():
    for profile, expected in EXPECTED.items():
        got = [pressure_width(p, 1.0, profile) for p in PRESSURES]
        assert got == pytest.approx(expected, abs=1e-3), profile


def test_normal_matches_retuned_linear_formula():
    # B.2.4 DELIBERATE CHANGE: "normal" is no longer the B.2.1 (0.3,
    # 0.8) curve. The old curve's working band spanned only ~1.8x in
    # width and was judged too subtle on hardware; the B.2.4 candidate
    # is linear (0.2, 1.0): width = 20% + 80% x pressure, ~2.4x across
    # the 0.1-0.6 band. This test pins the NEW formula exactly.
    import math

    for p in [i / 100.0 for i in range(101)]:
        linear = 0.2 + 0.8 * max(0.0, min(1.0, p))
        assert pressure_width(p, 2.5, "normal") == pytest.approx(
            2.5 * linear)
        # Default profile argument reproduces the old call shape.
        assert pressure_width(p, 2.5) == pressure_width(p, 2.5, "normal")


def test_ordering_soft_ge_normal_ge_hard():
    for i in range(1001):
        p = i / 1000.0
        s = pressure_width(p, 1.0, "soft")
        n = pressure_width(p, 1.0, "normal")
        h = pressure_width(p, 1.0, "hard")
        assert s >= n >= h
        for v in (s, n, h):
            assert 0.0 < v <= 1.0 and v == v  # finite, positive


def test_each_profile_monotonic():
    for profile in PRESSURE_PROFILES:
        prev = 0.0
        for i in range(1001):
            w = pressure_width(i / 1000.0, 1.0, profile)
            assert w >= prev
            prev = w


def test_soft_answers_light_pressure_hard_holds_back():
    assert pressure_width(0.05, 1.0, "soft") > 0.45
    assert pressure_width(0.1, 1.0, "soft") > 0.5
    assert pressure_width(0.1, 1.0, "hard") < 0.3
    assert pressure_width(0.5, 1.0, "hard") < 0.6
    for profile in PRESSURE_PROFILES:
        assert pressure_width(1.0, 3.0, profile) == pytest.approx(3.0)


def test_unknown_profile_raises_at_mapping():
    with pytest.raises(ValueError):
        pressure_width(0.5, 1.0, "velvet")
    with pytest.raises(ValueError):
        pressure_widths([0.5], 1.0, "velvet")
    with pytest.raises(ValueError):
        pressure_widths([0.5], 1.0, None)


def test_widths_sequence_maps_profile():
    got = pressure_widths([0.0, 0.5, 1.0], 2.0, "hard")
    assert got == tuple(pressure_width(p, 2.0, "hard") for p in (0.0, 0.5, 1.0))
    assert pressure_widths((), 1.0, "soft") == ()


# -- model field -----------------------------------------------------------

def test_profile_defaults_to_normal():
    assert InkStroke().pressure_profile == "normal"


def test_legacy_positional_construction_unaffected():
    stroke = InkStroke(AnnotationId(), PageId(), 0.0,
                       (Point(0.0, 0.0),), "#000000", 0.5, 1.0, (), (),
                       True)
    assert stroke.pressure_enabled is True
    assert stroke.pressure_profile == "normal"


def test_explicit_profile_retained_and_sanitized():
    assert InkStroke(points=(Point(0, 0),),
                     pressure_profile="soft").pressure_profile == "soft"
    assert InkStroke(points=(Point(0, 0),),
                     pressure_profile="velvet").pressure_profile == "normal"
    assert InkStroke(points=(Point(0, 0),),
                     pressure_profile="").pressure_profile == "normal"


# -- latch -----------------------------------------------------------------

def test_profile_latched_at_stroke_start():
    ctrl = InteractionController(_pen_tool(pressure=True),
                                 profile_resolver=lambda: "soft")
    live = ctrl.handle_pen_press(PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"]
    assert live.pressure_profile == "soft"


def test_midstroke_profile_change_keeps_active_next_adopts():
    state = {"profile": "soft"}
    ctrl = InteractionController(
        _pen_tool(pressure=True),
        profile_resolver=lambda: state["profile"])
    page = PageId()
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.4, 100)
    state["profile"] = "hard"
    mid = ctrl.handle_pen_move(page, 5.0, 0.0, 0.5, 116)["stroke_update"]
    assert mid.pressure_profile == "soft"
    ctrl.handle_pen_release(page, 10.0, 0.0, 0.5, 132)
    nxt = ctrl.handle_pen_press(page, 20.0, 0.0, 0.5, 200)["stroke_update"]
    assert nxt.pressure_profile == "hard"


def test_resolver_failure_and_none_fall_back_to_normal():
    def boom():
        raise RuntimeError("unreadable")

    ctrl = InteractionController(_pen_tool(pressure=True),
                                 profile_resolver=boom)
    assert ctrl.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_profile == \
        "normal"
    ctrl2 = InteractionController(_pen_tool(pressure=True),
                                  profile_resolver=lambda: "velvet")
    assert ctrl2.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_profile == \
        "normal"
    ctrl3 = InteractionController(_pen_tool(pressure=True))
    assert ctrl3.handle_pen_press(
        PageId(), 0.0, 0.0, 0.4, 100)["stroke_update"].pressure_profile == \
        "normal"


def test_configure_profile_override_matrix():
    pen = PenHandler()
    pen.configure(_pen_tool(), profile_override="hard")
    assert pen.down(PageId(), 0.0, 0.0)["stroke_update"].pressure_profile == \
        "hard"
    pen.configure(_pen_tool(), profile_override="velvet")
    assert pen.down(PageId(), 1.0, 1.0)["stroke_update"].pressure_profile == \
        "normal"
    pen.configure(_pen_tool())
    assert pen.down(PageId(), 2.0, 2.0)["stroke_update"].pressure_profile == \
        "normal"


# -- settings persistence + UI ----------------------------------------------

@pytest.fixture()
def profile_env(qapp):
    """Working QSettings hive for profile tests.

    pytest-qt leaves org/app names empty, under which NativeFormat
    QSettings silently drops writes. Point the session at a test hive
    (restored afterwards) so the real persistence functions round-trip.
    """
    from PySide6.QtCore import QCoreApplication, QSettings

    from app.settings_panel import PROFILE_KEY

    prev_org = QCoreApplication.organizationName()
    prev_app = QCoreApplication.applicationName()
    QCoreApplication.setOrganizationName("InkfallTest")
    QCoreApplication.setApplicationName("InkfallTest")
    old = QSettings().value(PROFILE_KEY, None)
    yield
    try:
        if old is None:
            QSettings().remove(PROFILE_KEY)
        else:
            QSettings().setValue(PROFILE_KEY, old)
        QCoreApplication.setOrganizationName(prev_org)
        QCoreApplication.setApplicationName(prev_app)
    except Exception:
        pass


def test_settings_round_trip_and_default(profile_env):
    from app.settings_panel import (
        DEFAULT_PROFILE,
        PROFILE_CHOICES,
        pressure_profile,
        set_pressure_profile,
    )

    assert DEFAULT_PROFILE == "normal"
    assert tuple(PROFILE_CHOICES) == ("soft", "normal", "hard")
    set_pressure_profile("soft")
    assert pressure_profile() == "soft"
    set_pressure_profile("hard")
    assert pressure_profile() == "hard"
    set_pressure_profile("velvet")
    assert pressure_profile() == "normal"


def test_settings_modal_shows_profile_section(qtbot, profile_env):
    from PySide6.QtWidgets import QLabel, QPushButton

    from app.main_window import MainWindow

    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    try:
        modal = window.settings_modal
        assert modal.findChild(QLabel, "inkfall-profile-title") is not None
        labels = sorted(b.text() for b in modal.findChildren(QPushButton)
                        if b.text() in ("Soft", "Normal", "Hard"))
        assert labels == ["Hard", "Normal", "Soft"]
        normal_btn = next(b for b in modal.findChildren(QPushButton)
                          if b.text() == "Normal")
        soft_btn = next(b for b in modal.findChildren(QPushButton)
                        if b.text() == "Soft")
        assert normal_btn.isChecked() is True
        soft_btn.click()
        from app.settings_panel import pressure_profile

        assert pressure_profile() == "soft"
        assert soft_btn.isChecked() is True
    finally:
        window.close()


# -- rendering-path integration ----------------------------------------------

def _make_viewer(qtbot):
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    return viewer


def test_profile_selects_rendering_widths(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        pts = tuple(Point(float(i * 10), 0.0) for i in range(3))
        prs = (0.1, 0.1, 0.1)
        tms = (1, 2, 3)
        soft = InkStroke(points=pts, color="#ff0000", width_mm=3.0,
                         pressures=prs, times_ms=tms, pressure_enabled=True,
                         pressure_profile="soft")
        hard = InkStroke(points=pts, color="#ff0000", width_mm=3.0,
                         pressures=prs, times_ms=tms, pressure_enabled=True,
                         pressure_profile="hard")
        soft_o = viewer._pressure_outline(soft)
        hard_o = viewer._pressure_outline(hard)
        assert soft_o is not None and hard_o is not None
        soft_h = max(abs(p.y) for p in soft_o.polygon)
        hard_h = max(abs(p.y) for p in hard_o.polygon)
        assert soft_h > hard_h
    finally:
        viewer.close()


def test_disabled_path_ignores_profile(qtbot):
    viewer = _make_viewer(qtbot)
    try:
        stroke = InkStroke(points=(Point(0.0, 0.0), Point(1.0, 0.0)),
                           pressures=(0.1, 0.9), times_ms=(1, 2),
                           pressure_enabled=False,
                           pressure_profile="soft")
        assert viewer._pressure_outline(stroke) is None
    finally:
        viewer.close()


def test_unknown_stroke_profile_falls_back_to_legacy(qtbot):
    from types import SimpleNamespace

    viewer = _make_viewer(qtbot)
    try:
        bogus = SimpleNamespace(points=(Point(0.0, 0.0), Point(1.0, 0.0)),
                                pressures=(0.5, 0.5),
                                pressure_enabled=True,
                                pressure_profile="velvet", width_mm=1.0)
        assert viewer._pressure_outline(bogus) is None
    finally:
        viewer.close()
