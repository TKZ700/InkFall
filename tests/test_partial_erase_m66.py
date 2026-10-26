"""M6.6 Eraser modes: Whole Stroke vs Partial Stroke.

Covers the pure partial-erasure geometry helper, the Eraser mode property
(model + popup + persistence), the store replacement primitive, the
replacement undo command, and the real gesture path in the viewer (mode
dispatch, one-gesture-one-undo-step grouping, HUD, identity, rendering).

Whole Stroke (M6.5) behavior stays the compatible default and is re-checked
here; the M6.1-M6.5 suites remain the deeper regression gates.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from core.types import AnnotationId, PageId, Point
from annotations.models import Highlighter, InkStroke
from annotations.partial_erase import erase_stroke
from annotations.store import AnnotationStore
from app.undo import (
    DocumentHistory,
    ReplaceAnnotationCommand,
)
from tools.models import (
    ERASER_MODE_PARTIAL,
    ERASER_MODE_WHOLE,
    ToolInstance,
    ToolType,
    editable_properties,
    normalize_eraser_mode,
)
from tests.test_eraser_m65 import (
    _add_highlighter,
    _add_stroke,
    _erase_line,
    _open_window,
    _page_id,
    _page_pt_to_widget,
    _pixel_is_red,
    _store,
)

pytestmark = pytest.mark.usefixtures("qapp")


# --------------------------------------------------------------- fixtures

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


def _eraser_tool(width_mm=2.0, mode=ERASER_MODE_WHOLE):
    return ToolInstance(kind=ToolType.ERASER, name="Eraser",
                        width_mm=width_mm, eraser_mode=mode)


def _flat(y, x0=0.0, x1=100.0, step=5.0):
    pts = []
    x = x0
    while x < x1:
        pts.append((x, y))
        x += step
    pts.append((x1, y))
    return pts


def _mixed_stroke(i, page):
    return InkStroke(
        page_id=page,
        points=(Point(0.0, float(i)), Point(1.0, float(i))),
        color="#ff0000", width_mm=1.0)


def _fragment_of(original, points, points_id=None):
    return InkStroke(
        id=points_id if points_id is not None else AnnotationId(),
        page_id=original.page_id,
        points=tuple(Point(float(x), float(y)) for x, y in points),
        color=original.color, width_mm=original.width_mm,
        opacity=original.opacity)


# =============================== Part A: geometry helper (Qt-free)

def test_helper_module_is_stdlib_only():
    path = (Path(__file__).resolve().parent.parent
            / "annotations" / "partial_erase.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = {"__future__", "math", "dataclasses"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in allowed


def test_helper_middle_cut_leaves_two_fragments_with_gap():
    pts = _flat(0.0)
    hw = [1.0] * len(pts)
    changed, frags = erase_stroke(
        pts, (), hw, [(50.0, -10.0, 50.0, 10.0)], 2.0)
    assert changed is True
    assert len(frags) == 2
    left, right = frags
    assert left.points[0] == (0.0, 0.0)
    assert right.points[-1] == (100.0, 0.0)
    assert left.points[-1][0] == pytest.approx(47.0, abs=1e-6)
    assert right.points[0][0] == pytest.approx(53.0, abs=1e-6)


def test_helper_cut_at_start_leaves_one_shortened_fragment():
    pts = _flat(0.0)
    hw = [1.0] * len(pts)
    # Eraser at x=3 reaches x=0 (offset 3 = half-width 1 + reach 2).
    changed, frags = erase_stroke(
        pts, (), hw, [(3.0, -10.0, 3.0, 10.0)], 2.0)
    assert changed is True
    assert len(frags) == 1
    assert frags[0].points[0][0] == pytest.approx(6.0, abs=1e-6)
    assert frags[0].points[-1][0] == pytest.approx(100.0, abs=1e-6)


def test_helper_cut_at_end_leaves_one_shortened_fragment():
    pts = _flat(0.0)
    hw = [1.0] * len(pts)
    changed, frags = erase_stroke(
        pts, (), hw, [(97.0, -10.0, 97.0, 10.0)], 2.0)
    assert changed is True
    assert len(frags) == 1
    assert frags[0].points[0][0] == pytest.approx(0.0, abs=1e-6)
    assert frags[0].points[-1][0] == pytest.approx(94.0, abs=1e-6)


def test_helper_full_erase_yields_no_fragment():
    pts = _flat(0.0)
    hw = [1.0] * len(pts)
    changed, frags = erase_stroke(
        pts, (), hw, [(-10.0, 0.0, 110.0, 0.0)], 20.0)
    assert changed is True
    assert frags == ()


def test_helper_near_miss_is_unchanged():
    pts = _flat(0.0)
    hw = [1.0] * len(pts)
    changed, frags = erase_stroke(
        pts, (), hw, [(50.0, 500.0, 50.0, 510.0)], 2.0)
    assert changed is False
    assert frags == ()


def test_helper_sparse_segment_crossing_is_clipped():
    # Only two stored samples; the eraser crosses the long segment between
    # them, which a sample-based test would miss.
    changed, frags = erase_stroke(
        [(0.0, 0.0), (100.0, 0.0)], (), [1.0, 1.0],
        [(50.0, -10.0, 50.0, 10.0)], 2.0)
    assert changed is True
    assert len(frags) == 2
    assert frags[0].points[-1][0] == pytest.approx(47.0, abs=1e-6)
    assert frags[1].points[0][0] == pytest.approx(53.0, abs=1e-6)


def test_helper_pressure_at_boundaries_is_interpolated():
    pts = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (30.0, 0.0)]
    prs = (0.0, 0.5, 1.0, 0.25)
    changed, frags = erase_stroke(
        pts, prs, [1.0] * 4, [(15.0, -5.0, 15.0, 5.0)], 2.0)
    assert changed is True
    assert len(frags) == 2
    left, right = frags
    # Original interior sample and its exact pressure are preserved.
    assert left.points.index((10.0, 0.0)) == 1
    assert left.pressures[1] == pytest.approx(0.5)
    # Cut boundary pressure is interpolated between neighbours.
    assert left.pressures[-1] == pytest.approx(0.6, abs=1e-9)
    assert right.pressures[0] == pytest.approx(0.9, abs=1e-9)
    assert len(left.pressures) == len(left.points)
    assert len(right.pressures) == len(right.points)


def test_helper_disjoint_erasers_yield_three_fragments():
    pts = _flat(0.0)
    changed, frags = erase_stroke(
        pts, (), [1.0] * len(pts),
        [(45.0, -5.0, 45.0, 5.0), (55.0, -5.0, 55.0, 5.0)], 2.0)
    assert changed is True
    assert len(frags) == 3


def test_helper_overlapping_erasers_merge_into_one_gap():
    pts = _flat(0.0)
    changed, frags = erase_stroke(
        pts, (), [1.0] * len(pts),
        [(45.0, -5.0, 45.0, 5.0), (48.0, -5.0, 48.0, 5.0)], 2.0)
    assert changed is True
    assert len(frags) == 2
    assert frags[0].points[-1][0] == pytest.approx(42.0, abs=1e-6)
    assert frags[1].points[0][0] == pytest.approx(51.0, abs=1e-6)


def test_helper_reversing_path_produces_multiple_intersections():
    # Zig-zag crossing one vertical eraser twice.
    pts = [(0.0, 0.0), (20.0, 10.0), (0.0, 20.0)]
    changed, frags = erase_stroke(
        pts, (), [1.0] * 3, [(10.0, -100.0, 10.0, 100.0)], 2.0)
    assert changed is True
    assert len(frags) == 3


def test_helper_preserves_short_valid_remnant():
    pts = _flat(0.0)
    # The collinear eraser removes everything up to x=99; the tiny 1pt
    # remnant at the end is still valid visible geometry and is kept.
    changed, frags = erase_stroke(
        pts, (), [1.0] * len(pts), [(-50.0, 0.0, 96.0, 0.0)], 2.0)
    assert changed is True
    assert len(frags) == 1
    assert frags[0].points[0][0] == pytest.approx(99.0, abs=1e-6)
    assert frags[0].points[-1][0] == pytest.approx(100.0, abs=1e-6)


def test_helper_never_emits_zero_length_fragments():
    pts = _flat(0.0)
    changed, frags = erase_stroke(
        pts, (), [1.0] * len(pts), [(50.0, -5.0, 50.0, 5.0)], 2.0)
    assert changed is True
    for frag in frags:
        length = 0.0
        for j in range(len(frag.points) - 1):
            (ax, ay), (bx, by) = frag.points[j], frag.points[j + 1]
            length += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        assert length > 0.0


def test_helper_point_stroke_inside_and_outside():
    changed, frags = erase_stroke(
        [(50.0, 0.0)], (), [1.0], [(50.0, -5.0, 50.0, 5.0)], 2.0)
    assert changed is True and frags == ()
    changed, frags = erase_stroke(
        [(50.0, 0.0)], (), [1.0], [(50.0, 500.0, 50.0, 510.0)], 2.0)
    assert changed is False and frags == ()


def test_helper_bad_input_is_unchanged():
    for bad_hw in ([1.0], [], [1.0, 1.0, 1.0]):
        changed, frags = erase_stroke(
            [(0.0, 0.0), (10.0, 0.0)], (), bad_hw,
            [(5.0, -5.0, 5.0, 5.0)], 2.0)
        if len(bad_hw) != 2:
            assert changed is False and frags == ()


# =============================== Part B: model + configuration

def test_eraser_mode_default_is_whole():
    assert ToolInstance(kind=ToolType.ERASER).eraser_mode == ERASER_MODE_WHOLE


def test_normalize_eraser_mode_handles_legacy_values():
    assert normalize_eraser_mode("partial") == ERASER_MODE_PARTIAL
    assert normalize_eraser_mode("whole") == ERASER_MODE_WHOLE
    assert normalize_eraser_mode(None) == ERASER_MODE_WHOLE
    assert normalize_eraser_mode("nonsense") == ERASER_MODE_WHOLE
    assert normalize_eraser_mode(42) == ERASER_MODE_WHOLE


def test_tool_instance_normalizes_unknown_mode():
    tool = ToolInstance(kind=ToolType.ERASER, eraser_mode="bogus")
    assert tool.eraser_mode == ERASER_MODE_WHOLE


def test_eraser_editable_properties_offer_mode_and_width():
    props = editable_properties(ToolType.ERASER)
    assert "eraser_mode" in props
    assert "width_mm" in props


def test_slot_roundtrip_preserves_mode():
    from app.toolshelf.store import _slot_from_dict, _slot_to_dict

    slot = _eraser_tool(mode=ERASER_MODE_PARTIAL)
    data = _slot_to_dict(slot)
    assert data["eraser_mode"] == ERASER_MODE_PARTIAL
    restored = _slot_from_dict(data)
    assert restored.eraser_mode == ERASER_MODE_PARTIAL


def test_legacy_slot_dict_without_mode_loads_whole():
    from app.toolshelf.store import _slot_from_dict, _slot_to_dict

    data = _slot_to_dict(_eraser_tool(mode=ERASER_MODE_PARTIAL))
    data.pop("eraser_mode")
    restored = _slot_from_dict(data)
    assert restored.eraser_mode == ERASER_MODE_WHOLE


def test_corrupt_slot_mode_normalizes_on_load():
    from app.toolshelf.store import _slot_from_dict, _slot_to_dict

    data = _slot_to_dict(_eraser_tool(mode=ERASER_MODE_PARTIAL))
    data["eraser_mode"] = "garbage"
    restored = _slot_from_dict(data)
    assert restored.eraser_mode == ERASER_MODE_WHOLE


def test_store_update_slot_updates_mode_immediately():
    from app.toolshelf.store import ToolShelfStore

    store = ToolShelfStore()
    slot = store.add_slot(kind=ToolType.ERASER, name="E", width_mm=2.0,
                          eraser_mode=ERASER_MODE_PARTIAL)
    assert store.find(slot.id.value).eraser_mode == ERASER_MODE_PARTIAL
    updated = store.update_slot(slot.id.value,
                                eraser_mode=ERASER_MODE_WHOLE)
    assert updated.eraser_mode == ERASER_MODE_WHOLE
    assert store.find(slot.id.value).eraser_mode == ERASER_MODE_WHOLE


def test_switching_eraser_presets_restores_each_mode():
    from app.toolshelf.store import ToolShelfStore

    store = ToolShelfStore()
    a = store.add_slot(kind=ToolType.ERASER, name="E1", width_mm=2.0,
                       eraser_mode=ERASER_MODE_WHOLE)
    b = store.add_slot(kind=ToolType.ERASER, name="E2", width_mm=2.0,
                       eraser_mode=ERASER_MODE_PARTIAL)
    store.set_active(b.id.value)
    assert store.active().eraser_mode == ERASER_MODE_PARTIAL
    store.set_active(a.id.value)
    assert store.active().eraser_mode == ERASER_MODE_WHOLE
    # Width and other properties are untouched by the mode switch.
    assert store.find(a.id.value).width_mm == 2.0
    assert store.find(b.id.value).width_mm == 2.0


# =============================== Part C: popup control

def _popup(slot):
    from app.theme.tokens import dark_tokens
    from app.toolshelf.tool_popup import PropertiesForm

    return PropertiesForm(slot, dark_tokens(), None)


def test_popup_shows_both_mode_labels(qtbot):
    form = _popup(_eraser_tool(mode=ERASER_MODE_WHOLE))
    qtbot.addWidget(form)
    try:
        section = form._mode_section
        assert section is not None
        names = {opt.accessibleName() for opt in section._options.values()}
        assert names == {"Whole Stroke", "Partial Stroke"}
        assert section.value() == ERASER_MODE_WHOLE
    finally:
        form.deleteLater()


def test_popup_selecting_partial_updates_configuration(qtbot):
    form = _popup(_eraser_tool(mode=ERASER_MODE_WHOLE))
    qtbot.addWidget(form)
    seen = []
    form.changed.connect(lambda slot: seen.append(slot))
    try:
        section = form._mode_section
        section._options[ERASER_MODE_PARTIAL].click()
        assert form._slot.eraser_mode == ERASER_MODE_PARTIAL
        assert section.value() == ERASER_MODE_PARTIAL
        assert seen and seen[-1].eraser_mode == ERASER_MODE_PARTIAL
    finally:
        form.deleteLater()


def test_popup_initial_selection_matches_slot(qtbot):
    form = _popup(_eraser_tool(mode=ERASER_MODE_PARTIAL))
    qtbot.addWidget(form)
    try:
        assert form._mode_section.value() == ERASER_MODE_PARTIAL
        assert form._mode_section._options[
            ERASER_MODE_PARTIAL].isChecked() is True
        assert form._mode_section._options[
            ERASER_MODE_WHOLE].isChecked() is False
    finally:
        form.deleteLater()


# =============================== Part D: store replacement

def test_store_replace_preserves_position_and_order():
    store = AnnotationStore(1)
    page = PageId()
    a, b, c = (_mixed_stroke(i, page) for i in range(3))
    for item in (a, b, c):
        store.add(item, 0)
    f1 = _fragment_of(a, [(0.0, 0.0), (0.5, 0.0)], points_id=a.id)
    f2 = _fragment_of(a, [(0.6, 0.0), (1.0, 0.0)])
    store.replace(a.id, [f1, f2])
    assert store.page(0) == (f1, f2, b, c)
    assert store.count(0) == 4
    assert store.get(f1.id) is f1
    assert store.get(a.id) is f1


def test_store_replace_rejects_bad_input_atomically():
    store = AnnotationStore(1)
    page = PageId()
    a, b = (_mixed_stroke(i, page) for i in range(2))
    store.add(a, 0)
    store.add(b, 0)
    # Duplicate identities *among the insertions* are rejected.
    shared = AnnotationId()
    dup1 = _fragment_of(a, [(0.0, 0.0), (0.1, 0.0)], points_id=shared)
    dup2 = _fragment_of(a, [(0.2, 0.0), (0.3, 0.0)], points_id=shared)
    with pytest.raises(ValueError):
        store.replace(a.id, [dup1, dup2])
    assert store.page(0) == (a, b)
    # An insertion colliding with a surviving annotation is rejected.
    collide = _fragment_of(a, [(0.0, 0.0), (0.1, 0.0)], points_id=b.id)
    with pytest.raises(ValueError):
        store.replace(a.id, [collide])
    assert store.page(0) == (a, b)
    # An unknown removal identity is rejected.
    with pytest.raises(ValueError):
        store.replace(AnnotationId(), [dup1])
    assert store.page(0) == (a, b)


def test_store_substitute_reverse_restores_original():
    store = AnnotationStore(1)
    page = PageId()
    a, b = (_mixed_stroke(i, page) for i in range(2))
    store.add(a, 0)
    store.add(b, 0)
    f1 = _fragment_of(a, [(0.0, 0.0), (0.5, 0.0)], points_id=a.id)
    f2 = _fragment_of(a, [(0.6, 0.0), (1.0, 0.0)])
    store.substitute([a.id], [f1, f2], 0, 0)
    assert store.page(0) == (f1, f2, b)
    store.substitute([f1.id, f2.id], [a], 0, 0)
    assert store.page(0) == (a, b)


# =============================== Part E: replacement command

def test_replace_command_undo_redo_keeps_identity_and_order():
    store = AnnotationStore(1)
    page = PageId()
    a, b = (_mixed_stroke(i, page) for i in range(2))
    store.add(a, 0)
    store.add(b, 0)
    history = DocumentHistory(store)
    f1 = _fragment_of(a, [(0.0, 0.0), (0.5, 0.0)], points_id=a.id)
    f2 = _fragment_of(a, [(0.6, 0.0), (1.0, 0.0)])
    history.stack.push(ReplaceAnnotationCommand(store, a.id, [f1, f2]))
    assert store.page(0) == (f1, f2, b)
    history.undo()
    assert store.page(0) == (a, b)
    history.redo()
    assert store.page(0) == (f1, f2, b)


def test_replace_command_rejects_unknown_identity():
    store = AnnotationStore(1)
    with pytest.raises(ValueError):
        ReplaceAnnotationCommand(store, AnnotationId(),
                                 [_mixed_stroke(0, PageId())])


def test_replace_then_delete_share_one_grouped_step():
    store = AnnotationStore(1)
    page = PageId()
    a, b, c = (_mixed_stroke(i, page) for i in range(3))
    for item in (a, b, c):
        store.add(item, 0)
    history = DocumentHistory(store)
    f1 = _fragment_of(a, [(0.0, 0.0), (0.5, 0.0)], points_id=a.id)
    history.push_replace_grouped(a.id, [f1])
    history.push_delete_grouped(b.id)
    assert history.stack.count() == 1
    assert history.group_open is True
    history.end_group()
    assert history.group_open is False
    assert store.page(0) == (f1, c)
    history.undo()
    assert store.page(0) == (a, b, c)
    history.redo()
    assert store.page(0) == (f1, c)


def test_replace_redo_is_idempotent_no_duplicate_ids():
    store = AnnotationStore(1)
    page = PageId()
    a = _mixed_stroke(0, page)
    store.add(a, 0)
    history = DocumentHistory(store)
    f1 = _fragment_of(a, [(0.0, 0.0), (0.5, 0.0)], points_id=a.id)
    f2 = _fragment_of(a, [(0.6, 0.0), (1.0, 0.0)])
    history.push_replace_grouped(a.id, [f1, f2])
    history.end_group()
    first = [x.id.value for x in store.page(0)]
    for _ in range(3):
        history.undo()
        history.redo()
    assert [x.id.value for x in store.page(0)] == first
    assert len(store.page(0)) == 2


# =============================== Part F: viewer gesture path

def test_partial_erase_middle_splits_into_two_fragments(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        page = _store(viewer).page(0)
        assert len(page) == 2
        left, right = page
        assert left.id.value == stroke.id.value      # deterministic keep
        assert right.id.value != stroke.id.value     # fresh identity
        assert left.points[0] == Point(100.0, 300.0)
        assert right.points[-1] == Point(300.0, 300.0)
        assert left.points[-1].x < 200.0 < right.points[0].x
        assert left.width_mm == stroke.width_mm
        assert left.color == stroke.color
        assert left.page_id == stroke.page_id
    finally:
        viewer.close_document()


def test_partial_erase_start_shortens_single_fragment(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 104.0, 200.0, 104.0, 400.0)
        page = _store(viewer).page(0)
        assert len(page) == 1
        frag = page[0]
        assert frag.id.value == stroke.id.value
        assert frag.points[-1] == Point(300.0, 300.0)
        assert frag.points[0].x > 100.0
    finally:
        viewer.close_document()


def test_partial_erase_multiple_annotations_one_undo_step(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        a = _add_stroke(viewer, [(100.0, 200.0), (300.0, 200.0)])
        b = _add_stroke(viewer, [(100.0, 400.0), (300.0, 400.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 150.0, 200.0, 450.0)
        page = _store(viewer).page(0)
        assert len(page) == 4
        history = viewer.document_history
        assert history.stack.count() == 1
        history.undo()
        assert _store(viewer).page(0) == (a, b)
        history.redo()
        assert len(_store(viewer).page(0)) == 4
    finally:
        viewer.close_document()


def test_partial_erase_one_undo_step_hud_roundtrip(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        history = viewer.document_history
        assert history.stack.count() == 1
        assert history.group_open is False
        before = [x.id.value for x in _store(viewer).page(0)]
        hud = viewer.undo_hud
        assert hud.can_undo() is True
        assert hud._undo_btn.isEnabled() is True
        hud._undo_btn.click()
        assert _store(viewer).page(0) == (stroke,)
        assert hud._redo_btn.isEnabled() is True
        hud._redo_btn.click()
        assert [x.id.value for x in _store(viewer).page(0)] == before
    finally:
        viewer.close_document()


def test_partial_erase_highlighter_preserves_style(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        hl = _add_highlighter(viewer, [(100.0, 300.0), (300.0, 300.0)],
                              width_mm=6.0, color="#ffd60a", opacity=0.35)
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        page = _store(viewer).page(0)
        assert len(page) == 2
        for frag in page:
            assert isinstance(frag, Highlighter)
            assert frag.color == "#ffd60a"
            assert frag.opacity == pytest.approx(0.35)
            assert frag.width_mm == pytest.approx(6.0)
            assert frag.page_id == hl.page_id
        assert page[0].points[-1].x < 200.0 < page[1].points[0].x
    finally:
        viewer.close_document()


def test_partial_erase_pressure_stroke_keeps_pressure_data(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        pts = [(100.0 + 5.0 * i, 300.0) for i in range(41)]
        pressures = tuple(0.2 + 0.6 * (i / 40.0) for i in range(41))
        _add_stroke(viewer, pts, pressure_enabled=True,
                    pressures=pressures, pressure_profile="normal")
        _erase_line(viewer, _page_id(viewer), 200.0, 250.0, 200.0, 350.0)
        page = _store(viewer).page(0)
        assert len(page) == 2
        for frag in page:
            assert frag.pressure_enabled is True
            assert len(frag.pressures) == len(frag.points)
            assert all(0.0 <= p <= 1.0 for p in frag.pressures)
    finally:
        viewer.close_document()


def test_whole_mode_still_removes_entire_stroke(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_WHOLE))
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert _store(viewer).page(0) == ()
        assert viewer.document_history.stack.count() == 1
    finally:
        viewer.close_document()


def test_default_eraser_tool_behaves_as_whole(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_WHOLE))
    try:
        assert viewer._eraser_mode() == ERASER_MODE_WHOLE
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        assert _store(viewer).page(0) == ()
    finally:
        viewer.close_document()


def test_partial_gesture_with_no_hit_creates_no_history(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        stroke = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 500.0, 500.0, 520.0, 520.0)
        assert _store(viewer).page(0) == (stroke,)
        history = viewer.document_history
        assert history.stack.count() == 0
        assert history.group_open is False
        assert viewer.undo_hud._undo_btn.isEnabled() is False
    finally:
        viewer.close_document()


def test_partial_erase_leaves_neighbour_annotation_intact(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        a = _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        b = _add_stroke(viewer, [(500.0, 300.0), (700.0, 300.0)])
        _erase_line(viewer, _page_id(viewer), 200.0, 200.0, 200.0, 400.0)
        page = _store(viewer).page(0)
        assert len(page) == 3
        assert b in page
        assert a not in page
    finally:
        viewer.close_document()


def test_partial_mode_is_latched_for_the_gesture(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        it = viewer._interaction
        pid = _page_id(viewer)
        viewer._apply_intent(it.handle_eraser_press(pid, 200.0, 200.0))
        assert viewer._eraser_mode_latch == ERASER_MODE_PARTIAL
        viewer._apply_intent(it.handle_eraser_move(pid, 200.0, 400.0))
        viewer._apply_intent(it.handle_eraser_release(pid, 200.0, 400.0))
        assert viewer._eraser_mode_latch is None
    finally:
        viewer.close_document()


def test_partial_repeat_overlap_adds_no_extra_entries(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
        it = viewer._interaction
        pid = _page_id(viewer)
        viewer._apply_intent(it.handle_eraser_press(pid, 200.0, 250.0))
        for y in (350.0, 300.0, 350.0):
            viewer._apply_intent(it.handle_eraser_move(pid, 200.0, y))
        viewer._apply_intent(it.handle_eraser_release(pid, 200.0, 350.0))
        page = _store(viewer).page(0)
        assert len(page) == 2
        assert viewer.document_history.stack.count() == 1
    finally:
        viewer.close_document()


def test_partial_document_close_leaves_no_open_group(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    _add_stroke(viewer, [(100.0, 300.0), (300.0, 300.0)])
    it = viewer._interaction
    pid = _page_id(viewer)
    viewer._apply_intent(it.handle_eraser_press(pid, 200.0, 250.0))
    viewer._apply_intent(it.handle_eraser_move(pid, 200.0, 350.0))
    assert viewer._eraser_history is not None
    viewer.close_document()
    assert viewer._eraser_history is None
    assert viewer._eraser_mode_latch is None


def test_partial_erase_pixels_gap_and_remainder(
        qtbot, tmp_path, cleared_settings):
    window, viewer = _open_window(qtbot, tmp_path,
                                  _eraser_tool(mode=ERASER_MODE_PARTIAL))
    try:
        _add_stroke(viewer, [(150.0, 300.0), (450.0, 300.0)],
                    width_mm=2.0)
        viewer.repaint()
        qtbot.wait(30)

        def red_at(lx):
            wx, wy = _page_pt_to_widget(viewer, lx, 300.0)
            return _pixel_is_red(viewer, wx, wy)

        assert any(red_at(x) for x in (198.0, 200.0, 202.0))
        _erase_line(viewer, _page_id(viewer), 300.0, 250.0, 300.0, 350.0)
        viewer.repaint()
        qtbot.wait(30)
        # Ends remain; the middle gap is gone.
        assert any(red_at(x) for x in (198.0, 200.0, 202.0))
        assert any(red_at(x) for x in (398.0, 400.0, 402.0))
        assert not any(red_at(x) for x in (298.0, 300.0, 302.0))
        # Undo restores the erased middle.
        viewer.undo_hud._undo_btn.click()
        viewer.repaint()
        qtbot.wait(30)
        assert any(red_at(x) for x in (298.0, 300.0, 302.0))
    finally:
        viewer.close_document()
