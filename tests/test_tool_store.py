"""M5-A1 tool store tests: CRUD, order, validation, persistence round-trip."""

import pytest

from app.toolshelf.store import (
    DOCK_KEY,
    SLOTS_KEY,
    ToolShelfStore,
    default_slots,
)
from tools.models import ToolInstance, ToolType


def test_defaults_contain_pan_and_selection():
    store = ToolShelfStore()
    kinds = [s.kind for s in store.slots]
    assert ToolType.PAN in kinds
    assert ToolType.SELECTION in kinds
    assert kinds[0] == ToolType.PAN
    assert kinds[1] == ToolType.SELECTION
    assert store.active_id == store.slots[0].id.value
    assert store.dock == "left"
    # No compact mode (explicitly abolished): the store has no such state.
    assert not hasattr(store, "compact")
    assert not hasattr(store, "set_compact")


def test_add_unlimited_slots():
    store = ToolShelfStore()
    pens = [store.add_slot(ToolType.PEN, name=f"Pen #{i}", color="#ff0000",
                           width_mm=0.5 + i * 0.1) for i in range(5)]
    assert len(store.slots) == 7
    assert len({s.id.value for s in store.slots}) == 7
    assert [s.name for s in pens] == [f"Pen #{i}" for i in range(5)]


def test_remove_and_builtin_guard():
    store = ToolShelfStore()
    pen = store.add_slot(ToolType.PEN, name="Pen #1")
    store.remove_slot(pen.id.value)
    assert store.find(pen.id.value) is None
    pan_id = next(s.id.value for s in store.slots if s.kind == ToolType.PAN)
    with pytest.raises(ValueError):
        store.remove_slot(pan_id)
    with pytest.raises(KeyError):
        store.remove_slot("missing-id")


def test_duplicate_and_reorder():
    store = ToolShelfStore()
    pen = store.add_slot(ToolType.PEN, name="Pen A", color="#0000ff", width_mm=0.7)
    copy = store.duplicate_slot(pen.id.value)
    assert copy.id.value != pen.id.value
    assert copy.color == pen.color and copy.width_mm == pen.width_mm
    order = [s.id.value for s in store.slots]
    assert order.index(copy.id.value) == order.index(pen.id.value) + 1
    store.move_slot(copy.id.value, 0)
    # Core tools stay pinned at the front: customs clamp after them.
    assert [s.kind for s in store.slots[:2]] == [ToolType.PAN, ToolType.SELECTION]
    assert store.slots[2].id.value == copy.id.value
    store.move_slot(copy.id.value, 999)
    assert store.slots[-1].id.value == copy.id.value
    # Core tools cannot be duplicated or reordered.
    pan_id = next(s.id.value for s in store.slots if s.kind == ToolType.PAN)
    select_id = next(s.id.value for s in store.slots
                     if s.kind == ToolType.SELECTION)
    with pytest.raises(ValueError):
        store.duplicate_slot(pan_id)
    with pytest.raises(ValueError):
        store.duplicate_slot(select_id)
    with pytest.raises(ValueError):
        store.move_slot(pan_id, 3)
    with pytest.raises(ValueError):
        store.move_slot(select_id, 3)
    # Core tools cannot be added through the Add path; Lasso can.
    with pytest.raises(ValueError):
        store.add_slot(ToolType.PAN, name="Pan 2")
    with pytest.raises(ValueError):
        store.add_slot(ToolType.SELECTION, name="Select 2")
    lasso = store.add_slot(ToolType.LASSO, name="Lasso 1")
    assert lasso.kind == ToolType.LASSO
    store.remove_slot(lasso.id.value)
    assert [s.kind for s in store.slots[:2]] == [ToolType.PAN, ToolType.SELECTION]


