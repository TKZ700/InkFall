"""Floating toolbar state store (M5-A1). Qt-free model + QSettings persistence.

A slot is a configured :class:`ToolInstance` (tools/models.py). The shelf
holds an arbitrary ordered list — never a hardcoded set — except that
Pan + Selection are always present (undeletable builtins, re-seeded on
restore). M5-A1 covers shell + CRUD/state only; no drawing.

Persistence (QSettings, never screen coords / animation state):
  inkfall/m5/toolbar/dock      left|right|top|bottom
  inkfall/m5/toolbar/active    ToolId.value
  inkfall/m5/toolbar/slots     JSON list of slot dicts (order = list order)
  inkfall/m5/toolbar/presets   JSON preset collections (customizable)
"""

from __future__ import annotations

import json

from core.types import ToolId
from tools.models import (
    DEFAULT_ERASER_MODE,
    ToolInstance,
    ToolType,
    normalize_eraser_mode,
)

__all__ = [
    "DOCK_POSITIONS",
    "DOCK_KEY",
    "ACTIVE_KEY",
    "SLOTS_KEY",
    "PRESETS_KEY",
    "DEFAULT_DOCK",
    "BUILTIN_KINDS",
    "ADDABLE_KINDS",
    "ToolShelfStore",
    "default_slots",
    "default_tool_config",
    "get_color_presets",
    "set_color_presets",
    "get_size_presets",
    "set_size_presets",
    "get_opacity_presets",
    "set_opacity_presets",
]

DOCK_POSITIONS = ("left", "right", "top", "bottom")
DEFAULT_DOCK = "left"
BUILTIN_KINDS = (ToolType.PAN, ToolType.SELECTION)

DOCK_KEY = "inkfall/m5/toolbar/dock"
ACTIVE_KEY = "inkfall/m5/toolbar/active"
SLOTS_KEY = "inkfall/m5/toolbar/slots"
PRESETS_KEY = "inkfall/m5/toolbar/presets"


def default_slots() -> list[ToolInstance]:
    """Seed shelf: always-on Pan + Selection navigation tools."""
    return [
        ToolInstance(kind=ToolType.PAN, name="Pan", color="#000000",
                     width_mm=0.5, opacity=1.0),
        ToolInstance(kind=ToolType.SELECTION, name="Select", color="#000000",
                     width_mm=0.5, opacity=1.0),
    ]


#: Kinds offered by the Add Tool picker (builtins are seeded, never added).
#: Grouped by the UI as Freehand (Pen/Highlighter/Eraser/Lasso),
#: Shapes (Line/Arrow/Rectangle/Ellipse/Polygon), Other (Note).
ADDABLE_KINDS: tuple[ToolType, ...] = (
    ToolType.PEN,
    ToolType.HIGHLIGHTER,
    ToolType.ERASER,
    ToolType.LASSO,
    ToolType.LINE,
    ToolType.ARROW,
    ToolType.RECTANGLE,
    ToolType.ELLIPSE,
    ToolType.POLYGON,
    ToolType.NOTE,
)

#: Add-library visual grouping (model-owned; widgets render it, never infer it).
ADDABLE_GROUPS: tuple[tuple[str, tuple[ToolType, ...]], ...] = (
    ("Freehand", (ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER,
                  ToolType.LASSO)),
    ("Shapes", (ToolType.LINE, ToolType.ARROW, ToolType.RECTANGLE,
                ToolType.ELLIPSE, ToolType.POLYGON)),
    ("Other", (ToolType.NOTE,)),
)

