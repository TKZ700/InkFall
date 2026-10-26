"""M5-A1 interaction routing tests: pan intents, stub tools, boundaries."""

import ast
from pathlib import Path

from annotations.interaction import InteractionController
from tools.models import ToolInstance, ToolType

ROOT = Path(__file__).resolve().parent.parent


def _pan_tool():
    return ToolInstance(kind=ToolType.PAN, name="Pan")


def test_pan_produces_scroll_intent():
    controller = InteractionController(_pan_tool())
    assert controller.handle_press(100.0, 100.0) == {"noop": True}
    intent = controller.handle_move(100.0, 140.0)
    assert intent == {"pan_by": (0.0, -40.0)}
    assert controller.handle_release(100.0, 140.0) == {"noop": True}


def test_pan_zero_delta_is_noop():
    controller = InteractionController(_pan_tool())
    controller.handle_press(50.0, 50.0)
    assert controller.handle_move(50.0, 50.0) == {"noop": True}


def test_non_pan_tools_route_to_recording_stub():
    # M6.7: the shape tools are now real (mapped) handlers too, so the
    # "not yet a real tool -> recording stub" contract is carried by a
    # genuinely-unimplemented tool (Note).
    note = ToolInstance(kind=ToolType.NOTE, name="Note")
    controller = InteractionController(note)
    assert controller.active_kind == ToolType.NOTE
    assert controller.handle_press(10.0, 20.0) == {"noop": True}
    assert controller.handle_move(30.0, 40.0) == {"noop": True}
    assert controller.handle_release(30.0, 40.0) == {"noop": True}
    stub = controller._handler()
    assert stub.events and stub.events[0].startswith("down:")


def test_shape_tools_route_to_shape_handler_not_stub():
    """M6.7: every shape kind is a real mapped handler (never a stub)."""
    from annotations.interaction import ShapeHandler

    for kind in (ToolType.LINE, ToolType.ARROW, ToolType.RECTANGLE,
                 ToolType.ELLIPSE, ToolType.POLYGON):
        controller = InteractionController(
            ToolInstance(kind=kind, name="Shape"))
        assert controller.active_kind == kind
        assert isinstance(controller._handler(), ShapeHandler)
        # Raw viewport-px input is still ignored for the shape tools: the
        # viewer maps coordinates first, exactly like pen/highlighter.
        assert controller.handle_press(10.0, 20.0) == {"noop": True}
        assert controller.handle_move(30.0, 40.0) == {"noop": True}
        assert controller.handle_release(30.0, 40.0) == {"noop": True}


def test_pen_routes_to_pen_handler_not_stub():
    """M5-B.1: pen gestures arrive mapped via handle_pen_* (stroke intents).

    Raw viewport-px handle_* calls are ignored for pen (the viewer maps
    coordinates first); unimplemented tools keep the recording stub.
    """
    from annotations.interaction import PenHandler
    from core.types import PageId

    pen = ToolInstance(kind=ToolType.PEN, name="Pen #1",
                       color="#112233", width_mm=0.7, opacity=0.8)
    controller = InteractionController(pen)
    assert isinstance(controller._handler(), PenHandler)
    assert controller.handle_press(10.0, 20.0) == {"noop": True}
    assert controller.handle_move(30.0, 40.0) == {"noop": True}
    assert controller.handle_release(30.0, 40.0) == {"noop": True}
    page = PageId()
    update = controller.handle_pen_press(page, 5.0, 6.0)
    assert "stroke_update" in update
    done = controller.handle_pen_release(page, 7.0, 8.0)
    stroke = done["stroke_done"]
    assert tuple((p.x, p.y) for p in stroke.points) == ((5.0, 6.0),
                                                        (7.0, 8.0))
    assert (stroke.color, stroke.width_mm, stroke.opacity) == (
        "#112233", 0.7, 0.8)
    assert stroke.page_id == page


def test_pen_move_without_press_is_noop_and_cancel_clears():
    from core.types import PageId

    pen = ToolInstance(kind=ToolType.PEN, name="Pen")
    controller = InteractionController(pen)
    page = PageId()
    assert controller.handle_pen_move(page, 1.0, 1.0) == {"noop": True}
    assert controller.handle_pen_release(page, 1.0, 1.0) == {"noop": True}
    controller.handle_pen_press(page, 1.0, 1.0)
    assert controller.cancel() == {"stroke_cancelled": True}
    # Cancelled stroke never completes: release afterwards is a noop.
    assert controller.handle_pen_release(page, 2.0, 2.0) == {"noop": True}


def test_tool_switch_resets_pan_gesture():
    controller = InteractionController(_pan_tool())
    controller.handle_press(0.0, 0.0)
    other = ToolInstance(kind=ToolType.PEN, name="Pen")
    controller.set_tool(other)
    assert controller.active_kind == ToolType.PEN
    # Stale pan state was cancelled: a move without press is a noop.
    assert controller.handle_move(10.0, 10.0) == {"noop": True}


def test_no_tool_defaults_to_pan():
    controller = InteractionController(None)
    assert controller.active_kind is None
    controller.handle_press(5.0, 5.0)
    assert controller.handle_move(5.0, 25.0) == {"pan_by": (0.0, -20.0)}


def test_interaction_layer_has_no_qt_fitz_or_rendering_imports():
    source = (ROOT / "annotations" / "interaction.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not ({"PySide6", "PySide2", "PyQt5", "PyQt6"} & modules)
    assert not ({"fitz", "pymupdf"} & modules)
    assert "from rendering" not in source and "import rendering" not in source
    assert "from document" not in source and "import document" not in source
    assert "Scheduler" not in source and "ProcessDecoder" not in source
