"""Canvas interaction routing (M5-A1). Qt-free architectural seam.

Flow: PdfViewer event -> InteractionController -> active tool handler ->
intent dict -> viewer applies (scroll/zoom/update only).

Handlers never import Qt, fitz, or rendering internals. Intents are
plain data: {"pan_by": (dx, dy)}, {"zoom_step": +1|-1}, {"select": ...},
{"noop": True}. M5-A1 implements Pan functionally; every other tool
routes to a recording stub until its milestone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from annotations.ink_geometry import (
    DEFAULT_PRESSURE_PROFILE,
    PRESSURE_PROFILES,
    LiveCenterline,
    LiveOutline,
    pressure_width,
)
from annotations.models import Highlighter, InkStroke, Shape, ShapeKind
from core.types import PageId, Point, Rect, Size
from tools.models import ToolInstance, ToolType

__all__ = [
    "CollapseStage",
    "EraserHandler",
    "EraserSegment",
    "GestureEvent",
    "HighlighterHandler",
    "InputStage",
    "InteractionController",
    "LiveHighlighter",
    "LiveStroke",
    "PanHandler",
    "PenHandler",
    "PolygonPreview",
    "ShapeHandler",
    "ShapeSnapContext",
    "SpeedAdaptiveStage",
    "StubHandler",
    "snap_hit",
]

#: Tool kinds captured by :class:`ShapeHandler` (M6.7).
SHAPE_TOOL_KINDS: tuple = (
    ToolType.LINE,
    ToolType.ARROW,
    ToolType.RECTANGLE,
    ToolType.ELLIPSE,
    ToolType.POLYGON,
)

#: ToolType -> canonical ShapeKind for the shape tools.
_TOOL_TO_SHAPE_KIND = {
    ToolType.LINE: ShapeKind.LINE,
    ToolType.ARROW: ShapeKind.ARROW,
    ToolType.RECTANGLE: ShapeKind.RECTANGLE,
    ToolType.ELLIPSE: ShapeKind.ELLIPSE,
    ToolType.POLYGON: ShapeKind.POLYGON,
}


def _box_rect(x0: float, y0: float, x1: float, y1: float) -> Rect:
    """Normalized (positive-extent) bounding rect of two corners."""
    left = x0 if x0 <= x1 else x1
    top = y0 if y0 <= y1 else y1
    return Rect(Point(left, top), Size(abs(x1 - x0), abs(y1 - y0)))


def _points_rect(points) -> Rect:
    """Normalized bounding rect of a point sequence (empty -> 0x0)."""
    if not points:
        return Rect(Point(0.0, 0.0), Size(0.0, 0.0))
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    left = min(xs)
    top = min(ys)
    return Rect(Point(left, top), Size(max(xs) - left, max(ys) - top))


def _snap_angle(ax: float, ay: float, x: float, y: float,
                step_deg: float = 15.0) -> tuple:
    """Snap the direction (ax,ay)->(x,y) to the nearest ``step_deg``.

    The distance is preserved; only the angle is quantized. Degenerate
    (zero-length) input is returned unchanged.
    """
    dx = x - ax
    dy = y - ay
    dist = math.hypot(dx, dy)
    if dist <= 0.0:
        return (x, y)
    step = math.radians(step_deg)
    angle = round(math.atan2(dy, dx) / step) * step
    return (ax + dist * math.cos(angle), ay + dist * math.sin(angle))


def _square_end(sx: float, sy: float, x: float, y: float) -> tuple:
    """Constrain (x,y) so the box from (sx,sy) is square (equal extents)."""
    dx = x - sx
    dy = y - sy
    side = max(abs(dx), abs(dy))
    nx = sx + (side if dx >= 0.0 else -side)
    ny = sy + (side if dy >= 0.0 else -side)
    return (nx, ny)


def _seg_intersection(x0: float, y0: float, x1: float, y1: float,
                      x2: float, y2: float, x3: float, y3: float):
    """Intersection of segments (0-1) and (2-3), or None.

    Both parameters must lie within their segment (``[0, 1]``); parallel
    or non-crossing segments yield None. Used by Connect Lines
    ("Snap to intersections").
    """
    d1x = x1 - x0
    d1y = y1 - y0
    d2x = x3 - x2
    d2y = y3 - y2
    denom = d1x * d2y - d1y * d2x
    if abs(denom) <= 1e-12:
        return None
    t = ((x2 - x0) * d2y - (y2 - y0) * d2x) / denom
    u = ((x2 - x0) * d1y - (y2 - y0) * d1x) / denom
    if t < 0.0 or t > 1.0 or u < 0.0 or u > 1.0:
        return None
    return (x0 + t * d1x, y0 + t * d1y)


@dataclass(frozen=True, slots=True)
class GestureEvent:
    """Pointer gesture in viewport pixels (viewer translates to doc space)."""

    x: float
    y: float
    buttons: int = 0
    pressure: float = 1.0


class PanHandler:
    """Drag-to-pan: converts pointer deltas into scrollbar intents."""

    def __init__(self) -> None:
        self._last: tuple[float, float] | None = None
        self.events: list[str] = []

    def down(self, event: GestureEvent) -> dict:
        self._last = (event.x, event.y)
        self.events.append("down")
        return {"noop": True}

    def move(self, event: GestureEvent) -> dict:
        if self._last is None:
            return {"noop": True}
        dx = self._last[0] - event.x
        dy = self._last[1] - event.y
        self._last = (event.x, event.y)
        self.events.append("move")
        if dx == 0 and dy == 0:
            return {"noop": True}
        return {"pan_by": (dx, dy)}

    def up(self, event: GestureEvent) -> dict:
        self._last = None
        self.events.append("up")
        return {"noop": True}

    def cancel(self) -> dict:
        self._last = None
        return {"noop": True}


class InputStage:
    """Positional input stage (B.3.2 experiment): raw in, derived out.

    Qt-free, causal, O(1) state. The base class is the identity: every
    accepted raw sample is emitted verbatim, preserving current
    behavior exactly. Subclasses (collapse / speed-adaptive filter) may
    drop an intermediate sample by returning None.

    Contract (see Plan v2):

    * Positional only — pressure/widths never enter the stage.
    * ``push`` is called iff ``_append_sample`` is called (pairing
      rule), with the already-clamped ``_times_ms`` value, so no
      double-feed is possible.
    * Post-``reset`` first push must emit (tap transparency).
    * ``final=True`` marks the ``up()``-append; collapse honors it by
      force-keeping the endpoint.
    * ``reset()`` clears all per-stroke state; the handler calls it at
      the same sites as the live-builder resets.
    """

    def reset(self) -> None:
        return None

    def push(self, x: float, y: float, t: int,
             *, final: bool = False) -> Point | None:
        return Point(float(x), float(y))


class CollapseStage(InputStage):
    """Drop intermediate samples within ``threshold_pt`` of last kept.

    Candidate A: purely spatial, timestamp-free. First sample (post
    reset) and ``final=True`` endpoints always emit, so taps,
    two-point strokes, and endpoints are preserved exactly (endpoint
    error 0 by construction). Non-finite input passes through
    verbatim so downstream validation/reset paths behave as today.
    """

    def __init__(self, threshold_pt: float = 0.3) -> None:
        try:
            d = float(threshold_pt)
        except (TypeError, ValueError):
            d = 0.3
        if not math.isfinite(d):
            d = 0.3
        self._d = max(0.0, d)
        self._kept: tuple[float, float] | None = None

    def reset(self) -> None:
        self._kept = None

    def push(self, x: float, y: float, t: int,
             *, final: bool = False) -> Point | None:
        px, py = float(x), float(y)
        if not (math.isfinite(px) and math.isfinite(py)):
            self._kept = (px, py)
            return Point(px, py)
        if (self._kept is None or final
                or math.hypot(px - self._kept[0],
                              py - self._kept[1]) >= self._d):
            self._kept = (px, py)
            return Point(px, py)
        return None


class SpeedAdaptiveStage(InputStage):
    """Light causal EMA whose gain rises with estimated speed.

    Candidate B: ``g = g_min + (g_max - g_min) * min(1, v / v_ref)``
    with ``v = dist / max(dt, 1)`` in pt/ms (positions are page-local
    pt, ``t`` is the handler-clamped integer-ms timestamp, hence
    non-decreasing and ``dt >= 0`` by construction).

    Safeguards (B.3.2 approval):

    * ``dt <= 0`` (repeated timestamps with moving coordinates) uses
      an effective ``dt`` of 1 ms — the sample stays live and
      near-transparent instead of freezing. Documented limitation:
      ``max(dt, 1)`` *underestimates* speed for true sub-millisecond
      intervals (integer-ms quantization); no device rate is assumed
      or invented to compensate.
    * Jumps larger than ``jump_pt`` re-initialize state (teleport =
      new sub-path, stays transparent for sparse inputs).
    * Non-finite input or output resets state and passes the raw
      sample through, mirroring the existing builder-reset behavior.
    * ``final`` is intentionally not special-cased: the endpoint
      goes through the normal update and any lag is measured (a
      snap-to-raw would hide lag as a terminal hook).

    O(1) state ``(sx, sy, t)``; synchronous; no future samples.
    """

    def __init__(self, g_min: float = 0.35, g_max: float = 1.0,
                 v_ref: float = 0.1, jump_pt: float = 25.0) -> None:
        glo = float(g_min)
        ghi = float(g_max)
        if not (math.isfinite(glo) and math.isfinite(ghi)):
            raise ValueError("gains must be finite")
        if not (0.0 <= glo <= 1.0 and 0.0 <= ghi <= 1.0):
            raise ValueError("gains must lie in [0, 1]")
        if glo > ghi:
            raise ValueError("g_min must not exceed g_max")
        vr = float(v_ref)
        if not (math.isfinite(vr) and vr > 0.0):
            raise ValueError("v_ref must be finite and positive")
        jp = float(jump_pt)
        if not (math.isfinite(jp) and jp > 0.0):
            raise ValueError("jump_pt must be finite and positive")
        self._gmin = glo
        self._gmax = ghi
        self._vref = vr
        self._jump = jp
        self._s: tuple[float, float] | None = None
        self._t: int | None = None

    def reset(self) -> None:
        self._s = None
        self._t = None

    def push(self, x: float, y: float, t: int,
             *, final: bool = False) -> Point | None:
        px, py = float(x), float(y)
        if not (math.isfinite(px) and math.isfinite(py)):
            self.reset()
            return Point(px, py)
        try:
            ti = int(t)
        except (TypeError, ValueError):
            ti = (self._t + 1) if self._t is not None else 0
        if self._s is None or self._t is None:
            self._s = (px, py)
            self._t = ti
            return Point(px, py)
        dt = ti - self._t
        deff = dt if dt > 0 else 1
        sx, sy = self._s
        dist = math.hypot(px - sx, py - sy)
        if dist > self._jump:
            self._s = (px, py)
            self._t = ti
            return Point(px, py)
        frac = min(1.0, (dist / deff) / self._vref)
        g = self._gmin + (self._gmax - self._gmin) * frac
        nx = sx + g * (px - sx)
        ny = sy + g * (py - sy)
        if not (math.isfinite(nx) and math.isfinite(ny)):
            self.reset()
            return Point(px, py)
        self._s = (nx, ny)
        self._t = ti
        return Point(nx, ny)


class LiveStroke:
    """Mutable per-stroke live view (B.3.5): O(1) per accepted sample.

    Emitted as ``intent["stroke_update"]`` by ``down()``/``move()`` so
    the live path no longer rebuilds full ``InkStroke`` tuples on
    every move (O(N^2) per stroke). Append-only ``points``/``pressures``
    /``times_ms`` mirror the accepted raw samples (O(N) total);
    metadata follows the same latch rules as ``_stroke()`` (pressure
    flag/profile latched at press, color/width/opacity live via
    ``sync_meta`` on mid-stroke reconfiguration). ``seq`` counts
    accepted samples, making the exactly-once pairing observable.

    Never hashed by value, never cached, never completed: ``up()``
    builds the single immutable ``InkStroke`` via ``_stroke()`` and
    the view is discarded. Identity is stable per stroke by design;
    viewer memos keyed on it are invalidated per update (see
    ``_apply_intent``), and ``Point`` elements are frozen, so sharing
    references with the raw/completed tuples is alias-safe.
    """

    __slots__ = ("page_id", "points", "pressures", "times_ms", "color",
                 "width_mm", "opacity", "pressure_enabled",
                 "pressure_profile", "seq", "__weakref__")

    def __init__(self, page_id, color, width_mm, opacity,
                 pressure_enabled, pressure_profile) -> None:
        self.page_id = page_id
        self.points: list = []
        self.pressures: list = []
        self.times_ms: list = []
        self.color = color
        self.width_mm = width_mm
        self.opacity = opacity
        self.pressure_enabled = pressure_enabled
        self.pressure_profile = pressure_profile
        self.seq = 0

    def append(self, point, pressure, now_ms) -> None:
        self.points.append(point)
        self.pressures.append(pressure)
        self.times_ms.append(now_ms)
        self.seq += 1

    def sync_meta(self, color, width_mm, opacity) -> None:
        self.color = color
        self.width_mm = width_mm
        self.opacity = opacity


class PenHandler:
    """Freehand pen capture (M5-B.1): press/move/release build InkStrokes.

    Qt-free: coordinates are page-local rotated PDF points supplied by
    the viewer (it owns layout/scroll/zoom mapping). Configuration is
    snapshotted from the active tool at press time.
    """

    #: Consecutive points closer than this (pt) are duplicates.
    _EPSILON = 1e-9

    def __init__(self, kind: ToolType = ToolType.PEN,
                 input_stage: InputStage | None = None) -> None:
        self.kind = kind
        self.events: list[str] = []
        self._page_id: PageId | None = None
        self._points: list[Point] = []
        # M5-B.2.0: authoritative raw per-sample capture, parallel to
        # _points (appended/discarded together). Empty while no stroke
        # is in progress or when the device provides no data.
        self._pressures: list[float] = []
        self._times_ms: list[int] = []
        self._color = "#000000"
        self._width_mm = 0.5
        self._opacity = 1.0
        # M5-B.2.1 Slice 3: press-time latch. configure() runs on every
        # press/move/release (via _pen_handler), so it only stages the
        # pending value; down() latches it for the new stroke and
        # _stroke() emits the latched value. Mid-stroke reconfiguration
        # can never restyle the active stroke.
        self._pending_pressure_enabled = False
        self._stroke_pressure_enabled = False
        # B.2.3: same latch mechanics for the response profile name.
        self._pending_profile = DEFAULT_PRESSURE_PROFILE
        self._stroke_profile = DEFAULT_PRESSURE_PROFILE
        # B.2.3: append-only incremental outline for the live stroke.
        # Fed mapped widths in parallel with _points; reset on
        # down/cancel/up. Never affects stored raw samples.
        self._live = LiveOutline()
        # B.2.10: widthless incremental centerline for the live
        # stroke. Fed every accepted raw sample regardless of the
        # pressure latch (widthless — latch-independent); serves the
        # Pressure-OFF paint path. Reset on down/cancel/up, like
        # _live. Never affects stored raw samples.
        self._center = LiveCenterline()
        # B.3.2: positional input stage. Identity by default (current
        # behavior exactly). An experiment stage translates accepted
        # raw samples into derived positions for the live builders;
        # raw storage, pressures, timestamps, and _stroke() output are
        # unaffected. ``_fed`` counts successful centerline feeds so
        # the divergence guard below stays exact when a stage drops
        # intermediate samples (identity: fed == len(_points) always).
        self._stage: InputStage = (
            input_stage if input_stage is not None else InputStage())
        self._fed = 0
        # B.3.5: mutable live view emitted per move (O(1)); the full
        # immutable InkStroke is built once at release via _stroke().
        # None exactly when no stroke is open.
        self._view: LiveStroke | None = None

    @staticmethod
    def _sanitize_profile(profile) -> str:
        """Normalize a profile name; unknown/empty → default (no raise)."""
        try:
            name = str(profile)
        except Exception:
            return DEFAULT_PRESSURE_PROFILE
        return name if name in PRESSURE_PROFILES else DEFAULT_PRESSURE_PROFILE

    def configure(self, tool: ToolInstance | None,
                  pressure_override=None, profile_override=None) -> None:
        """Refresh the config snapshot (press-time contract).

        ``pressure_override`` (None = no opinion) is OR-ed with the
        tool's own pressure flag for the pending value; the latch in
        ``down()`` still decides the stroke. Resolver failures must be
        absorbed by the caller into None (slot flag only).
        ``profile_override`` (None = keep slot-agnostic default) stages
        the pending response profile, sanitized; latched in ``down()``.
        A mid-stroke base-width change refeeds the live builder so the
        preview matches the finalized stroke; profile stays latched.
        """
        try:
            if tool is not None:
                old_width = self._width_mm
                self._color = str(tool.color)
                self._width_mm = float(tool.width_mm)
                self._opacity = max(0.0, min(1.0, float(tool.opacity)))
                slot_flag = bool(getattr(tool, "pressure", False))
                if pressure_override is None:
                    self._pending_pressure_enabled = slot_flag
                else:
                    self._pending_pressure_enabled = bool(
                        slot_flag or pressure_override)
                if profile_override is not None:
                    self._pending_profile = self._sanitize_profile(
                        profile_override)
                if self._points and old_width != self._width_mm:
                    self._refeed_live()
                # B.3.5: keep the live view's unlatched metadata in
                # step with the snapshot (flag/profile stay latched,
                # exactly like _stroke()).
                if self._points and self._view is not None:
                    self._view.sync_meta(self._color, self._width_mm,
                                         self._opacity)
        except Exception:
            pass

    def _stroke(self) -> InkStroke:
        return InkStroke(
            page_id=self._page_id if self._page_id is not None else PageId(),
            points=tuple(self._points),
            color=self._color,
            width_mm=self._width_mm,
            opacity=self._opacity,
            pressures=tuple(self._pressures),
            times_ms=tuple(self._times_ms),
            pressure_enabled=self._stroke_pressure_enabled,
            pressure_profile=self._stroke_profile,
        )

    @staticmethod
    def _clamp_pressure(pressure: float) -> float:
        """Normalize stylus pressure into 0..1 (never lose ink)."""
        try:
            value = float(pressure)
        except (TypeError, ValueError):
            return 1.0
        if value != value:  # NaN: device gave no usable reading
            return 1.0
        return max(0.0, min(1.0, value))

    @staticmethod
    def _clamp_time(now_ms: int, prev_ms: int | None) -> int:
        """Force a non-decreasing integer timestamp (ms)."""
        try:
            value = int(now_ms)
        except (TypeError, ValueError):
            value = prev_ms + 1 if prev_ms is not None else 0
        if prev_ms is not None and value <= prev_ms:
            value = prev_ms + 1
        return value

    def _feed_live(self, x: float, y: float) -> None:
        """Feed the just-appended sample to the live builder.

        Widths are mapped with the CURRENT base width but the LATCHED
        profile — exactly what the full rebuild uses at ``_stroke()``
        time. Invalid base widths reset the builder so painting falls
        back to the legacy path, mirroring ``_pressure_outline``.
        B.2.7: skip entirely when the latched stroke has pressure
        rendering off — ``live_outline()`` returns None in that case
        regardless, so feeding would only burn per-sample CPU for a
        builder nobody reads. Observable behavior unchanged.
        """
        if not self._stroke_pressure_enabled:
            return
        try:
            base = float(self._width_mm) * 72.0 / 25.4
            if not (math.isfinite(base) and base > 0.0):
                raise ValueError(f"bad base width {base!r}")
            self._live.append(
                float(x), float(y),
                pressure_width(self._pressures[-1], base,
                               self._stroke_profile))
        except (ValueError, IndexError):
            try:
                self._live.reset()
            except Exception:
                pass

    def _refeed_live(self) -> None:
        """Re-map all open samples after a mid-stroke width edit.

        B.2.7: with pressure rendering latched off there is nothing to
        re-map (``live_outline()`` stays None); just reset the builder
        so it holds no stale state.
        """
        if not self._stroke_pressure_enabled:
            try:
                self._live.reset()
            except Exception:
                pass
            return
        try:
            base = float(self._width_mm) * 72.0 / 25.4
            if not (math.isfinite(base) and base > 0.0):
                raise ValueError(f"bad base width {base!r}")
            self._live.reset()
            for pt, p in zip(self._points, self._pressures):
                self._live.append(
                    pt.x, pt.y,
                    pressure_width(p, base, self._stroke_profile))
        except (ValueError, IndexError):
            try:
                self._live.reset()
            except Exception:
                pass

    def _feed_center(self, x: float, y: float) -> bool:
        """Feed the just-appended sample to the widthless holder.

        B.2.10: unconditional (pressure latch plays no role — the
        holder carries no widths). Invalid coordinates reset the
        holder so painting falls back to the full-derive path,
        mirroring ``_feed_live``. Observable behavior unchanged.

        B.3.2: returns True iff the holder accepted the sample, so
        the caller can keep the ``_fed`` divergence counter exact.
        """
        try:
            self._center.append(float(x), float(y))
        except ValueError:
            try:
                self._center.reset()
            except Exception:
                pass
            return False
        return True

    def live_outline(self):
        """Incremental outline for the active stroke, or None.

        None when pressure rendering is off or no samples are open.
        Only documented geometry errors are absorbed here; unexpected
        failures propagate to the caller's guards.
        """
        try:
            if not self._stroke_pressure_enabled:
                return None
            if len(self._live) == 0:
                return None
            return self._live.outline()
        except ValueError:
            return None

    def live_centerline(self):
        """Incremental widthless centerline, or None when no stroke.

        B.2.10: exact-match with ``smooth_centerline(raw, None)`` at
        every prefix. Latch-independent (no widths involved). Only
        documented geometry errors are absorbed here; unexpected
        failures propagate to the caller's guards.

        B.3.2: the guard compares against the ``_fed`` feed counter
        instead of the raw point count, so a stage that drops
        intermediate samples keeps serving its derived prefix. With
        the default identity stage ``_fed == len(_points)`` always,
        hence behavior is unchanged.
        """
        try:
            if not self._points:
                return None
            if len(self._center) != self._fed:
                # Feed/append diverged (should be impossible through
                # down/move); fall back to the full derive rather than
                # serve a mismatched prefix.
                return None
            return self._center.smoothed()
        except ValueError:
            return None

    def _append_sample(self, x: float, y: float,
                       pressure: float, now_ms: int) -> None:
        self._points.append(Point(float(x), float(y)))
        self._pressures.append(self._clamp_pressure(pressure))
        prev = self._times_ms[-1] if self._times_ms else None
        self._times_ms.append(self._clamp_time(now_ms, prev))

    def _push_filtered(self, x: float, y: float, *, final: bool = False) -> None:
        """Push one accepted sample through the stage to live builders.

        B.3.2: called iff ``_append_sample`` was called (pairing
        rule), with the just-clamped timestamp. A ``None`` return
        (collapse drop) feeds nothing; ``_fed`` counts successful
        centerline feeds only.
        """
        f = self._stage.push(float(x), float(y), self._times_ms[-1],
                             final=final)
        if f is None:
            return
        self._feed_live(f.x, f.y)
        if self._feed_center(f.x, f.y):
            self._fed += 1

    @staticmethod
    def _same(a: Point, b: Point) -> bool:
        try:
            return (abs(a.x - b.x) < PenHandler._EPSILON
                    and abs(a.y - b.y) < PenHandler._EPSILON)
        except Exception:
            return False

    def down(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0) -> dict:
        self._page_id = page_id
        self._points = []
        self._pressures = []
        self._times_ms = []
        # B.2.6: bound the diagnostic event history at the stroke
        # boundary (it is append-only otherwise, forever, on the
        # cached per-slot handler). Nothing consumes cross-stroke
        # history — tests only count "down"s within a stroke and the
        # application never reads it — so a per-stroke log preserves
        # every current use while staying O(stroke).
        self.events = []
        self._stroke_pressure_enabled = self._pending_pressure_enabled
        self._stroke_profile = self._pending_profile
        self._live.reset()
        self._center.reset()
        self._stage.reset()
        self._fed = 0
        self._view = LiveStroke(page_id, self._color, self._width_mm,
                                self._opacity,
                                self._stroke_pressure_enabled,
                                self._stroke_profile)
        self._append_sample(x, y, pressure, now_ms)
        self._view.append(self._points[-1], self._pressures[-1],
                          self._times_ms[-1])
        self._push_filtered(x, y)
        self.events.append("down")
        return {"stroke_update": self._view}

    def move(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0) -> dict:
        if not self._points or self._page_id is None:
            return {"noop": True}
        if page_id != self._page_id:
            return {"noop": True}
        point = Point(float(x), float(y))
        if self._same(point, self._points[-1]):
            return {"noop": True}
        self._append_sample(x, y, pressure, now_ms)
        if self._view is not None:
            self._view.append(self._points[-1], self._pressures[-1],
                              self._times_ms[-1])
        self._push_filtered(x, y)
        self.events.append("move")
        return {"stroke_update": self._view}

    def up(self, page_id: PageId, x: float, y: float,
           pressure: float = 1.0, now_ms: int = 0) -> dict:
        if not self._points or self._page_id is None:
            return {"noop": True}
        if page_id == self._page_id:
            point = Point(float(x), float(y))
            if not self._same(point, self._points[-1]):
                self._append_sample(x, y, pressure, now_ms)
                if self._view is not None:
                    self._view.append(self._points[-1],
                                      self._pressures[-1],
                                      self._times_ms[-1])
                self._push_filtered(x, y, final=True)
        stroke = self._stroke()
        self._page_id = None
        self._points = []
        self._pressures = []
        self._times_ms = []
        # Final rendering uses the full rebuild (one-time cost); the
        # live builder resets for the next stroke.
        self._live.reset()
        self._center.reset()
        self._stage.reset()
        self._fed = 0
        self._view = None
        self.events.append("up")
        return {"stroke_done": stroke}

    def cancel(self) -> dict:
        self._page_id = None
        self._points = []
        self._pressures = []
        self._times_ms = []
        self._live.reset()
        self._center.reset()
        self._stage.reset()
        self._fed = 0
        self._view = None
        self.events.append("cancel")
        return {"stroke_cancelled": True}


class LiveHighlighter:
    """Mutable per-stroke live view for the free-draw highlighter (M6.3).

    Emitted as ``intent["highlight_update"]`` by ``down()``/``move()`` so
    the preview paints from an append-only point list instead of
    rebuilding tuples per event. The canonical :class:`Highlighter`
    annotation is built once at release; this view is then discarded.
    Identity is stable per stroke, so the viewer's single-slot live
    invalidation rect stays keyed to one object. Points are frozen
    ``Point`` values, so sharing references with the completed tuple is
    alias-safe.

    M7.1.4: ``pressures`` grows in lockstep with ``points`` (normalized
    stylus pressure per sample) so the live band can vary in width
    exactly like the committed one. The list is reused as the committed
    tuple at release, so live and committed geometry agree.
    """

    __slots__ = ("page_id", "points", "pressures", "color", "width_mm",
                 "opacity", "seq", "__weakref__")

    def __init__(self, page_id, color, width_mm, opacity) -> None:
        self.page_id = page_id
        self.points: list = []
        self.pressures: list = []
        self.color = color
        self.width_mm = width_mm
        self.opacity = opacity
        self.seq = 0

    def append(self, point, pressure: float = 1.0) -> None:
        self.points.append(point)
        self.pressures.append(pressure)
        self.seq += 1

    def set_pressures(self, pressures) -> None:
        """Replace the resolved pressures in lockstep with ``points``.

        M7.1.4.2: leading unavailable readings are reconciled against the
        first genuine sample, so already-appended samples can gain a
        corrected width. The list object is mutated in place — the viewer
        keeps rendering this same live view, exactly like ``points``.
        """
        self.pressures[:] = [float(p) for p in pressures]

    def sync_meta(self, color, width_mm, opacity) -> None:
        self.color = color
        self.width_mm = width_mm
        self.opacity = opacity


class HighlighterHandler:
    """Free-draw highlighter capture (M6.3, pressure M7.1.4).

    Qt-free: coordinates are page-local rotated PDF points supplied by
    the viewer (it owns layout/scroll/zoom mapping). The highlighter is a
    translucent rectangular band whose width follows the same normalized
    stylus pressure curve as the pen; the raw pressure per sample is
    captured alongside the points and the width geometry is derived at
    paint/hit-test time. A genuine numeric zero is kept as valid pressure
    evidence; an *unavailable* reading is reconciled against the stroke's
    neighbouring valid samples (a stroke with no usable reading at all
    falls back to ``1.0``, preserving constant-width mouse/legacy
    behavior). Rendering applies the shared centerline smoothing at paint
    time, so
    the canonical annotation stores the raw captured samples — editable
    geometry a later persistence milestone can save and reconstruct.
    Configuration is snapshotted from the active tool at press time.

    Intents are distinct from the pen's (``highlight_update`` /
    ``highlight_done`` / ``highlight_cancelled``) so the viewer can never
    confuse a highlighter with a legacy pen stroke.
    """

    #: Consecutive points closer than this (pt) are duplicates.
    _EPSILON = 1e-9

    def __init__(self) -> None:
        self.kind = ToolType.HIGHLIGHTER
        self.events: list[str] = []
        self._page_id: PageId | None = None
        self._points: list[Point] = []
        self._pressures: list[float] = []
        self._color = "#FFFF00"
        self._width_mm = 3.0
        self._opacity = 0.4
        self._view: LiveHighlighter | None = None

    def configure(self, tool: ToolInstance | None) -> None:
        """Refresh the config snapshot (press-time contract)."""
        try:
            if tool is not None:
                self._color = str(tool.color)
                self._width_mm = float(tool.width_mm)
                self._opacity = max(0.0, min(1.0, float(tool.opacity)))
                # Live unlatched metadata tracks the snapshot exactly
                # like the pen's LiveStroke.sync_meta.
                if self._points and self._view is not None:
                    self._view.sync_meta(self._color, self._width_mm,
                                         self._opacity)
        except Exception:
            pass

    @staticmethod
    def _same(a: Point, b: Point) -> bool:
        try:
            return (abs(a.x - b.x) < HighlighterHandler._EPSILON
                    and abs(a.y - b.y) < HighlighterHandler._EPSILON)
        except Exception:
            return False

    @staticmethod
    def _normalize_pressure(pressure):
        """Normalize one stylus reading; ``None`` means *unavailable*.

        M7.1.4.2: a genuine numeric zero is a valid sensor value and is
        preserved (the response curve's floor keeps the band visible).
        Unreadable or non-finite readings carry no evidence at all and
        stay ``None`` until neighbouring valid samples resolve them —
        they must never silently become full pressure (the previous
        ``<= 0 -> 1.0`` rule produced the full-width stroke start).
        """
        if pressure is None:
            return None
        try:
            value = float(pressure)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        return max(0.0, min(1.0, value))

    @staticmethod
    def _resolve_pressures(values):
        """Resolve raw samples (float or ``None``) into paint-safe floats.

        Per-stroke evidence policy (M7.1.4.2):

        * leading unavailable samples adopt the *first* genuine reading —
          the first valid sample reconciles the stroke start, so a press
          event without a usable reading can never introduce an
          artificial full-width tip;
        * interior/trailing unavailable runs interpolate between their
          bracketing valid readings (nearest valid when only one side
          exists), keeping width changes continuous;
        * a stroke with no usable reading at all (a sensor-less device or
          an input source without pressure) falls back to the nominal
          ``1.0``, preserving the established constant-width behavior.

        Genuine zeros are valid readings and are never rewritten.
        """
        raw = list(values or ())
        n = len(raw)
        if n == 0:
            return []
        valid = [i for i, value in enumerate(raw) if value is not None]
        if not valid:
            return [1.0] * n
        out = [0.0] * n
        for index in valid:
            out[index] = float(raw[index])
        first, last = valid[0], valid[-1]
        for index in range(first):
            out[index] = out[first]
        for index in range(last + 1, n):
            out[index] = out[last]
        for left, right in zip(valid, valid[1:]):
            span = right - left
            if span <= 1:
                continue
            start, end = out[left], out[right]
            for step in range(1, span):
                t = step / float(span)
                out[left + step] = start + (end - start) * t
        return out

    def _resolved(self):
        """Resolved pressure values for the samples captured so far."""
        return self._resolve_pressures(self._pressures)

    def _stroke(self) -> Highlighter:
        return Highlighter(
            page_id=self._page_id if self._page_id is not None else PageId(),
            points=tuple(self._points),
            color=self._color,
            opacity=self._opacity,
            width_mm=self._width_mm,
            pressures=tuple(self._resolved()),
        )

    def down(self, page_id: PageId, x: float, y: float,
             pressure=None, now_ms: int = 0) -> dict:
        self._page_id = page_id
        self._points = [Point(float(x), float(y))]
        self._pressures = [self._normalize_pressure(pressure)]
        self.events = []
        self._view = LiveHighlighter(page_id, self._color, self._width_mm,
                                     self._opacity)
        resolved = self._resolved()
        self._view.append(self._points[-1], resolved[-1])
        self._view.set_pressures(resolved)
        self.events.append("down")
        return {"highlight_update": self._view}

    def move(self, page_id: PageId, x: float, y: float,
             pressure=None, now_ms: int = 0) -> dict:
        if not self._points or self._page_id is None:
            return {"noop": True}
        if page_id != self._page_id:
            return {"noop": True}
        point = Point(float(x), float(y))
        if self._same(point, self._points[-1]):
            return {"noop": True}
        self._pressures.append(self._normalize_pressure(pressure))
        self._points.append(point)
        resolved = self._resolved()
        if self._view is not None:
            self._view.append(point, resolved[-1])
            self._view.set_pressures(resolved)
        self.events.append("move")
        return {"highlight_update": self._view}

    def up(self, page_id: PageId, x: float, y: float,
           pressure=None, now_ms: int = 0) -> dict:
        if not self._points or self._page_id is None:
            return {"noop": True}
        if page_id == self._page_id:
            point = Point(float(x), float(y))
            if not self._same(point, self._points[-1]):
                self._pressures.append(self._normalize_pressure(pressure))
                self._points.append(point)
                resolved = self._resolved()
                if self._view is not None:
                    self._view.append(point, resolved[-1])
                    self._view.set_pressures(resolved)
        stroke = self._stroke()
        self._page_id = None
        self._points = []
        self._pressures = []
        self._view = None
        self.events.append("up")
        return {"highlight_done": stroke}

    def cancel(self) -> dict:
        self._page_id = None
        self._points = []
        self._pressures = []
        self._view = None
        self.events.append("cancel")
        return {"highlight_cancelled": True}


@dataclass(frozen=True, slots=True)
class EraserSegment:
    """One eraser path sample in page-local rotated PDF points (M6.5).

    ``(x0, y0)-(x1, y1)`` is a single segment of the eraser's drag on
    one page (``x0 == x1 and y0 == y1`` is a point sample). The segment
    never spans pages: a page change starts a fresh sub-path.
    """

    page_id: PageId
    x0: float
    y0: float
    x1: float
    y1: float


class EraserHandler:
    """Stroke-level eraser gesture capture (M6.5).

    Qt-free: like the pen and highlighter it receives page-local
    rotated PDF points supplied by the viewer (it owns layout/scroll/
    zoom mapping) and reports the eraser's path as ``EraserSegment``
    intents. The handler performs no hit-testing and touches no store or
    history: the viewer, which owns the document's
    :class:`~annotations.store.AnnotationStore`, resolves which
    committed annotations each segment intersects and removes them
    through the existing delete-command infrastructure.

    A press only *starts* the gesture (it never creates a delete
    command by itself); movement — including the final drag to the
    release point — is what is hit-tested. A page change starts a fresh
    sub-path so segments are never bridged across pages.
    """

    def __init__(self) -> None:
        self.kind = ToolType.ERASER
        self.events: list[str] = []
        self._page_id: PageId | None = None
        self._last: tuple[float, float] | None = None

    def down(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0) -> dict:
        self._page_id = page_id
        self._last = (float(x), float(y))
        self.events = ["down"]
        return {"eraser_begin": True}

    def move(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0) -> dict:
        if self._page_id is None or self._last is None:
            return {"noop": True}
        x1, y1 = float(x), float(y)
        if page_id != self._page_id:
            # A different page starts a fresh sub-path: never bridge a
            # segment across the page boundary.
            self._page_id = page_id
            self._last = (x1, y1)
            self.events.append("move")
            return {"eraser_update": EraserSegment(page_id, x1, y1, x1, y1)}
        x0, y0 = self._last
        if x0 == x1 and y0 == y1:
            return {"noop": True}
        self._last = (x1, y1)
        self.events.append("move")
        return {"eraser_update": EraserSegment(self._page_id, x0, y0,
                                               x1, y1)}

    def up(self, page_id: PageId, x: float, y: float,
           pressure: float = 1.0, now_ms: int = 0) -> dict:
        segment = None
        if (self._page_id is not None and self._last is not None
                and page_id == self._page_id):
            x0, y0 = self._last
            x1, y1 = float(x), float(y)
            if x0 != x1 or y0 != y1:
                segment = EraserSegment(self._page_id, x0, y0, x1, y1)
        self._page_id = None
        self._last = None
        self.events.append("up")
        if segment is not None:
            return {"eraser_update": segment, "eraser_done": True}
        return {"eraser_done": True}

    def cancel(self) -> dict:
        self._page_id = None
        self._last = None
        self.events.append("cancel")
        return {"eraser_cancelled": True}


@dataclass(frozen=True, slots=True)
class ShapeSnapContext:
    """Candidate snap geometry for the active page (M6.7 Connect Lines).

    The viewer gathers this from the *committed* shapes on the page and
    hands it to the handler at gesture start (the handler never touches
    the store). ``endpoints`` are existing Line/Arrow endpoints and
    Polygon vertices; ``segments`` are existing straight edges.
    ``tol_pt`` is the snap radius in page-local points, derived by the
    viewer from a screen distance so snapping stays zoom-correct. An
    empty context (``tol_pt == 0``) disables snapping entirely.

    M6.7.2: ``pending_endpoints`` / ``pending_segments`` carry the
    *transient* candidates of an in-progress Polygon — its **confirmed**
    vertices and confirmed edges only. The viewer gathers these from the
    live handler (the unfinished polygon is not in the store yet). They
    are resolved identically to committed geometry; the provisional edge,
    the unconfirmed pointer and the fill contour are never included, so
    the polygon can never snap to itself. Empty when no polygon gesture
    is active.
    """

    endpoints: tuple = ()
    segments: tuple = ()
    tol_pt: float = 0.0
    pending_endpoints: tuple = ()
    pending_segments: tuple = ()


def snap_hit(context, x: float, y: float, *, anchor=None,
             endpoints: bool = False, intersections: bool = False):
    """Resolve a Connect-Lines target from ``context`` at ``(x, y)``.

    One shared target rule for every consumer: the shape tools resolve
    their press/move pointer through it, and the viewer's live line
    adjustment resolves the moving endpoint through it (M7.1 Phase B), so
    both always agree on what a connection target is and where it sits.

    ``endpoints`` checks the nearest exact endpoint inside ``tol_pt``
    (committed endpoints plus an in-progress Polygon's confirmed
    vertices). ``intersections`` checks where the ``anchor -> (x, y)``
    segment crosses a committed (or pending) segment and returns the
    nearest crossing inside the same tolerance. Endpoints win over
    intersections, matching the established handler order.

    Returns ``("endpoint"|"intersection", (x, y))`` or ``None``. The
    result is evidence for a transient indicator and for the caller's
    geometry — it is never stored by this module.
    """
    try:
        tol = float(getattr(context, "tol_pt", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if tol <= 0.0:
        return None
    try:
        endpoints_all = (tuple(getattr(context, "endpoints", ()) or ())
                         + tuple(getattr(context, "pending_endpoints", ())
                                 or ()))
        segments_all = (tuple(getattr(context, "segments", ()) or ())
                        + tuple(getattr(context, "pending_segments", ())
                                or ()))
    except Exception:
        return None
    if endpoints:
        best = None
        best_d = tol
        for p in endpoints_all:
            try:
                d = math.hypot(p.x - x, p.y - y)
            except Exception:
                continue
            if d <= best_d:
                best_d = d
                best = (p.x, p.y)
        if best is not None:
            return ("endpoint", best)
    if intersections:
        if anchor is None:
            return None
        try:
            ax, ay = float(anchor[0]), float(anchor[1])
        except (TypeError, IndexError, ValueError):
            return None
        best = None
        best_d = tol
        for seg in segments_all:
            try:
                (p1, p2) = seg
                ip = _seg_intersection(ax, ay, x, y,
                                       p1.x, p1.y, p2.x, p2.y)
            except Exception:
                ip = None
            if ip is None:
                continue
            d = math.hypot(ip[0] - x, ip[1] - y)
            if d <= best_d:
                best_d = d
                best = ip
        if best is not None:
            return ("intersection", best)
    return None


@dataclass(frozen=True, slots=True)
class PolygonPreview:
    """Transient, preview-only polygon construction feedback (M6.7.2).

    Emitted alongside the confirmed-edge preview :class:`Shape` while an
    in-progress Polygon gesture is live. It carries the geometry the
    painter needs for the *separately* rendered fill preview and the
    confirmed-vertex markers:

    * ``fill`` — confirmed vertices plus the current (unconfirmed)
      pointer position, used as an implicitly closed fill contour. It is
      never a stroked outline and never stored as a committed annotation.
    * ``vertices`` — the confirmed vertices only; the moving pointer is
      never included, so no marker is drawn for an unconfirmed position.
    * ``segment`` — the single *provisional* live edge from the last
      confirmed vertex to the pointer, or ``()`` when the pointer
      coincides with that vertex. It is drawn as its own stroke so it can
      never add a closing edge back to the first vertex and can never
      become a stored vertex.
    * ``snap_target`` / ``snap_kind`` — transient Connect-Lines feedback:
      when the pointer resolved to an eligible *existing* endpoint or
      intersection (not a plain 15° angular snap), ``snap_target`` is that
      page-local ``(x, y)`` and ``snap_kind`` is ``"endpoint"`` or
      ``"intersection"``. Empty/``""`` otherwise, and always empty while
      ``segment`` is empty (no live provisional edge). Paint-only: it never
      becomes a vertex and never enters the annotation store.
    * ``rect`` — the page-local bounding box of ``fill`` (which already
      covers the pointer), so the viewer can invalidate exactly the
      changed region.

    This object is Qt-free and never enters the annotation store.
    """

    page_id: PageId
    rect: Rect
    fill: tuple = ()
    vertices: tuple = ()
    segment: tuple = ()
    snap_target: tuple = ()
    snap_kind: str = ""


class ShapeHandler:
    """Straight-shape capture for the shape tools (M6.7). Qt-free.

    Coordinates are page-local rotated PDF points supplied by the viewer
    (it owns layout/scroll/zoom mapping and the document store). Two
    interaction families:

    * **Drag shapes** — Line, Arrow, Rectangle, Ellipse: press anchors a
      start point, movement adjusts the end point / opposite corner, and
      release commits exactly one :class:`~annotations.models.Shape`.
    * **Polygon** — each click/tap appends an ordered vertex; the shape
      is committed once when the pointer returns near the first vertex
      or on a double-click :meth:`finish`. A polygon gesture stays
      active across click/release cycles, so release never commits it.

    Drawing assists are applied *here*, so the live preview and the
    committed geometry are produced by the same code path and cannot
    disagree:

    * ``snap_angle`` — quantize the anchor->point direction to 15 deg
      (Line, Arrow, and each Polygon edge).
    * ``square`` / ``circle`` — constrain Rectangle/Ellipse to equal
      width and height.
    * ``connect_endpoints`` / ``connect_intersections`` — snap to a
      nearby existing endpoint / line crossing from the caller-supplied
      :class:`ShapeSnapContext`.

    Properties are latched at press time, so a mid-gesture property or
    preset change can never alter the shape being drawn. Degenerate
    shapes are never emitted (the intent is ``shape_cancelled``).
    """

    #: Consecutive points closer than this (pt) are duplicates.
    _EPS = 1e-9
    #: Minimum committed extent (pt) for a non-degenerate Line/Rect.
    _MIN_DIM_PT = 0.5
    #: Click-within radius (pt) that closes an in-progress polygon.
    _CLOSE_TOL_PT = 8.0

    def __init__(self) -> None:
        self.kind = ToolType.LINE
        self.events: list[str] = []
        self._page_id = None
        self._active = False
        self._start = None
        self._current = None
        self._vertices: list[Point] = []
        self._pointer = None
        # M6.7.2 follow-up: transient tag for the last Polygon pointer
        # resolution — ("endpoint"|"intersection", (x, y)) or None. Feeds
        # the paint-only snap indicator; never a vertex, never stored.
        self._pointer_snap = None
        self._snap = ShapeSnapContext()
        self._cfg = self._default_cfg()
        self._latched = dict(self._cfg)

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _default_cfg() -> dict:
        return {
            "color": "#000000", "width_mm": 0.5, "opacity": 1.0,
            "fill_enabled": False, "fill_color": "#ffffff",
            "fill_opacity": 1.0, "line_style": "solid",
            "start_ending": "none", "end_ending": "none",
            "snap_angle": False, "square": False, "circle": False,
            "connect_lines": False,
            "connect_endpoints": False, "connect_intersections": False,
            "close_path": False,
        }

    def configure(self, tool: ToolInstance | None) -> None:
        """Refresh the config snapshot (latched for real on press)."""
        try:
            if tool is None:
                return
            self.kind = tool.kind
            cfg = self._default_cfg()
            # M6.7.1: ONE canonical Connect Lines setting drives the full
            # auto-connect behavior; the endpoint/intersection flags are
            # internal split paths that always follow it (so they can
            # never disagree).
            connect = bool(getattr(tool, "connect_lines", False))
            # M6.7.2: Polygon has no Connect Lines toggle of its own. Its
            # endpoint/intersection Connect Lines behavior is derived from
            # the latched Snap-to-15° property instead: OFF => the
            # provisional edge and fill preview follow the raw pointer;
            # ON => the 15° angular constraint *and* Connect Lines
            # endpoint/intersection snapping are enabled together.
            # Polygon always closes regardless of any stored/legacy
            # close_path value.
            if self.kind == ToolType.POLYGON:
                connect = bool(getattr(tool, "snap_angle", False))
            close_path = (True if self.kind == ToolType.POLYGON
                          else bool(getattr(tool, "close_path", False)))
            cfg.update(
                color=str(tool.color),
                width_mm=float(tool.width_mm),
                opacity=max(0.0, min(1.0, float(tool.opacity))),
                fill_enabled=bool(tool.fill_enabled),
                fill_color=str(tool.fill_color),
                fill_opacity=max(0.0, min(1.0, float(tool.fill_opacity))),
                line_style=str(tool.line_style),
                start_ending=str(tool.start_ending),
                end_ending=str(tool.end_ending),
                snap_angle=bool(tool.snap_angle),
                square=bool(tool.square),
                circle=bool(tool.circle),
                connect_lines=connect,
                connect_endpoints=connect,
                connect_intersections=connect,
                close_path=close_path,
            )
            self._cfg = cfg
        except Exception:
            pass

    @property
    def active(self) -> bool:
        """True while a gesture (drag or polygon) is in progress."""
        return bool(self._active)

    # -- snapping --------------------------------------------------------

    def _anchor(self):
        """Fixed anchor of the current gesture, or None."""
        if self.kind == ToolType.POLYGON:
            if self._vertices:
                v = self._vertices[-1]
                return (v.x, v.y)
            return None
        return self._start

    def pending_snap_candidates(self):
        """Transient ``(endpoints, segments)`` for the live Polygon (M6.7.2).

        Offers the in-progress Polygon's **confirmed** vertices and
        confirmed edges as Connect-Lines candidates so a later vertex can
        snap back to the same unfinished polygon (closing onto the first
        vertex, or crossing a confirmed edge). The provisional edge, the
        unconfirmed pointer and the fill contour are never offered, and the
        live anchor (the provisional edge's own origin) and its incident
        edge are excluded too, so the current provisional edge can never be
        a snap target for itself. Empty (``(), ()``) unless a Polygon
        gesture is active.
        """
        if not (self._active and self.kind == ToolType.POLYGON):
            return (), ()
        verts = tuple(self._vertices)
        if len(verts) <= 1:
            # Only the provisional-edge anchor exists — never a target for
            # itself.
            return (), ()
        endpoints = verts[:-1]                    # confirmed, minus the anchor
        segments = tuple((verts[i], verts[i + 1])
                         for i in range(len(verts) - 2))  # minus the last edge
        return endpoints, segments

    def _snap_hit(self, x: float, y: float):
        """Return ``(kind, (x, y))`` for a Connect-Lines hit, or None.

        ``kind`` is ``"endpoint"`` or ``"intersection"``. Additive
        companion to :meth:`_snap_by_context` so the Polygon live preview
        can expose *why* the pointer resolved where it did (transient
        indicator state only; never stored). An eligible exact
        endpoint/intersection inside ``tol_pt`` yields a hit; the 15°
        angular constraint never does.

        Delegates to the shared :func:`snap_hit` resolver so the shape
        tools and the viewer's live recognized-line adjustment (M7.1
        Phase B) use exactly one target rule; the latched Connect-Lines
        flags still decide which target kinds this gesture may use.
        """
        return snap_hit(
            self._snap, x, y, anchor=self._anchor(),
            endpoints=bool(self._latched.get("connect_endpoints")),
            intersections=bool(self._latched.get("connect_intersections")))

    def _snap_by_context(self, x: float, y: float):
        """Return a snapped (x, y) from the Connect-Lines context, or None."""
        hit = self._snap_hit(x, y)
        return None if hit is None else hit[1]

    def _resolve_start(self, x: float, y: float) -> tuple:
        """Start point: only endpoint snapping applies (no direction yet)."""
        snapped = self._snap_by_context(x, y)
        return snapped if snapped is not None else (x, y)

    def _resolve_end(self, x: float, y: float) -> tuple:
        """Resolve an end point / vertex to its final coordinate.

        Polygon (M6.7.2): one deterministic policy feeds the provisional
        edge, the fill contour *and* the confirmed vertex, so the preview
        and the commit can never diverge — an eligible exact
        endpoint/intersection target within the Connect-Lines tolerance
        wins; otherwise the 15° angular constraint applies (when
        Snap-to-15° is on); otherwise the raw pointer is kept.

        Other tools keep the established "constraint first, Connect-Lines
        snap wins" order unchanged. The genuine hit (never the angular
        fallback) is recorded in ``self._pointer_snap`` for every kind so
        the Line/Arrow live preview can show the same snap indicator the
        Polygon uses.
        """
        if self.kind == ToolType.POLYGON:
            hit = self._snap_hit(x, y)
            # Transient feedback tag for the live preview indicator: only
            # an exact endpoint/intersection hit counts, never the 15°
            # angular fallback below.
            self._pointer_snap = hit
            if hit is not None:
                return hit[1]
            anchor = self._anchor()
            if anchor is not None and self._latched.get("snap_angle"):
                ax, ay = anchor
                return _snap_angle(ax, ay, x, y)
            return (x, y)
        anchor = self._anchor()
        if anchor is not None:
            ax, ay = anchor
            if self.kind == ToolType.RECTANGLE and self._latched.get("square"):
                x, y = _square_end(ax, ay, x, y)
            elif self.kind == ToolType.ELLIPSE and self._latched.get("circle"):
                x, y = _square_end(ax, ay, x, y)
            elif self._latched.get("snap_angle"):
                x, y = _snap_angle(ax, ay, x, y)
        # Record the genuine endpoint/intersection hit — never the angular
        # constraint above — so the live preview can flag it.
        self._pointer_snap = self._snap_hit(x, y)
        if self._pointer_snap is not None:
            x, y = self._pointer_snap[1]
        return (x, y)

    # -- shape construction ---------------------------------------------

    def _make_shape(self, points, rect: Rect,
                    close_path: bool | None = None) -> Shape:
        cfg = self._latched
        return Shape(
            kind=_TOOL_TO_SHAPE_KIND.get(self.kind, ShapeKind.LINE),
            rect=rect,
            color=cfg["color"],
            width_mm=cfg["width_mm"],
            opacity=cfg["opacity"],
            fill_enabled=cfg["fill_enabled"],
            fill_color=cfg["fill_color"],
            fill_opacity=cfg["fill_opacity"],
            line_style=cfg["line_style"],
            start_ending=cfg["start_ending"],
            end_ending=cfg["end_ending"],
            points=tuple(points or ()),
            close_path=(cfg.get("close_path", False) if close_path is None
                        else bool(close_path)),
            page_id=(self._page_id if self._page_id is not None
                     else PageId()),
        )

    def _preview_shape(self) -> Shape:
        if self.kind == ToolType.POLYGON:
            # M6.7.1/M6.7.2: the stroked outline shows ONLY the edges the
            # user has already confirmed (A->B, B->C, ...). It never
            # appends the moving pointer and never closes the path early;
            # the closing edge is added exactly once at completion. The
            # live fill contour, the provisional pointer edge, and the
            # confirmed-vertex markers travel separately (see
            # :meth:`_polygon_preview`) so they cannot add a stroke here.
            pts = tuple(self._vertices)
            return self._make_shape(pts, _points_rect(pts),
                                    close_path=False)
        sx, sy = self._start if self._start is not None else (0.0, 0.0)
        cx, cy = self._current if self._current is not None else (sx, sy)
        if self.kind in (ToolType.LINE, ToolType.ARROW):
            pts = (Point(sx, sy), Point(cx, cy))
            return self._make_shape(pts, _points_rect(pts))
        return self._make_shape((), _box_rect(sx, sy, cx, cy))

    def _resolved_snap(self):
        """Transient ``(x, y, kind)`` for a drag preview, or ``None``.

        Only a genuine endpoint/intersection hit counts *and* only when
        that resolved target is the visible end point actually being
        previewed — never a bare 15° angular move. Feeds the paint-only
        snap indicator; never stored.
        """
        hit = self._pointer_snap
        cur = self._current
        if hit is None or cur is None:
            return None
        kind, target = hit
        try:
            if (abs(float(cur[0]) - float(target[0])) <= self._EPS
                    and abs(float(cur[1]) - float(target[1])) <= self._EPS):
                return (float(target[0]), float(target[1]), str(kind))
        except Exception:
            pass
        return None

    def _drag_intent(self) -> dict:
        """Preview intent for a drag shape, with transient snap feedback.

        The snap indicator is offered for Line and Arrow only (their
        existing Connect-Lines configuration is unchanged); Rectangle and
        Ellipse keep their current preview exactly.
        """
        intent = {"shape_update": self._preview_shape()}
        if self.kind in (ToolType.LINE, ToolType.ARROW):
            intent["shape_snap"] = self._resolved_snap()
        return intent

    def _polygon_preview(self) -> PolygonPreview:
        """Transient fill contour, provisional edge, markers (M6.7.2).

        Emits three *separate* preview concerns so their closing behavior
        cannot interfere: the fill contour (confirmed vertices + pointer),
        the provisional live segment (last confirmed vertex -> pointer),
        and the confirmed-vertex list (markers). The pointer is never a
        confirmed vertex, and no closing edge back to the first vertex is
        ever produced here.
        """
        vertices = tuple(self._vertices)
        pointer = None
        if self._pointer is not None:
            pointer = (float(self._pointer[0]), float(self._pointer[1]))
        fill = list(vertices)
        segment = ()
        if pointer is not None:
            if vertices:
                last = vertices[-1]
                if (abs(pointer[0] - last.x) > self._EPS
                        or abs(pointer[1] - last.y) > self._EPS):
                    segment = (Point(last.x, last.y),
                               Point(pointer[0], pointer[1]))
            if (not fill
                    or abs(pointer[0] - fill[-1].x) > self._EPS
                    or abs(pointer[1] - fill[-1].y) > self._EPS):
                fill.append(Point(pointer[0], pointer[1]))
        fill_t = tuple(fill)
        # M6.7.2 follow-up: expose the transient Connect-Lines hit so the
        # viewer can paint a snap indicator — but only while a real
        # provisional edge exists (segment non-empty) and the pointer
        # actually resolved to the recorded target. This clears the
        # indicator the instant a vertex is confirmed (pointer == last
        # vertex => no segment), on cancellation, and on tool switch.
        snap_target = ()
        snap_kind = ""
        if segment and self._pointer_snap is not None:
            kind, target = self._pointer_snap
            try:
                tx, ty = float(target[0]), float(target[1])
            except Exception:
                tx = ty = None
            if tx is not None and (abs(pointer[0] - tx) <= self._EPS
                                   and abs(pointer[1] - ty) <= self._EPS):
                snap_target = (tx, ty)
                snap_kind = str(kind)
        page_id = (self._page_id if self._page_id is not None else PageId())
        return PolygonPreview(page_id=page_id, rect=_points_rect(fill_t),
                              fill=fill_t, vertices=vertices,
                              segment=segment, snap_target=snap_target,
                              snap_kind=snap_kind)

    def _polygon_intent(self) -> dict:
        """Preview intent: confirmed-edge outline + fill/marker geometry.

        The outline Shape's bounding ``rect`` covers the pointer too (from
        the preview rect), so the viewer's bounded invalidation repaints
        the whole live contour (fill + provisional edge), while its
        ``points`` remain the confirmed vertices only.
        """
        preview = self._polygon_preview()
        shape = self._make_shape(tuple(self._vertices), preview.rect,
                                 close_path=False)
        snap = None
        if preview.snap_target:
            snap = (float(preview.snap_target[0]),
                    float(preview.snap_target[1]), str(preview.snap_kind))
        return {"shape_update": shape, "shape_preview": preview,
                "shape_snap": snap}

    def _clear(self) -> None:
        self._active = False
        self._start = None
        self._current = None
        self._vertices = []
        self._pointer = None
        self._pointer_snap = None
        self._snap = ShapeSnapContext()

    # -- gesture ---------------------------------------------------------

    def down(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0,
             snap: ShapeSnapContext | None = None) -> dict:
        if self._active:
            if self.kind == ToolType.POLYGON:
                if snap is not None:
                    self._snap = snap
                return self._polygon_click(x, y)
            return {"noop": True}
        self._page_id = page_id
        self._latched = dict(self._cfg)
        self._snap = snap if snap is not None else ShapeSnapContext()
        self._start = None
        self._current = None
        self._vertices = []
        self._pointer = None
        self._pointer_snap = None
        self._active = True
        self.events = ["down"]
        if self.kind == ToolType.POLYGON:
            return self._polygon_click(x, y)
        sx, sy = self._resolve_start(x, y)
        self._start = (sx, sy)
        self._current = (sx, sy)
        return self._drag_intent()

    def move(self, page_id: PageId, x: float, y: float,
             pressure: float = 1.0, now_ms: int = 0,
             snap: ShapeSnapContext | None = None) -> dict:
        if not self._active or self._page_id is None:
            return {"noop": True}
        if page_id != self._page_id:
            return {"noop": True}
        if snap is not None:
            # M6.7.2: refresh the Connect-Lines targets on every move so
            # the endpoint/intersection resolver runs live against the
            # current pointer position (never only the press-time
            # context). When Polygon's Snap-to-15° — and therefore its
            # Connect Lines behavior — is on, the provisional edge and
            # fill must track the currently resolved position.
            self._snap = snap
        self.events.append("move")
        if self.kind == ToolType.POLYGON:
            # M6.7.2: pointer movement updates the live fill preview and
            # the provisional edge from the last confirmed vertex to the
            # pointer. It never appends a confirmed vertex and never adds
            # an unconfirmed closing stroke back to the first vertex.
            self._pointer = self._resolve_end(x, y)
            return self._polygon_intent()
        self._current = self._resolve_end(x, y)
        return self._drag_intent()

    def up(self, page_id: PageId, x: float, y: float,
           pressure: float = 1.0, now_ms: int = 0) -> dict:
        if not self._active or self._page_id is None:
            return {"noop": True}
        if self.kind == ToolType.POLYGON:
            # Committed on press (near-origin / double-click); release
            # only refreshes the provisional pointer.
            self.events.append("up")
            return {"noop": True}
        if page_id == self._page_id:
            self._current = self._resolve_end(x, y)
        return self._finalize_drag()

    def finish(self) -> dict:
        """Close an in-progress polygon (double-click). Exactly one commit."""
        if self._active and self.kind == ToolType.POLYGON:
            return self._finish_polygon()
        return {"noop": True}

    def cancel(self) -> dict:
        self._clear()
        self.events.append("cancel")
        return {"shape_cancelled": True}

    # -- finalization ----------------------------------------------------

    def _polygon_click(self, x: float, y: float) -> dict:
        if not self._vertices:
            vx, vy = self._resolve_start(x, y)
            self._vertices = [Point(vx, vy)]
            self._pointer = (vx, vy)
            self.events.append("vertex")
            return self._polygon_intent()
        first = self._vertices[0]
        if (len(self._vertices) >= 3
                and math.hypot(x - first.x, y - first.y)
                <= self._CLOSE_TOL_PT):
            return self._finish_polygon()
        vx, vy = self._resolve_end(x, y)
        last = self._vertices[-1]
        if abs(vx - last.x) < self._EPS and abs(vy - last.y) < self._EPS:
            return {"noop": True}
        self._vertices.append(Point(vx, vy))
        self._pointer = (vx, vy)
        self.events.append("vertex")
        return self._polygon_intent()

    def _finish_polygon(self) -> dict:
        vertices = tuple(self._vertices)
        self._clear()
        if len(vertices) < 3:
            self.events.append("cancel")
            return {"shape_cancelled": True}
        self.events.append("done")
        return {"shape_done": self._make_shape(
            vertices, _points_rect(vertices))}

    def _finalize_drag(self) -> dict:
        start = self._start
        current = self._current
        if start is None or current is None:
            self._clear()
            self.events.append("cancel")
            return {"shape_cancelled": True}
        sx, sy = start
        cx, cy = current
        if self.kind in (ToolType.LINE, ToolType.ARROW):
            if math.hypot(cx - sx, cy - sy) < self._MIN_DIM_PT:
                self._clear()
                self.events.append("cancel")
                return {"shape_cancelled": True}
            pts = (Point(sx, sy), Point(cx, cy))
            shape = self._make_shape(pts, _points_rect(pts))
        else:
            rect = _box_rect(sx, sy, cx, cy)
            if (rect.width < self._MIN_DIM_PT
                    or rect.height < self._MIN_DIM_PT):
                self._clear()
                self.events.append("cancel")
                return {"shape_cancelled": True}
            shape = self._make_shape((), rect)
        self._clear()
        self.events.append("done")
        return {"shape_done": shape}


class StubHandler:
    """Recording placeholder for tools whose milestone has not landed."""

    def __init__(self, kind: ToolType = ToolType.PEN) -> None:
        self.kind = kind
        self.events: list[str] = []

    def down(self, event: GestureEvent) -> dict:
        self.events.append(f"down:{event.x:.0f},{event.y:.0f}")
        return {"noop": True}

    def move(self, event: GestureEvent) -> dict:
        self.events.append(f"move:{event.x:.0f},{event.y:.0f}")
        return {"noop": True}

    def up(self, event: GestureEvent) -> dict:
        self.events.append("up")
        return {"noop": True}

    def cancel(self) -> dict:
        self.events.append("cancel")
        return {"noop": True}


class InteractionController:
    """Routes pointer gestures to the handler for the active tool."""

    def __init__(self, tool: ToolInstance | None = None,
                 pressure_resolver=None,
                 profile_resolver=None,
                 input_stage_factory=None) -> None:
        self._tool = tool
        # Post-Slice-3 fix A: optional zero-arg callable returning the
        # global pressure switch (truthy/falsey/None). Resolved at each
        # configuration and OR-ed with the slot flag; the press-time
        # latch keeps mid-stroke timing safe. None = slot flag only.
        self._pressure_resolver = pressure_resolver
        # B.2.3: optional zero-arg callable returning the Settings
        # pressure-response profile name. Resolved per configuration,
        # sanitized, and latched at stroke start like the flag above.
        # None = default profile.
        self._profile_resolver = profile_resolver
        # B.3.2: optional zero-arg callable returning a fresh
        # InputStage for each newly constructed PenHandler. None =
        # identity stage (current behavior exactly). Only the
        # experiment harness passes one; the viewer never does.
        self._input_stage_factory = input_stage_factory
        self._last_pen: PenHandler | None = None
        self._pan = PanHandler()
        self._pens: dict[str, PenHandler] = {}
        self._highlighters: dict[str, "HighlighterHandler"] = {}
        self._erasers: dict[str, EraserHandler] = {}
        self._shapes: dict[str, ShapeHandler] = {}
        self._stubs: dict[str, StubHandler] = {}
        self.last_intent: dict = {"noop": True}

    @property
    def active_tool(self) -> ToolInstance | None:
        return self._tool

    @property
    def active_kind(self) -> ToolType | None:
        return self._tool.kind if self._tool is not None else None

    def live_outline(self):
        """Incremental outline for the active pen stroke, or None.

        Pull-based (the viewer calls this when applying a live update):
        None for non-pen tools, disabled rendering, or empty strokes.
        Reads the last pen handler without reconfiguring (no resolver
        I/O, no snapshot churn). Never raises.
        """
        try:
            pen = getattr(self, "_last_pen", None)
        except Exception:
            return None
        if not isinstance(pen, PenHandler):
            return None
        try:
            return pen.live_outline()
        except Exception:
            return None

    def live_centerline(self):
        """Incremental widthless centerline for the active pen stroke.

        B.2.10: pull-based like ``live_outline``; None for non-pen
        tools or empty strokes. Serves the Pressure-OFF paint path.
        Never raises.
        """
        try:
            pen = getattr(self, "_last_pen", None)
        except Exception:
            return None
        if not isinstance(pen, PenHandler):
            return None
        try:
            return pen.live_centerline()
        except Exception:
            return None

    def set_tool(self, tool: ToolInstance | None) -> None:
        if self._tool is not None and tool is not None:
            if tool.id.value != self._tool.id.value:
                self._pan.cancel()
                pen = self._pens.get(self._tool.id.value)
                if pen is not None:
                    pen.cancel()
                highlighter = self._highlighters.get(self._tool.id.value)
                if highlighter is not None:
                    highlighter.cancel()
                eraser = self._erasers.get(self._tool.id.value)
                if eraser is not None:
                    eraser.cancel()
                shape = self._shapes.get(self._tool.id.value)
                if shape is not None:
                    shape.cancel()
        self._tool = tool

    def _highlight_handler(self) -> HighlighterHandler:
        """Cached per-slot highlighter handler with refreshed config."""
        key = self._tool.id.value if self._tool is not None else ""
        handler = self._highlighters.get(key)
        if handler is None or handler.kind != (
                self._tool.kind if self._tool is not None else None):
            handler = HighlighterHandler()
            self._highlighters[key] = handler
        handler.configure(self._tool)
        return handler

    def _eraser_handler(self) -> EraserHandler:
        """Cached per-slot eraser handler (M6.5)."""
        key = self._tool.id.value if self._tool is not None else ""
        handler = self._erasers.get(key)
        if handler is None:
            handler = EraserHandler()
            self._erasers[key] = handler
        return handler

    def _shape_handler(self) -> ShapeHandler:
        """Cached per-slot shape handler with refreshed config (M6.7)."""
        key = self._tool.id.value if self._tool is not None else ""
        handler = self._shapes.get(key)
        if handler is None or handler.kind != (
                self._tool.kind if self._tool is not None else None):
            handler = ShapeHandler()
            self._shapes[key] = handler
        handler.configure(self._tool)
        return handler

    def shape_active(self) -> bool:
        """True while the active shape tool has a gesture in progress."""
        try:
            tool = self._tool
            if tool is None:
                return False
            handler = self._shapes.get(tool.id.value)
            return bool(handler is not None and handler.active)
        except Exception:
            return False

    def shape_pending_snap_candidates(self):
        """Confirmed geometry of the in-progress Polygon as snap candidates.

        Returns ``(endpoints, segments)`` from the active shape handler's
        **confirmed** vertices/edges, or ``(), ()`` when no Polygon gesture
        is live. The viewer merges these into the
        :class:`ShapeSnapContext` so a later vertex can connect to the same
        unfinished polygon without committing it to the store. Reads the
        cached handler directly (no config refresh) so it is side-effect
        free on the pointer-move path.
        """
        try:
            tool = self._tool
            if tool is None:
                return (), ()
            handler = self._shapes.get(tool.id.value)
            if handler is None:
                return (), ()
            return handler.pending_snap_candidates()
        except Exception:
            return (), ()

    def _pen_handler(self) -> PenHandler:
        """Cached per-slot pen handler with refreshed config snapshot."""
        key = self._tool.id.value if self._tool is not None else ""
        pen = self._pens.get(key)
        if pen is None or pen.kind != (
                self._tool.kind if self._tool is not None else None):
            stage = None
            if self._input_stage_factory is not None:
                stage = self._input_stage_factory()
            pen = PenHandler(
                self._tool.kind if self._tool is not None else ToolType.PEN,
                input_stage=stage)
            self._pens[key] = pen
        override = None
        resolver = self._pressure_resolver
        if resolver is not None:
            try:
                override = resolver()
            except Exception:
                override = None
        profile = None
        profile_resolver = self._profile_resolver
        if profile_resolver is not None:
            try:
                profile = profile_resolver()
            except Exception:
                profile = None
        pen.configure(self._tool, pressure_override=override,
                      profile_override=profile)
        self._last_pen = pen
        return pen

    def _handler(self):
        if self._tool is None:
            return self._pan
        if self._tool.kind == ToolType.PAN:
            return self._pan
        if self._tool.kind == ToolType.PEN:
            return self._pen_handler()
        if self._tool.kind == ToolType.HIGHLIGHTER:
            return self._highlight_handler()
        if self._tool.kind == ToolType.ERASER:
            return self._eraser_handler()
        if self._tool.kind in SHAPE_TOOL_KINDS:
            return self._shape_handler()
        key = self._tool.id.value
        stub = self._stubs.get(key)
        if stub is None or stub.kind != self._tool.kind:
            stub = StubHandler(self._tool.kind)
            self._stubs[key] = stub
        return stub

    def handle_press(self, x: float, y: float, buttons: int = 1,
                     pressure: float = 1.0) -> dict:
        handler = self._handler()
        if isinstance(handler, (PenHandler, HighlighterHandler,
                                EraserHandler, ShapeHandler)):
            # Pen/highlighter gestures must arrive mapped via
            # handle_pen_*/handle_highlight_* (page-local rotated pt +
            # page identity); raw viewport-px input is ignored.
            self.last_intent = {"noop": True}
            return self.last_intent
        intent = handler.down(GestureEvent(float(x), float(y), buttons, pressure))
        self.last_intent = intent
        return intent

    def handle_move(self, x: float, y: float, buttons: int = 1,
                    pressure: float = 1.0) -> dict:
        handler = self._handler()
        if isinstance(handler, (PenHandler, HighlighterHandler,
                                EraserHandler, ShapeHandler)):
            self.last_intent = {"noop": True}
            return self.last_intent
        intent = handler.move(GestureEvent(float(x), float(y), buttons, pressure))
        self.last_intent = intent
        return intent

    def handle_release(self, x: float, y: float) -> dict:
        handler = self._handler()
        if isinstance(handler, (PenHandler, HighlighterHandler,
                                EraserHandler, ShapeHandler)):
            self.last_intent = {"noop": True}
            return self.last_intent
        intent = handler.up(GestureEvent(float(x), float(y)))
        self.last_intent = intent
        return intent

    def handle_pen_press(self, page_id: PageId, x: float, y: float,
                         pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Pen press in page-local rotated pt (viewer-mapped)."""
        intent = self._pen_handler().down(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_pen_move(self, page_id: PageId, x: float, y: float,
                        pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Pen move in page-local rotated pt (viewer-mapped)."""
        intent = self._pen_handler().move(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_pen_release(self, page_id: PageId, x: float, y: float,
                           pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Pen release in page-local rotated pt (viewer-mapped)."""
        intent = self._pen_handler().up(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_highlight_press(self, page_id: PageId, x: float, y: float,
                               pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Highlighter press in page-local rotated pt (viewer-mapped)."""
        intent = self._highlight_handler().down(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_highlight_move(self, page_id: PageId, x: float, y: float,
                              pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Highlighter move in page-local rotated pt (viewer-mapped)."""
        intent = self._highlight_handler().move(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_highlight_release(self, page_id: PageId, x: float, y: float,
                                 pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Highlighter release in page-local rotated pt (viewer-mapped)."""
        intent = self._highlight_handler().up(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_eraser_press(self, page_id: PageId, x: float, y: float,
                            pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Eraser press in page-local rotated pt (viewer-mapped)."""
        intent = self._eraser_handler().down(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_eraser_move(self, page_id: PageId, x: float, y: float,
                           pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Eraser move in page-local rotated pt (viewer-mapped)."""
        intent = self._eraser_handler().move(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_eraser_release(self, page_id: PageId, x: float, y: float,
                              pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Eraser release in page-local rotated pt (viewer-mapped)."""
        intent = self._eraser_handler().up(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def cancel(self) -> dict:
        intent = self._handler().cancel()
        self.last_intent = intent
        return intent

    def handle_shape_press(self, page_id: PageId, x: float, y: float,
                           pressure: float = 1.0, now_ms: int = 0,
                           snap: ShapeSnapContext | None = None) -> dict:
        """Shape press in page-local rotated pt (viewer-mapped, M6.7)."""
        intent = self._shape_handler().down(
            page_id, float(x), float(y), pressure, now_ms, snap)
        self.last_intent = intent
        return intent

    def handle_shape_move(self, page_id: PageId, x: float, y: float,
                          pressure: float = 1.0, now_ms: int = 0,
                          snap: ShapeSnapContext | None = None) -> dict:
        """Shape move in page-local rotated pt (viewer-mapped, M6.7)."""
        intent = self._shape_handler().move(
            page_id, float(x), float(y), pressure, now_ms, snap)
        self.last_intent = intent
        return intent

    def handle_shape_release(self, page_id: PageId, x: float, y: float,
                             pressure: float = 1.0, now_ms: int = 0) -> dict:
        """Shape release in page-local rotated pt (viewer-mapped, M6.7)."""
        intent = self._shape_handler().up(
            page_id, float(x), float(y), pressure, now_ms)
        self.last_intent = intent
        return intent

    def handle_shape_double_click(self) -> dict:
        """Finish an in-progress polygon (double-click commit, M6.7)."""
        intent = self._shape_handler().finish()
        self.last_intent = intent
        return intent