#: Model-owned per-kind defaults (widgets never hardcode these).
#: Fill stays disabled by default; the fill color default is a sensible
#: visible value applied only when Fill is explicitly enabled.
_DEFAULT_CONFIGS: dict[ToolType, dict] = {
    ToolType.PEN: {"name": "Pen", "color": "#1b1e26", "width_mm": 0.7,
                   "opacity": 1.0, "pressure": False, "smoothing": 0.35},
    ToolType.HIGHLIGHTER: {"name": "Highlighter", "color": "#ffd60a",
                           "width_mm": 3.5, "opacity": 0.4, "pressure": False,
                           "smoothing": 0.35},
    ToolType.ERASER: {"name": "Eraser", "color": "#6b7280", "width_mm": 2.0,
                      "opacity": 1.0, "pressure": False, "smoothing": 0.0,
                      "eraser_mode": DEFAULT_ERASER_MODE},
    ToolType.LASSO: {"name": "Lasso", "color": "#1b1e26", "width_mm": 0.5,
                     "opacity": 1.0, "pressure": False, "smoothing": 0.0},
    ToolType.LINE: {"name": "Line", "color": "#1b1e26", "width_mm": 0.5,
                    "opacity": 1.0, "pressure": False, "smoothing": 0.0,
                    "line_style": "solid", "start_ending": "none",
                    "end_ending": "none"},
    ToolType.RECTANGLE: {"name": "Rectangle", "color": "#1b1e26",
                         "width_mm": 0.5, "opacity": 1.0, "pressure": False,
                         "smoothing": 0.0, "fill_enabled": False,
                         "fill_color": "#ffffff", "fill_opacity": 1.0,
                         "line_style": "solid", "square": False},
    ToolType.ELLIPSE: {"name": "Ellipse", "color": "#1b1e26", "width_mm": 0.5,
                       "opacity": 1.0, "pressure": False, "smoothing": 0.0,
                       "fill_enabled": False, "fill_color": "#ffffff",
                       "fill_opacity": 1.0, "line_style": "solid",
                       "circle": False},
    ToolType.ARROW: {"name": "Arrow", "color": "#1b1e26", "width_mm": 0.5,
                     "opacity": 1.0, "pressure": False, "smoothing": 0.0,
                     "line_style": "solid", "start_ending": "none",
                     "end_ending": "arrow"},
    ToolType.POLYGON: {"name": "Polygon", "color": "#1b1e26",
                       "width_mm": 0.5, "opacity": 1.0, "pressure": False,
                       "smoothing": 0.0, "fill_enabled": False,
                       "fill_color": "#ffffff", "fill_opacity": 1.0,
                       "line_style": "solid"},
    ToolType.NOTE: {"name": "Note", "color": "#ffd60a", "width_mm": 0.5,
                    "opacity": 1.0, "pressure": False, "smoothing": 0.0},
}


def default_tool_config(kind: ToolType, name: str | None = None) -> ToolInstance:
    """Build a sensibly defaulted ToolInstance for an addable kind."""
    if kind not in ADDABLE_KINDS:
        raise ValueError(f"kind is not addable: {kind}")
    fields = dict(_DEFAULT_CONFIGS[kind])
    if name:
        fields["name"] = name
    return ToolInstance(kind=kind, **fields)


#: Starting preset collections (customizable, never sacred). The UI
#: reads these through the accessors below — never hardcodes them —
#: so users can later keep/remove/add/reorder presets. Persisted as
#: one JSON blob; missing keys fall back to the starting presets.
DEFAULT_COLOR_PRESETS: tuple[str, ...] = (
    "#111111", "#6b7280", "#e5484d", "#f76b15", "#ffd60a",
    "#30d158", "#2b6cff", "#8e4ec6", "#ad5700", "#ffffff",
)
DEFAULT_OPACITY_PRESETS: tuple[float, ...] = (0.2, 0.4, 0.7, 1.0)


def _read_preset_blob(settings=None) -> dict:
    if settings is None:
        try:
            from PySide6.QtCore import QSettings

            settings = QSettings()
        except Exception:
            return {}
    try:
        raw = settings.value(PRESETS_KEY, "")
        if raw:
            data = json.loads(str(raw))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_preset_blob(blob: dict, settings=None) -> None:
    if settings is None:
        try:
            from PySide6.QtCore import QSettings

            settings = QSettings()
        except Exception:
            return
    try:
        settings.setValue(PRESETS_KEY, json.dumps(blob))
    except Exception:
        pass


def get_color_presets(settings=None) -> list[str]:
    """Customizable color preset list (starting presets when unset)."""
    blob = _read_preset_blob(settings)
    try:
        colors = blob.get("colors", None)
        if isinstance(colors, list) and colors:
            return [str(c) for c in colors]
    except Exception:
        pass
    return list(DEFAULT_COLOR_PRESETS)


