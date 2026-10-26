"""M6.8.2 clipboard slice: Cut / Copy / Paste for annotation selections.

Focused coverage for the deferred clipboard milestone (not M6.8.3):

* the Qt-free payload format round-trips every supported model type
  (ink, highlighter, fill/rotation shape) with fresh identities and
  destination-derived page ids, and carries no runtime identity;
* one page-pt translation moves the whole group and preserves its
  relative layout, within-page order, and relative page offsets;
* malformed / incomplete / unsupported / version-incompatible payloads
  and groups that do not fit the destination are rejected before any
  object is constructed (no partial plan);
* Copy captures a multi-selection without touching the document, paint
  order, identities, selection, or the undo history;
* Cut is Copy + exactly one grouped delete across pages; undo restores
  the exact objects/positions/order, redo removes them again;
* Paste inserts new editable copies as one undo entry, maps source pages
  onto relative destination pages, selects the copies, preserves order,
  and leaves the originals untouched (undo/redo exact);
* context-menu and keyboard commands route through the same handlers,
  carry the platform shortcut hints, and the page-menu Paste anchors at
  the invocation point under zoom + scrolling;
* the payload outlives selection/tool changes, while an invalid
  clipboard or an out-of-range paste is a total no-op (no history entry,
  no document mutation, and no clipboard rewrite).
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt
from PySide6.QtGui import (
    QContextMenuEvent,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
)
from PySide6.QtWidgets import QApplication

from annotations.clipboard import (
    ClipboardError,
    ClipboardPayload,
    MIME_TYPE,
    paste_plan,
    payload_from_annotations,
)
from annotations.hit_test import annotation_extent
from annotations.models import (
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
    TextAssist,
)
from app.viewer import _PASTE_SCREEN_OFFSET
from core.types import PageId, Point, Rect, Size
from tests.helpers import make_pdf
from tools.models import ToolInstance, ToolType


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def clipboard(qapp):
    """A real (offscreen) clipboard, cleared before and after each test."""
    board = QGuiApplication.clipboard()
    board.setMimeData(QMimeData())
    yield board
    board.setMimeData(QMimeData())


# ---------------------------------------------------------------- model data

def _stroke(points=((100.0, 100.0), (140.0, 120.0), (180.0, 140.0)), **kw):
    base = dict(points=tuple(Point(x, y) for x, y in points),
                color="#112233", width_mm=0.6, opacity=0.9)
    base.update(kw)
    return InkStroke(**base)


def _highlight(points=((60.0, 300.0), (200.0, 302.0)), **kw):
    base = dict(points=tuple(Point(x, y) for x, y in points),
                color="#ffd60a", opacity=0.4, width_mm=3.0,
                text_assist=TextAssist(char_count=12, source="manual"))
    base.update(kw)
    return Highlighter(**base)


def _box(rect=(200.0, 200.0, 260.0, 240.0), **kw):
    base = dict(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(rect[0], rect[1]),
                          Size(rect[2] - rect[0], rect[3] - rect[1])),
                color="#e5484d", width_mm=1.0, opacity=0.8,
                fill_enabled=True, fill_color="#ffffff", fill_opacity=0.3,
                line_style="dashed", rotation_deg=30.0)
    base.update(kw)
    return Shape(**base)


def _page_id_for():
    table = {index: PageId(f"page-{index}") for index in range(16)}
    return lambda index: table[index]


# ------------------------------------------------------- payload (Qt-free)

def test_payload_round_trips_every_supported_type():
    ink = _stroke(pressures=(0.2, 0.6, 0.9), times_ms=(0, 12, 30),
                  pressure_enabled=True, pressure_profile="hard")
    highlight = _highlight()
    box = _box()
    payload = payload_from_annotations(
        [(3, box), (2, ink), (2, highlight)])

    # Document order regardless of caller order; anchor = lowest page.
    assert payload.count == 3
    assert payload.anchor_page == 2
    assert payload.page_span == 2
    assert [record.page for record in payload.records] == [2, 2, 3]

    wire = payload.to_bytes()
    assert json.loads(wire)["format"] == "inkfall-annotations"
    assert json.loads(wire)["version"] == 1
    parsed = ClipboardPayload.from_bytes(wire)
    assert parsed == payload

    page_id_for = _page_id_for()
    plan = paste_plan(parsed, anchor_page=0, page_count=4,
                      page_id_for_index=page_id_for)
    assert [page for page, _annotation in plan] == [0, 0, 1]

    restored_ink, restored_highlight, restored_box = [
        annotation for _page, annotation in plan]
    assert type(restored_ink) is InkStroke
    assert type(restored_highlight) is Highlighter
    assert type(restored_box) is Shape

    # Fresh identities, destination-derived page identities.
    pasted_ids = {annotation.id.value for _page, annotation in plan}
    assert len(pasted_ids) == 3
    assert pasted_ids.isdisjoint({ink.id.value, highlight.id.value,
                                  box.id.value})
    assert [annotation.page_id.value for _page, annotation in plan] == [
        "page-0", "page-0", "page-1"]

    # Editable properties travel exactly.
    assert restored_ink.points == ink.points
    assert restored_ink.color == "#112233"
    assert restored_ink.width_mm == 0.6
    assert restored_ink.opacity == 0.9
    assert restored_ink.pressures == (0.2, 0.6, 0.9)
    assert restored_ink.times_ms == (0, 12, 30)
    assert restored_ink.pressure_enabled is True
    assert restored_ink.pressure_profile == "hard"
    assert restored_highlight.points == highlight.points
    assert restored_highlight.text_assist == TextAssist(12, "manual")
    assert restored_box.kind is ShapeKind.RECTANGLE
    assert restored_box.rect == box.rect
    assert restored_box.fill_enabled is True
    assert restored_box.fill_color == "#ffffff"
    assert restored_box.fill_opacity == 0.3
    assert restored_box.line_style == "dashed"
    assert restored_box.rotation_deg == 30.0


def test_payload_carries_no_runtime_identity():
    ink = _stroke()
    wire = payload_from_annotations([(0, ink)]).to_bytes().decode("utf-8")
    assert str(ink.id.value) not in wire
    assert str(ink.page_id.value) not in wire
    record = json.loads(wire)["records"][0]
    assert set(record) == {"page", "kind", "data"}


def test_paste_translation_preserves_group_layout_and_page_spread():
    ink = _stroke()
    box = _box()
    span = _stroke(points=((20.0, 30.0), (90.0, 60.0)))
    payload = payload_from_annotations([(0, ink), (0, box), (2, span)])
    page_id_for = _page_id_for()

    draft = paste_plan(payload, anchor_page=1, page_count=6,
                       page_id_for_index=page_id_for)
    moved = paste_plan(payload, anchor_page=1, page_count=6,
                       page_id_for_index=page_id_for,
                       offset=(12.5, -7.25))
    assert [page for page, _annotation in moved] == [1, 1, 3]
    for (page, original), (moved_page, candidate) in zip(draft, moved):
        assert page == moved_page
        left = annotation_extent(original)
        right = annotation_extent(candidate)
        assert left is not None and right is not None
        assert right[0] == pytest.approx(left[0] + 12.5)
        assert right[1] == pytest.approx(left[1] - 7.25)
        assert (right[2] - right[0]) == pytest.approx(left[2] - left[0])
        assert (right[3] - right[1]) == pytest.approx(left[3] - left[1])
    # Relative layout inside a page is untouched by the translation.
    first_point = moved[0][1].points[0]
    second_origin = moved[1][1].rect.origin
    assert second_origin.x - first_point.x == pytest.approx(
        box.rect.origin.x - ink.points[0].x)
    assert second_origin.y - first_point.y == pytest.approx(
        box.rect.origin.y - ink.points[0].y)


_DELETE = object()


def _raw(**overrides):
    """Mutate the real wire format of one ink record for rejection tests."""
    payload = payload_from_annotations([(1, _stroke())])
    document = json.loads(payload.to_bytes())
    for path, value in overrides.items():
        target = document
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if key.isdigit() else target[key]
        last = keys[-1]
        index = int(last) if last.isdigit() else last
        if value is _DELETE:
            del target[index]
        else:
            target[index] = value
    return json.dumps(document).encode("utf-8")


@pytest.mark.parametrize("wire", [
    b"",
    b"not json at all",
    b"[]",
    b"{}",
    _raw(format="something-else"),
    _raw(version=2),
    _raw(records=[]),
    _raw(**{"records.0.kind": "video"}),
    _raw(**{"records.0.data": {}}),
    _raw(**{"records.0.data.color": "#112233", "records.0.extra": 1}),
    _raw(**{"records.0.data.points.0.0": float("nan")}),
    _raw(**{"records.0.data.opacity": 2.0}),
    _raw(**{"records.0.data.points": []}),
    _raw(**{"records.0.data.pressures": [0.5, 0.5]}),
    _raw(**{"records.0.data.points.0.0": _DELETE}),
])
def test_invalid_payloads_are_rejected_without_planning(wire):
    with pytest.raises(ClipboardError):
        ClipboardPayload.from_bytes(wire)


def test_group_that_does_not_fit_is_rejected_before_any_object():
    payload = payload_from_annotations([(0, _stroke()), (1, _stroke())])
    page_id_for = _page_id_for()

    with pytest.raises(ClipboardError):
        paste_plan(payload, anchor_page=3, page_count=4,
                   page_id_for_index=page_id_for)
    with pytest.raises(ClipboardError):
        paste_plan(payload, anchor_page=0, page_count=1,
                   page_id_for_index=page_id_for)

    plan = paste_plan(payload, anchor_page=2, page_count=4,
                      page_id_for_index=page_id_for)
    assert [page for page, _annotation in plan] == [2, 3]


# --------------------------------------------------------------- viewer setup

def _open_window(qtbot, tmp_path, pages=4, name="clipboard.pdf"):
    from app.main_window import MainWindow

    pdf = make_pdf(str(tmp_path / name), pages=pages, page_width=600.0,
                   page_height=400.0)
    window = MainWindow()
    window.resize(900, 700)
    window.show()
    qtbot.addWidget(window)
    assert window.viewer.open_document(pdf) is True
    window._stack.setCurrentIndex(1)
    viewer = window.viewer
    viewer.setFocus()
    qtbot.wait(60)
    return window, viewer


def _seed(viewer, annotation, page_index=0):
    layout = viewer._pipeline.layout
    stored = dataclasses.replace(
        annotation, page_id=layout.entry(page_index).page_id)
    viewer._document.annotations.add(stored, page_index)
    return stored


def _store(viewer):
    return viewer._document.annotations


def _pages(viewer):
    return [_store(viewer).page(index)
            for index in range(_store(viewer).page_count)]


def _history(viewer):
    return viewer.document_history.stack


def _selected(viewer):
    return [annotation_id.value
            for annotation_id in viewer._selection.selected_ids]


def _action(menu, text):
    for action in menu.actions():
        if action.text() == text:
            return action
    raise AssertionError(
        f"no action {text!r} in {[a.text() for a in menu.actions()]}")


def _factor(viewer):
    return (float(viewer._pipeline.layout.raster_scale)
            * viewer._display_scale())


def _close_menu(viewer):
    menu = getattr(viewer, "_selection_menu", None)
    if menu is not None:
        try:
            menu.close()
        except Exception:
            pass
    viewer._on_selection_menu_hidden()
    QApplication.instance().processEvents()


def _key(viewer, key, modifier=Qt.KeyboardModifier.NoModifier):
    viewer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifier))


# ------------------------------------------------------------------- Copy

def test_copy_multi_selection_leaves_document_and_history_unchanged(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path)
    first = _seed(viewer, _stroke(), 0)
    second = _seed(viewer, _box(), 0)
    third = _seed(viewer, _highlight(), 1)
    pages_before = _pages(viewer)
    viewer._selection.set([first.id, second.id, third.id])

    assert viewer._copy_selection() is True

    assert _pages(viewer) == pages_before
    assert _store(viewer).get(first.id) is first
    assert _history(viewer).count() == 0
    assert _selected(viewer) == [first.id.value, second.id.value,
                                 third.id.value]
    payload = viewer._clipboard_payload()
    assert payload is not None
    assert payload.count == 3
    assert payload.anchor_page == 0
    assert payload.page_span == 2
    window.close()


# -------------------------------------------------------------------- Cut

def test_cut_is_one_undoable_operation_across_pages(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path)
    survivor = _seed(viewer, _stroke(points=((10.0, 10.0), (30.0, 30.0))), 0)
    first = _seed(viewer, _stroke(), 0)
    second = _seed(viewer, _box(), 0)
    third = _seed(viewer, _highlight(), 1)
    viewer._selection.set([first.id, second.id, third.id])

    assert viewer._cut_selection() is True

    assert _history(viewer).count() == 1
    assert _store(viewer).page(0) == (survivor,)
    assert _store(viewer).count(1) == 0
    assert _selected(viewer) == []
    payload = viewer._clipboard_payload()
    assert payload is not None and payload.count == 3

    _history(viewer).undo()
    assert _store(viewer).page(0) == (survivor, first, second)
    assert _store(viewer).page(1) == (third,)
    assert _history(viewer).count() == 1

    _history(viewer).redo()
    assert _store(viewer).page(0) == (survivor,)
    assert _store(viewer).count(1) == 0
    window.close()


# ------------------------------------------------------------------ Paste

def test_paste_inserts_fresh_copies_and_selects_them(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path)
    first = _seed(viewer, _stroke(), 0)
    second = _seed(viewer, _box(), 0)
    third = _seed(viewer, _highlight(), 1)
    viewer._selection.set([first.id, second.id, third.id])
    assert viewer._copy_selection() is True

    assert viewer._paste_clipboard((1, 260.0, 200.0)) is True

    assert _history(viewer).count() == 1
    pasted_ids = _selected(viewer)
    assert len(pasted_ids) == 3
    assert set(pasted_ids).isdisjoint(
        {first.id.value, second.id.value, third.id.value})

    # Relative pages: source 0,0,1 -> destination 1,1,2, same order.
    page_one = [annotation.id.value for annotation in _store(viewer).page(1)
                if annotation.id.value in set(pasted_ids)]
    page_two = [annotation.id.value for annotation in _store(viewer).page(2)
                if annotation.id.value in set(pasted_ids)]
    assert page_one == pasted_ids[:2]
    assert page_two == pasted_ids[2:]
    assert _store(viewer).page(0) == (first, second)
    assert _store(viewer).page(1)[0] is third

    # Fresh, fully editable copies.
    ink_copy = _store(viewer).get(viewer._selection.selected_ids[0])
    box_copy = _store(viewer).get(viewer._selection.selected_ids[1])
    assert type(ink_copy) is InkStroke and type(box_copy) is Shape
    assert ink_copy.color == first.color
    assert ink_copy.width_mm == first.width_mm
    assert ink_copy.opacity == first.opacity
    assert len(ink_copy.points) == len(first.points)
    dx = ink_copy.points[0].x - first.points[0].x
    dy = ink_copy.points[0].y - first.points[0].y
    assert (dx, dy) != (0.0, 0.0)
    for original_point, copied_point in zip(first.points, ink_copy.points):
        assert copied_point.x - original_point.x == pytest.approx(dx)
        assert copied_point.y - original_point.y == pytest.approx(dy)
    assert box_copy.rect.size == second.rect.size
    assert box_copy.rotation_deg == second.rotation_deg
    assert box_copy.fill_enabled is True
    assert box_copy.line_style == second.line_style

    # The originals are byte-for-byte the same objects.
    assert _store(viewer).get(first.id) is first
    assert _store(viewer).get(second.id) is second
    assert _store(viewer).get(third.id) is third

    _history(viewer).undo()
    assert _store(viewer).page(0) == (first, second)
    assert _store(viewer).page(1) == (third,)
    assert _store(viewer).count(2) == 0
    assert _history(viewer).count() == 1

    _history(viewer).redo()
    assert _selected(viewer) == pasted_ids
    assert _store(viewer).get(ink_copy.id) is ink_copy
    assert _store(viewer).get(box_copy.id) is box_copy
    window.close()


def _visible_page_point(viewer, minimum=140.0):
    """A widget point well inside both the viewport and a visible page."""
    layout = viewer._pipeline.layout
    for index in range(layout.page_count):
        entry = layout.entry(index)
        rect = viewer._region_page_screen(entry)
        x0 = max(float(rect.x), 0.0)
        x1 = min(float(rect.x) + float(rect.width), float(viewer.width()))
        y0 = max(float(rect.y), 0.0)
        y1 = min(float(rect.y) + float(rect.height), float(viewer.height()))
        if x1 - x0 >= minimum and y1 - y0 >= minimum:
            # Stay clear of the page edge: the paste offset must not clamp.
            return index, ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
    return None, None


def test_page_menu_paste_anchors_at_the_invocation_point(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path, pages=5)
    source = _seed(viewer, _stroke(points=((20.0, 20.0), (60.0, 50.0))), 0)
    viewer._selection.set([source.id])
    assert viewer._copy_selection() is True

    # Zoom + scroll away from the source page before invoking the menu.
    viewer.set_zoom(1.75)
    viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
    qtbot.wait(80)

    page_index, point = _visible_page_point(viewer)
    assert page_index is not None
    wx, wy = point
    mapped = viewer._event_to_page_pt(wx, wy)
    assert mapped is not None
    anchor_lx, anchor_ly = mapped[1], mapped[2]

    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(int(wx), int(wy)),
        viewer.mapToGlobal(QPoint(int(wx), int(wy))))
    viewer.contextMenuEvent(event)
    menu = viewer._selection_menu
    assert menu is not None
    assert viewer._menu_anchor == (page_index, anchor_lx, anchor_ly)

    paste = _action(menu, "Paste")
    assert paste.isEnabled() is True
    paste.trigger()
    _close_menu(viewer)

    assert _history(viewer).count() == 1
    assert len(_selected(viewer)) == 1
    assert _selected(viewer) != [source.id.value]
    copy = _store(viewer).page(page_index)[-1]
    extent = annotation_extent(copy)
    assert extent is not None

    entry = viewer._pipeline.layout.entry(page_index)
    left, top = viewer._page_widget_point(entry, extent[0], extent[1])
    pointer_x, pointer_y = viewer._page_widget_point(entry, anchor_lx,
                                                     anchor_ly)
    assert left - pointer_x == pytest.approx(_PASTE_SCREEN_OFFSET, abs=1.5)
    assert top - pointer_y == pytest.approx(_PASTE_SCREEN_OFFSET, abs=1.5)
    # The offset is a screen-space constant converted to page pt, never a
    # widget-pixel delta added to page coordinates.
    assert (extent[0] - anchor_lx) == pytest.approx(
        _PASTE_SCREEN_OFFSET / _factor(viewer), rel=0.02)
    window.close()


# ------------------------------------------- shared handlers / shortcuts

def test_menu_and_keyboard_use_the_same_handlers(qtbot, tmp_path, clipboard,
                                                 monkeypatch):
    window, viewer = _open_window(qtbot, tmp_path)
    source = _seed(viewer, _stroke(), 0)
    calls = []

    monkeypatch.setattr(viewer, "_copy_selection",
                        lambda *a, **k: calls.append(("copy", a)) or True)
    monkeypatch.setattr(viewer, "_cut_selection",
                        lambda *a, **k: calls.append(("cut", a)) or True)
    monkeypatch.setattr(viewer, "_paste_clipboard",
                        lambda *a, **k: calls.append(("paste", a)) or True)

    viewer._selection.set([source.id])
    from app import clipboard as bridge

    assert bridge.write_payload(
        payload_from_annotations([(0, _stroke())])) is True
    viewer._menu_anchor = (0, 120.0, 80.0)
    menu = viewer._build_selection_menu([source.id], {source.id: source})
    assert _action(menu, "Cut").shortcut() == QKeySequence(
        QKeySequence.StandardKey.Cut)
    assert _action(menu, "Copy").shortcut() == QKeySequence(
        QKeySequence.StandardKey.Copy)
    assert _action(menu, "Paste").shortcut() == QKeySequence(
        QKeySequence.StandardKey.Paste)
    _action(menu, "Cut").trigger()
    _action(menu, "Copy").trigger()
    _action(menu, "Paste").trigger()
    menu.close()
    assert [name for name, _args in calls] == ["cut", "copy", "paste"]
    # The menu Paste captured *its* invocation point.
    assert calls[2][1] == ((0, 120.0, 80.0),)

    calls.clear()
    _key(viewer, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    _key(viewer, Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    _key(viewer, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert [name for name, _args in calls] == ["copy", "cut", "paste"]
    # The keyboard path has no menu invocation point.
    assert calls[2][1] == (None,)
    window.close()


# ------------------------------------------------- invariance + no-op safety

def test_clipboard_survives_selection_and_tool_changes(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path)
    source = _seed(viewer, _stroke(), 0)
    viewer._selection.set([source.id])
    assert viewer._copy_selection() is True

    viewer._clear_selection_state()
    viewer._interaction.set_tool(
        ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#e01b24",
                     width_mm=1.0, opacity=0.9))
    _store(viewer).remove(source.id)
    assert _selected(viewer) == []
    assert viewer._can_paste() is True

    assert viewer._paste_clipboard((0, 140.0, 140.0)) is True
    assert _history(viewer).count() == 1
    assert len(_store(viewer).page(0)) == 1
    assert len(_selected(viewer)) == 1
    assert viewer._selection_frame_visible() is True
    window.close()


def test_invalid_clipboard_and_out_of_range_paste_are_no_ops(
        qtbot, tmp_path, clipboard):
    window, viewer = _open_window(qtbot, tmp_path, pages=4)
    source = _seed(viewer, _stroke(), 0)
    viewer._selection.set([source.id])
    pages_before = _pages(viewer)

    # Unrelated clipboard content: Paste neither mutates nor rewrites it.
    clipboard.setText("keep this text")
    assert viewer._paste_clipboard(None) is False
    assert clipboard.text() == "keep this text"
    assert _pages(viewer) == pages_before
    assert _history(viewer).count() == 0
    assert _selected(viewer) == [source.id.value]

    # A valid two-page payload that cannot fit at the requested anchor.
    payload = payload_from_annotations([(0, _stroke()), (1, _stroke())])
    from app import clipboard as bridge

    assert bridge.write_payload(payload) is True
    assert viewer._can_paste(0) is True
    assert viewer._can_paste(3) is False
    assert viewer._paste_clipboard((3, 100.0, 100.0)) is False
    assert _pages(viewer) == pages_before
    assert _history(viewer).count() == 0
    assert _selected(viewer) == [source.id.value]
    window.close()
