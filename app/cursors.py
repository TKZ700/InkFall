"""Selection-interaction cursors (M6.8.2 Stage 5.2).

Qt offers no rotation cursor and Inkfall ships no cursor assets, so the
rotate affordance is a small cursor *drawn with Qt itself* — the same
programmatic-glyph convention the app already uses in :mod:`app.icons` —
rather than presenting the default arrow. Resize and move feedback uses
Qt's standard shapes.

The mapping is orientation-aware: a grip's cursor follows its *on-screen*
direction, so a rotated selection still shows the diagonal/edge resize
cursor that matches the direction the grip actually moves in.

Pure Qt, no new dependency; no module-level side effects beyond a small
cache of built cursors.
"""

from __future__ import annotations

import math
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QCursor, QImage, QPainter, QPainterPath,
                           QPen, QPixmap)

from annotations.ink_geometry import HIGHLIGHTER_BAND_ASPECT

__all__ = ["cursor_for_key", "cursor_key_for_handle", "frame_centre",
           "rotate_cursor", "cursor_for_spec", "tool_cursor_spec",
           "stroke_width_px", "apply_ui_cursor"]

#: Cursor keys the viewer applies (``None`` means "the tool default").
ROTATE_KEY = "rotate"

_CORNER_HANDLES = ("nw", "ne", "se", "sw")

#: Edge grip -> the two frame corners that span its tangent.
_EDGE_TANGENTS = {
    "n": ("nw", "ne"),
    "s": ("sw", "se"),
    "e": ("ne", "se"),
    "w": ("nw", "sw"),
}

#: Built cursors, keyed by (size, color).
_ROTATE_CACHE: dict = {}