def set_color_presets(colors: list[str], settings=None) -> list[str]:
    """Replace the color preset list (persisted, returned)."""
    cleaned = [str(c) for c in colors if str(c).strip()]
    blob = _read_preset_blob(settings)
    blob["colors"] = cleaned or list(DEFAULT_COLOR_PRESETS)
    _write_preset_blob(blob, settings)
    return list(blob["colors"])


def get_size_presets(kind: ToolType, settings=None) -> list[float]:
    """Customizable size preset steps for a kind (model-seeded)."""
    from tools.models import SIZE_PRESETS, size_presets_for

    blob = _read_preset_blob(settings)
    try:
        sizes = (blob.get("sizes", {}) or {}).get(kind.value, None)
        if isinstance(sizes, list) and sizes:
            return [float(s) for s in sizes if float(s) > 0]
    except Exception:
        pass
    base = size_presets_for(kind)
    return list(base) if base else list(SIZE_PRESETS.get(kind, ()))


def set_size_presets(kind: ToolType, sizes: list[float],
                     settings=None) -> list[float]:
    """Replace the size preset steps for a kind (persisted, returned)."""
    from tools.models import size_presets_for

    cleaned = [float(s) for s in sizes if float(s) > 0]
    fallback = list(size_presets_for(kind)) or [0.5]
    blob = _read_preset_blob(settings)
    try:
        per_kind = blob.get("sizes", {})
        if not isinstance(per_kind, dict):
            per_kind = {}
    except Exception:
        per_kind = {}
    per_kind[kind.value] = cleaned or fallback
    blob["sizes"] = per_kind
    _write_preset_blob(blob, settings)
    return list(per_kind[kind.value])


def get_opacity_presets(settings=None) -> list[float]:
    """Customizable opacity preset stops (starting stops when unset)."""
    blob = _read_preset_blob(settings)
    try:
        stops = blob.get("opacities", None)
        if isinstance(stops, list) and stops:
            return [max(0.0, min(1.0, float(s))) for s in stops]
    except Exception:
        pass
    return list(DEFAULT_OPACITY_PRESETS)


def set_opacity_presets(stops: list[float], settings=None) -> list[float]:
    """Replace the opacity preset stops (persisted, returned)."""
    cleaned = [max(0.0, min(1.0, float(s))) for s in stops]
    blob = _read_preset_blob(settings)
    blob["opacities"] = cleaned or list(DEFAULT_OPACITY_PRESETS)
    _write_preset_blob(blob, settings)
    return list(blob["opacities"])


def _slot_to_dict(slot: ToolInstance) -> dict:
    data = {
        "id": slot.id.value,
        "kind": slot.kind.value,
        "name": slot.name,
        "color": slot.color,
        "width_mm": slot.width_mm,
        "opacity": slot.opacity,
        "pressure": slot.pressure,
        "smoothing": slot.smoothing,
        "fill_enabled": slot.fill_enabled,
        "fill_color": slot.fill_color,
        "fill_opacity": slot.fill_opacity,
        "line_style": slot.line_style,
        "start_ending": slot.start_ending,
        "end_ending": slot.end_ending,
        "square": slot.square,
        "circle": slot.circle,
        "snap_angle": slot.snap_angle,
        "connect_endpoints": slot.connect_endpoints,
        "connect_intersections": slot.connect_intersections,
        "connect_lines": slot.connect_lines,
        "close_path": slot.close_path,
        "straight_line": slot.straight_line,
        "eraser_mode": slot.eraser_mode,
    }
    return data