def test_update_slot_and_kind_immutable():
    store = ToolShelfStore()
    pen = store.add_slot(ToolType.PEN, name="Pen A")
    updated = store.update_slot(pen.id.value, color="#00ff00", width_mm=1.2,
                                opacity=0.8, name="Pen B")
    assert updated.id.value == pen.id.value
    assert (updated.color, updated.width_mm, updated.opacity, updated.name) == \
        ("#00ff00", 1.2, 0.8, "Pen B")
    with pytest.raises(ValueError):
        store.update_slot(pen.id.value, kind=ToolType.HIGHLIGHTER)
    with pytest.raises(ValueError):
        store.update_slot(pen.id.value, width_mm=-1.0)
    with pytest.raises(ValueError):
        store.update_slot(pen.id.value, bogus_field=1)


def test_dock_validation_and_active():
    store = ToolShelfStore()
    for dock in ("left", "right", "top", "bottom"):
        assert store.set_dock(dock) == dock
    with pytest.raises(ValueError):
        store.set_dock("diagonal")
    pen = store.add_slot(ToolType.PEN, name="Pen #1")
    assert store.set_active(pen.id.value).id.value == pen.id.value
    assert store.active_id == pen.id.value
    with pytest.raises(KeyError):
        store.set_active("missing-id")


def _ini_settings(tmp_path):
    from PySide6.QtCore import QSettings

    return QSettings(str(tmp_path / "m5a1.ini"), QSettings.Format.IniFormat)


def test_persist_restore_round_trip(tmp_path):
    store = ToolShelfStore()
    pen = store.add_slot(ToolType.PEN, name="Pen Red", color="#ff0000",
                         width_mm=0.9, opacity=0.9, pressure=True,
                         smoothing=0.3)
    store.add_slot(ToolType.HIGHLIGHTER, name="HL", color="#ffff00",
                   width_mm=3.0, opacity=0.4)
    rect = store.add_slot(ToolType.RECTANGLE, name="R", color="#e5484d",
                          fill_enabled=True, fill_color="#ffd60a",
                          fill_opacity=0.5, line_style="dashed", square=True)
    line = store.add_slot(ToolType.LINE, name="L", color="#2b6cff",
                          line_style="dotted", start_ending="arrow",
                          end_ending="dot")
    store.set_dock("right")
    store.set_active(pen.id.value)
    settings = _ini_settings(tmp_path)
    store.persist(settings)
    settings.sync()
    assert settings.value(DOCK_KEY) == "right"
    assert settings.value(SLOTS_KEY)

    revived = ToolShelfStore.restore(settings)
    assert revived.dock == "right"
    assert revived.active_id == pen.id.value
    assert [s.id.value for s in revived.slots] == [s.id.value for s in store.slots]
    match = revived.find(pen.id.value)
    assert match is not None and match.color == "#ff0000"
    assert match.pressure is True
    assert not hasattr(match, "blend_mode")
    rmatch = revived.find(rect.id.value)
    assert (rmatch.fill_enabled is True and rmatch.fill_color == "#ffd60a"
            and abs(rmatch.fill_opacity - 0.5) < 1e-9
            and rmatch.line_style == "dashed" and rmatch.square is True)
    lmatch = revived.find(line.id.value)
    assert (lmatch.line_style == "dotted"
            and lmatch.start_ending == "arrow"
            and lmatch.end_ending == "dot")


def test_old_slots_deserialize_with_new_defaults(tmp_path):
    """Pre-extension persisted slots load unchanged (additive back-compat)."""
    import json

    from app.toolshelf.store import SLOTS_KEY

    old_pen = {"id": "abc", "kind": "pen", "name": "Old Pen",
               "color": "#123456", "width_mm": 0.7, "opacity": 0.8,
               "pressure": True, "smoothing": 0.2,
               "blend_mode": "multiply", "text_assist": True}
    old_rect = {"id": "def", "kind": "rectangle", "name": "Old Rect",
                "color": "#654321", "width_mm": 0.5, "opacity": 1.0}
    settings = _ini_settings(tmp_path)
    settings.setValue(SLOTS_KEY, json.dumps([old_pen, old_rect]))
    settings.sync()
    revived = ToolShelfStore.restore(settings)
    pen = revived.find("abc")
    assert pen is not None and pen.color == "#123456"
    assert pen.fill_enabled is False and pen.line_style == "solid"
    assert pen.square is False and pen.circle is False
    rect = revived.find("def")
    assert rect is not None and rect.fill_enabled is False
    assert rect.fill_opacity == 1.0 and rect.start_ending == "none"