def rotate_cursor(size: int = 22, color: str = "#8B93E0") -> QCursor:
    """A cached custom rotate cursor: an open arc with an arrowhead.

    Drawn at ``size`` px with the hotspot at the centre so the pointer
    stays on the rotation pivot, and cached per (size, color).
    """
    key = (int(size), str(color))
    cached = _ROTATE_CACHE.get(key)
    if cached is not None:
        return cached
    size = max(12, int(size))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tone = QColor(str(color))
        if not tone.isValid():
            tone = QColor("#8B93E0")
        pen = QPen(tone, max(1.6, size * 0.11))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = size * 0.26
        side = size - 2.0 * inset
        path = QPainterPath()
        path.arcMoveTo(inset, inset, side, side, 30.0)
        path.arcTo(inset, inset, side, side, 30.0, 270.0)
        painter.drawPath(path)
        # Arrowhead on the free end of the arc (tangent-continuous).
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tone)
        radius = side / 2.0
        cx = cy = inset + radius
        end = math.radians(300.0)
        tip_dx, tip_dy = math.sin(end), math.cos(end)
        ex = cx + radius * math.cos(end)
        ey = cy - radius * math.sin(end)
        head = max(3.0, size * 0.20)
        painter.drawPolygon([
            QPointF(ex + tip_dx * head, ey + tip_dy * head),
            QPointF(ex - tip_dy * head * 0.72,
                    ey + tip_dx * head * 0.72),
            QPointF(ex + tip_dy * head * 0.72,
                    ey - tip_dx * head * 0.72),
        ])
    finally:
        painter.end()
    cursor = QCursor(pixmap, size // 2, size // 2)
    _ROTATE_CACHE[key] = cursor
    return cursor


def frame_centre(points) -> tuple[float, float]:
    """Centre of the four frame corners, in widget space."""
    xs = [float(points[name][0]) for name in _CORNER_HANDLES]
    ys = [float(points[name][1]) for name in _CORNER_HANDLES]
    return (sum(xs) / 4.0, sum(ys) / 4.0)


def _diagonal_key(dx: float, dy: float):
    """The diagonal resize cursor for a signed screen direction."""
    return ("SizeFDiagCursor" if dx * dy > 0.0 else "SizeBDiagCursor")


def _axis_key(dx: float, dy: float):
    """Horizontal/vertical resize cursor, diagonal only when truly skewed."""
    ax, ay = abs(dx), abs(dy)
    if ax <= 0.42 * ay:
        return "SizeVerCursor"
    if ay <= 0.42 * ax:
        return "SizeHorCursor"
    return _diagonal_key(dx, dy)


def cursor_key_for_handle(handle, points):
    """Cursor key for one grip, following its on-screen direction.

    ``points`` is the frame's grip-centre mapping (see
    ``Viewer._selection_handle_points``). Corner grips always read as
    diagonal (the established convention for corner resize), oriented by
    the corner's actual screen direction; edge grips read horizontal or
    vertical according to the edge normal, so a rotated selection still
    gets a truthful cursor.
    """
    if handle == "rotate":
        return ROTATE_KEY
    if not points:
        return None
    try:
        if handle in _CORNER_HANDLES:
            cx, cy = frame_centre(points)
            hx, hy = points[handle]
            dx, dy = float(hx) - cx, float(hy) - cy
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                return None
            return _diagonal_key(dx, dy)
        pair = _EDGE_TANGENTS.get(handle)
        if pair is None:
            return None
        ax, ay = points[pair[0]]
        bx, by = points[pair[1]]
        tx, ty = float(bx) - float(ax), float(by) - float(ay)
        if abs(tx) <= 1e-9 and abs(ty) <= 1e-9:
            return None
        # The edge's inward/outward normal is the resize direction.
        return _axis_key(-ty, tx)
    except Exception:
        return None


def cursor_for_key(key):
    """The ``QCursor`` for a key, or ``None`` for the tool default."""
    if key is None:
        return None
    if key == ROTATE_KEY:
        return rotate_cursor()
    try:
        shape = getattr(Qt.CursorShape, str(key))
    except Exception:
        return None
    return QCursor(shape)


# -- Tool cursors ----------------------------------------------------------
#
# Each drawing tool gets a cursor that previews what it will do, drawn with
# Qt itself (the same no-assets convention as :func:`rotate_cursor`) and
# derived from the *renderer's* own stroke convention rather than from
# cursor-local defaults:
#
#   pen / line / arrow / shapes -> filled disc, active color + width
#   highlighter                 -> vertical translucent band, color + opacity
#   eraser                      -> tilted two-section eraser icon
#   lasso                       -> rope loop + tail, working-tip hotspot
#   pan                         -> native open/closed hand
#   selection / note            -> no custom cursor (Qt's default is kept)
#
# Rendering is keyed by a *quantized* spec (kind, color, logical size,
# alpha, device pixel ratio) held in a bounded LRU, so continuous zoom
# reuses the same cursor images instead of allocating one per raw float.
# Only the cursor representation is quantized; no annotation width is
# touched.

#: mm -> PDF points, the conversion every paint path uses for ``width_mm``.
MM_TO_PT = 72.0 / 25.4

#: Effective-diameter clamps for the cursor. They bound the *cursor* only;
#: the drawn stroke keeps its configured width at any zoom. The ceilings
#: stay inside the cursor sizes window systems reliably support (32 px is
#: universal; 48/64 are widely supported), so a preview is never scaled
#: down by the platform.
STROKE_MIN_PX = 3.0
STROKE_MAX_PX = 48.0
HIGHLIGHTER_THICK_MIN_PX = 3.0
HIGHLIGHTER_THICK_MAX_PX = 32.0
HIGHLIGHTER_LEN_MIN_PX = 28.0
HIGHLIGHTER_LEN_MAX_PX = 64.0
#: Eraser silhouette: mild coupling with the effective eraser diameter
#: (long side ~= 0.6 * diameter + 10), clamped to a compact, platform-safe
#: icon. The actual eraser keeps its real diameter.
ERASER_ICON_MIN_PX = 18.0
ERASER_ICON_MAX_PX = 36.0
ERASER_ICON_COUPLING = 0.6
ERASER_ICON_BASE_PX = 10.0

#: The highlighter cursor's long edge is proportional to its thickness, so
#: the rectangular footprint stays recognisable at fine and thick settings.
#: The aspect ratio itself is shared with the highlighter renderer
#: (``annotations.ink_geometry.HIGHLIGHTER_BAND_ASPECT``) so the cursor and
#: the painted band cannot drift apart; the clamps above remain
#: cursor-only screen-space limits.
HIGHLIGHTER_ASPECT = HIGHLIGHTER_BAND_ASPECT

_CURSOR_INK_DARK = "#12151b"
_CURSOR_INK_LIGHT = "#f7f8fa"
_CURSOR_HALO = "#ffffff"

#: Bounded cursor cache: keys are quantized specs, values are QCursors.
_TOOL_CURSOR_CACHE: "OrderedDict[tuple, QCursor]" = OrderedDict()
_TOOL_CURSOR_CACHE_MAX = 64

#: Static per-DPR glyphs (lasso) built once and reused.
_STATIC_CURSORS: dict = {}


def _finite(value, fallback: float) -> float:
    try:
        out = float(value)
    except Exception:
        return float(fallback)
    if not math.isfinite(out):
        return float(fallback)
    return out


def _clamp(value: float, low: float, high: float) -> float:
    return min(float(high), max(float(low), float(value)))


def _quantize(value, low: float, high: float, step: float = 1.0) -> int:
    """Clamped, step-quantized cursor representation (never a stroke width)."""
    clamped = _clamp(_finite(value, low), low, high)
    return int(round(clamped / float(step)))


def _color_key(color) -> str:
    tone = QColor(str(color))
    if not tone.isValid():
        tone = QColor("#1b1e26")
    return tone.name(QColor.NameFormat.HexRgb)


def _alpha_key(opacity) -> int:
    """Quantized alpha in 5/255 steps (the rendered cursor translucency)."""
    return int(round(_clamp(_finite(opacity, 1.0), 0.0, 1.0) * 51.0))


def _tone_luminance(tone: QColor) -> float:
    return (0.2126 * tone.redF() + 0.7152 * tone.greenF()
            + 0.0722 * tone.blueF())


def _contrast_tone(fill: QColor) -> QColor:
    """Neutral edge opposite the fill's luminance (no accent colors)."""
    if _tone_luminance(fill) > 0.45:
        return QColor(_CURSOR_INK_DARK)
    return QColor(_CURSOR_INK_LIGHT)


def stroke_width_px(tool, zoom: float = 1.0) -> float:
    """The renderer's effective on-screen stroke width for ``tool``.

    Same convention as every paint path: ``width_mm`` -> PDF points
    (72 / 25.4) -> widget pixels through the continuous UI zoom, with the
    renderer's 1 px floor.
    """
    width_mm = _finite(getattr(tool, "width_mm", 0.5), 0.5)
    if width_mm <= 0.0:
        width_mm = 0.5
    return max(1.0, width_mm * MM_TO_PT * _finite(zoom, 1.0))


def tool_cursor_spec(tool, zoom: float = 1.0, pan_dragging: bool = False,
                     dpr: float = 1.0):
    """Hashable spec of the rendered cursor for ``tool`` (or ``None``).

    ``None`` means "no custom cursor" — the widget keeps Qt's default
    (Selection and Note today). Everything in the spec is quantized, so
    the viewer can use it as its applied-cursor key and the cache stays
    bounded and shared across continuous zoom.
    """
    if tool is None:
        return None
    kind = str(getattr(getattr(tool, "kind", None), "value", "")
               or getattr(tool, "kind", ""))
    scale = _quantize(dpr, 1.0, 3.0, 0.25)
    if kind == "pan":
        return ("pan", bool(pan_dragging), scale)
    if kind == "lasso":
        return ("lasso", scale)
    width_px = stroke_width_px(tool, zoom)
    if kind == "eraser":
        # The icon is mildly coupled to the effective diameter; quantize the
        # rendered long side, not the raw diameter, so the cache is shared.
        long_side = _clamp(width_px * ERASER_ICON_COUPLING
                           + ERASER_ICON_BASE_PX,
                           ERASER_ICON_MIN_PX, ERASER_ICON_MAX_PX)
        return ("eraser", int(round(long_side)), scale)
    if kind == "highlighter":
        thickness = _clamp(width_px, HIGHLIGHTER_THICK_MIN_PX,
                           HIGHLIGHTER_THICK_MAX_PX)
        length = _clamp(thickness * HIGHLIGHTER_ASPECT,
                        HIGHLIGHTER_LEN_MIN_PX, HIGHLIGHTER_LEN_MAX_PX)
        return ("highlighter", _color_key(getattr(tool, "color", "")),
                int(round(thickness)), int(round(length)),
                _alpha_key(getattr(tool, "opacity", 1.0)), scale)
    if kind in ("pen", "line", "arrow", "rectangle", "ellipse", "polygon"):
        return ("stroke", _color_key(getattr(tool, "color", "")),
                _quantize(width_px, STROKE_MIN_PX, STROKE_MAX_PX),
                _alpha_key(getattr(tool, "opacity", 1.0)), scale)
    return None


def _dpr_from_scale(scale: int) -> float:
    return _clamp(int(scale) * 0.25, 1.0, 3.0)


def _render_cursor(width: float, height: float, dpr: float, hot_x: int,
                   hot_y: int, draw) -> QCursor:
    """Rasterize ``draw`` into a DPR-tagged cursor pixmap.

    The art is painted into a physical-pixel ``QImage`` at device pixel
    ratio 1 with an explicit ``scale(dpr)`` transform (so the result never
    depends on how a backend treats a DPR-tagged *paint device*), then the
    pixmap carries the device pixel ratio for Qt's cursor/OS scaling, per
    Qt's high-DPI conventions. ``hot_x``/``hot_y`` are logical pixels,
    which is the coordinate space Qt's cursor hotspot uses.
    """
    physical_w = max(1, int(round(width * dpr)))
    physical_h = max(1, int(round(height * dpr)))
    image = QImage(physical_w, physical_h,
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(dpr, dpr)
        draw(painter)
    finally:
        painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return QCursor(pixmap, int(hot_x), int(hot_y))


def _stroke_dot_cursor(color, diameter_px: int, alpha_key: int,
                       dpr: float) -> QCursor:
    """Filled disc previewing a pen/line/arrow/shape stroke."""
    diameter = float(max(3, int(diameter_px)))
    pad = 1.0
    size = diameter + 2.0 * pad
    rect = QRectF(pad, pad, diameter, diameter)
    fill = QColor(str(color))
    if not fill.isValid():
        fill = QColor("#1b1e26")
    fill.setAlpha(max(1, min(255, int(round(alpha_key * 5.0)))))
    edge = _contrast_tone(fill)
    edge.setAlpha(235)

    def draw(painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawEllipse(rect)
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

    hotspot = int(round(pad + diameter / 2.0))
    return _render_cursor(size, size, dpr, hotspot, hotspot, draw)


def _highlighter_cursor(color, thickness_px: int, length_px: int,
                        alpha_key: int, dpr: float) -> QCursor:
    """Vertical translucent band previewing the highlighter footprint.

    The band's horizontal width is the effective stroke thickness and its
    height is the same ``HIGHLIGHTER_BAND_ASPECT`` multiple the painter
    sweeps. The hotspot sits at the rectangle's centre — the nib's
    contact point — so the painted band expands and contracts
    symmetrically around the pointer as pressure changes. Square corners,
    and deliberately **no** border,
    outline, halo or edge pen: the fill is the configured color at the
    configured opacity (never boosted), so the preview cannot imply a more
    opaque mark than the highlighter paints.
    """
    thickness = float(max(3, int(thickness_px)))
    length = float(max(3, int(length_px)))
    pad = 1.0
    rect = QRectF(pad, pad, thickness, length)
    fill = QColor(str(color))
    if not fill.isValid():
        fill = QColor("#ffd60a")
    fill.setAlpha(max(1, min(255, int(round(alpha_key * 5.0)))))

    def draw(painter: QPainter) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRect(rect)

    hot_x = int(round(pad + thickness / 2.0))
    hot_y = int(round(pad + length / 2.0))
    return _render_cursor(thickness + 2.0 * pad, length + 2.0 * pad, dpr,
                          hot_x, hot_y, draw)


def _eraser_cursor(long_side_px: int, dpr: float) -> QCursor:
    """Tilted two-section eraser silhouette at the working tip.

    A rounded body is rotated so its working end points down-left; the tip
    section is the light rubber, the back section the darker sleeve. A
    white outer halo under a near-black outline keeps the silhouette
    readable on light and dark pages without tinting it. The hotspot is
    the lower-left working corner — where the eraser meets the page — not
    the icon's centre. The long side is mildly coupled to the effective
    eraser diameter by the spec, then clamped for platform-safe sizes.
    """
    canvas = float(max(14, min(40, int(long_side_px))))
    pad = 3.5
    length = max(8.0, canvas - 2.0 * pad)
    breadth = max(5.5, length * 0.42)
    radius = max(1.5, breadth * 0.28)
    division = -length / 2.0 + length * 0.45
    angle = -40.0
    radians = math.radians(angle)
    cos_a, sin_a = math.cos(radians), math.sin(radians)

    halo = QColor(_CURSOR_HALO)
    halo.setAlpha(200)
    ink = QColor(_CURSOR_INK_DARK)
    ink.setAlpha(242)
    rubber = QColor(_CURSOR_INK_LIGHT)
    rubber.setAlpha(246)
    sleeve = QColor("#6f7787")
    sleeve.setAlpha(235)

    body = QPainterPath()
    body.addRoundedRect(
        QRectF(-length / 2.0, -breadth / 2.0, length, breadth),
        radius, radius)
    sleeve_rect = QRectF(division, -breadth / 2.0 - 1.0,
                         length / 2.0 - division + 1.0, breadth + 2.0)

    def draw(painter: QPainter) -> None:
        painter.translate(canvas / 2.0, canvas / 2.0)
        painter.rotate(angle)
        # Outer white halo first, so the fills cover its inner half: the
        # silhouette keeps a clean light margin on dark pages.
        halo_pen = QPen(halo, 3.2)
        halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(halo_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(body)
        # Two sections: light rubber at the working tip, mid sleeve behind.
        painter.save()
        painter.setClipPath(body)
        painter.fillPath(body, rubber)
        painter.fillRect(sleeve_rect, sleeve)
        painter.restore()
        # Outline + the division between the two sections.
        ink_pen = QPen(ink, 1.4)
        ink_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(ink_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(body)
        painter.setPen(QPen(ink, 1.2))
        painter.drawLine(QPointF(division, -breadth / 2.0),
                         QPointF(division, breadth / 2.0))

    # Working corner (the rounded lower-left tangent) in logical pixels.
    hot_lx = -length / 2.0 + radius * 0.293
    hot_ly = breadth / 2.0 - radius * 0.293
    hot_x = int(round(canvas / 2.0 + hot_lx * cos_a - hot_ly * sin_a))
    hot_y = int(round(canvas / 2.0 + hot_lx * sin_a + hot_ly * cos_a))
    hot_x = max(0, min(int(canvas) - 1, hot_x))
    hot_y = max(0, min(int(canvas) - 1, hot_y))
    return _render_cursor(canvas, canvas, dpr, hot_x, hot_y, draw)


def _lasso_cursor(dpr: float) -> QCursor:
    """Custom lasso glyph with the hotspot at its working tip.

    A solid rope tail starts at the working tip (the hotspot), passes a
    cinch knot and feeds a dashed rope loop that trails down-right, so the
    loop never covers the point being marked. Monochrome dual-tone
    (near-black ink over a white halo) reads on light and dark pages; the
    glyph is built once per device pixel ratio and reused.
    """
    size = 24.0
    halo = QColor(_CURSOR_HALO)
    halo.setAlpha(200)
    ink = QColor(_CURSOR_INK_DARK)
    ink.setAlpha(240)

    # Solid rope tail: working tip -> cinch knot -> inside the loop.
    tail = QPainterPath()
    tail.moveTo(2.6, 2.6)
    tail.quadTo(5.4, 8.6, 8.4, 10.4)
    tail.lineTo(10.6, 11.6)

    # Dashed rope loop: nine round-capped spans around an ellipse.
    centre_x, centre_y = 13.8, 14.4
    radius_x, radius_y = 6.8, 6.4
    spans: list = []
    for index in range(9):
        start = math.radians(206.0 + index * 40.0)
        end = start + math.radians(26.0)
        span = QPainterPath()
        span.moveTo(centre_x + radius_x * math.cos(start),
                    centre_y + radius_y * math.sin(start))
        for step in range(1, 4):
            angle = start + (end - start) * step / 3.0
            span.lineTo(centre_x + radius_x * math.cos(angle),
                        centre_y + radius_y * math.sin(angle))
        spans.append(span)

    def draw(painter: QPainter) -> None:
        halo_pen = QPen(halo, 3.4)
        halo_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        ink_pen = QPen(ink, 1.7)
        ink_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        ink_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for pen in (halo_pen, ink_pen):
            painter.setPen(pen)
            painter.drawPath(tail)
            for span in spans:
                painter.drawPath(span)
        # Cinch knot: a halo-separated dot where tail and loop meet.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(8.4, 10.4), 2.6, 2.6)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(8.4, 10.4), 1.5, 1.5)

    return _render_cursor(size, size, dpr, 2, 2, draw)


def _build_tool_cursor(spec):
    head = spec[0]
    dpr = _dpr_from_scale(spec[-1])
    if head == "stroke":
        return _stroke_dot_cursor(spec[1], spec[2], spec[3], dpr)
    if head == "highlighter":
        return _highlighter_cursor(spec[1], spec[2], spec[3], spec[4], dpr)
    if head == "eraser":
        return _eraser_cursor(spec[1], dpr)
    return None


def cursor_for_spec(spec):
    """The cached ``QCursor`` for a spec from :func:`tool_cursor_spec`.

    ``None`` when the tool keeps Qt's default cursor. Pan uses native Qt
    hand shapes; the lasso glyph is static per device pixel ratio; the
    sized tools come from the bounded LRU, so mouse moves never rebuild a
    pixmap and continuous zoom cannot grow the cache.
    """
    if spec is None:
        return None
    try:
        head = spec[0]
        if head == "pan":
            shape = (Qt.CursorShape.ClosedHandCursor if spec[1]
                     else Qt.CursorShape.OpenHandCursor)
            return QCursor(shape)
        if head == "lasso":
            key = ("lasso", int(spec[1]))
            cursor = _STATIC_CURSORS.get(key)
            if cursor is None:
                cursor = _lasso_cursor(_dpr_from_scale(key[1]))
                _STATIC_CURSORS[key] = cursor
            return cursor
        cached = _TOOL_CURSOR_CACHE.get(spec)
        if cached is not None:
            _TOOL_CURSOR_CACHE.move_to_end(spec)
            return cached
        cursor = _build_tool_cursor(spec)
        if cursor is None:
            return None
        _TOOL_CURSOR_CACHE[spec] = cursor
        while len(_TOOL_CURSOR_CACHE) > _TOOL_CURSOR_CACHE_MAX:
            _TOOL_CURSOR_CACHE.popitem(last=False)
        return cursor
    except Exception:
        return None


# -- UI cursor ownership ---------------------------------------------------
#
# M7.1.4.2: the drawing-tool cursor belongs to the document canvas. In
# Qt a child widget *without* an explicit cursor inherits its parent's, so
# every chrome surface that lives inside the viewer (tool shelf, undo/redo
# capsule, view controls, popups, dock overlays) would otherwise inherit
# the pen/highlighter/eraser cursor in its own gaps. Rather than patching
# individual buttons, each chrome root is walked once: every descendant
# that does not already own a cursor (``WA_SetCursor``) is given the
# ordinary arrow. Controls that legitimately need another cursor —
# ``PointingHand`` buttons, the shelf's ``OpenHand``, text-entry I-beams,
# resize grips — have already set their own and are left untouched, as
# are widgets created later (they inherit the arrow from their parent).

def apply_ui_cursor(root) -> None:
    """Give one chrome surface (and its descendants) the arrow cursor.

    Idempotent and bounded: only the ``root`` subtree is visited, and a
    widget with an explicit cursor is never overridden. Text-entry
    controls are left alone even if the backend does not flag
    ``WA_SetCursor`` for them, so their I-beam is preserved. Safe to call
    again after a surface rebuild.
    """
    try:
        from PySide6.QtWidgets import (QAbstractSpinBox, QLineEdit,
                                       QPlainTextEdit, QTextEdit, QWidget)

        if not isinstance(root, QWidget):
            return
        # Text-entry controls keep their native I-beam: even if a backend
        # does not flag WA_SetCursor for them, the canvas cursor must never
        # replace it.
        text_entry = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)
    except Exception:
        return
    stack = [root]
    visited = 0
    while stack and visited < 4096:
        widget = stack.pop()
        visited += 1
        try:
            if (not isinstance(widget, text_entry)
                    and not widget.testAttribute(
                        Qt.WidgetAttribute.WA_SetCursor)):
                widget.setCursor(Qt.CursorShape.ArrowCursor)
        except Exception:
            pass
        try:
            for child in widget.children():
                if isinstance(child, QWidget):
                    stack.append(child)
        except Exception:
            pass