def _slot_from_dict(data: dict) -> ToolInstance | None:
    try:
        kind = ToolType(str(data.get("kind", "")))
    except ValueError:
        return None
    try:
        width = float(data.get("width_mm", 0.5))
        opacity = float(data.get("opacity", 1.0))
    except (TypeError, ValueError):
        return None
    # Old persisted slots predate the additive fields: every new key
    # falls back to its model default, so old settings load unchanged.
    # Stale Blend/Text keys are ignored (those designs do not exist).
    # M6.7.1: one canonical Connect Lines setting. A legacy preset that
    # stored the separate endpoint/intersection flags migrates to the
    # single setting if EITHER was enabled (deterministic, lossless).
    connect = (bool(data.get("connect_lines", False))
               or bool(data.get("connect_endpoints", False))
               or bool(data.get("connect_intersections", False)))
    try:
        return ToolInstance(
            id=ToolId(value=str(data.get("id") or ToolId().value)),
            kind=kind,
            name=str(data.get("name", kind.value.title())),
            color=str(data.get("color", "#000000")),
            width_mm=width,
            opacity=opacity,
            pressure=bool(data.get("pressure", False)),
            smoothing=float(data.get("smoothing", 0.0)),
            fill_enabled=bool(data.get("fill_enabled", False)),
            fill_color=str(data.get("fill_color", "#ffffff")),
            fill_opacity=float(data.get("fill_opacity", 1.0)),
            line_style=str(data.get("line_style", "solid")),
            start_ending=str(data.get("start_ending", "none")),
            end_ending=str(data.get("end_ending", "none")),
            square=bool(data.get("square", False)),
            circle=bool(data.get("circle", False)),
            snap_angle=bool(data.get("snap_angle", False)),
            connect_endpoints=connect,
            connect_intersections=connect,
            connect_lines=connect,
            close_path=bool(data.get("close_path", False)),
            straight_line=bool(data.get("straight_line", False)),
            eraser_mode=normalize_eraser_mode(
                data.get("eraser_mode", DEFAULT_ERASER_MODE)),
        )
    except (ValueError, TypeError):
        return None


def migrate_core_selection(
        slots: list[ToolInstance]) -> list[ToolInstance]:
    """Migrate stored core Lasso to the Selection core tool.

    Pre-split shelves persist the core navigator as kind ``lasso``
    (named "Select"). The split makes Selection the permanent core and
    Lasso an optional custom: the FIRST lasso slot (the core position)
    becomes ``selection`` with identity and name preserved — but only
    when no ``selection`` slot already exists. Otherwise the lasso
    stays a custom lasso (converting it would duplicate Selection).
    Every user-created custom lasso slot keeps kind ``lasso``
    untouched. Idempotent and lossless for legitimate customs.
    """
    kinds = {s.kind for s in slots}
    convert = ToolType.SELECTION not in kinds
    converted = False
    out: list[ToolInstance] = []
    for slot in slots:
        if slot.kind == ToolType.LASSO and convert and not converted:
            try:
                out.append(ToolInstance(
                    id=slot.id, kind=ToolType.SELECTION, name=slot.name,
                    color=slot.color, width_mm=slot.width_mm,
                    opacity=slot.opacity, pressure=slot.pressure,
                    smoothing=slot.smoothing,
                    fill_enabled=slot.fill_enabled,
                    fill_color=slot.fill_color,
                    fill_opacity=slot.fill_opacity,
                    line_style=slot.line_style,
                    start_ending=slot.start_ending,
                    end_ending=slot.end_ending, square=slot.square,
                    circle=slot.circle, snap_angle=slot.snap_angle,
                    connect_endpoints=slot.connect_endpoints,
                    connect_intersections=slot.connect_intersections,
                    connect_lines=slot.connect_lines,
                    close_path=slot.close_path,
                    straight_line=slot.straight_line))
                converted = True
                continue
            except (ValueError, TypeError):
                pass
        out.append(slot)
    return out


def dedupe_core_slots(slots: list[ToolInstance]) -> list[ToolInstance]:
    """Collapse duplicate core tools to one slot per core kind.

    Legacy persisted shelves can carry two Selection (or Pan) slots —
    e.g. a seeded builtin plus a migrated twin. Only the FIRST slot of
    each core kind survives (identity + active references stay valid);
    every custom slot — including custom lasso — is preserved untouched
    in relative order. Idempotent: a second pass changes nothing.
    """
    try:
        from tools.models import CORE_KINDS
    except Exception:
        return list(slots)
    seen: set = set()
    out: list[ToolInstance] = []
    for slot in slots:
        try:
            kind = slot.kind
        except Exception:
            out.append(slot)
            continue
        if kind in CORE_KINDS:
            if kind in seen:
                continue
            seen.add(kind)
        out.append(slot)
    return out


