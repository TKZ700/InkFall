"""Tool data models (M0 foundation, no UI).

Distinction:
- ToolDefinition: defines a tool *type* (Pen, Highlighter, ...).
- ToolInstance: a user's configured *preset* instance of that type.

The future toolbar holds an arbitrary list of ToolInstances, never a
hardcoded list of tool types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.types import ToolId


class ToolType(str, Enum):
    """Available tool types."""

    PAN = "pan"
    SELECTION = "selection"
    LASSO = "lasso"
    PEN = "pen"
    HIGHLIGHTER = "highlighter"
    ERASER = "eraser"
    LINE = "line"
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    ARROW = "arrow"
    POLYGON = "polygon"
    NOTE = "note"


#: Line-style patterns (visual stroke previews, never text-first).
LINE_STYLES: tuple[str, ...] = ("solid", "dashed", "dotted")

#: Line ending styles (visual ending previews, never text-first).
#: "none" = plain stroke end; "dot"/"bar"/"arrow" draw the marker.
LINE_ENDINGS: tuple[str, ...] = ("none", "dot", "bar", "arrow")

#: Eraser modes (M6.6). "whole" removes every complete Pen/Highlighter
#: annotation touched by the eraser (the M6.5 behavior); "partial"
#: removes only the geometry the eraser path actually intersects.
ERASER_MODE_WHOLE = "whole"
ERASER_MODE_PARTIAL = "partial"
ERASER_MODES: tuple[str, ...] = (ERASER_MODE_WHOLE, ERASER_MODE_PARTIAL)
ERASER_MODE_LABELS: dict[str, str] = {
    ERASER_MODE_WHOLE: "Stroke",
    ERASER_MODE_PARTIAL: "Partial",
}
DEFAULT_ERASER_MODE = ERASER_MODE_WHOLE


def normalize_eraser_mode(value) -> str:
    """Coerce a stored/forwarded eraser mode; unknown -> Whole (compat).

    Never raises: a missing, legacy, or corrupt value falls back to the
    compatible Whole Stroke default.
    """
    try:
        text = str(value)
    except Exception:
        return DEFAULT_ERASER_MODE
    return text if text in ERASER_MODES else DEFAULT_ERASER_MODE


#: Editable property names per tool kind (M5-A1 refinement). Pan,
#: Selection and Lasso expose no properties; every other kind maps
#: onto shared ToolInstance fields (consumed by the future annotation
#: engine). The UI filters rows through this table — never through
#: widget names. Blend Mode is NOT part of the design.
EDITABLE_PROPERTIES: dict[ToolType, tuple[str, ...]] = {
    ToolType.PAN: (),
    ToolType.SELECTION: (),
    ToolType.LASSO: (),
    ToolType.PEN: ("color", "width_mm", "opacity"),
    ToolType.HIGHLIGHTER: ("color", "width_mm", "opacity", "straight_line"),
    ToolType.ERASER: ("eraser_mode", "width_mm"),
    ToolType.LINE: ("color", "width_mm", "opacity", "line_style",
                    "start_ending", "end_ending", "snap_angle",
                    "connect_lines"),
    ToolType.RECTANGLE: ("color", "width_mm", "opacity", "fill_enabled",
                         "fill_color", "fill_opacity", "line_style",
                         "square"),
    ToolType.ELLIPSE: ("color", "width_mm", "opacity", "fill_enabled",
                       "fill_color", "fill_opacity", "line_style",
                       "circle"),
    ToolType.ARROW: ("color", "width_mm", "opacity", "line_style",
                     "start_ending", "end_ending", "snap_angle",
                     "connect_lines"),
    # M6.7.2: Polygon always connects (endpoint + intersection snapping)
    # and is always a closed shape, so neither is a user-editable
    # property. The behavior is forced internally by the shape handler.
    ToolType.POLYGON: ("color", "width_mm", "opacity", "fill_enabled",
                       "fill_color", "fill_opacity", "line_style",
                       "snap_angle"),
    ToolType.NOTE: ("color",),
}


def editable_properties(kind: ToolType) -> tuple[str, ...]:
    """Property names editable for a tool kind (empty for Pan/Selection/Lasso)."""
    try:
        return EDITABLE_PROPERTIES[kind]
    except KeyError:
        return ()


def has_editable_properties(instance: ToolInstance) -> bool:
    """True when a tool instance exposes a properties UI."""
    try:
        return len(editable_properties(instance.kind)) > 0
    except Exception:
        return False


#: Tool kinds that are permanent built-in core tools (Pan + Selection).
#: Core tools cannot be duplicated, removed, reordered, added via the
#: Add picker, or edited through the normal Tool Properties popup.
CORE_KINDS: tuple[ToolType, ...] = (ToolType.PAN, ToolType.SELECTION)


def is_core_tool(instance: ToolInstance) -> bool:
    """True for permanent core tools (role check — never widget names)."""
    try:
        return instance.kind in CORE_KINDS
    except Exception:
        return False


#: Visual size preset steps per tool kind, in mm. Presentation scale for
#: the visual-first size controls — the numeric editor beside them writes
#: the same ``width_mm`` field, so both paths converge on one value.
SIZE_PRESETS: dict[ToolType, tuple[float, ...]] = {
    ToolType.PEN: (0.3, 0.5, 0.8, 1.2, 2.0),
    ToolType.HIGHLIGHTER: (1.5, 2.5, 3.5, 5.0),
    ToolType.ERASER: (1.0, 2.0, 3.5, 5.0),
    ToolType.LINE: (0.3, 0.5, 0.8, 1.2),
    ToolType.RECTANGLE: (0.3, 0.5, 0.8, 1.2),
    ToolType.ELLIPSE: (0.3, 0.5, 0.8, 1.2),
    ToolType.ARROW: (0.3, 0.5, 0.8, 1.2),
    ToolType.POLYGON: (0.3, 0.5, 0.8, 1.2),
}


def size_presets_for(kind: ToolType) -> tuple[float, ...]:
    """Size preset steps for a kind (empty when size is not editable)."""
    try:
        if "width_mm" not in EDITABLE_PROPERTIES[kind]:
            return ()
        return SIZE_PRESETS.get(kind, ())
    except KeyError:
        return ()


def size_family(kind: ToolType) -> str:
    """Visual size family: ``dot`` (pen/eraser circles) or ``stroke``."""
    if kind in (ToolType.PEN, ToolType.ERASER):
        return "dot"
    return "stroke"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Defines a tool type (e.g. Pen)."""

    id: ToolId = field(default_factory=ToolId)
    kind: ToolType = ToolType.PEN
    display_name: str = "Pen"


