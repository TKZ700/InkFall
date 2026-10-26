"""Annotation data models (M0 foundation, no engine).

A Highlighter is a freehand/vector annotation. It MUST be representable
without any PDF text extraction. ``text_assist`` is optional metadata that
a future text-aware mode may attach; it is never required.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum

from core.types import AnnotationId, PageId, Point, Rect, Size

from annotations.ink_geometry import (
    DEFAULT_PRESSURE_PROFILE,
    PRESSURE_PROFILES,
)
# M6.7: the committed shape reuses the *existing* line-style / ending
# vocabulary declared by the tool model (single source of truth), so a
# shape loaded from disk can be normalized without inventing a second
# property system. ``tools.models`` is Qt-free and imports only
# ``core.types``, so this is acyclic.
from tools.models import LINE_ENDINGS, LINE_STYLES


def _clamp_unit(value, default: float = 0.0) -> float:
    """Coerce to a finite float in ``[0, 1]``; else ``default`` (never raises)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(1.0, v))


def _normalize_deg(value, default: float = 0.0) -> float:
    """Coerce to a finite angle in ``(-180, 180]``; else ``default``.

    M6.8.2 shape-rotation semantics: angles wrap at a full turn (``360``
    and ``-360`` are ``0``, ``540`` is ``180``), the canonical
    half-turn is ``180`` (never ``-180``), and a missing or non-finite
    value falls back to ``default`` (never raises, matching
    ``_clamp_unit``).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    v = math.fmod(v, 360.0)
    if v <= -180.0:
        v += 360.0
    elif v > 180.0:
        v -= 360.0
    return v


@dataclass(frozen=True, slots=True)
class TextAssist:
    """Optional text-aware metadata for a highlighter.

    Future enhancement only. Stores e.g. matched char/quad info so the
    highlighter can snap to text. ``None`` means plain freehand.
    """

    char_count: int = 0
    source: str = "manual"


@dataclass(frozen=True, slots=True)
class AnnotationBase:
    """Shared identity fields for all annotations."""

    id: AnnotationId = field(default_factory=AnnotationId)
    page_id: PageId = field(default_factory=PageId)
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class InkStroke(AnnotationBase):
    """Freehand pen stroke defined by document-space points.

    M5-B.2.0: optional per-sample raw capture. ``pressures`` holds the
    normalized stylus pressure per point (0..1, clamped); ``times_ms``
    holds the per-sample event timestamp in milliseconds. Both are
    authoritative raw input: derived values (smoothed positions,
    per-sample widths, outline geometry) are recomputed, never stored.
    Empty tuples mean "not captured" (legacy strokes, mouse input
    without pressure); non-empty tuples must match ``points`` length.
    ``pressure_enabled`` selects variable-width outline rendering for
    this stroke; ``False`` keeps the legacy scalar-width path.
    """

    points: tuple[Point, ...] = ()
    color: str = "#000000"
    width_mm: float = 0.5
    opacity: float = 1.0
    pressures: tuple[float, ...] = ()
    times_ms: tuple[int, ...] = ()
    # M5-B.2.1 Slice 3: rendering-mode flag, latched at stroke start
    # (see PenHandler). Appended last so existing positional
    # construction keeps working; default False preserves legacy
    # scalar-width rendering. Immutable after creation: later tool
    # edits never restyle an existing stroke.
    pressure_enabled: bool = False
    # B.2.3: pressure-response profile name, latched at stroke start
    # alongside pressure_enabled (see PenHandler). Appended after it so
    # all earlier positional construction keeps working; default
    # "normal" preserves the original curve. Unknown values normalize
    # to "normal" in __post_init__ (forward-compatible, never raises
    # for stored data).
    pressure_profile: str = DEFAULT_PRESSURE_PROFILE

    def __post_init__(self) -> None:
        n = len(self.points)
        if self.pressures and len(self.pressures) != n:
            raise ValueError(
                f"pressures length {len(self.pressures)} != points {n}")
        if self.times_ms and len(self.times_ms) != n:
            raise ValueError(
                f"times_ms length {len(self.times_ms)} != points {n}")
        if self.pressures:
            clamped = []
            for p in self.pressures:
                try:
                    v = float(p)
                except (TypeError, ValueError):
                    raise ValueError(f"non-numeric pressure {p!r}")
                if v != v or v in (float("inf"), float("-inf")):
                    raise ValueError(f"non-finite pressure {p!r}")
                clamped.append(max(0.0, min(1.0, v)))
            object.__setattr__(self, "pressures", tuple(clamped))
        try:
            profile = str(self.pressure_profile)
        except Exception:
            profile = ""
        if profile not in PRESSURE_PROFILES:
            object.__setattr__(
                self, "pressure_profile", DEFAULT_PRESSURE_PROFILE)


@dataclass(frozen=True, slots=True)
class Highlighter(AnnotationBase):
    """Freehand highlighter. Text extraction is never required.

    M7.1.4: ``pressures`` optionally holds the normalized stylus pressure
    per point (0..1, clamped), parallel to ``points``. It is appended last
    so existing positional construction keeps working and existing
    annotations (empty tuple) stay readable. Empty means "no pressure
    captured" (legacy data) and renders at the constant ``width_mm``
    band; mouse input is captured as all-1.0, which maps to the same
    nominal width. Non-empty tuples must match ``points`` length.
    Variable band width is derived at paint/hit-test time, never stored.
    """

    points: tuple[Point, ...] = ()
    color: str = "#FFFF00"
    opacity: float = 0.4
    width_mm: float = 3.0
    text_assist: TextAssist | None = None
    pressures: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        n = len(self.points)
        if self.pressures and len(self.pressures) != n:
            raise ValueError(
                f"pressures length {len(self.pressures)} != points {n}")
        if self.pressures:
            clamped = []
            for p in self.pressures:
                try:
                    v = float(p)
                except (TypeError, ValueError):
                    raise ValueError(f"non-numeric pressure {p!r}")
                if v != v or v in (float("inf"), float("-inf")):
                    raise ValueError(f"non-finite pressure {p!r}")
                clamped.append(max(0.0, min(1.0, v)))
            object.__setattr__(self, "pressures", tuple(clamped))


class ShapeKind(str, Enum):
    """Supported shape kinds (data only, no rendering)."""

    LINE = "line"
    RECTANGLE = "rectangle"
    ELLIPSE = "ellipse"
    ARROW = "arrow"
    # M6.7: ordered-vertex polygon. Vertices live in ``Shape.points``;
    # ``Shape.rect`` still carries the normalized bounding box.
    POLYGON = "polygon"


@dataclass(frozen=True, slots=True)
class Shape(AnnotationBase):
    """Vector shape annotation in document (page-local) space.

    Canonical geometry (M6.7)
    -------------------------
    * ``RECTANGLE`` / ``ELLIPSE``: the enclosing box is ``rect`` (always
      normalized: positive width/height, ``origin`` at the top-left of the
      box). ``points`` is empty.
    * ``LINE`` / ``ARROW``: the two endpoints are ``points`` (start then
      end), which preserves direction — a normalized ``rect`` alone
      cannot. ``rect`` is the derived bounding box.
    * ``POLYGON``: the ordered vertices are ``points`` (not implicitly
      closed — the painter closes the path). ``rect`` is the bounding box.

    Rotation (M6.8.2)
    -----------------
    ``rotation_deg`` rotates a RECTANGLE's or ELLIPSE's *unrotated local
    box* about its own center (positive = clockwise in y-down page
    space, matching Qt's painter rotation). ``rect`` keeps the canonical
    unrotated box — for ``rotation_deg == 0`` (all pre-M6.8.2 data,
    default) it is both the local box and the enclosing box unchanged;
    for a rotated shape the *rendered* enclosing bounds come from
    ``annotations.shape_hit.shape_extent`` (and every consumer routed
    through it), while ``rect``/center stays the rotation pivot. All
    other kinds ignore ``rotation_deg``: point-based geometry (Line,
    Arrow, Polygon, ink) encodes any rotation directly in its points.
    The value is normalized in ``__post_init__`` to ``(-180, 180]``.

    All fields after ``width_mm`` are appended with safe defaults so
    pre-M6.7 positional construction (and stored data) loads unchanged.
    ``__post_init__`` only *normalizes*; it never raises, matching the
    forward-compatible contract used by ``InkStroke``.
    """

    kind: ShapeKind = ShapeKind.RECTANGLE
    rect: Rect = field(
        default_factory=lambda: Rect(Point(0.0, 0.0), Size(10.0, 10.0))
    )
    color: str = "#000000"
    width_mm: float = 0.5
    # Outline opacity (M6.7), independent of any fill opacity below.
    opacity: float = 1.0
    fill_enabled: bool = False
    fill_color: str = "#ffffff"
    fill_opacity: float = 1.0
    line_style: str = "solid"
    start_ending: str = "none"
    end_ending: str = "none"
    # Line/Arrow endpoints and ordered Polygon vertices (see above).
    points: tuple[Point, ...] = ()
    # Polygon completion assist (M6.7): True draws the closing segment
    # (last vertex -> first) and allows a fill; False leaves the placed
    # edges as an open polyline. Matches the tool's "Close shape" toggle.
    close_path: bool = False
    # M6.8.2 rotation for RECTANGLE/ELLIPSE (see the docstring above).
    # Appended last so every earlier positional construction keeps
    # working; 0.0 preserves the unrotated behavior of all existing
    # data. Point-based kinds ignore it.
    rotation_deg: float = 0.0

    def __post_init__(self) -> None:
        # Gentle normalization of stored/forwarded values. Never raises:
        # an unusable value falls back to the compatible default.
        object.__setattr__(self, "opacity", _clamp_unit(self.opacity, 1.0))
        object.__setattr__(
            self, "fill_opacity", _clamp_unit(self.fill_opacity, 1.0))
        object.__setattr__(
            self, "rotation_deg", _normalize_deg(self.rotation_deg))
        if self.line_style not in LINE_STYLES:
            object.__setattr__(self, "line_style", "solid")
        if self.start_ending not in LINE_ENDINGS:
            object.__setattr__(self, "start_ending", "none")
        if self.end_ending not in LINE_ENDINGS:
            object.__setattr__(self, "end_ending", "none")
        if not isinstance(self.points, tuple):
            try:
                object.__setattr__(self, "points", tuple(self.points))
            except Exception:
                object.__setattr__(self, "points", ())



@dataclass(frozen=True, slots=True)
class StickyNote(AnnotationBase):
    """Text note pinned at a document-space position."""

    position: Point = field(default_factory=lambda: Point(0.0, 0.0))
    text: str = ""
    color: str = "#FFFF88"