class ToolShelfStore:
    """Ordered tool-slot list with dock/active state."""

    def __init__(self, slots: list[ToolInstance] | None = None,
                 dock: str = DEFAULT_DOCK,
                 active_id: str | None = None) -> None:
        self._slots: list[ToolInstance] = list(slots) if slots else default_slots()
        self._dock = dock if dock in DOCK_POSITIONS else DEFAULT_DOCK
        self.ensure_defaults()
        if active_id is not None and self.find(active_id) is not None:
            self._active_id = active_id
        else:
            self._active_id = self._slots[0].id.value if self._slots else ""

    # -- state ----------------------------------------------------------

    @property
    def dock(self) -> str:
        return self._dock

    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def slots(self) -> tuple[ToolInstance, ...]:
        return tuple(self._slots)

    def find(self, slot_id: str) -> ToolInstance | None:
        for slot in self._slots:
            if slot.id.value == slot_id:
                return slot
        return None

    def active(self) -> ToolInstance | None:
        return self.find(self._active_id)

    def ensure_defaults(self) -> None:
        """Migrate + re-seed Pan/Selection builtins (front, stable order)."""
        self._slots = migrate_core_selection(list(self._slots))
        self._slots = dedupe_core_slots(self._slots)
        kinds = {s.kind for s in self._slots}
        insert_at = 0
        for kind, name in ((ToolType.PAN, "Pan"),
                           (ToolType.SELECTION, "Select")):
            if kind not in kinds:
                self._slots.insert(insert_at, ToolInstance(
                    kind=kind, name=name, color="#000000",
                    width_mm=0.5, opacity=1.0))
            insert_at += 1
        self._pin_core_front()

    def _pin_core_front(self) -> None:
        """Keep core tools pinned at indices 0..1 (stable Pan, Lasso order).

        Customs stay in relative order after the core block. Called after
        every mutation so no path can strand a core tool mid-list.
        """
        try:
            from tools.models import CORE_KINDS, is_core_tool
        except Exception:
            return
        core: list[ToolInstance] = []
        customs: list[ToolInstance] = []
        for slot in self._slots:
            try:
                (core if is_core_tool(slot) else customs).append(slot)
            except Exception:
                customs.append(slot)
        ordered_core = [s for kind in CORE_KINDS
                        for s in core if s.kind == kind]
        ordered_core += [s for s in core
                         if s.kind not in CORE_KINDS]
        self._slots = ordered_core + customs

    # -- roles ------------------------------------------------------------

    @property
    def core_slots(self) -> tuple[ToolInstance, ...]:
        """Permanent core tools (Pan, Selection) — always front."""
        try:
            from tools.models import is_core_tool

            return tuple(s for s in self._slots if is_core_tool(s))
        except Exception:
            return ()

    @property
    def custom_slots(self) -> tuple[ToolInstance, ...]:
        """User-customizable slots (everything that is not core)."""
        try:
            from tools.models import is_core_tool

            return tuple(s for s in self._slots if not is_core_tool(s))
        except Exception:
            return tuple(self._slots)

    def insertion_index_for_new(self) -> int:
        """Append position for a new custom tool (end of the shelf)."""
        return len(self._slots)

    # -- mutations --------------------------------------------------------

    def set_dock(self, dock: str) -> str:
        if dock not in DOCK_POSITIONS:
            raise ValueError(f"dock must be one of {DOCK_POSITIONS}")
        self._dock = dock
        return dock

    def set_active(self, slot_id: str) -> ToolInstance:
        slot = self.find(slot_id)
        if slot is None:
            raise KeyError(f"unknown tool slot: {slot_id}")
        self._active_id = slot.id.value
        return slot

    def add_slot(self, kind: ToolType = ToolType.PEN, **fields) -> ToolInstance:
        from tools.models import is_core_tool as _is_core

        probe = ToolInstance(kind=kind, **fields)
        if _is_core(probe):
            raise ValueError(f"core tools cannot be added: {kind.value}")
        self._slots.append(probe)
        self._pin_core_front()
        return probe

    def remove_slot(self, slot_id: str) -> None:
        slot = self.find(slot_id)
        if slot is None:
            raise KeyError(f"unknown tool slot: {slot_id}")
        if slot.kind in BUILTIN_KINDS:
            raise ValueError(f"cannot remove builtin tool: {slot.kind.value}")
        self._slots = [s for s in self._slots if s.id.value != slot_id]
        if self._active_id == slot_id:
            self._active_id = self._slots[0].id.value if self._slots else ""

    def duplicate_slot(self, slot_id: str) -> ToolInstance:
        slot = self.find(slot_id)
        if slot is None:
            raise KeyError(f"unknown tool slot: {slot_id}")
        from tools.models import is_core_tool as _is_core

        if _is_core(slot):
            raise ValueError(f"cannot duplicate core tool: {slot.kind.value}")
        data = _slot_to_dict(slot)
        data.pop("id", None)
        data["name"] = f"{slot.name} copy"
        copy = _slot_from_dict(data)
        if copy is None:
            raise ValueError("invalid tool configuration")
        index = next(i for i, s in enumerate(self._slots)
                     if s.id.value == slot_id)
        self._slots.insert(index + 1, copy)
        return copy

    def move_slot(self, slot_id: str, to_index: int) -> None:
        index = next((i for i, s in enumerate(self._slots)
                      if s.id.value == slot_id), None)
        if index is None:
            raise KeyError(f"unknown tool slot: {slot_id}")
        from tools.models import is_core_tool as _is_core

        if _is_core(self._slots[index]):
            raise ValueError("core tools cannot be reordered")
        slot = self._slots.pop(index)
        # Customs live after the core block — never land inside it.
        floor = len(self.core_slots)
        clamped = max(floor, min(len(self._slots), int(to_index)))
        self._slots.insert(clamped, slot)
        self._pin_core_front()

    def update_slot(self, slot_id: str, **fields) -> ToolInstance:
        slot = self.find(slot_id)
        if slot is None:
            raise KeyError(f"unknown tool slot: {slot_id}")
        if "id" in fields or "kind" in fields and fields.get("kind", slot.kind) != slot.kind:
            # Kind changes create a new logical tool; keep identity stable —
            # only non-identity fields are editable in place.
            if fields.get("kind", slot.kind) != slot.kind:
                raise ValueError("tool kind is immutable; duplicate instead")
            fields.pop("id", None)
        data = _slot_to_dict(slot)
        for key, value in fields.items():
            if key not in data or key == "id":
                raise ValueError(f"unknown tool field: {key}")
            data[key] = value
        updated = _slot_from_dict(data)
        if updated is None:
            raise ValueError("invalid tool configuration")
        index = next(i for i, s in enumerate(self._slots)
                     if s.id.value == slot_id)
        self._slots[index] = updated
        return updated

    # -- persistence -------------------------------------------------------

    def persist(self, settings=None) -> None:
        """Write dock/active/slots to QSettings (deferred-safe)."""
        if settings is None:
            from PySide6.QtCore import QSettings

            settings = QSettings()
        settings.setValue(DOCK_KEY, self._dock)
        settings.setValue(ACTIVE_KEY, self._active_id)
        settings.setValue(SLOTS_KEY, json.dumps([_slot_to_dict(s) for s in self._slots]))

    @classmethod
    def restore(cls, settings=None) -> "ToolShelfStore":
        """Read persisted state; corrupt entries fall back to defaults."""
        if settings is None:
            try:
                from PySide6.QtCore import QSettings

                settings = QSettings()
            except Exception:
                return cls()
        try:
            dock = str(settings.value(DOCK_KEY, DEFAULT_DOCK) or DEFAULT_DOCK)
        except Exception:
            dock = DEFAULT_DOCK
        try:
            active_id = settings.value(ACTIVE_KEY, None)
            active_id = str(active_id) if active_id else None
        except Exception:
            active_id = None
        slots: list[ToolInstance] = []
        try:
            raw = settings.value(SLOTS_KEY, "")
            if raw:
                for item in json.loads(str(raw)):
                    slot = _slot_from_dict(item) if isinstance(item, dict) else None
                    if slot is not None:
                        slots.append(slot)
        except Exception:
            slots = []
        return cls(slots=slots or None, dock=dock, active_id=active_id)