@dataclass(frozen=True, slots=True)
class ToolInstance:
    """A user's configured preset, e.g. 'Pen #2, blue, 0.7mm'.

    Additive optional fields (fill/line/endings/square/circle) carry
    safe defaults so persisted slots from before the extension load
    unchanged. No Blend Mode, no text-specific fields: those designs
    do not exist in Inkfall.
    """

    id: ToolId = field(default_factory=ToolId)
    kind: ToolType = ToolType.PEN
    name: str = "Pen #1"
    color: str = "#000000"
    width_mm: float = 0.5
    opacity: float = 1.0
    pressure: bool = False
    smoothing: float = 0.0
    fill_enabled: bool = False
    fill_color: str = "#ffffff"
    fill_opacity: float = 1.0
    line_style: str = "solid"
    start_ending: str = "none"
    end_ending: str = "none"
    square: bool = False
    circle: bool = False
    # Gesture-assist configuration (M5-A2 UI + persistence; the drawing
    # behavior lands with the future annotation engine, which consumes
    # these fields — no engine is built here).
    snap_angle: bool = False
    connect_endpoints: bool = False
    connect_intersections: bool = False
    close_path: bool = False
    straight_line: bool = False
    # M6.6: Eraser mode ("whole" | "partial"), stored per tool slot.
    # Appended last so pre-M6.6 positional construction keeps working;
    # the default preserves the M6.5 Whole Stroke behavior.
    eraser_mode: str = DEFAULT_ERASER_MODE
    # M6.7.1: ONE user-facing "Connect Lines" setting. It drives the
    # complete supported auto-connect behavior (endpoint AND
    # intersection snapping). The legacy ``connect_endpoints`` /
    # ``connect_intersections`` fields are retained only as
    # backward-compatible mirrors: ``__post_init__`` keeps all three
    # equal, so they can never contradict one another and an enabled
    # legacy setting is never lost. Appended last so pre-M6.7.1
    # positional construction keeps working.
    connect_lines: bool = False

    def __post_init__(self) -> None:
        if self.width_mm <= 0:
            raise ValueError("width_mm must be positive")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be in [0, 1]")
        if not 0.0 <= self.fill_opacity <= 1.0:
            raise ValueError("fill_opacity must be in [0, 1]")
        if self.line_style not in LINE_STYLES:
            raise ValueError(f"line_style must be one of {LINE_STYLES}")
        if self.start_ending not in LINE_ENDINGS:
            raise ValueError(f"start_ending must be one of {LINE_ENDINGS}")
        if self.end_ending not in LINE_ENDINGS:
            raise ValueError(f"end_ending must be one of {LINE_ENDINGS}")
        # Legacy/corrupt eraser modes normalize to the compatible default
        # (never raise for stored data).
        if self.eraser_mode not in ERASER_MODES:
            object.__setattr__(
                self, "eraser_mode", normalize_eraser_mode(self.eraser_mode))
        # M6.7.1: collapse Connect Lines to one canonical value. A legacy
        # preset that enabled either constituent flag is promoted to the
        # single setting (never silently dropped); the mirrors are then
        # forced equal so no combination can disagree.
        connect = bool(self.connect_lines) or bool(self.connect_endpoints) \
            or bool(self.connect_intersections)
        if self.connect_lines != connect:
            object.__setattr__(self, "connect_lines", connect)
        if self.connect_endpoints != connect:
            object.__setattr__(self, "connect_endpoints", connect)
        if self.connect_intersections != connect:
            object.__setattr__(self, "connect_intersections", connect)
