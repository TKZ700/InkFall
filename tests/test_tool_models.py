"""Tool model tests: definitions vs independent instances."""

from tools.models import ToolDefinition, ToolInstance, ToolType


def test_tool_definition_represents_type():
    pen = ToolDefinition(kind=ToolType.PEN, display_name="Pen")
    assert pen.kind == ToolType.PEN


def test_two_pen_instances_have_different_properties():
    pen1 = ToolInstance(kind=ToolType.PEN, name="Pen #1", color="#000000", width_mm=0.5)
    pen2 = ToolInstance(kind=ToolType.PEN, name="Pen #2", color="#0000FF", width_mm=0.7)
    assert pen1.id != pen2.id
    assert pen1.color != pen2.color
    assert pen1.width_mm != pen2.width_mm


def test_two_highlighter_instances_have_different_properties():
    hl1 = ToolInstance(
        kind=ToolType.HIGHLIGHTER, name="Highlighter #1",
        color="#FFFF00", width_mm=3.0, opacity=0.4,
    )
    hl2 = ToolInstance(
        kind=ToolType.HIGHLIGHTER, name="Highlighter #2",
        color="#00FF00", width_mm=3.0, opacity=0.3,
    )
    assert hl1.opacity != hl2.opacity
    assert hl1.color != hl2.color


def test_toolbar_can_hold_arbitrary_instances():
    presets = [
        ToolInstance(kind=ToolType.PEN, name=f"Pen #{i}", color="#000000", width_mm=0.5 + i * 0.1)
        for i in range(5)
    ]
    assert len(presets) == 5
    assert len({p.id for p in presets}) == 5