def test_core_lasso_migrates_to_selection_lossless(tmp_path):
    """Stored core lasso becomes Selection; customs survive; idempotent."""
    import json

    from app.toolshelf.store import SLOTS_KEY, _slot_to_dict

    core_lasso = ToolInstance(kind=ToolType.LASSO, name="Select")
    custom_lasso = ToolInstance(kind=ToolType.LASSO, name="My Lasso")
    custom_pen = ToolInstance(kind=ToolType.PEN, name="Pen")
    blob = [_slot_to_dict(s) for s in (core_lasso, custom_lasso, custom_pen)]
    settings = _ini_settings(tmp_path)
    settings.setValue(SLOTS_KEY, json.dumps(blob))
    settings.sync()
    revived = ToolShelfStore.restore(settings)
    kinds = [s.kind for s in revived.slots]
    assert kinds[0] == ToolType.PAN and kinds[1] == ToolType.SELECTION
    assert revived.slots[1].id.value == core_lasso.id.value
    assert revived.slots[1].name == "Select"
    kept = [s for s in revived.slots if s.kind == ToolType.LASSO]
    assert len(kept) == 1 and kept[0].id.value == custom_lasso.id.value
    assert revived.find(custom_pen.id.value) is not None
    # Idempotent: a second restore migrates nothing further.
    settings2 = _ini_settings(tmp_path)
    revived.persist(settings2)
    settings2.sync()
    again = ToolShelfStore.restore(settings2)
    assert [s.id.value for s in again.slots] == \
        [s.id.value for s in revived.slots]
    assert [s.kind for s in again.slots[:2]] == \
        [ToolType.PAN, ToolType.SELECTION]


def test_preset_collections_customizable(tmp_path):
    from app.toolshelf.store import (
        get_color_presets,
        get_opacity_presets,
        get_size_presets,
        set_color_presets,
        set_opacity_presets,
        set_size_presets,
    )

    settings = _ini_settings(tmp_path)
    assert len(get_color_presets(settings)) == 10  # starting presets
    assert get_size_presets(ToolType.PEN, settings) == [0.3, 0.5, 0.8, 1.2, 2.0]
    assert get_opacity_presets(settings) == [0.2, 0.4, 0.7, 1.0]
    assert set_color_presets(["#111111", "#ffffff"], settings) == \
        ["#111111", "#ffffff"]
    assert get_color_presets(settings) == ["#111111", "#ffffff"]
    assert set_size_presets(ToolType.PEN, [0.5, 1.0], settings) == [0.5, 1.0]
    assert get_size_presets(ToolType.PEN, settings) == [0.5, 1.0]
    assert set_opacity_presets([0.5, 1.0], settings) == [0.5, 1.0]
    # Other kinds unaffected by a per-kind size change.
    assert get_size_presets(ToolType.LINE, settings) == [0.3, 0.5, 0.8, 1.2]


def test_restore_corrupt_falls_back(tmp_path):
    settings = _ini_settings(tmp_path)
    settings.setValue(SLOTS_KEY, "not-json{{{")
    settings.setValue(DOCK_KEY, "diagonal")
    revived = ToolShelfStore.restore(settings)
    kinds = [s.kind for s in revived.slots]
    assert ToolType.PAN in kinds and ToolType.SELECTION in kinds
    assert revived.dock == "left"


def test_restore_reseeds_missing_builtins(tmp_path):
    settings = _ini_settings(tmp_path)
    lonely = ToolInstance(kind=ToolType.PEN, name="Solo")
    store = ToolShelfStore(slots=[lonely])
    store.persist(settings)
    settings.sync()
    revived = ToolShelfStore.restore(settings)
    kinds = [s.kind for s in revived.slots]
    assert ToolType.PAN in kinds and ToolType.SELECTION in kinds
    assert revived.find(lonely.id.value) is not None
