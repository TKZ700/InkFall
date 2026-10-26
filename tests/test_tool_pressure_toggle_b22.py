"""B.2.2: per-tool pressure toggle + global-settings removal.

Form-level toggle behavior (Qt, offscreen), store persistence, Settings
modal absence of the removed global controls, and viewer latch
integration through the real ``_on_tool_live_changed`` path.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from app.theme.tokens import dark_tokens
from app.toolshelf.tool_popup import PropertiesForm
from core.types import PageId
from tools.models import ToolInstance, ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _slot(kind=ToolType.PEN, pressure=False, **fields):
    base = {"kind": kind, "name": "T1", "color": "#112233",
            "width_mm": 0.7, "opacity": 1.0, "pressure": pressure}
    base.update(fields)
    return ToolInstance(**base)


def _pressure_toggle(form):
    return getattr(form, "_pressure", None)


# -- toggle presence ---------------------------------------------------

def test_toggle_present_for_pen_unchecked_by_default(qtbot):
    form = PropertiesForm(_slot(ToolType.PEN), dark_tokens(), None)
    qtbot.addWidget(form)
    toggle = _pressure_toggle(form)
    assert toggle is not None
    assert toggle._option.isChecked() is False


def test_toggle_reflects_enabled_slot(qtbot):
    form = PropertiesForm(_slot(ToolType.PEN, pressure=True),
                          dark_tokens(), None)
    qtbot.addWidget(form)
    assert _pressure_toggle(form)._option.isChecked() is True


def test_toggle_present_for_highlighter(qtbot):
    form = PropertiesForm(_slot(ToolType.HIGHLIGHTER), dark_tokens(), None)
    qtbot.addWidget(form)
    assert _pressure_toggle(form) is not None


def test_toggle_absent_for_other_kinds(qtbot):
    for kind in (ToolType.ERASER, ToolType.PAN, ToolType.SELECTION,
                 ToolType.LINE, ToolType.NOTE):
        form = PropertiesForm(_slot(kind), dark_tokens(), None)
        qtbot.addWidget(form)
        assert _pressure_toggle(form) is None, kind


# -- toggle behavior -----------------------------------------------------

def test_clicking_toggle_updates_slot_and_emits(qtbot):
    form = PropertiesForm(_slot(ToolType.PEN), dark_tokens(), None)
    qtbot.addWidget(form)
    seen = []
    form.changed.connect(seen.append)
    toggle = _pressure_toggle(form)
    toggle._option.click()
    assert form._slot.pressure is True
    assert seen and seen[-1].pressure is True
    toggle._option.click()
    assert form._slot.pressure is False
    assert seen[-1].pressure is False


def test_highlighter_toggle_persists_flag_without_paint_claim(qtbot):
    form = PropertiesForm(_slot(ToolType.HIGHLIGHTER), dark_tokens(), None)
    qtbot.addWidget(form)
    form._pressure._option.click()
    assert form._slot.pressure is True


# -- store persistence -----------------------------------------------------

def test_slot_pressure_persists_per_slot(tmp_path):
    from PySide6.QtCore import QSettings

    from app.toolshelf.store import ToolShelfStore

    settings = QSettings(str(tmp_path / "pressure.ini"),
                         QSettings.Format.IniFormat)
    store = ToolShelfStore()
    first = store.add_slot(ToolType.PEN, name="P1")
    second = store.add_slot(ToolType.PEN, name="P2")
    assert first.pressure is False and second.pressure is False
    store.update_slot(first.id.value, pressure=True)
    assert store.find(first.id.value).pressure is True
    assert store.find(second.id.value).pressure is False
    store.persist(settings)
    settings.sync()
    restored = ToolShelfStore.restore(settings)
    assert restored.find(first.id.value).pressure is True
    assert restored.find(second.id.value).pressure is False


def test_fresh_defaults_have_pressure_disabled():
    from app.toolshelf.store import default_tool_config

    for kind in (ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER):
        assert default_tool_config(kind).pressure is False


# -- Settings modal no longer shows global controls -----------------------

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


def test_settings_modal_has_no_global_inking_controls(qtbot,
                                                      cleared_settings):
    from app.main_window import MainWindow

    window = MainWindow()
    window.resize(800, 600)
    window.show()
    qtbot.addWidget(window)
    modal = window.settings_modal
    assert modal.findChild(QLabel, "inkfall-inking-title") is None
    assert modal.findChild(QLabel, "inkfall-inking-note") is None
    assert modal.findChild(QLabel, "inkfall-smoothing-label") is None
    texts = [w.text() for w in modal.findChildren(QLabel)]
    assert not any("globally" in t.lower() for t in texts)
    buttons = [b.text() for b in modal.findChildren(QPushButton)]
    assert "Pressure-sensitive inking" not in buttons
    window.close()


# -- viewer latch integration through the live-edit path -------------------

def _bare_viewer(qtbot):
    from document.manager import DocumentManager

    from app.viewer import PdfViewer

    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    return viewer


def _close_viewer(viewer):
    try:
        viewer._tool_persist_timer.stop()
    except Exception:
        pass
    viewer.close()  # noqa: actual QWidget close (not this helper)


def test_viewer_wires_no_global_resolver(qtbot, cleared_settings):
    viewer = _bare_viewer(qtbot)
    assert viewer._interaction._pressure_resolver is None
    _close_viewer(viewer)


def test_live_toggle_keeps_active_stroke_next_adopts(qtbot,
                                                     cleared_settings):
    viewer = _bare_viewer(qtbot)
    try:
        slot = viewer._tool_store.add_slot(
            ToolType.PEN, name="LP", color="#112233", width_mm=0.7)
        viewer._tool_store.set_active(slot.id.value)
        viewer._interaction.set_tool(slot)
        page = PageId()
        live = viewer._interaction.handle_pen_press(
            page, 0.0, 0.0, 0.5, 100)["stroke_update"]
        assert live.pressure_enabled is False
        # Real production path: popup edit persists + re-sets same slot.
        viewer._on_tool_live_changed(replace(slot, pressure=True))
        assert viewer._tool_store.find(slot.id.value).pressure is True
        mid = viewer._interaction.handle_pen_move(
            page, 5.0, 0.0, 0.5, 116)["stroke_update"]
        assert mid.pressure_enabled is False
        viewer._interaction.handle_pen_release(page, 10.0, 0.0, 0.5, 132)
        nxt = viewer._interaction.handle_pen_press(
            page, 20.0, 0.0, 0.5, 200)["stroke_update"]
        assert nxt.pressure_enabled is True
    finally:
        _close_viewer(viewer)


def test_slot_pressure_reaches_rendering_paths(qtbot, cleared_settings):
    viewer = _bare_viewer(qtbot)
    try:
        slot = viewer._tool_store.add_slot(
            ToolType.PEN, name="RP", color="#ff0000", width_mm=3.0)
        viewer._interaction.set_tool(replace(slot, pressure=True))
        page = PageId()
        viewer._apply_intent(viewer._interaction.handle_pen_press(
            page, 0.0, 0.0, 0.1, 100))
        viewer._apply_intent(viewer._interaction.handle_pen_move(
            page, 10.0, 0.0, 1.0, 116))
        release = viewer._interaction.handle_pen_release(
            page, 20.0, 0.0, 1.0, 132)
        viewer._apply_intent(release)
        # M6.4: this bare viewer has no open document, so a completed
        # stroke has no store to commit to; the produced annotation
        # (from the release intent) still carries the latched pressure
        # state into the rendering path.
        done = release["stroke_done"]
        assert done.pressure_enabled is True
        outline = viewer._pressure_outline(done)
        assert outline is not None and outline.dots == ()
    finally:
        _close_viewer(viewer)
