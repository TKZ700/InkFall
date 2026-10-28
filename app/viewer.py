"""First real PDF viewer (M3-C). Qt consumer of the M3-B pipeline.

Ownership: this widget owns the document session (manager handle,
``DocumentPipeline``, scroll/zoom state, transient display images).
The rendering backend owns rasterization, cache bytes, scheduling,
workers and generation freshness — this module never touches PyMuPDF,
never submits to ``RendererEngine`` directly, and never blocks on a
``Future``.

QImage lifetime rule: exactly one explicit detachment copy per arrived
tile (``QImage(...).copy()``), so cache eviction can never dangle the UI.
QImages are NEVER placed in ``TileCache``; the display map is pruned to
the current wanted set plus one retained generation (bucket-transition
continuity without unbounded growth).
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys
import time

import numpy as np

from PySide6.QtCore import (QAbstractNativeEventFilter, QPoint, QRect, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QScrollBar, QWidget

from app.viewport_controller import (
    display_scale_for,
    page_screen_rect,
    render_viewport_size,
    scroll_fraction,
    scroll_from_fraction,
    split_current_stale,
    to_int_rect,
    snapped_tile_rect,
)
from annotations.interaction import (
    SHAPE_TOOL_KINDS,
    InteractionController,
    LiveHighlighter,
    ShapeSnapContext,
    snap_hit,
)
from annotations.ink_geometry import (
    HIGHLIGHTER_BAND_ASPECT, SMOOTH_MIN_SPACING_PT, SMOOTH_PASSES,
    build_outline, highlighter_band_hulls, pressure_widths,
    smooth_centerline)
from annotations.models import (
    AnnotationBase,
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
)
from annotations.partial_erase import erase_stroke
# M6.7.2: Qt-free shape hit-testing geometry (shapes always erase in full).
from annotations.shape_hit import (
    segment_hits_shape as _shape_segment_hits,
    shape_extent as _shape_extent,
    shape_rotation_deg as _shape_rotation_deg,
)
# M6.8.1: Qt-free region selection — hit-testing against the rendered
# geometry (marquee and lasso) and the view-only selection state + frame
# geometry. ``clip_polygon_to_rect`` clips a lasso loop to one page's
# displayed bounds before per-page conversion.
from annotations.hit_test import (
    annotation_extent,
    clip_polygon_to_rect,
    pick_annotation,
    polygon_area,
    select_in_polygon,
    select_in_rect,
)
from annotations.selection import SelectionState
# M7.1 Phase A/B Part 2: Qt-free live adjustment for recognized lines and
# closed shapes (pure geometry only; no store, history, timer or widget
# access lives in that module).
from annotations.shape_adjust import (
    CLOSED_KINDS,
    adjusted_closed,
    adjusted_line,
    begin_closed_adjustment,
    begin_line_adjustment,
    centroid,
    rebase_closed_candidate,
    rebase_line,
    snapped_line,
)
# M7.1: Qt-free shape recognition, contour fitting and morph geometry.
# Recognition runs only on the dwell trigger; the morph helpers only while
# a conversion animation is live (never per pointer sample).
from annotations.shape_recognize import (
    CHAIN_MAX_SIDES,
    CHAIN_MAX_TURN_DEG,
    CHAIN_MERGE_RATIO,
    CHAIN_MIN_SIDES,
    HOLD_MS,
    MIN_SNAP_PT,
    MORPH_MS,
    morph_points,
    morph_translation,
    prepare_morph,
    recognize_shape,
)
# M6.8.2 Stage 5: Qt-free stroke/fill recolor policy (which annotations a
# color operation touches and the exact replacement candidate for each).
from annotations.recolor import (
    recolored,
    supports_fill_color,
    supports_stroke_color,
)
# M6.8.2: Qt-free shared-space group transform (prevalidated candidates)
# for the selection-editing gestures.
from annotations.transform import (
    GroupTransform,
    apply_group,
    box_handle_points,
    geometry_changed,
    move_annotation,
    move_vertex,
    normalize_deg,
    positive_scale_clamp,
    resize_box_handle,
    vertex_points,
)
from app.cursors import (apply_ui_cursor, cursor_for_key, cursor_for_spec,
                         cursor_key_for_handle, tool_cursor_spec)
from app.fx import reduced_motion_enabled
from app.reading_position import ReadingPositionStore
from app.theme.tokens import dark_tokens
from app.toolshelf.shelf import ToolShelf
from app.toolshelf.store import ToolShelfStore
from app.undo import (
    AddAnnotationCommand,
    BatchMoveCommand,
    DeleteAnnotationCommand,
    DeletePageCommand,
    DocumentHistory,
    InsertPageCommand,
    ReplaceAnnotationCommand,
    RotatePageCommand,
)
from tools.models import (
    DEFAULT_ERASER_MODE,
    ERASER_MODE_PARTIAL,
    ToolType,
    normalize_eraser_mode,
)
from core.errors import InkfallError
from core.types import Point, Rect, Size
from document.manager import DocumentManager
from rendering.cache import cache_identity
from rendering.coordinates import rotated_page_size
from rendering.layout import DocumentLayout
from rendering.pipeline import DocumentPipeline

__all__ = ["PdfViewer", "POLL_INTERVAL_MS", "MIN_UI_ZOOM", "MAX_UI_ZOOM"]

#: Display-map poll cadence (single-shot re-armed while work is in flight).
POLL_INTERVAL_MS = 40

#: Continuous UI-zoom clamp (raster buckets cover 0.5..8.0).
MIN_UI_ZOOM = 0.25
MAX_UI_ZOOM = 8.0

#: Read-only sessions keep the navigation/selection tools only: every
#: annotation-creating or document-mutating tool is greyed out and
#: disabled on the shelf (widget level), with the selection gate in
#: ``_on_tool_selected`` as the backstop for keyboard shortcuts and
#: programmatic selection routes.
_READONLY_TOOL_KINDS = (ToolType.PAN, ToolType.SELECTION, ToolType.LASSO)

#: M7.0 view controls: page-jump glide (the same retargetable primitive
#: the wheel uses) and the scroll-settle delay before an active fit mode
#: is recalculated for the page under the viewport centre.
_PAGE_GLIDE_MS = 240
_FIT_SETTLE_MS = 160
#: Relative zoom change below which a refit is a no-op (0.1%): keeps the
#: settle refit from relayouting for sub-pixel changes and stops any
#: possibility of a recalculation loop.
_FIT_EPSILON = 0.001

#: Scrollbar presentation insets (presentation only — the bar keeps its
#: sizeHint width, so shelf reservation math is unchanged). INSET_X = 10
#: clears the 6px native resize zone at the viewer edge with room over.
_SCROLLBAR_INSET_X = 10
_SCROLLBAR_INSET_Y = 10
#: Idle delay before the scrollbar fades back out.
_SCROLLBAR_HIDE_MS = 900

#: M7.0.1.2: clearance between the persistent bottom-row controls (undo
#: capsule, floating view controls) and between them and a bottom-docked
#: toolbar. The same gap language the panel uses for its own anchoring.
_CHROME_GAP = 8

#: M6.7 Connect-Lines snap radius, in widget pixels. Converted to a
#: page-local point radius per gesture so snapping stays zoom-correct.
_SHAPE_SNAP_PX = 12.0

#: M7.1.4.1 ink-chain merge: absolute floor (page pt) on a seam tolerance
#: derived from :data:`CHAIN_MERGE_RATIO` times the merged diagonal, so a
#: tiny glyph still needs its sides to meet rather than merely be close.
_CHAIN_MIN_TOL_PT = 0.75

#: M7.1.2 hold-to-shape: pointer travel (widget px, converted per event to
#: page-local pt) that counts as real movement and restarts the dwell
#: timer. Raised from 3.0: a natural hold tremor/stylus oscillation must
#: not keep postponing recognition, and sub-threshold jitter must not
#: cancel an armed morph. Genuine drawing accumulates past this and
#: restarts the dwell as before.
_HOLD_MOVE_EPS_PX = 8.0

#: M7.1.1 jitter damping for that epsilon: the test compares a damped
#: pointer position (exponential average, ~3 samples) with the anchored
#: hold position. Oscillating jitter of a few pixels averages out instead
#: of re-centring the anchor on every swing — the old raw-position test
#: let such oscillation postpone the dwell indefinitely. Genuine drawing
#: moves the average, so it restarts the timer as before.
_HOLD_MOVE_EMA_ALPHA = 0.35

#: M7.1 morph tick cadence (ms). The morph is a short bounded animation,
#: not a continuous one: the timer stops itself at the final frame.
_MORPH_TICK_MS = 16

#: M6.7.2: confirmed-vertex marker radius for the in-progress Polygon
#: preview. A screen-space constant (not scaled by zoom) so the marker
#: stays visually consistent at every zoom level; it is paint-only and
#: never alters the canonical geometry. Radius 4.5 px => ~9 px diameter.
_POLYGON_VERTEX_RADIUS_PX = 4.5

#: M6.7.2: Connect-Lines snap-indicator radius for the live shape preview
#: (Polygon, and now Line/Arrow). A screen-space constant, deliberately
#: larger than a confirmed-vertex marker and drawn as a *hollow* accent ring
#: (no fill), so an active endpoint/intersection snap reads as "connecting to
#: an existing shape" and never as another confirmed vertex. Paint-only.
_SNAP_RING_PX = 6.5

#: B.2.6: incremental live-invalidation tail. The live outline can only
#: differ from the previously painted one within the smoothing
#: influence radius of the new tip (see _live_tail_rect for the proof),
#: so the per-move update rect is derived from the last this many raw
#: samples instead of rescanning every outline vertex. O(1) per event.
_LIVE_TAIL_SAMPLES = 64
#: Extra resampled steps covered beyond the smoothing passes (rail
#: joints +/-1, decimation provisional-tip retraction, safety margin).
_LIVE_TAIL_EXTRA_STEPS = 4

#: M6.5: anti-aliased-edge tolerance for eraser hit-testing (page-local
#: pt). The visible stroke is drawn antialiased, so a hit is accepted
#: when the geometric distance to the stroke's outline is within this
#: small allowance. Kept well below the eraser radius so a genuine near
#: miss still preserves the stroke.
_ERASER_AA_TOLERANCE_PT = 0.5

#: M6.8.1: Selection pick tolerance, in widget pixels. Converted to a
#: page-local point radius per click so a thin stroke stays grabbable at
#: every zoom level (the conversion divides by ``raster_scale * scale``).
#: Deliberately smaller than the Connect-Lines snap radius (_SHAPE_SNAP_PX)
#: so selection never grabs a neighbour the user did not aim at.
_SELECT_TOL_PX = 6.0

#: M6.8.1: a press that never drags further than this (in EITHER axis)
#: is a click, never a region selection. Derived from the reference
#: implementation's click threshold; screen-space so it is zoom
#: independent.
_MARQUEE_MIN_PX = 4.0

#: M6.8.1: extra contact tolerance, in screen px, for region geometry
#: (the exact band test uses ``tol = _select_tol_pt()`` which already
#: includes ``_SELECT_TOL_PX``; this adds nothing extra but a floor so
#: an empty tolerance still admits touching contact).
_REGION_MIN_TOL_PX = 0.0

#: M6.8.1: selection frame color. The theme interaction accent
#: (#8B93E0, see app/theme/tokens.py). The frame is drawn as a single
#: animated dashed outline around the whole selection group — distinct
#: from the Polygon snap ring (hollow accent circle) and the
#: confirmed-vertex markers (filled dots), so selection can never be
#: mistaken for either.
_SELECT_ACCENT = QColor("#8B93E0")

#: M6.8.1: shared selection frame — outward offset, in widget px, so the
#: single group frame clears the enclosed ink instead of touching it.
#: Constant on screen so it never scales with zoom.
_SELECT_FRAME_PAD_PX = 4.0

#: M6.8.1: "marching ants" dash geometry, in units of the pen's width
#: (Qt's dash-pattern convention). ``4 on / 3 off`` reads as a calm
#: selection loop rather than an error indicator.
_SELECT_DASH_ON = 4.0
_SELECT_DASH_OFF = 3.0
_SELECT_DASH_CYCLE = _SELECT_DASH_ON + _SELECT_DASH_OFF

#: M6.8.1: selection animation cadence. ~30 fps keeps the ants smooth
#: and cheap; each tick advances the dash by half a pen width (one
#: screen px at the 2 px pen) and the pulse by one step of its period.
_SELECT_ANIM_INTERVAL_MS = 33
_SELECT_DASH_OFFSET_STEP = 0.5

#: M6.8.1: the pulse is a slow, bounded opacity breathing (never a harsh
#: flash, never a full disappearance). ``_SELECT_PULSE_PERIOD_TICKS`` at
#: ~30 fps is a ~2 s cycle; the floor keeps the frame clearly visible at
#: its dimmest.
_SELECT_PULSE_PERIOD_TICKS = 60
_SELECT_PULSE_MIN = 0.7
_SELECT_PULSE_MAX = 1.0

#: M6.8.2 Stage 4: interactive resize/rotation handles on the shared
#: frame. Sizes are screen-space constants (never scaled by zoom) so the
#: grips stay equally hittable at every zoom level. ``_HANDLE_NAMES`` is
#: the stable clockwise order (top-left first); ``_HANDLE_HIT_PX`` is the
#: grab radius and is deliberately small enough (relative to the 8 px
#: grip pitch) that adjacent visual language stays unambiguous, while a
#: nearest-grip tie-break keeps the vector itself deterministic.
_HANDLE_SIZE_PX = 8.0
_HANDLE_HIT_PX = 4.5
_HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")
#: Rotation grip: above the frame's top-centre, on a short connector.
_ROTATE_HANDLE_OFFSET_PX = 22.0
_ROTATE_HANDLE_RADIUS_PX = 5.0
_ROTATE_HANDLE_HIT_PX = 9.0
#: M7.1.4.2 direct vertex/box editing: round handles (distinct from the
#: square frame grips) sit exactly on a single selected shape's defining
#: points. Screen-space constants, like the frame grips.
_VERTEX_HANDLE_RADIUS_PX = 3.6
_VERTEX_HANDLE_HIT_PX = 6.5
#: A pathological freehand polygon is not turned into thousands of live
#: handles; above this count the shape stays whole-object editable only.
_MAX_VERTEX_HANDLES = 512
#: Minimum scaled group side, in screen px, converted to shared page-pt
#: through the live projection factor so the floor is zoom-independent.
_MIN_GROUP_SIDE_PX = 12.0

#: M6.8.2 clipboard slice: consistent screen-space offset between the
#: invocation point and the pasted group's top-left, so the copies are
#: discoverable and never sit exactly on top of the originals. Expressed
#: in widget px (converted to page-pt through the live projection factor),
#: so it reads the same at every zoom level.
_PASTE_SCREEN_OFFSET = 16.0

_PAGE_FILL = QColor("#ffffff")

# M4.6.4: pure solid black canvas — the page is the focus. Surrounding
# fill only; page rasterization and tile logic are untouched (frozen).
_BACKGROUND_FILL = QColor("#000000")
_EMPTY_TEXT_FILL = QColor("#e0e0e0")


def _invert_image(image: QImage) -> QImage:
    """RGB-inverted copy of one display tile (alpha preserved).

    M7.0.1 PDF appearance: the display pipeline holds RGBA8888 tiles, so
    inverting the colour channels only (``InvertRgb``) leaves the alpha
    untouched and is exactly reversible — the same call restores the
    original pixels. A copy is returned: the image being inverted is
    never modified, so nothing that shares it can be affected.

    M7.1.4.3: this is the "Inverted" appearance mode, pixel-identical to
    the previous two-state toggle's Dark mode.
    """
    try:
        out = image.copy()
        out.invertPixels(QImage.InvertMode.InvertRgb)
        return out
    except Exception:
        try:
            return QImage(image)
        except Exception:
            return image


def _dark_reading_image(image: QImage) -> QImage:
    """Night-reading tile: HSL lightness inverted, hue/saturation kept.

    Why not RGB inversion (that is the separate "Inverted" mode): a
    channel-wise inversion also flips hue and saturation, so coloured ink
    and scans turn into colour negatives. Why not a grayscale negative
    with a colour wash: it throws away saturation entirely and leaves
    pale, washed-out pages.

    This transform works in HSL, which maps *lightness* independently of
    hue and saturation:

    * white paper (HSL L = 1) -> black page (L = 0);
    * black text (L = 0) -> white text (L = 1);
    * coloured text keeps its exact hue and saturation, only its
      lightness flips -> e.g. dark red ink becomes light red ink, not
      gray or pink, so colour content stays saturated and readable;
    * a saturated mid-tone maps to the complementary lightness
      (L -> 1 - L), which is the symmetric "night mode" reading of it.

    Vectorized with numpy (a declared project dependency, already used by
    the render pipeline): no per-pixel Python loops, one C-speed pass per
    tile. Alpha is copied verbatim, so transparent/antialiased edges are
    preserved exactly. Applies only to the viewer's detached display copy
    of a cached original tile — never to renderer-owned pixels.

    Buffer contract (M7.1.4.4): the source pixels are materialized into
    an owned ``bytes`` snapshot before NumPy reads them, ``bytesPerLine``
    and ``sizeInBytes`` are validated against the tile geometry, and the
    transformed ``QImage`` plus its detached copy are checked for null /
    size / format before being returned. Anything unexpected raises
    loudly; there is no silent fallback to another appearance mode and no
    masking of an invalid image.
    """
    src = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = int(src.width()), int(src.height())
    if width <= 0 or height <= 0:
        return image.copy()
    if src.isNull() or src.format() != QImage.Format.Format_RGBA8888:
        # Loud, never a silent fallback to another appearance mode.
        raise RuntimeError(
            "dark appearance: tile did not convert to RGBA8888")
    stride = int(src.bytesPerLine())
    size = int(src.sizeInBytes())
    if stride <= 0 or stride % 4 or size != stride * height:
        raise RuntimeError(
            "dark appearance: unexpected tile buffer geometry "
            f"({width}x{height}, stride {stride}, {size} bytes)")
    # Take an owned snapshot of the pixel bytes *before* NumPy sees them:
    # the array below can never alias Qt's buffer (or a temporary's), so
    # no view can outlive its source and no later Qt detach can mutate
    # the array mid-transform. ``constBits()`` is documented as
    # ``bytes | bytearray | memoryview``; materializing through one
    # ``tobytes()`` normalizes all three.
    packed = memoryview(src.constBits()).tobytes()
    if len(packed) != size:
        raise RuntimeError(
            "dark appearance: pixel snapshot size mismatch "
            f"({len(packed)} != {size})")
    pixels = np.frombuffer(packed, dtype=np.uint8).reshape(
        height, stride // 4, 4)[:, :width, :]
    rgb = pixels[:, :, :3].astype(np.float32) / 255.0
    alpha = pixels[:, :, 3].copy()
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    high = np.maximum(np.maximum(red, green), blue)
    low = np.minimum(np.minimum(red, green), blue)
    chroma = high - low
    saturation = chroma > 1e-6
    safe_chroma = np.where(saturation, chroma, 1.0)
    lightness = (high + low) * 0.5

    # Hue in [0, 6), only meaningful where the pixel is not neutral.
    hue = np.zeros_like(lightness)
    high_red = saturation & (high == red)
    high_green = saturation & ~high_red & (high == green)
    high_blue = saturation & ~high_red & ~high_green
    hue[high_red] = ((green - blue) / safe_chroma)[high_red] % 6.0
    hue[high_green] = (((blue - red) / safe_chroma) + 2.0)[high_green]
    hue[high_blue] = (((red - green) / safe_chroma) + 4.0)[high_blue]
    # HSL saturation (equivalently chroma / (1 - |2L - 1|)).
    sat = np.where(
        saturation,
        chroma / np.maximum(1.0 - np.abs(2.0 * lightness - 1.0), 1e-6),
        0.0)
    sat = np.clip(sat, 0.0, 1.0)

    # Invert lightness only, then rebuild RGB from the same H and S.
    inverted = 1.0 - lightness
    rebuilt_chroma = (1.0 - np.abs(2.0 * inverted - 1.0)) * sat
    second = rebuilt_chroma * (1.0 - np.abs((hue % 2.0) - 1.0))
    offset = inverted - rebuilt_chroma * 0.5
    zero = np.zeros_like(inverted)
    sector = hue % 6.0
    out_red = np.select(
        [sector < 1.0, sector < 2.0, sector < 3.0, sector < 4.0,
         sector < 5.0],
        [rebuilt_chroma, second, zero, zero, second],
        default=rebuilt_chroma)
    out_green = np.select(
        [sector < 1.0, sector < 2.0, sector < 3.0, sector < 4.0,
         sector < 5.0],
        [second, rebuilt_chroma, rebuilt_chroma, second, zero],
        default=zero)
    out_blue = np.select(
        [sector < 1.0, sector < 2.0, sector < 3.0, sector < 4.0,
         sector < 5.0],
        [zero, zero, second, rebuilt_chroma, rebuilt_chroma],
        default=second)
    transformed = np.stack(
        [out_red + offset, out_green + offset, out_blue + offset], axis=2)
    transformed = np.rint(np.clip(transformed * 255.0, 0.0, 255.0))
    # Assemble into an owned, C-contiguous RGBA buffer with explicit
    # channel writes (no ``dstack`` shape/broadcast dependency) and the
    # alpha plane copied byte-for-byte from the snapshot.
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[:, :, 0] = transformed[:, :, 0].astype(np.uint8)
    out[:, :, 1] = transformed[:, :, 1].astype(np.uint8)
    out[:, :, 2] = transformed[:, :, 2].astype(np.uint8)
    out[:, :, 3] = alpha
    data = out.tobytes()
    # Same constructor the proven Light-tile path uses in ``_poll``.
    result = QImage(data, width, height, width * 4,
                    QImage.Format.Format_RGBA8888)
    if (result.isNull() or result.width() != width
            or result.height() != height
            or result.format() != QImage.Format.Format_RGBA8888):
        raise RuntimeError(
            "dark appearance: transformed tile image is invalid")
    # ``.copy()`` deep-copies into a Qt-owned buffer, detaching the result
    # from the Python bytes snapshot above.
    out_image = result.copy()
    if (out_image.isNull() or out_image.width() != width
            or out_image.height() != height
            or int(out_image.sizeInBytes()) != width * 4 * height):
        raise RuntimeError(
            "dark appearance: detached transformed tile is invalid")
    return out_image


def _appearance_tile(image: QImage, mode: str) -> QImage:
    """Apply one appearance mode to a detached display tile.

    ``light`` returns the detached original, ``inverted`` the historical
    full RGB inversion, ``dark`` the HSL-lightness reading transform.
    Always applied to a freshly detached copy from the renderer's cached
    original tile — never to an already-transformed display tile.
    """
    mode = str(mode or "light").strip().lower()
    if mode == "inverted":
        return _invert_image(image)
    if mode == "dark":
        return _dark_reading_image(image)
    return image


def _appearance_solid(color: QColor, mode: str) -> QColor:
    """Per-mode paper colour, mirroring ``_appearance_tile`` exactly."""
    mode = str(mode or "light").strip().lower()
    try:
        red, green, blue = color.red(), color.green(), color.blue()
    except Exception:
        return QColor(color)
    if mode == "inverted":
        return QColor(255 - red, 255 - green, 255 - blue)
    if mode == "dark":
        # Same HSL-lightness inversion as the tiles (paper is neutral, so
        # white -> black exactly; a tinted paper flips its lightness while
        # keeping its hue and saturation). Computed from RGB with the
        # standard-library HSL helpers instead of ``QColor.toHsl()``:
        # PySide6 6.11 returns a *QColor* from ``toHsl()`` (older bindings
        # returned an ``(h, s, l, a)`` tuple), so unpacking it raised
        # ``TypeError`` inside ``paintEvent`` and left the whole page
        # unpainted in Dark mode.
        import colorsys

        hue, light, sat = colorsys.rgb_to_hls(
            red / 255.0, green / 255.0, blue / 255.0)
        nr, ng, nb = colorsys.hls_to_rgb(hue, 1.0 - light, sat)
        return QColor(
            max(0, min(255, int(round(nr * 255.0)))),
            max(0, min(255, int(round(ng * 255.0)))),
            max(0, min(255, int(round(nb * 255.0)))),
            color.alpha(),
        )
    return QColor(color)


class _MoveConflict(Exception):
    """M6.8.2 group-move preflight abort (raised before any macro opens).

    Pure control flow for the two-phase preflight commit: an intervening
    replacement of a still-existing selected annotation, or an
    unsupported group member, must discard the whole operation without
    touching the store or history. Never escapes the viewer.
    """


def _mouse_buttons_value(buttons) -> int:
    """Best-effort int conversion for Qt mouse-button flags.

    M5-B.1: PySide6 exposes ``event.buttons()`` as a flag type that
    ``int()`` rejects, which silently disabled every mouse-gesture path
    (press/move both convert before routing). Centralized here so canvas
    gestures keep working regardless of binding conversion support.
    """
    try:
        return int(buttons)
    except (TypeError, ValueError):
        pass
    try:
        return int(buttons.value)
    except Exception:
        return 0


class _DockZone(QWidget):
    """Translucent docking hint: canvas shows through, never a panel.

    Idle is a faint accent wash; the hovered zone strengthens. No text,
    no opaque fill — zones disappear entirely when repositioning ends.
    """

    def __init__(self, dock: str, parent=None) -> None:
        super().__init__(parent)
        self._dock = dock
        self._active = False
        self.setProperty("dock_target", dock)
        self.setProperty("dock_zone", True)
        self.setObjectName(f"dock-zone-{dock}")
        self.setAccessibleName(f"Dock {dock} zone")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_active_target = "unset"

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = None
            try:
                window = self.window()
                if window is not None and hasattr(window, "_theme"):
                    tokens = window._theme.tokens
            except Exception:
                tokens = None
            try:
                if tokens is not None:
                    from app.theme.tokens import token_rgba

                    ar, ag, ab, aa = token_rgba(tokens.accent_soft)
                    wash = QColor(ar, ag, ab)
                    edge = QColor(tokens.accent)
                else:
                    raise ValueError("no tokens")
            except Exception:
                wash = QColor(139, 147, 224)
                edge = QColor("#8B93E0")
            wash.setAlpha(28 if not self._active else 70)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(wash)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1),
                                    12, 12)
            # No idle border: the wash alone marks the region. Only the
            # hovered zone gets an accent edge (restrained, 2px).
            if self._active:
                pen_color = QColor(edge)
                pen_color.setAlpha(230)
                from PySide6.QtGui import QPen as _QPen

                pen = _QPen(pen_color)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2),
                                        12, 12)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


def _settings_pressure_profile():
    """Settings response-profile name for the interaction resolver.

    Lazy import keeps settings UI out of the viewer import graph;
    failure (or unknown stored values) means the default profile —
    ``pressure_profile()`` already sanitizes, this is belt-and-braces.
    """
    try:
        from app.settings_panel import pressure_profile

        name = pressure_profile()
        return name if isinstance(name, str) else "normal"
    except Exception:
        return "normal"


try:
    import ctypes
    import ctypes.wintypes as _ctypes_wintypes
except Exception:  # pragma: no cover - non-Windows platforms
    ctypes = None
    _ctypes_wintypes = None

_WM_SETCURSOR = 0x0020
_HTCLIENT = 1


class _CursorResetFilter(QAbstractNativeEventFilter):
    """Answer ``WM_SETCURSOR`` so Windows cannot reset Qt's cursor choice.

    Cursor ownership, M7.1.4.2.2: cursor changes on Windows are only
    sticky until the next ``WM_SETCURSOR``. That message is posted by the
    system whenever the pointer enters or moves within a window — pen
    pointers included — and Qt leaves it to ``DefWindowProc``, which
    applies the *window-class* cursor (the standard arrow). Qt's mouse
    machinery re-applies the widget-under-pointer cursor on every mouse
    move, so the mouse path never notices, but the stylus stream is
    accepted at the widget level and synthesises no mouse events and no
    Enter/Leave: after a reset, nothing re-applies Qt's decision. This
    is exactly why a pen cursor survives continuous in-window movement
    but snaps back to an arrow when the stylus leaves the application,
    returns and stops immediately.

    The filter therefore answers each ``WM_SETCURSOR`` for the viewer's
    top-level client area itself: it first re-derives the cursor for
    the message's own pointer position through the viewer's existing
    tablet-path helpers (so the arrival point shows the truthful
    tool/selection/chrome cursor immediately, M7.1.4.2.3), then
    force-applies the resulting Qt window cursor (:meth:`QWindow.cursor`
    is the single source of truth for whoever set it last — Qt's
    internal mouse machinery for a child widget, or the viewer's own
    helpers) and reports the message handled, which suppresses the
    class-cursor reset. ``setCursor`` is preceded by ``unsetCursor``
    because Qt's window-level cursor setter skips the platform call
    when the value already matches — the OS cursor was reset *behind
    Qt's back*, so the rewrite must be forced.

    The filter is event-driven and idempotent: no timing heuristics, no
    override cursor, no state of its own. Every non-client hit and every
    other window (native menus, tooltips, other applications) keeps the
    default behaviour; returning ``False`` there is exactly today's
    behaviour.
    """

    def __init__(self, viewer):
        super().__init__()
        self._viewer = viewer
        self._hwnd = None

    def _top_level_hwnd(self):
        """Resolve and cache the native handle of the viewer's top-level."""
        try:
            top = self._viewer.window()
            handle = top.windowHandle()
            if handle is None:
                return None
            self._hwnd = int(top.winId())
            return self._hwnd
        except Exception:
            self._hwnd = None
            return None

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        """Re-apply Qt's cursor on every ``WM_SETCURSOR`` of our window."""
        if ctypes is None or _ctypes_wintypes is None:
            return False
        try:
            if bytes(eventType) != b"windows_generic_MSG":
                return False
            native = _ctypes_wintypes.MSG.from_address(int(message))
            if native.message != _WM_SETCURSOR:
                return False
            hwnd = self._hwnd if self._hwnd is not None else self._top_level_hwnd()
            if hwnd is None or int(native.hwnd or 0) != hwnd:
                return False
            if (native.lParam & 0xFFFF) != _HTCLIENT:
                return False
            # M7.1.4.2.3: derive the truthful cursor from the native
            # pointer position carried with this message, before the
            # forced re-apply below. Without this step the entering
            # pointer first shows the cursor from before the leave; the
            # derivation runs through the same helpers a TabletMove
            # uses, so the tool/selection/chrome cursor is applied at
            # the exact instant the system asks for one. Points that do
            # not map into the viewer (other chrome bands, scrollbars)
            # do not run a derivation — the re-apply below then keeps
            # the previous behaviour: last-applied cursor, reset
            # suppressed.
            try:
                self._viewer.derive_pointer_cursor_from_global(
                    int(native.pt.x), int(native.pt.y))
            except Exception:
                pass
            window = self._viewer.window().windowHandle()
            if window is None:
                return False
            cursor = window.cursor()
            if cursor is None:
                return False
            # Force the platform call: Qt skips an equal re-set, but the
            # OS cursor was reset *behind Qt's back*, so the rewrite must
            # be forced. Unset -> apply restores exactly the cursor Qt
            # last chose and no other window state.
            window.unsetCursor()
            window.setCursor(cursor)
            return True
        except Exception:
            return False


class PdfViewer(QWidget):
    """Document viewer: scroll/zoom/paint over a ``DocumentPipeline``."""

    #: Human-readable failure reason for an operation that could not run
    #: (title, detail, kind). The main window forwards it to the existing
    #: toast host, so the viewer never reaches into window chrome.
    statusMessage = Signal(str, str, str)
    # M8: emitted when an unsaved blank turns dirty or its save state
    # changes, so the window can refresh the workspace title.
    documentStateChanged = Signal()
    # M8: the HUD's Save half (unsaved blank sessions).
    saveRequested = Signal()

    def __init__(self, document_manager: DocumentManager | None = None) -> None:
        super().__init__()
        # M6.8.2 fix: enable tablet tracking so the canvas receives
        # ``TabletMove`` events while the stylus hovers (tip lifted). The
        # Polygon live preview (provisional edge / fill / snap indicator)
        # must follow the pen without contact, exactly as it follows the
        # mouse; without tracking Qt delivers nothing until the tip
        # touches. Tracking only adds hover delivery — presses, strokes
        # and all existing input are unchanged.
        try:
            self.setTabletTracking(True)
        except Exception:
            pass
        self._manager = document_manager if document_manager is not None else DocumentManager()
        self._reading_positions = ReadingPositionStore()
        self._document = None
        self._pipeline: DocumentPipeline | None = None
        self._ui_zoom = 1.0
        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        # Tool cursors: the chrome owns its pointer. A child with no cursor
        # inherits its parent's, so without this the active tool's drawing
        # cursor would appear over the scrollbar.
        self._scrollbar.setCursor(Qt.CursorShape.ArrowCursor)
        self._scrollbar.valueChanged.connect(self._on_scroll)
        # M5-B.1.2: horizontal channel mirroring the vertical one. The
        # QScrollBar value (logical px) is the single authoritative
        # scroll position per axis; _scroll_doc_x() derives render px.
        self._hscrollbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self._hscrollbar.setCursor(Qt.CursorShape.ArrowCursor)  # as above
        self._hscrollbar.valueChanged.connect(self._on_scroll)
        # Width-gated visibility: hidden entirely until horizontal
        # overflow exists (see _set_hscrollbar_visible); _sync_scrollbar
        # owns the state, the fade chrome only animates what is shown.
        self._hscrollbar_shown = False
        self._hscrollbar.setVisible(False)
        # Presentation-only fade chrome: visible on hover/scroll, idle
        # fades out. Scroll mechanics (_on_scroll, ranges, glide) and
        # the resize veto are untouched — opacity never gates input.
        try:
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            self._scrollbar_fx = QGraphicsOpacityEffect(self._scrollbar)
            self._scrollbar_fx.setOpacity(0.0)
            self._scrollbar.setGraphicsEffect(self._scrollbar_fx)
        except Exception:
            self._scrollbar_fx = None
        try:
            from PySide6.QtWidgets import QGraphicsOpacityEffect

            self._hscrollbar_fx = QGraphicsOpacityEffect(self._hscrollbar)
            self._hscrollbar_fx.setOpacity(0.0)
            self._hscrollbar.setGraphicsEffect(self._hscrollbar_fx)
        except Exception:
            self._hscrollbar_fx = None
        self._scrollbar_anim = None
        self._hscrollbar_anim = None
        self._scrollbar_hide = QTimer(self)
        self._scrollbar_hide.setSingleShot(True)
        self._scrollbar_hide.setInterval(900)
        try:
            self._scrollbar_hide.timeout.connect(
                lambda: self._fade_scrollbar(0.0))
        except Exception:
            pass
        try:
            self._scrollbar.installEventFilter(self)
            self._hscrollbar.installEventFilter(self)
        except Exception:
            pass
        try:
            self._style_scrollbar()
        except Exception:
            pass
        self._display: dict[tuple, QImage] = {}
        self._wanted_prev: set[tuple] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._error: str | None = None
        # Async tab-switch loading state (Phase 2): while a background
        # worker prepares the destination pipeline, the outgoing session
        # stays attached (instantly restorable) but the canvas paints a
        # loading veil instead of its pages.
        self._loading_title: str | None = None
        # Optional background retirer for detached pipelines (installed
        # by MainWindow): moves engine/decoder shutdown joins off the GUI
        # thread. None keeps the historical inline close.
        self._pipeline_retirer = None
        # Instrumentation (tests/benchmarks assert state, never screenshots).
        self.painted_tiles = 0
        self.painted_placeholders = 0
        self.total_painted_tiles = 0
        self.total_painted_placeholders = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # M5-A1: floating tool shelf + interaction routing. Shelf is a
        # plain child (like the scrollbar) so it survives fullscreen;
        # it never touches the pipeline, workers, or fitz.
        self._tool_store = ToolShelfStore.restore()
        self._interaction = InteractionController(
            self._tool_store.active(),
            profile_resolver=_settings_pressure_profile)
        # M5-B.1 / M6.4 pen overlay: committed strokes live in the
        # owning document's AnnotationStore (the authoritative state)
        # and are painted from there; the viewer keeps NO parallel
        # committed list. Only the TRANSIENT in-progress stroke is held
        # here. Points are page-local rotated PDF points (see
        # _event_to_page_pt); _live_stroke is the stroke being drawn.
        self._live_stroke = None
        # M6.3 free-draw highlighter: unlike the pen, a committed
        # highlighter is NOT kept in a viewer list — the owning
        # Document's AnnotationStore is the authoritative state and the
        # paint path reads it directly. These slots hold only the
        # TRANSIENT in-progress preview (a LiveHighlighter from the
        # interaction layer) plus its bounded invalidation rects.
        self._live_highlighter = None
        self._live_highlight_prev_rect = None
        self._live_highlight_full_rect = None
        # M6.7 shape tools (Line/Arrow/Rectangle/Ellipse/Polygon): like
        # the highlighter, a committed shape lives only in the owning
        # document's AnnotationStore; these slots hold the TRANSIENT
        # in-progress preview (a provisional Shape) plus its bounded
        # invalidation rects. ``_shape_mouse_open`` / ``_shape_tablet_open``
        # are the dual-stream guards; a polygon gesture stays open across
        # click/release cycles until it closes or is cancelled.
        self._live_shape = None
        self._live_shape_preview = None
        # M6.7.2: transient Connect-Lines snap feedback for the live shape
        # preview — ``(x, y, kind)`` or None. Drives the paint-only ring for
        # the Polygon, Line and Arrow previews; never stored.
        self._live_shape_snap = None
        self._live_shape_prev_rect = None
        self._live_shape_full_rect = None
        self._shape_mouse_open = False
        self._shape_tablet_open = False
        # M5-B.2.4: previous live-stroke update rect (widget px). B.3.9:
        # bounded invalidation — each live update repaints the union of
        # the current tail rect with this previous SINGLE rect (never
        # an ever-growing accumulation). Reset on done/cancel/close
        # (those paths repaint fully).
        self._live_prev_rect = None
        # B.3.9: running full-stroke union for cancel-erase scope. This
        # is the accumulated rect the old code kept in _live_prev_rect;
        # per-move updates no longer accumulate, so this slot preserves
        # the exact erase behavior when a stroke is discarded. Used
        # only on cancel, never for moves. Reset on done/cancel/close.
        self._live_full_rect = None
        # B.3.9: (owner view, color, width, opacity) of the live stroke.
        # A mid-stroke tool restyle changes already-painted pixels
        # beyond any tail bound, so it takes a full repaint (rare).
        # Reset on done/cancel/close/failure.
        self._live_meta = None
        # M5-B.2.4: completed-stroke outline cache. _pressure_outline
        # output depends ONLY on InkStroke's frozen fields (points,
        # pressures, width_mm, pressure_enabled, pressure_profile), so
        # keying by the stroke object itself is exact: an immutable
        # stroke can never change its outline. Zoom/scroll/opacity map
        # the cached page-space outline at paint time and never affect
        # it. Cleared in close_document (the only stroke-reset path;
        # open_document funnels through it). The live stroke is never
        # cached (still growing; served by live_outline()). Legacy or
        # duck-typed non-InkStroke items bypass the cache and use the
        # uncached path, as do cache-hostile None results (cheap).
        self._outline_cache: dict = {}
        self.outline_cache_hits = 0
        self.outline_cache_misses = 0
        # B.2.10: completed Pressure-OFF smoothed-centerline cache.
        # Same lifecycle argument as _outline_cache: InkStroke is
        # frozen, so keying by the stroke object is exact, and
        # entries die with it in close_document. One tuple per
        # completed OFF stroke, like the outline cache.
        self._off_smooth_cache: dict = {}
        self.off_smooth_cache_hits = 0
        self.off_smooth_cache_misses = 0
        # M6.3: committed-highlighter smoothed-centerline cache. Same
        # lifecycle/identity argument as _off_smooth_cache: Highlighter
        # is a frozen dataclass, so keying by the annotation object is
        # exact, and entries die with the annotation (store clear on
        # close). The transient live preview is never cached.
        self._hl_smooth_cache: dict = {}
        # M8: committed-highlighter rectangular-band cache (page-local
        # footprint hulls from ``highlighter_band_hulls``). Same lifecycle
        # and identity argument as _hl_smooth_cache: frozen annotations
        # only, entries die with the annotation, and move previews plus the
        # live preview are never cached. Band geometry is page-local pt, so
        # zoom and scroll never invalidate it.
        self._hl_band_cache: dict = {}
        # M7.1 hold-to-shape: one restartable single-shot dwell timer
        # (armed/restarted by real pointer movement, never by a poll),
        # plus the bounded morph state it arms. ``_shape_morph`` is None
        # while idle, else a small dict:
        #   kind ("pen"|"highlighter"), target (the live view object whose
        #   preview is substituted — identity is the stale-gesture guard),
        #   page_id, raw/fitted (aligned page-local samples), candidate
        #   (recognized geometry for the release commit), thickness_pt,
        #   phase (0..1), start_ms, duration_ms, prev_rect.
        # A morph never outlives its gesture: release commits it, cancel/
        # tool switch/close drops it. ``_shape_move_anchor`` is the damped
        # page-local point at the last dwell restart (movement epsilon)
        # and ``_shape_move_ema`` the running damped pointer position.
        self._shape_dwell_timer = QTimer(self)
        self._shape_dwell_timer.setSingleShot(True)
        self._shape_dwell_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._shape_dwell_timer.setInterval(int(HOLD_MS))
        self._shape_dwell_timer.timeout.connect(self._on_shape_dwell)
        self._shape_morph_timer = QTimer(self)
        self._shape_morph_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._shape_morph_timer.setInterval(int(_MORPH_TICK_MS))
        self._shape_morph_timer.timeout.connect(self._advance_shape_morph)
        self._shape_morph = None
        self._shape_move_anchor = None
        self._shape_move_ema = None
        # M7.1 Ink to Line: the highlighter slot's persisted
        # ``straight_line`` property, latched at press exactly like the
        # other per-stroke properties (a mid-stroke toggle can never
        # restyle the active stroke).
        self._hl_snap_latched = False
        # M5-B.2.4: page-space bbox memo, keyed by id() of cached
        # outlines with a strong ref (no id-reuse hazard). Completed
        # outlines are pinned by _outline_cache anyway, so entries die
        # with it in close_document. Completed-outline entries are
        # unbounded in principle but one per stroke, like the cache.
        self._bbox_memo: dict = {}
        # B.2.8: single-slot live-geometry memos (identity-keyed, fixed
        # size — never unbounded). The live outline object is replaced
        # on every state-changing append and the live InkStroke on every
        # move, so an ``is``-identity hit proves freshness; any mutation
        # installs a new object and misses. Cleared wherever the live
        # stroke ends (done/cancel/close) so dead strokes are never
        # pinned.
        self._live_extents_memo = None  # (outline, extents|None) | None
        # B.2.8/B.2.10: single-slot OFF-centerline memo. The live
        # stroke object is replaced on every move, so an
        # ``is``-identity hit proves freshness; a miss pulls the
        # handler-owned incremental holder (exact-match) and
        # re-memos. Cleared wherever the live stroke ends so dead
        # strokes are never pinned.
        self._live_smooth_memo = None  # (stroke, spts tuple) | None
        # M5-B.2.0.1: single-pointer stream guards. Exactly one of these
        # is True while a pen stroke is open via that stream; both clear
        # on stroke_done / stroke_cancelled / close. _pen_forward_target
        # pairs a chrome pen tap's press/move/release to one child.
        self._tablet_stroke_open = False
        self._mouse_stroke_open = False
        self._pen_forward_target = None
        # M5-B.2.0.2: pending double-tap chain (target, ms, x, y) for
        # chrome pen taps; cleared on DblClick, target change, or expiry.
        self._pen_last_tap = None
        # M6.5: stroke-level eraser gesture state. ``_eraser_history`` is
        # the document history that holds the gesture's open undo group
        # (None while no group is open), so finalize/cancel always
        # targets the same stack the gesture started on — never a stale
        # one. The stream guards mirror the pen's so a stylus eraser and
        # a mouse eraser can never interleave.
        self._eraser_history = None
        self._eraser_mouse_open = False
        self._eraser_tablet_open = False
        # M6.8.1 Selection/Lasso tools: view-only multi-annotation
        # selection. ``_selection`` holds the selected AnnotationIds in
        # document order (never persisted, never the annotations
        # themselves); the dual-stream guards mirror the pen's so one
        # physical stylus gesture cannot be re-processed through Qt's
        # synthesized mouse fallback (nor vice versa).
        self._selection = SelectionState()
        self._selection_mouse_open = False
        self._selection_tablet_open = False
        # M6.8.2 Stage 5.2 hover cursor: the *key* of the cursor currently
        # applied (``None`` = nothing set). Selection controls use their
        # key strings; the active tool's cursor uses its rendered spec
        # tuple. Comparing keys (never QObjects) is what keeps an
        # unchanged hover from re-setting the same cursor every move.
        self._hover_cursor_key = None
        # M7.1.4.2.1 cursor recovery: True when the platform may have
        # dropped the applied cursor without the key changing (mouse
        # pointer left the canvas/window, window deactivated). The next
        # canvas pointer event then forces one re-apply, so the canvas
        # cursor is truthful again without waiting for an unrelated
        # event. The stylus stream needs no such flag: Qt delivers no
        # widget-level Enter/Leave for a pen (proximity goes only to the
        # application), and the platform can drop the applied cursor
        # with no event reaching this widget at all, so ``tabletEvent``
        # invalidates the cursor key on every stylus press/move/release
        # instead of trying to guess a transition (see below).
        self._pointer_cursor_stale = True
        # ``_last_pointer_cursor_key`` mirrors the last key the cursor
        # helpers actually requested — it survives the stylus stream's
        # per-event invalidation of ``_hover_cursor_key`` and is what
        # :meth:`_refresh_tool_cursor` consults for selection-priority,
        # so an unrelated refresh between two stylus events can never
        # override an in-force selection or chrome cursor.
        self._last_pointer_cursor_key = None
        # M7.1.4.2.2: native ``WM_SETCURSOR`` answerer (Windows only),
        # installed lazily in :meth:`showEvent`; kept here so nothing
        # else can create a second one.
        self._cursor_reset_filter = None
        # M6.8.1 region gesture (widget space): ``None`` while idle;
        # while open, ``{"kind": "rect"|"polygon", "points": [(x, y)]}``
        # with points[0] the press anchor and the last the current.
        # ``_region_prev_screen`` is the overlay's previous widget-space
        # bbox for scoped repaints.
        self._region_gesture = None
        self._region_prev_screen = None
        # M6.8.2 group move (Selection and Lasso share the behavior): a
        # press on a *selected* annotation arms a potential move.
        # ``_move_gesture`` holds the gesture-start snapshot — the
        # pressed kind, the selected ids, their *pristine* store objects
        # (the whole operation is built from these, never from preview
        # candidates), the widget-space anchor, and the threshold
        # crossing flag. Moving candidates live in ``_transform_preview``
        # (keyed by stable AnnotationId, geometry already re-expressed in
        # *destination*-page local coordinates) and never enter the store
        # before a successful commit; ``_transform_dsts`` carries each
        # candidate's destination page index for the preview paint
        # routing (a member transferring pages is painted by the
        # destination pass only — painted exactly once). ``None`` in
        # all three while idle; preview fields always change together.
        self._move_gesture = None
        self._transform_preview = None
        self._transform_dsts = None
        # M6.8.2 corrective: the shared frame is *oriented*. The frame is
        # represented by its four widget-space corners (clockwise from the
        # north-west) so rotating a selection rotates the frame itself
        # instead of collapsing to the axis-aligned bounding box of the
        # rotated contents. ``_frame_state`` caches the frame
        # view-independently (page-local centre, half-extents in page-pt,
        # angle, and the selected ids), so the oriented frame stays glued
        # to the content across zoom/scroll while a rotation's orientation
        # survives into the next resize; ``_preview_frame`` overrides it
        # for the duration of a live transform gesture and is *promoted*
        # to ``_frame_state`` on a successful commit. Both are view-only.
        self._frame_state = None
        self._preview_frame = None
        # M6.8.2 Stage 5: the currently shown selection context menu (or
        # None). Held so a second context-menu event for the same gesture
        # cannot open a duplicate menu; cleared when the menu hides.
        self._selection_menu = None
        # M6.8.2 clipboard slice: the page + page-local point that the
        # *current* menu invocation targets (None for a keyboard-invoked
        # menu). Set immediately before a menu is built, read by the menu
        # builder, and captured by the Paste action's closure so a shown
        # menu can never act on a later invocation's coordinates.
        self._menu_anchor = None
        # M6.8.2 clipboard slice: the last meaningful pointer position
        # expressed in page-local pt (page index, x, y) - the placement
        # fallback for Ctrl+V after the pointer left the canvas. Page
        # coordinates survive zoom/scroll, so a stale widget position can
        # never be re-interpreted against a changed viewport.
        self._last_pointer_pt = None
        # M6.8.1 shared selection frame animation: one lightweight timer,
        # parented to the viewer so it can never outlive it, drives the
        # "marching ants" dash offset and the bounded opacity pulse of the
        # single group frame. ``_selection_phase`` is a monotonically
        # increasing tick counter (reset each time the animation starts)
        # so tests advance the animation deterministically without sleeps.
        # M6.8.2 Stage 5.2: the last active canvas tool kind seen by the
        # selection tool policy, so a *real* tool change can be told apart
        # from a re-application of the tool that is already active.
        self._active_tool_kind = None
        self._selection_phase = 0.0
        self._selection_timer = QTimer(self)
        self._selection_timer.setInterval(_SELECT_ANIM_INTERVAL_MS)
        self._selection_timer.timeout.connect(
            self._advance_selection_animation)
        # M6.8.1 Pan selection: a stationary press *outside* the shared
        # frame clears immediately (no deferred timer — the previous
        # design's single/double-click ambiguity delay is gone). A press
        # inside the frame retains initially; any real drag past the
        # movement threshold clears at once. A stylus double-tap is
        # recognised from the tablet stream itself (``_pan_last_tap``,
        # timed by the platform double-click interval and bounded by the
        # start-drag distance) because a stylus tap never yields a
        # ``MouseButtonDblClick``; ``_pan_tablet_open`` guards against a
        # synthesized mouse fallback re-processing the same gesture.
        self._pan_press_point = None
        self._pan_dragged = False
        self._pan_last_tap = None
        self._pan_tablet_open = False
        # One-shot guard: a stylus double-tap already selected, so a
        # synthesized mouse double-click for the same gesture is dropped.
        self._pan_tablet_dbl = False
        # Tablet hover de-dup: the widget position of the last accepted
        # ``TabletMove``. With tablet tracking on, Qt may also synthesize a
        # mouse move for the same hover; the next button-free mouse move at
        # this position is dropped so the polygon preview updates once.
        self._tablet_hover_pt = None
        # M6.6: the eraser mode is latched for the life of one gesture so
        # a mid-gesture property change can never mix Whole and Partial
        # semantics; None means "read the active tool now".
        self._eraser_mode_latch = None
        # M5-B.2.0.2 Stage 2: Windows Ink gesture filter (installed on
        # first show, Windows only; removed on close).
        self._ink_gesture_filter = None
        self._shelf = ToolShelf(self._tool_store, dark_tokens(), self)
        self._shelf.toolSelected.connect(self._on_tool_selected)
        self._shelf.dockChanged.connect(lambda _d: self._persist_tools())
        self._shelf.editRequested.connect(self._on_tool_edit_requested)
        self._shelf.duplicateRequested.connect(self._on_tool_duplicate_requested)
        self._shelf.removeRequested.connect(self._on_tool_remove_requested)
        self._shelf.addRequested.connect(self._on_add_requested)
        self._shelf.repositionStarted.connect(self._on_reposition_started)
        self._shelf.repositionMoved.connect(self._on_reposition_moved)
        self._shelf.repositionFinished.connect(self._on_reposition_finished)
        self._shelf.repositionCancelled.connect(self._on_reposition_cancelled)
        self._shelf.slotReorderStarted.connect(self._on_slot_reorder_started)
        self._shelf.slotReorderMoved.connect(self._on_slot_reorder_moved)
        self._shelf.slotReorderFinished.connect(self._on_slot_reorder_finished)
        self._shelf.slotReorderCancelled.connect(self._on_slot_reorder_cancelled)
        # Custom-tool drag state (commit happens on release only).
        self._reordering = False
        self._reorder_slot = None
        self._reorder_ghost = None
        self._reorder_trash = None
        self._reorder_indicator = None
        self._reorder_index: int | None = None
        self._reorder_over_trash = False
        self._shelf.setVisible(True)
        # M5-A1 Undo/Redo UI shell: bottom-left capsule, plain viewer
        # child like the shelf (survives fullscreen), no command engine.
        from app.hud import UndoRedoCapsule

        self._undo_hud = UndoRedoCapsule(dark_tokens(), self)
        self._undo_hud.setVisible(True)
        # M6.2: per-document undo histories (QUndoStack per opened
        # document, keyed by DocumentId.value, dropped on close).
        # Wired to the capsule below; tools create commands on these
        # histories in later milestones.
        self._undo_histories: dict[str, DocumentHistory] = {}
        self._undo_hud.undoRequested.connect(self._on_undo_requested)
        self._undo_hud.redoRequested.connect(self._on_redo_requested)
        self._undo_hud.saveRequested.connect(self.saveRequested.emit)
        self._tool_popup = None
        self._tool_popup_launcher = None
        self._tool_popup_slot = None
        self._dock_targets: list = []
        self._docking = False
        self._dock_preview: str | None = None
        # Live-inspector persistence: every property edit applies at once;
        # QSettings writes are debounced so slider drags don't spam disk.
        self._tool_persist_timer = QTimer(self)
        self._tool_persist_timer.setSingleShot(True)
        self._tool_persist_timer.setInterval(300)
        self._tool_persist_timer.timeout.connect(self._flush_tool_persist)
        # M7.0 floating view controls: a compact trigger (bottom-right)
        # with page navigation, zoom, and one-shot fit modes. The panel
        # only renders viewer state and emits intent; the viewer remains
        # the single source of truth for page, zoom, the active fit
        # mode, and the PDF appearance. Plain viewer children, like the
        # shelf and the HUD.
        from app.view_controls import ViewControls

        self._active_fit: str | None = None
        # M7.0.1 / M7.1.4.3 view-only PDF appearance (Light by default).
        # Session state only: no settings, no cross-session preference,
        # and the render cache's own pixels are never touched (see _poll).
        self._pdf_appearance = "light"
        self._view_controls = ViewControls(
            self, dark_tokens(), MIN_UI_ZOOM, MAX_UI_ZOOM,
            insets=self._view_overlay_insets,
            obstacles=self._chrome_obstacles,
            row=self._bottom_row_trigger_origin)
        self._view_controls.prevRequested.connect(self._view_prev_page)
        self._view_controls.nextRequested.connect(self._view_next_page)
        self._view_controls.pageRequested.connect(self.goto_page)
        self._view_controls.zoomInRequested.connect(self.zoom_in)
        self._view_controls.zoomOutRequested.connect(self.zoom_out)
        self._view_controls.zoomPercentRequested.connect(
            self.set_zoom_percent)
        self._view_controls.modeRequested.connect(self.apply_view_mode)
        self._view_controls.appearanceRequested.connect(
            self.set_pdf_appearance)
        self._view_controls.set_available(False)
        # Floating page navigator (Pages disc + filmstrip/grid panel):
        # the same overlay pattern — plain viewer children, presentation
        # only; it never enters the layout, so viewport geometry, page
        # layout, and the render pipeline are untouched while it is open.
        from app.page_navigator import PageNavigator

        self._page_navigator = PageNavigator(
            self, dark_tokens(),
            geometry_provider=self._navigator_geometry_snapshot,
            origin_provider=self._pages_trigger_origin,
            chrome_provider=self._navigator_chrome_widgets,
            capsule_host=self._view_controls.trigger,
            parent=self)
        self._page_navigator.pageSelected.connect(self.goto_page)
        self._page_navigator.pageContextMenuRequested.connect(
            self._on_navigator_page_menu)
        self._page_navigator.set_available(False)
        # One shared [View | Pages] capsule: the View trigger paints as
        # the left segment; the navigator's body paints the silhouette.
        try:
            self._view_controls.trigger.set_capsule_segment(True)
        except Exception:
            pass
        # Scroll settle (single restartable one-shot; no polling): an
        # active fit mode is recalculated for the page under the viewport
        # centre once scrolling has genuinely stopped, never mid-glide.
        # ``_refit_busy`` marks the window of a refit's own scroll
        # writes: those must not re-arm the settle timer, so a refit can
        # never feed itself and no recalculation loop is possible.
        self._refit_busy = False
        self._scroll_settle = QTimer(self)
        self._scroll_settle.setSingleShot(True)
        self._scroll_settle.setInterval(_FIT_SETTLE_MS)
        self._scroll_settle.timeout.connect(
            lambda: self._reapply_fit(anchor=True))
        # M7.1.4.2 UI cursor ownership: the drawing-tool cursor belongs to
        # the document canvas. These chrome surfaces are plain child
        # widgets of the viewer, so a child without an explicit cursor
        # would inherit the active pen/highlighter/eraser cursor in its
        # own gaps. One deterministic pass per surface gives the arrow to
        # every descendant that does not already own a cursor; controls
        # that legitimately need another cursor (PointingHand buttons,
        # the shelf's OpenHand grab, text-entry I-beams) keep theirs, and
        # widgets created later inherit the arrow from their parent.
        # ``ViewControls`` is a controller, not a widget, so its two real
        # surfaces (trigger and panel) are registered individually.
        view_controls = getattr(self, "_view_controls", None)
        surfaces = [self._shelf, self._undo_hud]
        for attr in ("trigger", "panel"):
            surface = getattr(view_controls, attr, None)
            if surface is not None:
                surfaces.append(surface)
        navigator = getattr(self, "_page_navigator", None)
        for attr in ("trigger", "panel"):
            surface = getattr(navigator, attr, None)
            if surface is not None:
                surfaces.append(surface)
        for surface in surfaces:
            apply_ui_cursor(surface)

    @property
    def tool_shelf(self) -> ToolShelf:
        """The floating annotation tool shelf (M5-A1 shell)."""
        return self._shelf

    @property
    def tool_store(self) -> ToolShelfStore:
        """Shelf state (slots, dock, active tool)."""
        return self._tool_store

    @property
    def undo_hud(self):
        """Floating Undo/Redo capsule (wired to per-document history)."""
        return self._undo_hud

    @property
    def current_document(self):
        """The open managed document, or None (M8 identity/dirty owner)."""
        return self._document

    def _document_is_readonly(self) -> bool:
        """True when the open session is an explicit read-only view.

        The UI-level half of the read-only guarantee (the document
        boundary enforces the other half): mutating tools, annotation
        commits, page operations, paste, undo/redo, and the mutating
        menu entries check this before doing anything, so a read-only
        session only ever navigates, zooms, and reads.
        """
        doc = self._document
        return doc is not None and bool(getattr(doc, "read_only", False))

    def _refuse_readonly(self, action: str = "edit") -> bool:
        """Toast a read-only refusal; always returns True (handled)."""
        self._clipboard_feedback(
            "Read-only session",
            "This is a read-only view of a document that is already open. "
            f"It cannot {action}.",
            "info",
        )
        return True

    def _apply_tool_availability(self) -> None:
        """Grey out mutating tools in read-only sessions (and back).

        Idempotent; called at every document attach/detach seam so an
        editable <-> read-only tab switch (including between copies of
        the same file) always leaves the shelf consistent with the live
        session. A read-only attach also falls an active mutating tool
        back to an allowed one; an editable attach restores every tile.
        """
        try:
            if self._document_is_readonly():
                self._shelf.set_tool_availability(_READONLY_TOOL_KINDS)
                active = self._tool_store.active()
                if (active is not None
                        and active.kind not in _READONLY_TOOL_KINDS):
                    self._restore_allowed_tool()
            else:
                self._shelf.set_tool_availability(None)
        except Exception:
            pass

    def _restore_allowed_tool(self) -> None:
        """Re-select a read-only-allowed tool via the normal pathway.

        Prefers the tool the interaction controller still holds (the
        last genuinely applied selection, kept valid by the gate above);
        falls back to the first allowed slot in store order. Going
        through ``ToolShelf.select_slot`` re-syncs the shelf buttons,
        the tool store and the interaction controller as one — never a
        bare store or interaction write.
        """
        try:
            current = self._interaction.active_tool
            if (current is not None
                    and current.kind in _READONLY_TOOL_KINDS
                    and self._tool_store.find(current.id.value) is not None):
                self._shelf.select_slot(current.id.value)
                return
            for slot in self._tool_store.slots:
                if slot.kind in _READONLY_TOOL_KINDS:
                    self._shelf.select_slot(slot.id.value)
                    return
        except Exception:
            pass
        # No allowed slot exists on the shelf (custom-only toolbar): at
        # least leave the shelf visuals consistent with the gate.
        self._refresh_shelf()

    @property
    def document_history(self) -> DocumentHistory | None:
        """Undo history for the open document (created on demand).

        Future tool milestones push Add/Delete commands here when they
        commit annotations. None when no usable document is open.
        """
        return self._history_for(self._document, create=True)

    def _history_for(self, doc, create: bool):
        """History bound to one document's store, or None.

        Histories never cross documents: each entry is keyed by
        DocumentId and dropped when its document closes. A closed or
        missing document yields None so HUD actions can never operate
        on a stale stack.
        """
        try:
            if doc is None or doc.is_closed:
                return None
            key = doc.id.value
        except Exception:
            return None
        try:
            history = self._undo_histories.get(key)
        except Exception:
            return None
        if history is None and create:
            try:
                history = DocumentHistory(doc.annotations)
            except Exception:
                return None
            self._undo_histories[key] = history
        return history

    def _sync_undo_hud(self) -> None:
        """Reflect the current document's undo/redo availability."""
        try:
            history = self._history_for(self._document, create=False)
            self._undo_hud.set_can_undo(
                history.can_undo() if history is not None else False)
            self._undo_hud.set_can_redo(
                history.can_redo() if history is not None else False)
        except Exception:
            pass
        self._sync_unsaved_state()

    def _sync_unsaved_state(self) -> None:
        """Keep the Save affordance and dirty flag in step (M8).

        Single choke point by construction: every content change in this
        viewer — pen/highlighter/shape commits, eraser groups, undo,
        redo, and page operations — ends in ``_sync_undo_hud``. A live
        session is dirty exactly while its undo stack is away from the
        clean index, which is the flag the close/exit guard consults;
        ``can_save`` separately decides whether a Save action exists.
        """
        doc = self._document
        try:
            live = doc is not None and not doc.is_closed
        except Exception:
            live = False
        # Save is offered only where it can genuinely preserve the
        # session (an opened/saved PDF, or a blank after its first path is
        # chosen). A blocked/unsupported manifest disables Save. Dirty
        # tracking stays separate, so every content edit still warns on
        # explicit close / application exit.
        try:
            managed = live and doc.can_save
        except Exception:
            managed = False
        try:
            self._undo_hud.set_can_save(managed)
        except Exception:
            pass
        if not live:
            return
        try:
            history = self._undo_histories.get(doc.id.value)
            clean = True if history is None else history.stack.isClean()
            if clean:
                if doc.is_dirty:
                    doc.clear_dirty()
                    self.documentStateChanged.emit()
            elif not doc.is_dirty and doc.mark_changed():
                self.documentStateChanged.emit()
        except Exception:
            pass

    def mark_document_saved(self, doc=None) -> None:
        """Reset one document's dirty baseline after a successful export."""
        target = self._document if doc is None else doc
        try:
            if target is not None and not target.is_closed:
                history = self._undo_histories.get(target.id.value)
                if history is not None:
                    history.stack.setClean()
                target.clear_dirty()
        except Exception:
            pass
        if target is self._document:
            self._sync_unsaved_state()

    def mark_saved(self) -> None:
        """Reset the active document's dirty baseline (compatibility)."""
        self.mark_document_saved(self._document)

    def capture_reading_position(self) -> dict | None:
        """Capture page/scroll/zoom for the active session."""
        if self._pipeline is None:
            return None
        try:
            layout = self._pipeline.layout
            count = int(layout.page_count)
            if count <= 0:
                return None
            page = self._current_page_index()
            page = 0 if page is None else max(0, min(count - 1, int(page)))
            entry = layout.entry(page)
            render_height = max(1, int(entry.render_height_px))
            _vw, viewport_height = self._render_viewport()
            center = self._scroll_doc() + float(viewport_height) / 2.0
            fraction = (center - float(entry.y_offset_px)) / float(render_height)
            fraction = max(0.0, min(1.0, fraction))
            total_width = max(1.0, float(layout.total_width_px))
            x_fraction = max(
                0.0, min(1.0, self._scroll_doc_x() / total_width))
            zoom = max(0.05, min(16.0, float(self._ui_zoom)))
            return {
                "page": page,
                "page_count": count,
                "fraction": fraction,
                "x_fraction": x_fraction,
                "zoom": zoom,
            }
        except Exception:
            return None

    def persist_reading_position(self, migrate_from: str | None = None) -> None:
        """Store the active session's position under its stable key."""
        doc = self._document
        if doc is None:
            return
        payload = self.capture_reading_position()
        if payload is None:
            return
        try:
            key = doc.reading_key
            self._reading_positions.save(key, payload)
            if migrate_from and migrate_from != key:
                self._reading_positions.remove(migrate_from)
        except Exception:
            pass

    def migrate_reading_position(self, old_key: str, new_key: str) -> None:
        """Move a stored position when an inactive document adopts a path."""
        if not old_key or not new_key or old_key == new_key:
            return
        try:
            payload = self._reading_positions.load(old_key)
            if payload is not None:
                self._reading_positions.save(new_key, payload)
            self._reading_positions.remove(old_key)
        except Exception:
            pass

    def prepare_document_save(self, doc, target_path: str) -> None:
        """Detach an active source renderer before a same-path save.

        Synchronous convenience for non-UI callers. The UI save path uses
        :meth:`save_rebind_plan` + :meth:`finish_save_rebind` instead, so
        the rebind (which joins decoder processes, seconds on a large
        document) can run off the GUI thread.
        """
        if doc is None:
            return
        doc.prepare_for_save(target_path)
        plan = self.save_rebind_plan(doc)
        if plan is None:
            return
        pipeline, render_path = plan
        pipeline.rebind_source(render_path)
        self.finish_save_rebind()

    def save_rebind_plan(self, doc):
        """Return ``(pipeline, render_path)`` when a save must rebind.

        Called on the GUI thread after ``Document.prepare_for_save``; it
        only reads viewer state. The returned work is Qt-free
        (``pipeline.rebind_source`` releases the old engine and joins the
        process-decoder children) and may therefore run on a worker
        thread. ``None`` means the pipeline already reads the document's
        current render path (no rebind needed).
        """
        if doc is None or doc is not self._document or self._pipeline is None:
            return None
        pipeline = self._pipeline
        try:
            render_path = doc.render_path
            if not render_path:
                return None
            current = os.path.normcase(os.path.abspath(pipeline.source_path))
            target = os.path.normcase(os.path.abspath(render_path))
        except Exception:
            try:
                return (pipeline, doc.render_path)
            except Exception:
                return None
        if current == target:
            return None
        return (pipeline, render_path)

    def finish_save_rebind(self) -> None:
        """Refresh the canvas after a (possibly background) save rebind."""
        self._request_update()
        self.update()

    def _restore_reading_position(self, payload) -> None:
        if not payload or self._pipeline is None:
            return
        try:
            layout = self._pipeline.layout
            count = int(layout.page_count)
            if count <= 0:
                return
            page = max(0, min(count - 1, int(payload.get("page", 0))))
            entry = layout.entry(page)
            _vw, viewport_height = self._render_viewport()
            center = (float(entry.y_offset_px)
                      + float(payload.get("fraction", 0.0))
                      * float(entry.render_height_px))
            scale = self._display_scale()
            logical_y = int(round(
                (center - float(viewport_height) / 2.0) * scale))
            maximum = max(0, int(self._scrollbar.maximum()))
            self._scrollbar.setValue(max(0, min(maximum, logical_y)))

            total_width = max(0.0, float(layout.total_width_px))
            logical_x = int(round(
                float(payload.get("x_fraction", 0.0)) * total_width * scale))
            hmaximum = max(0, int(self._hscrollbar.maximum()))
            self._hscrollbar.setValue(max(0, min(hmaximum, logical_x)))
            self._request_update()
            self.update()
        except Exception:
            pass

    def _on_undo_requested(self) -> None:
        # A read-only session owns no mutating history (nothing was ever
        # committed); refuse before touching shared viewer state.
        if self._document_is_readonly():
            self._refuse_readonly("undo changes")
            return
        # M6.8.2: an active group-move preview never survives undo/redo —
        # discard it first (it was never in the store or history) so the
        # history action operates on gesture-free state.
        self._move_cancel()
        # M6.8.2 corrective: the store geometry is about to change under
        # the same selection ids, so the cached frame orientation would be
        # stale — recompute it from the restored content instead.
        self._frame_state = None
        # M6.5: an open eraser group is finalized first so Undo always
        # reverses complete, coherent steps.
        self._finalize_eraser()
        try:
            history = self._history_for(self._document, create=False)
            if history is not None:
                history.undo()
        except Exception:
            pass
        self._sync_undo_hud()
        # M6.3: store mutations from undo/redo are the authoritative
        # annotation state; repaint so committed highlighters appear or
        # disappear (a full update is correct for a one-shot edit and
        # covers annotations on pages that are currently off-view).
        # M6.8.1: undo can remove the selected annotation — drop a stale
        # selection before repainting (selection is never in the history).
        self._revalidate_selection()
        self.update()

    def _on_redo_requested(self) -> None:
        if self._document_is_readonly():
            self._refuse_readonly("redo changes")
            return
        # M6.8.2: same preview-first cancellation as Undo.
        self._move_cancel()
        # M6.8.2 corrective: same stale-orientation reset as Undo.
        self._frame_state = None
        self._finalize_eraser()
        try:
            history = self._history_for(self._document, create=False)
            if history is not None:
                history.redo()
        except Exception:
            pass
        self._sync_undo_hud()
        # M6.8.1: redo can remove (or re-add) the selected annotation.
        self._revalidate_selection()
        self.update()

    def update_chrome_tokens(self, tokens) -> None:
        """Apply a theme switch to shelf chrome (called by MainWindow)."""
        try:
            self._shelf.update_tokens(tokens)
        except Exception:
            pass
        try:
            self._undo_hud.update_tokens(tokens)
        except Exception:
            pass
        try:
            if self._tool_popup is not None:
                self._tool_popup.update_tokens(tokens)
        except Exception:
            pass
        try:
            self._view_controls.update_tokens(tokens)
        except Exception:
            pass
        try:
            self._page_navigator.update_tokens(tokens)
        except Exception:
            pass

    def _persist_tools(self) -> None:
        try:
            self._tool_store.persist()
        except Exception:
            pass
        try:
            self._position_shelf()
        except Exception:
            pass

    def _on_tool_selected(self, slot) -> None:
        # A read-only session keeps the navigation/selection tools only:
        # every annotation-creating or erasing tool is refused here (and
        # greyed out on the shelf), with the frozen annotation store and
        # the document's own guards backing the refusal at the boundary.
        try:
            kind = ToolType(slot.kind)
        except Exception:
            kind = None
        if (self._document_is_readonly() and kind is not None
                and kind not in _READONLY_TOOL_KINDS):
            self._refuse_readonly("edit annotations")
            # The shelf flips the store's active tool BEFORE this handler
            # runs, so the refusal must re-select a valid tool through the
            # normal selection pathway: shelf, tool store and interaction
            # controller must never disagree about what is active.
            self._restore_allowed_tool()
            return
        # A tool switch closes any open properties popup: no stale popup
        # may remain attached to the previously selected tool.
        try:
            self._close_tool_popup()
        except Exception:
            pass
        try:
            # Discard any in-progress pen stroke (M5-B.1): the gesture
            # belongs to the previous tool. Completed strokes persist.
            self._apply_intent(self._interaction.cancel())
        except Exception:
            pass
        # M7.1: a tool switch also drops any pending hold-to-shape dwell or
        # live morph — recognition belongs to the tool that captured the
        # stroke, never to the next one (the cancel above may have already
        # done this; this is the no-open-stroke safety net).
        try:
            if (self._shape_morph is not None
                    or self._shape_move_anchor is not None):
                self._clear_shape_conversion()
        except Exception:
            pass
        # M5-B.1.1.1: shelf selection callbacks capture the ToolInstance
        # built with the buttons, but live edits replace the stored
        # instance — resolve the current configuration by slot id so
        # drawing uses the stored properties, not stale defaults.
        # Fallback is the passed slot itself, never a different slot.
        try:
            fresh = self._tool_store.find(slot.id.value)
            if fresh is not None:
                slot = fresh
        except Exception:
            pass
        try:
            # M6.5: a tool switch must never leave an eraser undo group
            # half-open (the earlier cancel() already finalizes; this is
            # the idempotent safety net for any other exit path).
            self._finalize_eraser()
            # M6.8.2 Stage 5.2: remember what the tool *was* before the
            # switch so the policy below sees a real change.
            self._note_active_tool_kind()
            self._interaction.set_tool(slot)
        except Exception:
            pass
        # M6.8.2 Stage 5.2: any real change of the active tool clears the
        # whole selection (ids, frame, handles, cursor, animation).
        self._sync_selection_tool_policy()
        # M6.8.2 Stage 5.2: a tool switch drops any selection cursor (the
        # new tool owns the pointer); the next move re-derives it.
        self._reset_hover_cursor()
        try:
            self._tool_store.persist()
        except Exception:
            pass
        # Tool switch changes cursor/chrome only — no pipeline work.
        self.update()

    def _refresh_shelf(self) -> None:
        try:
            self._shelf.refresh()
        except Exception:
            pass
        try:
            # M6.5: shelf refresh can change the active tool as well;
            # never leave an eraser undo group half-open.
            self._finalize_eraser()
            # M6.8.2 Stage 5.2: a refresh can switch the active tool too, so
            # note the previous kind before re-applying it.
            self._note_active_tool_kind()
            self._interaction.set_tool(self._tool_store.active())
        except Exception:
            pass
        # M6.8.2 Stage 5.2: a shelf refresh can also switch the active tool,
        # which clears the whole selection (see _sync_selection_tool_policy).
        self._sync_selection_tool_policy()
        # Tool cursors: the refresh may have switched the active tool (or its
        # properties) — keep the pointer's cursor truthful.
        self._refresh_tool_cursor()
        try:
            self._tool_store.persist()
        except Exception:
            pass
        try:
            self._position_shelf()
        except Exception:
            pass
        self.update()

    def _tool_tokens(self):
        try:
            from app.theme.tokens import dark_tokens

            tokens = dark_tokens()
            window = self.window()
            if window is not None and hasattr(window, "_theme"):
                try:
                    tokens = window._theme.tokens
                except Exception:
                    pass
            return tokens
        except Exception:
            from app.theme.tokens import dark_tokens

            return dark_tokens()

    def _close_tool_popup(self) -> None:
        popup, self._tool_popup = self._tool_popup, None
        self._tool_popup_launcher = None
        self._tool_popup_slot = None
        try:
            self._shelf.set_options_open(False)
        except Exception:
            pass
        if popup is not None:
            try:
                popup.close_popup(animate=False)
            except Exception:
                pass
            try:
                popup.setParent(None)
                popup.deleteLater()
            except Exception:
                pass
        # M7.1.4.2: a closed child popup hands the pointer back to the
        # canvas without necessarily crossing a widget boundary, so force
        # the current tool/selection cursor back (never onto UI chrome).
        self._restore_canvas_cursor_under_pointer()

    def _open_properties_popup(self, slot, launcher) -> bool:
        """Anchored properties popup (double-click AND menu Edit path)."""
        try:
            from tools.models import has_editable_properties

            if not has_editable_properties(slot):
                return False
        except Exception:
            return False
        try:
            from app.toolshelf.tool_popup import PropertiesForm, ToolPopup

            self._close_tool_popup()
            # M5-B.1.1: shelf callbacks capture the ToolInstance built
            # with the buttons, but live edits replace the stored
            # instance — resolve the current configuration by slot id so
            # the popup never initializes from stale values. Fallback is
            # the passed slot itself, never a different slot.
            try:
                fresh = self._tool_store.find(slot.id.value)
                if fresh is not None:
                    slot = fresh
            except Exception:
                pass
            popup = ToolPopup(self._tool_tokens(), self)
            # M7.1.4.2: the popup is a child of the canvas; its ordinary
            # controls get the UI arrow once the content exists (their own
            # explicit cursors still win), so no drawing cursor can leak.
            # The integrated selected-tool capsule toggles THIS popup:
            # treat a press on the three-dot control as internal so the
            # toggle handler owns open/close.
            try:
                popup.set_toggle_source(getattr(self._shelf, "dots", None))
            except Exception:
                pass
            form = PropertiesForm(slot, self._tool_tokens(), popup)
            form.changed.connect(self._on_tool_live_changed)
            form.removeRequested.connect(
                self._on_tool_remove_from_properties)
            popup.set_content(form)
            apply_ui_cursor(popup)
            popup.closed.connect(self._on_tool_popup_closed)
            self._tool_popup = popup
            self._tool_popup_launcher = launcher
            self._tool_popup_slot = slot
            try:
                rect = launcher.rect()
                origin = launcher.mapToGlobal(rect.topLeft())
                from PySide6.QtCore import QRect as _QRect

                launcher_global = _QRect(origin, rect.size())
            except Exception:
                launcher_global = None
            try:
                horizontal = self._shelf._orientation() == "horizontal"
            except Exception:
                horizontal = None
            popup.show_near(launcher_global, self,
                            prefer_horizontal=horizontal)
            # The options control reads as open (one stronger fill, no
            # ring) for as long as this popup lives.
            try:
                self._shelf.set_options_open(True)
            except Exception:
                pass
            return True
        except Exception:
            self._close_tool_popup()
            return False

    def _on_tool_popup_closed(self) -> None:
        # The properties popup is gone, whatever closed it (the three-dot
        # toggle, Escape, or the outside-click filter). The capsule's
        # active highlight therefore belongs back on the selected tool.
        try:
            self._shelf.set_options_open(False)
        except Exception:
            pass
        # Flush pending live edits: closing keeps applied changes.
        try:
            self._flush_tool_persist()
        except Exception:
            pass
        if self._tool_popup is not None:
            try:
                popup, self._tool_popup = self._tool_popup, None
                popup.setParent(None)
                popup.deleteLater()
            except Exception:
                self._tool_popup = None
        # M7.1.4.2: closing the popup returns the pointer to the canvas;
        # re-derive its cursor now (the forced re-set is what makes this
        # visible even though the tool itself did not change).
        self._restore_canvas_cursor_under_pointer()

    def _reanchor_tool_popup(self) -> None:
        """Keep an open popup glued to its launcher (never stale)."""
        popup = self._tool_popup
        launcher = self._tool_popup_launcher
        if popup is None or launcher is None:
            return
        try:
            if not popup.isVisible():
                return
        except Exception:
            return
        try:
            from PySide6.QtCore import QRect as _QRect

            rect = launcher.rect()
            origin = launcher.mapToGlobal(rect.topLeft())
            launcher_global = _QRect(origin, rect.size())
        except Exception:
            return
        try:
            from app.toolshelf.tool_popup import stop_widget_anims

            stop_widget_anims(popup)
        except Exception:
            pass
        try:
            popup.show_near(launcher_global, self)
        except Exception:
            pass

    def _on_tool_edit_requested(self, slot, launcher=None) -> None:
        """Menu Edit AND double-click share the anchored popup.

        The selected-tool three-dot control is a *toggle*: it reuses this
        exact flow, but when the popup already open belongs to the slot
        it is pressed for, the click closes that popup instead of
        re-opening it. The popup's outside-click filter treats that
        control as internal, so this handler is the only actor (no
        fighting the dismissal filter, no double toggle).
        """
        try:
            dots = getattr(self._shelf, "dots", None)
            is_capsule = launcher is not None and launcher is dots
        except Exception:
            is_capsule = False
        if is_capsule and self._tool_popup is not None:
            current = self._tool_popup_slot
            try:
                same = (current is not None
                        and current.id.value == slot.id.value)
            except AttributeError:
                same = False
            if same:
                self._close_tool_popup()
                return
        if launcher is None:
            try:
                button = self._shelf._buttons.get(slot.id.value)
                launcher = button if button is not None else self._shelf
            except Exception:
                launcher = self._shelf
        self._open_properties_popup(slot, launcher)

    def _on_tool_live_changed(self, updated) -> None:
        """One live inspector edit: store + button + preview, persist later."""
        try:
            slot_id = updated.id.value
        except AttributeError:
            return
        try:
            self._tool_store.update_slot(
                slot_id, name=updated.name, color=updated.color,
                width_mm=updated.width_mm, opacity=updated.opacity,
                pressure=updated.pressure, smoothing=updated.smoothing,
                fill_enabled=updated.fill_enabled,
                fill_color=updated.fill_color,
                fill_opacity=updated.fill_opacity,
                line_style=updated.line_style,
                start_ending=updated.start_ending,
                end_ending=updated.end_ending, square=updated.square,
                circle=updated.circle, snap_angle=updated.snap_angle,
                connect_endpoints=updated.connect_endpoints,
                connect_intersections=updated.connect_intersections,
                close_path=updated.close_path,
                straight_line=updated.straight_line,
                eraser_mode=normalize_eraser_mode(
                    getattr(updated, "eraser_mode", DEFAULT_ERASER_MODE)))
        except (KeyError, ValueError, AttributeError):
            return
        # In-place button repaint (no shelf rebuild: the popup anchor
        # launcher must stay alive while the inspector is open).
        try:
            button = self._shelf._buttons.get(slot_id)
            if button is not None:
                button.update_slot(self._tool_store.find(slot_id))
        except Exception:
            pass
        # Active tool edits reach the interaction controller at once.
        try:
            if slot_id == self._tool_store.active_id:
                self._interaction.set_tool(self._tool_store.active())
                # Tool cursors: color/width/opacity edits refresh the cursor now —
                # no mouse move required.
                self._refresh_tool_cursor()
        except Exception:
            pass
        self._schedule_tool_persist()

    def _schedule_tool_persist(self) -> None:
        try:
            self._tool_persist_timer.start()
        except Exception:
            pass

    def _flush_tool_persist(self) -> None:
        try:
            self._tool_persist_timer.stop()
        except Exception:
            pass
        try:
            self._tool_store.persist()
        except Exception:
            pass

    def _on_tool_duplicate_requested(self, slot) -> None:
        try:
            self._tool_store.duplicate_slot(slot.id.value)
        except (KeyError, ValueError):
            return
        self._refresh_shelf()

    def _on_tool_remove_requested(self, slot) -> None:
        try:
            slot_id = slot.id.value
        except AttributeError:
            return
        # A properties popup bound to this instance is invalid the moment
        # the slot disappears — close it before the store mutation.
        try:
            popup_slot = self._tool_popup_slot
            if (popup_slot is not None
                    and popup_slot.id.value == slot_id):
                self._close_tool_popup()
        except Exception:
            pass
        try:
            was_active = slot_id == self._tool_store.active_id
            self._tool_store.remove_slot(slot_id)
        except (KeyError, ValueError):
            return
        self._refresh_shelf()
        if was_active:
            # Store already fell back to the first surviving slot (core
            # Pan/Selection are permanent). Route that fallback through
            # the normal selection path so the interaction controller,
            # cursor, selection policy and persisted active id all agree.
            fallback = self._tool_store.active()
            if fallback is not None:
                try:
                    self._on_tool_selected(fallback)
                except Exception:
                    pass

    def _on_tool_remove_from_properties(self, slot) -> None:
        """Properties-header trash: remove this toolbar instance only."""
        try:
            from tools.models import is_core_tool

            if is_core_tool(slot):
                return
        except Exception:
            pass
        self._close_tool_popup()
        self._on_tool_remove_requested(slot)

    # -- Add Tool ----------------------------------------------------------

    def _on_add_requested(self, launcher=None) -> None:
        """Open the anchored AddToolGrid popup (no pipeline work)."""
        try:
            from app.toolshelf.tool_popup import AddToolGrid, ToolPopup

            self._close_tool_popup()
            popup = ToolPopup(self._tool_tokens(), self)
            grid = AddToolGrid(self._tool_tokens(), popup)
            grid.chosen.connect(self._on_add_tool_chosen)
            popup.set_content(grid)
            apply_ui_cursor(popup)
            popup.closed.connect(self._on_tool_popup_closed)
            self._tool_popup = popup
            if launcher is None:
                launcher = getattr(self._shelf, "_add", None) or self._shelf
            self._tool_popup_launcher = launcher
            self._tool_popup_slot = None
            try:
                rect = launcher.rect()
                origin = launcher.mapToGlobal(rect.topLeft())
                from PySide6.QtCore import QRect as _QRect

                launcher_global = _QRect(origin, rect.size())
            except Exception:
                launcher_global = None
            try:
                horizontal = self._shelf._orientation() == "horizontal"
            except Exception:
                horizontal = None
            popup.show_near(launcher_global, self,
                            prefer_horizontal=horizontal)
        except Exception:
            self._close_tool_popup()

    def _on_add_tool_chosen(self, kind) -> None:
        try:
            from app.toolshelf.store import default_tool_config

            defaults = default_tool_config(kind)
            slot = self._tool_store.add_slot(
                defaults.kind, name=defaults.name, color=defaults.color,
                width_mm=defaults.width_mm, opacity=defaults.opacity,
                pressure=defaults.pressure, smoothing=defaults.smoothing,
                fill_enabled=defaults.fill_enabled,
                fill_color=defaults.fill_color,
                fill_opacity=defaults.fill_opacity,
                line_style=defaults.line_style,
                start_ending=defaults.start_ending,
                end_ending=defaults.end_ending, square=defaults.square,
                circle=defaults.circle)
        except (KeyError, ValueError):
            self._close_tool_popup()
            return
        self._close_tool_popup()
        self._refresh_shelf()
        try:
            self._shelf.select_slot(slot.id.value)
        except Exception:
            pass

    # -- toolbar repositioning ----------------------------------------------

    def _dock_target_positions(self) -> dict:
        # Center points of the edge zone overlays (kept for compat).
        rects = self._dock_zone_rects()
        out = {}
        for dock, rect in rects.items():
            try:
                out[dock] = (rect.x() + rect.width() // 2,
                             rect.y() + rect.height() // 2)
            except Exception:
                out[dock] = (0, 0)
        return out

    def _dock_zone_rects(self) -> dict:
        """Toolbar-footprint dock zones (never text buttons).

        Derived from the SAME placement anchors as the committed and
        preview toolbar geometry: each zone wraps its dock's placement
        footprint (orientation-correct via _shelf_placement) with a
        small halo pad, so highlight, preview and commit always agree.
        No parallel hard-coded rectangles, no center region.
        """
        from PySide6.QtCore import QRect as _QRect

        w, h = max(1, self.width()), max(1, self.height())
        pad = 16
        halo = 32
        rects = {}
        for dock in ("top", "bottom", "left", "right"):
            try:
                x, y, sw, sh = self._shelf_placement(dock)
            except Exception:
                continue
            cx, cy = x + sw // 2, y + sh // 2
            if dock in ("top", "bottom"):
                span = sw + halo * 2
                zx = min(max(0, cx - span // 2), max(0, w - span))
                if dock == "top":
                    rects[dock] = _QRect(zx, 0, span, y + sh + pad)
                else:
                    zy = max(0, y - pad)
                    rects[dock] = _QRect(zx, zy, span, h - zy)
            else:
                span = sh + halo * 2
                zy = min(max(0, cy - span // 2), max(0, h - span))
                if dock == "left":
                    rects[dock] = _QRect(0, zy, x + sw + pad, span)
                else:
                    zx = max(0, x - pad)
                    rects[dock] = _QRect(zx, zy, x + sw + pad - zx, span)
        return rects

    def _show_dock_targets(self) -> None:
        self._hide_dock_targets()
        try:
            from app import fx as _fx

            rects = self._dock_zone_rects()
            for dock in ("top", "left", "right", "bottom"):
                zone = _DockZone(dock, self)
                try:
                    rect = rects[dock]
                    zone.setGeometry(rect)
                except Exception:
                    pass
                zone.show()
                try:
                    _fx.slide_fade_in(zone, 0, 160, zone, dy_px=-6)
                except Exception:
                    pass
                self._dock_targets.append(zone)
            self._highlight_dock_target(None)
        except Exception:
            pass

    def _hide_dock_targets(self) -> None:
        targets, self._dock_targets = self._dock_targets, []
        for chip in targets:
            try:
                from app.toolshelf.tool_popup import stop_widget_anims

                stop_widget_anims(chip)
            except Exception:
                pass
            try:
                chip.setParent(None)
                chip.deleteLater()
            except Exception:
                pass

    def _highlight_dock_target(self, dock: str | None) -> None:
        for chip in self._dock_targets:
            try:
                is_active = (chip.property("dock_target") == dock)
                if getattr(chip, "set_active_target", None) == is_active:
                    continue
                chip.set_active_target = is_active
                try:
                    chip.set_active(is_active)
                except Exception:
                    try:
                        chip.update()
                    except Exception:
                        pass
            except Exception:
                pass

    def _nearest_dock(self, global_pos) -> str | None:
        try:
            local = self.mapFromGlobal(global_pos)
        except Exception:
            return None
        w, h = max(1, self.width()), max(1, self.height())
        distances = {
            "top": local.y(),
            "bottom": h - local.y(),
            "left": local.x(),
            "right": w - local.x(),
        }
        return min(distances, key=lambda dock: distances[dock])

    def _on_reposition_started(self) -> None:
        # A docking drag invalidates popup anchoring: close cleanly first.
        try:
            self._close_tool_popup()
        except Exception:
            pass
        self._docking = True
        self._dock_preview = None
        self._show_dock_targets()
        self._highlight_dock_target(None)

    def _on_reposition_moved(self, global_pos) -> None:
        if not self._docking:
            return
        dock = self._nearest_dock(global_pos)
        if dock != self._dock_preview:
            self._dock_preview = dock
            try:
                self._preview_shelf_dock(dock)
            except Exception:
                pass
            # Zones derive from placement anchors: rebuild them now so
            # the highlight tracks the preview orientation/size, then
            # re-assert the hovered target on the fresh zones.
            try:
                self._show_dock_targets()
            except Exception:
                pass
            self._highlight_dock_target(dock)
        # Real-time follow: the toolbar itself tracks the pointer
        # (clamped to the viewer); release snaps to the preview dock.
        try:
            self._follow_cursor_with_shelf(global_pos)
        except Exception:
            pass

    def _follow_cursor_with_shelf(self, global_pos) -> None:
        """Move the shelf with the cursor during repositioning."""
        try:
            local = self.mapFromGlobal(global_pos)
        except Exception:
            return
        try:
            shelf = self._shelf
            sw, sh = max(1, shelf.width()), max(1, shelf.height())
            x = min(max(0, local.x() - sw // 2),
                    max(0, self.width() - sw))
            y = min(max(0, local.y() - sh // 2),
                    max(0, self.height() - sh))
            shelf.move(int(x), int(y))
            shelf.raise_()
        except Exception:
            pass

    def _on_reposition_finished(self) -> None:
        was_docking, self._docking = self._docking, False
        preview, self._dock_preview = self._dock_preview, None
        self._hide_dock_targets()
        if not was_docking:
            return
        if preview is not None and preview != self._tool_store.dock:
            try:
                self._shelf.set_dock(preview)
            except (KeyError, ValueError):
                pass
        else:
            try:
                self._shelf.cancel_preview()
            except Exception:
                pass
            try:
                self._position_shelf()
            except Exception:
                pass

    def _on_reposition_cancelled(self) -> None:
        """Esc during reposition: revert to the stored dock, no commit."""
        self._docking = False
        self._dock_preview = None
        self._hide_dock_targets()
        try:
            self._shelf.cancel_preview()
        except Exception:
            pass
        try:
            self._position_shelf()
        except Exception:
            pass

    def _preview_shelf_dock(self, dock: str | None) -> None:
        """Preview a dock WITHOUT touching the store (orientation flips).

        Position stays cursor-driven (live follow); only the
        orientation/size preview applies until release commits.
        """
        if dock is None:
            return
        try:
            self._shelf.preview_dock(dock)
            self._shelf.raise_()
        except Exception:
            pass

    # -- custom-tool reorder + trash (commit on release only) ---------------

    def _on_slot_reorder_started(self, slot) -> None:
        try:
            from tools.models import is_core_tool

            if is_core_tool(slot):
                return
        except Exception:
            return
        self._reordering = True
        self._reorder_slot = slot
        self._reorder_index = None
        self._reorder_over_trash = False
        try:
            from PySide6.QtWidgets import QLabel

            button = self._shelf._buttons.get(slot.id.value)
            ghost = QLabel(self)
            if button is not None:
                try:
                    ghost.setPixmap(button.grab())
                except Exception:
                    pass
            ghost.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            ghost.setStyleSheet("background: transparent; border: none;")
            try:
                ghost.setWindowOpacity(0.55)
            except Exception:
                pass
            ghost.setVisible(True)
            ghost.raise_()
            self._reorder_ghost = ghost
        except Exception:
            pass
        self._show_reorder_trash()

    def _trash_position(self) -> tuple[int, int]:
        """Small trash sits adjacent to the shelf (never center-screen)."""
        try:
            geo = self._shelf.geometry()
            sx, sy, sw, sh = geo.x(), geo.y(), geo.width(), geo.height()
        except Exception:
            sw, sh, sx, sy = 56, 200, 12, 100
        size, gap = 40, 10
        cy = max(0, sy + (sh - size) // 2)
        # Prefer the shelf's outer side; fall back inside the window.
        candidates = [sx + sw + gap, max(0, sx - gap - size)]
        for x in candidates:
            if 0 <= x <= max(0, self.width() - size):
                return int(x), int(cy)
        return int(max(0, min(max(0, self.width() - size),
                             sx + sw + gap))), int(cy)

    def _show_reorder_trash(self) -> None:
        self._hide_reorder_trash()
        try:
            from app import fx as _fx
            from app.icons import icon as _icon_fn
            from app.modal import ModalButton

            trash = ModalButton("", circular=True, parent=self, danger=True)
            try:
                trash.setIcon(_icon_fn("trash", "#e5484d", 20))
            except Exception:
                pass
            trash.setProperty("reorder_trash", True)
            trash.setAccessibleName("Delete dragged tool")
            trash.setText("")
            trash.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            trash.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            trash.setFixedSize(40, 40)
            x, y = self._trash_position()
            trash.move(max(0, x), max(0, y))
            trash.show()
            trash.raise_()
            try:
                _fx.slide_fade_in(trash, 0, 160, trash, dy_px=8)
            except Exception:
                pass
            self._reorder_trash = trash
            self._style_reorder_trash(False)
        except Exception:
            pass

    def _style_reorder_trash(self, hovered: bool) -> None:
        trash = self._reorder_trash
        if trash is None:
            return
        try:
            from app.icons import icon as _icon_fn

            tokens = self._tool_tokens()
            if hovered:
                trash.setIcon(_icon_fn("trash", "#ffffff", 20))
                trash.setStyleSheet(
                    "QPushButton { color: #ffffff;"
                    " background-color: #e5484d;"
                    " border: 2px solid #ff6369;"
                    " border-radius: 20px; }")
            else:
                trash.setIcon(_icon_fn("trash", "#e5484d", 20))
                trash.setStyleSheet(
                    f"QPushButton {{ color: {tokens.text_secondary};"
                    f" background-color: {tokens.surface_raised};"
                    f" border: 1px solid {tokens.border};"
                    f" border-radius: 20px; }}")
        except Exception:
            pass

    def _hide_reorder_trash(self) -> None:
        trash, self._reorder_trash = self._reorder_trash, None
        if trash is not None:
            try:
                from app.toolshelf.tool_popup import stop_widget_anims

                stop_widget_anims(trash)
            except Exception:
                pass
            try:
                trash.setParent(None)
                trash.deleteLater()
            except Exception:
                pass

    def _on_slot_reorder_moved(self, global_pos) -> None:
        if not self._reordering:
            return
        try:
            ghost = self._reorder_ghost
            if ghost is not None:
                local = self.mapFromGlobal(global_pos)
                ghost.move(local.x() - ghost.width() // 2,
                           local.y() - ghost.height() // 2)
                ghost.raise_()
        except Exception:
            pass
        try:
            trash = self._reorder_trash
            over = False
            if trash is not None:
                try:
                    over = trash.geometry().contains(
                        self.mapFromGlobal(global_pos))
                except Exception:
                    over = False
            if over != self._reorder_over_trash:
                self._reorder_over_trash = over
                self._style_reorder_trash(over)
        except Exception:
            pass
        try:
            idx = self._reorder_insertion_index(global_pos)
            self._reorder_index = idx
            self._place_reorder_indicator(idx)
        except Exception:
            pass

    def _custom_buttons_in_order(self) -> list:
        """Custom slot buttons in store order (core excluded)."""
        try:
            from tools.models import is_core_tool

            out = []
            for slot in self._tool_store.slots:
                if is_core_tool(slot):
                    continue
                button = self._shelf._buttons.get(slot.id.value)
                if button is not None:
                    out.append(button)
            return out
        except Exception:
            return []

    def _reorder_insertion_index(self, global_pos) -> int:
        buttons = self._custom_buttons_in_order()
        if not buttons:
            return 0
        try:
            local = self._shelf.mapFromGlobal(global_pos)
        except Exception:
            return len(buttons)
        horizontal = self._shelf._orientation() == "horizontal"
        coord = local.x() if horizontal else local.y()
        for i, button in enumerate(buttons):
            try:
                # Buttons live inside the scroll viewport's inner widget:
                # their x()/y() are inner coordinates (shifted by viewport
                # offset + scroll position), so map each center into shelf
                # space before comparing with the shelf-space pointer.
                pt = button.mapTo(self._shelf, button.rect().center())
                center = pt.x() if horizontal else pt.y()
            except Exception:
                continue
            if coord < center:
                return i
        return len(buttons)

    def _place_reorder_indicator(self, idx: int | None) -> None:
        self._hide_reorder_indicator()
        if idx is None or not self._reordering:
            return
        buttons = self._custom_buttons_in_order()
        if not buttons:
            return
        idx = max(0, min(len(buttons), int(idx)))
        horizontal = self._shelf._orientation() == "horizontal"
        try:
            from PySide6.QtWidgets import QWidget

            indicator = QWidget(self._shelf)
            try:
                accent = QColor(self._tool_tokens().accent)
                if not accent.isValid():
                    accent = QColor("#8B93E0")
            except Exception:
                accent = QColor("#8B93E0")
            indicator.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            indicator.setStyleSheet(
                f"background-color: {accent.name()}; border-radius: 2px;")
            try:
                anchors = [b.mapTo(self._shelf, b.rect().center())
                           for b in buttons]
            except Exception:
                anchors = []
            if horizontal:
                indicator.setFixedSize(4, 36)
                if idx <= 0:
                    gx = (anchors[0].x() - 20 - 5) if anchors else 0
                elif idx >= len(buttons):
                    gx = (anchors[-1].x() + 20 + 1) if anchors else 0
                else:
                    gx = ((anchors[idx - 1].x() + anchors[idx].x()) // 2 - 2
                          if len(anchors) > idx else 0)
                indicator.move(max(0, gx), (self._shelf.height() - 36) // 2)
            else:
                indicator.setFixedSize(36, 4)
                if idx <= 0:
                    gy = (anchors[0].y() - 20 - 5) if anchors else 0
                elif idx >= len(buttons):
                    gy = (anchors[-1].y() + 20 + 1) if anchors else 0
                else:
                    gy = ((anchors[idx - 1].y() + anchors[idx].y()) // 2 - 2
                          if len(anchors) > idx else 0)
                indicator.move((self._shelf.width() - 36) // 2, max(0, gy))
            indicator.show()
            indicator.raise_()
            self._reorder_indicator = indicator
        except Exception:
            pass

    def _hide_reorder_indicator(self) -> None:
        indicator, self._reorder_indicator = self._reorder_indicator, None
        if indicator is not None:
            try:
                indicator.setParent(None)
                indicator.deleteLater()
            except Exception:
                pass

    def _hide_reorder_ghost(self) -> None:
        ghost, self._reorder_ghost = self._reorder_ghost, None
        if ghost is not None:
            try:
                ghost.setParent(None)
                ghost.deleteLater()
            except Exception:
                pass

    def _on_slot_reorder_finished(self) -> None:
        slot, self._reorder_slot = self._reorder_slot, None
        was_reordering, self._reordering = self._reordering, False
        over_trash = self._reorder_over_trash
        index = self._reorder_index
        self._reorder_index = None
        self._reorder_over_trash = False
        self._hide_reorder_ghost()
        self._hide_reorder_trash()
        self._hide_reorder_indicator()
        if not was_reordering or slot is None:
            return
        if over_trash:
            # Drop on trash: remove the custom tool (core is immune:
            # remove_slot raises, so a core drop can never delete).
            try:
                self._tool_store.remove_slot(slot.id.value)
            except (KeyError, ValueError):
                return
            self._refresh_shelf()
            return
        try:
            from tools.models import is_core_tool

            customs = [s for s in self._tool_store.slots
                       if not is_core_tool(s)]
            ids = [s.id.value for s in customs]
            if slot.id.value not in ids or index is None:
                return
            ncore = len(self._tool_store.slots) - len(customs)
            old = ids.index(slot.id.value)
            if int(index) == old:
                return  # dropped back home: no-op, no refresh flicker
            # The index was measured with the dragged item still in place;
            # after its removal, later gaps shift down by one.
            to = int(index) - (1 if old < int(index) else 0)
            self._tool_store.move_slot(slot.id.value, ncore + to)
        except (KeyError, ValueError, AttributeError):
            return
        self._refresh_shelf()
        try:
            self._tool_store.persist()
        except Exception:
            pass

    def _on_slot_reorder_cancelled(self) -> None:
        # Esc: nothing was committed (commit happens on release only).
        self._reorder_slot = None
        self._reordering = False
        self._reorder_index = None
        self._reorder_over_trash = False
        self._hide_reorder_ghost()
        self._hide_reorder_trash()
        self._hide_reorder_indicator()

    # -- document lifecycle ------------------------------------------------

    def open_document(self, path: str) -> bool:
        """Open a PDF and start rendering its first viewport."""
        self.close_document()
        try:
            doc = self._manager.open(path)
            pages = [doc.page_info(i) for i in range(doc.page_count)]
        except InkfallError as exc:
            self._error = str(exc)
            self.update()
            return False
        except Exception as exc:  # never leak backend specifics to the UI
            self._error = f"Cannot open document: {exc}"
            self.update()
            return False
        return self._start_session(doc, pages)

    def open_managed_document(self, doc) -> bool:
        """Open an already-managed document (M8 blank workspace).

        The blank session is created by ``DocumentManager`` before any
        path exists; this variant seeds the viewer from it without a
        second manager open (which would key on a path that does not
        exist). Everything else — pipeline, scroll, undo HUD, fit,
        view controls — is the same session start as a normal open.
        """
        self.close_document()
        if doc is None or doc.is_closed:
            self._error = "Cannot open document."
            self.update()
            return False
        try:
            pages = [doc.page_info(i) for i in range(doc.page_count)]
        except Exception as exc:
            self._error = f"Cannot open document: {exc}"
            self.update()
            return False
        return self._start_session(doc, pages)

    def _start_session(self, doc, pages) -> bool:
        """Build the pipeline for ``doc`` and seed the viewer session."""
        try:
            position = self._reading_positions.load(doc.reading_key)
        except Exception:
            position = None
        try:
            zoom = float(position.get("zoom", 1.0)) if position else 1.0
        except (TypeError, ValueError):
            zoom = 1.0
        zoom = max(0.05, min(16.0, zoom))
        try:
            self._pipeline = DocumentPipeline(
                doc.id, doc.render_path, pages, ui_zoom=zoom,
                user_rotation=0
            )
        except Exception as exc:
            try:
                self._manager.close(doc.id)
            except Exception:
                pass
            self._error = f"Cannot open document: {exc}"
            self.update()
            return False
        self._document = doc
        self._ui_zoom = zoom
        self._error = None
        self._sync_scrollbar(preserve_fraction=False)
        self._restore_reading_position(position)
        self._request_update()
        self._timer.start()
        self._sync_undo_hud()
        self._active_fit = None
        try:
            self._view_controls.set_available(True)
            self._view_controls.reposition()
            self._sync_view_controls()
        except Exception:
            pass
        try:
            self._page_navigator.set_available(True)
            self._page_navigator.reposition()
        except Exception:
            pass
        self._apply_tool_availability()
        self.update()
        return True

    # -- tab sessions (Phase 2): suspend / resume without closing ---------

    def capture_session_state(self) -> dict:
        """In-memory view state for a tab that is about to be suspended.

        Reading position is included so reactivation restores instantly
        without a QSettings round-trip; the same payload is also persisted
        under the document's stable key at suspension time.
        """
        return {
            "fit": self._active_fit,
            "appearance": self._pdf_appearance,
            "reading": self.capture_reading_position(),
        }

    @property
    def gesture_live(self) -> bool:
        """True while a stroke/drag/selection gesture is in progress.

        Used by the fullscreen tab-strip reveal: resizing the canvas while
        a gesture is live would corrupt its coordinate frame, so the
        reveal is suppressed until the gesture resolves.
        """
        try:
            return bool(
                self._tablet_stroke_open
                or self._mouse_stroke_open
                or self._eraser_tablet_open
                or self._eraser_mouse_open
                or self._shape_tablet_open
                or self._shape_mouse_open
                or self._selection_tablet_open
                or self._selection_mouse_open
                or self._pan_tablet_open
                or self._region_gesture is not None
                or self._move_gesture is not None
            )
        except Exception:
            return False

    def set_pipeline_retirer(self, retirer) -> None:
        """Install an off-thread retiree for detached pipelines.

        ``retirer(pipeline)`` is called instead of the inline
        ``pipeline.close()`` whenever a session swap or teardown releases
        a pipeline; it typically hands the close (engine thread join +
        process-decoder shutdown) to a background pool. ``None`` restores
        the inline close (deterministic standalone/test behavior).
        """
        self._pipeline_retirer = retirer

    def _retire_pipeline(self, pipeline) -> None:
        """Release a detached pipeline (background pool when installed)."""
        if pipeline is None:
            return
        retirer = self._pipeline_retirer
        if retirer is not None:
            try:
                retirer(pipeline)
                return
            except Exception:
                pass
        try:
            pipeline.close()
        except Exception:
            pass

    def is_loading(self) -> bool:
        """True while an async session switch is being prepared."""
        return self._loading_title is not None

    def enter_loading(self, title: str | None = None) -> None:
        """Show the quiet loading canvas for an async session switch.

        The outgoing session stays ATTACHED and internally consistent
        (restorable instantly if the request fails or is superseded), but
        the canvas paints only the plain document background — no text,
        no spinner, no animation — so the previous PDF is never presented
        under the new tab's title. Canvas input is disabled for the
        veil's lifetime (an edit or scroll would land on the outgoing
        document while the destination's tab is the one highlighted);
        window chrome, tabs and menus stay interactive. The poll timer
        stops (no pipeline work is submitted for a hidden document).
        ``title`` is accepted for API stability and is never painted.
        """
        self._loading_title = str(title or "Untitled")
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            self.setEnabled(False)
        except Exception:
            pass
        self.update()

    def exit_loading(self) -> None:
        """Leave the loading veil (commit, cancel or failure path)."""
        self._loading_title = None
        try:
            self.setEnabled(True)
        except Exception:
            pass
        if self._pipeline is not None:
            self._timer.start()
        self.update()

    def _resolve_position(self, doc, state: dict):
        """Position payload + clamped zoom for ``doc``.

        The reading-position store is a GUI-thread resource; background
        preparation resolves BEFORE dispatching and passes the payload
        across. ``state["reading"]`` (captured view state) wins over a
        store round-trip, exactly like the historical switch path.
        """
        position = state.get("reading")
        if position is None:
            try:
                position = self._reading_positions.load(doc.reading_key)
            except Exception:
                position = None
        try:
            zoom = float(position.get("zoom", 1.0)) if position else 1.0
        except (TypeError, ValueError):
            zoom = 1.0
        zoom = max(0.05, min(16.0, zoom))
        return position, zoom

    def resolve_session_zoom(self, doc, incoming_state: dict | None = None) -> float:
        """GUI-thread zoom resolution for background preparation.

        The reading-position store is a GUI-thread resource, so the
        window resolves the zoom BEFORE dispatching the worker; the
        value crosses to the pool thread as plain data.
        """
        state = incoming_state if isinstance(incoming_state, dict) else {}
        _position, zoom = self._resolve_position(doc, state)
        return zoom

    def switch_document(self, doc, incoming_state: dict | None = None):
        """Bind ``doc`` as the live session without closing any document.

        Synchronous variant (reference implementation; used by callers
        that must complete inline). The target pipeline is built on the
        calling thread BEFORE the outgoing session is touched, so a
        failure returns ``(False, None)`` with the current session fully
        intact. The window's tab switching uses the asynchronous
        preparation path (:meth:`commit_prepared_session` fed by a
        background worker) and does not come through here.

        Returns ``(success, outgoing_state)`` where ``outgoing_state`` is
        the captured state to store on the departing tab record (``None``
        when no session was live).
        """
        if doc is None or getattr(doc, "is_closed", True):
            return (False, None)
        try:
            pages = [doc.page_info(i) for i in range(doc.page_count)]
        except Exception:
            return (False, None)
        state = incoming_state if isinstance(incoming_state, dict) else {}
        _position, zoom = self._resolve_position(doc, state)
        try:
            pipeline = DocumentPipeline(
                doc.id, doc.render_path, pages, ui_zoom=zoom,
                user_rotation=0
            )
        except Exception:
            return (False, None)
        return self.commit_prepared_session(doc, pipeline, state, zoom)

    def commit_prepared_session(self, doc, pipeline,
                                incoming_state: dict | None = None,
                                zoom: float | None = None):
        """Attach a PREBUILT pipeline on the GUI thread.

        The suspend/attach tail of :meth:`switch_document`, shared by the
        synchronous path and the asynchronous preparation path: the
        pipeline was built off-thread, this runs on the GUI thread and
        touches only widgets. ``incoming_state`` carries the tab's view
        state (fit/appearance/reading); ``zoom`` the pre-resolved zoom.

        Returns ``(success, outgoing_state)``. The outgoing session is
        only suspended once the target is validated, so a failure leaves
        it exactly as it was.
        """
        if (doc is None or pipeline is None
                or getattr(doc, "is_closed", True)):
            return (False, None)
        state = incoming_state if isinstance(incoming_state, dict) else {}
        position = state.get("reading")
        if position is None:
            try:
                position = self._reading_positions.load(doc.reading_key)
            except Exception:
                position = None
        if zoom is None:
            try:
                zoom = float(position.get("zoom", 1.0)) if position else 1.0
            except (TypeError, ValueError):
                zoom = 1.0
            zoom = max(0.05, min(16.0, zoom))

        # -- Target validated: the swap below cannot destroy the outgoing
        # session. Suspend it (gestures resolved, state captured).
        outgoing_state = None
        old_pipeline = None
        if self._document is not None or self._pipeline is not None:
            # M6.5/M6.7: resolve an open eraser group / shape gesture
            # against the document it belongs to, before the swap.
            self._finalize_eraser()
            try:
                self._interaction.cancel()
            except Exception:
                pass
            self._timer.stop()
            try:
                self.persist_reading_position()
            except Exception:
                pass
            outgoing_state = self.capture_session_state()
            old_pipeline = self._pipeline
            self._document = None
            self._pipeline = None
            # Undo histories stay keyed by document id: an inactive tab
            # keeps its own history (nothing here touches another doc).
            self._sync_undo_hud()
            self._clear_session_transients()
            self._sync_scrollbar(preserve_fraction=False)
            try:
                self._clear_fit_mode()
            except Exception:
                pass
            self._error = None

        # -- Attach the target session.
        self._pipeline = pipeline
        self._document = doc
        self._ui_zoom = zoom
        self._error = None
        appearance = state.get("appearance")
        if appearance:
            try:
                self.set_pdf_appearance(appearance)
            except Exception:
                pass
        self._sync_scrollbar(preserve_fraction=False)
        self._restore_reading_position(position)
        self._request_update()
        self._timer.start()
        self._sync_undo_hud()
        self._active_fit = None
        try:
            self._view_controls.set_available(True)
            self._view_controls.reposition()
        except Exception:
            pass
        try:
            # A document switch must never leave the previous document's
            # previews on screen under the new tab: the panel closes with
            # the outgoing session (its thumbnail keys already carry the
            # per-document identity, so nothing stale can be displayed).
            self._page_navigator.close_panel(animate=False)
            self._page_navigator.set_available(True)
            self._page_navigator.reposition()
        except Exception:
            pass
        fit = state.get("fit")
        if fit in ("width", "height", "page"):
            self._active_fit = fit
            try:
                self._reapply_fit()
            except Exception:
                pass
        try:
            self._sync_view_controls()
        except Exception:
            pass
        self._apply_tool_availability()
        self.update()
        # Retire the old pipeline only now that the target is fully live
        # (background pool when a retirer is installed, else inline).
        self._retire_pipeline(old_pipeline)
        return (True, outgoing_state)

    def _clear_session_transients(self) -> None:
        """Drop every document/selection-scoped transient of the session.

        Shared by tab switching and explicit close: no live stroke, shape,
        eraser group, selection, gesture, hover cursor, menu anchor or
        frame animation may leak across a document boundary.
        """
        self._display.clear()
        self._wanted_prev.clear()
        # Annotation state is viewer-session-local: a closed document
        # takes its committed strokes (cleared in Document.close via the
        # manager) and any live stroke with it.
        try:
            self._outline_cache = {}
            self._off_smooth_cache = {}
            self._bbox_memo = {}
            self._live_prev_rect = None
            self._live_full_rect = None
            self._live_meta = None
            self._live_extents_memo = None
            self._live_smooth_memo = None
            self._live_stroke = None
            self._live_highlighter = None
            self._live_highlight_prev_rect = None
            self._live_highlight_full_rect = None
            self._hl_smooth_cache = {}
            self._hl_band_cache = {}
            # M7.1: a closed document takes any pending hold-to-shape dwell
            # or live morph with it (no timer or morph callback may outlive
            # its document; nothing is committed on close).
            try:
                self._shape_dwell_timer.stop()
            except Exception:
                pass
            try:
                self._shape_morph_timer.stop()
            except Exception:
                pass
            self._shape_morph = None
            self._shape_move_anchor = None
            self._shape_move_ema = None
            self._hl_snap_latched = False
            self._tablet_stroke_open = False
            self._mouse_stroke_open = False
            self._pen_forward_target = None
            self._pen_last_tap = None
            self._eraser_history = None
            self._eraser_mouse_open = False
            self._eraser_tablet_open = False
            self._eraser_mode_latch = None
            self._live_shape = None
            self._live_shape_preview = None
            self._live_shape_snap = None
            self._live_shape_prev_rect = None
            self._live_shape_full_rect = None
            self._shape_mouse_open = False
            self._shape_tablet_open = False
            # M6.8.1: the selection is document-scoped view state; a
            # closed document takes it (and any open region gesture and
            # tap stream) with it.
            self._selection.clear()
            self._selection_mouse_open = False
            self._selection_tablet_open = False
            # M6.8.2 Stage 5.2: the selection cursor is selection-scoped view
            # state; a closed document drops it with everything else.
            self._reset_hover_cursor()
            self._region_gesture = None
            self._region_prev_screen = None
            # M6.8.2: a closed document takes any group-move gesture and
            # preview with it (the pristine geometry was never written).
            self._move_gesture = None
            self._move_preview_clear()
            # M6.8.2 corrective: the cached frame orientation is
            # selection-scoped too; a new document starts fresh.
            self._frame_state = None
            # M6.8.1: drop any Pan tap/drag tracking so a stylus
            # double-tap chain can never resolve against another document.
            self._pan_last_tap = None
            self._pan_press_point = None
            self._pan_dragged = False
            self._pan_tablet_open = False
            self._pan_tablet_dbl = False
            self._tablet_hover_pt = None
            # M6.8.2 clipboard slice: a pointer position from a previous
            # document must never become a Ctrl+V placement target.
            self._last_pointer_pt = None
            # ...and neither must a previous document's menu invocation
            # point become a background-menu Paste anchor.
            self._menu_anchor = None
            # M6.8.1: the frame animation is document-scoped too; a closed
            # document stops it (no timer outlives the selection).
            self._stop_selection_animation()
        except Exception:
            pass

    def _teardown_session(self):
        """Detach and release the live session; returns ``(doc, pipeline)``.

        The managed ``Document`` is NOT closed here: inactive tabs keep
        their document alive, so only an explicit tab/application close
        hands the id to ``DocumentManager.close`` (see ``close_document``).
        """
        # M6.5: never leave an eraser undo group half-open across a
        # document switch (the history belongs to the departing session).
        self._finalize_eraser()
        # M6.7: discard any in-progress shape gesture (drag or polygon)
        # so an unfinished shape can never leak into another document.
        try:
            self._interaction.cancel()
        except Exception:
            pass
        self._timer.stop()
        try:
            self.persist_reading_position()
        except Exception:
            pass
        pipeline, doc = self._pipeline, self._document
        self._pipeline = None
        self._document = None
        # M6.2: drop the closing document's undo history so commands
        # (and their annotation values) release promptly; other open
        # documents keep their own histories untouched. A tab switch does
        # NOT come through here, so suspended tabs keep their history.
        try:
            if doc is not None:
                self._undo_histories.pop(doc.id.value, None)
        except Exception:
            pass
        self._sync_undo_hud()
        self._clear_session_transients()
        if pipeline is not None:
            self._retire_pipeline(pipeline)
        self._error = None
        self._sync_scrollbar(preserve_fraction=False)
        try:
            self._clear_fit_mode()
            self._view_controls.close_panel(animate=False)
            self._view_controls.set_available(False)
            self._sync_view_controls()
        except Exception:
            pass
        try:
            navigator = getattr(self, "_page_navigator", None)
            if navigator is not None:
                navigator.close_panel(animate=False)
                navigator.set_available(False)
                if doc is not None:
                    navigator.drop_document(doc.id.value)
        except Exception:
            pass
        # No live session: every tool returns to normal availability (the
        # next attach re-applies read-only state when it lands).
        self._apply_tool_availability()
        self.update()
        return (doc, pipeline)

    def close_document(self) -> None:
        """Release pipeline, workers, cache and the manager handle."""
        doc, _pipeline = self._teardown_session()
        if doc is not None:
            try:
                self._manager.close(doc.id)
            except Exception:
                pass

    def forget_document(self, doc) -> None:
        """Drop a closed document's undo history from this shared viewer.

        Suspended tabs keep their per-document histories while they are
        inactive; once the owning tab is closed (and the manager has
        closed the document), the history must not outlive it. The live
        document is handled by :meth:`_teardown_session`; this covers
        inactive tabs and is idempotent for the active one.
        """
        if doc is None:
            return
        try:
            self._undo_histories.pop(doc.id.value, None)
        except Exception:
            pass
        try:
            if doc is not self._document:
                return
        except Exception:
            return
        try:
            self._sync_undo_hud()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Idempotent application-exit path (no work may survive).

        The page navigator's decoder joins here — AFTER the unsaved-work
        guard has passed (MainWindow.closeEvent calls this only once the
        exit is committed), so a cancelled close leaves the navigator
        and its decoder fully usable. Idempotent: repeated calls are
        no-ops.
        """
        try:
            navigator = getattr(self, "_page_navigator", None)
            if navigator is not None:
                navigator.shutdown()
        except Exception:
            pass
        self.close_document()

    @property
    def has_document(self) -> bool:
        """True while a document session is open."""
        return self._pipeline is not None

    # -- geometry (single source: pipeline layout) --------------------------

    def _display_scale(self) -> float:
        if self._pipeline is None:
            return self._ui_zoom
        return display_scale_for(self._ui_zoom, self._pipeline.raster_scale)

    def _render_viewport(self) -> tuple[float, float]:
        scale = self._display_scale()
        return render_viewport_size(float(self.width()), float(self.height()), scale)

    def _scroll_doc(self) -> float:
        return float(self._scrollbar.value()) / self._display_scale()

    def _scroll_doc_x(self) -> float:
        """Horizontal viewport origin in render px (M5-B.1.2)."""
        return float(self._hscrollbar.value()) / self._display_scale()

    def _sync_scrollbar(self, preserve_fraction: bool = True,
                        h_anchor: float | None = None) -> None:
        """Resync both scrollbar ranges; vertical behavior unchanged.

        M5-B.1.2: the horizontal axis preserves the document point
        under the viewport center when ``h_anchor`` (a doc-space X in
        render px, captured before the zoom/resize) is given; otherwise
        it preserves fraction like the vertical axis. A zero range
        settles the value to 0 (deliberate fit-again transition).
        """
        frac = 0.0
        if preserve_fraction and self._scrollbar.maximum() > 0:
            frac = scroll_fraction(float(self._scrollbar.value()), float(self._scrollbar.maximum()))
        hfrac = 0.0
        if (preserve_fraction and h_anchor is None
                and self._hscrollbar.maximum() > 0):
            hfrac = scroll_fraction(float(self._hscrollbar.value()),
                                    float(self._hscrollbar.maximum()))
        if self._pipeline is None:
            self._scrollbar.setRange(0, 0)
            self._scrollbar.setValue(0)
            self._hscrollbar.setRange(0, 0)
            self._hscrollbar.setValue(0)
            self._set_hscrollbar_visible(False)
            return
        total_logical = self._pipeline.layout.total_height_px * self._display_scale()
        maximum = max(0, int(total_logical - float(self.height())))
        self._scrollbar.setRange(0, maximum)
        self._scrollbar.setValue(int(scroll_from_fraction(frac, float(maximum))) if maximum else 0)
        scale = self._display_scale()
        total_w_logical = self._pipeline.layout.total_width_px * scale
        hmaximum = max(0, int(total_w_logical - float(self.width())))
        self._hscrollbar.setRange(0, hmaximum)
        # Width-based visibility: the bar exists only when the content is
        # genuinely wider than the viewport. Recomputed on every range
        # sync (resize, zoom, rotation, page ops, document switch), so
        # the state can never go stale.
        self._set_hscrollbar_visible(hmaximum > 0)
        if not hmaximum:
            self._hscrollbar.setValue(0)
        elif h_anchor is not None and scale > 0:
            vw_render, _vh_render = self._render_viewport()
            max_render = max(0.0, float(
                self._pipeline.layout.total_width_px) - vw_render)
            sval = min(max(float(h_anchor) - vw_render / 2.0, 0.0),
                       max_render)
            self._hscrollbar.setValue(int(sval * scale))
        else:
            self._hscrollbar.setValue(
                int(scroll_from_fraction(hfrac, float(hmaximum))))

    # -- scroll / zoom input (never renders synchronously) -------------------

    def _on_scroll(self) -> None:
        if self._pipeline is None:
            return
        self._poke_scrollbar()
        # A scroll moves the document under a stationary pointer: re-apply
        # an active body move so its scroll-aware preview stays pinned to
        # the pointer (see :meth:`_move_follow_scroll`).
        self._move_follow_scroll()
        self._request_update()
        self.update()
        try:
            # M7.0: page indicator follows the real scroll position; an
            # active fit mode waits for the scroll to settle (never
            # refits mid-glide).
            self._sync_view_controls()
            self._restart_scroll_settle()
        except Exception:
            pass

    # -- scrollbar presentation (fade chrome; mechanics untouched) --------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Scrollbar hover shows the bar; leave re-arms the fade-out."""
        try:
            from PySide6.QtCore import QEvent

            if obj is self._scrollbar or obj is self._hscrollbar:
                if event.type() == QEvent.Type.Enter:
                    try:
                        self._scrollbar_hide.stop()
                    except Exception:
                        pass
                    self._poke_scrollbar(hold=True)
                elif event.type() == QEvent.Type.Leave:
                    try:
                        self._scrollbar_hide.start()
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def _set_hscrollbar_visible(self, visible: bool) -> None:
        """Show the horizontal bar only while horizontal overflow exists.

        M5-B.1.2 revealed both bars as one fade unit, so the horizontal
        bar flashed on every vertical scroll even when the document fit
        (its QSS groove + min-width handle still painted at full width).
        The bar is now hidden entirely — not faded, disabled, or left
        with a zero-length thumb — until its range is non-zero. State is
        tracked explicitly (``isVisible`` also reads False while the
        viewer itself is offscreen, which would skip real transitions).
        Fit math keeps reserving the strip unconditionally, so appearing
        overflow never toggles fit zoom (no oscillation); the docked
        geometry is likewise untouched, preserving the vertical bar's
        layout.
        """
        desired = bool(visible)
        if getattr(self, "_hscrollbar_shown", None) is desired:
            return
        self._hscrollbar_shown = desired
        try:
            self._hscrollbar.setVisible(desired)
        except Exception:
            pass
        if desired:
            # Overflow just appeared (zoom, wide page, narrower window):
            # reveal through the normal fade so the user sees it.
            self._poke_scrollbar()
        else:
            try:
                if self._hscrollbar_fx is not None:
                    self._hscrollbar_fx.setOpacity(0.0)
            except Exception:
                pass

    def _fade_scrollbar(self, to_opacity: float) -> None:
        """Animate scrollbar chrome opacity (presentation only).

        M5-B.1.2: both bars share one fade state; the vertical anim
        attribute keeps its exact type/semantics for introspection.
        """
        effects = [getattr(self, "_scrollbar_fx", None),
                   getattr(self, "_hscrollbar_fx", None)]
        effects = [e for e in effects if e is not None]
        if not effects:
            return
        try:
            from app import fx as _fx

            ms = _fx._effective_duration(140 if to_opacity > 0 else 260)
        except Exception:
            ms = 200
        try:
            if self._scrollbar_anim is not None:
                self._scrollbar_anim.stop()
        except Exception:
            pass
        try:
            if self._hscrollbar_anim is not None:
                self._hscrollbar_anim.stop()
        except Exception:
            pass
        self._scrollbar_anim = None
        self._hscrollbar_anim = None
        if ms <= 0:
            try:
                for effect in effects:
                    effect.setOpacity(float(to_opacity))
            except Exception:
                pass
            return
        try:
            from PySide6.QtCore import QPropertyAnimation

            for i, effect in enumerate(effects):
                anim = QPropertyAnimation(effect, b"opacity", self)
                anim.setDuration(max(1, int(ms)))
                try:
                    anim.setStartValue(float(effect.opacity()))
                except Exception:
                    pass
                anim.setEndValue(float(to_opacity))
                anim.start()
                if i == 0:
                    self._scrollbar_anim = anim
                else:
                    self._hscrollbar_anim = anim
        except Exception:
            try:
                for effect in effects:
                    effect.setOpacity(float(to_opacity))
            except Exception:
                pass

    def _style_scrollbar(self) -> None:
        """Wider inset rounded theme-aware bar (presentation only)."""
        try:
            tokens = self._tool_tokens()
            bg = getattr(tokens, "surface_overlay", "rgba(255,255,255,28)")
            handle = getattr(tokens, "text_muted", "#7E8698")
            accent = getattr(tokens, "accent", "#8B93E0")
            self._scrollbar.setStyleSheet(
                "QScrollBar:vertical { background: transparent; width: 14px;"
                " margin: 0px; border: none; }"
                f"QScrollBar::groove:vertical {{ background: {bg};"
                " border: none; border-radius: 7px; width: 10px; }"
                f"QScrollBar::handle:vertical {{ background: {handle};"
                " border: none; border-radius: 6px; min-height: 40px;"
                " width: 10px; }"
                f"QScrollBar::handle:vertical:hover {{ background: {accent}; }}"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
                " { height: 0px; background: none; border: none; }"
                "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
                " { background: none; border: none; }")
            # M5-B.1.2: horizontal twin of the same chrome.
            self._hscrollbar.setStyleSheet(
                "QScrollBar:horizontal { background: transparent; height: 14px;"
                " margin: 0px; border: none; }"
                f"QScrollBar::groove:horizontal {{ background: {bg};"
                " border: none; border-radius: 7px; height: 10px; }"
                f"QScrollBar::handle:horizontal {{ background: {handle};"
                " border: none; border-radius: 6px; min-width: 40px;"
                " height: 10px; }"
                f"QScrollBar::handle:horizontal:hover {{ background: {accent}; }}"
                "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
                " { width: 0px; background: none; border: none; }"
                "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal"
                " { background: none; border: none; }")
        except Exception:
            pass

    def _poke_scrollbar(self, hold: bool = False) -> None:
        """Reveal the scrollbar; auto-hide 900ms after last activity."""
        try:
            self._fade_scrollbar(1.0)
        except Exception:
            pass
        if hold:
            return
        try:
            self._scrollbar_hide.start()
        except Exception:
            pass

    def _request_update(self) -> None:
        if self._pipeline is None:
            return
        vw, vh = self._render_viewport()
        try:
            self._pipeline.update(self._scroll_doc(), int(vw), int(vh),
                                  scroll_x_doc=self._scroll_doc_x())
        except Exception:
            pass  # closed/pressure paths are counted pipeline-side

    def set_zoom(self, ui_zoom: float) -> float:
        """Apply continuous UI zoom; returns the clamped value."""
        # M5-B.1.2: capture the doc point under the viewport center
        # before anything changes so the horizontal axis can re-anchor
        # to it (subject to the new bounds) instead of jumping.
        h_anchor: float | None = None
        width_ratio = 1.0
        try:
            if self._pipeline is not None:
                old_scale = self._display_scale()
                if old_scale > 0:
                    vw_old, _vh_old = self._render_viewport()
                    h_anchor = self._scroll_doc_x() + vw_old / 2.0
                    old_w = float(self._pipeline.layout.total_width_px)
                    if old_w > 0:
                        self._zoom_old_width = old_w
                    else:
                        self._zoom_old_width = None
                else:
                    self._zoom_old_width = None
            else:
                self._zoom_old_width = None
        except Exception:
            h_anchor = None
            self._zoom_old_width = None
        zoom = min(MAX_UI_ZOOM, max(MIN_UI_ZOOM, float(ui_zoom)))
        self._ui_zoom = zoom
        # Tool cursors: cursor sizes scale with the effective stroke width.
        self._refresh_tool_cursor()
        if self._pipeline is None:
            self.update()
            self._sync_view_controls()
            return zoom
        try:
            self._pipeline.set_zoom(zoom)
        except Exception:
            pass
        try:
            if h_anchor is not None:
                new_w = float(self._pipeline.layout.total_width_px)
                old_w = getattr(self, "_zoom_old_width", None)
                if old_w:
                    width_ratio = new_w / float(old_w)
                h_anchor = h_anchor * width_ratio
        except Exception:
            pass
        self._sync_scrollbar(preserve_fraction=True, h_anchor=h_anchor)
        self._request_update()
        self.update()
        self._sync_view_controls()
        return zoom

    def zoom_in(self) -> float:
        """Zoom one step in (manual zoom drops any active fit mode)."""
        self._clear_fit_mode()
        return self.set_zoom(self._ui_zoom * 1.25)

    def zoom_out(self) -> float:
        """Zoom one step out (manual zoom drops any active fit mode)."""
        self._clear_fit_mode()
        return self.set_zoom(self._ui_zoom / 1.25)

    # -- M7.0 view controls: page, zoom, and fit modes ----------------------
    #
    # The viewer owns all view state. Fit modes are one-shot *actions*
    # that set the existing continuous UI zoom; while one is active (a
    # mode button, not Actual Size), it is recalculated once scrolling
    # settles or the viewport/document changes, anchored to the document
    # point under the viewport centre so the page never jumps. Any manual
    # zoom (wheel, +/- keys, typed percentage) clears the mode first, so
    # the fit can never fight deliberate input.

    def _clear_fit_mode(self) -> None:
        self._active_fit = None
        try:
            self._scroll_settle.stop()
        except Exception:
            pass

    def _restart_scroll_settle(self) -> None:
        """Restart the single scroll-settle timer (only while refitting)."""
        if self._active_fit is None or self._refit_busy:
            return
        try:
            self._scroll_settle.start()
        except Exception:
            pass

    def _fit_viewport(self) -> tuple[float, float]:
        """Usable logical viewport for fit math.

        The scrollbars are overlay chrome; their strips are always
        reserved here (not only while they happen to be scrollable) so a
        fit result can never toggle the bar on and off and oscillate.
        """
        width = float(self.width())
        height = float(self.height())
        try:
            bar_w = float(self._scrollbar.sizeHint().width())
        except Exception:
            bar_w = 12.0
        try:
            bar_h = float(self._hscrollbar.sizeHint().height())
        except Exception:
            bar_h = 12.0
        if bar_w <= 0:
            bar_w = 12.0
        if bar_h <= 0:
            bar_h = 12.0
        return (max(64.0, width - bar_w - float(_SCROLLBAR_INSET_X)),
                max(64.0, height - bar_h))

    def _fit_zoom_for(self, mode: str) -> float | None:
        """UI zoom that fits the centre page for ``mode`` (None if n/a).

        Page dimensions come from the layout entry (unrotated media
        points plus the *effective* rotation already composed by
        ``DocumentLayout``), so PDF points, render px, and widget zoom
        stay in their existing spaces: at ``ui_zoom = 1.0`` one PDF point
        is one logical pixel — the application's actual-size convention.
        """
        if self._pipeline is None:
            return None
        if mode not in ("page", "width", "height"):
            return None
        try:
            layout = self._pipeline.layout
            index = self._current_page_index()
            if index is None or not 0 <= int(index) < layout.page_count:
                return None
            entry = layout.entry(int(index))
            rotated = rotated_page_size(
                Size(float(entry.width_pt), float(entry.height_pt)),
                int(entry.effective_rotation))
            page_w = float(rotated.width)
            page_h = float(rotated.height)
            if page_w <= 0 or page_h <= 0:
                return None
            avail_w, avail_h = self._fit_viewport()
            if mode == "width":
                zoom = avail_w / page_w
            elif mode == "height":
                zoom = avail_h / page_h
            else:
                zoom = min(avail_w / page_w, avail_h / page_h)
            return min(MAX_UI_ZOOM, max(MIN_UI_ZOOM, zoom))
        except Exception:
            return None

    def apply_view_mode(self, mode: str) -> None:
        """Apply one view mode (Fit Page/Width/Height, or Actual Size)."""
        if self._pipeline is None:
            return
        if mode == "actual":
            # One-shot: 100% UI zoom *is* the application's actual size
            # (one PDF point per logical pixel). It must leave no fit
            # mode active — a resize must not silently re-zoom it.
            self._clear_fit_mode()
            self.set_zoom(1.0)
            self._sync_view_controls()
            return
        zoom = self._fit_zoom_for(mode)
        if zoom is None:
            return
        self._active_fit = mode
        self.set_zoom(zoom)
        self._sync_view_controls()

    def _reapply_fit(self, anchor: bool = False) -> None:
        """Recalculate the active fit mode (no-op when unchanged).

        ``anchor`` preserves the document point under the viewport centre
        across the zoom change (scroll-settle/rotation path); the resize
        path keeps the existing fraction preservation, like every other
        zoom change in the viewer.

        The whole pass runs with ``_refit_busy`` set: the scrollbar writes
        made here (set_zoom's resync and the anchor correction) therefore
        never re-arm the settle timer, so one scroll burst can trigger at
        most one refit — a feedback loop is structurally impossible.
        """
        if self._active_fit is None or self._pipeline is None:
            return
        zoom = self._fit_zoom_for(self._active_fit)
        if zoom is None:
            return
        if abs(zoom - self._ui_zoom) <= max(
                1e-9, self._ui_zoom * _FIT_EPSILON):
            return
        old_scale = self._display_scale()
        centre_doc: float | None = None
        if anchor and old_scale > 0:
            centre_doc = (float(self._scrollbar.value())
                          + float(self.height()) / 2.0) / old_scale
        self._refit_busy = True
        try:
            self.set_zoom(zoom)
            if centre_doc is not None:
                new_scale = self._display_scale()
                if new_scale > 0:
                    target = int(round(centre_doc * new_scale
                                       - float(self.height()) / 2.0))
                    # QScrollBar clamps to the range: short pages and the
                    # end of the document never produce an out-of-range
                    # jump.
                    self._scrollbar.setValue(target)
        finally:
            self._refit_busy = False

    def set_zoom_manual(self, ui_zoom: float) -> float:
        """Zoom requested by the user directly: drops any fit mode."""
        self._clear_fit_mode()
        return self.set_zoom(ui_zoom)

    def set_zoom_percent(self, percent: float) -> float:
        """Apply a typed percentage (clamped by ``set_zoom`` itself)."""
        try:
            value = float(percent) / 100.0
        except Exception:
            return self._ui_zoom
        return self.set_zoom_manual(value)

    def goto_page(self, index: int) -> None:
        """Glide to a page's top with the existing scroll mechanism.

        The target is the page's own layout offset in logical px, clamped
        to the bar's range (a last short page ends with its bottom at the
        viewport bottom). ``smooth_scroll_to`` is the retargetable wheel
        primitive, so rapid navigation never stacks animations and
        reduced-motion snaps instead.
        """
        if self._pipeline is None:
            return
        try:
            layout = self._pipeline.layout
            if layout.page_count <= 0:
                return
            page = max(0, min(int(index), layout.page_count - 1))
            target = int(round(
                layout.entry(page).y_offset_px * self._display_scale()))
        except Exception:
            return
        target = max(int(self._scrollbar.minimum()),
                     min(int(self._scrollbar.maximum()), target))
        from app import fx as _fx

        _fx.smooth_scroll_to(self._scrollbar, target, _PAGE_GLIDE_MS, self)
        # Keep the navigator's current-page ring in step with explicit
        # jumps (its thumbnail comes into view while the panel is open).
        try:
            navigator = getattr(self, "_page_navigator", None)
            if navigator is not None:
                navigator.sync_state(page)
                navigator.reveal_current()
        except Exception:
            pass

    def _view_prev_page(self) -> None:
        index = self._current_page_index()
        if index is None or int(index) <= 0:
            return
        self.goto_page(int(index) - 1)

    def _view_next_page(self) -> None:
        index = self._current_page_index()
        if index is None or self._pipeline is None:
            return
        if int(index) >= int(self._pipeline.layout.page_count) - 1:
            return
        self.goto_page(int(index) + 1)

    def _view_overlay_insets(self) -> tuple[int, int]:
        """Right/bottom chrome strips the floating controls stay clear of."""
        try:
            bar_w = int(self._scrollbar.sizeHint().width())
        except Exception:
            bar_w = 12
        try:
            bar_h = int(self._hscrollbar.sizeHint().height())
        except Exception:
            bar_h = 12
        right = _SCROLLBAR_INSET_X + max(8, bar_w) + 6
        bottom = max(8, bar_h) + 6
        return int(right), int(bottom)

    def _sync_view_controls(self) -> None:
        """Push the actual page/zoom state into the floating panel."""
        controls = getattr(self, "_view_controls", None)
        if controls is None:
            return
        page: int | None = None
        count = 0
        if self._pipeline is not None:
            try:
                count = int(self._pipeline.layout.page_count)
                page = self._current_page_index()
            except Exception:
                page, count = None, 0
        try:
            percent = int(round(float(self._ui_zoom) * 100.0))
        except Exception:
            percent = None
        try:
            controls.sync_state(page, count, percent, self._pdf_appearance)
        except Exception:
            pass
        # The page navigator pulls its geometry via the cached provider
        # and only needs the live current page here (cheap per frame).
        navigator = getattr(self, "_page_navigator", None)
        if navigator is not None:
            try:
                navigator.set_appearance(self._pdf_appearance)
                navigator.sync_state(page)
            except Exception:
                pass

    # -- M7.0.1 / M7.1.4.3 PDF appearance (view-only) ----------------------
    #
    # Three genuinely distinct display modes: Light (original), Dark
    # (night reading: HSL lightness inverted, hue/saturation preserved)
    # and Inverted (the historical full RGB inversion, pixel-identical to
    # the previous two-state toggle's Dark). Appearance is applied to the
    # viewer's own *display* tiles — the detached QImage copies built in
    # ``_poll`` — never to the renderer's cache, the document, or a file
    # on disk. The display map stays keyed by unchanged cache identities
    # (no second rendering pipeline, no cache-key change).
    #
    # Mode switches DROP the transformed display map instead of
    # transforming it in place: the poll cycle then re-derives every
    # resident tile from the renderer's cached ORIGINAL pixels under the
    # current mode. That keeps "Inverted" exactly reversible and makes it
    # impossible to compound a mode change (also for the non-involutive
    # HSL-lightness transform). Inkfall's own overlays (strokes, shapes,
    # highlighters, frames, chrome) are painted after the tiles and are
    # therefore untouched.

    def set_pdf_appearance(self, mode="light") -> None:
        """Select the PDF display appearance.

        ``mode`` is one of ``"light"`` / ``"dark"`` / ``"inverted"``.
        Legacy booleans are tolerated (True -> "inverted", False ->
        "light") so previously stored/scripted callers keep the old
        pixel behavior.
        """
        if isinstance(mode, bool):
            mode = "inverted" if mode else "light"
        mode = str(mode or "light").strip().lower()
        if mode not in ("light", "dark", "inverted"):
            mode = "light"
        if mode == self._pdf_appearance:
            return
        self._pdf_appearance = mode
        self._rebuild_display_appearance()
        # Re-derive the resident tiles immediately from the renderer's
        # cached ORIGINAL pixels (exactly the regular poll's admission
        # path) so a mode switch never shows a placeholder frame; the
        # 40 ms poll then continues normally. If no document/pipeline is
        # live this is a harmless no-op.
        try:
            self._poll()
        except Exception:
            pass
        self.update()
        self._sync_view_controls()

    def _rebuild_display_appearance(self) -> None:
        """Drop transformed display tiles; they re-derive from originals.

        The renderer's cache still holds the original pixels, and the
        next ``_poll`` rebuilds every visible identity through
        ``_appearance_tile`` (mode-aware). Clearing, rather than
        transforming in place, is what guarantees that switching among
        Light / Dark / Inverted can never stack transforms — most
        importantly the non-involutive HSL-lightness transform.
        """
        try:
            self._display.clear()
        except Exception:
            pass

    def _pdf_page_fill(self) -> QColor:
        """Paper colour for the painting path, per appearance mode.

        The page fill shows in the seams between tiles and behind tiles
        that have not rendered yet; it is derived from the same per-mode
        colour transform as the tiles so the page stays one surface
        instead of leaking light paper through a dark document.
        """
        return _appearance_solid(_PAGE_FILL, self._pdf_appearance)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """Mouse wheel: Ctrl = zoom, otherwise smooth per-pixel scroll.

        The glide only moves the scrollbar value (shared fx primitive,
        re-targetable, reduced-motion = instant) into the existing
        valueChanged -> pipeline path. No render loop, no prefetch
        change: the viewport stays authoritative.
        """
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            # M6.8.2 Stage 5.2: the pointer now hovers different geometry.
            self._reset_hover_cursor()
            event.accept()
            return
        from app import fx as _fx

        bar = self._scrollbar
        self._poke_scrollbar()
        pixel = event.pixelDelta()
        delta = pixel if not pixel.isNull() else event.angleDelta()
        _fx.smooth_scroll_to(bar, bar.value() - int(delta.y()), 110, self)
        self._reset_hover_cursor()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        """Clipboard shortcuts (the app shortcut registry owns the rest).

        Zoom, paging, Delete, Deselect and tool keys are registry actions;
        keeping only the clipboard slice here ensures a focused editor (or
        a standard Ctrl+C/X/V target) keeps its native behavior.
        """
        # M6.8.2 clipboard slice: Ctrl+X / Ctrl+C / Ctrl+V route through
        # the *same* handlers as the context menu.
        if self._document is not None and self._pipeline is not None:
            try:
                if event.matches(QKeySequence.StandardKey.Copy):
                    if self._selection.has_selection:
                        self._copy_selection()
                        event.accept()
                        return
                elif event.matches(QKeySequence.StandardKey.Cut):
                    if self._selection.has_selection:
                        self._cut_selection()
                        event.accept()
                        return
                elif event.matches(QKeySequence.StandardKey.Paste):
                    self._paste_clipboard(None)
                    event.accept()
                    return
            except Exception:
                pass
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep the scrollbars docked; resync ranges and repaint."""
        bar_width = self._scrollbar.sizeHint().width()
        bar_height = self._hscrollbar.sizeHint().height()
        self._scrollbar.setGeometry(
            self.width() - bar_width - _SCROLLBAR_INSET_X,
            _SCROLLBAR_INSET_Y, bar_width,
            max(0, self.height() - 2 * _SCROLLBAR_INSET_Y - bar_height))
        self._hscrollbar.setGeometry(
            0, self.height() - bar_height,
            max(0, self.width() - bar_width), bar_height)
        self._position_shelf()
        # M5-B.1.2: re-anchor horizontal to the pre-resize viewport
        # center (same zoom, new width); falls back to fraction.
        h_anchor: float | None = None
        try:
            old_w = event.oldSize().width()
            scale = self._display_scale()
            if old_w > 0 and scale > 0 and self._pipeline is not None:
                h_anchor = (float(self._hscrollbar.value()) / scale
                            + (float(old_w) / scale) / 2.0)
        except Exception:
            h_anchor = None
        self._sync_scrollbar(preserve_fraction=True, h_anchor=h_anchor)
        self._request_update()
        try:
            self._view_controls.reposition()
        except Exception:
            pass
        try:
            self._page_navigator.reposition()
        except Exception:
            pass
        try:
            self._reapply_fit()
        except Exception:
            pass
        try:
            self._sync_view_controls()
        except Exception:
            pass
        super().resizeEvent(event)
        try:
            self._reanchor_tool_popup()
        except Exception:
            pass
        self.update()

    # -- shared bottom chrome row (M7.0.1.2) ---------------------------
    #
    # The undo capsule (bottom-left) and the floating view-controls
    # trigger (bottom-right) are ONE row. Both are positioned from a
    # single calculation over their live sizes, so their vertical centres
    # always match even though their heights differ. The docked toolbar is
    # never lifted for them: a bottom-docked toolbar is placed in the free
    # span between the two, and an already-docked shelf is left exactly
    # where its own dock geometry puts it.

    def _bottom_row_center(self) -> int:
        """Viewer-local y of the shared bottom-row centre (live geometry).

        The row's band is as tall as the taller of the two controls and
        rests on the deeper of their bottom margins (the trigger's
        scrollbar-clearing inset vs ``HUD_MARGIN``). Centring both in that
        band aligns their centres exactly at every window size.
        """
        margin = 12
        try:
            from app.hud import HUD_MARGIN

            margin = int(HUD_MARGIN)
        except Exception:
            pass
        try:
            _right, bottom = self._view_overlay_insets()
            bottom_inset = int(bottom)
        except Exception:
            bottom_inset = margin
        row_h = 0
        try:
            row_h = max(row_h, int(self._undo_hud.height()))
        except Exception:
            pass
        try:
            row_h = max(row_h, int(self._view_controls.trigger.height()))
        except Exception:
            pass
        return int(self.height() - max(margin, bottom_inset) - row_h // 2)

    def _bottom_row_anchors(self) -> tuple:
        """Anchored capsule / trigger rects on the row (no shelf awareness).

        The capsule keeps its left anchor and the trigger keeps its right
        anchor. Pure row geometry: this is what a bottom-docked toolbar
        reserves its free span from.
        """
        center = self._bottom_row_center()
        margin = 12
        try:
            from app.hud import HUD_MARGIN

            margin = int(HUD_MARGIN)
        except Exception:
            pass
        hud = self._undo_hud
        capsule = QRect(int(margin), int(center - hud.height() // 2),
                        int(hud.width()), int(hud.height()))
        try:
            trigger = self._view_controls.trigger
            right, _bottom = self._view_overlay_insets()
            right_inset = int(right)
        except Exception:
            return capsule, QRect()
        # The View trigger anchors the [View | Pages] capsule's LEFT slot:
        # reserve the Pages segment's width so the whole shared silhouette
        # stays inside the right chrome inset.
        pages_w = 0
        try:
            navigator = getattr(self, "_page_navigator", None)
            if navigator is not None:
                pages_w = int(navigator.trigger.width())
        except Exception:
            pages_w = 0
        rect = QRect(
            int(max(0, self.width() - trigger.width() - right_inset
                    - pages_w)),
            int(center - trigger.height() // 2),
            int(trigger.width()), int(trigger.height()))
        return capsule, rect

    def _bottom_row_rects(self) -> tuple:
        """Committed capsule / trigger rects on the row (shelf-aware).

        The persistent controls must never cover each other or the docked
        shelf. When a restored shelf occupies a floating control's spot,
        that control slides horizontally just clear of the shelf column —
        never vertically, so the two stay on the row and the shelf is
        never displaced.
        """
        capsule, trigger = self._bottom_row_anchors()
        if trigger.isNull():
            return capsule, trigger
        gap = _CHROME_GAP
        try:
            shelf = self._shelf.geometry()
        except Exception:
            return capsule, trigger
        try:
            if capsule.intersects(shelf):
                clear = int(shelf.right() + gap)
                if clear + capsule.width() + gap <= trigger.left():
                    capsule.moveLeft(clear)
            if trigger.intersects(shelf):
                # The View trigger anchors the [View | Pages] capsule:
                # clear the whole shared silhouette (both slots), not
                # just the View segment.
                capsule_w = 0
                try:
                    navigator = getattr(self, "_page_navigator", None)
                    if navigator is not None:
                        capsule_w = int(navigator.trigger.width())
                except Exception:
                    capsule_w = 0
                limit = capsule.right() + gap
                trigger.moveLeft(max(limit,
                                     int(shelf.left() - gap
                                         - trigger.width() - capsule_w)))
        except Exception:
            pass
        return capsule, trigger

    def _bottom_row_span(self, avail_w: int, margin: int) -> tuple:
        """Free horizontal span (left, right) for a bottom-docked toolbar.

        Between the capsule and the floating trigger, on the shared row.
        The toolbar keeps its docked bottom position; only this span is
        resolved — a collision here is a horizontal problem.
        """
        left = int(margin)
        right = int(max(left + 1, avail_w - margin))
        try:
            capsule, trigger = self._bottom_row_anchors()
            left = max(left, int(capsule.right() + _CHROME_GAP))
            if trigger.isNull() or not self._view_controls.trigger.isVisible():
                return left, right
            # The [View | Pages] capsule starts at the View trigger's left
            # edge (the Pages segment lives inside the shared silhouette).
            right = min(right, int(trigger.left() - _CHROME_GAP))
        except Exception:
            pass
        return left, max(left, right)

    def _bottom_row_trigger_origin(self):
        """Trigger top-left on the shared row (None before it exists)."""
        try:
            self._view_controls
        except Exception:
            return None
        try:
            _capsule, trigger = self._bottom_row_rects()
            if trigger.isNull():
                return None
            return QPoint(trigger.x(), trigger.y())
        except Exception:
            return None

    def _pages_trigger_origin(self):
        """Capsule origin (top-left of the View trigger's slot).

        The navigator seats the Pages segment at +44 inside the shared
        [View | Pages] silhouette and anchors the compact panel above
        this row position. Same shelf-collision rule as the View
        trigger itself: a docked shelf pushes the pair left, never
        vertically.
        """
        try:
            navigator = getattr(self, "_page_navigator", None)
            if navigator is None:
                return None
            _capsule, trigger = self._bottom_row_rects()
            if trigger.isNull():
                return None
            gap = _CHROME_GAP
            width = int(navigator.trigger.width())
            x = int(trigger.x() - gap - width)
            y = int(trigger.y())
            try:
                shelf = self._shelf.geometry()
                if QRect(x, y, width, trigger.height()).intersects(shelf):
                    x = int(shelf.left() - gap - width)
            except Exception:
                pass
            return QPoint(max(0, x), y)
        except Exception:
            return None

    def _navigator_chrome_widgets(self) -> list:
        """Chrome subtrees the navigator's dismissal filter must respect.

        Presses on these pass through unconsumed (normal chrome clicks);
        presses on any other viewer descendant are canvas and get
        consumed so a dismissal click can never start a stroke.
        """
        widgets = []
        try:
            widgets.append(self._shelf)
        except Exception:
            pass
        try:
            widgets.append(self._undo_hud)
        except Exception:
            pass
        try:
            widgets.append(self._scrollbar)
            widgets.append(self._hscrollbar)
        except Exception:
            pass
        try:
            widgets.append(self._view_controls.trigger)
            widgets.append(self._view_controls.panel)
        except Exception:
            pass
        try:
            widgets.append(self._page_navigator.capsule_body)
        except Exception:
            pass
        try:
            if self._tool_popup is not None:
                widgets.append(self._tool_popup)
        except Exception:
            pass
        return widgets

    def _navigator_geometry_snapshot(self):
        """Page-geometry snapshot for the page navigator (None w/o doc).

        Cached per ``(pipeline, epoch)``: the layout is immutable between
        page operations / zoom-bucket changes, so the O(pages) aspect
        walk runs once per generation, never per scroll frame. The
        CURRENT page is pushed separately (it changes every frame).
        """
        pipeline = self._pipeline
        if pipeline is None:
            return None
        try:
            key = (id(pipeline), int(pipeline.epoch))
            cached = getattr(self, "_nav_geom_cache", None)
            if cached is not None and cached[0] == key:
                return cached[1]
            layout = pipeline.layout
            count = int(layout.page_count)
            doc = self._document
            snapshot = {
                "count": count,
                # The immutable layout itself (GUI-thread reads only):
                # the strip derives each page's aspect from its pt
                # dimensions on demand — no O(pages) enumeration at
                # snapshot time and no zoom-bucket invalidation.
                "layout": layout,
                # The document's annotation store (GUI-owned, read-only
                # from the navigator's GUI paths): per-page revisions
                # drive overlay cache keys; page snapshots are captured
                # on the GUI thread only.
                "annotations": (doc.annotations
                                if doc is not None else None),
                "doc": (str(doc.id.value) if doc is not None else None),
                "path": (str(doc.render_path) if doc is not None else ""),
                "rotation": 0,
            }
            self._nav_geom_cache = (key, snapshot)
            return snapshot
        except Exception:
            return None

    def _chrome_obstacles(self) -> list:
        """Chrome rectangles the expanded view-controls panel must avoid.

        The docked shelf and the corner capsule are anchored; the panel is
        the transient overlay, so it is the one that moves. Live widget
        geometry only — never a parallel rectangle.
        """
        rects = []
        try:
            rects.append(self._shelf.geometry())
        except Exception:
            pass
        try:
            rects.append(self._undo_hud.geometry())
        except Exception:
            pass
        return rects

    def _shelf_placement(self, dock: str | None = None) -> tuple:
        """Single anchor for committed, preview and final geometry.

        Returns (x, y, sw, sh): where the shelf lands for ``dock``.
        Zone highlights, live previews and commits all derive from
        this one function — never parallel rectangles.
        """
        shelf = self._shelf
        sw, sh = shelf.width(), shelf.height()
        if sw <= 0 or sh <= 0:
            shelf.adjustSize()
            sw, sh = shelf.width(), shelf.height()
        if sw <= 0 or sh <= 0:
            hint = shelf.sizeHint()
            sw, sh = max(sw, hint.width()), max(sh, hint.height())
        margin = 12
        bar_width = self._scrollbar.sizeHint().width()
        avail_w = max(0, self.width() - bar_width - _SCROLLBAR_INSET_X)
        if dock is None:
            try:
                dock = self._tool_store.dock
            except Exception:
                dock = "left"
        # Hypothetical-dock sizing: the live shelf size belongs to its
        # CURRENT orientation. When the requested dock flips the axis,
        # transpose so zones/previews for left/right use the vertical
        # (narrow x long) footprint and top/bottom the horizontal one.
        # Same single source still anchors zones, preview and commit.
        try:
            current_horizontal = shelf._orientation() == "horizontal"
        except Exception:
            current_horizontal = sw >= sh
        wanted_horizontal = dock in ("top", "bottom")
        if wanted_horizontal != current_horizontal:
            sw, sh = sh, sw
        sw = min(sw, max(56, avail_w - margin * 2))
        sh = min(sh, max(56, self.height() - margin * 2))
        if dock == "right":
            x = max(0, avail_w - sw - margin)
            y = max(0, (self.height() - sh) // 2)
        elif dock == "top":
            x = max(0, (avail_w - sw) // 2)
            y = margin
        elif dock == "bottom":
            # M7.0.1.2: a bottom-docked toolbar keeps its docked bottom
            # edge (never lifted). The bottom row belongs to the capsule
            # (left) and the floating trigger (right), so the toolbar is
            # placed in the free span between them and capped to it — a
            # horizontal resolution, not a vertical one.
            left, right = self._bottom_row_span(avail_w, margin)
            span = max(1, right - left)
            sw = min(sw, max(56, span))
            x = left + max(0, (span - sw) // 2)
            y = max(0, self.height() - sh - margin)
        else:
            x = margin
            y = max(0, (self.height() - sh) // 2)
        return int(x), int(y), int(sw), int(sh)

    def _position_shelf(self, dock: str | None = None) -> None:
        """Pin the floating shelf to a dock side (layout-valid geometry).

        Sizes from the shelf's own fixed size (set from its laid-out
        contents in ``refresh``), never from stale hints — the shelf
        keeps a bounded natural size and can never become a sidebar.
        ``dock`` overrides the stored position for drag previews.
        """
        try:
            shelf = self._shelf
            # Shrink the custom viewport to the available budget BEFORE
            # measuring: the shelf recomputes its own fixed size from the
            # reduced viewport instead of being squashed (squashing leaves
            # fixed-size children painting outside the toolbar pill).
            try:
                horizontal = shelf._orientation() == "horizontal"
            except Exception:
                horizontal = True
            try:
                margin = 12
                bar_width = self._scrollbar.sizeHint().width()
                avail_w = max(0, self.width() - bar_width
                              - _SCROLLBAR_INSET_X)
                if horizontal:
                    avail = avail_w - margin * 2
                    active_dock = dock
                    if active_dock is None:
                        try:
                            active_dock = self._tool_store.dock
                        except Exception:
                            active_dock = "left"
                    if active_dock == "bottom":
                        # M7.0.1.2: the bottom row's free span is the real
                        # budget — the toolbar keeps its bottom position.
                        left, right = self._bottom_row_span(avail_w, margin)
                        avail = max(56, right - left)
                else:
                    avail = self.height() - margin * 2
                shelf.fit_to_available(int(max(56, avail)))
            except Exception:
                pass
            x, y, sw, sh = self._shelf_placement(dock)
            if sw != shelf.width() or sh != shelf.height():
                shelf.setFixedSize(sw, sh)
            shelf.move(x, y)
            shelf.raise_()
            self._scrollbar.raise_()
            shelf.raise_()
            try:
                # M7.0.1.2: the capsule shares ONE bottom row with the
                # floating trigger (same vertical centre, live sizes). It
                # keeps its left anchor; if the docked shelf occupies that
                # spot it slides just clear of the shelf column — never
                # vertically, so the row stays intact.
                capsule, _trigger = self._bottom_row_rects()
                hud = self._undo_hud
                hud.move(capsule.x(), capsule.y())
                hud.raise_()
                shelf.raise_()
            except Exception:
                pass
            try:
                # The capsule and its three-dot button are overlay
                # children of the shelf: keep them above the shelf's own
                # slot tiles after the raise sequence (in that z-order,
                # so the dots are never painted over by the capsule).
                shelf.raise_capsule_stack()
            except Exception:
                pass
            try:
                # M7.0: floating view controls stay above the scrollbar
                # chrome; reposition() raises the trigger (and the panel
                # when open) after every layout change.
                self._view_controls.reposition()
            except Exception:
                pass
            try:
                self._reanchor_tool_popup()
            except Exception:
                pass
        except Exception:
            pass

    # -- canvas gestures (M5-A1 routing; viewer stays thin) ------------------

    def _is_pen_active(self) -> bool:
        """True while the active tool is a pen (M5-B.1)."""
        try:
            return self._interaction.active_kind == ToolType.PEN
        except Exception:
            return False

    def _is_highlighter_active(self) -> bool:
        """True while the active tool is a highlighter (M6.3)."""
        try:
            return self._interaction.active_kind == ToolType.HIGHLIGHTER
        except Exception:
            return False

    def _is_eraser_active(self) -> bool:
        """True while the active tool is the stroke-level eraser (M6.5)."""
        try:
            return self._interaction.active_kind == ToolType.ERASER
        except Exception:
            return False

    def _ink_active(self) -> bool:
        """True while the active tool captures freehand ink (M6.3).

        Pen and Highlighter share the same mapped page-local input
        pipeline and stream guards; only the destination differs (the
        legacy pen list vs. the document-owned store).
        """
        return self._is_pen_active() or self._is_highlighter_active()

    def _ink_press(self, page_id, x: float, y: float, pressure: float,
                   now_ms: int) -> dict:
        """Route a mapped press to the active ink tool's handler."""
        # M7.1.1: a new gesture starts with fresh dwell/jitter state, so a
        # damped position or anchor left over from the previous stroke can
        # never suppress (or fake) the first movement of this one.
        self._shape_move_anchor = None
        self._shape_move_ema = None
        if self._is_highlighter_active():
            # M7.1: latch the slot's "Ink to Line" property per stroke,
            # exactly like color/width/opacity (a mid-stroke toggle can
            # never restyle or re-route the active gesture).
            try:
                self._hl_snap_latched = self._highlighter_snap_enabled()
            except Exception:
                self._hl_snap_latched = False
            return self._interaction.handle_highlight_press(
                page_id, x, y, pressure, now_ms)
        self._hl_snap_latched = False
        return self._interaction.handle_pen_press(
            page_id, x, y, 1.0 if pressure is None else pressure, now_ms)

    def _ink_move(self, page_id, x: float, y: float, pressure: float,
                  now_ms: int) -> dict:
        """Route a mapped move to the active ink tool's handler."""
        if self._is_highlighter_active():
            return self._interaction.handle_highlight_move(
                page_id, x, y, pressure, now_ms)
        return self._interaction.handle_pen_move(
            page_id, x, y, 1.0 if pressure is None else pressure, now_ms)

    def _ink_release(self, page_id, x: float, y: float, pressure: float,
                     now_ms: int) -> dict:
        """Route a mapped release to the active ink tool's handler."""
        if self._is_highlighter_active():
            return self._interaction.handle_highlight_release(
                page_id, x, y, pressure, now_ms)
        return self._interaction.handle_pen_release(
            page_id, x, y, 1.0 if pressure is None else pressure, now_ms)

    def _is_shape_active(self) -> bool:
        """True while the active tool is one of the shape tools (M6.7)."""
        try:
            return self._interaction.active_kind in SHAPE_TOOL_KINDS
        except Exception:
            return False

    def _shape_session_open(self) -> bool:
        """True while a shape gesture (drag or polygon) is in progress."""
        try:
            return bool(self._interaction.shape_active())
        except Exception:
            return False

    # -- M6.8.1 selection/lasso tools (view-only; never touches store) --

    def _is_selection_active(self) -> bool:
        """True while the active tool is Selection or Lasso (M6.8.1).

        Both tools drive the same region-selection engine and share the
        same selection state; only the gesture shape differs (rect
        marquee vs freehand loop).
        """
        try:
            return self._interaction.active_kind in (ToolType.SELECTION,
                                                     ToolType.LASSO)
        except Exception:
            return False

    def _selection_frame_visible(self) -> bool:
        """True while a selection exists and its frame should be displayed.

        M6.8.2 Stage 5.2.1: a selection's *existence* is independent of the
        active tool. Selection/Lasso create one through their gesture, Pan
        through a double-click, and a context-menu right-click can select
        under **any** tool (Pen, Highlighter, Eraser, shape, polygon, note).
        In every case the shared border, the eight resize grips, and the
        rotation grip are shown, and they stay visible while the selection
        exists - notably after the context menu closes, with the original
        tool still active.

        This replaces the older "may display a selection" predicate, which
        was gated on the active tool and therefore left a right-click
        selection under Pen/Pan/Eraser without any visible frame or handles.
        Gesture eligibility is a separate question (see
        :meth:`_frame_manipulation_press`); a genuine tool change still
        clears the selection outright.
        """
        try:
            return (self._selection.has_selection
                    and self._document is not None)
        except Exception:
            return False

    def _selection_tool_active(self) -> bool:
        """True while the active tool natively owns selection gestures.

        Selection and Lasso drive the region engine; the Pan tool has no
        gesture engine but selects with a double-click. Kept narrow and
        tool-based for the few routing decisions that really are about the
        tool (the Delete-key applicability), while *display* and frame
        manipulation use :meth:`_selection_frame_visible` so a context-menu
        selection under any tool is fully visible and editable.
        """
        try:
            return self._interaction.active_kind in (
                ToolType.SELECTION, ToolType.LASSO, ToolType.PAN)
        except Exception:
            return False

    def _note_active_tool_kind(self) -> None:
        """Record the *current* kind as the tool policy's "previous" value.

        Called immediately before the active tool is (re)applied, so
        :meth:`_sync_selection_tool_policy` can tell a genuine tool change
        apart from a re-application of the tool that is already active —
        even on the very first sync after construction, where no earlier
        sync has cached a kind yet.
        """
        try:
            self._active_tool_kind = self._interaction.active_kind
        except Exception:
            pass

    def _clear_selection_state(self) -> None:
        """Drop the selection and every selection-specific view state.

        Clears the selected ids, the cached oriented-frame state, the
        frame animation, and any hover/selection cursor — so no frame,
        handle, or cursor can outlive the selection it described. The
        store, the pages, and the undo history are never touched: a
        selection is pure view state (no history entry is created).

        M7.1.2 stale-frame fix: the frame's *painted* footprint is
        captured and invalidated here, before the state that maps it is
        dropped. The frame and its eight grips (plus the rotation grip)
        are painted above every annotation layer, and the paint path
        skips them once the selection is gone — it does not erase their
        previous pixels by itself. Clearing the selection therefore used
        to leave the obsolete border and handles on screen until some
        unrelated repaint happened to cover them. That is exactly what a
        Pen stroke starting outside a just-moved/rotated selection hit:
        the press cleared the selection, the stroke then repainted only
        its own growing region, and the old frame stayed behind. The
        scoped invalidation repaints precisely the footprint that was
        painted (``_selection_frame_screen_box`` includes the grips and
        the rotation grip), so the stale border/handles are erased while
        the new stroke is drawn normally. No full-canvas repaint is
        imposed when the footprint is known.

        The animation is stopped *before* that repaint, and
        ``_advance_selection_animation`` re-checks ``_should_animate``
        when it fires, so a tick already queued when the selection was
        dropped can never paint a frame for the old selection over the
        stroke that replaced it.
        """
        had_selection = False
        stale_box = None
        try:
            had_selection = bool(self._selection.has_selection)
            if had_selection:
                # Must be read while the frame state still exists: after
                # the clear there is nothing left to map the old pixels.
                stale_box = self._selection_frame_screen_box()
        except Exception:
            stale_box = None
        try:
            self._selection.clear()
        except Exception:
            pass
        self._frame_state = None
        self._preview_frame = None
        self._reset_hover_cursor()
        self._sync_selection_animation()
        try:
            if stale_box is not None:
                self.update(stale_box)
            elif had_selection:
                # A frame was shown but its footprint could not be mapped
                # (viewport/document torn down, unmappable layout): never
                # leave pixels behind — fall back to a full repaint.
                self.update()
        except Exception:
            pass

    def _sync_selection_tool_policy(self) -> None:
        """Clear the whole selection on a real change of the active tool.

        M6.8.2 Stage 5.2 policy (supersedes the M6.8.1 "selection is a
        Selection/Lasso/Pan affordance" rule): the selection belongs to the
        tool that made it, so *any* real change of the active canvas tool —
        including Selection to Lasso, and to or from Pan — clears it
        completely through :meth:`_clear_selection_state`. An in-flight
        group move/transform is cancelled through the shared lifecycle
        (never committed, no history entry), and an open region gesture is
        abandoned.

        Re-selecting the already-active tool is not a tool change and
        clears nothing, so an idempotent shelf refresh (or a live tool
        property edit) never disturbs the user's selection. A right-click
        context-menu selection is not a tool change either: opening,
        dismissing, or executing a menu leaves the active tool — and
        therefore the selection — alone.

        Called wherever the active tool can change (tool select + shelf
        refresh).
        """
        try:
            # A tool change breaks any stylus double-tap chain.
            self._pan_last_tap = None
            self._pan_tablet_dbl = False
            try:
                kind = self._interaction.active_kind
            except Exception:
                kind = None
            previous = self._active_tool_kind
            self._active_tool_kind = kind
            changed = previous is not None and kind != previous
            # A tool switch abandons an active group move (the preview
            # belonged to the previous tool's gesture; store, history, and
            # selection are untouched by the cancellation itself).
            self._move_cancel()
            if self._region_gesture is not None:
                self._region_gesture = None
                self._region_prev_screen = None
            if changed and self._selection.has_selection:
                self._clear_selection_state()
        except Exception:
            pass
        self._sync_selection_animation()

    def _revalidate_selection(self) -> bool:
        """Drop selections whose annotations are no longer in the store.

        Returns True when the selection shrank (undo/redo/erase can
        remove selected annotations; the selection is never itself in
        the history, so it must self-heal).

        M6.8.2 Stage 5.2: this is the self-heal that runs after undo/redo/
        erase, so it is also where a selection cursor is invalidated — the
        grips the pointer was hovering may be gone (the next mouse move
        re-derives the hover cursor). Never paint-driven.
        """
        self._reset_hover_cursor()
        try:
            if not self._selection.has_selection:
                return False
            document = self._document
            store = document.annotations if document is not None else None
            if self._selection.revalidate(store):
                self._sync_selection_animation()
                self.update()
                return True
        except Exception:
            try:
                self._selection.clear()
                self._sync_selection_animation()
            except Exception:
                pass
        return False

    def _select_tol_pt(self) -> float:
        """Region pick tolerance in page-local pt (zoom-correct).

        Converts the screen-space ``_SELECT_TOL_PX`` through the same
        ``factor = bucket * scale`` the paint path uses, so a fixed
        on-screen grab distance maps to a proportionally larger page
        distance when zoomed out. Falls back to ``0.0`` (exact geometry
        only) when the mapping is unavailable.
        """
        try:
            layout = self._pipeline.layout
            bucket = float(layout.raster_scale)
            factor = bucket * self._display_scale()
            if factor > 0.0:
                return _SELECT_TOL_PX / factor
        except Exception:
            pass
        return 0.0

    # -- M6.8.1 region gesture engine (marquee + lasso) ---------------------

    def _region_tool_kind(self) -> str:
        """The gesture shape of the active tool: ``rect`` or ``polygon``."""
        try:
            return ("rect" if self._interaction.active_kind
                    == ToolType.SELECTION else "polygon")
        except Exception:
            return "rect"

    def _region_pages(self):
        """Visible ``(index, entry)`` pairs the gesture can cover."""
        pipeline = self._pipeline
        if pipeline is None:
            return []
        try:
            layout = pipeline.layout
            scroll_doc = self._scroll_doc()
            vw_render, vh_render = self._render_viewport()
            return [(index, layout.entry(index))
                    for index in layout.pages_in(
                        scroll_doc, scroll_doc + vh_render)]
        except Exception:
            return []

    def _region_page_screen(self, entry):
        """Widget-space rect of one page (same mapping the paint uses)."""
        pipeline = self._pipeline
        if pipeline is None:
            return None
        layout = pipeline.layout
        scroll_doc = self._scroll_doc()
        scroll_doc_x = self._scroll_doc_x()
        vw_render, _vh_render = self._render_viewport()
        return page_screen_rect(
            float(entry.y_offset_px), float(entry.render_width_px),
            float(entry.render_height_px), scroll_doc, vw_render,
            self._display_scale(), scroll_x_doc=scroll_doc_x,
            content_width_px=float(layout.total_width_px))

    def _region_page_pt(self, entry, wx: float, wy: float):
        """Widget pt -> page-local pt for one page (inverse mapping).

        Mirrors :meth:`_event_to_page_pt` exactly but scoped to that
        page, so one widget-space gesture converts per page. Rotation
        needs no extra step here: it is applied exactly once at layout
        build (entry geometry already encodes the effective rotation),
        so the projection stays a pure scale+translate for every
        effective rotation — mathematically valid to keep regions
        axis-aligned after conversion.
        """
        pipeline = self._pipeline
        if pipeline is None:
            return None
        try:
            layout = pipeline.layout
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return None
            scale = self._display_scale()
            if scale <= 0:
                return None
            vw_render, _vh_render = self._render_viewport()
            scroll_doc = self._scroll_doc()
            scroll_doc_x = self._scroll_doc_x()
            x_off = DocumentLayout.page_x_origin(
                vw_render, float(layout.total_width_px),
                float(entry.render_width_px))
            lx = (float(wx) / scale + scroll_doc_x - x_off) / bucket
            ly = (float(wy) / scale + scroll_doc
                  - float(entry.y_offset_px)) / bucket
            return (lx, ly)
        except Exception:
            return None

    def _annotation_at_widget(self, wx: float, wy: float):
        """Topmost committed annotation under a widget point, or ``None``.

        Point pick for click-to-select (Selection tool) and Pan
        double-click: the point is clipped to each visible page's
        displayed bounds, mapped to page-local pt with the same per-page
        transform the region engine uses, then tested against each
        annotation's *rendered* geometry. Overlapping annotations resolve
        by the established paint order (pen ink > shapes > highlighters;
        later insertion wins) via :func:`pick_annotation`. View-only: it
        reads the store and never writes it.
        """
        document = self._document
        if document is None or self._pipeline is None:
            return None
        tol_pt = self._select_tol_pt()
        for index, entry in self._region_pages():
            page_rect = self._region_page_screen(entry)
            if page_rect is None:
                continue
            px0 = float(page_rect.x)
            py0 = float(page_rect.y)
            px1 = px0 + float(page_rect.width)
            py1 = py0 + float(page_rect.height)
            if not (px0 <= wx <= px1 and py0 <= wy <= py1):
                continue
            local = self._region_page_pt(entry, wx, wy)
            if local is None:
                continue
            try:
                annotations = document.annotations.page(index)
            except Exception:
                continue
            hit = pick_annotation(annotations, local[0], local[1], tol_pt)
            if hit is not None:
                return hit
        return None

    def _region_commit(self, widget_region) -> bool:
        """Commit a finished gesture: select intersecting annotations.

        ``widget_region`` is ``("rect", (x0, y0, x1, y1))`` or
        ``("polygon", ((x, y), ...))`` in widget space. Pure view state
        — it never writes the store and never pushes a history entry.
        Returns True when the selection actually changed (a repeated
        identical gesture changes nothing and repaints not).
        """
        try:
            tol_pt = self._select_tol_pt()
            min_px = _MARQUEE_MIN_PX
            kind, region = widget_region
            if kind == "rect":
                ax, ay = region[0]
                bx, by = region[1]
                x0, x1 = (min(ax, bx), max(ax, bx))
                y0, y1 = (min(ay, by), max(ay, by))
                if (x1 - x0) < min_px and (y1 - y0) < min_px:
                    # Defensive fallback: the Selection click path handles
                    # sub-threshold releases before this point, so a rect
                    # this small is only ever an empty gesture (clears).
                    return self._selection.clear()
                widget_poly = None
            else:
                widget_poly = tuple(region)
                if len(widget_poly) < 3 or polygon_area(widget_poly) <= 0.0:
                    return self._selection.clear()
                xs = [p[0] for p in widget_poly]
                ys = [p[1] for p in widget_poly]
                x0, x1 = min(xs), max(xs)
                y0, y1 = min(ys), max(ys)
                if (x1 - x0) < min_px and (y1 - y0) < min_px:
                    return self._selection.clear()
            document = self._document
            if document is None or self._pipeline is None:
                return self._selection.clear()
            ids: list = []
            store = document.annotations
            for index, entry in self._region_pages():
                try:
                    annotations = store.page(index)
                except Exception:
                    continue
                page_rect = self._region_page_screen(entry)
                if page_rect is None:
                    continue
                prect = (float(page_rect.x), float(page_rect.y),
                         float(page_rect.x) + float(page_rect.width),
                         float(page_rect.y) + float(page_rect.height))
                if not (prect[0] <= x1 and prect[2] >= x0
                        and prect[1] <= y1 and prect[3] >= y0):
                    continue
                if kind == "rect":
                    # Clip the marquee to this page's displayed bounds
                    # BEFORE converting to page-local coordinates.
                    cw0 = max(x0, prect[0])
                    ch0 = max(y0, prect[1])
                    cw1 = min(x1, prect[2])
                    ch1 = min(y1, prect[3])
                    l0 = self._region_page_pt(entry, cw0, ch0)
                    l1 = self._region_page_pt(entry, cw1, ch1)
                    if l0 is None or l1 is None:
                        continue
                    local_region = (min(l0[0], l1[0]), min(l0[1], l1[1]),
                                    max(l0[0], l1[0]), max(l0[1], l1[1]))
                    hits = select_in_rect(annotations, local_region, tol_pt)
                else:
                    # Clip the lasso loop to the page, then convert.
                    clipped = clip_polygon_to_rect(widget_poly, prect)
                    if len(clipped) < 3:
                        continue
                    local = []
                    for wx, wy in clipped:
                        mapped = self._region_page_pt(entry, wx, wy)
                        if mapped is None:
                            local = ()
                            break
                        local.append(mapped)
                    if len(local) < 3:
                        continue
                    hits = select_in_polygon(annotations, local, tol_pt)
                for annotation in hits:
                    ids.append(annotation.id)
            return self._selection.set(ids)
        except Exception:
            try:
                return self._selection.clear()
            except Exception:
                return False

    def _frame_manipulation_press(self, wx: float, wy: float,
                                  kind: str) -> bool:
        """Let the visible selection frame consume a press, or return False.

        M6.8.2 Stage 5.2.1: the displayed frame is an active manipulation
        surface for **every** tool, not only Selection/Lasso/Pan. A press on
        a resize/rotation grip arms that transform, and a press on a selected
        body or anywhere inside the oriented frame interior arms the shared
        group move - both taking precedence over the active tool's ordinary
        pointer behavior (marquee/lasso, panning, drawing, erasing, shaping).
        This is what makes a context-menu selection under Pen/Pan/Eraser
        genuinely interactive instead of a decorative outline.

        Returns True only when the press was consumed, so a point *outside*
        the frame is left to the caller: drawing outside the frame still
        draws, and Pan still pans outside it. Never starts a drawing,
        erasing, or panning gesture itself. Both mouse and stylus presses
        route through here with the same precedence, and the armed gesture
        then owns motion/release through the existing state machine.
        """
        if not self._selection.has_selection:
            return False
        if self._move_gesture is not None:
            # A frame gesture (move/resize/rotate) already owns the pointer:
            # this press belongs to it, so no tool may start anything from it
            # (a synthesized mirror press must never open a stroke).
            return True
        if self._region_gesture is not None:
            return False  # a marquee/lasso keeps extending through the caller
        try:
            wx = float(wx)
            wy = float(wy)
            # A direct vertex/box handle is the most specific target: it
            # takes priority over the padded frame grips and over every
            # whole-object gesture (M7.1.4.2).
            vertex_handle = None
            try:
                vertex_handle = self._vertex_handle_at(wx, wy)
            except Exception:
                vertex_handle = None
            if vertex_handle is not None:
                if self._vertex_press(vertex_handle, wx, wy, kind):
                    return True
            # A frame grip takes priority over body/interior movement, and
            # over the tool's own routing (the rotation grip sits outside
            # the frame).
            handle = None
            try:
                handle = self._transform_handle_at(wx, wy)
            except Exception:
                handle = None
            if handle is not None:
                if self._transform_press(handle, wx, wy, kind):
                    return True
            # A press on a selected body - or anywhere inside the oriented
            # frame - arms the group move instead of the tool's gesture.
            target = self._group_move_target(wx, wy)
            if target is not None:
                move_id, interior = target
                if self._move_press(move_id, wx, wy, kind,
                                    interior=interior):
                    return True
            return False
        except Exception:
            return False

    def _frame_gesture_tablet(self, etype, x: float, y: float,
                              kind: str) -> bool:
        """Route a stylus event through the visible selection frame.

        M6.8.2 Stage 5.2.1: the stylus mirrors the mouse precedence - a
        press on a grip arms the transform, a press on a selected body or
        inside the frame arms the group move - and the armed gesture then
        consumes its own move/release events through the shared state
        machine. Returns True when the event was consumed by the frame (or
        by an already-armed frame gesture), so the caller must stop before
        its tool path.
        """
        try:
            from PySide6.QtCore import QEvent

            if self._move_gesture is not None:
                if etype == QEvent.Type.TabletMove:
                    self._move_move(float(x), float(y))
                elif etype == QEvent.Type.TabletRelease:
                    self._move_release(float(x), float(y))
                return True
            if etype != QEvent.Type.TabletPress:
                return False
            return self._frame_manipulation_press(float(x), float(y), kind)
        except Exception:
            return False

    def _deselect_for_tool_press(self, wx: float, wy: float) -> bool:
        """Drop a stale selection when a tool gesture starts outside it.

        M6.8.2 Stage 5.2.2: a selection made with the right-click context
        menu (or by another tool) stays visible while the pointer merely
        moves, hovers another object, or sits anywhere on the canvas, but as
        soon as the *active tool's own* gesture starts outside the oriented
        frame - and outside the frame's handle regions - the selection is no
        longer meaningful. The press belongs to the tool, so the whole
        selection state is dropped through the shared
        :meth:`_clear_selection_state` and the caller then routes that very
        same event to the tool unchanged: Pen draws, the eraser erases, a
        shape gesture starts, Pan pans. Exactly one gesture runs, and no
        stale frame, grip, cursor, preview, or undo entry is left behind.

        Only in-frame presses are exempt, and an already-armed gesture always
        wins, so this can never pre-empt a transform/move or a marquee/lasso
        (the Selection/Lasso callers do not use it at all, and Pan keeps its
        own immediate deselect). Pure view state: the store, the pages, and
        the history are never touched.
        """
        try:
            if not self._selection.has_selection:
                return False
            if (self._move_gesture is not None
                    or self._region_gesture is not None):
                return False
            if self._transform_handle_at(wx, wy) is not None:
                return False
            if self._vertex_handle_at(wx, wy) is not None:
                return False
            if self._pan_point_in_frame(wx, wy):
                return False
            self._clear_selection_state()
            return True
        except Exception:
            return False

    def _region_press(self, wx: float, wy: float, kind: str) -> None:
        """Open a region gesture at a widget-space press point.

        For the Selection tool (``kind == "rect"``) the press also records
        the topmost annotation under the pointer, so a sub-threshold
        release can resolve to a click that selects it (or clears on empty
        canvas) without ever drawing a marquee. The Lasso stores no hit:
        its click/sub-threshold release stays an empty gesture that
        clears. No annotation or store state is touched here.

        M6.8.2 Stage 4 priority: a press on a *frame grip* (resize or
        rotation) arms that transform before the body-move test, so a
        grip is always grabbable even where it overlaps an annotation;
        otherwise a press on a *selected* body arms the group move, and a
        press on an unselected body or empty canvas falls through to the
        unchanged M6.8.1 marquee/lasso.

        M6.8.2 Stage 5.2.1: grips and the whole *frame interior* are handled
        by the shared :meth:`_frame_manipulation_press` - the same helper
        every other tool uses - so Selection/Lasso keep exactly their
        established routing while Pen/Eraser/Shape/Pan gain the frame as a
        manipulation surface.
        """
        if self._region_gesture is not None or self._move_gesture is not None:
            return  # a gesture already owns the pointer
        try:
            wx = float(wx)
            wy = float(wy)
            # The visible frame owns its grips and its interior first.
            if self._frame_manipulation_press(wx, wy, kind):
                return
            hit = None
            if kind == "rect":
                try:
                    candidate = self._annotation_at_widget(wx, wy)
                    if candidate is not None:
                        hit = candidate.id
                except Exception:
                    hit = None
            self._region_gesture = {"kind": "rect" if kind == "rect"
                                    else "polygon",
                                    "points": [(wx, wy)],
                                    "hit": hit}
            self._region_prev_screen = None
            self.update()
        except Exception:
            self._region_gesture = None

    def _region_move(self, wx: float, wy: float, shift: bool = False) -> None:
        """Track a region gesture; paints the live marquee/lasso."""
        # M6.8.2: an open group-move/transform gesture owns the pointer
        # instead — its moves update the shared preview (``shift`` is
        # forwarded for the resize aspect-lock).
        if self._move_gesture is not None:
            self._move_move(float(wx), float(wy), bool(shift))
            return
        gesture = self._region_gesture
        if gesture is None:
            return
        try:
            gesture["points"].append((float(wx), float(wy)))
            changed = False
            try:
                current = self._region_gesture_screen()
                if current is not None:
                    prev = self._region_prev_screen
                    self._region_prev_screen = QRect(current)
                    if prev is not None:
                        current = current.united(prev)
                    self.update(current)
                    changed = True
            except Exception:
                changed = False
            if not changed:
                self.update()
        except Exception:
            pass

    def _region_gesture_screen(self):
        """Widget-space bounding box of the live gesture overlay."""
        gesture = self._region_gesture
        if gesture is None:
            return None
        points = gesture["points"]
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        pad = 2.0  # dashed overlay stroke width
        box = QRect(int(min(xs) - pad), int(min(ys) - pad),
                    int(max(xs) - min(xs) + 2 * pad) or 1,
                    int(max(ys) - min(ys) + 2 * pad) or 1)
        return box

    def _region_click_commit(self, hit_id) -> bool:
        """Resolve a Selection click: select the hit annotation, else clear.

        ``hit_id`` is the annotation recorded at press (``None`` on empty
        canvas). A stale/absent id clears rather than keeping a dangling
        selection. Pure view state: never writes the store and never
        pushes a history entry. Returns True when the selection changed.
        """
        try:
            document = self._document
            if hit_id is None or document is None:
                return self._selection.clear()
            if document.annotations.get(hit_id) is None:
                return self._selection.clear()
            return self._selection.set([hit_id])
        except Exception:
            try:
                return self._selection.clear()
            except Exception:
                return False

    def _region_release(self, wx: float, wy: float) -> None:
        """Close and commit the open region gesture.

        A Selection release that stayed within the drag threshold resolves
        to a click: the annotation hit at press becomes the selection, or
        empty canvas clears (this supersedes the earlier click-always-
        clears rule). Beyond the threshold the rectangular marquee
        commits as before. The Lasso always commits its loop (a
        sub-threshold loop is already an empty gesture).
        """
        # M6.8.2: a group-move release closes through its own state
        # machine (the region gesture was never opened for it).
        if self._move_gesture is not None:
            self._move_release(float(wx), float(wy))
            return
        gesture = self._region_gesture
        if gesture is None:
            return
        # M6.8.2 Stage 5.2: the gesture that owned the pointer is ending and
        # the selection is about to change, so any move/resize/rotate cursor
        # is dropped here; the next mouse move re-derives the hover cursor
        # from the new selection. (The paint path never resets a cursor.)
        self._reset_hover_cursor()
        wx = float(wx)
        wy = float(wy)
        try:
            if gesture["kind"] == "rect":
                ax, ay = gesture["points"][0]
                if (abs(wx - ax) < _MARQUEE_MIN_PX
                        and abs(wy - ay) < _MARQUEE_MIN_PX):
                    changed = self._region_click_commit(gesture.get("hit"))
                    self._region_overlay_clear()
                    if changed:
                        # A fresh selection starts its ants from zero.
                        self._selection_phase = 0.0
                    self._sync_selection_animation()
                    if changed:
                        self.update()
                    return
                region = ("rect", (gesture["points"][0], (wx, wy)))
            else:
                gesture["points"].append((wx, wy))
                region = ("polygon", tuple(gesture["points"]))
        except Exception:
            self._region_gesture = None
            self._region_prev_screen = None
            self.update()
            return
        changed = self._region_commit(region)
        self._region_overlay_clear()
        if changed:
            # A fresh selection starts its marching-ants cycle from zero.
            self._selection_phase = 0.0
        self._sync_selection_animation()
        if changed:
            self.update()

    def _region_overlay_clear(self) -> None:
        """Repaint the (now-closed) gesture overlay away and clean up."""
        gesture = self._region_gesture
        self._region_gesture = None
        prev = self._region_prev_screen
        self._region_prev_screen = None
        try:
            if gesture is not None:
                box = self._region_gesture_screen()
                if box is not None:
                    self.update(box if prev is None else box.united(prev))
                    return
        except Exception:
            pass
        self.update()

    # -- M6.8.2 group move (Selection/Lasso share one behavior) ------------

    def _is_move_preview(self, annotation) -> bool:
        """True when ``annotation`` is an active group-move candidate.

        Paint-path caches (outlines, smoothed centerlines, bbox memos)
        are memoized per committed annotation object; transient preview
        candidates are churning garbage and must never be cached into
        them. O(group size), only on the candidate paths.
        """
        preview = self._transform_preview
        if not preview:
            return False
        try:
            return any(candidate is annotation
                       for candidate in preview.values())
        except Exception:
            return False

    def _frame_interior_hit(self, wx: float, wy: float):
        """A selected id to drag for a press inside the oriented frame.

        M6.8.2 Stage 5.2: the whole frame — *including* the empty space
        between group members — behaves as one draggable selection
        surface, so a press anywhere inside the oriented quad can start a
        group move even when it hits no stroke or fill. The containment
        test is the shared, rotation-aware :meth:`_pan_point_in_frame`
        (the oriented quad, never its axis-aligned bounds).

        ``None`` when there is no selection, when the point is outside the
        quad, or when it lands on a grip: a rotation/resize grip keeps its
        established precedence. The returned id is only the gesture's
        reference member (any selected, stored one works).
        """
        try:
            if not self._selection.has_selection:
                return None
            if self._transform_handle_at(wx, wy) is not None:
                return None
            if self._vertex_handle_at(wx, wy) is not None:
                return None
            if not self._pan_point_in_frame(wx, wy):
                return None
            document = self._document
            if document is None:
                return None
            store = document.annotations
            for annotation_id in self._selection.selected_ids:
                try:
                    if store.contains(annotation_id):
                        return annotation_id
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _group_move_target(self, wx: float, wy: float):
        """``(id, interior)`` for a press that should arm a group move.

        Precedence: a frame grip returns ``None`` here (so the caller's
        transform routing keeps the grips), then a hit on an already
        *selected* body, then any point inside the oriented selection frame.
        M6.8.2 Stage 5.2.1: the *whole* frame is a movement surface while it
        is visible - the empty space between group members included, under
        every tool - so Pan's frame interior finally moves the group instead
        of panning the viewport. ``None`` leaves the caller's established
        routing untouched (marquee/lasso for Selection/Lasso, panning for
        Pan, drawing/erasing for the ink tools, all outside the frame).

        The returned flag tells the caller whether the press was armed by
        empty frame space rather than a body, so a sub-threshold click there
        can keep the selection instead of re-resolving it. Pure geometry:
        never writes anything.
        """
        if not self._selection.has_selection:
            return None
        if self._transform_handle_at(wx, wy) is not None:
            return None
        if self._vertex_handle_at(wx, wy) is not None:
            return None
        try:
            hit = self._annotation_at_widget(wx, wy)
        except Exception:
            hit = None
        if hit is not None and self._selection.is_selected(hit.id):
            return (hit.id, False)
        interior_id = self._frame_interior_hit(wx, wy)
        if interior_id is None:
            return None
        return (interior_id, True)

    def _move_press(self, hit_id, wx: float, wy: float, kind: str,
                    interior: bool = False) -> bool:
        """Arm a group-move gesture on a press over a *selected* body.

        Captures the gesture-start snapshot — the stable selected ids
        and their pristine store objects *plus each one's home page
        (the transfer source for a cross-page drop)* — and nothing
        else: no store write, no history, no preview. Both Selection
        (rect) and Lasso (polygon) presses route here, so dragging a
        selected annotation moves the whole group under either tool.
        """
        if hit_id is None or self._move_gesture is not None:
            return False
        document = self._document
        if document is None or not self._selection.is_selected(hit_id):
            return False
        try:
            store = document.annotations
            if not store.contains(hit_id):
                return False
            snapshots: dict = {}
            homes: dict = {}
            for annotation_id in self._selection.selected_ids:
                try:
                    snapshots[annotation_id] = store.get(annotation_id)
                except Exception:
                    snapshots[annotation_id] = None
                try:
                    located = store.locate(annotation_id)
                    homes[annotation_id] = (
                        located[0] if located is not None else None)
                except Exception:
                    homes[annotation_id] = None
        except Exception:
            return False
        if snapshots.get(hit_id) is None:
            return False
        geom = self._frame_geometry(self._selection_frame_corners() or ())
        # The press point as a *page-local* anchor on a fixed reference
        # page (the hit member's home when locatable). Every later delta
        # is measured through the *current* viewport mapping of this same
        # page (see :meth:`_move_pointer_delta`), so the move is
        # scroll-aware: a scroll changes which document point lies under a
        # stationary pointer, and the anchor keeps the group following the
        # pointer instead of drifting with the viewport origin.
        ref_index = homes.get(hit_id)
        if ref_index is None:
            ref_index = 0
        anchor = None
        try:
            ref_entry = self._pipeline.layout.entry(int(ref_index))
        except Exception:
            ref_entry = None
        if ref_entry is not None:
            anchor = self._region_page_pt(ref_entry, float(wx), float(wy))
        self._move_gesture = {
            "mode": "move",
            "kind": kind,
            "ids": tuple(self._selection.selected_ids),
            "snapshots": snapshots,
            "homes": homes,
            "start": (float(wx), float(wy)),
            "anchor": anchor,
            "anchor_index": int(ref_index),
            # The latest pointer position, so a pure viewport scroll can
            # re-apply the move from the same pointer.
            "last": (float(wx), float(wy)),
            # The frame at press as view-independent geometry, so the
            # preview frame translates with the group and keeps whatever
            # orientation a previous rotation left behind.
            "centre_w": tuple(geom["centre"]) if geom else None,
            "half_w": tuple(geom["half"]) if geom else None,
            "angle": geom["angle"] if geom else 0.0,
            "moved": False,
            "hit": hit_id,
            # True when the press was armed by empty frame-interior space
            # (Stage 5.2) rather than by a body hit: a sub-threshold click
            # then keeps the selection, exactly like a grip tap.
            "interior": bool(interior),
        }
        return True

    # -- M6.8.2 Stage 4 resize / rotation handles -------------------------

    def _transform_press(self, handle, wx: float, wy: float,
                         kind: str) -> bool:
        """Arm a resize/rotation gesture on a *frame grip* press.

        Captures the gesture-start snapshot exactly like
        :meth:`_move_press` — the stable selected ids, their pristine
        store objects, their home page indices, and each page's shared
        offset ``o_k`` — plus the displayed frame mapped into shared
        space (``v = w / B``) and, per mode, the fixed anchor (resize) or
        the pivot and pointer start angle (rotate). Nothing is written:
        no store mutation, no history, no preview.

        ``handle`` is a name from ``_HANDLE_NAMES`` or ``"rotate"``.
        Only members with a locatable page and a usable offset join the
        gesture; if none survives the press is not armed (the caller
        then falls back to ordinary move/click routing). Both Selection
        and Lasso presses route here, so a transform never depends on
        which tool created the selection. Page assignment is never
        changed by a resize/rotate (see :meth:`_transform_commit`).
        """
        if handle is None or self._move_gesture is not None:
            return False
        if handle != "rotate" and handle not in _HANDLE_NAMES:
            return False
        document = self._document
        if document is None or not self._selection.has_selection:
            return False
        pipeline = self._pipeline
        if pipeline is None:
            return False
        # The transform frame is the *displayed* (padded, oriented) frame
        # the grips sit on — never an individual annotation's extent — so a
        # grip release at its own start position is an exact identity, and
        # the opposite displayed corner is the fixed scale anchor. Using
        # the oriented quad keeps a follow-up resize in the rotated frame's
        # local axes.
        corners_w = self._selection_frame_corners()
        if corners_w is None:
            return False
        factor = self._move_group_factor()
        if factor is None or factor <= 0.0:
            return False
        try:
            store = document.annotations
            layout = pipeline.layout
            snapshots: dict = {}
            homes: dict = {}
            offsets: dict = {}
            for annotation_id in self._selection.selected_ids:
                try:
                    annotation = store.get(annotation_id)
                except Exception:
                    annotation = None
                if annotation is None:
                    continue
                try:
                    located = store.locate(annotation_id)
                except Exception:
                    located = None
                if located is None:
                    continue
                home = located[0]
                try:
                    entry = layout.entry(int(home))
                    offset = self._move_entry_offset(entry)
                except Exception:
                    offset = None
                if offset is None:
                    continue
                snapshots[annotation_id] = annotation
                homes[annotation_id] = home
                offsets[annotation_id] = offset
        except Exception:
            return False
        if not snapshots:
            return False
        corners = tuple((float(x) / factor, float(y) / factor)
                        for x, y in corners_w)
        nw, ne, se, sw = corners
        geom = self._frame_geometry(corners_w)
        if geom is None:
            return False
        angle = geom["angle"]
        cx, cy = (geom["centre"][0] / factor, geom["centre"][1] / factor)
        base = (math.hypot(ne[0] - nw[0], ne[1] - nw[1]),
                math.hypot(sw[0] - nw[0], sw[1] - nw[1]))
        gesture = {
            "mode": "rotate" if handle == "rotate" else "resize",
            "kind": kind,
            "handle": handle,
            "ids": tuple(snapshots),
            "snapshots": snapshots,
            "homes": homes,
            "offsets": offsets,
            "start": (float(wx), float(wy)),
            "factor": factor,
            "corners": corners,
            "centre_w": (float(geom["centre"][0]), float(geom["centre"][1])),
            "half_w": (float(geom["half"][0]), float(geom["half"][1])),
            "angle": angle,
            "base": base,
            "moved": False,
            "hit": None,
            "shift": False,
        }
        if handle == "rotate":
            gesture["pivot"] = (cx, cy)
            gesture["start_angle"] = math.atan2(
                float(wy) / factor - cy, float(wx) / factor - cx)
        else:
            anchor = self._resize_anchor(handle, corners)
            if anchor is None:
                return False
            gesture["anchor"] = anchor
            # The grabbed grip's own start point (shared) — the Shift
            # aspect-lock measures the dominant displacement from here.
            gesture["grip"] = (float(wx) / factor, float(wy) / factor)
        self._move_gesture = gesture
        return True

    @staticmethod
    def _axis_scale(delta, base) -> float:
        """Scale factor from a dragged delta and its base length.

        A zero/non-finite base leaves the axis unchanged (``1.0``) rather
        than dividing by zero; sign is preserved so
        :func:`positive_scale_clamp` can reject a flip through the anchor.
        """
        try:
            delta = float(delta)
            base = float(base)
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(delta):
            return 1.0
        if not math.isfinite(base) or base == 0.0:
            return 1.0
        return delta / base

    def _resize_factors(self, gesture, wx: float, wy: float, shift: bool):
        """Shared-space ``(sx, sy)`` for one resize pointer position.

        Corner grips free-scale both axes (Shift uses the dominant pointer
        displacement for one uniform scale anchored at the opposite
        corner); edge grips scale exactly one axis and pin the other to
        ``1.0`` so an edge drag can never change the other axis. The
        result is clamped strictly positive with a zoom-independent
        minimum side, so the group can neither vanish nor flip through
        the anchor. Both the anchor-relative pointer displacement and the
        grip's base displacement are measured in the frame's *local* axes
        (``gesture["angle"]``), so a rotated frame resizes along its own
        axes rather than the screen axes. Returns ``None`` when the
        gesture is unmappable.
        """
        try:
            factor = float(gesture["factor"])
            ax, ay = gesture["anchor"]
            handle = gesture["handle"]
            rad = math.radians(float(gesture["angle"]))
            c = math.cos(rad)
            s = math.sin(rad)
            px = float(wx) / factor
            py = float(wy) / factor
            nw, ne, se, sw = gesture["corners"]
            min_side = _MIN_GROUP_SIDE_PX / factor
            base = (float(gesture["base"][0]), float(gesture["base"][1]))

            def local_u(p):
                dx = p[0] - ax
                dy = p[1] - ay
                return dx * c + dy * s

            def local_v(p):
                dx = p[0] - ax
                dy = p[1] - ay
                return -dx * s + dy * c

            def mid(a, b):
                return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

            pointer = (px, py)
            corner = {"nw": nw, "ne": ne, "se": se, "sw": sw}
            if handle in corner:
                grip = corner[handle]
                sx = self._axis_scale(local_u(pointer), local_u(grip))
                sy = self._axis_scale(local_v(pointer), local_v(grip))
                if shift:
                    # Shift locks the original aspect ratio: one uniform
                    # scale from the *dominant* local-axis displacement.
                    # The min-size floor is applied as a single uniform
                    # floor (max of both axes' floors) so the ratio
                    # survives even in the minimum-size regime.
                    gx, gy = gesture["grip"]
                    du = local_u(pointer) - local_u((gx, gy))
                    dv = local_v(pointer) - local_v((gx, gy))
                    uniform = sx if abs(du) >= abs(dv) else sy
                    cx, cy = positive_scale_clamp(uniform, uniform,
                                                  base, min_side)
                    uniform = max(cx, cy)
                    return (uniform, uniform)
                return positive_scale_clamp(sx, sy, base, min_side)
            if handle in ("e", "w"):
                grip = mid(ne, se) if handle == "e" else mid(nw, sw)
                sx = self._axis_scale(local_u(pointer), local_u(grip))
                sx, _ = positive_scale_clamp(sx, 1.0, base, min_side)
                return (sx, 1.0)
            if handle in ("n", "s"):
                grip = mid(nw, ne) if handle == "n" else mid(sw, se)
                sy = self._axis_scale(local_v(pointer), local_v(grip))
                _, sy = positive_scale_clamp(1.0, sy, base, min_side)
                return (1.0, sy)
            return None
        except Exception:
            return None

    def _rotation_angle(self, gesture, wx: float, wy: float):
        """Total rotation in degrees for one pointer position, or None.

        The angle is the pointer's bearing about the fixed shared pivot
        minus its bearing at press, normalized to ``(-180, 180]`` so a
        drag across the -180/180 boundary is continuous. Recomputed from
        the start angle every call (never accumulated), so repeated
        previews never drift.
        """
        try:
            factor = float(gesture["factor"])
            px, py = gesture["pivot"]
            angle = math.degrees(math.atan2(
                float(wy) / factor - py, float(wx) / factor - px)
                - gesture["start_angle"])
            return normalize_deg(angle)
        except Exception:
            return None

    def _resized_frame_corners(self, gesture, sx: float, sy: float):
        """Widget corners of the resize frame after a local-axes scale.

        The *unpadded* frame is transformed by the exact map the
        annotations receive (``R(angle) S R(-angle)`` about the shared
        anchor) and the fixed screen pad is re-applied, so the oriented
        frame stays congruent with the scaled content and keeps a constant
        screen margin at any zoom.
        """
        pad = _SELECT_FRAME_PAD_PX
        factor = float(gesture["factor"])
        ax, ay = gesture["anchor"]
        awx = ax * factor
        awy = ay * factor
        rad = math.radians(float(gesture["angle"]))
        c = math.cos(rad)
        s = math.sin(rad)
        cx, cy = gesture["centre_w"]
        hu, hv = gesture["half_w"]
        ex = cx - awx
        ey = cy - awy
        lx = ex * c + ey * s
        ly = -ex * s + ey * c
        lx *= sx
        ly *= sy
        ncx = awx + lx * c - ly * s
        ncy = awy + lx * s + ly * c
        return self._frame_corners_widget(ncx, ncy, hu * sx + pad,
                                          hv * sy + pad, gesture["angle"])

    def _rotated_frame_corners(self, gesture, angle_deg: float):
        """Widget corners of the rotation frame orbited about the pivot."""
        pad = _SELECT_FRAME_PAD_PX
        factor = float(gesture["factor"])
        px, py = gesture["pivot"]
        pwx = px * factor
        pwy = py * factor
        rad = math.radians(float(angle_deg))
        c = math.cos(rad)
        s = math.sin(rad)
        cx, cy = gesture["centre_w"]
        hu, hv = gesture["half_w"]
        ex = cx - pwx
        ey = cy - pwy
        ncx = pwx + ex * c - ey * s
        ncy = pwy + ex * s + ey * c
        return self._frame_corners_widget(ncx, ncy, hu + pad, hv + pad,
                                          float(gesture["angle"]) + angle_deg)

    def _transform_preview_from(self, gesture, transform,
                                frame_corners=None) -> None:
        """Install preview candidates rebuilt from start snapshots.

        Every member recomputes its candidate from its pristine snapshot
        and the *total* ``transform`` (never from the previous preview),
        then keeps its home page — resize/rotation never reparent an
        annotation. Painting and the shared frame pick the candidates up
        through ``_transform_preview``. ``frame_corners`` (widget space),
        when given, becomes the live preview frame so the oriented frame,
        its grips, and the transformed content stay visually consistent.
        """
        before = self._selection_frame_screen_box()
        candidates: dict = {}
        dsts: dict = {}
        for annotation_id, original in gesture["snapshots"].items():
            offset = gesture["offsets"].get(annotation_id)
            if offset is None:
                continue
            try:
                candidate = apply_group(original, offset, transform)
            except Exception:
                candidate = None
            if candidate is None:
                continue
            candidates[annotation_id] = candidate
            dsts[annotation_id] = gesture["homes"].get(annotation_id)
        self._move_preview_set(candidates, dsts)
        if frame_corners is not None:
            self._set_preview_frame(frame_corners)
        after = self._selection_frame_screen_box()
        self._move_repaint(before, after)

    def _move_group_factor(self):
        """Global projection factor ``B`` (page pt -> widget px), or None.

        ``w = B * p + b_k`` for the verified layout projection, so one
        widget-space delta converts to the same page-pt delta on every
        page: the per-page offset ``b_k`` cancels for translation.
        """
        try:
            layout = self._pipeline.layout
            bucket = float(layout.raster_scale)
            scale = self._display_scale()
        except Exception:
            return None
        if bucket <= 0.0 or scale <= 0.0:
            return None
        return bucket * scale

    def _move_entry_offset(self, entry):
        """Per-page offset ``o_k = b_k / B`` for one layout entry.

        ``w = B * p + b_k`` (verified scale-and-translation projection;
        page rotation is baked into the entry, never re-applied here),
        and ``_region_page_pt`` is the pure affine inverse — exact even
        where the widget origin lies outside the page — so
        ``o_k = -_region_page_pt(entry_k, 0, 0)`` is exact.
        """
        pt = self._region_page_pt(entry, 0.0, 0.0)
        if pt is None:
            return None
        return (-float(pt[0]), -float(pt[1]))

    def _move_destination(self, candidate, home_index):
        """Destination page and page-local candidate for one member.

        Policy (deterministic, documented):
        * the transformed geometry's center decides. While the center is
          inside some displayed page's content rectangle, that page is
          the destination — page rects never overlap, so the first
          containment wins;
        * otherwise (the center is in the inter-page/side gutter) the
          nearest page rectangle wins — Euclidean distance from the
          center to the rect, ties broken toward the lower page index —
          so a drag across the boundary between stacked pages keeps a
          continuous, positioned preview (see below), and a release in
          a gutter drops onto the nearest page deterministically. The
          vertical stack has no row gutter (pages are contiguous), so
          ordinary vertical traversal always has the center inside some
          page rectangle.

        The candidate is re-expressed in the destination's page-local
        coordinates with ``p_j = p_k - (o_j - o_k)`` (the shared drag
        delta is already baked into ``candidate``), and its ``page_id``
        follows the destination so the frame, painting, and hit-testing
        all map it through the same page transform. The widget position
        is invariant under the conversion —
        ``B * p_j + b_j == B * p_k + b_k`` — so the preview never jumps
        when the destination flips, and an annotation deliberately moved
        onto another page stays visible and becomes part of that page
        instead of remaining source-bound and clipped out of view.

        Returns ``(dst_page_index, dst_local_candidate)`` or ``None``
        when nothing is mappable (callers keep the member home then).
        """
        layout = self._pipeline.layout if self._pipeline is not None else None
        if layout is None:
            return None
        try:
            home_entry = layout.entry(int(home_index))
        except Exception:
            return None
        extent = annotation_extent(candidate)
        if extent is None:
            return None
        minx, miny, maxx, maxy = extent
        cx = (minx + maxx) / 2.0
        cy = (miny + maxy) / 2.0
        center_w = self._page_widget_point(home_entry, cx, cy)
        if center_w is None:
            return None
        pages = self._region_pages()
        if not pages:
            return None
        rects: list = []
        for index, entry in pages:
            rect = self._region_page_screen(entry)
            if rect is not None:
                rects.append((index, entry, rect))
        dst_index = None
        dst_entry = None
        for index, entry, rect in rects:
            if (rect.x <= center_w[0] <= rect.x + rect.width
                    and rect.y <= center_w[1] <= rect.y + rect.height):
                dst_index, dst_entry = index, entry
                break
        if dst_index is None:
            best = None
            for index, entry, rect in rects:
                dx = max(rect.x - center_w[0], 0.0,
                         center_w[0] - (rect.x + rect.width))
                dy = max(rect.y - center_w[1], 0.0,
                         center_w[1] - (rect.y + rect.height))
                distance = dx * dx + dy * dy
                # Strict less-than keeps the lower page index on ties;
                # rects are scanned in ascending page order.
                if best is None or distance < best[0]:
                    best = (distance, index, entry)
            if best is not None:
                dst_index, dst_entry = best[1], best[2]
        if dst_index is None or dst_entry is None:
            return None
        if dst_index == home_index:
            return (home_index, candidate)
        o_home = self._move_entry_offset(home_entry)
        o_dst = self._move_entry_offset(dst_entry)
        if o_home is None or o_dst is None:
            return None
        # p_j = p_k - (o_j - o_k): identical widget position under the
        # destination page's transform.
        converted = move_annotation(candidate, o_home[0] - o_dst[0],
                                    o_home[1] - o_dst[1])
        if converted is None:
            return None
        try:
            converted = dataclasses.replace(converted,
                                            page_id=dst_entry.page_id)
        except Exception:
            return None
        return (dst_index, converted)

    def _move_preview_route(self, entry_index):
        """Preview routing for one page's committed-items paint pass.

        Returns ``(skip_ids, arrivals)`` while a group move previews:
        committed annotations whose candidates transferred to another
        page are skipped here (painted by the destination pass only —
        never duplicated), candidates whose destination is THIS page
        but whose store home is elsewhere arrive and are painted after
        this page's store items. \"None, None\" (or any exception)
        means no preview: callers substitute nothing. Same-page
        candidates stay substituted in place by the caller's existing
        id-keyed substitution.
        """
        preview = self._transform_preview
        dsts = self._transform_dsts
        if not preview or not dsts:
            return None, None
        skip: set = set()
        arrivals: list = []
        try:
            store = self._document.annotations
            for annotation_id in preview:
                located = store.locate(annotation_id)
                home = located[0] if located is not None else None
                dst_index = dsts.get(annotation_id)
                if home == entry_index:
                    if dst_index is not None and dst_index != entry_index:
                        # Transferred away: this slot must stay empty.
                        skip.add(annotation_id)
                    continue
                if dst_index == entry_index:
                    candidate = preview.get(annotation_id)
                    if candidate is not None:
                        arrivals.append(candidate)
        except Exception:
            return None, None
        return skip, arrivals

    def _paint_move_preview_overlay(self, painter, dirty) -> None:
        """Unclipped preview pieces for bands the clipped pass misses.

        M6.8.2 cross-page movement: while a group move previews, a
        candidate is normally painted by its assigned page's clipped
        pass (page-rect clipping, B.2.4). A candidate whose whole
        widget-space band lies OUTSIDE that rect — e.g. a thin member
        dropped through the inter-page gutter — would vanish entirely;
        the move contract forbids the preview disappearing in the
        gutter, so after the pages are painted, each such candidate is
        painted once more through its page's usual per-kind path,
        restricted to exactly the move-preview candidates destined for
        that page and clipped to the band ∩ the pages' bounding hull
        (the stretch *between* page rects). The hull rule keeps the
        established clipped behavior at the outer canvas margins
        untouched (a single-page document has no between-pages stretch,
        so nothing gains unclipped ink there).
        """
        preview = self._transform_preview
        dsts = self._transform_dsts
        if not preview or not dsts:
            return
        pipeline = self._pipeline
        if pipeline is None:
            return
        if self._document is None:
            return
        try:
            from PySide6.QtCore import QRectF

            layout = pipeline.layout
            bucket = float(layout.raster_scale)
            scale = float(self._display_scale())
            if bucket <= 0 or scale <= 0:
                return
            factor = bucket * scale
            rects = []
            for _index, entry in self._region_pages():
                rect = self._region_page_screen(entry)
                if rect is not None:
                    rects.append(rect)
            if not rects:
                return
            hull = QRectF(
                min(float(rect.x) for rect in rects),
                min(float(rect.y) for rect in rects),
                max(float(rect.x) + float(rect.width)
                    for rect in rects)
                - min(float(rect.x) for rect in rects),
                max(float(rect.y) + float(rect.height)
                    for rect in rects)
                - min(float(rect.y) for rect in rects))
            for annotation_id in preview:
                dst_index = dsts.get(annotation_id)
                if dst_index is None:
                    continue
                candidate = preview.get(annotation_id)
                if candidate is None:
                    continue
                try:
                    entry = layout.entry(int(dst_index))
                except Exception:
                    continue
                assigned = self._region_page_screen(entry)
                if assigned is None:
                    continue
                extent = annotation_extent(candidate)
                if extent is None:
                    continue
                minx, miny, maxx, maxy = extent
                x0 = float(assigned.x) + minx * factor
                y0 = float(assigned.y) + miny * factor
                x1 = float(assigned.x) + maxx * factor
                y1 = float(assigned.y) + maxy * factor
                band = QRectF(x0, y0, max(0.0, x1 - x0),
                              max(0.0, y1 - y0))
                page = QRectF(float(assigned.x), float(assigned.y),
                              float(assigned.width),
                              float(assigned.height))
                if band.intersects(page):
                    # The clipped pass painted the covered part; the
                    # remainder keeps the established paper-edge
                    # clipping (partial visibility, never full loss).
                    continue
                clip = band.intersected(hull)
                if clip.isEmpty():
                    continue
                if dirty is not None:
                    work = clip.intersected(QRectF(dirty))
                    if work.isEmpty():
                        continue
                # Re-run the assigned page's per-kind pass restricted
                # to the preview candidates destined for it, clipped
                # to the uncovered band. Mapping origin/factor match
                # the pass (page_rect stays the real assigned rect).
                if isinstance(candidate, InkStroke):
                    self._paint_page_strokes(
                        painter, layout, entry, assigned, scale,
                        None, overlay_rect=clip, preview_only=True)
                elif isinstance(candidate, Highlighter):
                    self._paint_page_highlighters(
                        painter, layout, entry, assigned, scale,
                        None, overlay_rect=clip, preview_only=True)
                elif isinstance(candidate, Shape):
                    self._paint_page_shapes(
                        painter, layout, entry, assigned, scale,
                        None, overlay_rect=clip, preview_only=True)
        except Exception:
            pass

    def _move_preview_clear(self) -> None:
        """Discard the transient group-move preview, routing with it.

        Both preview fields always change together; ``None`` in each is
        the idle state.
        """
        self._transform_preview = None
        self._transform_dsts = None
        # M6.8.2 corrective: the live preview frame is transient too. A
        # commit promotes it to ``_frame_state`` *before* this clear (see
        # the commit paths); every other caller (cancel, abort, no-op)
        # wants the frame to fall back to the last committed orientation.
        self._preview_frame = None

    def _move_preview_set(self, candidates: dict, dsts: dict) -> None:
        """Install preview candidates and their destination routing."""
        self._transform_preview = candidates or None
        self._transform_dsts = dsts or None

    def _set_preview_frame(self, corners) -> None:
        """Record the live frame's four widget corners for this gesture."""
        if not corners:
            return
        self._preview_frame = {
            "ids": tuple(self._selection.selected_ids),
            "corners": tuple((float(x), float(y)) for x, y in corners),
        }

    def _translate_preview_frame(self, gesture, dx: float, dy: float) -> None:
        """Translate the gesture's start frame by a widget-space delta."""
        if not gesture:
            return
        centre = gesture.get("centre_w")
        half = gesture.get("half_w")
        if centre is None or half is None:
            return
        pad = _SELECT_FRAME_PAD_PX
        corners = self._frame_corners_widget(
            centre[0] + dx, centre[1] + dy, half[0] + pad, half[1] + pad,
            gesture.get("angle", 0.0))
        self._set_preview_frame(corners)

    def _move_pointer_delta(self, gesture, wx: float, wy: float):
        """Page-pt pointer displacement since press, viewport-aware.

        The gesture records its press point as a page-local ``anchor`` on
        a fixed reference page; measuring the current pointer through the
        *same* page's current mapping yields a delta that already folds in
        any viewport scroll between press and now::

            d = _region_page_pt(ref, w_now) - _region_page_pt(ref, w_press)

        Every page shares one projection scale ``B`` and differs only by a
        constant per-page offset, so ``d`` is the shared-space
        displacement applied to every member's home page. A scroll moves
        the document under a stationary pointer — i.e. it changes
        ``_region_page_pt(ref, w_now)`` — so the group stays under the
        pointer instead of drifting by the scroll amount (the pre-fix
        behaviour used a raw widget delta and ignored scroll entirely).
        Returns ``(pdx, pdy)``, or ``None`` when the reference page is
        gone or the anchor is unusable.
        """
        anchor = gesture.get("anchor")
        if anchor is None:
            # Defensive fallback (no captured anchor): the legacy widget
            # delta / B, rather than dropping the move entirely.
            start = gesture.get("start")
            factor = self._move_group_factor()
            if start is None or factor is None or factor <= 0.0:
                return None
            return ((float(wx) - float(start[0])) / factor,
                    (float(wy) - float(start[1])) / factor)
        try:
            entry = self._pipeline.layout.entry(
                int(gesture.get("anchor_index", 0)))
        except Exception:
            return None
        cur = self._region_page_pt(entry, float(wx), float(wy))
        if cur is None:
            return None
        return (float(cur[0]) - float(anchor[0]),
                float(cur[1]) - float(anchor[1]))

    def _move_follow_scroll(self) -> None:
        """Re-apply an active body move after the viewport scrolled.

        The move delta is scroll-aware, but it is only computed when a
        pointer move arrives. A scroll (wheel/touchpad, scrollbar drag,
        keyboard, or a resize that changed the range) changes the document
        point under a stationary pointer, so the preview must be
        recomputed from the gesture's last pointer position; without this
        the group drifts off the pointer by exactly the scroll amount and
        can leave the viewport. Only the plain body move is re-derived
        here — resize/rotation keep their own start-relative transform —
        and nothing is committed before release.
        """
        gesture = self._move_gesture
        if gesture is None or gesture.get("mode", "move") != "move":
            return
        last = gesture.get("last")
        if last is None:
            return
        try:
            self._move_move(float(last[0]), float(last[1]))
        except Exception:
            pass

    def _move_move(self, wx: float, wy: float, shift: bool = False) -> None:
        """Update the transient group transform preview for one move.

        Candidates are recomputed from the *pristine* gesture-start
        snapshots and the scroll-aware shared-space pointer delta
        (:meth:`_move_pointer_delta`), so relative positions are
        preserved exactly across pages of any size, offset, and rotation
        — and never from a previously transformed candidate. Nothing is
        mutated: painting and the shared frame pick the candidates up
        through ``_transform_preview``.

        ``_move_gesture`` carries the active ``mode``: a plain body press
        arms ``"move"``; a frame-grip press arms ``"resize"`` or
        ``"rotate"``; a direct vertex/box-handle press arms ``"vertex"``
        (M7.1.4.2). They share this single entry point (and the single
        ``_transform_preview`` channel) so every caller — region, Pan,
        and tablet — keeps one pointer-owning gesture.
        """
        gesture = self._move_gesture
        if gesture is None:
            return
        mode = gesture.get("mode", "move")
        if mode == "resize":
            self._resize_move(wx, wy, shift)
            return
        if mode == "rotate":
            self._rotate_move(wx, wy)
            return
        if mode == "vertex":
            self._vertex_move(wx, wy)
            return
        try:
            start_x, start_y = gesture["start"]
            # Remember the latest pointer so a pure viewport scroll can
            # re-apply the move from the same pointer position.
            gesture["last"] = (float(wx), float(wy))
            dx = float(wx) - start_x
            dy = float(wy) - start_y
            # The established drag threshold: below it the press is
            # still an ordinary click — no preview, no store touch.
            if not gesture["moved"]:
                if abs(dx) < _MARQUEE_MIN_PX and abs(dy) < _MARQUEE_MIN_PX:
                    return
                gesture["moved"] = True
            # Scroll-aware shared-space delta, measured through the
            # *current* viewport and never accumulated from a previous
            # preview, so a scroll during the drag cannot drift the group
            # off the pointer. The frame itself still translates by the
            # raw widget delta (it is a widget-space shape pinned to the
            # pointer, independent of which document point it covers).
            delta = self._move_pointer_delta(gesture, wx, wy)
            if delta is None:
                return
            pdx, pdy = delta
            before = self._selection_frame_screen_box()
            candidates: dict = {}
            dsts: dict = {}
            for annotation_id, original in gesture["snapshots"].items():
                if original is None:
                    continue
                try:
                    candidate = move_annotation(original, pdx, pdy)
                except Exception:
                    candidate = None
                if candidate is None:
                    continue
                # Destination policy per member (deterministic): a
                # member whose transformed center crossed onto another
                # displayed page is re-expressed in that page's local
                # coordinates there — the widget position is invariant,
                # so the preview stays visually continuous across the
                # crossing; an unmappable member keeps its home page.
                home = gesture.get("homes", {}).get(annotation_id)
                dst_index = home
                if home is not None:
                    try:
                        placed = self._move_destination(candidate, home)
                    except Exception:
                        placed = None
                    if placed is not None:
                        dst_index, candidate = placed
                candidates[annotation_id] = candidate
                dsts[annotation_id] = dst_index
            self._move_preview_set(candidates, dsts)
            # The shared frame translates with the group, keeping whatever
            # orientation a previous rotation left behind.
            self._translate_preview_frame(gesture, dx, dy)
            after = self._selection_frame_screen_box()
            self._move_repaint(before, after)
        except Exception:
            # Never lose pixels to a mapping failure.
            self.update()

    def _resize_move(self, wx: float, wy: float, shift: bool = False) -> None:
        """Update the group resize preview for one pointer position.

        Factors come from :meth:`_resize_factors` (from the start frame
        and pointer, never from the previous preview), so the preview is
        drift-free and Shift/aspect-lock is derived fresh each move. The
        Shift state is remembered on the gesture so release can rebuild
        the identical transform if the platform sends no final move.
        """
        gesture = self._move_gesture
        if gesture is None or gesture.get("mode") != "resize":
            return
        try:
            start_x, start_y = gesture["start"]
            if not gesture.get("moved"):
                if (abs(float(wx) - start_x) < _MARQUEE_MIN_PX
                        and abs(float(wy) - start_y) < _MARQUEE_MIN_PX):
                    return
                gesture["moved"] = True
            gesture["shift"] = bool(shift)
            factors = self._resize_factors(gesture, wx, wy, bool(shift))
            if factors is None:
                return
            sx, sy = factors
            ax, ay = gesture["anchor"]
            self._transform_preview_from(
                gesture,
                GroupTransform(anchor=Point(ax, ay), sx=sx, sy=sy,
                               frame_deg=float(gesture["angle"])),
                self._resized_frame_corners(gesture, sx, sy))
        except Exception:
            self.update()

    def _rotate_move(self, wx: float, wy: float) -> None:
        """Update the group rotation preview for one pointer position.

        The angle is the total bearing change about the fixed shared pivot
        since press, normalized across the -180/180 boundary. Candidates
        are recomputed from the start snapshots each call, so there is no
        accumulated drift.
        """
        gesture = self._move_gesture
        if gesture is None or gesture.get("mode") != "rotate":
            return
        try:
            start_x, start_y = gesture["start"]
            if not gesture.get("moved"):
                if (abs(float(wx) - start_x) < _MARQUEE_MIN_PX
                        and abs(float(wy) - start_y) < _MARQUEE_MIN_PX):
                    return
                gesture["moved"] = True
            angle = self._rotation_angle(gesture, wx, wy)
            if angle is None:
                return
            px, py = gesture["pivot"]
            self._transform_preview_from(
                gesture,
                GroupTransform(pivot=Point(px, py), angle_deg=angle),
                self._rotated_frame_corners(gesture, angle))
        except Exception:
            self.update()

    def _move_repaint(self, before, after) -> None:
        """Scoped repaint of the preview's old+new frame, or full.

        Translation moves every member by one shared offset, so the
        union of the before/after padded frame boxes covers every pixel
        the preview changed (member extents are the frame inputs; the
        slack covers dash edges, stroke antialiasing, and the pulse).
        Any failure falls back to a full update — never lose pixels.
        """
        try:
            rect = (before if after is None
                    else after if before is None else before.united(after))
            if rect is not None:
                self.update(rect.adjusted(-8, -8, 8, 8))
            else:
                self.update()
        except Exception:
            self.update()

    def _move_release(self, wx: float, wy: float) -> None:
        """Close the armed group move/transform: commit or discard.

        A release that stayed under the drag threshold resolves to a
        normal click on the pressed annotation (Selection) or retains
        the selection (Lasso) — either way, no history entry and an
        untouched store. A real drag runs the two-phase preflight
        commit; an unmappable viewport simply discards. A frame-grip
        press (resize/rotate) closes through :meth:`_transform_release`.
        """
        gesture = self._move_gesture
        if gesture is None:
            return
        mode = gesture.get("mode", "move")
        self._move_gesture = None
        # M6.8.2 Stage 5.2: the drag cursor belonged to the gesture; drop
        # it here so a ClosedHand/resize/rotate pointer can never outlive
        # the drag (the next move re-derives the hover cursor).
        self._reset_hover_cursor()
        if mode in ("resize", "rotate"):
            self._transform_release(gesture, wx, wy)
            return
        if mode == "vertex":
            self._vertex_release(gesture, wx, wy)
            return
        try:
            start_x, start_y = gesture["start"]
            dx = float(wx) - start_x
            dy = float(wy) - start_y
        except Exception:
            dx = dy = 0.0
        if not gesture.get("moved"):
            self._move_click(gesture)
            return
        # Scroll-aware commit delta: identical to what the last preview
        # used (and recomputed here from the release pointer), so the
        # committed page/position always matches the visible preview even
        # if the viewport scrolled during the drag.
        pdelta = self._move_pointer_delta(gesture, wx, wy)
        if pdelta is not None:
            # Pin the final frame before the commit (the release position
            # can differ from the last move); a successful commit promotes
            # it, a no-op/abort drops it back to the committed orientation.
            self._translate_preview_frame(gesture, dx, dy)
            self._move_commit(gesture, pdelta[0], pdelta[1])
            return
        # The drag threshold was crossed but the viewport cannot map a
        # shared-space delta: discard the preview (store/history/
        # selection unchanged).
        self._move_preview_clear()
        self.update()

    def _move_click(self, gesture) -> None:
        """Sub-threshold press/release on a selected body — no history.

        Selection resolves exactly like the M6.8.1 click (the pressed
        annotation becomes the whole selection). Lasso used to clear on
        every sub-threshold gesture, but a press over a *selected* body
        is now a group-move press: the click retains the selection
        instead of dissolving an untouched multi-selection. A frame-grip
        press (``handle`` set) is never a body click: it simply keeps
        the selection, so a sub-threshold grip tap never selects/deselects.
        """
        try:
            if gesture.get("handle") is not None or gesture.get("interior"):
                self._move_preview_clear()
                self._sync_selection_animation()
                return
            if gesture["kind"] == "rect":
                changed = self._region_click_commit(gesture.get("hit"))
                if changed:
                    # A fresh selection starts its ants from zero.
                    self._selection_phase = 0.0
                self._sync_selection_animation()
                if changed:
                    self.update()
                return
            self._sync_selection_animation()
        except Exception:
            pass

    def _move_commit(self, gesture, pdx: float, pdy: float) -> None:
        """Two-phase preflight commit of one grouped batch move (D5/D7/D8).

        Phase 1 is read-only: ids come from the non-mutating
        ``selected_ids`` accessor and ``store.get``/``snapshots`` —
        ``SelectionState.resolve()``/``revalidate()`` are never called
        here. A vanished id is not a failure by itself (proceed with
        survivors), but a still-present annotation that is not even the
        same object as its gesture-start snapshot (an unexpected
        intervening edit) aborts the whole operation before any macro
        opens, and unsupported members abort as a whole — a group is
        never partially moved.

        Phase 2 builds the complete batch *before* the first
        submission: each survivor's final candidate is recomputed from
        its pristine snapshot (never from preview state), its
        destination page is resolved with the same deterministic policy
        the preview used (a member whose center crossed onto another
        displayed page transfers there — page assignment follows the
        drop instead of staying source-bound and clipped out of view;
        mapping failures keep the member home), and true no-op members
        are dropped. The fully validated
        :class:`~app.undo.BatchMoveCommand` (same-page members keep
        their pristine slot; cross-page members are removed from their
        source and inserted on the destination page) is then submitted
        as exactly one grouped history entry through
        ``push_prebuilt_grouped`` and the group is closed. Qt macros
        group executions — they never roll back — so the command is
        fully simulated and validated inside its constructor, before
        the push.
        """
        document = self._document
        try:
            store = document.annotations
        except Exception:
            store = None
        if store is None:
            self._move_preview_clear()
            self.update()
            return
        ids = tuple(self._selection.selected_ids)
        snapshots = gesture["snapshots"]
        homes = gesture.get("homes") or {}
        entries: list = []
        try:
            for annotation_id in ids:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    # Vanished annotation: never a failure by itself;
                    # survivors proceed (D7).
                    continue
                original = snapshots.get(annotation_id)
                if original is None or original is not current:
                    # An intervening edit replaced a still-existing
                    # selected annotation: never overwrite it — abort
                    # the whole operation before the macro opens.
                    raise _MoveConflict()
                candidate = move_annotation(original, pdx, pdy)
                if candidate is None:
                    # An unsupported member is never silently skipped
                    # from a group: abort as a whole.
                    raise _MoveConflict()
                home = homes.get(annotation_id)
                dst_index, target, transferred = home, candidate, False
                if home is not None:
                    try:
                        placed = self._move_destination(candidate, home)
                    except Exception:
                        placed = None
                    if placed is not None:
                        dst_index, target = placed
                        transferred = dst_index != home
                if dst_index is None:
                    # The source page cannot even be located, so a
                    # relocation cannot be named: abort rather than
                    # guess (a group is never partially moved).
                    raise _MoveConflict()
                if not transferred and not geometry_changed(original,
                                                            candidate):
                    continue  # true no-op member
                # A relocation keeps the annotation's stable identity;
                # the command validates it again pre-push.
                entries.append((annotation_id, target, int(dst_index)))
        except _MoveConflict:
            self._move_preview_clear()
            self.update()
            return
        except Exception:
            # Anything unexpected in preflight aborts pre-macro; the
            # store and history are still untouched by this operation.
            self._move_preview_clear()
            self.update()
            return
        if not entries:
            # Every surviving move is a true no-op: no group opened,
            # no entry, and stale selection ids are NOT cleaned here
            # (no commit happened).
            self._move_preview_clear()
            self.update()
            return
        history = self._history_for(document, create=True)
        if history is None:
            self._move_preview_clear()
            self.update()
            return
        # Fully constructed and simulated (and therefore validated)
        # against the pristine store inside the constructor, before any
        # submission; a construction failure here aborts pre-macro with
        # the store untouched.
        try:
            command = BatchMoveCommand(store, entries, text="Move")
        except Exception:
            self._move_preview_clear()
            self.update()
            return
        try:
            history.push_prebuilt_grouped(command, text="Move")
        finally:
            history.end_group()
            # The store now holds the final layout (or whatever prefix
            # executed before an unexpected error — a Qt macro never
            # rolls back); run the established post-commit path: stale
            # ids drop and the frame re-syncs from the store. The final
            # preview frame is promoted first so a rotated/translated
            # frame survives the commit.
            self._promote_preview_frame()
            self._sync_undo_hud()
            self._revalidate_selection()
            self._sync_selection_animation()
            self.update()

    def _transform_release(self, gesture, wx: float, wy: float) -> None:
        """Close an armed resize/rotation gesture: commit, discard, no-op.

        A grip release below the drag threshold keeps the selection and
        writes nothing. A real drag rebuilds the *total* transform from
        the start frame and pointer (resize uses the remembered Shift
        state; rotate uses the total pointer bearing change) and commits
        through :meth:`_transform_commit`.
        """
        if not gesture.get("moved"):
            self._move_preview_clear()
            self._sync_selection_animation()
            self.update()
            return
        mode = gesture.get("mode")
        if mode == "resize":
            factors = self._resize_factors(gesture, wx, wy,
                                           bool(gesture.get("shift")))
            if factors is None:
                self._move_preview_clear()
                self.update()
                return
            sx, sy = factors
            ax, ay = gesture["anchor"]
            transform = GroupTransform(anchor=Point(ax, ay), sx=sx, sy=sy,
                                       frame_deg=float(gesture["angle"]))
            # Pin the final frame (the release position can differ from
            # the last move) so a successful commit promotes it.
            self._set_preview_frame(
                self._resized_frame_corners(gesture, sx, sy))
            text = "Resize"
        else:
            angle = self._rotation_angle(gesture, wx, wy)
            if angle is None:
                self._move_preview_clear()
                self.update()
                return
            px, py = gesture["pivot"]
            transform = GroupTransform(pivot=Point(px, py),
                                       angle_deg=angle)
            self._set_preview_frame(
                self._rotated_frame_corners(gesture, angle))
            text = "Rotate"
        self._transform_commit(gesture, transform, text)

    def _transform_commit(self, gesture, transform, text: str,
                          ids=None) -> None:
        """Two-phase preflight commit of one grouped resize/rotation.

        Phase 1 is read-only (``store.get``/``locate`` and the gesture's
        pristine snapshots; no mutating selection call). A vanished id is
        a survivor-only skip; a still-existing annotation that is not the
        gesture-start object, an unmappable offset, a member the transform
        cannot express, or a member without a home page aborts the whole
        operation *before* any macro opens — a group is never partially
        transformed. True no-op members are dropped; if every member is a
        no-op no history entry is created. The page assignment is always
        the member's home page (resize/rotation never reparent): the
        validated :class:`~app.undo.BatchMoveCommand` re-inserts every
        target at its pristine slot, so one grouped undo restores the
        exact original objects, pages, and paint order and one redo
        reproduces the committed geometry.

        ``ids`` is the frozen target list. It defaults to the live
        selection (interactive gestures); the command rotations pass the
        menu's snapshot ids so a selection changed while the menu was open
        is never operated on.
        """
        document = self._document
        try:
            store = document.annotations
        except Exception:
            store = None
        if store is None:
            self._move_preview_clear()
            self.update()
            return
        ids = (tuple(self._selection.selected_ids) if ids is None
               else tuple(ids))
        snapshots = gesture["snapshots"]
        homes = gesture.get("homes") or {}
        offsets = gesture.get("offsets") or {}
        entries: list = []
        try:
            for annotation_id in ids:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    # Vanished annotation: never a failure by itself;
                    # survivors proceed. The selection is cleaned only
                    # after a successful commit.
                    continue
                original = snapshots.get(annotation_id)
                if original is None or original is not current:
                    # An intervening edit replaced a still-existing
                    # selected annotation: never overwrite it — abort
                    # the whole operation before the macro opens.
                    raise _MoveConflict()
                offset = offsets.get(annotation_id)
                if offset is None:
                    raise _MoveConflict()
                candidate = apply_group(original, offset, transform)
                if candidate is None:
                    # An unsupported/invalid member is never silently
                    # skipped from a group: abort as a whole.
                    raise _MoveConflict()
                home = homes.get(annotation_id)
                if home is None:
                    raise _MoveConflict()
                if not geometry_changed(original, candidate):
                    continue  # true no-op member
                entries.append((annotation_id, candidate, int(home)))
        except _MoveConflict:
            self._move_preview_clear()
            self.update()
            return
        except Exception:
            self._move_preview_clear()
            self.update()
            return
        if not entries:
            # Every surviving member is a true no-op (or none survived):
            # no group opened, no entry, and the preview is discarded.
            self._move_preview_clear()
            self.update()
            return
        history = self._history_for(document, create=True)
        if history is None:
            self._move_preview_clear()
            self.update()
            return
        try:
            command = BatchMoveCommand(store, entries, text=text)
        except Exception:
            self._move_preview_clear()
            self.update()
            return
        try:
            history.push_prebuilt_grouped(command, text=text)
        finally:
            history.end_group()
            self._promote_preview_frame()
            self._sync_undo_hud()
            self._revalidate_selection()
            self._sync_selection_animation()
            self.update()

    # -- M7.1.4.2 direct vertex / box-handle editing -----------------------

    def _editable_vertex_target(self):
        """The single selected shape's editable handles, or ``None``.

        Returns ``(annotation_id, annotation, {handle: (wx, wy)})`` when
        exactly one selected annotation is a supported
        :class:`~annotations.models.Shape`:

        * Line/Arrow/Polygon expose every defining point (ordered; lines
          and arrows are start/end), keyed ``("vertex", index)``;
        * Rectangle/Ellipse expose the eight box handles (corners + edge
          midpoints), keyed ``("box", name)``, rotated about the box
          centre exactly like the painted shape.

        The displayed annotation (an active gesture's preview candidate
        when present) is mapped through its own page projection, so the
        handles sit pixel-exactly on the geometry under scrolling, zoom,
        and page rotation. Ink/highlighter strokes keep whole-object
        editing (no per-sample handles). Pure read: never mutates or
        stores anything.
        """
        try:
            if not self._selection.has_selection:
                return None
            ids = tuple(self._selection.selected_ids)
            if len(ids) != 1:
                return None
            annotation_id = ids[0]
            annotation = self._selection_annotation_for(annotation_id)
            if not isinstance(annotation, Shape):
                return None
            vertices = vertex_points(annotation)
            if vertices is not None:
                if len(vertices) > _MAX_VERTEX_HANDLES:
                    return None
                local = {("vertex", index): (float(point.x), float(point.y))
                         for index, point in enumerate(vertices)}
            else:
                box = box_handle_points(annotation)
                if box is None:
                    return None
                local = {("box", name): (float(point.x), float(point.y))
                         for name, point in box.items()}
            document = self._document
            if document is None:
                return None
            store = document.annotations
            if not store.contains(annotation_id):
                return None
            located = store.locate(annotation_id)
            if located is None:
                return None
            entry = self._pipeline.layout.entry(int(located[0]))
            handles = {}
            for key, (lx, ly) in local.items():
                mapped = self._page_widget_point(entry, lx, ly)
                if mapped is None:
                    continue
                handles[key] = (float(mapped[0]), float(mapped[1]))
            if not handles:
                return None
            return annotation_id, annotation, handles
        except Exception:
            return None

    def _vertex_handle_at(self, wx: float, wy: float):
        """Nearest editable vertex/box handle under a widget point."""
        info = self._editable_vertex_target()
        if info is None:
            return None
        try:
            wx = float(wx)
            wy = float(wy)
            best = None
            for key, (hx, hy) in info[2].items():
                distance = math.hypot(wx - hx, wy - hy)
                if distance > _VERTEX_HANDLE_HIT_PX:
                    continue
                if best is None or distance < best[0]:
                    best = (distance, key)
            return best[1] if best is not None else None
        except Exception:
            return None

    def _vertex_cursor_key(self, handle):
        """Cursor key for one editable handle.

        A free vertex reads as the four-way move cursor (it moves in any
        direction); a box corner/edge reads as the matching diagonal or
        axis resize cursor, derived from the *oriented* handle
        directions, so a rotated box still tells the truth.
        """
        try:
            if not handle:
                return None
            if handle[0] == "vertex":
                return "SizeAllCursor"
            info = self._editable_vertex_target()
            if info is None:
                return None
            points = {key[1]: value for key, value in info[2].items()
                      if key[0] == "box"}
            return cursor_key_for_handle(handle[1], points)
        except Exception:
            return None

    @staticmethod
    def _box_aspect_locked(shape) -> bool:
        """True for an Ellipse that is geometrically a circle.

        Recognition represents a circle as an Ellipse whose box is square
        (the circle path is the only producer of an equal-radii ellipse).
        Such a shape keeps its circular geometry during handle editing:
        uniform scaling about the opposite corner (corner handle) or about
        the box centre (edge handle as a radius drag). All other shapes
        resize freely per axis; a non-circular ellipse is never forced
        circular.
        """
        try:
            if not isinstance(shape, Shape) or shape.kind != ShapeKind.ELLIPSE:
                return False
            rect = shape.rect
            width = abs(float(rect.x1) - float(rect.x0))
            height = abs(float(rect.y1) - float(rect.y0))
            if width <= 0.0 or height <= 0.0:
                return False
            return abs(width - height) <= max(0.75, 0.02 * max(width, height))
        except Exception:
            return False

    def _vertex_press(self, handle, wx: float, wy: float,
                      kind: str) -> bool:
        """Arm a direct vertex/box-handle drag on a single selected shape.

        Takes the same gesture-start snapshot contract as the frame
        gestures (pristine store object, stable id, home page, press
        point) and nothing else: no store write, no history, no preview.
        A free vertex or box handle wins over the whole-object move,
        because the handle is the more specific target.
        """
        if handle is None or self._move_gesture is not None:
            return False
        info = self._editable_vertex_target()
        if info is None or handle not in info[2]:
            return False
        annotation_id, annotation, _handles = info
        document = self._document
        if document is None:
            return False
        try:
            store = document.annotations
            if not store.contains(annotation_id):
                return False
            located = store.locate(annotation_id)
            if located is None:
                return False
            home = int(located[0])
        except Exception:
            return False
        gesture = {
            "mode": "vertex",
            "kind": kind,
            "handle": handle,
            "ids": (annotation_id,),
            "snapshots": {annotation_id: annotation},
            "homes": {annotation_id: home},
            "start": (float(wx), float(wy)),
            "last": (float(wx), float(wy)),
            "moved": False,
            "hit": annotation_id,
            "aspect_lock": self._box_aspect_locked(annotation),
        }
        self._move_gesture = gesture
        return True

    def _vertex_pointer_pt(self, gesture, wx: float, wy: float):
        """The pointer in the edited shape's own page-local page-pt."""
        try:
            annotation_id = gesture["ids"][0]
            home = gesture["homes"].get(annotation_id)
            entry = self._pipeline.layout.entry(int(home))
            point = self._region_page_pt(entry, float(wx), float(wy))
            if point is None:
                return None
            return Point(float(point[0]), float(point[1]))
        except Exception:
            return None

    def _vertex_candidate(self, gesture, wx: float, wy: float):
        """Candidate annotation for the current handle pointer position.

        Recomputed from the gesture-start snapshot (never from the
        previous preview), so repeated moves are drift-free and a release
        without a final move still commits the truthful geometry.
        """
        try:
            annotation_id = gesture["ids"][0]
            original = gesture["snapshots"].get(annotation_id)
            handle = gesture.get("handle")
            if original is None or not handle:
                return None
            target = self._vertex_pointer_pt(gesture, wx, wy)
            if target is None:
                return None
            if handle[0] == "vertex":
                return move_vertex(original, int(handle[1]), target)
            return resize_box_handle(
                original, handle[1], target,
                aspect_lock=bool(gesture.get("aspect_lock")))
        except Exception:
            return None

    def _vertex_move(self, wx: float, wy: float) -> None:
        """Update the live vertex/box preview for one pointer position."""
        gesture = self._move_gesture
        if gesture is None or gesture.get("mode") != "vertex":
            return
        try:
            start_x, start_y = gesture["start"]
            gesture["last"] = (float(wx), float(wy))
            if not gesture.get("moved"):
                if (abs(float(wx) - start_x) < _MARQUEE_MIN_PX
                        and abs(float(wy) - start_y) < _MARQUEE_MIN_PX):
                    return
                gesture["moved"] = True
            candidate = self._vertex_candidate(gesture, wx, wy)
            if candidate is None:
                return
            annotation_id = gesture["ids"][0]
            home = gesture["homes"].get(annotation_id)
            before = self._selection_frame_screen_box()
            # The frame follows the edited geometry: a cached orientation
            # or a previous preview frame must not pin it.
            self._frame_state = None
            self._preview_frame = None
            self._move_preview_set({annotation_id: candidate},
                                   {annotation_id: home})
            after = self._selection_frame_screen_box()
            self._move_repaint(before, after)
        except Exception:
            self.update()

    def _vertex_release(self, gesture, wx: float, wy: float) -> None:
        """Close an armed vertex/box gesture: commit or discard.

        A sub-threshold press/release keeps the selection and writes
        nothing (the handle tap is still a click). A real drag commits
        exactly one grouped history entry; an unmappable or unchanged
        candidate discards the preview with the store untouched.
        """
        self._frame_state = None
        if not gesture.get("moved"):
            self._move_preview_clear()
            self._sync_selection_animation()
            self.update()
            return
        candidate = self._vertex_candidate(gesture, wx, wy)
        if candidate is None:
            self._move_preview_clear()
            self.update()
            return
        annotation_id = gesture["ids"][0]
        self._move_preview_set(
            {annotation_id: candidate},
            {annotation_id: gesture["homes"].get(annotation_id)})
        self._vertex_commit(gesture, candidate)

    def _vertex_commit(self, gesture, candidate) -> None:
        """Two-phase preflight commit of one direct shape edit.

        Same safety contract as the frame transforms: the current store
        object must still be the gesture-start snapshot (otherwise an
        intervening edit is never overwritten), the home page must be
        known, and a true no-op opens no history group. The validated
        same-page :class:`~app.undo.BatchMoveCommand` re-inserts the
        candidate at the pristine slot, so one undo restores the exact
        original object and paint order.
        """
        document = self._document
        try:
            store = document.annotations
        except Exception:
            store = None
        if store is None:
            self._move_preview_clear()
            self.update()
            return
        annotation_id = gesture["ids"][0]
        original = gesture["snapshots"].get(annotation_id)
        home = gesture["homes"].get(annotation_id)
        try:
            current = store.get(annotation_id)
        except Exception:
            current = None
        if (current is None or original is None or current is not original
                or home is None or candidate is None
                or not geometry_changed(original, candidate)):
            self._move_preview_clear()
            self.update()
            return
        history = self._history_for(document, create=True)
        if history is None:
            self._move_preview_clear()
            self.update()
            return
        text = "Edit Shape"
        try:
            command = BatchMoveCommand(
                store, [(annotation_id, candidate, int(home))], text=text)
        except Exception:
            self._move_preview_clear()
            self.update()
            return
        try:
            history.push_prebuilt_grouped(command, text=text)
        finally:
            history.end_group()
            self._move_preview_clear()
            # Geometry (and therefore the frame extents) changed: re-derive
            # the frame from the committed shapes rather than reusing the
            # pre-edit orientation.
            self._frame_state = None
            self._sync_undo_hud()
            self._revalidate_selection()
            self._sync_selection_animation()
            self.update()

    def _promote_preview_frame(self) -> None:
        """Commit a successful gesture's frame (preview -> persistent state).

        On success the live preview frame becomes the frame for the
        still-current selection, so a rotation is preserved into the next
        resize instead of collapsing back to an axis-aligned box. Every
        other exit (cancel, abort, no-op) uses ``_move_preview_clear``
        directly and deliberately leaves the last committed orientation.
        """
        promoted = self._preview_frame
        self._move_preview_clear()
        if promoted is not None:
            state = self._frame_from_widget_corners(
                promoted.get("ids"), promoted.get("corners"))
            if state is not None:
                self._frame_state = state

    def _move_cancel(self) -> None:
        """Cancel an armed/active group move; nothing was ever pushed.

        The store, history, selection, and HUD are exactly as they were
        before the gesture; the preview (if any) is discarded so the
        pristine annotations repaint.
        """
        gesture = self._move_gesture
        self._move_gesture = None
        # M6.8.2 Stage 5.2: same as release — never leave a drag cursor
        # behind after Escape/tool-switch/document-close cancellation.
        self._reset_hover_cursor()
        # ``_move_preview_clear`` also drops the transient preview frame, so
        # the frame falls back to the last committed orientation (``_frame_
        # state``) — exactly the desired cancel semantics.
        self._move_preview_clear()
        if gesture is not None:
            self.update()

    # -- M6.8.2 Stage 5 selection operations (menu + keyboard) -------------

    def _delete_selection(self) -> bool:
        """Delete the whole current selection (keyboard Delete path).

        Snapshots the *current* selection and routes through
        :meth:`_delete_targets`, so the keyboard shortcut shares one
        implementation with the context menu's Delete action.
        """
        if self._document_is_readonly():
            self._refuse_readonly("delete annotations")
            return False
        if self._move_gesture is not None:
            self._move_cancel()
        if self._region_gesture is not None:
            self._region_overlay_clear()
        ids = tuple(self._selection.selected_ids)
        if not ids:
            return False
        snapshots = self._selection_target_snapshots(ids)
        return self._delete_targets(ids, snapshots, "Delete Annotations")

    def delete_selected_annotation(self) -> bool:
        """Shortcut entry point: the exact gating the Delete key had.

        The shortcut registry owns the key now; this wrapper preserves the
        original conditions (selection tool, non-empty selection, no active
        gesture) and then reuses :meth:`_delete_selection`.
        """
        if (self._selection_tool_active()
                and self._selection.has_selection
                and self._move_gesture is None
                and self._region_gesture is None):
            return self._delete_selection()
        return False

    def cancel_selection(self) -> bool:
        """Shortcut entry point: cancel a group move, else deselect.

        Reuses the existing move-cancel and pure-view-state clearing paths;
        no store, page, or history state is touched.
        """
        if self._move_gesture is not None:
            self._move_cancel()
            return True
        if self._selection.has_selection:
            self._clear_selection_state()
            return True
        return False

    def _delete_targets(self, ids, snapshots, text: str) -> bool:
        """Delete every valid target as exactly one grouped undo entry.

        The complete plan is preconstructed and validated against the
        pristine store *before* the first command is pushed, so a later
        invalid target can never leave a partially deleted selection. A
        vanished id is a survivor-only skip; a still-existing annotation
        that is not the snapshot object aborts the whole operation (never
        delete a different object than the menu named). Same-page targets
        are submitted in descending original position order so the grouped
        undo — which runs children in reverse — re-inserts every object at
        its exact pristine slot, restoring objects, pages, and paint
        order. Returns True when a group was committed.
        """
        plan = self._build_delete_plan(ids, snapshots)
        if not plan:
            return False
        return self._commit_delete_plan(plan, text)

    def _build_delete_plan(self, ids, snapshots):
        """Prebuild + validate every delete command, or ``None``.

        Split out of :meth:`_delete_targets` for Cut, which must prepare
        (and prove) the complete deletion plan *before* publishing the
        clipboard payload and only then commit it — never a partial
        deletion. Pure read of the store; nothing is pushed or mutated.
        Commands come back sorted for grouped submission (ascending page,
        descending original position within a page).
        """
        document = self._document
        try:
            store = document.annotations
        except Exception:
            store = None
        if store is None:
            return None
        plan: list = []
        try:
            for annotation_id in ids:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    continue  # vanished: survivor-only skip
                original = snapshots.get(annotation_id)
                if original is not None and original is not current:
                    return None  # intervening replacement: abort whole
                located = store.locate(annotation_id)
                if located is None:
                    return None
                page_index, position = int(located[0]), int(located[1])
                plan.append((page_index, position,
                             DeleteAnnotationCommand(store, annotation_id)))
        except Exception:
            return None
        if not plan:
            return None
        plan.sort(key=lambda item: (item[0], -item[1]))
        return plan

    def _commit_delete_plan(self, plan, text: str) -> bool:
        """Submit a prebuilt delete plan as one grouped undo entry."""
        document = self._document
        if document is None or not plan:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        try:
            for _page, _pos, command in plan:
                history.push_prebuilt_grouped(command, text=text)
        except Exception:
            # A push can only fail if a target vanished between preflight
            # and submission: close the partial group and roll it back so
            # the store never keeps a prefix of the deletion plan.
            try:
                history.end_group()
                history.stack.undo()
            except Exception:
                pass
            self._sync_undo_hud()
            self._revalidate_selection()
            self._sync_selection_animation()
            self.update()
            return False
        history.end_group()
        self._sync_undo_hud()
        self._revalidate_selection()
        self._sync_selection_animation()
        self.update()
        return True

    # -- M6.8.2 clipboard slice (Cut / Copy / Paste) ----------------------

    def _selection_records(self):
        """``[(page_index, annotation), ...]`` for the current selection.

        Resolved against the current store through the selection's own
        self-healing rules (a vanished identity simply drops, exactly like
        Delete/Recolor), then ordered deterministically by
        ``(page index, store position)`` so a payload always carries
        document order regardless of how the selection was made
        (Selection, Lasso, Pan double-click, or a right-click under any
        tool). Returns ``()`` when nothing is selected.
        """
        document = self._document
        if document is None:
            return ()
        try:
            store = document.annotations
        except Exception:
            return ()
        try:
            annotations = self._selection.resolve(store)
        except Exception:
            return ()
        located: list = []
        for annotation in annotations:
            position = None
            try:
                found = store.locate(annotation.id)
                if found is not None:
                    position = (int(found[0]), int(found[1]))
            except Exception:
                position = None
            if position is None:
                return ()
            located.append((position[0], position[1], annotation))
        located.sort(key=lambda item: (item[0], item[1]))
        return tuple((page, annotation)
                     for page, _position, annotation in located)

    def _clipboard_feedback(self, title: str, detail: str = "",
                            kind: str = "error") -> None:
        """Report an operation failure through the app's feedback channel."""
        try:
            self.statusMessage.emit(str(title),
                                    "" if detail is None else str(detail),
                                    str(kind))
        except Exception:
            pass

    def _publish_payload(self, payload, action: str) -> bool:
        """Publish a validated payload; report why when the clipboard refuses."""
        try:
            from app import clipboard as clipboard_bridge

            if clipboard_bridge.write_payload(payload):
                return True
        except Exception:
            pass
        self._clipboard_feedback(
            f"Cannot {action}", "The system clipboard is unavailable.")
        return False

    def _clipboard_payload(self):
        """The validated Inkfall payload on the system clipboard, or None."""
        try:
            from app import clipboard as clipboard_bridge

            return clipboard_bridge.read_payload()
        except Exception:
            return None

    def _can_paste(self, page_index=None) -> bool:
        """True when Paste has a valid payload that fits this document.

        ``page_index`` is the menu's target page when known; a multi-page
        payload that would run past the last page is not offered at all
        (the same rule the commit path enforces), so the menu never shows
        a Paste action that can only fail.
        """
        payload = self._clipboard_payload()
        if payload is None:
            return False
        document = self._document
        if document is None:
            return False
        if page_index is None:
            return True
        try:
            return (int(page_index) + int(payload.page_span)
                    <= int(document.page_count))
        except Exception:
            return False

    def _copy_selection(self) -> bool:
        """Copy the whole current selection (menu + Ctrl+C share this).

        Captures a stable snapshot of every selected annotation, validates
        that the complete selection is representable, publishes one
        versioned payload to the system clipboard, and touches nothing
        else: geometry, identities, page assignment, paint order, the undo
        history, the selection, and the active tool all stay exactly as
        they were. An empty/unsupported/invalid selection fails before the
        clipboard is changed.
        """
        records = self._selection_records()
        if not records:
            return False
        try:
            from annotations.clipboard import ClipboardError
            from annotations.clipboard import payload_from_annotations

            payload = payload_from_annotations(records)
        except ClipboardError as exc:
            self._clipboard_feedback("Cannot copy selection", str(exc))
            return False
        except Exception:
            self._clipboard_feedback("Cannot copy selection",
                                     "The selection could not be encoded.")
            return False
        return self._publish_payload(payload, "copy")

    def _cut_selection(self) -> bool:
        """Copy the selection, then delete it as one grouped undo entry.

        Order is the safety contract: the payload is built and fully
        validated, the *complete* deletion plan is prebuilt against the
        pristine store, and only then is the clipboard published — so a
        serialization or preflight failure deletes nothing. The deletion
        itself is the same grouped operation the Delete command uses
        (exactly one undo entry, exact-order restoration on undo), and the
        clipboard payload is never derived from the history.
        """
        records = self._selection_records()
        if not records:
            return False
        # A keyboard Cut must not leave an armed move/region preview built
        # from annotations it is about to delete (the menu path already
        # cancels gestures before it opens).
        if self._move_gesture is not None:
            self._move_cancel()
        if self._region_gesture is not None:
            self._region_overlay_clear()
        snapshots = {annotation.id: annotation
                     for _page, annotation in records}
        try:
            from annotations.clipboard import ClipboardError
            from annotations.clipboard import payload_from_annotations

            payload = payload_from_annotations(records)
        except ClipboardError as exc:
            self._clipboard_feedback("Cannot cut selection", str(exc))
            return False
        except Exception:
            self._clipboard_feedback("Cannot cut selection",
                                     "The selection could not be encoded.")
            return False
        plan = self._build_delete_plan(tuple(snapshots), snapshots)
        if not plan:
            self._clipboard_feedback(
                "Cannot cut selection",
                "The selection changed; nothing was deleted.")
            return False
        # An eraser gesture's open undo macro must not swallow the cut.
        self._finalize_eraser()
        if not self._publish_payload(payload, "cut"):
            return False
        return self._commit_delete_plan(plan, "Cut Annotations")

    def _paste_clipboard(self, anchor=None) -> bool:
        """Paste the clipboard payload as new annotations (menu + Ctrl+V).

        ``anchor`` is the ``(page_index, page_local_x, page_local_y)``
        captured by the invoking context menu (``None`` for the keyboard
        path, which falls back to the last meaningful pointer position and
        then to the visible page's centre). The whole insertion plan is
        validated — destination pages in range, every candidate built with
        a fresh identity and destination page id — before the first object
        is added, and the complete paste is one grouped undo entry.
        """
        if self._document_is_readonly():
            self._refuse_readonly("paste annotations")
            return False
        payload = self._clipboard_payload()
        if payload is None:
            self._clipboard_feedback(
                "Nothing to paste",
                "The clipboard does not contain Inkfall annotations.")
            return False
        document = self._document
        if document is None or self._pipeline is None:
            return False
        # A keyboard Paste replaces the selection: drop any armed preview
        # built from the outgoing selection before computing placement.
        if self._move_gesture is not None:
            self._move_cancel()
        if self._region_gesture is not None:
            self._region_overlay_clear()
        target = self._paste_target(anchor)
        if target is None:
            self._clipboard_feedback(
                "Cannot paste", "No page is available for placement.")
            return False
        page_index, lx, ly = target
        plan = self._build_paste_plan(payload, page_index)
        if plan is None:
            return False
        placement = self._paste_placement(plan, page_index, lx, ly)
        if placement is None:
            self._clipboard_feedback(
                "Cannot paste", "The placement could not be resolved.")
            return False
        plan = self._build_paste_plan(payload, page_index, offset=placement)
        if plan is None:
            return False
        store = document.annotations
        ids = [annotation.id for _page, annotation in plan]
        # Revalidate immediately before mutation: fresh identities must be
        # free, every destination page must still exist, and the document
        # identity must not have changed under the prepared plan.
        try:
            if document is not self._document or document.is_closed:
                return False
            for page, _annotation in plan:
                if not 0 <= int(page) < int(document.page_count):
                    return False
            if len({str(a.value) for a in ids}) != len(ids):
                return False
            if any(store.contains(annotation_id) for annotation_id in ids):
                return False
        except Exception:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        self._finalize_eraser()
        try:
            from app.undo import AddAnnotationCommand

            commands = [AddAnnotationCommand(store, annotation, int(page))
                        for page, annotation in plan]
        except Exception:
            return False
        try:
            for command in commands:
                history.push_prebuilt_grouped(command, text="Paste Annotations")
        except Exception:
            # Atomicity guard: a failed insertion closes and rolls back the
            # partial group instead of leaving a prefix of the paste behind.
            try:
                history.end_group()
                history.stack.undo()
            except Exception:
                pass
            self._sync_undo_hud()
            self._sync_selection_animation()
            self.update()
            return False
        history.end_group()
        # After a successful commit the inserted copies become the
        # selection, so their shared frame is visible and immediately
        # manipulable under the active tool.
        try:
            changed = self._selection.set(ids)
        except Exception:
            changed = False
        if changed:
            self._selection_phase = 0.0
        self._frame_state = None
        self._sync_selection_animation()
        self._sync_undo_hud()
        self.update()
        return True

    def _build_paste_plan(self, payload, anchor_page: int, offset=(0.0, 0.0)):
        """Validate and build the whole paste plan, or ``None``.

        Every destination page is range-checked and every annotation is
        constructed (fresh identity, destination page id, translated
        geometry) inside the Qt-free :func:`annotations.clipboard.paste_plan`
        before anything is inserted.
        """
        document = self._document
        if document is None:
            return None
        try:
            from annotations.clipboard import ClipboardError, paste_plan
            from document.models import page_id_for

            document_id = document.id
            plan = paste_plan(
                payload, anchor_page=int(anchor_page),
                page_count=int(document.page_count),
                page_id_for_index=lambda index: page_id_for(document_id,
                                                            index),
                offset=offset)
        except ClipboardError as exc:
            self._clipboard_feedback("Cannot paste", str(exc))
            return None
        except Exception:
            self._clipboard_feedback("Cannot paste",
                                     "The payload could not be placed.")
            return None
        return plan or None

    def _paste_target(self, anchor):
        """Resolve the paste anchor to page-local pt, or ``None``.

        A menu invocation supplies its own page + page-local pointer
        point, revalidated against the current page count. The keyboard
        path prefers the last meaningful pointer position inside a page
        and otherwise uses the centre of the current visible page region —
        never a stale widget-space coordinate.
        """
        layout = self._pipeline.layout if self._pipeline is not None else None
        if layout is None:
            return None
        if anchor is not None:
            try:
                page_index, lx, ly = anchor
                page_index = int(page_index)
                lx = float(lx)
                ly = float(ly)
            except (TypeError, ValueError):
                return None
            if not 0 <= page_index < layout.page_count:
                return None
            if not (math.isfinite(lx) and math.isfinite(ly)):
                return None
            return (page_index, lx, ly)
        remembered = self._last_pointer_pt
        if remembered is not None:
            try:
                page_index, lx, ly = remembered
                page_index = int(page_index)
            except (TypeError, ValueError):
                page_index = -1
            if 0 <= page_index < layout.page_count \
                    and self._page_local_inside(page_index, lx, ly):
                return (page_index, float(lx), float(ly))
        page_index = self._current_page_index()
        if page_index is None or not 0 <= int(page_index) < layout.page_count:
            return None
        return self._page_local_centre(int(page_index))

    def _page_local_inside(self, page_index: int, lx, ly) -> bool:
        """True when a page-local point lies within that page's pt bounds."""
        try:
            entry = self._pipeline.layout.entry(int(page_index))
            bucket = float(self._pipeline.layout.raster_scale)
            width = float(entry.render_width_px) / bucket
            height = float(entry.render_height_px) / bucket
            lx = float(lx)
            ly = float(ly)
        except Exception:
            return False
        return 0.0 <= lx <= width and 0.0 <= ly <= height

    def _page_local_centre(self, page_index: int):
        """Page-local pt centre of one page's displayed frame."""
        try:
            entry = self._pipeline.layout.entry(int(page_index))
            bucket = float(self._pipeline.layout.raster_scale)
            return (int(page_index),
                    float(entry.render_width_px) / bucket / 2.0,
                    float(entry.render_height_px) / bucket / 2.0)
        except Exception:
            return None

    def _paste_placement(self, plan, page_index: int, lx: float, ly: float):
        """Page-pt translation that lands the group just past the pointer.

        The group's *projected* widget-space bounds (using the real
        layout, so per-page size/rotation and the shared scale+translate
        projection are honoured) are placed at the invocation point plus
        :data:`_PASTE_SCREEN_OFFSET` px, clamped so the group's top-left
        stays on the anchor page. The result is a single page-pt delta —
        never widget pixels added to PDF points — so zoom, scrolling, and
        page rotation cannot skew the placement.
        """
        pipeline = self._pipeline
        if pipeline is None:
            return None
        factor = self._move_group_factor()
        if not factor or factor <= 0.0:
            return None
        try:
            entry = pipeline.layout.entry(int(page_index))
        except Exception:
            return None
        pointer = self._page_widget_point(entry, float(lx), float(ly))
        if pointer is None:
            return None
        bounds = None
        try:
            for destination, annotation in plan:
                extent = annotation_extent(annotation)
                if extent is None:
                    return None
                page_entry = pipeline.layout.entry(int(destination))
                corners = ((extent[0], extent[1]), (extent[2], extent[1]),
                           (extent[2], extent[3]), (extent[0], extent[3]))
                for cx, cy in corners:
                    point = self._page_widget_point(page_entry, cx, cy)
                    if point is None:
                        return None
                    if bounds is None:
                        bounds = [point[0], point[1], point[0], point[1]]
                    else:
                        bounds[0] = min(bounds[0], point[0])
                        bounds[1] = min(bounds[1], point[1])
                        bounds[2] = max(bounds[2], point[0])
                        bounds[3] = max(bounds[3], point[1])
        except Exception:
            return None
        if bounds is None:
            return None
        target_x = float(pointer[0]) + _PASTE_SCREEN_OFFSET
        target_y = float(pointer[1]) + _PASTE_SCREEN_OFFSET
        page_rect = self._region_page_screen(entry)
        if page_rect is not None:
            try:
                target_x = min(max(target_x, float(page_rect.x) + 2.0),
                               float(page_rect.x + page_rect.width) - 2.0)
                target_y = min(max(target_y, float(page_rect.y) + 2.0),
                               float(page_rect.y + page_rect.height) - 2.0)
            except Exception:
                pass
        return ((target_x - float(bounds[0])) / factor,
                (target_y - float(bounds[1])) / factor)

    def _recolor_targets(self, ids, snapshots, *, stroke=None,
                         fill=None) -> bool:
        """Recolor every applicable target as one grouped undo entry.

        Mirrors the deletion preflight: every replacement command is
        preconstructed and validated before any is pushed, a vanished id
        is a survivor-only skip, and a still-existing annotation that is
        not the snapshot object aborts the whole operation. Members the
        requested color does not apply to (or that already carry it) are
        skipped by the Qt-free :func:`annotations.recolor.recolored`
        policy. Selecting a *fill* color additionally enables the fill for
        fill-capable shapes (M7.1.4.1), even when its color already
        matches; when no member actually changes, no history entry opens.
        Returns True when a group was committed.
        """
        document = self._document
        try:
            store = document.annotations
        except Exception:
            store = None
        if store is None:
            return False
        commands: list = []
        try:
            for annotation_id in ids:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    continue  # vanished: survivor-only skip
                original = snapshots.get(annotation_id)
                if original is not None and original is not current:
                    return False  # intervening replacement: abort whole
                replacement = recolored(current, stroke=stroke, fill=fill)
                # M7.1.4.1: choosing a fill color expresses fill intent.
                # ``recolored`` stays a pure color policy (it never touches
                # ``fill_enabled``); the menu layer enables the fill here,
                # even when the chosen color equals the existing one, so a
                # hollow shape (recognized or hand-drawn) actually renders
                # the requested fill.
                if fill is not None and supports_fill_color(current):
                    candidate = (replacement if replacement is not None
                                 else current)
                    if not bool(getattr(candidate, "fill_enabled", False)):
                        replacement = dataclasses.replace(
                            candidate, fill_enabled=True)
                if replacement is None:
                    continue  # unsupported type or already this color
                commands.append(ReplaceAnnotationCommand(
                    store, annotation_id, [replacement], text="Recolor"))
        except Exception:
            return False
        if not commands:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        try:
            for command in commands:
                history.push_prebuilt_grouped(command, text="Recolor")
        finally:
            history.end_group()
            self._sync_undo_hud()
            self._revalidate_selection()
            self._sync_selection_animation()
            self.update()
        return True

    def _rotate_selection(self, ids, snapshots, angle_deg, text: str) -> bool:
        """Rotate the whole snapshot selection about its shared group center.

        Reuses the interactive rotation machinery rather than a second
        geometry implementation: the pivot is the *same* padded
        selection-frame centre (in shared page-pt space) the rotation grip
        uses, each member is rotated through the Qt-free
        :class:`~annotations.transform.GroupTransform` about that pivot
        with its own page offset, and the commit runs through
        :meth:`_transform_commit` (all-or-nothing, one grouped undo, page
        assignments untouched). The oriented frame is rotated with the
        contents and promoted so the persistent frame stays in agreement
        with the new geometry. A request that cannot be expressed for a
        supported member, or that is a genuine no-op, opens no entry.
        """
        document = self._document
        pipeline = self._pipeline
        if document is None or pipeline is None:
            return False
        try:
            store = document.annotations
        except Exception:
            return False
        corners_w = self._selection_frame_corners()
        if corners_w is None:
            return False
        factor = self._move_group_factor()
        if not factor or factor <= 0.0:
            return False
        geom = self._frame_geometry(corners_w)
        if geom is None:
            return False
        centre = geom["centre"]
        half = geom["half"]
        pivot_x = float(centre[0]) / factor
        pivot_y = float(centre[1]) / factor
        members: dict = {}
        homes: dict = {}
        offsets: dict = {}
        try:
            layout = pipeline.layout
            for annotation_id in ids:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    continue  # vanished: survivor-only skip
                original = snapshots.get(annotation_id)
                if original is not None and original is not current:
                    return False  # intervening replacement: abort whole
                located = store.locate(annotation_id)
                if located is None:
                    return False
                home = int(located[0])
                try:
                    offset = self._move_entry_offset(layout.entry(home))
                except Exception:
                    offset = None
                if offset is None:
                    # Cannot express this member's shared-space offset:
                    # abort the whole group rather than rotate partially.
                    return False
                members[annotation_id] = current
                homes[annotation_id] = home
                offsets[annotation_id] = offset
        except Exception:
            return False
        if not members:
            return False
        angle = normalize_deg(float(angle_deg))
        gesture = {
            "mode": "rotate",
            "ids": tuple(members),
            "snapshots": members,
            "homes": homes,
            "offsets": offsets,
            "factor": factor,
            "pivot": (pivot_x, pivot_y),
            "centre_w": (float(centre[0]), float(centre[1])),
            "half_w": (float(half[0]), float(half[1])),
            "angle": float(geom["angle"]),
        }
        transform = GroupTransform(pivot=Point(pivot_x, pivot_y),
                                   angle_deg=angle)
        try:
            self._set_preview_frame(
                self._rotated_frame_corners(gesture, angle))
        except Exception:
            pass
        self._transform_commit(gesture, transform, text,
                               ids=tuple(members))
        return True

    def _pan_point_in_frame(self, wx: float, wy: float) -> bool:
        """True when a widget point lies within the shared selection frame.

        The frame is the same padded widget-space union the Selection tool
        draws, so the whitespace between annotations in a multi-selection
        counts as "inside". An unmeasurable frame (no selection geometry)
        reports outside.

        Shared by Pan's retain/clear rule and — M6.8.2 Stage 5.2 — by the
        frame-interior group move (:meth:`_frame_interior_hit`), so both
        use one containment definition.
        """
        corners = self._selection_frame_corners()
        if corners is None:
            return False
        # Convex-quad containment (sign-consistent cross products), so a
        # rotated frame is tested against the oriented quad rather than its
        # looser axis-aligned bound. For an unrotated frame this is exactly
        # the inclusive rectangle test it replaces.
        try:
            wx = float(wx)
            wy = float(wy)
            sign = 0
            n = len(corners)
            for i in range(n):
                ax, ay = corners[i]
                bx, by = corners[(i + 1) % n]
                cross = (bx - ax) * (wy - ay) - (by - ay) * (wx - ax)
                if cross == 0.0:
                    continue
                s = 1 if cross > 0.0 else -1
                if sign == 0:
                    sign = s
                elif s != sign:
                    return False
            return True
        except Exception:
            return False

    def _pan_clear_selection(self) -> bool:
        """Clear the whole selection, stop the frame animation, repaint.

        Shared by every Pan deselect path: the immediate press-outside
        clear, the real-drag clear, and the empty-space double-click. Pure
        view state: never touches the store or history. There is no
        deferred callback any more, so nothing can re-touch the selection
        afterwards. Returns True when a selection was actually cleared.
        """
        try:
            if not self._selection.has_selection:
                return False
            # The shared clear also drops the cached oriented frame and any
            # selection cursor (Stage 5.2.1), so no stale grip/move cursor
            # can survive the deselect.
            self._clear_selection_state()
            self.update()
            return True
        except Exception:
            return False

    def _pan_press_resolve(self, wx: float, wy: float) -> None:
        """Immediate Pan deselect: a stationary press outside the frame.

        M6.8.2 fix: clears the selection *on press* (never after
        ``doubleClickInterval``). A press inside the frame retains it; an
        empty canvas or another annotation outside the frame clears it.
        The subsequent single click never selects — Pan needs a
        double-click. Pure view state.
        """
        try:
            if not self._selection.has_selection:
                return
            if self._pan_point_in_frame(float(wx), float(wy)):
                return
            self._pan_clear_selection()
        except Exception:
            pass

    def _note_pan_move(self, wx: float, wy: float) -> None:
        """Detect the start of a real Pan drag and deselect immediately.

        Uses the existing movement threshold. The moment pointer movement
        crosses it — regardless of whether the drag started inside the
        shared frame, outside it, or crosses its boundary — the whole
        selection is cleared and its animation stopped, well before mouse
        release. The pan itself is untouched (selection is view-only), so
        scrolling continues exactly as before.
        """
        anchor = self._pan_press_point
        if anchor is None or self._pan_dragged:
            return
        if (abs(wx - anchor[0]) >= _MARQUEE_MIN_PX
                or abs(wy - anchor[1]) >= _MARQUEE_MIN_PX):
            self._pan_dragged = True
            self._pan_last_tap = None   # a drag breaks any double-tap chain
            self._pan_clear_selection()
            # Tool cursors: the drag is now real — switch to the closed hand at the
            # moment the gesture becomes a drag (keyed, so it is one set).
            self._refresh_tool_cursor()

    def _pan_double_click_select(self, wx: float, wy: float) -> bool:
        """Pan tool: a double-click/double-tap picks an annotation (M6.8.1).

        The Pan tool keeps its normal single-click/drag panning; a
        double-click (mouse) or double-tap (stylus) is Inkfall's secondary
        select affordance and shows the same shared animated frame while
        Pan stays active. A double-click on an annotation replaces the
        selection in exactly one operation — the earlier first tap may
        already have cleared the previous selection (immediate press
        deselect), so this must still complete the new one. A double-click
        on empty space *outside* the shared frame clears the selection — no
        accidental pan/zoom (a double-click carries no drag). Pure view
        state: no store write, no history entry. Returns True when the
        selection changed.
        """
        try:
            candidate = self._annotation_at_widget(float(wx), float(wy))
        except Exception:
            candidate = None
        if candidate is None:
            # Empty space: outside the frame clears it; inside the frame
            # it retains.
            if not self._pan_point_in_frame(float(wx), float(wy)):
                self._pan_clear_selection()
            return False
        changed = False
        try:
            changed = self._selection.set([candidate.id])
        except Exception:
            changed = False
        if changed:
            self._selection_phase = 0.0
        self._sync_selection_animation()
        self.update()
        return changed

    def _pan_tablet_press(self, wx: float, wy: float, now_ms: int) -> None:
        """Stylus Pan press: immediate deselect + double-tap recognition.

        A stylus tap never produces a ``MouseButtonDblClick``, so the
        double-tap is recognised here from the tablet stream: two
        stationary taps within the platform double-click interval and the
        start-drag spatial threshold select the tapped annotation (or
        clear on empty space outside the frame), exactly like a mouse
        double-click. The first tap deselects immediately when it lands
        outside the shared frame. Never selects on a single tap.
        """
        last = self._pan_last_tap
        is_double = False
        try:
            if last is not None:
                interval = QApplication.doubleClickInterval()
                span = abs(wx - last[1]) + abs(wy - last[2])
                if (0 <= now_ms - last[0] <= interval
                        and span <= QApplication.startDragDistance()):
                    is_double = True
        except Exception:
            is_double = False
        self._pan_press_point = (float(wx), float(wy))
        self._pan_dragged = False
        if is_double:
            self._pan_last_tap = None
            # One-shot guard: if the platform also synthesizes a mouse
            # double-click for this stylus double-tap, drop it so the
            # selection runs exactly once.
            self._pan_tablet_dbl = True
            self._pan_double_click_select(float(wx), float(wy))
            return
        self._pan_tablet_dbl = False
        self._pan_last_tap = (int(now_ms), float(wx), float(wy))
        self._pan_press_resolve(float(wx), float(wy))

    # -- M6.8.1 shared selection frame + animation -------------------------

    def _page_widget_point(self, entry, lx: float, ly: float):
        """Forward of ``_region_page_pt``: page-local pt -> widget pt.

        The same mapping the paint path and input mapping use, so the
        shared selection frame stays pixel-aligned with the ink under
        scrolling, zoom, page offsets, mixed page sizes, and effective
        rotation (rotation is baked once into ``entry`` geometry, so the
        projection remains a pure scale+translate).
        """
        pipeline = self._pipeline
        if pipeline is None:
            return None
        try:
            layout = pipeline.layout
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return None
            scale = self._display_scale()
            if scale <= 0:
                return None
            vw_render, _vh_render = self._render_viewport()
            x_off = DocumentLayout.page_x_origin(
                vw_render, float(layout.total_width_px),
                float(entry.render_width_px))
            wx = (x_off + float(lx) * bucket
                  - self._scroll_doc_x()) * scale
            wy = (float(entry.y_offset_px) + float(ly) * bucket
                  - self._scroll_doc()) * scale
            return (wx, wy)
        except Exception:
            return None

    def _selection_annotation_for(self, annotation_id):
        """What is being shown now for a selected id (M6.8.2).

        An active group-move preview substitutes for its store
        annotation (same stable id); otherwise the authoritative store
        object is returned. Pure read: neither branch mutates anything.
        """
        preview = self._transform_preview
        if preview is not None:
            try:
                candidate = preview.get(annotation_id)
            except Exception:
                candidate = None
            if candidate is not None:
                return candidate
        document = self._document
        if document is None:
            return None
        try:
            return document.annotations.get(annotation_id)
        except Exception:
            return None

    def _selection_extent_widget(self):
        """Widget-space union of every selected annotation's extent.

        Returns ``(minx, miny, maxx, maxy)`` in widget coordinates, or
        ``None`` when nothing selected has measurable geometry. Each
        annotation is mapped through *its own* page transform before the
        union is taken, so a single shared frame can enclose a selection
        that spans several pages; a selection on one page degenerates to
        that annotation's own padded box.
        """
        document = self._document
        pipeline = self._pipeline
        if document is None or pipeline is None:
            return None
        try:
            layout = pipeline.layout
        except Exception:
            return None
        xs: list = []
        ys: list = []
        for annotation_id in self._selection.selected_ids:
            # M6.8.2: an active group-move preview substitutes so the
            # shared frame tracks the moved candidates, too.
            annotation = self._selection_annotation_for(annotation_id)
            if annotation is None:
                continue
            index = self._page_index_for(getattr(annotation, "page_id", None))
            if index is None:
                continue
            try:
                entry = layout.entry(index)
            except Exception:
                continue
            extent = annotation_extent(annotation)
            if extent is None:
                continue
            minx, miny, maxx, maxy = extent
            p0 = self._page_widget_point(entry, minx, miny)
            p1 = self._page_widget_point(entry, maxx, maxy)
            if p0 is None or p1 is None:
                continue
            xs.extend((p0[0], p1[0]))
            ys.extend((p0[1], p1[1]))
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def _selection_frame_corners(self):
        """The oriented frame's four widget corners (clockwise from NW).

        Returns ``((nw), (ne), (se), (sw))`` or ``None`` when there is
        nothing to frame. The frame is a *rectangle*: a rigid rotation of
        the padded extent, so a live transform can rotate and scale it in
        lock-step with the annotations it encloses.

        The frame is cached *view-independently* (page-local centre,
        half-extents in page-pt, and orientation in degrees — see
        :meth:`_frame_from_widget_corners`), so it stays glued to the
        content across zoom, scroll, and page-offset changes while still
        remembering the orientation a rotation left behind:

        * while a gesture previews, ``_preview_frame`` is authoritative;
        * otherwise ``_frame_state`` is reused when it belongs to the
          *same* selected ids;
        * a changed/invalid selection recomputes a fresh, axis-aligned
          frame from the (preview-substituted) annotation extents.

        Pure view state: nothing here is stored on an annotation.
        """
        try:
            ids = tuple(self._selection.selected_ids)
            preview = self._preview_frame
            if preview is not None and preview.get("ids") == ids:
                return preview.get("corners")
            state = self._frame_state
            if state is not None and state.get("ids") == ids:
                corners = self._frame_state_corners(state)
                if corners:
                    return corners
            extent = self._selection_extent_widget()
            if extent is None:
                return None
            minx, miny, maxx, maxy = extent
            pad = _SELECT_FRAME_PAD_PX
            corners = (
                (minx - pad, miny - pad),
                (maxx + pad, miny - pad),
                (maxx + pad, maxy + pad),
                (minx - pad, maxy + pad),
            )
            self._frame_state = self._frame_from_widget_corners(ids, corners)
            return corners
        except Exception:
            return None

    @staticmethod
    def _frame_corners_widget(cx, cy, hu, hv, angle_deg):
        """Four widget corners from centre, half-extents, and orientation.

        ``hu``/``hv`` are the *displayed* half-extents (already padded);
        ``angle_deg`` is the clockwise y-down orientation of the top edge.
        Corners are returned clockwise from the north-west.
        """
        rad = math.radians(float(angle_deg))
        c = math.cos(rad)
        s = math.sin(rad)
        ux, uy = c, s
        vx, vy = -s, c
        return (
            (cx - ux * hu - vx * hv, cy - uy * hu - vy * hv),
            (cx + ux * hu - vx * hv, cy + uy * hu - vy * hv),
            (cx + ux * hu + vx * hv, cy + uy * hu + vy * hv),
            (cx - ux * hu + vx * hv, cy - uy * hu + vy * hv),
        )

    @staticmethod
    def _frame_geometry(corners_w):
        """View-space centre, *unpadded* half-extents, and angle of a quad."""
        try:
            nw, ne, se, sw = corners_w
            cx = (nw[0] + se[0]) / 2.0
            cy = (nw[1] + se[1]) / 2.0
            angle = normalize_deg(math.degrees(
                math.atan2(ne[1] - nw[1], ne[0] - nw[0])))
            hu = max(0.0, math.hypot(ne[0] - nw[0],
                                     ne[1] - nw[1]) / 2.0
                     - _SELECT_FRAME_PAD_PX)
            hv = max(0.0, math.hypot(sw[0] - nw[0],
                                     sw[1] - nw[1]) / 2.0
                     - _SELECT_FRAME_PAD_PX)
            return {"centre": (cx, cy), "half": (hu, hv), "angle": angle}
        except Exception:
            return None

    def _frame_widget_to_local(self, corners):
        """Map widget points into page-0 local pt (a fixed content chart)."""
        try:
            entry = self._pipeline.layout.entry(0)
        except Exception:
            return None
        out = []
        for wx, wy in corners:
            page = self._region_page_pt(entry, float(wx), float(wy))
            if page is None:
                return None
            out.append((float(page[0]), float(page[1])))
        return tuple(out)

    def _frame_from_widget_corners(self, ids, corners_w):
        """Cache a frame in view-independent form; ``None`` if unmappable."""
        geom = self._frame_geometry(corners_w)
        if geom is None:
            return None
        factor = self._move_group_factor()
        if factor is None or factor <= 0.0:
            return None
        centre_local = self._frame_widget_to_local((geom["centre"],))
        if centre_local is None:
            return None
        hu, hv = geom["half"]
        return {
            "ids": ids,
            "center": centre_local[0],
            "half": (hu / factor, hv / factor),
            "angle": geom["angle"],
        }

    def _frame_state_corners(self, state):
        """Widget corners of a cached view-independent frame (or ``None``)."""
        try:
            factor = self._move_group_factor()
            if factor is None or factor <= 0.0:
                return None
            entry = self._pipeline.layout.entry(0)
            lx, ly = state["center"]
            centre = self._page_widget_point(entry, float(lx), float(ly))
            if centre is None:
                return None
            pad = _SELECT_FRAME_PAD_PX
            hu = float(state["half"][0]) * factor + pad
            hv = float(state["half"][1]) * factor + pad
            return self._frame_corners_widget(
                float(centre[0]), float(centre[1]), hu, hv,
                float(state["angle"]))
        except Exception:
            return None

    def _selection_frame_widget_rect(self):
        """Widget-space ``(left, top, width, height)`` bounding the frame.

        ``None`` when there is nothing to frame. For an unrotated frame
        this is the padded extent exactly; for a rotated frame it is the
        axis-aligned bound of the oriented quad (used for scoped repaint
        regions, never for drawing the frame — see
        :meth:`_selection_frame_corners`).

        Pure *selection geometry*: it never grows to cover the grips. The
        grip-inclusive repaint bound is
        :meth:`_selection_frame_paint_rect`.
        """
        corners = self._selection_frame_corners()
        if corners is None:
            return None
        try:
            xs = [float(c[0]) for c in corners]
            ys = [float(c[1]) for c in corners]
            minx, miny = min(xs), min(ys)
            maxx, maxy = max(xs), max(ys)
            return (minx, miny, maxx - minx, maxy - miny)
        except Exception:
            return None

    def _selection_frame_paint_rect(self):
        """``_selection_frame_widget_rect`` grown to cover the grips.

        The grips are part of the frame's painted footprint but reach
        outside the oriented quad: the rotation grip and its connector sit
        a fixed screen distance beyond the oriented top edge. Excluding
        them left their previous pixels outside every scoped repaint during
        a rotation drag — painted once, then never invalidated again —
        which is exactly the "temporary-looking marks left on the canvas"
        defect (M6.8.2 Stage 5.2 corrective).

        Only repaint scoping uses this: the drawn frame, hit-testing, and
        ``_selection_frame_widget_rect``'s geometry contract are unchanged.
        """
        rect = self._selection_frame_widget_rect()
        if rect is None:
            return None
        try:
            left, top, width, height = (float(v) for v in rect)
            minx, miny = left, top
            maxx, maxy = left + width, top + height
            points = self._selection_handle_points()
            if points:
                half = _HANDLE_SIZE_PX / 2.0
                for name in _HANDLE_NAMES:
                    hx, hy = points[name]
                    minx = min(minx, hx - half)
                    maxx = max(maxx, hx + half)
                    miny = min(miny, hy - half)
                    maxy = max(maxy, hy + half)
                tox, toy = points["n"]
                rx, ry = points["rotate"]
                rx0 = min(tox, rx) - _ROTATE_HANDLE_RADIUS_PX
                rx1 = max(tox, rx) + _ROTATE_HANDLE_RADIUS_PX
                ry0 = min(toy, ry) - _ROTATE_HANDLE_RADIUS_PX
                ry1 = max(toy, ry) + _ROTATE_HANDLE_RADIUS_PX
                minx = min(minx, rx0)
                maxx = max(maxx, rx1)
                miny = min(miny, ry0)
                maxy = max(maxy, ry1)
            # M7.1.4.2: the round direct-edit handles are part of the
            # painted footprint too (a dragged vertex can sit exactly on
            # the unscaled extent boundary).
            info = self._editable_vertex_target()
            if info is not None:
                pad = _VERTEX_HANDLE_RADIUS_PX + 1.5
                for hx, hy in info[2].values():
                    minx = min(minx, hx - pad)
                    maxx = max(maxx, hx + pad)
                    miny = min(miny, hy - pad)
                    maxy = max(maxy, hy + pad)
            return (minx, miny, maxx - minx, maxy - miny)
        except Exception:
            return rect

    # -- M6.8.2 Stage 5.2 selection hover cursors --------------------------

    def _handle_cursor_key(self, handle):
        """Cursor key for a frame grip, following its on-screen direction."""
        try:
            points = self._selection_handle_points()
        except Exception:
            points = None
        return cursor_key_for_handle(handle, points)

    def _selection_cursor_key(self, wx: float, wy: float, dragging: bool = False):
        """Cursor key for a pointer position (``None`` = tool default).

        Precedence mirrors the press routing exactly — rotation grip,
        resize grips, then the movable interior of the selection frame — so
        the cursor always tells the truth about what a press would do.

        M6.8.2 Stage 5.2.1 cursor contract:

        * a direct vertex/box handle of a single selected shape -> the
          four-way move cursor (vertices) or the matching directional
          resize cursor (box handles);
        * rotation grip -> the dedicated rotate cursor;
        * resize grips -> the diagonal/horizontal/vertical resize cursor
          matching the grip's *on-screen* direction;
        * a movable selected annotation body, and the empty interior of the
          oriented frame -> the four-way ``SizeAllCursor``, never a
          hand/pan cursor;
        * anything else -> ``None``, i.e. the active tool's own cursor.

        The frame is visible under *every* tool (a context-menu selection
        under Pen/Pan/Eraser included), so the grip/body/interior cursors
        are available wherever a press would really manipulate it - and a
        point outside the frame resolves to ``None`` so the tool's own
        cursor (Pan's, or the drawing tools') is never overridden there.
        """
        try:
            if not self._selection_frame_visible():
                return None
            if dragging and self._region_gesture is not None:
                # A marquee/lasso owns the pointer: it is collecting a new
                # region, so the tool's own cursor is the truthful one.
                return None
            gesture = self._move_gesture
            if dragging and gesture is not None:
                mode = gesture.get("mode")
                if mode == "move":
                    # The four-way move cursor is the drag cursor too: the
                    # gesture keeps communicating "moving an object".
                    return "SizeAllCursor"
                if mode in ("resize", "rotate"):
                    return self._handle_cursor_key(gesture.get("handle"))
                if mode == "vertex":
                    return self._vertex_cursor_key(gesture.get("handle"))
                return None
            if not self._selection.has_selection:
                return None
            # A direct vertex/box handle outranks the padded frame grips:
            # the cursor matches the routing precedence in the press path.
            vertex = self._vertex_handle_at(wx, wy)
            if vertex is not None:
                return self._vertex_cursor_key(vertex)
            handle = self._transform_handle_at(wx, wy)
            if handle is not None:
                return self._handle_cursor_key(handle)
            # Move affordance only where a press would actually arm a group
            # move - the same routing helper the press paths use, so the
            # cursor can never disagree with the gesture. The whole oriented
            # frame is a drag surface while it is visible.
            target = self._group_move_target(wx, wy)
            if target is not None:
                return "SizeAllCursor"
            return None
        except Exception:
            return None

    def _apply_hover_cursor(self, key) -> None:
        """Apply a cursor key, skipping a no-op set (no flicker).

        The applied *key* is remembered rather than the ``QCursor`` object:
        comparing keys is cheap and exact, and never re-sets an unchanged
        cursor on every mouse move.
        """
        if key == self._hover_cursor_key:
            return
        self._hover_cursor_key = key
        self._last_pointer_cursor_key = key
        try:
            if key is None:
                self.unsetCursor()
                return
            cursor = cursor_for_key(key)
            if cursor is None:
                self.unsetCursor()
                self._hover_cursor_key = None
                self._last_pointer_cursor_key = None
            else:
                self.setCursor(cursor)
        except Exception:
            self._hover_cursor_key = None
            self._last_pointer_cursor_key = None

    def _update_selection_cursor(self, wx: float, wy: float,
                                 dragging: bool = False) -> None:
        """Set the hover/drag cursor for the selection controls.

        When no selection control owns the pointer the active *tool's*
        cursor is applied instead — the same precedence the press routing
        uses.
        """
        key = self._selection_cursor_key(wx, wy, dragging=dragging)
        if key is None:
            self._apply_tool_cursor()
        else:
            self._apply_hover_cursor(key)

    # -- Tool cursors -------------------------------------------------------

    def _pan_dragging(self) -> bool:
        """True while a Pan gesture has actually crossed the drag threshold."""
        try:
            return bool(self._pan_press_point is not None
                        and self._pan_dragged)
        except Exception:
            return False

    def _cursor_dpr(self) -> float:
        """Device pixel ratio the cursor images should be rendered for."""
        try:
            dpr = float(self.devicePixelRatioF() or 1.0)
        except Exception:
            dpr = 1.0
        if not math.isfinite(dpr) or dpr <= 0.0:
            dpr = 1.0
        return max(1.0, min(3.0, dpr))

    def _apply_tool_cursor(self) -> None:
        """Apply the active tool's cursor as the pointer's tool default.

        Uses the same keyed contract as the selection cursors:
        ``_hover_cursor_key`` holds the rendered spec, so an unchanged
        cursor is never re-set. The images come from ``app.cursors``'
        bounded cache, so this is refresh-on-change only — never a rebuild
        per mouse move.
        """
        try:
            spec = tool_cursor_spec(
                self._interaction.active_tool, self._ui_zoom,
                pan_dragging=self._pan_dragging(), dpr=self._cursor_dpr())
        except Exception:
            spec = None
        key = ("tool", spec)
        if key == self._hover_cursor_key:
            return
        self._hover_cursor_key = key
        self._last_pointer_cursor_key = key
        try:
            cursor = cursor_for_spec(spec)
            if cursor is None:
                self.unsetCursor()
                self._hover_cursor_key = None
                self._last_pointer_cursor_key = None
            else:
                self.setCursor(cursor)
        except Exception:
            self._hover_cursor_key = None
            self._last_pointer_cursor_key = None

    def _refresh_tool_cursor(self) -> None:
        """Re-derive the tool cursor after cursor-affecting state changed.

        Only when the tool owns the pointer (nothing applied, or a tool
        spec): a selection cursor in force (grip/rotate/size-all) or a
        chrome (``ui``) cursor while the pointer sits over UI must
        survive an unrelated zoom or property change. The check reads the last key the
        helpers actually *requested* (``_last_pointer_cursor_key``),
        which survives the stylus stream's per-event invalidation of
        ``_hover_cursor_key`` — otherwise a refresh between two stylus
        events could momentarily override an in-force selection cursor.
        """
        key = self._last_pointer_cursor_key
        if key is None or (isinstance(key, tuple) and key[:1] == ("tool",)):
            self._apply_tool_cursor()

    def _reset_hover_cursor(self) -> None:
        """Drop any selection cursor (tool switch, scroll, selection change,
        gesture end) so no stale resize/move/rotate cursor can survive a
        change of context; the tool's own cursor takes its place at once —
        no mouse move needed to see a new tool's cursor."""
        self._apply_tool_cursor()

    def _reapply_pointer_cursor(self, wx, wy) -> None:
        """Force the pointer cursor for ``(wx, wy)`` back onto the canvas.

        The keyed contract deliberately suppresses unchanged re-sets, which
        is exactly wrong when the *displayed* cursor was lost (the pointer
        left the application and the platform dropped the applied custom
        cursor): the remembered key still matches, so an ordinary refresh
        would be a no-op. Clearing the key first makes the next derivation
        re-apply the truthful cursor for the current tool and the current
        selection interaction state (grip, size-all, or tool).
        """
        self._pointer_cursor_stale = False
        self._hover_cursor_key = None
        try:
            self._update_selection_cursor(
                float(wx), float(wy),
                dragging=self._move_gesture is not None)
        except Exception:
            self._apply_tool_cursor()

    def _derive_pointer_cursor(self, wx: float, wy: float,
                               dragging: bool = False) -> None:
        """Derive the canvas pointer cursor for a live pointer position.

        One derivation shared by the mouse and stylus hover paths, so both
        obey identical precedence: while hovering the window's resize grip
        (and not dragging) the viewer's own cursor is dropped so the
        window-level cursor shows; everywhere else
        ``_update_selection_cursor`` resolves selection grips / frame drag
        cursors first and falls back to the active tool's own cursor. The
        keyed contract inside those helpers means an unchanged cursor is
        never re-set, and chrome widgets keep their own explicit cursors
        because this only ever runs while the pointer is over the canvas.
        """
        try:
            if self._pointer_cursor_stale:
                self._pointer_cursor_stale = False
                self._hover_cursor_key = None
            if (not dragging
                    and self._window_resize_edges(
                        QPoint(int(wx), int(wy))) is not None):
                self._apply_hover_cursor(None)
            else:
                self._update_selection_cursor(
                    float(wx), float(wy), dragging=bool(dragging))
        except Exception:
            self._apply_tool_cursor()

    def _chrome_cursor(self, target):
        """Effective cursor of the chrome widget under a stylus hover.

        Walks to the nearest widget that actually owns a cursor
        (``WA_SetCursor``), exactly like Qt's own resolution, so buttons
        keep ``PointingHand`` and text entries keep their I-beam. Falls
        back to the ordinary arrow when nothing owns one.
        """
        try:
            from PySide6.QtGui import QCursor

            widget = target
            while widget is not None:
                if widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor):
                    return QCursor(widget.cursor())
                widget = widget.parentWidget()
            return QCursor(Qt.CursorShape.ArrowCursor)
        except Exception:
            try:
                from PySide6.QtGui import QCursor

                return QCursor(Qt.CursorShape.ArrowCursor)
            except Exception:
                return None

    def _apply_chrome_cursor(self, target) -> None:
        """Show the hovered chrome widget's own cursor while a pen is over it.

        Qt synthesises Enter/Leave (and therefore the widget-under-pointer
        cursor) only for *mouse* input. Inkfall accepts tablet events, so a
        pen hovering the shelf, undo capsule, popup or view controls keeps
        displaying the viewer's canvas tool cursor even though the widget
        under it owns a different one. Applying that widget's effective
        cursor to the viewer forces the platform update; the ``("ui", …)``
        key differs from every tool key, so the first canvas move after
        the pen leaves chrome re-applies the tool cursor. Child widgets
        are never written to: only read.
        """
        cursor = self._chrome_cursor(target)
        if cursor is None:
            return
        try:
            pixmap = cursor.pixmap()
            key = (("ui", "pixmap", int(pixmap.cacheKey()))
                   if not pixmap.isNull()
                   else ("ui", cursor.shape().name))
        except Exception:
            key = ("ui", "arrow")
        if key == self._hover_cursor_key:
            return
        self._hover_cursor_key = key
        self._last_pointer_cursor_key = key
        try:
            self.setCursor(cursor)
        except Exception:
            self._hover_cursor_key = None
            self._last_pointer_cursor_key = None

    def _restore_canvas_cursor_under_pointer(self) -> None:
        """Re-apply the canvas cursor if the pointer is over the viewer.

        Used on window activation and after a child popup closes: those
        transitions can hand the pointer back to the canvas without
        crossing a widget boundary (and therefore without an ``Enter``).
        Non-canvas chrome keeps its own explicit cursors, so this can
        never paint a drawing cursor over UI.
        """
        try:
            from PySide6.QtGui import QCursor

            pos = self.mapFromGlobal(QCursor.pos())
            if self.rect().contains(pos):
                self._reapply_pointer_cursor(pos.x(), pos.y())
        except Exception:
            pass

    def enterEvent(self, event) -> None:  # noqa: N802
        """Restore the canvas cursor when the pointer enters the canvas.

        M7.1.4.2: a custom cursor is lost when the pointer leaves the
        application window; without this event-driven re-derivation the
        next return shows the platform default (or a stale state) until
        something else happens to set a cursor. Forced re-application is
        bounded to the actual entry event — no timers, no polling.
        """
        super().enterEvent(event)
        try:
            pos = event.position()
            self._reapply_pointer_cursor(pos.x(), pos.y())
        except Exception:
            self._apply_tool_cursor()

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Mark the canvas cursor stale when the pointer leaves it.

        The platform drops the applied custom cursor on some window
        transitions (pointer to another window, window deactivation) and
        the remembered key would otherwise suppress the re-apply. The
        next canvas pointer event forces exactly one re-set.
        """
        self._pointer_cursor_stale = True
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        """Re-derive the canvas cursor when the window becomes active.

        Covers Alt-Tab/window-focus returns where no ``Enter`` is
        delivered because the pointer never left the canvas. One-shot,
        event-driven, and restricted to the canvas area so ordinary UI
        keeps the normal Qt cursor.
        """
        super().changeEvent(event)
        try:
            from PySide6.QtCore import QEvent

            window_activate = getattr(QEvent.Type, "WindowActivate", None)
            if (event.type() != QEvent.Type.ActivationChange
                    and (window_activate is None
                         or event.type() != window_activate)):
                return
            if not self.isActiveWindow():
                self._pointer_cursor_stale = True
                return
            self._restore_canvas_cursor_under_pointer()
        except Exception:
            pass

    def _selection_frame_screen_box(self):
        """Integer widget-space ``QRect`` of the frame (scoped repaints).

        Grown to include the grips (see :meth:`_selection_frame_paint_rect`)
        so a scoped repaint always invalidates the full painted frame
        footprint — the fix for the rotation-drag ghost.
        """
        rect = self._selection_frame_paint_rect()
        if rect is None:
            return None
        try:
            left, top, width, height = rect
            pad = 3.0  # slack for the antialiased dash edge
            return QRect(int(math.floor(left - pad)),
                         int(math.floor(top - pad)),
                         int(math.ceil(width + 2.0 * pad)) or 1,
                         int(math.ceil(height + 2.0 * pad)) or 1)
        except Exception:
            return None

    def _selection_handle_points(self):
        """Widget-space grip centres for the displayed shared frame.

        Returns ``{name: (x, y)}`` for the eight resize grips (corners +
        edge midpoints, clockwise from the top-left) plus ``"rotate"``,
        or ``None`` when nothing is framed. Grips follow the *oriented*
        frame — a rotated selection's grips sit on its rotated corners and
        edges, and the rotation grip sits a fixed screen distance outward
        from the midpoint of the frame's oriented top edge (never above
        the axis-aligned bounds of the rotated contents).
        """
        corners = self._selection_frame_corners()
        if corners is None:
            return None
        try:
            nw, ne, se, sw = ((float(c[0]), float(c[1])) for c in corners)

            def mid(a, b):
                return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

            top = mid(nw, ne)
            centre = mid(nw, se)
            vx = top[0] - centre[0]
            vy = top[1] - centre[1]
            norm = math.hypot(vx, vy)
            if norm <= 0.0:
                vx, vy, norm = 0.0, -1.0, 1.0
            rotate = (top[0] + vx / norm * _ROTATE_HANDLE_OFFSET_PX,
                      top[1] + vy / norm * _ROTATE_HANDLE_OFFSET_PX)
            return {
                "nw": nw,
                "n": top,
                "ne": ne,
                "e": mid(ne, se),
                "se": se,
                "s": mid(se, sw),
                "sw": sw,
                "w": mid(sw, nw),
                "rotate": rotate,
            }
        except Exception:
            return None

    def _transform_handle_at(self, wx: float, wy: float):
        """Name of the resize/rotation grip under a widget point, or None.

        The rotation grip wins whenever its (larger) radius contains the
        point; otherwise the *nearest* resize grip within
        ``_HANDLE_HIT_PX`` wins, ties broken by the stable clockwise
        order. Nearest-wins makes overlapping radii unambiguous and the
        result deterministic. Pure geometry: reads the displayed frame,
        never the store.
        """
        points = self._selection_handle_points()
        if not points:
            return None
        try:
            wx = float(wx)
            wy = float(wy)
            rx, ry = points["rotate"]
            if math.hypot(wx - rx, wy - ry) <= _ROTATE_HANDLE_HIT_PX:
                return "rotate"
            best = None
            for rank, name in enumerate(_HANDLE_NAMES):
                hx, hy = points[name]
                distance = math.hypot(wx - hx, wy - hy)
                if distance > _HANDLE_HIT_PX:
                    continue
                key = (distance, rank)
                if best is None or key < best[0]:
                    best = (key, name)
            return best[1] if best is not None else None
        except Exception:
            return None

    def _resize_anchor(self, handle: str, corners):
        """Fixed shared-space anchor of one resize grip (opposite edge).

        ``corners`` is the oriented frame ``(nw, ne, se, sw)`` in shared
        space. A corner grip pins the opposite corner; an edge grip pins
        the opposite edge's *midpoint* (the free axis keeps ``scale == 1``
        so an edge drag can never change the other axis). Returns
        ``(ax, ay)`` shared page-pt, or ``None`` for an unknown grip.
        """
        if corners is None:
            return None
        try:
            nw, ne, se, sw = corners

            def mid(a, b):
                return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

            return {
                "se": nw,
                "nw": se,
                "ne": sw,
                "sw": ne,
                "e": mid(nw, sw),
                "w": mid(ne, se),
                "n": mid(sw, se),
                "s": mid(nw, ne),
            }.get(handle)
        except Exception:
            return None

    def _selection_pulse(self) -> float:
        """Bounded opacity factor (``[_SELECT_PULSE_MIN, MAX]``) from phase.

        A slow sine so the frame "breathes" rather than flashes; the floor
        guarantees it never disappears at the dimmest point.
        """
        span = _SELECT_PULSE_MAX - _SELECT_PULSE_MIN
        phase = float(self._selection_phase)
        wave = 0.5 + 0.5 * math.sin(
            2.0 * math.pi * phase / float(_SELECT_PULSE_PERIOD_TICKS))
        return _SELECT_PULSE_MIN + span * wave

    def _selection_frame_pen(self) -> QPen:
        """The animated dashed pen for the shared selection frame.

        The dash offset advances with ``_selection_phase`` ("marching
        ants"); the colour alpha carries the bounded pulse. Always
        visible, never fully transparent.
        """
        pen = QPen(_SELECT_ACCENT)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([_SELECT_DASH_ON, _SELECT_DASH_OFF])
        offset = self._selection_phase * _SELECT_DASH_OFFSET_STEP
        if _SELECT_DASH_CYCLE > 0.0:
            offset = offset % _SELECT_DASH_CYCLE
        pen.setDashOffset(float(offset))
        color = QColor(_SELECT_ACCENT)
        color.setAlphaF(self._selection_pulse())
        pen.setColor(color)
        return pen

    def _should_animate_selection(self) -> bool:
        """True while the shared frame should be animating.

        Requires a visible (existing) selection frame and a visible viewer,
        so the timer never runs for a hidden or idle canvas. The frame is
        animated under every tool, including a context-menu selection made
        while Pen/Pan/Eraser is active.
        """
        try:
            return self._selection_frame_visible() and self.isVisible()
        except Exception:
            return False

    def _sync_selection_animation(self) -> None:
        """Start/stop the frame animation to match the current state.

        Idempotent: safe to call from every selection-changing path and
        from the paint path. Runs only for a non-empty selection on an
        active, visible Selection/Lasso viewer; otherwise stops the timer.
        The phase is *not* reset here (a fresh commit resets it) so a
        pinned phase survives an incidental repaint.

        Deliberately *not* a cursor hook: the paint path calls this too, so
        resetting the hover cursor here would drop it on every repaint.
        Cursor invalidation lives in the actual change/gesture-end paths.
        """
        try:
            if self._should_animate_selection():
                if not self._selection_timer.isActive():
                    self._selection_timer.start()
            else:
                self._selection_timer.stop()
        except Exception:
            pass

    def _stop_selection_animation(self) -> None:
        """Stop the frame animation (document close / teardown / hide)."""
        try:
            self._selection_timer.stop()
        except Exception:
            pass

    def _advance_selection_animation(self) -> None:
        """One animation tick: advance the phase, repaint only the frame."""
        try:
            if not self._should_animate_selection():
                self._selection_timer.stop()
                return
            self._selection_phase = float(self._selection_phase) + 1.0
            box = self._selection_frame_screen_box()
            if box is not None:
                self.update(box)
            else:
                self.update()
        except Exception:
            pass

    def _shape_kind(self):
        try:
            return self._interaction.active_kind
        except Exception:
            return None

    def _shape_press(self, page_id, x: float, y: float, pressure: float,
                     now_ms: int) -> dict:
        """Route a mapped press to the active shape handler (with snap)."""
        snap = self._shape_snap_context(page_id)
        return self._interaction.handle_shape_press(
            page_id, x, y, pressure, now_ms, snap)

    def _shape_move(self, page_id, x: float, y: float, pressure: float,
                    now_ms: int) -> dict:
        """Route a mapped move to the active shape handler (with snap).

        The Connect-Lines snap context is rebuilt on every move so the
        endpoint/intersection resolver runs live and the Polygon
        provisional edge/fill track the resolved position (M6.7.2).
        """
        snap = self._shape_snap_context(page_id)
        return self._interaction.handle_shape_move(
            page_id, x, y, pressure, now_ms, snap)

    def _shape_release(self, page_id, x: float, y: float, pressure: float,
                       now_ms: int) -> dict:
        return self._interaction.handle_shape_release(
            page_id, x, y, pressure, now_ms)

    def _shape_snap_context(self, page_id) -> ShapeSnapContext:
        """Candidate snap geometry for Connect Lines on the active page.

        Gathers endpoints/edges from *committed* shapes on the page (the
        handler never touches the store). The tolerance is a screen
        distance converted to page-local points so snapping is
        zoom-correct. Any failure yields an empty (disabled) context.
        """
        try:
            document = self._document
            if document is None:
                return ShapeSnapContext()
            page_index = self._page_index_for(page_id)
            if page_index is None:
                return ShapeSnapContext()
        except Exception:
            return ShapeSnapContext()
        endpoints: list = []
        segments: list = []
        try:
            for ann in document.annotations.page(page_index):
                if not isinstance(ann, Shape):
                    continue
                kind = getattr(ann, "kind", None)
                pts = tuple(getattr(ann, "points", ()) or ())
                if kind in (ShapeKind.LINE, ShapeKind.ARROW) and len(pts) >= 2:
                    endpoints.extend(pts[:2])
                    segments.append((pts[0], pts[1]))
                elif kind == ShapeKind.POLYGON and len(pts) >= 2:
                    endpoints.extend(pts)
                    for i in range(len(pts) - 1):
                        segments.append((pts[i], pts[i + 1]))
                    segments.append((pts[-1], pts[0]))
                elif kind in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE):
                    r = getattr(ann, "rect", None)
                    if r is None:
                        continue
                    corners = (Point(r.x0, r.y0), Point(r.x1, r.y0),
                               Point(r.x1, r.y1), Point(r.x0, r.y1))
                    endpoints.extend(corners)
                    segments.extend([
                        (corners[0], corners[1]), (corners[1], corners[2]),
                        (corners[2], corners[3]), (corners[3], corners[0])])
        except Exception:
            return ShapeSnapContext()
        tol = 0.0
        try:
            layout = self._pipeline.layout
            bucket = float(layout.raster_scale)
            scale = self._display_scale()
            factor = bucket * scale
            if factor > 0.0:
                tol = _SHAPE_SNAP_PX / factor
        except Exception:
            tol = 0.0
        # M6.7.2: the in-progress Polygon is not committed yet, so its own
        # *confirmed* vertices/edges are offered as transient candidates for
        # later vertices. The provisional edge, the unconfirmed pointer and
        # the fill contour are never included (no self-snapping).
        pending_eps: tuple = ()
        pending_segs: tuple = ()
        try:
            pending_eps, pending_segs = (
                self._interaction.shape_pending_snap_candidates())
        except Exception:
            pending_eps, pending_segs = (), ()
        return ShapeSnapContext(endpoints=tuple(endpoints),
                                segments=tuple(segments), tol_pt=tol,
                                pending_endpoints=tuple(pending_eps),
                                pending_segments=tuple(pending_segs))

    def _commit_shape(self, annotation) -> None:
        """M6.7 shape commit (delegates to ``_commit_annotation``)."""
        if isinstance(annotation, Shape):
            self._commit_annotation(annotation)

    def _shape_widget_rect(self, shape):
        """Widget-space bounding rect of a shape (for live invalidation).

        Inverse of :meth:`_event_to_page_pt` using the same projection
        constants as the paint paths, so preview invalidation and commit
        repaint cover exactly the pixels the shape occupies. ``None``
        when the shape cannot be mapped (off-view / no document).
        """
        try:
            page_index = self._page_index_for(shape.page_id)
            if page_index is None:
                return None
            pipeline = self._pipeline
            if pipeline is None:
                return None
            layout = pipeline.layout
            bucket = float(layout.raster_scale)
            scale = self._display_scale()
            if bucket <= 0.0 or scale <= 0.0:
                return None
            entry = layout.entry(page_index)
            vw_render, _vh = self._render_viewport()
            page_rect = page_screen_rect(
                float(entry.y_offset_px), float(entry.render_width_px),
                float(entry.render_height_px), self._scroll_doc(), vw_render,
                scale, scroll_x_doc=self._scroll_doc_x(),
                content_width_px=float(layout.total_width_px))
            factor = bucket * scale
            r = shape.rect
            x0 = float(page_rect.x) + r.x0 * factor
            y0 = float(page_rect.y) + r.y0 * factor
            x1 = float(page_rect.x) + r.x1 * factor
            y1 = float(page_rect.y) + r.y1 * factor
            try:
                width_px = max(1.0, float(shape.width_mm) * 72.0 / 25.4
                               * self._ui_zoom)
            except Exception:
                width_px = 1.0
            pad = width_px * 4.0 + 14.0
            left = min(x0, x1) - pad
            top = min(y0, y1) - pad
            w = abs(x1 - x0) + 2.0 * pad + 2.0
            h = abs(y1 - y0) + 2.0 * pad + 2.0
            return QRect(int(left), int(top), int(w), int(h))
        except Exception:
            return None

    @staticmethod
    def _pen_clock_ms() -> int:
        """Monotonic fallback timestamp (ms) for pressure-less input."""
        try:
            return int(time.monotonic() * 1000)
        except Exception:
            return 0

    @staticmethod
    def _event_time_ms(event) -> int:
        """Event timestamp (ms) when the platform provides one.

        M5-B.2.0: Qt input events may carry no usable timestamp
        (synthetic/offscreen events report 0); the handler clamps any
        non-monotonic value, so callers forward whatever is available.
        """
        try:
            stamp = int(event.timestamp())
            if stamp > 0:
                return stamp
        except Exception:
            pass
        return PdfViewer._pen_clock_ms()

    def _chrome_surfaces(self) -> tuple:
        """Every viewer-owned widget subtree that owns stylus taps.

        ``tabletEvent`` accepts tablet input for the whole viewer, so Qt
        does not synthesize mouse events for widgets inside it; a tap is
        routed only when its deepest hit widget descends from a surface
        registered here. This is the single source of truth for chrome
        membership: a surface is registered once, for its whole subtree,
        instead of being re-listed (and forgotten) in individual
        handlers.
        """
        shelf = getattr(self, "_shelf", None)
        view_controls = getattr(self, "_view_controls", None)
        navigator = getattr(self, "_page_navigator", None)
        surfaces = (
            shelf,
            getattr(shelf, "capsule", None),
            getattr(shelf, "dots", None),
            # The undo/redo capsule is chrome exactly like the shelf; it
            # was missing from the old inline list, so pen taps on it fell
            # through to the canvas tool routing instead of its buttons.
            getattr(self, "_undo_hud", None),
            getattr(self, "_tool_popup", None),
            getattr(self, "_scrollbar", None),
            getattr(self, "_hscrollbar", None),
            # M7.0: a pen tap on the floating view controls is chrome
            # input, exactly like the shelf or a scrollbar — it must never
            # also draw on the page.
            getattr(view_controls, "trigger", None),
            getattr(view_controls, "panel", None),
            # The page navigator (Pages segment + filmstrip/grid panel +
            # shared capsule body) is chrome on exactly the same terms.
            getattr(navigator, "trigger", None),
            getattr(navigator, "panel", None),
            getattr(navigator, "capsule_body", None),
        )
        return tuple(surface for surface in surfaces if surface is not None)

    def _tablet_chrome_target(self, x: float, y: float):
        """Deepest child under (x, y) owned by toolbar chrome, else None.

        M5-B.2.0.1 F1: Qt's own hit-test (childAt), never guessed
        geometry. M7.1.4.4: membership comes from :meth:`_chrome_surfaces`
        so every registered surface (shelf subtree, undo/redo capsule,
        tool popup, scrollbars, view controls) behaves identically.
        """
        try:
            target = self.childAt(int(x), int(y))
        except Exception:
            return None
        if target is None:
            return None
        try:
            for root in self._chrome_surfaces():
                try:
                    if target is root or root.isAncestorOf(target):
                        return target
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _forward_pen_to_child(self, target, tablet_type, x: float, y: float,
                              mouse_kind=None, modifiers=None) -> bool:
        """Deliver one tablet press/move/release as a mouse event.

        M5-B.2.0.1 F1: press/move/release pairing mirrors mouse grab
        semantics — the whole tap goes to the press target. Returns
        False when the target is gone (caller clears the pairing).
        M5-B.2.0.2: ``mouse_kind`` overrides the tablet-to-mouse mapping
        so a qualifying second tap forwards MouseButtonDblClick instead
        of a second press (Qt never infers dblclick from two presses).
        M7.1.4.4: the tablet event's keyboard modifiers are preserved, so
        Shift/Ctrl semantics (e.g. extending a selection in a text field)
        behave exactly as with the mouse instead of always arriving
        unmodified.
        M7.1.4.2.1: click-to-focus is granted explicitly on a press (see
        below) because a synthetic event sent straight to a widget
        bypasses Qt's window-level focus handling.
        """
        try:
            from PySide6.QtCore import QEvent as _QE
            from PySide6.QtWidgets import QApplication as _QA

            if modifiers is None:
                modifiers = Qt.KeyboardModifier.NoModifier
            glob = self.mapToGlobal(QPoint(int(x), int(y)))
            try:
                local = target.mapFromGlobal(glob)
            except Exception:
                return False
            if mouse_kind is not None:
                kind = mouse_kind
                button = Qt.MouseButton.LeftButton
                buttons = Qt.MouseButton.LeftButton
            elif tablet_type == _QE.Type.TabletPress:
                kind = _QE.Type.MouseButtonPress
                button = Qt.MouseButton.LeftButton
                buttons = Qt.MouseButton.LeftButton
            elif tablet_type == _QE.Type.TabletRelease:
                kind = _QE.Type.MouseButtonRelease
                button = Qt.MouseButton.LeftButton
                buttons = Qt.MouseButton.NoButton
            else:
                kind = _QE.Type.MouseMove
                button = Qt.MouseButton.NoButton
                buttons = Qt.MouseButton.LeftButton
            # M7.1.4.2.1 click-to-focus: Qt grants mouse-click focus in
            # its window-level mouse pipeline, which an event sent
            # directly to the widget bypasses. A stylus tap therefore
            # never focused the text-entry fields (no caret, no keyboard
            # entry, no Enter / editingFinished commit, no selection
            # drag that starts from focus). Mirror the real mouse
            # exactly: a forwarded press — or the forwarded dblclick
            # that replaces a qualifying second press — gives focus to
            # any widget whose focus policy permits click focus
            # (ClickFocus / StrongFocus / WheelFocus); NoFocus chrome
            # (shelf buttons, trigger, panel, capsule) is never touched.
            # Focus precedes the event so the field's focusIn runs first
            # and the event below places the caret at the true
            # field-local coordinates, as a real mouse press does.
            if kind in (_QE.Type.MouseButtonPress,
                        _QE.Type.MouseButtonDblClick):
                try:
                    click_focus = (Qt.FocusPolicy.ClickFocus
                                   | Qt.FocusPolicy.StrongFocus
                                   | Qt.FocusPolicy.WheelFocus)
                    if target.focusPolicy() & click_focus:
                        target.setFocus(Qt.FocusReason.MouseFocusReason)
                except Exception:
                    pass
            _QA.sendEvent(target, QMouseEvent(
                kind, local, glob, button, buttons, modifiers))
            return True
        except Exception:
            return False

    def _event_to_page_pt(self, x: float, y: float):
        """Map widget px to ``(page_id, x_pt, y_pt)`` or None.

        Stored points are page-local rotated PDF points: page-local
        render px divided by the capture raster bucket. Rotation is
        applied exactly once, at layout build (entry geometry already
        encodes the effective rotation); this mapping only inverts the
        ``page_screen_rect`` projection. ``None`` when there is no
        document, the point falls in a page gap, or (M5-B.1.2) outside
        the page horizontally — gutter positions are rejected, never
        silently clamped onto the page edge.
        """
        try:
            pipeline = self._pipeline
            if pipeline is None:
                return None
            layout = pipeline.layout
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return None
            scale = self._display_scale()
            if scale <= 0:
                return None
            vw_render, _vh_render = self._render_viewport()
            scroll_doc = self._scroll_doc()
            scroll_doc_x = self._scroll_doc_x()
            doc_y = float(y) / scale + scroll_doc
            pages = layout.pages_in(doc_y, doc_y + 1.0)
            if not pages:
                return None
            entry = layout.entry(pages[0])
            x_off = DocumentLayout.page_x_origin(
                vw_render, float(layout.total_width_px),
                float(entry.render_width_px))
            width_pt = float(entry.render_width_px) / bucket
            height_pt = float(entry.render_height_px) / bucket
            lx = (float(x) / scale + scroll_doc_x - x_off) / bucket
            if not 0.0 <= lx <= width_pt:
                return None
            ly = (doc_y - float(entry.y_offset_px)) / bucket
            if not 0.0 <= ly <= height_pt:
                return None
            return (entry.page_id, lx, ly)
        except Exception:
            return None

    def _apply_intent(self, intent: dict) -> None:
        """Apply a controller intent (pan/pen/highlighter/eraser)."""
        if not isinstance(intent, dict):
            return
        if ("eraser_update" in intent or "eraser_done" in intent
                or "eraser_cancelled" in intent or "eraser_begin" in intent):
            self._apply_eraser_intent(intent)
            return
        if ("shape_update" in intent or "shape_done" in intent
                or "shape_cancelled" in intent):
            self._apply_shape_intent(intent)
            return
        hl = intent.get("highlight_update")
        if hl is not None:
            # Transient preview only — nothing is committed until up().
            try:
                self._live_highlighter = hl
            except Exception:
                pass
            # M7.1: arm/restart the hold timer (and cancel a live morph if
            # the pointer is genuinely moving again) before repainting.
            try:
                self._note_shape_movement(hl)
            except Exception:
                pass
            try:
                cur = self._live_highlight_rect(hl)
                if cur is None:
                    raise ValueError("no highlight rect")
                prev = self._live_highlight_prev_rect
                rect = cur.united(prev) if prev is not None else cur
                self._live_highlight_prev_rect = QRect(cur)
                if self._live_highlight_full_rect is None:
                    self._live_highlight_full_rect = QRect(rect)
                else:
                    self._live_highlight_full_rect = (
                        self._live_highlight_full_rect.united(rect))
                self.update(rect)
            except Exception:
                try:
                    self._live_highlight_prev_rect = None
                    self.update()
                except Exception:
                    pass
            return
        done = intent.get("highlight_done")
        if done is not None:
            # Clear the preview, commit ONE canonical annotation to the
            # owning document's store via the M6.2 history, then repaint
            # the committed bounds (union with the last live rect).
            try:
                live = self._live_highlighter
                self._live_highlighter = None
                prev = self._live_highlight_prev_rect
                self._live_highlight_prev_rect = None
                self._live_highlight_full_rect = None
                self._tablet_stroke_open = False
                self._mouse_stroke_open = False
                # M7.1: hold-to-shape commits the recognized contour; an
                # enabled "Ink to Line" commits the straightened segment;
                # otherwise the freehand annotation commits unchanged.
                final = self._final_highlighter_annotation(done, live)
                morph_rect = self._clear_shape_conversion(repaint=False)
                self._commit_highlighter(final)
                rect = self._stroke_bounds_rect(final)
                if prev is not None:
                    prev_q = QRect(prev)
                    rect = prev_q if rect is None else rect.united(prev_q)
                if morph_rect is not None:
                    morph_q = QRect(morph_rect)
                    rect = morph_q if rect is None else rect.united(morph_q)
                if rect is None:
                    raise ValueError("no completion rect")
                self.update(rect)
            except Exception:
                try:
                    self._live_highlight_prev_rect = None
                    self._live_highlight_full_rect = None
                    self.update()
                except Exception:
                    pass
            return
        if intent.get("highlight_cancelled"):
            try:
                self._live_highlighter = None
            except Exception:
                pass
            self._tablet_stroke_open = False
            self._mouse_stroke_open = False
            # M7.1: a cancelled gesture takes any pending recognition with
            # it (no conversion, no morph callback may outlive it), and the
            # morph's own region is repainted so no fitted frame survives.
            self._clear_shape_conversion()
            try:
                full = self._live_highlight_full_rect
                self._live_highlight_prev_rect = None
                self._live_highlight_full_rect = None
                if full is None:
                    raise ValueError("no highlight rect")
                self.update(QRect(full))
            except Exception:
                try:
                    self._live_highlight_prev_rect = None
                    self._live_highlight_full_rect = None
                    self.update()
                except Exception:
                    pass
            return
        stroke = intent.get("stroke_update")
        if stroke is not None:
            try:
                self._live_stroke = stroke
            except Exception:
                pass
            # B.3.5: the live view keeps stable identity across moves,
            # so the smoothed-centerline memo is invalidated per update
            # (coalesced paints from one update still share one pull).
            self._live_smooth_memo = None
            # M7.1: arm/restart the hold timer (cancelling a live morph if
            # the pointer is genuinely moving again) before repainting.
            try:
                self._note_shape_movement(stroke)
            except Exception:
                pass
            # B.2.4: rect-scoped live update. Union with the previous
            # rect so vacated pixels repaint too; any failure falls
            # back to a full update (never lose pixels).
            try:
                cur = self._live_update_rect(stroke)
                if cur is None:
                    raise ValueError("no live rect")
                # B.3.9: restyle check first. Changed pixels from the
                # new sample lie within the current tail bound (see
                # _live_tail_rect), and pixels the previous tail
                # vacated are covered by the previous rect — but a
                # mid-stroke color/width/opacity change alters
                # already-painted pixels everywhere, so it repaints
                # fully instead (rare path, always correct).
                try:
                    w = float(getattr(stroke, "width_mm", 0.5))
                    if w != w or w in (float("inf"), float("-inf")):
                        w = None
                except Exception:
                    w = None
                try:
                    o = max(0.0, min(1.0, float(
                        getattr(stroke, "opacity", 1.0))))
                except Exception:
                    o = 1.0
                try:
                    c = str(getattr(stroke, "color", "#000000"))
                except Exception:
                    c = "#000000"
                meta_now = (c, w, o)
                meta = self._live_meta
                if (meta is not None and meta[0] is stroke
                        and meta[1] != meta_now):
                    self._live_meta = (stroke, meta_now)
                    self._live_prev_rect = None
                    self.update()
                    return
                if meta is None or meta[0] is not stroke:
                    self._live_meta = (stroke, meta_now)
                prev = self._live_prev_rect
                if prev is not None:
                    rect = cur.united(prev)
                else:
                    rect = cur
                # B.3.9: store the CURRENT rect only — never the union.
                # Storing the union here would reintroduce the unbounded
                # accumulation this milestone removes.
                self._live_prev_rect = QRect(cur)
                # Cancel-erase scope: running union of every update
                # rect this stroke (identical pixels to the old
                # accumulated _live_prev_rect); moves never read it.
                if self._live_full_rect is None:
                    self._live_full_rect = QRect(rect)
                else:
                    self._live_full_rect = self._live_full_rect.united(
                        rect)
                self.update(rect)
            except Exception:
                try:
                    self._live_prev_rect = None
                    self._live_meta = None
                    self.update()
                except Exception:
                    pass
            return
        done = intent.get("stroke_done")
        if done is not None:
            try:
                live = self._live_stroke
            except Exception:
                live = None
            try:
                self._live_stroke = None
            except Exception:
                pass
            # M7.1: a completed hold-to-shape morph commits its recognized
            # ``Shape`` (built from the pen's own latched properties)
            # through the same history path as any other annotation; every
            # other release commits the freehand stroke unchanged.
            final, consumed = self._final_pen_annotation(done, live)
            morph_rect = self._clear_shape_conversion(repaint=False)
            # M6.4: commit the completed pen stroke to the owning
            # document's store through the shared per-document history
            # (the command's initial redo() inserts it; no viewer list
            # keeps a second copy). M7.1.4.1: a chain-merged polygon
            # replaces its consumed sides in one grouped undo step.
            self._commit_pen_final(final, consumed)
            # M5-B.2.0.1 F2: stroke closed by either stream — release
            # both stream claims so the next press starts clean.
            self._tablet_stroke_open = False
            self._mouse_stroke_open = False
            # B.2.6: scoped completion invalidation (union of the
            # completed bounds and the last live rect — pixel-identical
            # to a full update via the same paint path); anything
            # uncomputable keeps the previous full update.
            try:
                prev = self._live_prev_rect
                self._live_prev_rect = None
                self._live_full_rect = None
                self._live_meta = None
                self._live_extents_memo = None
                self._live_smooth_memo = None
                if final is done:
                    rect = self._completion_rect(done, prev)
                else:
                    # Recognized shape: bounds come from the shape's own
                    # extents (rect- or point-based), unioned with the
                    # vacated live geometry.
                    rect = self._shape_commit_rect(final)
                    if prev is not None:
                        prev_q = QRect(prev)
                        rect = prev_q if rect is None else rect.united(prev_q)
                if morph_rect is not None:
                    morph_q = QRect(morph_rect)
                    rect = morph_q if rect is None else rect.united(morph_q)
                if rect is None:
                    raise ValueError("no completion rect")
                self.update(rect)
            except Exception:
                try:
                    self._live_prev_rect = None
                    self._live_full_rect = None
                    self._live_meta = None
                    self.update()
                except Exception:
                    pass
            return
        if intent.get("stroke_cancelled"):
            try:
                self._live_stroke = None
            except Exception:
                pass
            self._tablet_stroke_open = False
            self._mouse_stroke_open = False
            # M7.1: a cancelled gesture takes any pending recognition with
            # it (no conversion, no morph callback may outlive it), and the
            # morph's own region is repainted so no fitted frame survives.
            self._clear_shape_conversion()
            # B.3.9: cancellation erases the live stroke, so repaint
            # the running full-stroke union (identical pixels to the
            # accumulated rect this slot replaces); without one (or on
            # any failure) keep the previous full update.
            try:
                full = self._live_full_rect
                self._live_prev_rect = None
                self._live_full_rect = None
                self._live_meta = None
                self._live_extents_memo = None
                self._live_smooth_memo = None
                if full is None:
                    raise ValueError("no live rect")
                self.update(QRect(full))
            except Exception:
                try:
                    self._live_prev_rect = None
                    self._live_full_rect = None
                    self._live_meta = None
                    self.update()
                except Exception:
                    pass
            return
        pan = intent.get("pan_by")
        if pan is None:
            return
        try:
            dx, dy = pan
            bar = self._scrollbar
            bar.setValue(max(bar.minimum(),
                             min(bar.maximum(), bar.value() + int(dy))))
            # M5-B.1.2: horizontal pan shares the grab semantics —
            # dragging right follows the document right.
            hbar = self._hscrollbar
            hbar.setValue(max(hbar.minimum(),
                              min(hbar.maximum(), hbar.value() + int(dx))))
        except Exception:
            pass

    # -- M6.3 highlighter commit + geometry ----------------------------------

    def _page_index_for(self, page_id):
        """Zero-based page index for a page identity, or None.

        The layout is the single source of the page_id -> index mapping
        (entries are built in page order), so this never duplicates or
        invents a page association.
        """
        try:
            key = getattr(page_id, "value", page_id)
            layout = self._pipeline.layout
            for index in range(layout.page_count):
                if getattr(getattr(layout.entry(index), "page_id", None),
                           "value", None) == key:
                    return index
        except Exception:
            return None
        return None

    @staticmethod
    def _annotation_has_geometry(annotation) -> bool:
        """True when an annotation carries committable geometry (M6.7).

        Freehand annotations carry ``points``; rect-based shapes
        (Rectangle/Ellipse) carry a positive-extent ``rect`` instead.
        """
        if getattr(annotation, "points", ()):
            return True
        rect = getattr(annotation, "rect", None)
        if rect is None:
            return False
        try:
            return float(rect.width) > 0.0 and float(rect.height) > 0.0
        except Exception:
            return False

    def _commit_annotation(self, annotation) -> None:
        """Push exactly one Add command for a completed annotation.

        M6.4: the shared commit path for the pen and highlighter tools.
        The owning document's store is authoritative: nothing is
        inserted here directly (the command's initial redo() inserts it)
        and no viewer list receives a copy. Preconditions are validated
        before the push; failures leave store/history untouched.
        """
        if self._document_is_readonly():
            self._refuse_readonly("add annotations")
            return
        try:
            if not isinstance(annotation, AnnotationBase):
                return
            if not self._annotation_has_geometry(annotation):
                return
            history = self._history_for(self._document, create=True)
            if history is None:
                return
            page_index = self._page_index_for(annotation.page_id)
            if page_index is None:
                return
            history.push_add(annotation, page_index)
            # First production history consumer: reflect the new
            # undo/redo availability at once (M6.2's shell only synced
            # on open/close/undo/redo).
            self._sync_undo_hud()
        except Exception:
            return

    def _commit_highlighter(self, annotation) -> None:
        """M6.3 highlighter commit (delegates to ``_commit_annotation``)."""
        if isinstance(annotation, Highlighter):
            self._commit_annotation(annotation)

    # -- M6.5 stroke-level eraser --------------------------------------------

    def _finalize_eraser(self) -> None:
        """Close the open eraser undo group, if any (idempotent).

        Never leaves a half-open group: called on gesture end, cancel,
        tool switch, document close, and before undo/redo. The group is
        only opened when a deletion actually happens, so a gesture that
        hits nothing leaves no history entry at all.
        """
        history = self._eraser_history
        self._eraser_history = None
        # M6.6: the gesture is over, so the next gesture re-reads the
        # active tool's mode.
        self._eraser_mode_latch = None
        if history is None:
            return
        try:
            history.end_group()
        except Exception:
            pass
        # M6.5 hotfix: the HUD was last synced mid-gesture, while the Qt
        # macro was still being composed (``QUndoStack.canUndo()`` reports
        # False then), so after the gesture ended Undo/Redo would have
        # stayed disabled. Re-sync now that the group is closed. The HUD
        # reads the stack's real state, so if the macro could not be
        # closed (``end_group`` returned False / ``group_open`` still
        # True) the controls correctly remain disabled instead of falsely
        # claiming a usable history.
        # M6.8.1: a completed erase may have removed the selected
        # annotation; drop a now-dangling selection.
        self._revalidate_selection()
        self._sync_undo_hud()

    def _eraser_radius_pt(self) -> float:
        """Effective eraser radius in page-local pt.

        The eraser's existing ``width_mm`` size property is its on-page
        diameter — the same convention every other tool uses for
        ``width_mm`` — so the effective radius is half of it, converted
        from mm to PDF points (72 / 25.4). A missing/invalid size falls
        back to the stored Eraser default (2.0 mm).
        """
        width_mm = 2.0
        try:
            tool = self._interaction.active_tool
            if tool is not None:
                width_mm = float(tool.width_mm)
        except Exception:
            width_mm = 2.0
        if (not math.isfinite(width_mm) or width_mm <= 0.0):
            width_mm = 2.0
        return width_mm * 72.0 / 25.4 / 2.0

    def _apply_eraser_intent(self, intent: dict) -> None:
        """Apply one eraser intent (segment hit-test / done / cancel)."""
        try:
            if intent.get("eraser_begin"):
                # M6.6: latch the active tool's mode for this gesture.
                self._eraser_mode_latch = self._eraser_mode()
            segment = intent.get("eraser_update")
            deleted = False
            if segment is not None:
                deleted = self._erase_segment(segment)
            if intent.get("eraser_done") or intent.get("eraser_cancelled"):
                self._finalize_eraser()
                self._eraser_mouse_open = False
                self._eraser_tablet_open = False
            if deleted:
                # Immediate feedback: the erased stroke must disappear as
                # soon as the eraser reaches it, not when the drag ends.
                self.update()
        except Exception:
            pass

    def _apply_shape_intent(self, intent: dict) -> None:
        """Apply one shape intent (M6.7 preview / done / cancel).

        The preview is a transient provisional ``Shape`` painted through
        the exact same painter as a committed one, so what the user sees
        is what gets committed. Nothing enters the store until
        ``shape_done``; a cancelled or degenerate gesture leaves no
        history entry.
        """
        done = intent.get("shape_done")
        if done is not None:
            try:
                self._live_shape = None
                self._live_shape_preview = None
                self._live_shape_snap = None
                prev = self._live_shape_prev_rect
                self._live_shape_prev_rect = None
                self._live_shape_full_rect = None
                self._shape_mouse_open = False
                self._shape_tablet_open = False
                self._commit_shape(done)
                rect = self._shape_widget_rect(done)
                if prev is not None:
                    rect = QRect(prev) if rect is None else rect.united(
                        QRect(prev))
                if rect is None:
                    self.update()
                else:
                    self.update(rect)
            except Exception:
                try:
                    self._live_shape_prev_rect = None
                    self._live_shape_full_rect = None
                    self.update()
                except Exception:
                    pass
            return
        if intent.get("shape_cancelled"):
            try:
                self._live_shape = None
                self._live_shape_preview = None
                self._live_shape_snap = None
            except Exception:
                pass
            self._shape_mouse_open = False
            self._shape_tablet_open = False
            try:
                full = self._live_shape_full_rect
                self._live_shape_prev_rect = None
                self._live_shape_full_rect = None
                if full is None:
                    raise ValueError("no shape rect")
                self.update(QRect(full))
            except Exception:
                try:
                    self.update()
                except Exception:
                    pass
            return
        shape = intent.get("shape_update")
        if shape is None:
            return
        try:
            self._live_shape = shape
            # M6.7.2: Polygon carries a separate transient fill-contour +
            # confirmed-vertex-marker payload (never stored, never stroked).
            self._live_shape_preview = intent.get("shape_preview")
            # Transient Connect-Lines snap feedback for the live preview
            # ring (Polygon, Line, Arrow). ``(x, y, kind)`` or None.
            self._live_shape_snap = intent.get("shape_snap")
        except Exception:
            return
        try:
            cur = self._shape_widget_rect(shape)
            if cur is None:
                raise ValueError("no shape rect")
            if (self._live_shape_preview is not None
                    or self._live_shape_snap is not None):
                # M6.7.2: the snap ring sits exactly on the resolved endpoint
                # (a corner of the preview bbox), so inflate the bounded
                # invalidation by the ring radius so the indicator is fully
                # repainted as it moves and fully erased when it clears.
                # Still bounded to the live shape + one ring.
                m = int(math.ceil(_SNAP_RING_PX)) + 3
                cur = cur.adjusted(-m, -m, m, m)
            prev = self._live_shape_prev_rect
            rect = cur.united(prev) if prev is not None else cur
            self._live_shape_prev_rect = QRect(cur)
            if self._live_shape_full_rect is None:
                self._live_shape_full_rect = QRect(rect)
            else:
                self._live_shape_full_rect = (
                    self._live_shape_full_rect.united(rect))
            self.update(rect)
        except Exception:
            try:
                self._live_shape_prev_rect = None
                self.update()
            except Exception:
                pass

    def _eraser_mode(self) -> str:
        """The active Eraser tool's mode (defaults to Whole Stroke)."""
        try:
            tool = self._interaction.active_tool
            if tool is not None:
                return normalize_eraser_mode(
                    getattr(tool, "eraser_mode", None))
        except Exception:
            pass
        return DEFAULT_ERASER_MODE

    def _eraser_mode_locked(self) -> str:
        """The mode for the current gesture (latched at gesture start)."""
        latched = getattr(self, "_eraser_mode_latch", None)
        if latched is not None:
            return latched
        return self._eraser_mode()

    def _erase_segment(self, segment) -> bool:
        """Apply one page-local eraser segment (Whole or Partial mode).

        Returns True when at least one annotation changed. Whole Stroke
        removes each touched annotation through the existing grouped
        delete path. Partial Stroke replaces each touched annotation with
        its surviving fragments through the grouped replace path (a full
        removal falls back to the delete path). Both share one gesture
        undo group, so one continuous eraser drag is one undo step. The
        store is the discovery source, so an already-erased annotation can
        never be targeted twice in one gesture.
        """
        document = self._document
        if document is None:
            return False
        try:
            page_index = self._page_index_for(segment.page_id)
        except Exception:
            page_index = None
        if page_index is None:
            return False
        try:
            annotations = list(document.annotations.page(page_index))
        except Exception:
            return False
        radius_pt = self._eraser_radius_pt()
        mode = self._eraser_mode_locked()
        partial = mode == ERASER_MODE_PARTIAL
        actions = []  # (annotation, None=delete | "delete" | [fragments])
        for annotation in annotations:
            try:
                if isinstance(annotation, Shape):
                    # M6.7.2: BOTH eraser modes erase a touched shape in
                    # FULL — Partial Stroke stays partial only for Pen
                    # and Highlighter. Shapes use the same grouped delete
                    # path as a whole-stroke stroke erase.
                    if not self._shape_bbox_may_hit(
                            annotation, segment.x0, segment.y0,
                            segment.x1, segment.y1, radius_pt):
                        continue
                    if self._shape_hit(annotation, segment, radius_pt):
                        actions.append((annotation, None))
                    continue
                if not isinstance(annotation, (InkStroke, Highlighter)):
                    continue
                if not self._eraser_bbox_may_hit(
                        annotation, segment.x0, segment.y0,
                        segment.x1, segment.y1, radius_pt):
                    continue
                if partial:
                    outcome = self._partial_erase_annotation(
                        annotation, segment, radius_pt)
                    if outcome is None:
                        continue
                    actions.append((annotation, outcome))
                elif self._eraser_hit(
                        annotation, segment.x0, segment.y0,
                        segment.x1, segment.y1, radius_pt):
                    actions.append((annotation, None))
            except Exception:
                continue
        if not actions:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        if self._eraser_history is not None and (
                self._eraser_history is not history):
            self._finalize_eraser()
        deleted = False
        for annotation, outcome in actions:
            try:
                if not history.store.contains(annotation.id):
                    continue
                if outcome is None or outcome == "delete":
                    history.push_delete_grouped(annotation.id)
                else:
                    history.push_replace_grouped(
                        annotation.id, outcome)
                self._eraser_history = history
                deleted = True
            except Exception:
                continue
        if deleted:
            self._sync_undo_hud()
        return deleted

    def _eraser_raw_geometry(self, annotation):
        """Raw centerline + per-sample half-widths + pressures, or None.

        Unlike :meth:`_eraser_stroke_geometry` (which returns the smoothed
        centerline the renderer draws, for hit-testing), this returns the
        *stored* samples so a partial erase can cut the canonical
        geometry while preserving original point locations, order, and
        pressure data. Half-widths match the renderer's local width so
        the clip respects pressure-variable strokes.
        """
        points = getattr(annotation, "points", ()) or ()
        raw = [(float(p.x), float(p.y)) for p in points]
        if not raw:
            return None
        try:
            base_pt = abs(float(annotation.width_mm)) * 72.0 / 25.4
        except (TypeError, ValueError):
            return None
        if not math.isfinite(base_pt) or base_pt <= 0.0:
            return None
        half = base_pt / 2.0
        pressures = getattr(annotation, "pressures", ())
        if isinstance(annotation, InkStroke):
            pressure_on = (bool(getattr(annotation, "pressure_enabled", False))
                           and len(pressures) == len(raw))
            profile = getattr(annotation, "pressure_profile", "normal")
        elif isinstance(annotation, Highlighter):
            pressure_on = bool(pressures) and len(pressures) == len(raw)
            profile = "normal"
        else:
            pressure_on = False
            profile = "normal"
        if pressure_on:
            try:
                widths = pressure_widths(tuple(pressures), base_pt, profile)
                halves = [max(0.0, float(w)) / 2.0 for w in widths]
            except ValueError:
                halves = [half] * len(raw)
            if isinstance(annotation, Highlighter):
                # The painted nib is ``aspect`` times taller than it is
                # wide; the conservative circular reach that covers the
                # whole visible band is the band's half-height.
                halves = [h * HIGHLIGHTER_BAND_ASPECT for h in halves]
            return raw, halves, tuple(float(p) for p in pressures)
        if isinstance(annotation, Highlighter):
            half = half * HIGHLIGHTER_BAND_ASPECT
        return raw, [half] * len(raw), ()

    def _partial_erase_annotation(self, annotation, segment, radius_pt):
        """Clip one annotation against one eraser segment (Partial mode).

        Returns ``None`` when nothing was erased (no replacement), the
        string ``"delete"`` when the whole stroke is gone (the caller uses
        the existing delete path), or a list of replacement annotations
        (the surviving fragments, one keeping the original identity).
        """
        geometry = self._eraser_raw_geometry(annotation)
        if geometry is None:
            return None
        raw, halves, pressures = geometry
        if len(halves) != len(raw):
            return None
        reach = radius_pt + _ERASER_AA_TOLERANCE_PT
        changed, fragments = erase_stroke(
            raw, pressures, halves,
            [(segment.x0, segment.y0, segment.x1, segment.y1)], reach)
        if not changed:
            return None
        if not fragments:
            return "delete"
        replacements = self._build_fragments(annotation, fragments)
        if not replacements:
            return "delete"
        # The original leaves the store; release its cached geometry now
        # so no stale centerline/outline entry can outlive it.
        self._drop_annotation_caches(annotation)
        return replacements

    def _build_fragments(self, annotation, fragments):
        """Turn clipped fragments into replacement annotations (M6.6).

        The first fragment keeps the original identity (deterministic);
        the rest get fresh identities assigned once here, so Redo
        reproduces the identical fragment set. Style, page, pressure
        mode, and per-point pressure are preserved.
        """
        out = []
        for index, fragment in enumerate(fragments):
            try:
                if index == 0:
                    out.append(self._make_fragment(
                        annotation, fragment, annotation.id))
                else:
                    out.append(self._make_fragment(
                        annotation, fragment, None))
            except Exception:
                return []
        return out

    def _make_fragment(self, annotation, fragment, annotation_id):
        """Build one fragment annotation, preserving style and pressure."""
        points = tuple(Point(float(x), float(y))
                       for x, y in fragment.points)
        kwargs = {
            "page_id": annotation.page_id,
            "created_at": annotation.created_at,
        }
        if annotation_id is not None:
            kwargs["id"] = annotation_id
        if isinstance(annotation, Highlighter):
            return Highlighter(
                points=points,
                color=annotation.color,
                opacity=annotation.opacity,
                width_mm=annotation.width_mm,
                text_assist=annotation.text_assist,
                pressures=fragment.pressures,
                **kwargs)
        return InkStroke(
            points=points,
            color=annotation.color,
            width_mm=annotation.width_mm,
            opacity=annotation.opacity,
            pressures=fragment.pressures,
            times_ms=(),
            pressure_enabled=getattr(annotation, "pressure_enabled", False),
            pressure_profile=getattr(
                annotation, "pressure_profile", "normal"),
            **kwargs)

    def _drop_annotation_caches(self, annotation):
        """Evict cached geometry keyed by a now-replaced annotation."""
        for cache_name in ("_outline_cache", "_off_smooth_cache",
                           "_hl_smooth_cache", "_hl_band_cache"):
            try:
                cache = getattr(self, cache_name, None)
                if cache is not None:
                    cache.pop(annotation, None)
            except Exception:
                pass

    def _eraser_bbox_may_hit(self, annotation, x0: float, y0: float,
                             x1: float, y1: float, radius_pt: float
                             ) -> bool:
        """Conservative bbox prefilter shared by both eraser modes.

        Raw points expanded by the maximum half-width (the base width
        bounds every pressure width) plus the eraser reach; a bounding-box
        overlap alone never triggers removal — the precise geometry test
        does. False on unusable input.
        """
        points = getattr(annotation, "points", ()) or ()
        if not points:
            return False
        try:
            base_half = abs(float(annotation.width_mm)) * 72.0 / 25.4 / 2.0
        except (TypeError, ValueError):
            base_half = 0.0
        if not math.isfinite(base_half):
            base_half = 0.0
        try:
            xs = [float(p.x) for p in points]
            ys = [float(p.y) for p in points]
        except Exception:
            return False
        pad = base_half + radius_pt + _ERASER_AA_TOLERANCE_PT
        if (max(xs) + pad < min(x0, x1) or min(xs) - pad > max(x0, x1)
                or max(ys) + pad < min(y0, y1)
                or min(ys) - pad > max(y0, y1)):
            return False
        return True

    def _eraser_hit(self, annotation, x0: float, y0: float,
                    x1: float, y1: float, radius_pt: float) -> bool:
        """True when an eraser segment intersects the visible annotation.

        A cheap conservative bbox prefilter (:meth:`_eraser_bbox_may_hit`)
        rejects far annotations before the precise geometric test, so a
        bounding-box overlap alone can never delete a long, thin stroke.
        """
        if not self._eraser_bbox_may_hit(annotation, x0, y0, x1, y1,
                                         radius_pt):
            return False
        try:
            geometry = self._eraser_stroke_geometry(annotation)
        except Exception:
            return False
        if geometry is None:
            return False
        centers, halves = geometry
        if not centers or len(halves) != len(centers):
            return False
        limit0 = halves[0] + radius_pt + _ERASER_AA_TOLERANCE_PT
        if len(centers) == 1:
            return self._point_seg_distance(
                centers[0][0], centers[0][1], x0, y0, x1, y1) <= limit0
        for i in range(len(centers) - 1):
            cx0, cy0 = centers[i]
            cx1, cy1 = centers[i + 1]
            limit = max(halves[i], halves[i + 1]) + radius_pt \
                + _ERASER_AA_TOLERANCE_PT
            if self._seg_seg_distance(
                    x0, y0, x1, y1, cx0, cy0, cx1, cy1) <= limit:
                return True
        return False

    # -- M6.7.1: shape erasing (Whole + Partial) -----------------------------

    def _shape_outline_half(self, shape) -> float:
        """Outline half-width in page points (0.0 when unusable)."""
        try:
            base = abs(float(shape.width_mm)) * 72.0 / 25.4
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(base) or base <= 0.0:
            return 0.0
        return base / 2.0

    def _shape_bbox_may_hit(self, shape, x0: float, y0: float,
                            x1: float, y1: float, radius_pt: float) -> bool:
        """Conservative bbox prefilter for shapes (both eraser modes).

        Uses the shape's visible extent (native bbox / point bbox) padded
        by the outline half-width and the eraser reach. A bounding-box
        overlap alone never erases — the precise geometry test does.
        """
        try:
            extent = _shape_extent(shape)
        except Exception:
            return False
        if extent is None:
            return False
        pad = self._shape_outline_half(shape) + radius_pt \
            + _ERASER_AA_TOLERANCE_PT
        minx, miny, maxx, maxy = extent
        if (maxx + pad < min(x0, x1) or minx - pad > max(x0, x1)
                or maxy + pad < min(y0, y1) or miny - pad > max(y0, y1)):
            return False
        return True

    def _shape_hit(self, shape, segment, radius_pt) -> bool:
        """True when the eraser's visible geometry touches ``shape``.

        Shared by BOTH eraser modes: a genuine hit (visible outline or
        filled interior) erases the whole shape. A near miss outside the
        rendered geometry returns False and leaves the shape untouched.
        """
        reach = radius_pt + _ERASER_AA_TOLERANCE_PT
        half = self._shape_outline_half(shape)
        try:
            return bool(_shape_segment_hits(
                shape, segment.x0, segment.y0, segment.x1, segment.y1,
                reach, half))
        except Exception:
            return False

    def _eraser_stroke_geometry(self, annotation):
        """Page-local centerline + per-point half-widths, or None.

        Mirrors the paint path exactly: the smoothed centerline the
        renderer draws (reusing the same helpers and the painter's
        constant-width centerline caches read-only) with a constant
        half-width for Pressure-OFF pen strokes and for highlighters
        without captured pressure, and pressure-variable half-widths (via
        ``pressure_widths`` and the shared ``_hl_geometry``) for
        Pressure-ON pen strokes and pressure-carrying highlighters. No new
        outline generator is introduced.
        """
        points = getattr(annotation, "points", ()) or ()
        raw = [(float(p.x), float(p.y)) for p in points]
        if not raw:
            return None
        try:
            base_pt = abs(float(annotation.width_mm)) * 72.0 / 25.4
        except (TypeError, ValueError):
            base_pt = 0.0
        if not math.isfinite(base_pt) or base_pt <= 0.0:
            return None
        half = base_pt / 2.0
        # Conservative isotropic reach for a highlighter covers the whole
        # painted nib: its visible half-height is ``aspect`` times the
        # half-width, matching the centred rectangular band the painter
        # sweeps (M7.1.4.1).
        band_half = (half * HIGHLIGHTER_BAND_ASPECT
                     if isinstance(annotation, Highlighter) else half)
        pressures = getattr(annotation, "pressures", ())
        if isinstance(annotation, InkStroke):
            pressure_on = (bool(getattr(annotation, "pressure_enabled", False))
                           and len(pressures) == len(raw))
            profile = getattr(annotation, "pressure_profile", "normal")
        elif isinstance(annotation, Highlighter):
            # M7.1.4: rendered band width follows captured pressure.
            pressure_on = bool(pressures) and len(pressures) == len(raw)
            profile = "normal"
        else:
            pressure_on = False
            profile = "normal"
        if pressure_on:
            try:
                if isinstance(annotation, Highlighter):
                    spts, swidths = self._hl_geometry(annotation)
                else:
                    widths = pressure_widths(
                        tuple(pressures), base_pt, profile)
                    spts, swidths = smooth_centerline(raw, widths)
            except ValueError:
                return (tuple(raw), (band_half,) * len(raw))
            if swidths is None or len(swidths) != len(spts):
                return (tuple(spts), (band_half,) * len(spts))
            halves = tuple(max(0.0, float(w)) / 2.0 for w in swidths)
            if isinstance(annotation, Highlighter):
                halves = tuple(h * HIGHLIGHTER_BAND_ASPECT for h in halves)
            return (tuple(spts), halves)
        if isinstance(annotation, Highlighter):
            spts = self._hl_smoothed(annotation)
        else:
            spts = None
            try:
                spts = self._off_smooth_cache.get(annotation)
            except Exception:
                spts = None
            if spts is None:
                try:
                    spts, _ = smooth_centerline(raw, None)
                except ValueError:
                    spts = tuple(raw)
        return (tuple(spts), (band_half,) * len(spts))

    @staticmethod
    def _point_seg_distance(px: float, py: float, x0: float, y0: float,
                            x1: float, y1: float) -> float:
        """Euclidean distance from a point to a segment."""
        dx = x1 - x0
        dy = y1 - y0
        if dx == 0.0 and dy == 0.0:
            return math.hypot(px - x0, py - y0)
        t = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))

    @staticmethod
    def _segments_cross(ax0: float, ay0: float, ax1: float, ay1: float,
                        bx0: float, by0: float, bx1: float, by1: float
                        ) -> bool:
        """True when two segments properly cross (interior intersection).

        Touching/collinear/degenerate cases are not "crossings"; the
        endpoint-to-segment distances in the caller still cover them.
        """
        def orient(ox, oy, px, py, qx, qy):
            return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

        d1 = orient(bx0, by0, bx1, by1, ax0, ay0)
        d2 = orient(bx0, by0, bx1, by1, ax1, ay1)
        d3 = orient(ax0, ay0, ax1, ay1, bx0, by0)
        d4 = orient(ax0, ay0, ax1, ay1, bx1, by1)
        return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))

    @classmethod
    def _seg_seg_distance(cls, ax0: float, ay0: float, ax1: float,
                          ay1: float, bx0: float, by0: float, bx1: float,
                          by1: float) -> float:
        """Minimum distance between two segments (0 when they cross)."""
        if cls._segments_cross(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
            return 0.0
        return min(
            cls._point_seg_distance(ax0, ay0, bx0, by0, bx1, by1),
            cls._point_seg_distance(ax1, ay1, bx0, by0, bx1, by1),
            cls._point_seg_distance(bx0, by0, ax0, ay0, ax1, ay1),
            cls._point_seg_distance(bx1, by1, ax0, ay0, ax1, ay1))

    def _live_highlight_rect(self, preview):
        """Widget-space update rect for the live highlighter, or None.

        Uses the same page-origin mapping as the paint path and the
        raw-points ± half-width fallback of ``_stroke_paint_rect`` (the
        preview has no outline). ``None`` (page off-view, uncomputable)
        means the caller repaints fully.
        """
        return self._stroke_bounds_rect(preview)

    def _stroke_bounds_rect(self, stroke):
        """Conservative widget-space bbox for a plain polyline stroke.

        Shared by the highlighter live preview and its completion rect:
        ``_page_origin_for`` (paint-path mapping) + the raw-points ±
        half-width fallback of ``_stroke_paint_rect`` (no outline).
        ``None`` - caller repaints fully.
        """
        try:
            page_key = getattr(getattr(stroke, "page_id", None),
                               "value", None)
            mapped = self._page_origin_for(page_key)
            if mapped is None:
                return None
            ox, oy, factor = mapped
            bounds = self._stroke_paint_rect(stroke, None, ox, oy, factor)
            if bounds is None:
                return None
            return QRect(bounds)
        except Exception:
            return None

    def _hl_geometry(self, annotation):
        """Smoothed highlighter ``(centerline, widths_or_None)``.

        Shared by paint bounds and hit-testing so live and committed
        geometry agree. Committed highlighters are frozen, so the pair is
        memoized (one entry per annotation, dying with it in
        close_document); the transient live preview is never cached.

        M7.1.4: when the annotation carries per-sample pressures (stylus),
        each width is mapped through the established ``pressure_widths``
        curve from the configured nominal ``width_mm`` and smoothed
        alongside the points. ``None`` widths — legacy annotations, mouse
        input (all-1.0 pressures also maps to the nominal width) and
        malformed data — keep the constant-width band exactly as before.
        """
        committed = False
        try:
            committed = (isinstance(annotation, Highlighter)
                         and not self._is_move_preview(annotation))
        except Exception:
            committed = False
        if committed:
            try:
                cached = self._hl_smooth_cache.get(annotation)
            except Exception:
                cached = None
            if cached is not None:
                return cached
        raw = [(float(p.x), float(p.y))
               for p in (getattr(annotation, "points", ()) or ())]
        try:
            base_pt = abs(float(annotation.width_mm)) * 72.0 / 25.4
        except (AttributeError, TypeError, ValueError):
            base_pt = 0.0
        if not math.isfinite(base_pt) or base_pt <= 0.0:
            base_pt = 1.0
        widths = None
        pressures = getattr(annotation, "pressures", ()) or ()
        if pressures and len(pressures) == len(raw):
            try:
                mapped = pressure_widths(tuple(pressures), base_pt)
            except ValueError:
                mapped = ()
            if mapped and len(mapped) == len(raw):
                widths = mapped
        try:
            spts, swidths = smooth_centerline(raw, widths)
        except ValueError:
            spts, swidths = tuple(raw), widths
        if swidths is not None and len(swidths) != len(spts):
            swidths = None
        geometry = (spts, swidths)
        if committed:
            try:
                self._hl_smooth_cache[annotation] = geometry
            except Exception:
                pass
        return geometry

    def _hl_smoothed(self, annotation):
        """Smoothed centerline for a highlighter, or raw points on failure.

        The centerline half of :meth:`_hl_geometry`, so every consumer
        (paint, bounds, hit-testing, recognition input) shares exactly the
        same smoothed points.
        """
        return self._hl_geometry(annotation)[0]

    @staticmethod
    def _is_highlighter_band(stroke) -> bool:
        """True for highlighter paint items (committed or live preview).

        Bounds work is shared by committed ``Highlighter`` annotations,
        group-move preview candidates, the completion rect, and the
        transient ``LiveHighlighter`` view, so the band branch must
        recognise both classes.
        """
        return isinstance(stroke, (Highlighter, LiveHighlighter))

    def _hl_band_hulls(self, annotation):
        """Page-local rectangular brush footprints for one highlighter.

        The band uses the same smoothed centerline the previous round pen
        drew and the shared Qt-free footprint math in
        ``annotations.ink_geometry`` (``highlighter_band_hulls``): the
        centre of the rectangle follows the centerline, the band extends
        ``HIGHLIGHTER_BAND_ASPECT / 2`` times the effective thickness above
        and below it, and the thickness follows the existing ``width_mm``
        convention (``mm * 72 / 25.4`` page-local pt). M7.1.4: per-sample
        widths from stylus pressure are forwarded to the sweep, so only the
        band's width varies — the rectangular nib and its aspect stay
        intact.

        Committed highlighters are frozen, so their hull rings are
        memoized (one entry per annotation, dying with it in
        close_document, same lifecycle as ``_hl_smooth_cache``). Move
        previews and the transient live preview are never cached: the live
        point list grows with every sample.

        M7.1: while a hold-to-shape morph targets the live preview, the
        hulls are built from the *interpolated* centerline instead (never
        cached), so the rectangular band itself morphs into the recognized
        contour through this one shared geometry path.
        """
        try:
            thickness_pt = float(annotation.width_mm) * 72.0 / 25.4
            if not math.isfinite(thickness_pt) or thickness_pt <= 0.0:
                thickness_pt = 1.0
        except (AttributeError, TypeError, ValueError):
            thickness_pt = 1.0
        try:
            morph = self._morph_preview_points("highlighter", annotation)
        except Exception:
            morph = None
        if morph:
            try:
                return highlighter_band_hulls(morph, thickness_pt)
            except ValueError:
                return ()
        cached = None
        committed = False
        try:
            committed = (isinstance(annotation, Highlighter)
                         and not self._is_move_preview(annotation))
            if committed:
                cached = self._hl_band_cache.get(annotation)
        except Exception:
            cached = None
        if cached is not None:
            return cached
        try:
            spts, swidths = self._hl_geometry(annotation)
            hulls = highlighter_band_hulls(
                spts, thickness_pt, widths=swidths)
        except ValueError:
            hulls = ()
        if committed and hulls:
            try:
                self._hl_band_cache[annotation] = hulls
            except Exception:
                pass
        return hulls

    # -- M7.1 hold-to-shape + highlighter Ink to Line -------------------------

    def _ink_live_view(self):
        """The active ink tool's live view, or None when no stroke is open."""
        try:
            if self._is_highlighter_active():
                live = self._live_highlighter
                return live if getattr(live, "points", None) else None
            if self._is_pen_active():
                live = self._live_stroke
                return live if getattr(live, "points", None) else None
            return None
        except Exception:
            return None

    def _recognition_input(self, live):
        """``(centerline, thickness_pt)`` for recognition, or ``(None, 0)``.

        The centerline is exactly what the paint path draws as the raw
        side of a morph: the smoothed live centerline for the pen and the
        shared ``_hl_smoothed`` view for the highlighter — so recognition
        judges the geometry the user actually sees. Thickness follows the
        established ``width_mm * 72 / 25.4`` page-pt conversion.
        """
        try:
            if self._is_highlighter_active():
                points = self._hl_smoothed(live)
            else:
                points = None
                try:
                    points = self._interaction.live_centerline()
                except Exception:
                    points = None
                if not points:
                    raw = [(float(p.x), float(p.y))
                           for p in (getattr(live, "points", ()) or ())]
                    try:
                        points, _ = smooth_centerline(raw, None)
                    except ValueError:
                        points = tuple(raw)
            try:
                thickness = float(live.width_mm) * 72.0 / 25.4
                if not math.isfinite(thickness) or thickness < 0.0:
                    thickness = 0.0
            except (AttributeError, TypeError, ValueError):
                thickness = 0.0
            return points, thickness
        except Exception:
            return None, 0.0

    @staticmethod
    def _stroke_side_direction(points, *, at_end: bool):
        """Unit travel direction at one end of an ordered stroke.

        ``at_end`` returns the direction the pen was moving as it reached
        the final sample; otherwise the direction it left the first sample.
        ``None`` for a degenerate stroke.
        """
        pts = list(points or ())
        if len(pts) < 2:
            return None
        span = min(len(pts), 6)
        if at_end:
            a, b = pts[-span], pts[-1]
        else:
            a, b = pts[0], pts[span - 1]
        dx = float(b[0]) - float(a[0])
        dy = float(b[1]) - float(a[1])
        length = math.hypot(dx, dy)
        if not math.isfinite(length) or length <= 0.0:
            return None
        return (dx / length, dy / length)

    @classmethod
    def _seam_turn_ok(cls, prev_points, next_points) -> bool:
        """True when two sides meet at a corner, not a hairpin reversal."""
        incoming = cls._stroke_side_direction(prev_points, at_end=True)
        outgoing = cls._stroke_side_direction(next_points, at_end=False)
        if incoming is None or outgoing is None:
            return False
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        dot = -1.0 if dot < -1.0 else (1.0 if dot > 1.0 else dot)
        return math.degrees(math.acos(dot)) <= CHAIN_MAX_TURN_DEG

    def _chain_ring_for(self, live, live_points):
        """Merged ring of the live pen stroke plus connected raw strokes.

        M7.1.4.1, conservative ink-chain merge: separately captured *raw
        pen* sides (:class:`InkStroke` only — never vector shapes, arrows or
        highlighters) may be joined end-to-end into one outline when their
        endpoints genuinely meet. The merge is only a recognition input:
        the caller must still get a closed, fully evidenced candidate from
        :func:`recognize_shape`, so proximity alone never accepts a shape.

        Returns ``(merged_points, consumed_ids)`` or ``None``. Consumed ids
        are exactly the committed strokes on the winning path; the caller
        replaces them with the recognized polygon in one grouped undo step.
        """
        try:
            if self._is_highlighter_active():
                return None
            page_index = self._page_index_for(getattr(live, "page_id", None))
            if page_index is None:
                return None
            store = self._document.annotations
            page_items = list(store.page(page_index))
        except Exception:
            return None
        # Recent raw pen strokes only (the live gesture is not committed
        # yet). A stroke that does not itself read as a straight side is
        # unrelated ink and is never folded into a polygon.
        sides: list = []
        for annotation in page_items:
            if not isinstance(annotation, InkStroke):
                continue
            try:
                pts = [(float(p.x), float(p.y))
                       for p in (getattr(annotation, "points", ()) or ())]
                if len(pts) < 2:
                    continue
                own = abs(float(annotation.width_mm)) * 72.0 / 25.4
            except Exception:
                continue
            try:
                own_candidate = recognize_shape(pts, own)
            except Exception:
                own_candidate = None
            if own_candidate is None or own_candidate.kind != "line":
                continue
            sides.append({"points": pts, "id": annotation.id})
        sides = sides[-(CHAIN_MAX_SIDES - 1):]
        if len(sides) < CHAIN_MIN_SIDES - 1:
            return None
        try:
            live_pts = []
            for point in (live_points or ()):
                try:
                    live_pts.append((float(point[0]), float(point[1])))
                except (TypeError, IndexError, KeyError):
                    live_pts.append((float(point.x), float(point.y)))
        except Exception:
            return None
        if len(live_pts) < 2:
            return None
        sides.append({"points": live_pts, "id": None})
        live_index = len(sides) - 1

        def bbox_diag(items):
            points = [p for item in items for p in sides[item]["points"]]
            if not points:
                return 0.0
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return math.hypot(max(xs) - min(xs), max(ys) - min(ys))

        # Pair stroke ends that genuinely meet. The tolerance is local to
        # the two strokes, so unrelated ink elsewhere on the page can never
        # inflate a seam. Each end is used at most once, each side has
        # degree at most two, and pairs are chosen nearest-first with
        # stable end indices so ordering is deterministic.
        ends = []
        for index, side in enumerate(sides):
            ends.append((index, 0, side["points"][0]))
            ends.append((index, 1, side["points"][-1]))
        pairs = []
        for first in range(len(ends)):
            for second in range(first + 1, len(ends)):
                a_item, _a_end, a_pt = ends[first]
                b_item, _b_end, b_pt = ends[second]
                if a_item == b_item:
                    continue
                distance = math.hypot(a_pt[0] - b_pt[0],
                                      a_pt[1] - b_pt[1])
                local = bbox_diag((a_item, b_item))
                tol_pair = max(CHAIN_MERGE_RATIO * local,
                               _CHAIN_MIN_TOL_PT)
                if distance <= tol_pair:
                    pairs.append((distance / tol_pair, first, second))
        pairs.sort(key=lambda item: (item[0], item[1], item[2]))
        degree = {i: 0 for i in range(len(sides))}
        used: set = set()
        seam_of: dict = {}
        partners: set = set()
        for _distance, first, second in pairs:
            if first in used or second in used:
                continue
            item_a = ends[first][0]
            item_b = ends[second][0]
            if degree[item_a] >= 2 or degree[item_b] >= 2:
                continue
            if (item_a, item_b) in partners or (item_b, item_a) in partners:
                continue
            used.add(first)
            used.add(second)
            degree[item_a] += 1
            degree[item_b] += 1
            partners.add((item_a, item_b))
            seam_of[(ends[first][0], ends[first][1])] = (
                ends[second][0], ends[second][1])
            seam_of[(ends[second][0], ends[second][1])] = (
                ends[first][0], ends[first][1])

        # The connected component around the live stroke must be one simple
        # open path (exactly two free ends), not a branch or a stray pair.
        adjacency = {i: set() for i in range(len(sides))}
        for (item_a, _ea), (item_b, _eb) in list(seam_of.items()):
            adjacency[item_a].add(item_b)
            adjacency[item_b].add(item_a)
        component: set = set()
        stack = [live_index]
        while stack:
            item = stack.pop()
            if item in component:
                continue
            component.add(item)
            stack.extend(adjacency[item] - component)
        if len(component) < CHAIN_MIN_SIDES:
            return None
        used_ends = {ends[index][:2] for index in used}
        free_ends = [(item, end) for item in component for end in (0, 1)
                     if (item, end) not in used_ends]
        if len(free_ends) != 2:
            return None
        if any(degree[item] not in (1, 2) for item in component):
            return None
        # Every side outside the component stays untouched by this path (the
        # matching may already have paired unrelated strokes; those pairs
        # stay out of the component and are simply ignored).
        free_ends.sort()
        start_item, start_end = free_ends[0]
        end_item, end_end = free_ends[1]
        start_pt = sides[start_item]["points"][0 if start_end == 0 else -1]
        end_pt = sides[end_item]["points"][0 if end_end == 0 else -1]
        chain_diag = bbox_diag(component)
        close_tol = max(CHAIN_MERGE_RATIO * chain_diag, _CHAIN_MIN_TOL_PT)
        if math.hypot(start_pt[0] - end_pt[0],
                      start_pt[1] - end_pt[1]) > close_tol:
            return None

        # Walk the path end-to-end, orienting every side so its seam to the
        # previous side is its first sample, and reject hairpin seams.
        order: list = []
        current_item, entry_end = start_item, start_end
        previous_points = None
        seen: set = set()
        while True:
            if current_item in seen:
                return None
            seen.add(current_item)
            points = sides[current_item]["points"]
            if entry_end == 1:
                points = list(reversed(points))
            if previous_points is not None and not self._seam_turn_ok(
                    previous_points, points):
                return None
            order.append((current_item, points))
            previous_points = points
            exit_end = 0 if entry_end == 1 else 1
            nxt = seam_of.get((current_item, exit_end))
            if nxt is None:
                break
            current_item, entry_end = nxt
        if set(item for item, _pts in order) != component:
            return None

        merged: list = []
        for _item, points in order:
            for point in points:
                if merged and math.hypot(point[0] - merged[-1][0],
                                         point[1] - merged[-1][1]) <= 1e-9:
                    continue
                merged.append(point)
        if len(merged) < 4:
            return None
        consumed = tuple(sides[item]["id"] for item, _pts in order
                         if sides[item]["id"] is not None)
        if len(consumed) < CHAIN_MIN_SIDES - 1:
            return None
        return tuple(merged), consumed

    def _note_shape_movement(self, live) -> None:
        """Restart the hold timer after a *real* pointer movement.

        Called from every applied ink update (mouse and stylus share this
        path). The comparison uses a damped pointer position against the
        anchored hold position, so two failure modes are avoided at once:
        oscillating stylus jitter (the average barely moves, whatever the
        swing) and slow drift (the accumulated distance is what counts,
        not the distance between two consecutive samples). Genuine
        drawing — including deliberately slow drawing — moves the average
        past the epsilon and restarts the dwell.

        A movement while a morph is live enters live adjustment for a
        recognized pen line (Phase A, :meth:`_note_line_adjustment`) and
        for every recognized closed pen shape (Phase B Part 2,
        :meth:`_note_closed_adjustment`): the preview stays and the frozen
        handle follows the stylus. Highlighter conversions and any other
        morph return to the freehand geometry, so the next genuine pause
        may recognize again.
        """
        try:
            points = getattr(live, "points", ()) or ()
            if not points:
                return
            last = points[-1]
            x, y = float(last.x), float(last.y)
        except Exception:
            return
        ema = self._shape_move_ema
        if ema is None:
            ema = (x, y)
        else:
            alpha = _HOLD_MOVE_EMA_ALPHA
            ema = (ema[0] + (x - ema[0]) * alpha,
                   ema[1] + (y - ema[1]) * alpha)
        self._shape_move_ema = ema
        try:
            eps = _HOLD_MOVE_EPS_PX / max(float(self._ui_zoom), 1e-9)
        except Exception:
            eps = _HOLD_MOVE_EPS_PX
        anchor = self._shape_move_anchor
        # M7.1 Phase A: a recognized pen line stays live-adjustable while
        # the pointer keeps moving; the moving endpoint follows the stylus
        # and the opposite endpoint anchors the geometry. Non-line morphs
        # (highlighter, closed shapes) keep the historical cancel path.
        # Checked before the damped-jitter return: the line adjustment gates
        # on the *raw* pointer offset from the recognition origin, so the
        # EMA's lag can neither fake nor postpone activation.
        try:
            if self._note_line_adjustment(live, eps):
                return
        except Exception:
            pass
        # Phase B Part 2: the recognized closed shapes stay live-adjustable
        # through the same gesture. Highlighter conversions still fall
        # through to the historical cancel path.
        try:
            if self._note_closed_adjustment(live, eps):
                return
        except Exception:
            pass
        if anchor is not None and math.hypot(ema[0] - anchor[0],
                                            ema[1] - anchor[1]) < eps:
            # Jitter, not drawing: an armed morph must survive it too.
            # (Cancelling on every applied sample would drop a *valid*
            # conversion a moment after it appeared, so the release then
            # committed the freehand stroke again.)
            return
        if self._shape_morph is not None:
            try:
                self._clear_shape_conversion()
            except Exception:
                pass
            # _clear_shape_conversion drops the damping state; restore the
            # current average so this same gesture keeps a stable anchor.
            self._shape_move_ema = ema
        self._shape_move_anchor = ema
        try:
            if self._ink_live_view() is not None:
                self._shape_dwell_timer.start()
            else:
                self._shape_dwell_timer.stop()
        except Exception:
            pass

    def _note_line_adjustment(self, live, eps) -> bool:
        """Live endpoint adjustment for a recognized pen line (Phase A).

        Returns True when this movement belongs to the line-adjustment path
        (including sub-threshold jitter, which must keep the recognized
        preview alive); False lets closed pen shapes use their own path
        (:meth:`_note_closed_adjustment`) and leaves highlighter
        conversions and stale targets on the historical behaviour.

        The first movement whose *raw* pointer offset from the recognition
        origin reaches the jitter epsilon switches the armed morph into
        adjustment: the recognized contour is rebased into the preview space
        the morph showed, one endpoint is frozen as the anchor and the other
        becomes the moving endpoint. Every later sample is applied
        absolutely from the recognition origin, so the geometry is
        recomputed from scratch each time (no drift) and a rejected
        degenerate frame simply leaves the last valid preview on screen.

        M7.1 Phase B: while adjustment is live, Connect-Lines snapping is
        always on for the moving endpoint (D1=A). A genuine endpoint or
        intersection target resolves through the same context and resolver
        the shape tools use; when one exists, the moving endpoint is placed
        on it exactly and the shared circular indicator identifies it.
        Leaving the tolerance clears the indicator and restores ordinary
        absolute pointer tracking.
        """
        state = self._shape_morph
        if state is None or state.get("kind") != "pen":
            return False
        candidate = state.get("candidate")
        if (candidate is None or getattr(candidate, "kind", None) != "line"
                or bool(getattr(candidate, "closed", False))):
            return False
        if state.get("target") is not live:
            return False
        if state.get("adjust") is None:
            pointer = self._live_pointer_pt(live)
            origin = state.get("origin_pt")
            if pointer is None or origin is None:
                return True
            if math.hypot(pointer[0] - origin[0],
                          pointer[1] - origin[1]) < eps:
                # Jitter, not drawing: keep the recognized preview.
                return True
            if not self._activate_line_adjustment():
                return False
        adjustment = (state.get("adjust") or {}).get("adjustment")
        pointer = self._live_pointer_pt(live)
        if adjustment is None or pointer is None:
            return True
        snap = self._line_adjust_snap_target(live, adjustment, pointer)
        if snap is not None:
            new = snapped_line(adjustment, (snap[0], snap[1]))
        else:
            new = adjusted_line(adjustment, pointer)
        if new is None:
            # Degenerate (shorter than the recognized minimum): freeze the
            # last valid preview — and its resolved indicator, if any —
            # instead of flickering back to freehand.
            return True
        state["candidate"] = new
        state["snap"] = snap
        state["fitted"] = tuple(
            (float(p[0]), float(p[1])) for p in new.contour)
        try:
            self._repaint_shape_morph()
        except Exception:
            pass
        return True

    def _line_adjust_snap_target(self, live, adjustment, pointer):
        """Connect-Lines target for the adjusted moving endpoint, or None.

        Reuses the existing Connect-Lines machinery end to end (M7.1
        Phase B, decision D1=A): ``_shape_snap_context`` supplies the
        committed endpoints/segments on the live page with the established
        screen-pixel tolerance, and the shared :func:`snap_hit` resolver
        applies the shape tools' own target rule. Endpoint and intersection
        snapping are always enabled for this interaction; the adjustment's
        immutable opposite endpoint is the anchor used for intersection
        resolution, exactly like a Line tool's start point.

        Returns ``(x, y, "endpoint"|"intersection")`` in page-local points,
        or None when no genuine target is inside the tolerance. Paint-only
        evidence: callers use it for the geometry *and* the shared
        indicator; nothing is stored on the annotation.
        """
        try:
            page_id = getattr(live, "page_id", None)
            if page_id is None:
                return None
            context = self._shape_snap_context(page_id)
            if context is None or float(
                    getattr(context, "tol_pt", 0.0) or 0.0) <= 0.0:
                return None
            hit = snap_hit(context, pointer[0], pointer[1],
                           anchor=adjustment.anchor,
                           endpoints=True, intersections=True)
            if hit is None:
                return None
            kind, target = hit
            return (float(target[0]), float(target[1]), str(kind))
        except Exception:
            return None

    def _activate_line_adjustment(self) -> bool:
        """Switch an armed, recognized pen line into live adjustment.

        The recognized contour is translated by the morph preview offset so
        the preview the user is looking at and the geometry that will be
        committed are the same line. The morph timer is stopped at phase 1
        (the fitted frame is already on screen) and the adjustment state is
        installed; nothing is committed here.
        """
        state = self._shape_morph
        if state is None:
            return False
        candidate = state.get("candidate")
        origin = state.get("origin_pt")
        if candidate is None or origin is None:
            return False
        offset = state.get("preview_offset") or (0.0, 0.0)
        base = rebase_line(candidate, offset)
        if base is None:
            return False
        adjustment = begin_line_adjustment(base, origin)
        if adjustment is None:
            return False
        state["candidate"] = base
        state["adjust"] = {"adjustment": adjustment}
        state["snap"] = None
        state["phase"] = 1.0
        try:
            self._shape_morph_timer.stop()
        except Exception:
            pass
        return True

    def _note_closed_adjustment(self, live, eps) -> bool:
        """Live adjustment for a recognized closed pen shape (Phase B.2).

        Returns True when this movement belongs to the closed-shape path
        (including sub-threshold jitter, which keeps the preview alive);
        False leaves highlighters and non-shape morphs on the historical
        cancel path.

        The first sample whose *raw* pointer offset from the recognition
        origin reaches the jitter threshold switches the armed morph into
        adjustment: the recognized contour is rebased by the exact morph
        translation, the handle nearest the recognition pointer is frozen,
        and the morph timer stops at its completed frame. Every later
        sample is an absolute delta from that frozen base, so repeated
        samples cannot drift and returning to the origin restores the
        rebased geometry. There is deliberately no connector snapping
        here: Connect-Lines remains line-only.
        """
        state = self._shape_morph
        if state is None or state.get("kind") != "pen":
            return False
        candidate = state.get("candidate")
        if candidate is None or not bool(getattr(candidate, "closed", False)):
            return False
        if getattr(candidate, "kind", None) not in CLOSED_KINDS:
            return False
        if state.get("target") is not live:
            return False
        if state.get("adjust") is None:
            pointer = self._live_pointer_pt(live)
            origin = state.get("origin_pt")
            if pointer is None or origin is None:
                return True
            if math.hypot(pointer[0] - origin[0],
                          pointer[1] - origin[1]) < eps:
                # Jitter, not adjustment: keep the recognized preview.
                return True
            if not self._activate_closed_adjustment():
                return False
        adjustment = (state.get("adjust") or {}).get("adjustment")
        pointer = self._live_pointer_pt(live)
        if adjustment is None or pointer is None:
            return True
        new = adjusted_closed(adjustment, pointer)
        if new is None:
            # Invalid frame: freeze the last valid preview (the caller
            # commits that geometry on release).
            return True
        state["candidate"] = new
        state["fitted"] = tuple(
            (float(point[0]), float(point[1])) for point in new.contour)
        state["snap"] = None
        try:
            self._repaint_shape_morph()
        except Exception:
            pass
        return True

    def _activate_closed_adjustment(self) -> bool:
        """Switch an armed closed-shape morph into live adjustment.

        Rebases the recognized contour by the same translation the morph
        preview applied, freezes the nearest handle, and stops the morph
        timer at the completed frame. Nothing is committed here.
        """
        state = self._shape_morph
        if state is None:
            return False
        candidate = state.get("candidate")
        origin = state.get("origin_pt")
        if candidate is None or origin is None:
            return False
        offset = state.get("preview_offset") or (0.0, 0.0)
        base = rebase_closed_candidate(candidate, offset)
        if base is None:
            return False
        adjustment = begin_closed_adjustment(
            base, origin, state.get("thickness_pt", 0.0))
        if adjustment is None:
            return False
        state["candidate"] = base
        state["adjust"] = {"adjustment": adjustment}
        state["snap"] = None
        state["phase"] = 1.0
        try:
            self._shape_morph_timer.stop()
        except Exception:
            pass
        return True

    @staticmethod
    def _live_pointer_pt(live):
        """Page-local position of the live view's latest sample, or None."""
        try:
            last = live.points[-1]
            x = float(last.x)
            y = float(last.y)
        except (AttributeError, IndexError, TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return (x, y)

    @staticmethod
    def _morph_preview_offset(candidate, raw, fitted, morph):
        """Candidate-to-preview translation of an armed morph (page pt).

        ``prepare_morph`` translates the fitted side so its centroid
        coincides with the raw stroke's centroid. The final morph frame —
        what the user sees while holding — therefore sits at
        ``candidate.contour + offset``, while an *unadjusted* release
        commits the unshifted contour: the historical behaviour, kept
        unchanged here. Live adjustment rebases the contour by this offset
        so the adjusted preview and its committed annotation are the same
        geometry.

        Phase A lines keep the established centroid difference (exact for
        a uniformly resampled segment). Closed shapes use
        :func:`morph_translation`, which reproduces the exact translation
        ``prepare_morph`` applied — a vertex centroid would be wrong for
        an ordered ring.
        """
        try:
            kind = getattr(candidate, "kind", None)
            if kind == "line" and not bool(
                    getattr(candidate, "closed", False)):
                preview = centroid(morph[1])
                source = centroid(fitted)
                if preview is None or source is None:
                    return (0.0, 0.0)
                return (preview[0] - source[0], preview[1] - source[1])
            if (bool(getattr(candidate, "closed", False))
                    and kind in CLOSED_KINDS):
                return morph_translation(raw, fitted, closed=True)
        except Exception:
            pass
        return (0.0, 0.0)

    def _on_shape_dwell(self) -> None:
        """Dwell elapsed: recognize once, then arm the morph (or do nothing).

        M7.1.4.1: a straight/ambiguous single stroke additionally offers the
        connected raw-stroke chain to recognition. A closed, fully evidenced
        chain result wins over the single-stroke line reading; otherwise the
        single-stroke candidate (or no conversion) stands exactly as before.
        """
        try:
            if self._shape_morph is not None:
                return
            live = self._ink_live_view()
            if live is None:
                return
            points, thickness = self._recognition_input(live)
            if not points:
                return
            candidate = recognize_shape(points, thickness)
            if candidate is not None and candidate.closed:
                self._begin_shape_morph(live, points, candidate, thickness)
                return
            # M7.1.4.1: no closed single-stroke reading. Offer the raw
            # pen-stroke chain (sides captured separately) to the same
            # recognizer; the merged ring must earn a closed candidate on
            # its own evidence. A straight side alone still arms the usual
            # line morph when no credible chain exists.
            chain = self._chain_ring_for(live, points)
            if chain is not None:
                ring, consumed = chain
                merged = recognize_shape(ring, thickness)
                if (merged is not None and merged.closed
                        and merged.kind in ("polygon", "triangle",
                                            "rectangle")):
                    self._begin_shape_morph(
                        live, ring, merged, thickness, chain_ids=consumed)
                    return
            if candidate is not None:
                self._begin_shape_morph(live, points, candidate, thickness)
        except Exception:
            pass

    def _morph_fitted_side(self, candidate):
        """The fitted contour in exactly the form the release will render.

        The pen commits clean ``Shape`` geometry that its own painter draws
        verbatim, so the fitted side is the contour itself. A highlighter
        band always renders its centerline through ``smooth_centerline``
        (committed and live alike), so its fitted side is pre-smoothed with
        that same shared function — the last morph frame and the committed
        band then paint the identical centerline.
        """
        points = tuple(candidate.contour or ())
        if not points:
            return points
        if not self._is_highlighter_active():
            return points
        ring = (points + (points[0],)) if candidate.closed else points
        try:
            smoothed, _ = smooth_centerline(ring, None)
        except ValueError:
            return points
        smoothed = tuple(smoothed)
        if (candidate.closed and len(smoothed) > 1
                and smoothed[-1] == smoothed[0]):
            smoothed = smoothed[:-1]
        return smoothed

    def _begin_shape_morph(self, live, raw, candidate, thickness,
                           chain_ids=()) -> None:
        """Arm the conversion morph toward ``candidate`` (or jump if reduced)."""
        try:
            fitted = self._morph_fitted_side(candidate)
            morph = prepare_morph(raw, fitted, closed=candidate.closed)
        except Exception:
            return
        try:
            duration = 0 if reduced_motion_enabled() else int(MORPH_MS)
        except Exception:
            duration = int(MORPH_MS)
        try:
            self._shape_dwell_timer.stop()
        except Exception:
            pass
        # Anchor the damped position *now*: while the morph preview is
        # shown, continued stylus jitter must not cancel it. Only movement
        # past the epsilon (real drawing) does, in _note_shape_movement.
        self._shape_move_anchor = self._shape_move_ema
        try:
            page_key = getattr(live.page_id, "value", None)
        except Exception:
            page_key = None
        self._shape_morph = {
            "kind": ("highlighter" if self._is_highlighter_active()
                     else "pen"),
            "target": live,
            "page_id": page_key,
            "raw": morph[0],
            "fitted": morph[1],
            "candidate": candidate,
            "thickness_pt": float(thickness),
            "chain_ids": tuple(chain_ids or ()),
            "phase": 1.0 if duration <= 0 else 0.0,
            "start_ms": time.monotonic() * 1000.0,
            "duration_ms": duration,
            "prev_rect": None,
            # M7.1 Phase A live line adjustment. ``origin_pt`` is the
            # absolute page-local pointer position at recognition (absolute
            # updates are measured from it, so no incremental delta can
            # accumulate); ``preview_offset`` is the candidate-to-preview
            # translation prepare_morph applied; ``adjust`` is None until
            # the first real movement switches a line morph into adjustment.
            "origin_pt": self._live_pointer_pt(live),
            "preview_offset": self._morph_preview_offset(
                candidate, raw, fitted, morph),
            "adjust": None,
            # M7.1 Phase B: resolved Connect-Lines target for the adjusted
            # moving endpoint — (x, y, "endpoint"|"intersection") in page
            # points, or None. Paint-only evidence, like the shape tools'.
            "snap": None,
        }
        if duration > 0:
            try:
                self._shape_morph_timer.start()
            except Exception:
                pass
        self._repaint_shape_morph()

    def _morph_phase(self, state) -> float:
        """Eased morph phase (ease-out cubic) for one morph state."""
        try:
            duration = int(state.get("duration_ms") or 0)
        except Exception:
            duration = 0
        if duration <= 0:
            return 1.0
        try:
            elapsed = time.monotonic() * 1000.0 - float(
                state.get("start_ms") or 0.0)
        except Exception:
            return 1.0
        if elapsed <= 0.0:
            return 0.0
        if elapsed >= duration:
            return 1.0
        t = elapsed / float(duration)
        return 1.0 - (1.0 - t) ** 3

    def _advance_shape_morph(self) -> None:
        """One morph tick: advance the phase and repaint only its region."""
        try:
            state = self._shape_morph
            if state is None:
                self._shape_morph_timer.stop()
                return
            if state.get("adjust") is not None:
                # Phase A: live adjustment owns the preview now; the morph
                # timer must never overwrite the adjusted geometry.
                self._shape_morph_timer.stop()
                return
            state["phase"] = self._morph_phase(state)
            if state["phase"] >= 1.0:
                # The morph is complete; the fitted geometry stays on
                # screen (the live view keeps painting it) until release.
                self._shape_morph_timer.stop()
            self._repaint_shape_morph()
        except Exception:
            try:
                self._shape_morph_timer.stop()
            except Exception:
                pass

    def _morph_preview_points(self, kind, target):
        """Interpolated page-local centerline for a matching morph, or None.

        Identity of ``target`` (the live view object) is the stale-gesture
        guard: a morph captured for a previous stroke can never substitute
        geometry into a newer one. A closed conversion repeats its first
        frame point so the preview ring is complete — exactly the ring the
        release commits (both the pen polyline and the swept highlighter
        band then end on the closing edge).
        """
        state = self._shape_morph
        if state is None:
            return None
        try:
            if state.get("kind") != kind or state.get("target") is not target:
                return None
            if state.get("adjust") is not None:
                # M7.1 Phase A: live-adjusted line geometry is painted
                # verbatim (already in preview/commit space); no morph
                # interpolation runs while adjustment owns the gesture.
                candidate = state.get("candidate")
                contour = tuple(getattr(candidate, "contour", ()) or ())
                if not contour:
                    return None
                points = tuple((float(p[0]), float(p[1])) for p in contour)
                if bool(getattr(candidate, "closed", False)):
                    points = points + (points[0],)
                return points
            points = morph_points((state.get("raw"), state.get("fitted")),
                                  state.get("phase", 1.0))
            candidate = state.get("candidate")
            if points and getattr(candidate, "closed", False):
                points = points + (points[0],)
            return points
        except Exception:
            return None

    def _morph_widget_rect(self):
        """Conservative widget-space rect covering raw AND fitted geometry."""
        state = self._shape_morph
        if state is None:
            return None
        try:
            samples = list(state.get("raw") or ()) + list(
                state.get("fitted") or ())
            if not samples:
                return None
            mapped = self._page_origin_for(state.get("page_id"))
            if mapped is None:
                return None
            ox, oy, factor = mapped
            scale = max(float(factor), 1e-9)
            xs = [p[0] for p in samples]
            ys = [p[1] for p in samples]
            # Pad by the largest footprint the two sides could paint (the
            # highlighter band height) plus the AA margin, so no morph
            # frame can leave stale pixels behind.
            try:
                thickness = float(state.get("thickness_pt") or 0.0)
            except Exception:
                thickness = 0.0
            pad = max(0.0, thickness) * HIGHLIGHTER_BAND_ASPECT * scale + 6.0
            # M7.1 Phase B: the shared snap ring is drawn around the
            # resolved Connect-Lines target (the moving endpoint), so its
            # geometry needs the same margin the shape layer uses to
            # repaint/erase the indicator.
            try:
                if state.get("snap"):
                    pad += float(math.ceil(_SNAP_RING_PX)) + 3.0
            except Exception:
                pass
            left = ox + min(xs) * scale - pad
            top = oy + min(ys) * scale - pad
            width = (max(xs) - min(xs)) * scale + 2.0 * pad
            height = (max(ys) - min(ys)) * scale + 2.0 * pad
            rect = QRect(int(math.floor(left)), int(math.floor(top)),
                         int(math.ceil(width)) + 1, int(math.ceil(height)) + 1)
            if rect.width() <= 0 or rect.height() <= 0:
                return None
            return rect
        except Exception:
            return None

    def _repaint_shape_morph(self) -> None:
        """Repaint the union of the previous and current morph bounds."""
        try:
            rect = self._morph_widget_rect()
            state = self._shape_morph
            prev = state.get("prev_rect") if state else None
            if state is not None:
                state["prev_rect"] = QRect(rect) if rect is not None else None
            if rect is None or prev is None:
                self.update()
                return
            self.update(QRect(rect).united(QRect(prev)))
        except Exception:
            try:
                self.update()
            except Exception:
                pass

    def _clear_shape_conversion(self, *, repaint: bool = True):
        """Drop dwell/morph state; optionally repaint the morph's area.

        Returns the morph's last repaint rect (widget space) or None, so a
        release path can union it into its own completion rect instead of
        repainting twice.
        """
        try:
            self._shape_dwell_timer.stop()
        except Exception:
            pass
        try:
            self._shape_morph_timer.stop()
        except Exception:
            pass
        state = self._shape_morph
        self._shape_morph = None
        self._shape_move_anchor = None
        self._shape_move_ema = None
        prev = None
        try:
            prev = state.get("prev_rect") if state else None
        except Exception:
            prev = None
        if repaint and state is not None:
            # Only a live morph leaves fitted pixels to wipe; with no morph
            # (idle dwell state only) the caller's own repaint suffices.
            try:
                if prev is not None:
                    self.update(QRect(prev))
                else:
                    self.update()
            except Exception:
                try:
                    self.update()
                except Exception:
                    pass
        return prev

    def _highlighter_snap_enabled(self) -> bool:
        """The active highlighter slot's persisted "Ink to Line" toggle."""
        try:
            tool = self._interaction.active_tool
            return bool(getattr(tool, "straight_line", False))
        except Exception:
            return False

    @staticmethod
    def _highlighter_candidate_points(candidate):
        """Page-local ``Point`` tuple for a recognized highlighter contour.

        Closed contours repeat their first vertex so the swept rectangular
        band also covers the closing edge (the highlighter's centerline is
        an open polyline by model contract).
        """
        try:
            points = [Point(float(x), float(y)) for x, y in candidate.contour]
        except Exception:
            return ()
        if not points:
            return ()
        if candidate.closed:
            points.append(points[0])
        return tuple(points)

    def _snapped_highlighter_points(self, done):
        """The straight segment for an enabled "Ink to Line" release.

        The segment keeps the recorded pointer endpoints (no surprise
        displacement on commit); a degenerate tap returns ``None`` so the
        freehand result is committed unchanged.
        """
        try:
            points = getattr(done, "points", ()) or ()
            if len(points) < 2:
                return None
            start, end = points[0], points[-1]
            distance = math.hypot(float(end.x) - float(start.x),
                                  float(end.y) - float(start.y))
            if distance < MIN_SNAP_PT:
                return None
            return (Point(float(start.x), float(start.y)),
                    Point(float(end.x), float(end.y)))
        except Exception:
            return None

    def _final_highlighter_annotation(self, done, live):
        """The highlighter geometry to commit for a finished gesture.

        Hold-to-shape wins (the morph preview is what the user saw); with
        no conversion and the slot's "Ink to Line" enabled, the stroke is
        straightened between its recorded endpoints. Otherwise the
        freehand annotation commits unchanged — the two paths can never
        both apply to one stroke.

        M7.1.4: the freehand path commits the handler's ``Highlighter``
        verbatim, so its captured pressure samples survive. The two
        converted paths commit a fixed recognized segment/contour at the
        configured nominal width and deliberately carry no pressure:
        their geometry no longer follows the drawn samples.
        """
        try:
            state = self._shape_morph
            if (state is not None and state.get("kind") == "highlighter"
                    and state.get("target") is live):
                points = self._highlighter_candidate_points(
                    state.get("candidate"))
                if len(points) >= 2:
                    return Highlighter(page_id=done.page_id, points=points,
                                       color=done.color,
                                       width_mm=done.width_mm,
                                       opacity=done.opacity)
            if self._hl_snap_latched:
                points = self._snapped_highlighter_points(done)
                if points is not None:
                    return Highlighter(page_id=done.page_id, points=points,
                                       color=done.color,
                                       width_mm=done.width_mm,
                                       opacity=done.opacity)
        except Exception:
            pass
        return done

    @staticmethod
    def _shape_bbox_rect(points):
        """Page-local ``Rect`` bounding a point sequence (never empty)."""
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        return Rect(Point(minx, miny),
                    Size(max(maxx - minx, 0.01), max(maxy - miny, 0.01)))

    def _shape_from_candidate(self, candidate, ink, *, force_polygon=False):
        """Build the recognized ``Shape`` for a pen conversion, or None.

        Properties are explicit and conservative (M7.1): the pen's own
        color/width/opacity, no fill, solid line style, no endings, no
        extra rotation beyond the fitted box angle, and a closed path only
        for the polygon shapes (triangle and generic polygon). The generic
        polygon stores exactly the recognizer's fitted, ordered vertices —
        never the resampled recognition samples.

        M7.1.4.1: a chain-merged outline is always committed as one editable
        closed ``ShapeKind.POLYGON`` carrying the actual fitted vertices
        (``force_polygon``), even when the fitted contour is box-shaped, so
        separately captured sides can never become a polygon plus a
        duplicate closing line.
        """
        try:
            contour = tuple(candidate.contour or ())
            if not contour:
                return None
            common = dict(page_id=ink.page_id, color=ink.color,
                          width_mm=ink.width_mm, opacity=ink.opacity,
                          fill_enabled=False, fill_color="#ffffff",
                          fill_opacity=1.0, line_style="solid",
                          start_ending="none", end_ending="none")
            if candidate.kind == "line" and len(contour) >= 2:
                p0, p1 = contour[0], contour[1]
                return Shape(kind=ShapeKind.LINE,
                             rect=self._shape_bbox_rect((p0, p1)),
                             points=(Point(float(p0[0]), float(p0[1])),
                                     Point(float(p1[0]), float(p1[1]))),
                             **common)
            if candidate.kind in ("triangle", "polygon") and len(
                    contour) >= 3:
                return Shape(kind=ShapeKind.POLYGON,
                             rect=self._shape_bbox_rect(contour),
                             points=tuple(Point(float(x), float(y))
                                          for x, y in contour),
                             close_path=True, **common)
            if candidate.kind in ("rectangle", "circle") and len(
                    candidate.meta) == 5:
                cx, cy, ha, hb, deg = (float(v) for v in candidate.meta)
                if ha <= 0.0 or hb <= 0.0:
                    return None
                if force_polygon and candidate.kind == "rectangle" and len(
                        contour) >= 3:
                    return Shape(kind=ShapeKind.POLYGON,
                                 rect=self._shape_bbox_rect(contour),
                                 points=tuple(Point(float(x), float(y))
                                              for x, y in contour),
                                 close_path=True, **common)
                kind = (ShapeKind.RECTANGLE if candidate.kind == "rectangle"
                        else ShapeKind.ELLIPSE)
                return Shape(kind=kind,
                             rect=Rect(Point(cx - ha, cy - hb),
                                       Size(2.0 * ha, 2.0 * hb)),
                             rotation_deg=deg, **common)
        except Exception:
            return None
        return None

    def _shape_commit_rect(self, shape):
        """Widget-space paint bounds for a just-committed recognized shape."""
        try:
            extents = _shape_extent(shape)
            if extents is None:
                return None
            page_key = getattr(getattr(shape, "page_id", None), "value", None)
            mapped = self._page_origin_for(page_key)
            if mapped is None:
                return None
            ox, oy, factor = mapped
            return self._map_extents(extents, ox, oy, factor)
        except Exception:
            return None

    def _final_pen_annotation(self, done, live):
        """``(final_annotation, consumed_chain_ids)`` for a finished pen gesture.

        A completed hold-to-shape morph commits its recognized ``Shape``
        through the same history path as any other annotation; every other
        release commits the freehand stroke unchanged. When the morph came
        from an ink-chain merge, the ids of the committed sides it consumed
        are returned too, so the caller can replace them and the new shape
        with exactly one grouped undo step (M7.1.4.1).
        """
        try:
            state = self._shape_morph
            if (state is None or state.get("kind") != "pen"
                    or state.get("target") is not live):
                return done, ()
            consumed = tuple(state.get("chain_ids") or ())
            shape = self._shape_from_candidate(
                state.get("candidate"), done, force_polygon=bool(consumed))
            if shape is not None:
                return shape, consumed
        except Exception:
            pass
        return done, ()

    def _commit_pen_final(self, final, consumed) -> None:
        """Commit one pen release, atomically replacing a merged chain.

        A plain release commits one annotation as before. A chain merge
        commits the recognized polygon and removes the committed sides it
        consumed in *one* grouped undo step: one Undo restores every side
        and removes the polygon, one Redo re-applies the merge. All
        commands are constructed (and validated against the untouched
        store) before any is pushed; if that preflight fails, nothing has
        mutated and the polygon still commits on its own.
        """
        if not consumed:
            self._commit_annotation(final)
            return
        try:
            if not isinstance(final, Shape):
                raise ValueError("a merged chain must commit a shape")
            store = self._document.annotations
            history = self._history_for(self._document, create=True)
            page_index = self._page_index_for(final.page_id)
            if history is None or page_index is None:
                raise ValueError("no history or page for the merge")
            located = []
            for annotation_id in consumed:
                try:
                    _page, position = store.locate(annotation_id)
                except Exception:
                    # A vanished side is a survivor-only skip; the polygon
                    # still commits, simply without that deletion.
                    continue
                located.append((int(position), annotation_id))
            # Descending page position: earlier removals cannot shift the
            # positions captured by the remaining commands.
            located.sort(reverse=True)
            commands = [DeleteAnnotationCommand(store, annotation_id)
                        for _position, annotation_id in located]
            commands.append(AddAnnotationCommand(store, final, page_index))
        except Exception:
            self._commit_annotation(final)
            return
        history = self._history_for(self._document, create=True)
        if history is None:
            self._commit_annotation(final)
            return
        try:
            for command in commands:
                history.push_prebuilt_grouped(command, "Close Shape")
        except Exception:
            pass
        finally:
            history.end_group()
        self._sync_undo_hud()

    def _window_resize_edges(self, point):
        """Qt.Edges when a viewer-local ``point`` sits in the window grip.

        M7.0.1: the frameless shell owns its outer grip pixels and starts a
        native system resize from ``MainWindow.mousePressEvent`` — but the
        canvas accepts every press, so a press on the window's left, right
        or bottom edge never reached the window and a PDF session could no
        longer be resized by its edges (the library page's passive children
        propagate, so it could). The window's own ``_resize_edge_at`` stays
        the single source of truth for the grip width, the corner mapping
        and every control/scrollbar veto; this helper only reports the
        position, duck-typed, so the viewer gains no dependency on the
        window. Returns None when there is no such window or no grip here.
        """
        try:
            window = self.window()
            probe = getattr(window, "_resize_edge_at", None)
            if window is None or window is self or probe is None:
                return None
            return probe(window.mapFromGlobal(self.mapToGlobal(point)))
        except Exception:
            return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Route canvas presses to the interaction controller."""
        try:
            # M7.0.1: the window's outer resize grip wins over the canvas.
            # Ignoring lets Qt propagate the press to the parent window
            # (the same contract the library page's passive children
            # already honour), which starts the native system resize;
            # chrome keeps its pixels because the window's own veto walk
            # refuses the grip over buttons, fields and scrollbars.
            if (event.button() == Qt.MouseButton.LeftButton
                    and self._window_resize_edges(
                        event.position().toPoint()) is not None):
                event.ignore()
                return
            # B.2.2: a pen tap forwarded to a non-accepting chrome child
            # (e.g. a popup section label) propagates back here as a
            # synthetic mouse press. Real mouse presses on chrome never
            # reach the viewer (children own them), so anything hitting
            # chrome here is propagation — swallow it before any tool
            # path can open a stroke or pan from it.
            if self._tablet_chrome_target(
                    float(event.position().x()),
                    float(event.position().y())) is not None:
                event.accept()
                return
            # M6.8.2 clipboard slice: a press is a meaningful pointer
            # position too (Ctrl+V must not fall back to a stale hover).
            self._remember_pointer(
                float(event.position().x()), float(event.position().y()))
            # M6.8.2 Stage 5: a right-click is a context-menu request,
            # never the start of a drawing/selection/pan gesture. Swallow
            # it here so no tool (ink, eraser, shape, Selection/Lasso,
            # Pan, or a group move) can begin from it; the menu itself is
            # opened only from ``contextMenuEvent`` (which Qt delivers on
            # release on Windows and on press elsewhere) — that split is
            # what keeps one physical right-click from opening twice.
            if event.button() == Qt.MouseButton.RightButton:
                event.accept()
                return
            if self._is_eraser_active():
                # M6.5: same dual-stream protection as ink - drop a
                # mouse press while a tablet eraser owns the pointer.
                if (self._eraser_tablet_open
                        and not self._eraser_mouse_open):
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: a visible selection frame owns its
                # grips and its interior before the eraser sees the press,
                # so a context-menu selection stays editable under any tool.
                if self._frame_manipulation_press(
                        float(event.position().x()),
                        float(event.position().y()), "polygon"):
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is None:
                    event.accept()
                    return
                page_id, lx, ly = mapped
                # M6.8.2 Stage 5.2.2: the erase starts outside the frame, so
                # a stale context selection is dropped first and the erase
                # then runs normally on this same press.
                self._deselect_for_tool_press(
                    float(event.position().x()), float(event.position().y()))
                self._apply_intent(self._interaction.handle_eraser_press(
                    page_id, lx, ly, 1.0, self._pen_clock_ms()))
                self._eraser_mouse_open = True
                event.accept()
                return
            if self._ink_active():
                # M5-B.2.0.1 F2: drop a mouse press while a tablet
                # stroke owns the pointer (dual-stream protection).
                if self._tablet_stroke_open and not self._mouse_stroke_open:
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: the visible selection frame owns its
                # grips and its interior before ink starts, so a
                # context-menu selection stays editable under Pen while
                # drawing *outside* the frame is untouched.
                if self._frame_manipulation_press(
                        float(event.position().x()),
                        float(event.position().y()), "polygon"):
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is None:
                    event.accept()
                    return
                page_id, lx, ly = mapped
                # M6.8.2 Stage 5.2.2: ink starts outside the frame, so a
                # stale context selection is dropped first and the stroke
                # then starts normally on this same press.
                self._deselect_for_tool_press(
                    float(event.position().x()), float(event.position().y()))
                self._apply_intent(self._ink_press(
                    page_id, lx, ly, 1.0, self._pen_clock_ms()))
                self._mouse_stroke_open = True
                event.accept()
                return
            if self._is_shape_active():
                # M6.7: same dual-stream protection as ink/eraser.
                if self._shape_tablet_open and not self._shape_mouse_open:
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: the visible selection frame owns its
                # grips and its interior before a shape gesture starts.
                if self._frame_manipulation_press(
                        float(event.position().x()),
                        float(event.position().y()), "polygon"):
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is not None:
                    page_id, lx, ly = mapped
                    # M6.8.2 Stage 5.2.2: the shape gesture starts outside
                    # the frame, so a stale context selection is dropped
                    # first and the shape interaction then runs normally.
                    self._deselect_for_tool_press(
                        float(event.position().x()),
                        float(event.position().y()))
                    self._apply_intent(self._shape_press(
                        page_id, lx, ly, 1.0, self._pen_clock_ms()))
                event.accept()
                self._shape_mouse_open = self._shape_session_open()
                return
            if self._is_selection_active():
                # M6.8.1: press OPENS the marquee/lasso gesture (it is
                # committed on release, so a click can never select an
                # individual annotation). The same dual-stream guard as
                # ink/eraser/shape drops a synthesized mouse press while
                # a stylus gesture owns the pointer (the reverse
                # ordering is handled in ``tabletEvent``).
                if (self._selection_tablet_open
                        and not self._selection_mouse_open):
                    event.accept()
                    return
                kind = ("rect" if self._region_tool_kind() == "rect"
                        else "polygon")
                self._region_press(float(event.position().x()),
                                   float(event.position().y()), kind)
                event.accept()
                self._selection_mouse_open = True
                return
            button = _mouse_buttons_value(event.buttons())
            if self._interaction.active_kind == ToolType.PAN:
                # M6.8.2: a synthesized mouse press for a stylus gesture
                # (tablet stream owns the pointer) is dropped so the same
                # physical gesture cannot deselect/pan twice.
                if self._pan_tablet_open:
                    event.accept()
                    return
                # A fresh Pan press starts drag tracking and clears the
                # selection immediately if it lands outside the frame (no
                # deferred timer). The first tap of a double-click may
                # clear; the double-click handler still completes the new
                # selection. A mouse press also breaks any stylus
                # double-tap chain.
                wx = float(event.position().x())
                wy = float(event.position().y())
                self._pan_last_tap = None
                self._pan_tablet_dbl = False
                self._pan_press_point = (wx, wy)
                self._pan_dragged = False
                # M6.8.2 Stage 5.2.1: the visible selection frame owns the
                # press inside its oriented polygon - the grips first, then
                # the whole interior (empty space and the gaps between group
                # members included) - routed through the shared transform /
                # group-move state machine. This is the corrected Pan
                # behavior: dragging the frame interior moves the selection
                # instead of panning the viewport. Only a press *outside*
                # the frame reaches Pan's own navigation below (immediate
                # deselect, then drag).
                if self._frame_manipulation_press(wx, wy, "polygon"):
                    event.accept()
                    return
                self._pan_press_resolve(wx, wy)
            # M6.8.2 Stage 5.2.1: any remaining tool also yields to the
            # visible selection frame (grips, then interior) before its own
            # press handling.
            if self._frame_manipulation_press(
                    float(event.position().x()),
                    float(event.position().y()), "polygon"):
                event.accept()
                return
            # M6.8.2 Stage 5.2.2: the pointer is outside the frame and its
            # handles, so the tool's own press owns it - clear the stale
            # selection first, then run the tool exactly once.
            self._deselect_for_tool_press(
                float(event.position().x()), float(event.position().y()))
            button = _mouse_buttons_value(event.buttons())
            self._apply_intent(self._interaction.handle_press(
                float(event.position().x()), float(event.position().y()),
                buttons=button))
            event.accept()
            return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Finish an in-progress polygon with exactly one commit (M6.7).

        Qt routes the second click of a double-click here (not to
        :meth:`mousePressEvent`), so the first click has already placed
        one vertex; closing on the double-click adds no spurious vertex.

        M6.8.1/M6.8.2: while the Pan tool is active a double-click on an
        annotation selects it and shows the shared animated frame. The
        first click of the double-click already flowed through the Pan
        press routing, which clears the previous selection immediately if
        the press landed outside the frame — so this must still complete
        the new selection. Exactly one selection operation runs here. A
        synthesized mouse double-click for a stylus double-tap (handled in
        ``tabletEvent``) is dropped.
        """
        try:
            # M6.8.2 Stage 5: a right-button double-click is a context-menu
            # gesture, never a polygon finish / selection.
            if event.button() == Qt.MouseButton.RightButton:
                event.accept()
                return
            if self._is_shape_active() and self._shape_session_open():
                if self._shape_tablet_open and not self._shape_mouse_open:
                    event.accept()
                    return
                self._apply_intent(
                    self._interaction.handle_shape_double_click())
                event.accept()
                self._shape_mouse_open = self._shape_session_open()
                return
            if self._interaction.active_kind == ToolType.PAN:
                if self._pan_tablet_open or self._pan_tablet_dbl:
                    # A synthesized mouse double-click mirroring a stylus
                    # double-tap already handled in ``tabletEvent``.
                    self._pan_tablet_dbl = False
                    event.accept()
                    return
                self._pan_double_click_select(
                    float(event.position().x()),
                    float(event.position().y()))
                event.accept()
                return
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)

    # -- M6.8.2 Stage 5 selection context menu -----------------------------

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """Open the annotation or page context menu (M6.8.2 Stage 5.1).

        A right-click never starts a tool gesture (the mouse press/release
        paths drop the right button); it only requests a menu, and the
        menu is resolved from the *pointer target*, independent of the
        active tool:

        * a right-click on an annotation opens the annotation menu under
          every tool. A target that belongs to the current selection keeps
          the whole selection; an unselected target is selected with the
          established single-click semantics first. The active tool never
          changes.
        * a right-click inside a displayed page with no annotation under
          the pointer opens the page menu for *that* page (rotate/delete)
          — never the annotation menu of an unrelated selection. The page
          background is not the selection's surface, so the pointer's
          right-click clears the whole selection (from any page) and
          removes its frame, handles, and cursor state first; the menu
          itself stays inert until an action is chosen.
        * a right-click outside every page and every annotation opens
          nothing and mutates nothing (the selection is left alone).

        Exactly one menu can be owned at a time (Qt may deliver the event
        on both the press and release path on some platforms); the
        reference is dropped again the moment the menu hides. Opening or
        dismissing a menu touches no annotation, page, or history entry:
        only an accepted action mutates.
        """
        try:
            if self._document is None or self._pipeline is None:
                event.ignore()
                return
            if self._selection_menu is not None:
                event.accept()
                return
            # An in-flight gesture must not survive into the menu.
            self._move_cancel()
            if self._region_gesture is not None:
                self._region_overlay_clear()
            anchor = event.globalPos()
            menu = None
            if event.reason() == QContextMenuEvent.Reason.Keyboard:
                # A keyboard-invoked menu has no pointer invocation point:
                # Paste then uses the Ctrl+V placement rules.
                self._menu_anchor = None
                menu = self._keyboard_context_menu()
            else:
                wx, wy = self._context_menu_widget_pos(event)
                # M6.8.2 clipboard slice: the page-local invocation point
                # of *this* menu (page index + page-local pt), captured
                # before anything is built so Paste can anchor its group
                # there even after zoom/scroll. A point that is not on a
                # page yields None (keyboard-style placement).
                self._menu_anchor = self._pointer_page_pt(wx, wy)
                hit = self._annotation_at_widget(wx, wy)
                if hit is not None:
                    menu = self._annotation_context_menu(hit)
                else:
                    page_index = self._page_index_at_widget(wx, wy)
                    if page_index is None:
                        # Outside every page and every annotation: no menu,
                        # no side effect (the selection is left alone).
                        event.ignore()
                        return
                    # M6.8.2 Stage 5.2: the page *background* is not the
                    # selection's surface. A right-click there clears the
                    # whole selection — even one made on another page —
                    # and drops its frame, handles, and cursor state before
                    # the page menu is shown, so the menu can never be read
                    # as an action on a stale selection. Nothing is
                    # mutated: the menu itself is inert until an action is
                    # chosen.
                    self._clear_selection_state()
                    menu = self._build_page_menu(page_index)
            if menu is None:
                event.ignore()
                return
            self._selection_menu = menu
            try:
                menu.aboutToHide.connect(self._on_selection_menu_hidden)
                menu.popup(anchor)
            except Exception:
                # The menu could not be shown: release ownership so the
                # next request is not blocked by a stuck reference.
                self._selection_menu = None
                try:
                    menu.deleteLater()
                except Exception:
                    pass
                event.ignore()
                return
            try:
                from app import fx as _fx

                _fx.animate_menu_open(menu, 140, -6, self)
            except Exception:
                pass
            event.accept()
        except Exception:
            event.ignore()

    def _on_navigator_page_menu(self, page_index, global_pos) -> None:
        """Open the page context menu for a right-clicked All Pages card.

        The filmstrip hit-test guarantees a real page under the pointer
        (it never signals for empty space), so the target is exactly the
        right-clicked page — never the current selection. The menu is
        the SAME builder the canvas context menu uses
        (``_build_page_menu``), which closes every action over the given
        index, so Delete/Rotate/Insert/Paste route through the existing
        undoable handlers unchanged. Menu ownership mirrors the canvas
        flow: one reference, dropped again on hide.
        """
        try:
            if self._document is None or self._pipeline is None:
                return
            if self._selection_menu is not None:
                return
            # An in-flight gesture must not survive into the menu (same
            # hygiene as the canvas context menu), and the page
            # background is not the selection's surface: clear any
            # selection first so the page menu can never be read as an
            # action on a stale selection frame.
            self._move_cancel()
            if self._region_gesture is not None:
                self._region_overlay_clear()
            self._clear_selection_state()
            menu = self._build_page_menu(int(page_index))
            if menu is None:
                return
            # Paste anchors at the centre of the right-clicked page, in
            # page-local pt (the space that survives zoom/scroll), or
            # falls back to keyboard-style placement when the size is
            # unavailable.
            self._menu_anchor = None
            try:
                info = self._document.page_info(int(page_index))
                self._menu_anchor = (int(page_index),
                                     float(info.width) / 2.0,
                                     float(info.height) / 2.0)
            except Exception:
                pass
            self._selection_menu = menu
            try:
                menu.aboutToHide.connect(self._on_selection_menu_hidden)
                menu.popup(global_pos)
            except Exception:
                self._selection_menu = None
                try:
                    menu.deleteLater()
                except Exception:
                    pass
                return
            try:
                from app import fx as _fx

                _fx.animate_menu_open(menu, 140, -6, self)
            except Exception:
                pass
        except Exception:
            pass

    def _context_menu_widget_pos(self, event):
        """Widget-space position of a context-menu event (fallback safe)."""
        try:
            return (float(event.pos().x()), float(event.pos().y()))
        except Exception:
            local = self.mapFromGlobal(event.globalPos())
            return (float(local.x()), float(local.y()))

    def _keyboard_context_menu(self):
        """Resolve a keyboard menu request from the current context.

        A non-empty selection targets its annotations; otherwise the
        current page (at the viewport centre) gets the page menu. No
        pointer position is invented.
        """
        if self._selection.has_selection:
            ids = tuple(self._selection.selected_ids)
            snapshots = self._selection_target_snapshots(ids)
            menu = self._build_selection_menu(ids, snapshots)
            if menu is not None:
                return menu
        page_index = self._current_page_index()
        if page_index is None:
            return None
        return self._build_page_menu(page_index)

    def _annotation_context_menu(self, hit):
        """The selection menu for the annotation under the pointer.

        A hit that is already selected preserves the whole selection; an
        unselected hit becomes the selection first (single-click
        semantics). Identical to the Stage 5 annotation flow, now
        reachable from any tool.
        """
        if not self._selection.is_selected(hit.id):
            if self._selection.select(hit.id):
                self._selection_phase = 0.0
                self._sync_selection_animation()
                self.update()
        if not self._selection.has_selection:
            return None
        ids = tuple(self._selection.selected_ids)
        snapshots = self._selection_target_snapshots(ids)
        return self._build_selection_menu(ids, snapshots)

    def _page_index_at_widget(self, wx: float, wy: float):
        """Zero-based page index under a widget point, or None.

        Rejects gaps, the gutter, and anything outside a displayed page,
        so a right-click off every page opens nothing.
        """
        mapped = self._event_to_page_pt(wx, wy)
        if mapped is None:
            return None
        return self._page_index_for(mapped[0])

    def _pointer_page_pt(self, wx: float, wy: float):
        """``(page_index, page_local_x, page_local_y)`` or None.

        The page-local invocation point a context menu hands to Paste.
        Page-local pt (never widget px) is what survives zoom, scrolling,
        and a page-rotation change between menu open and action.
        """
        mapped = self._event_to_page_pt(wx, wy)
        if mapped is None:
            return None
        page_index = self._page_index_for(mapped[0])
        if page_index is None:
            return None
        return (int(page_index), float(mapped[1]), float(mapped[2]))

    def _remember_pointer(self, wx: float, wy: float) -> None:
        """Remember the last meaningful pointer position inside a page.

        Stored in page-local pt (the same space annotations live in), so
        the Ctrl+V placement fallback stays meaningful across zoom,
        scrolling, and page rotation. Off-page points (gutters, chrome)
        never replace the remembered position.
        """
        try:
            remembered = self._pointer_page_pt(float(wx), float(wy))
        except Exception:
            remembered = None
        if remembered is not None:
            self._last_pointer_pt = remembered

    def _current_page_index(self):
        """The page at the viewport centre (keyboard menu fallback)."""
        try:
            layout = self._pipeline.layout
            if layout.page_count <= 0:
                return None
            _vw, vh = self._render_viewport()
            mid = self._scroll_doc() + vh / 2.0
            pages = layout.pages_in(mid, mid + 1.0)
            if pages:
                return int(pages[0])
            return 0
        except Exception:
            return None

    def _build_page_menu(self, page_index):
        """Construct (but do not show) the page context menu (M6.8.2).

        Targets the page under the pointer only. Rotate Page is always
        available; Delete Page is disabled for the last remaining page
        (an empty document is invalid, so there is no blank-page
        substitute). Returns the ``QMenu`` or ``None``. A read-only
        session gets the header plus a read-only note only — every
        mutating entry (insert/rotate/paste/delete) is withheld, not
        merely disabled.
        """
        document = self._document
        if document is None:
            return None
        try:
            count = int(document.page_count)
        except Exception:
            return None
        if not 0 <= int(page_index) < count:
            return None
        try:
            from app.menu_material import InkfallMenu, add_submenu

            tokens = self._tool_tokens()
            menu = InkfallMenu(self, tokens)
            page_number = int(page_index) + 1
            header = menu.addAction(f"Page {page_number} of {count}")
            header.setEnabled(False)
            menu.addSeparator()
            if self._document_is_readonly():
                note = menu.addAction("Read-only session — actions disabled")
                note.setEnabled(False)
                return menu
            # Insertion targets the page under the pointer and captures
            # its geometry at menu-build time. A blank/template page is
            # generated at those displayed dimensions, so landscape and
            # rotated targets stay visually consistent.
            try:
                target_info = document.page_info(int(page_index))
            except Exception:
                target_info = None
            before = self._add_menu_action(
                menu, f"Insert Blank Page Before Page {page_number}",
                lambda: self._insert_blank_page(
                    int(page_index), True, target_info))
            after = self._add_menu_action(
                menu, f"Insert Blank Page After Page {page_number}",
                lambda: self._insert_blank_page(
                    int(page_index), False, target_info))
            try:
                from app.icons import icon

                ink = getattr(tokens, "text_primary", None) or "#111111"
                before.setIcon(icon("plus", ink, 16))
                after.setIcon(icon("plus", ink, 16))
            except Exception:
                pass
            menu.addSeparator()
            rotate = add_submenu(menu, "Rotate Page", tokens)
            cw = self._add_menu_action(
                rotate, "Rotate Clockwise 90°",
                lambda: self._rotate_page(page_index, 90,
                                          "Rotate Page Clockwise"))
            ccw = self._add_menu_action(
                rotate, "Rotate Counterclockwise 90°",
                lambda: self._rotate_page(page_index, -90,
                                          "Rotate Page Counterclockwise"))
            half = self._add_menu_action(
                rotate, "Rotate 180°",
                lambda: self._rotate_page(page_index, 180,
                                          "Rotate Page 180°"))
            try:
                from app.icons import icon

                ink = getattr(tokens, "text_primary", None) or "#111111"
                cw.setIcon(icon("rotate-cw", ink, 16))
                ccw.setIcon(icon("rotate-ccw", ink, 16))
                half.setIcon(icon("rotate-180", ink, 16))
            except Exception:
                pass
            menu.addSeparator()
            # M6.8.2 clipboard slice: paste onto this page, anchored at the
            # invocation point (captured at menu-build time, so a shown
            # menu can never use a later invocation's coordinates). The
            # action is disabled unless the system clipboard currently
            # holds a payload this build can paste.
            paste_anchor = self._menu_anchor
            if (paste_anchor is not None
                    and int(paste_anchor[0]) != int(page_index)):
                paste_anchor = None
            paste = self._add_menu_action(
                menu, "Paste",
                lambda: self._paste_clipboard(paste_anchor))
            try:
                from app.icons import icon

                paste.setIcon(icon("paste", tokens.text_primary, 16))
            except Exception:
                pass
            try:
                paste.setShortcut(QKeySequence(QKeySequence.StandardKey.Paste))
                paste.setShortcutContext(
                    Qt.ShortcutContext.WidgetShortcut)
            except Exception:
                pass
            paste.setEnabled(self._can_paste(page_index))
            menu.addSeparator()
            delete = self._add_menu_action(
                menu, f"Delete Page {int(page_index) + 1}",
                lambda: self._delete_page(page_index, "Delete Page"))
            try:
                from app.icons import icon

                danger = getattr(tokens, "danger", None) or "#D97A76"
                delete.setIcon(icon("trash", danger, 16))
            except Exception:
                pass
            if count <= 1:
                delete.setEnabled(False)
            return menu
        except Exception:
            return None

    # -- M6.8.2 Stage 5.1 page mutations ----------------------------------

    def _release_pipeline_for_page_op(self) -> None:
        """Release the render pipeline before a page edit.

        A page edit rewrites the document's working copy, so the old
        workers must drop their file handles first. Per-layout view caches
        are cleared so nothing stale survives into the rebuilt pipeline.
        """
        pipeline, self._pipeline = self._pipeline, None
        try:
            self._timer.stop()
        except Exception:
            pass
        if pipeline is not None:
            try:
                pipeline.close()
            except Exception:
                pass
        try:
            self._display.clear()
            self._wanted_prev.clear()
        except Exception:
            pass

    def _rebuild_after_page_op(self) -> None:
        """Rebuild the pipeline + view state after a page edit.

        The page count/geometry changed under every cached view artefact
        (frames, previews, live strokes, scroll ranges), so they are all
        dropped and the selection self-heals (a page deletion removes
        annotations; a rotation moves them). No annotation or history
        entry is touched here.
        """
        document = self._document
        if document is None:
            return
        try:
            pages = [document.page_info(i)
                     for i in range(document.page_count)]
            self._pipeline = DocumentPipeline(
                document.id, document.render_path, pages,
                ui_zoom=self._ui_zoom, user_rotation=0)
        except Exception:
            self._pipeline = None
            self._error = "Cannot rebuild the document view."
            self.update()
            return
        try:
            self._outline_cache = {}
            self._off_smooth_cache = {}
            self._bbox_memo = {}
            self._tablet_hover_pt = None
            self._pen_forward_target = None
            self._pen_last_tap = None
            self._tablet_stroke_open = False
            self._mouse_stroke_open = False
            self._live_stroke = None
            self._live_prev_rect = None
            self._live_full_rect = None
            self._live_meta = None
            self._shape_mouse_open = False
            self._shape_tablet_open = False
            self._live_shape = None
            self._live_shape_preview = None
            self._live_shape_snap = None
            self._live_shape_prev_rect = None
            self._live_shape_full_rect = None
            self._region_gesture = None
            self._region_prev_screen = None
            self._move_gesture = None
            self._move_preview_clear()
            self._pan_press_point = None
            self._pan_dragged = False
            # Page identity/geometry changed: a cached frame orientation
            # is no longer meaningful.
            self._frame_state = None
        except Exception:
            pass
        self._revalidate_selection()
        self._sync_selection_animation()
        self._sync_scrollbar(preserve_fraction=True)
        self._request_update()
        if self._pipeline is not None:
            try:
                self._timer.start()
            except Exception:
                pass
        self._sync_undo_hud()
        try:
            # Rotation / page insert / delete changed the page geometry:
            # refresh the count and recalculate an active fit mode once,
            # anchored so the current position does not jump.
            self._reapply_fit(anchor=True)
            self._sync_view_controls()
        except Exception:
            pass
        self.update()

    def _apply_page_rotation(self, page_index: int, degrees: int) -> None:
        """Effect callback for :class:`RotatePageCommand`."""
        document = self._document
        if document is None:
            return
        self._release_pipeline_for_page_op()
        try:
            document.rotate_page(int(page_index), int(degrees))
        finally:
            self._rebuild_after_page_op()

    def _apply_page_delete(self, page_index: int):
        """Effect callback for :class:`DeletePageCommand` (returns token)."""
        document = self._document
        if document is None:
            return None
        self._release_pipeline_for_page_op()
        try:
            return document.delete_page(int(page_index))
        finally:
            self._rebuild_after_page_op()

    def _apply_page_insert(self, removed) -> None:
        """Undo callback for :class:`DeletePageCommand`."""
        document = self._document
        if document is None or removed is None:
            return
        self._release_pipeline_for_page_op()
        try:
            document.insert_page(removed)
        finally:
            self._rebuild_after_page_op()

    def _apply_page_insert_blank(self, page_index, spec, width, height) -> None:
        """First redo callback for :class:`InsertPageCommand`."""
        document = self._document
        if document is None:
            return
        self._release_pipeline_for_page_op()
        try:
            document.insert_blank_page(
                int(page_index), spec, float(width), float(height))
        finally:
            self._rebuild_after_page_op()

    def _insert_blank_page(self, target_index, before, target_info=None) -> bool:
        """Push one undoable blank-page insertion around the target page.

        The inserted page uses the document's recovered ``PaperSpec``
        (or a plain fallback), the target page's displayed dimensions,
        and the same generated template path as New Blank pages. It
        becomes the visible page after the rebuild.
        """
        if self._document_is_readonly():
            self._refuse_readonly("insert pages")
            return False
        document = self._document
        if document is None:
            return False
        try:
            count = int(document.page_count)
            target = int(target_index)
            if not 0 <= target < count:
                return False
            if target_info is None:
                target_info = document.page_info(target)
            width = float(target_info.size.width)
            height = float(target_info.size.height)
            spec = document.paper_spec
            insert_at = target if before else target + 1
        except Exception:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        try:
            command = InsertPageCommand(
                document, insert_at,
                lambda index: self._apply_page_insert_blank(
                    index, spec, width, height),
                self._apply_page_delete,
                self._apply_page_insert,
                text="Insert Blank Page")
        except Exception:
            return False
        # Apply first so a failed working-copy write never leaves a
        # broken, unpushed-into entry on the undo stack.
        try:
            command.redo()
        except Exception:
            return False
        command.mark_pre_applied()
        try:
            history.push(command)
        except Exception:
            return False
        self._sync_undo_hud()
        try:
            self.goto_page(insert_at)
        except Exception:
            pass
        return True

    def _rotate_page(self, page_index, degrees, text) -> bool:
        """Push one undoable page rotation; False when nothing changed."""
        if self._document_is_readonly():
            self._refuse_readonly("rotate pages")
            return False
        document = self._document
        if document is None:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        try:
            command = RotatePageCommand(document, int(page_index),
                                        int(degrees),
                                        self._apply_page_rotation,
                                        text=text)
        except Exception:
            return False
        # Run the effect once *before* pushing: a failed working-copy write
        # rolls the document back (Document.rotate_page) and would otherwise
        # leave a broken, unpushed-into entry on the stack once Qt's
        # push-time redo() raised.
        try:
            command.redo()
        except Exception:
            return False
        command.mark_pre_applied()
        try:
            history.push(command)
        except Exception:
            return False
        self._sync_undo_hud()
        return True

    def _delete_page(self, page_index, text) -> bool:
        """Push one undoable page deletion (never the last page)."""
        if self._document_is_readonly():
            self._refuse_readonly("delete pages")
            return False
        document = self._document
        if document is None:
            return False
        try:
            if int(document.page_count) <= 1:
                return False
        except Exception:
            return False
        history = self._history_for(document, create=True)
        if history is None:
            return False
        try:
            command = DeletePageCommand(document, int(page_index),
                                        self._apply_page_delete,
                                        self._apply_page_insert, text=text)
        except Exception:
            return False
        # See _rotate_page: apply first so a failed write cannot leave a
        # broken entry on the undo stack.
        try:
            command.redo()
        except Exception:
            return False
        command.mark_pre_applied()
        try:
            history.push(command)
        except Exception:
            return False
        self._sync_undo_hud()
        return True

    def _on_selection_menu_hidden(self) -> None:
        """Drop the shown menu reference once it hides (idempotent).

        The ``QMenu`` is *not* deleted here: the shared entrance animation
        (``app.fx.animate_menu_open``) keeps a reference to it in its
        ``finished`` callback, exactly as the tool shelf's menu does, so
        the widget is left parented for Qt to reclaim with the viewer
        rather than freed out from under a still-running animation.
        """
        self._selection_menu = None
        # M7.1.4.2: dismissing the context menu can return the pointer to
        # the canvas without a widget-boundary ``Enter``; re-derive the
        # truthful cursor now (the same one-shot used on activation).
        self._restore_canvas_cursor_under_pointer()

    def _selection_target_snapshots(self, ids) -> dict:
        """``{annotation_id: current_store_object}`` for a menu snapshot.

        Missing/unreadable entries map to ``None`` (treated as vanished
        by the target-validation policy). Pure read; never mutates.
        """
        document = self._document
        if document is None:
            return {}
        try:
            store = document.annotations
        except Exception:
            return {}
        out: dict = {}
        for annotation_id in ids:
            try:
                out[annotation_id] = store.get(annotation_id)
            except Exception:
                out[annotation_id] = None
        return out

    @staticmethod
    def _add_menu_action(menu, text, slot):
        """Add one text action wired to ``slot``; returns the action."""
        action = menu.addAction(text)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_selection_menu(self, ids, snapshots):
        """Construct (but do not show) the selection context menu.

        Only actions that apply to the snapshot are added: Rotate appears
        when at least one target is transformable, Fill Color appears only
        when at least one target is a fill-capable shape, and Paste is
        enabled only while the system clipboard holds a valid payload this
        build can paste. Cut/Copy/Paste are the *same* handlers Ctrl+X /
        Ctrl+C / Ctrl+V use, and the paste anchor is captured now (at
        menu-build time) rather than read when the action fires. Returns
        the ``QMenu`` or ``None`` if it cannot be built.
        """
        try:
            from app.menu_material import InkfallMenu, add_submenu

            tokens = self._tool_tokens()
            menu = InkfallMenu(self, tokens)
            # Clipboard slice: a closure over a *local* anchor snapshot, so
            # a later menu invocation can never redirect this menu's Paste.
            paste_anchor = self._menu_anchor
            try:
                paste_page = (int(paste_anchor[0])
                              if paste_anchor is not None else None)
            except (TypeError, ValueError):
                paste_page = None
            transformable = any(
                self._annotation_transformable(snapshots.get(i))
                for i in ids)
            stroke_ok = any(supports_stroke_color(snapshots.get(i))
                            for i in ids)
            fill_ok = any(supports_fill_color(snapshots.get(i))
                          for i in ids)
            if transformable:
                rotate = add_submenu(menu, "Rotate", tokens)
                left = self._add_menu_action(
                    rotate, "Rotate Left 90°",
                    lambda: self._rotate_selection(ids, snapshots, -90.0,
                                                   "Rotate Left"))
                right = self._add_menu_action(
                    rotate, "Rotate Right 90°",
                    lambda: self._rotate_selection(ids, snapshots, 90.0,
                                                   "Rotate Right"))
                half = self._add_menu_action(
                    rotate, "Rotate 180°",
                    lambda: self._rotate_selection(ids, snapshots, 180.0,
                                                   "Rotate 180°"))
                try:
                    from app.icons import icon

                    ink = getattr(tokens, "text_primary", None) or "#111111"
                    left.setIcon(icon("rotate-ccw", ink, 16))
                    right.setIcon(icon("rotate-cw", ink, 16))
                    half.setIcon(icon("rotate-180", ink, 16))
                except Exception:
                    pass
            if stroke_ok or fill_ok:
                recolor = add_submenu(menu, "Recolor", tokens)
                if stroke_ok:
                    self._populate_color_menu(
                        add_submenu(recolor, "Stroke Color", tokens), ids,
                        snapshots, "stroke")
                if fill_ok:
                    self._populate_color_menu(
                        add_submenu(recolor, "Fill Color", tokens), ids,
                        snapshots, "fill")
                    recolor.addSeparator()
                    self._add_fill_opacity_action(
                        recolor, ids, snapshots)
            menu.addSeparator()
            # M6.8.2 clipboard slice: Cut / Copy / Paste. Copy never
            # mutates anything; Cut is Copy + the same grouped delete
            # Delete uses; Paste inserts new copies and selects them.
            # Unavailable commands are disabled rather than hidden so the
            # menu keeps a stable shape.
            cut = self._add_menu_action(menu, "Cut", self._cut_selection)
            copy = self._add_menu_action(menu, "Copy", self._copy_selection)
            paste = self._add_menu_action(
                menu, "Paste", lambda: self._paste_clipboard(paste_anchor))
            try:
                from app.icons import icon

                ink = getattr(tokens, "text_primary", None) or "#111111"
                cut.setIcon(icon("cut", ink, 16))
                copy.setIcon(icon("copy", ink, 16))
                paste.setIcon(icon("paste", ink, 16))
            except Exception:
                pass
            try:
                cut.setShortcut(QKeySequence(QKeySequence.StandardKey.Cut))
                copy.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
                paste.setShortcut(QKeySequence(QKeySequence.StandardKey.Paste))
                for action in (cut, copy, paste):
                    action.setShortcutContext(
                        Qt.ShortcutContext.WidgetShortcut)
            except Exception:
                pass
            paste.setEnabled(self._can_paste(paste_page))
            menu.addSeparator()
            delete = self._add_menu_action(
                menu, "Delete",
                lambda: self._delete_targets(ids, snapshots,
                                             "Delete Annotations"))
            # Read-only affordances so Delete reads as destructive without
            # shouting: the existing trash glyph tinted with the theme's
            # danger tone, and a Delete key hint.
            try:
                from app.icons import icon

                danger = getattr(tokens, "danger", None) or "#D97A76"
                delete.setIcon(icon("trash", danger, 16))
            except Exception:
                pass
            try:
                # ``QKeySequence`` is imported once at module scope; a local
                # import here would shadow it for the whole function and
                # break the Cut/Copy/Paste shortcut hints above.
                delete.setShortcut(QKeySequence(Qt.Key.Key_Delete))
                delete.setShortcutContext(
                    Qt.ShortcutContext.WidgetShortcut)
            except Exception:
                pass
            return menu
        except Exception:
            return None

    def _populate_color_menu(self, menu, ids, snapshots, which) -> None:
        """Fill a Stroke/Fill submenu with palette swatches + Custom…."""
        try:
            from app.toolshelf.store import get_color_presets

            presets = list(get_color_presets())
        except Exception:
            presets = []
        for color in presets:
            action = menu.addAction(self._color_swatch_icon(color),
                                    str(color))
            action.triggered.connect(
                lambda _checked=False, col=color: self._apply_recolor_choice(
                    ids, snapshots, which, col))
        menu.addSeparator()
        custom = menu.addAction("Custom…")
        custom.triggered.connect(
            lambda _checked=False: self._recolor_custom(ids, snapshots,
                                                        which))

    def _add_fill_opacity_action(self, menu, ids, snapshots) -> None:
        """Append the Fill Opacity slider row (M7.1.4.1).

        Live drag updates are applied in place without history so the fill
        follows the slider immediately. On release the originals are put
        back and one grouped ``ReplaceAnnotationCommand`` per changed target
        is pushed, so a single Undo restores every target's original fill
        color, fill opacity and fill-enabled state and a single Redo
        re-applies the final states. A drag that ends on the original state
        opens no history entry.
        """
        try:
            from PySide6.QtWidgets import (
                QHBoxLayout,
                QLabel,
                QSlider,
                QWidget,
                QWidgetAction,
            )
        except Exception:
            return
        document = self._document
        try:
            store = document.annotations
        except Exception:
            return
        if store is None:
            return
        targets = [snapshots.get(i) for i in ids
                   if supports_fill_color(snapshots.get(i))]
        if not targets:
            return
        seed = 100
        for snapshot in targets:
            try:
                seed = int(round(
                    max(0.0, min(1.0, float(snapshot.fill_opacity))) * 100.0))
                break
            except Exception:
                continue
        state = {"originals": None, "finals": {}, "finished": False}

        try:
            action = QWidgetAction(menu)
            container = QWidget(menu)
            row = QHBoxLayout(container)
            try:
                row.setContentsMargins(10, 2, 10, 2)
                row.setSpacing(8)
            except Exception:
                pass
            title = QLabel("Fill Opacity", container)
            slider = QSlider(Qt.Orientation.Horizontal, container)
            slider.setRange(0, 100)
            slider.setValue(max(0, min(100, seed)))
            slider.setMinimumWidth(110)
            readout = QLabel(f"{slider.value()}%", container)
            readout.setMinimumWidth(38)
            row.addWidget(title)
            row.addWidget(slider)
            row.addWidget(readout)
            action.setDefaultWidget(container)
            menu.addAction(action)
        except Exception:
            return

        def capture_originals():
            """Snapshot fillable targets once, before the first live edit."""
            if state["originals"] is not None:
                return state["originals"]
            originals = {}
            for annotation_id in ids:
                snapshot = snapshots.get(annotation_id)
                if not supports_fill_color(snapshot):
                    continue
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None or (
                        snapshot is not None and current is not snapshot):
                    # Vanished or replaced under the menu: this control
                    # aborts rather than edit an unexpected annotation.
                    state["originals"] = {}
                    return state["originals"]
                try:
                    page_index, position = store.locate(annotation_id)
                except Exception:
                    state["originals"] = {}
                    return state["originals"]
                originals[annotation_id] = (current, page_index, position)
            state["originals"] = originals
            return originals

        def apply_live(percent):
            readout.setText(f"{percent}%")
            if state["finished"]:
                return
            originals = capture_originals()
            if not originals:
                return
            opacity = max(0.0, min(1.0, float(percent) / 100.0))
            for annotation_id in originals:
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None:
                    continue  # vanished: survivor-only skip
                base = state["finals"].get(annotation_id) or current
                try:
                    candidate = dataclasses.replace(
                        base, fill_opacity=opacity, fill_enabled=True)
                except Exception:
                    continue
                try:
                    store.replace(annotation_id, [candidate])
                except Exception:
                    continue
                state["finals"][annotation_id] = candidate
            self.update()

        def finish_drag():
            if state["finished"]:
                return
            state["finished"] = True
            originals = state["originals"] or {}
            changed = []
            restore = []
            for annotation_id, (original, _page, _position) in (
                    originals.items()):
                final = state["finals"].get(annotation_id)
                if final is None:
                    continue
                try:
                    current = store.get(annotation_id)
                except Exception:
                    current = None
                if current is None or current is not final:
                    # Vanished or superseded by another action while the
                    # control was live: leave it untouched, record nothing.
                    continue
                restore.append((annotation_id, original))
                try:
                    opacity_changed = (float(getattr(
                        original, "fill_opacity", 1.0))
                        != float(getattr(final, "fill_opacity", 1.0)))
                    enabled_changed = (bool(getattr(
                        original, "fill_enabled", False))
                        != bool(getattr(final, "fill_enabled", False)))
                except Exception:
                    continue
                if opacity_changed or enabled_changed:
                    changed.append((annotation_id, final))
            # Put every original back so the replacement commands capture
            # the pre-drag state, not the last live preview.
            for annotation_id, original in restore:
                try:
                    store.replace(annotation_id, [original])
                except Exception:
                    pass
            if not changed:
                self.update()
                return
            commands = []
            try:
                for annotation_id, final in changed:
                    commands.append(ReplaceAnnotationCommand(
                        store, annotation_id, [final], text="Fill Opacity"))
            except Exception:
                commands = []
            history = self._history_for(document, create=True)
            if not commands or history is None:
                self.update()
                return
            try:
                for command in commands:
                    history.push_prebuilt_grouped(
                        command, text="Fill Opacity")
            finally:
                history.end_group()
                self._sync_undo_hud()
                self._revalidate_selection()
                self._sync_selection_animation()
                self.update()

        slider.valueChanged.connect(apply_live)
        slider.sliderReleased.connect(finish_drag)
        try:
            menu.aboutToHide.connect(finish_drag)
        except Exception:
            pass

    def _apply_recolor_choice(self, ids, snapshots, which, color) -> None:
        if which == "fill":
            self._recolor_targets(ids, snapshots, fill=color)
        else:
            self._recolor_targets(ids, snapshots, stroke=color)

    def _recolor_custom(self, ids, snapshots, which) -> None:
        """Open the color picker seeded with the selection's current tone."""
        try:
            from PySide6.QtGui import QColor
            from PySide6.QtWidgets import QColorDialog

            initial = self._recolor_initial_color(ids, snapshots, which)
            chosen = QColorDialog.getColor(
                QColor(initial), self, "Choose Color")
            if not chosen.isValid():
                return
            self._apply_recolor_choice(ids, snapshots, which, chosen.name())
        except Exception:
            pass

    @staticmethod
    def _recolor_initial_color(ids, snapshots, which) -> str:
        """Current tone of the first applicable member (picker seed)."""
        for annotation_id in ids:
            annotation = snapshots.get(annotation_id)
            if which == "fill":
                if supports_fill_color(annotation):
                    return str(getattr(annotation, "fill_color", "#ffffff"))
            elif supports_stroke_color(annotation):
                return str(getattr(annotation, "color", "#000000"))
        return "#000000"

    def _color_swatch_icon(self, color, size: int = 15):
        """A small rounded color chip for a palette action."""
        try:
            from PySide6.QtGui import QIcon, QPainter, QPixmap

            pixmap = QPixmap(int(size), int(size))
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                edge = QColor(str(getattr(self._tool_tokens(), "border",
                                          "#888888")))
                if not edge.isValid():
                    edge = QColor("#888888")
                fill = QColor(str(color))
                if not fill.isValid():
                    fill = QColor("#000000")
                painter.setPen(QPen(edge, 1.0))
                painter.setBrush(fill)
                painter.drawRoundedRect(1.0, 1.0, size - 2.0, size - 2.0,
                                        3.0, 3.0)
            finally:
                painter.end()
            return QIcon(pixmap)
        except Exception:
            from PySide6.QtGui import QIcon

            return QIcon()

    @staticmethod
    def _annotation_transformable(annotation) -> bool:
        """True when the group transform can express a command on it."""
        if isinstance(annotation, (InkStroke, Highlighter)):
            return bool(getattr(annotation, "points", ()))
        if isinstance(annotation, Shape):
            kind = getattr(annotation, "kind", None)
            if kind in (ShapeKind.LINE, ShapeKind.ARROW,
                        ShapeKind.POLYGON):
                return bool(getattr(annotation, "points", ()))
            if kind in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE):
                rect = getattr(annotation, "rect", None)
                try:
                    return (rect is not None
                            and float(rect.width) > 0.0
                            and float(rect.height) > 0.0)
                except Exception:
                    return False
        return False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Route canvas drags (pan) to the interaction controller."""
        try:
            # M6.8.2 de-dup: with tablet tracking on, Qt may synthesize a
            # button-free mouse move for a stylus hover that was already
            # handled as a TabletMove. Drop that one mirrored move (same
            # position) so the polygon live preview updates exactly once.
            marker = self._tablet_hover_pt
            self._tablet_hover_pt = None
            if (marker is not None
                    and event.buttons() == Qt.MouseButton.NoButton
                    and abs(float(event.position().x()) - marker[0]) <= 1.0
                    and abs(float(event.position().y()) - marker[1]) <= 1.0):
                event.accept()
                return
            # M6.8.2 Stage 5: a right-button drag never pans/draws/extends
            # a gesture (only a left-button drag does). Hover moves carry
            # NoButton and are unaffected.
            if (event.buttons() & Qt.MouseButton.RightButton
                    and not (event.buttons() & Qt.MouseButton.LeftButton)):
                event.accept()
                return
            # M6.8.2 Stage 5.2: hover/drag cursor feedback for the selection
            # controls, evaluated *before* the tool routing (the Selection
            # tool swallows button-free moves, so its own branch would never
            # see a hover). Non-selection positions resolve to the active
            # tool's own cursor.
            self._remember_pointer(
                float(event.position().x()), float(event.position().y()))
            # M7.0.1: hovering the window's resize grip drops the
            # viewer's own tool cursor so the window-inherited resize
            # cursor (set by the shell's move observer) becomes visible;
            # the next move off the grip re-derives it. M7.1.4.4: the
            # derivation lives in the shared helper so the stylus path
            # matches this one exactly.
            self._derive_pointer_cursor(
                float(event.position().x()), float(event.position().y()),
                dragging=bool(event.buttons()
                              & Qt.MouseButton.LeftButton))
            # M6.8.2 Stage 5.2.1: an armed selection-frame gesture (move,
            # resize, rotate) owns the pointer for *every* tool, so it is
            # routed before the active tool's ordinary drag handling - a
            # frame move armed under Pen must never turn into a stroke, and
            # one armed under Pan must never turn into a viewport pan.
            if self._move_gesture is not None:
                self._move_move(
                    float(event.position().x()),
                    float(event.position().y()),
                    shift=bool(event.modifiers()
                               & Qt.KeyboardModifier.ShiftModifier))
                event.accept()
                return
            if self._is_selection_active():
                # M6.8.1: the open marquee/lasso gesture owns the
                # pointer; moves extend it (across pages and gaps) and
                # paint the live overlay. Without an open gesture the
                # move is swallowed so a stray drag can never pan the
                # canvas or draw anything.
                if (self._selection_tablet_open
                        and not self._selection_mouse_open):
                    event.accept()
                    return
                self._region_move(
                    float(event.position().x()),
                    float(event.position().y()),
                    shift=bool(event.modifiers()
                               & Qt.KeyboardModifier.ShiftModifier))
                event.accept()
                return
            if self._is_shape_active() and self._shape_session_open():
                # M6.7: a shape gesture owns the pointer. Handled for
                # both button-down drags and button-free polygon moves
                # (the rubber-band edge follows the pointer).
                if self._shape_tablet_open and not self._shape_mouse_open:
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is not None:
                    page_id, lx, ly = mapped
                    self._apply_intent(self._shape_move(
                        page_id, lx, ly, 1.0, self._pen_clock_ms()))
                event.accept()
                self._shape_mouse_open = self._shape_session_open()
                return
            if event.buttons() != Qt.MouseButton.NoButton:
                if self._is_eraser_active():
                    if (self._eraser_tablet_open
                            and not self._eraser_mouse_open):
                        event.accept()
                        return
                    mapped = self._event_to_page_pt(
                        float(event.position().x()),
                        float(event.position().y()))
                    # Points outside pages are skipped; the gesture keeps
                    # its in-page sub-path.
                    if mapped is not None:
                        page_id, lx, ly = mapped
                        self._apply_intent(
                            self._interaction.handle_eraser_move(
                                page_id, lx, ly, 1.0, self._pen_clock_ms()))
                    event.accept()
                    return
                if self._ink_active():
                    if (self._tablet_stroke_open
                            and not self._mouse_stroke_open):
                        event.accept()
                        return
                    mapped = self._event_to_page_pt(
                        float(event.position().x()),
                        float(event.position().y()))
                    # Points outside pages are skipped; the stroke keeps
                    # its earlier in-page points (still bound to one page).
                    if mapped is not None:
                        page_id, lx, ly = mapped
                        self._apply_intent(self._ink_move(
                            page_id, lx, ly, 1.0, self._pen_clock_ms()))
                    event.accept()
                    return
                button = _mouse_buttons_value(event.buttons())
                if self._interaction.active_kind == ToolType.PAN:
                    # M6.8.2 corrective: a selected-object move gesture
                    # owns the pointer; route it instead of panning.
                    if self._move_gesture is not None:
                        self._move_move(float(event.position().x()),
                                        float(event.position().y()))
                        event.accept()
                        return
                    # M6.8.1 follow-up: any Pan drag past the movement
                    # threshold marks the gesture as a drag, so its
                    # release never resolves to a selection-clearing click.
                    self._note_pan_move(float(event.position().x()),
                                        float(event.position().y()))
                self._apply_intent(self._interaction.handle_move(
                    float(event.position().x()), float(event.position().y()),
                    buttons=button))
                event.accept()
                return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """End the active gesture."""
        try:
            # M6.8.2 Stage 5: a right-button release closes the context
            # menu gesture; it never finalizes a drawing/move gesture.
            if event.button() == Qt.MouseButton.RightButton:
                event.accept()
                return
            # M6.8.2 Stage 5.2.1: an armed selection-frame gesture closes
            # through its own state machine for *every* tool - a frame move
            # armed under Pen must not be finalized as an empty stroke, and
            # one armed under Pan must not be finalized as a viewport pan.
            if self._move_gesture is not None:
                self._move_release(float(event.position().x()),
                                   float(event.position().y()))
                self._pan_press_point = None
                self._pan_dragged = False
                self._refresh_tool_cursor()
                event.accept()
                return
            if self._is_eraser_active():
                if (self._eraser_tablet_open
                        and not self._eraser_mouse_open):
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is not None:
                    page_id, lx, ly = mapped
                else:
                    # Released outside pages: finalize with the sub-path
                    # captured so far (None page appends no segment).
                    page_id, lx, ly = None, 0.0, 0.0
                self._apply_intent(self._interaction.handle_eraser_release(
                    page_id, lx, ly, 1.0, self._pen_clock_ms()))
                event.accept()
                return
            if self._ink_active():
                if (self._tablet_stroke_open
                        and not self._mouse_stroke_open):
                    # A tablet stroke is open: this release belongs to
                    # the other stream — never finalize on it.
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is not None:
                    page_id, lx, ly = mapped
                else:
                    # Released outside pages: finalize with the points
                    # captured so far (None page appends nothing).
                    page_id, lx, ly = None, 0.0, 0.0
                self._apply_intent(self._ink_release(
                    page_id, lx, ly, 1.0, self._pen_clock_ms()))
                event.accept()
                return
            if self._is_shape_active():
                if self._shape_tablet_open and not self._shape_mouse_open:
                    event.accept()
                    return
                mapped = self._event_to_page_pt(
                    float(event.position().x()), float(event.position().y()))
                if mapped is not None:
                    page_id, lx, ly = mapped
                else:
                    # Released outside pages: finalize with the last
                    # in-page point (None page adds nothing). A polygon
                    # stays open across releases.
                    page_id, lx, ly = None, 0.0, 0.0
                self._apply_intent(self._shape_release(
                    page_id, lx, ly, 1.0, self._pen_clock_ms()))
                event.accept()
                self._shape_mouse_open = self._shape_session_open()
                return
            if self._is_selection_active():
                # M6.8.1: release CLOSES and commits the gesture. A
                # release belonging to the other stream (a stylus
                # gesture still open) is swallowed, never treated as
                # this stream's release.
                if (self._selection_tablet_open
                        and not self._selection_mouse_open):
                    event.accept()
                    return
                self._selection_mouse_open = False
                self._region_release(float(event.position().x()),
                                     float(event.position().y()))
                event.accept()
                return
            if self._interaction.active_kind == ToolType.PAN:
                # M6.8.2 corrective: close a selected-object move gesture
                # through its own state machine (no viewport pan finalize).
                if self._move_gesture is not None:
                    wx = float(event.position().x())
                    wy = float(event.position().y())
                    self._move_release(wx, wy)
                    self._pan_press_point = None
                    self._pan_dragged = False
                    event.accept()
                    return
                # M6.8.2: Pan deselect happens on press (outside the frame)
                # or when a drag crosses the threshold - never on release,
                # so there is no deferred timer and nothing to schedule.
                # The release only closes the pan stream. A synthesized
                # mouse release for a stylus gesture is dropped so the same
                # physical gesture is processed once.
                if self._pan_tablet_open:
                    event.accept()
                    return
                wx = float(event.position().x())
                wy = float(event.position().y())
                self._apply_intent(self._interaction.handle_release(wx, wy))
                self._pan_press_point = None
                self._pan_dragged = False
                # Tool cursors: the drag ended — the open hand is restored at once.
                self._refresh_tool_cursor()
                event.accept()
                return
            self._apply_intent(self._interaction.handle_release(
                float(event.position().x()), float(event.position().y())))
            event.accept()
            return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def tabletEvent(self, event) -> None:  # noqa: N802
        """Route stylus input with pressure (M5-A1 foundation).

        M7.1.4.2: pressure is forwarded as a normalized ``float`` or as
        ``None`` when the event carries no usable measurement. A genuine
        numeric zero survives as ``0.0`` (a valid sensor value); only
        unreadable/non-finite readings become ``None`` so the highlighter
        can reconcile them against neighbouring valid samples instead of
        turning the stroke start into full pressure.
        """
        try:
            pressure = 1.0
            try:
                reading = float(event.pressure())
            except Exception:
                reading = None
            if reading is not None:
                if math.isfinite(reading):
                    pressure = max(0.0, min(1.0, reading))
                else:
                    pressure = None
            x = float(event.position().x())
            y = float(event.position().y())
            etype = event.type()
            from PySide6.QtCore import QEvent

            tablet_types = (QEvent.Type.TabletPress, QEvent.Type.TabletMove,
                            QEvent.Type.TabletRelease)
            # Proximity events are sent only to QApplication (Qt documents
            # TabletEnter/LeaveProximity as application-level); a widget's
            # tabletEvent never receives them, so cursor recovery cannot
            # rely on them. The stylus stream instead re-applies its
            # cursor on every press/move/release (see below), and
            # hovering chrome applies the chrome widget's own cursor.
            # M6.8.2 Stage 5.2 corrective: a right-click is a context-menu
            # request, never tool input — including a stylus barrel-button
            # click, which arrives as a *tablet* event. Without this guard
            # the pen/highlighter/shape/eraser paths would start a stroke
            # from the barrel press (while the context menu opens at the
            # same time), so right-click-under-every-tool was only reliable
            # with a mouse. Any event carrying the right button is dropped
            # before any tool routing; the menu itself still comes from
            # ``contextMenuEvent``.
            try:
                _right = (event.button() == Qt.MouseButton.RightButton
                          or bool(event.buttons()
                                  & Qt.MouseButton.RightButton))
            except Exception:
                _right = False
            if _right:
                event.accept()
                return
            # M6.8.2 de-dup marker lifetime: a press/release ends the hover
            # it may have preceded, so the next mouse move is only ever
            # mirrored from a TabletMove.
            if etype != QEvent.Type.TabletMove:
                self._tablet_hover_pt = None
            # M5-B.2.0.1 F1: toolbar chrome owns pen taps that land on
            # it. Pairing follows the press target for the whole tap
            # (mouse-grab semantics); canvas never sees these events.
            now_ms = self._event_time_ms(event)
            try:
                pen_modifiers = event.modifiers()
            except Exception:
                pen_modifiers = Qt.KeyboardModifier.NoModifier
            # M7.1.4.2.1 cursor ownership: the stylus stream re-applies
            # its cursor on every press/move/release, unconditionally.
            # Qt synthesises no Enter/Leave for a pen and delivers
            # proximity only to the application object (a widget's
            # tabletEvent never sees it), so a window transition —
            # desktop, another application window, shell/IME surfaces —
            # or a digitizer range change can drop the applied cursor
            # with no event reaching this widget. ``_hover_cursor_key``
            # therefore records only what was *requested*, never what
            # the OS is displaying, and a matching key must not
            # suppress a re-apply anywhere in this stream. Re-applying
            # is cheap by design: pixmaps come from ``app.cursors``'
            # bounded caches (no rebuild per move) and an unchanged
            # ``setCursor`` is a platform no-op. The mouse path keeps
            # its flicker-free keyed contract untouched.
            self._hover_cursor_key = None
            if etype in tablet_types:
                if etype == QEvent.Type.TabletPress:
                    target = self._tablet_chrome_target(x, y)
                    self._pen_forward_target = target
                    if target is not None:
                        # The press may be the session's first event (no
                        # hover move): the widget's own cursor is still
                        # the truthful one while the pen is over it.
                        self._apply_chrome_cursor(target)
                    # M5-B.2.0.2: qualifying second tap on the same
                    # target becomes MouseButtonDblClick instead of a
                    # second press — the only sequence Qt routes to
                    # mouseDoubleClickEvent (popup opens exactly once,
                    # no duplicate selection).
                    mouse_kind = QEvent.Type.MouseButtonPress
                    if target is not None and self._pen_last_tap is not None:
                        old = self._pen_last_tap
                        try:
                            interval = QApplication.doubleClickInterval()
                            span = (abs(x - old[2]) + abs(y - old[3]))
                            if (old[0] is target
                                    and 0 <= now_ms - old[1] <= interval
                                    and span <= QApplication.startDragDistance()):
                                mouse_kind = QEvent.Type.MouseButtonDblClick
                                self._pen_last_tap = None
                            else:
                                self._pen_last_tap = (target, now_ms, x, y)
                        except Exception:
                            self._pen_last_tap = (target, now_ms, x, y)
                    elif target is not None:
                        self._pen_last_tap = (target, now_ms, x, y)
                    if target is not None:
                        if not self._forward_pen_to_child(
                                target, etype, x, y, mouse_kind,
                                pen_modifiers):
                            self._pen_forward_target = None
                            self._pen_last_tap = None
                        elif etype == QEvent.Type.TabletRelease:
                            self._pen_forward_target = None
                        event.accept()
                        return
                    # Canvas press breaks any pending double-tap chain,
                    # exactly like an intervening mouse press elsewhere.
                    self._pen_last_tap = None
                    # A press-only session (a tablet that reports no
                    # hover) still needs a truthful cursor at contact.
                    try:
                        self._derive_pointer_cursor(x, y, dragging=True)
                    except Exception:
                        pass
                else:
                    target = self._pen_forward_target
                    if target is not None:
                        if not self._forward_pen_to_child(
                                target, etype, x, y,
                                modifiers=pen_modifiers):
                            self._pen_forward_target = None
                        elif etype == QEvent.Type.TabletRelease:
                            self._pen_forward_target = None
                        event.accept()
                        return
                    # move/release without a chrome press: canvas path
            # A canvas pen tap outside an open popup dismisses it, exactly
            # like a mouse press (whose host filter the tablet path must
            # replicate explicitly). Because the router accepts tablet
            # presses, the panels' app-level filters only ever see
            # ``MouseButtonPress``, so this is the one place that owns
            # stylus dismissal; adding a TabletPress branch to those
            # filters would duplicate an unreachable rule.
            if etype == QEvent.Type.TabletPress:
                try:
                    popup = self._tool_popup
                    if (popup is not None and popup.isVisible()
                            and not popup.geometry().contains(
                                QPoint(int(x), int(y)))):
                        self._close_tool_popup()
                except Exception:
                    pass
                # The view-controls panel follows the same outside-press
                # rule (its own filter is mouse-only for the reason
                # above). Taps inside it never reach here: they are
                # forwarded to the panel subtree as chrome input.
                try:
                    view_controls = getattr(self, "_view_controls", None)
                    if (view_controls is not None
                            and view_controls.is_open):
                        view_controls.close_panel()
                except Exception:
                    pass
                # The page navigator follows the same outside-press rule
                # (its own filter is mouse-only for the reason above).
                # The boundary is GEOMETRIC: a stylus tap inside the open
                # panel is chrome input — closing here would both dismiss
                # the navigator and fall through to the ink router,
                # drawing on the document. Taps inside the panel never
                # reach this block anyway (they are forwarded as chrome
                # input via _tablet_chrome_target), so this geometry
                # check is the safety net for hit-test misses.
                try:
                    navigator = getattr(self, "_page_navigator", None)
                    if (navigator is not None and navigator.is_open
                            and not navigator.panel_covers_viewer_point(
                                x, y)):
                        navigator.close_panel()
                except Exception:
                    pass
            # Stylus hover/drag on the canvas re-derives the pointer
            # cursor from the actual position, through the same helper as
            # the mouse path. With the pen there are no mouse moves to
            # restore the active tool's cursor after the pointer has been
            # over chrome, a popup, or the window resize grip.
            if etype == QEvent.Type.TabletMove:
                try:
                    target = self._tablet_chrome_target(x, y)
                except Exception:
                    target = None
                if target is not None:
                    # Qt sends no Enter/Leave for tablet hover, so the
                    # chrome widget's own cursor would otherwise never
                    # reach the platform: apply it to the viewer (child
                    # cursors are read, never written).
                    self._apply_chrome_cursor(target)
                else:
                    self._derive_pointer_cursor(
                        x, y, dragging=bool(event.buttons()
                                            & Qt.MouseButton.LeftButton))
            # M6.5: the eraser shares the mapped page-local input path
            # (and the same stream guards) as the pen/highlighter.
            if self._is_eraser_active() and etype in tablet_types:
                mapped = self._event_to_page_pt(x, y)
                if etype == QEvent.Type.TabletPress and mapped is None:
                    event.ignore()
                    return
                if self._eraser_mouse_open and not self._eraser_tablet_open:
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: the visible selection frame owns the
                # stylus press/move/release before the eraser path.
                if self._frame_gesture_tablet(etype, x, y, "polygon"):
                    event.accept()
                    return
                if etype == QEvent.Type.TabletRelease and mapped is None:
                    self._apply_intent(
                        self._interaction.handle_eraser_release(
                            None, 0.0, 0.0, pressure, now_ms))
                elif mapped is not None:
                    page_id, lx, ly = mapped
                    if etype == QEvent.Type.TabletPress:
                        # M6.8.2 Stage 5.2.2: stylus erase starting outside
                        # the frame drops a stale selection first.
                        self._deselect_for_tool_press(x, y)
                        self._apply_intent(
                            self._interaction.handle_eraser_press(
                                page_id, lx, ly, pressure, now_ms))
                        self._eraser_tablet_open = True
                    elif etype == QEvent.Type.TabletMove:
                        self._apply_intent(
                            self._interaction.handle_eraser_move(
                                page_id, lx, ly, pressure, now_ms))
                    else:
                        self._apply_intent(
                            self._interaction.handle_eraser_release(
                                page_id, lx, ly, pressure, now_ms))
                event.accept()
                return
            # M6.7: the shape tools share the same mapped page-local input
            # path (and stream guards) as pen/highlighter/eraser.
            if self._is_shape_active() and etype in tablet_types:
                mapped = self._event_to_page_pt(x, y)
                if etype == QEvent.Type.TabletPress and mapped is None:
                    event.ignore()
                    return
                if self._shape_mouse_open and not self._shape_tablet_open:
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: the visible selection frame owns the
                # stylus press/move/release before the shape path.
                if self._frame_gesture_tablet(etype, x, y, "polygon"):
                    event.accept()
                    return
                if etype == QEvent.Type.TabletRelease and mapped is None:
                    self._apply_intent(self._shape_release(
                        None, 0.0, 0.0, pressure, now_ms))
                elif mapped is not None:
                    page_id, lx, ly = mapped
                    if etype == QEvent.Type.TabletPress:
                        # M6.8.2 Stage 5.2.2: a stylus shape gesture starting
                        # outside the frame drops a stale selection first.
                        self._deselect_for_tool_press(x, y)
                        self._apply_intent(self._shape_press(
                            page_id, lx, ly, pressure, now_ms))
                    elif etype == QEvent.Type.TabletMove:
                        self._apply_intent(self._shape_move(
                            page_id, lx, ly, pressure, now_ms))
                    else:
                        self._apply_intent(self._shape_release(
                            page_id, lx, ly, pressure, now_ms))
                self._shape_tablet_open = self._shape_session_open()
                if etype == QEvent.Type.TabletMove and mapped is not None:
                    # M6.8.2: record the hover position so a synthesized
                    # mouse move for the same hover is dropped (the live
                    # preview must update once per physical move).
                    self._tablet_hover_pt = (x, y)
                    self._remember_pointer(x, y)
                event.accept()
                return
            if self._is_selection_active() and etype in tablet_types:
                # M6.8.1: a stylus press/move/release drives the same
                # marquee/lasso gesture as the mouse. The event is
                # explicitly ACCEPTED so Qt does not synthesize a
                # fallback mouse event (which would double-process the
                # gesture); the dual-stream guard additionally drops a
                # synthesized mouse press when the reverse ordering
                # occurs. A selection touches no store and no history,
                # so a gesture can never mutate annotations.
                if (self._selection_mouse_open
                        and not self._selection_tablet_open):
                    event.accept()
                    return
                if etype == QEvent.Type.TabletPress:
                    kind = ("rect" if self._region_tool_kind() == "rect"
                            else "polygon")
                    self._region_press(x, y, kind)
                    self._selection_tablet_open = True
                elif etype == QEvent.Type.TabletMove:
                    try:
                        _shift = bool(event.modifiers()
                                      & Qt.KeyboardModifier.ShiftModifier)
                    except Exception:
                        _shift = False
                    self._region_move(x, y, shift=_shift)
                elif etype == QEvent.Type.TabletRelease:
                    self._selection_tablet_open = False
                    self._region_release(x, y)
                event.accept()
                return
            # M5-B.2.0: pressure/timestamp are captured into the raw
            # per-sample arrays. Width mapping lands in a later slice;
            # rendering is unchanged (now_ms was captured once above).
            if self._ink_active() and etype in tablet_types:
                mapped = self._event_to_page_pt(x, y)
                if etype == QEvent.Type.TabletPress and mapped is None:
                    # M5-B.2.0.1 F3: unmapped press is ignored, never
                    # swallowed and never touching handler state.
                    event.ignore()
                    return
                # M5-B.2.0.1 F2: one physical stroke, one stream. A
                # tablet press/move/release is dropped while a mouse
                # stroke owns the pointer (and vice versa in the mouse
                # handlers below).
                if self._mouse_stroke_open and not self._tablet_stroke_open:
                    event.accept()
                    return
                # M6.8.2 Stage 5.2.1: the visible selection frame owns the
                # stylus press/move/release before the ink path (drawing
                # outside the frame is untouched).
                if self._frame_gesture_tablet(etype, x, y, "polygon"):
                    event.accept()
                    return
                if etype == QEvent.Type.TabletRelease and mapped is None:
                    self._apply_intent(
                        self._ink_release(None, 0.0, 0.0, pressure, now_ms))
                elif mapped is not None:
                    page_id, lx, ly = mapped
                    if etype == QEvent.Type.TabletPress:
                        # M6.8.2 Stage 5.2.2: a stylus stroke starting outside
                        # the frame drops a stale selection first.
                        self._deselect_for_tool_press(x, y)
                        self._apply_intent(
                            self._ink_press(page_id, lx, ly, pressure,
                                            now_ms))
                        self._tablet_stroke_open = True
                    elif etype == QEvent.Type.TabletMove:
                        self._apply_intent(
                            self._ink_move(page_id, lx, ly, pressure,
                                           now_ms))
                    else:
                        self._apply_intent(
                            self._ink_release(page_id, lx, ly, pressure,
                                              now_ms))
                event.accept()
                return
            # M6.8.2: the Pan tool owns the stylus press/move/release for
            # dragging, plus double-tap selection (a stylus tap never
            # yields a MouseButtonDblClick). ``_pan_tablet_open`` guards
            # the matching synthesized mouse events.
            if self._interaction.active_kind == ToolType.PAN \
                    and etype in tablet_types:
                if etype == QEvent.Type.TabletPress:
                    # M6.8.2 Stage 5.2.1: a stylus landing on the visible
                    # selection frame - a grip, a selected body, or the frame
                    # interior - arms the shared transform / group move
                    # before the immediate Pan deselect and double-tap logic
                    # (the rotation grip sits outside the frame, so this must
                    # come first).
                    if self._frame_manipulation_press(x, y, "polygon"):
                        self._pan_tablet_open = True
                        self._pan_press_point = None
                        self._pan_dragged = False
                        event.accept()
                        return
                    self._pan_tablet_open = True
                    self._pan_tablet_press(x, y, now_ms)
                    self._apply_intent(self._interaction.handle_press(
                        x, y, pressure=pressure))
                elif etype == QEvent.Type.TabletMove:
                    if self._move_gesture is not None:
                        self._move_move(x, y)
                    else:
                        self._note_pan_move(x, y)
                        self._apply_intent(self._interaction.handle_move(
                            x, y, pressure=pressure))
                else:
                    _was_move = self._move_gesture is not None
                    if _was_move:
                        self._move_release(x, y)
                    self._pan_tablet_open = False
                    self._pan_press_point = None
                    self._pan_dragged = False
                    # Tool cursors: a cancelled/ended tablet pan restores the tool
                    # cursor (the closed hand was a mouse-drag affordance).
                    self._refresh_tool_cursor()
                    if not _was_move:
                        self._apply_intent(self._interaction.handle_release(
                            x, y))
                event.accept()
                return
            if etype == QEvent.Type.TabletPress:
                self._apply_intent(self._interaction.handle_press(
                    x, y, pressure=pressure))
            elif etype == QEvent.Type.TabletMove:
                self._apply_intent(self._interaction.handle_move(
                    x, y, pressure=pressure))
            elif etype == QEvent.Type.TabletRelease:
                self._apply_intent(self._interaction.handle_release(x, y))
            event.accept()
            return
        except Exception:
            pass
        super().tabletEvent(event)

    # -- async completion (QTimer poll; never blocks on futures) ------------

    def _poll(self) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        vw, vh = self._render_viewport()
        try:
            # Re-run admission on the 40 ms poll so deferred prefetch work
            # re-queues as soon as the visible set is resident (cheap:
            # update() is O(visible), no I/O on the GUI thread).
            pipeline.update(self._scroll_doc(), int(vw), int(vh),
                              scroll_x_doc=self._scroll_doc_x())
            ready = pipeline.visible_ready(self._scroll_doc(), int(vw), int(vh),
                                           scroll_x_doc=self._scroll_doc_x())
        except Exception:
            return
        changed = False
        wanted: set[tuple] = set()
        for tile in ready:
            identity = cache_identity(tile.key)
            wanted.add(identity)
            if identity not in self._display:
                image = QImage(
                    tile.data, tile.width_px, tile.height_px, tile.stride,
                    QImage.Format.Format_RGBA8888,
                ).copy()  # THE single detachment copy (see module docs)
                # M7.1.4.3 view-only appearance: the detached copy this
                # viewer owns is transformed for the current mode; the
                # renderer's own cached pixels and cache identities stay
                # untouched, so every mode always starts from originals.
                self._display[identity] = _appearance_tile(
                    image, self._pdf_appearance)
                changed = True
        keep, _drop = split_current_stale(wanted | self._wanted_prev, self._display)
        if len(keep) != len(self._display):
            changed = True
        self._display = keep
        self._wanted_prev = wanted
        if changed:
            self.update()

    # -- painting (no submission, no waiting, no fitz) ------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        """Paint pages, ready tiles, and placeholders. Returns immediately.

        M4.6.9: single owner — begin() verified, end() exactly once via
        finally (early returns never skip it, never paint when inactive).
        """
        painter = QPainter(self)
        try:
            if not painter.isActive():
                return
            # B.2.4: repaint only Qt's dirty region. Bare update()
            # calls (scroll, zoom, resize, stroke completion) still
            # mark the whole widget, so those paths repaint fully by
            # construction; live strokes narrow the region via
            # _apply_intent. Empty/null region - full repaint (safe).
            try:
                dirty = event.region().boundingRect()
                if dirty.isNull():
                    raise ValueError("empty dirty region")
            except Exception:
                dirty = self.rect()
            self._paint_body(painter, dirty)
            # M6.8.2: a cross-page preview whose widget-space band left
            # its assigned page entirely (inter-page gutter during a
            # drag) keeps a visible preview: uncovered bands are
            # painted here through the page path, hull-clipped.
            self._paint_move_preview_overlay(painter, dirty)
            # M6.8.1: keep the frame animation alive for exactly as long
            # as a selection is painted (safe no-op when idle).
            self._sync_selection_animation()
            # M6.8.1: the single shared selection frame is drawn in widget
            # space (it can span pages and the gaps between them), above
            # every annotation layer, clipped to the viewport.
            self._paint_selection_frame(painter, dirty)
            # M6.8.1: the live marquee/lasso overlay is widget-space (it
            # can span pages and gaps) and paints above everything.
            self._paint_region_gesture_overlay(painter, dirty)
        except Exception:
            pass
        finally:
            try:
                if painter.isActive():
                    painter.end()
            except Exception:
                pass

    def _paint_body(self, painter, dirty=None) -> None:
        """PaintEvent body (painter guaranteed active by the caller).

        ``dirty`` is the widget-space region that must be recomputed
        (B.2.4); ``None`` repaints everything. Background, page fills,
        tiles, and strokes outside ``dirty`` are skipped — paint order,
        clipping, opacity, and z-order are unchanged, so a partial
        repaint composes identically to a full one over the region.
        """
        if dirty is None:
            dirty = self.rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(dirty, _BACKGROUND_FILL)
        self.painted_tiles = 0
        self.painted_placeholders = 0
        # Async switch in progress: the outgoing session stays attached
        # (instantly restorable) but is never presented under the
        # destination tab's title. The loading state is deliberately
        # QUIET: the plain document canvas — the same surface an empty
        # viewer shows — with no text, spinner or animation. The
        # destination pages appear as soon as the commit lands.
        if self._loading_title is not None:
            return
        pipeline = self._pipeline
        if pipeline is None:
            if self._error is not None:
                painter.setPen(_EMPTY_TEXT_FILL)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._error)
            else:
                painter.setPen(_EMPTY_TEXT_FILL)
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter, "Open a PDF (Ctrl+O)"
                )
            return
        scale = self._display_scale()
        scroll_doc = self._scroll_doc()
        scroll_doc_x = self._scroll_doc_x()
        vw_render, vh_render = self._render_viewport()
        layout = pipeline.layout
        content_width = float(layout.total_width_px)
        try:
            pages = layout.pages_in(scroll_doc, scroll_doc + vh_render)
            planned = pipeline.visible_plan(scroll_doc, int(vw_render), int(vh_render),
                                            scroll_x_doc=scroll_doc_x)
        except Exception:
            return
        wanted = {cache_identity(key) for key in planned}
        by_page: dict[object, list] = {}
        for key in planned:
            by_page.setdefault(key.page_id.value, []).append(key)
        for index in pages:
            try:
                entry = layout.entry(index)
            except Exception:
                continue
            page_rect = page_screen_rect(
                float(entry.y_offset_px), float(entry.render_width_px),
                float(entry.render_height_px), scroll_doc, vw_render, scale,
                scroll_x_doc=scroll_doc_x, content_width_px=content_width,
            )
            r = to_int_rect(page_rect)
            page_target = QRect(r.x, r.y, r.width, r.height)
            # B.2.4: pages fully outside the dirty region keep their
            # existing pixels (tiles, fills, and strokes alike).
            if not page_target.intersects(dirty):
                continue
            painter.fillRect(page_target, self._pdf_page_fill())
            for key in by_page.get(entry.page_id.value, []):
                identity = cache_identity(key)
                t = snapped_tile_rect(
                    key.tile_x, key.tile_y, key.tile_w, key.tile_h, page_rect, scale
                )
                target = QRect(t.x, t.y, t.width, t.height)
                # B.2.4: off-region tiles are neither drawn nor counted.
                if not target.intersects(dirty):
                    continue
                image = self._display.get(identity)
                if image is not None and identity in wanted:
                    painter.drawImage(target, image)
                    self.painted_tiles += 1
                    self.total_painted_tiles += 1
                else:
                    # Unloaded / refreshing tile: exactly the active
                    # read mode's page paper, so a still-arriving tile is
                    # invisible instead of flashing an unrelated colour
                    # or a grid seam. Same source as the page fill above.
                    painter.fillRect(target, self._pdf_page_fill())
                    self.painted_placeholders += 1
                    self.total_painted_placeholders += 1
            # M6.3 highlighter layer: committed highlighters come from
            # the document store (authoritative), plus the transient
            # live preview. Painted under the pen ink, above the page.
            self._paint_page_highlighters(painter, layout, entry,
                                          page_rect, scale, dirty)
            # M6.7 shape layer: committed shapes come from the document
            # store (authoritative), plus the transient live preview.
            # Fixed layer order: highlighters -> shapes -> pen ink.
            self._paint_page_shapes(painter, layout, entry, page_rect,
                                    scale, dirty)
            # M5-B.1 pen overlay: strokes paint above tiles, clipped to
            # the page. Tile images and cache data are never touched.
            self._paint_page_strokes(painter, layout, entry, page_rect,
                                     scale, dirty)

    def _pressure_outline(self, stroke):
        """Variable-width outline for one stroke, or None for legacy path.

        M5-B.2.1 Slice 3: returns a ``StrokeOutline`` only when the
        stroke latched ``pressure_enabled`` at press time AND carries
        one pressure sample per point. Everything else (disabled
        tools, legacy strokes, empty/mismatched arrays, malformed
        widths) yields None so the caller keeps the scalar-width path
        — pressure samples are never invented. Only the documented
        ``ValueError`` from the geometry utilities is absorbed; any
        other exception propagates (caller paint path already guards).
        """
        if not getattr(stroke, "pressure_enabled", False):
            return None
        points = getattr(stroke, "points", ())
        pressures = getattr(stroke, "pressures", ())
        if not points or len(pressures) != len(points):
            return None
        try:
            base_pt = float(stroke.width_mm) * 72.0 / 25.4
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(base_pt) and base_pt > 0.0):
            return None
        try:
            # B.2.3: latched response profile; unknown values fall back
            # to the legacy path via ValueError (never lose ink).
            widths = pressure_widths(
                tuple(pressures), base_pt,
                getattr(stroke, "pressure_profile", "normal"))
            # B.2.2: render-geometry smoothing on copies; raw samples
            # stay the source of truth.
            smooth_pts, smooth_widths = smooth_centerline(
                [(p.x, p.y) for p in points], widths)
            return build_outline(
                smooth_pts, smooth_widths if smooth_widths is not None
                else widths)
        except ValueError:
            return None

    def _cached_outline(self, stroke):
        """Completed-stroke outline via the B.2.4 geometry cache.

        Live strokes and non-``InkStroke`` items always take the
        uncached path. ``None`` results (legacy/disabled strokes) are
        cheap early-outs and are never stored.
        """
        if stroke is self._live_stroke or not isinstance(stroke, InkStroke):
            return self._pressure_outline(stroke)
        try:
            outline = self._outline_cache.get(stroke)
        except Exception:
            return self._pressure_outline(stroke)
        if outline is not None:
            self.outline_cache_hits += 1
            return outline
        self.outline_cache_misses += 1
        outline = self._pressure_outline(stroke)
        # M6.8.2: transient group-move candidates are churning garbage —
        # never pinned into the committed-outline cache.
        if outline is not None and not self._is_move_preview(stroke):
            try:
                self._outline_cache[stroke] = outline
            except Exception:
                pass
        return outline

    @staticmethod
    def _outline_page_extents(outline):
        """Page-space (minx, miny, maxx, maxy) of an outline, or None.

        B.2.4: pure function of the immutable outline value (rails +
        dots/discs); the paint path memoizes it per cached outline so
        per-paint skip checks stay O(1).
        """
        try:
            xs: list = []
            ys: list = []
            for p in list(getattr(outline, "polygon", ()) or ()):
                xs.append(float(p.x))
                ys.append(float(p.y))
            for dot in (list(getattr(outline, "dots", ()) or ())
                        + list(getattr(outline, "discs", ()) or ())):
                cx = float(dot.center.x)
                cy = float(dot.center.y)
                r = float(dot.radius)
                xs.extend((cx - r, cx + r))
                ys.extend((cy - r, cy + r))
            if not xs or not ys:
                return None
            for v in xs + ys:
                if v != v or v in (float("inf"), float("-inf")):
                    return None
            return (min(xs), min(ys), max(xs), max(ys))
        except Exception:
            return None

    @staticmethod
    def _map_extents(extents, ox, oy, factor):
        """Map page-space extents to a padded widget-space QRect."""
        from PySide6.QtCore import QRectF

        minx, miny, maxx, maxy = extents
        pad = 3.0
        scale = max(float(factor), 1e-9)
        rect = QRectF(ox + minx * scale - pad,
                      oy + miny * scale - pad,
                      (maxx - minx) * scale + 2.0 * pad,
                      (maxy - miny) * scale + 2.0 * pad).toAlignedRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        return rect

    def _stroke_paint_rect(self, stroke, outline, ox, oy, factor):
        """Conservative widget-space bbox for one stroke, or None.

        B.2.4 dirty-region helper. Page-space extents (outline rails +
        dots/discs, or raw points ± half width as fallback) mapped with
        the paint path's own ``(ox, oy, factor)`` plus a 3 px screen
        pad for antialiasing overhang and rounding. M8: highlighter items
        use the rectangular band extents instead (``[x ± t/2]`` by
        ``[y - aspect*t/2, y + aspect*t/2]``) so culling, live updates,
        and completion repaints cover the whole painted footprint (the
        nib's contact point is the centre of that rectangle). ``None``
        means "uncomputable" — the caller must paint unconditionally
        (safe).
        """
        try:
            pad = 3.0
            scale = max(float(factor), 1e-9)
            xs: list = []
            ys: list = []
            if outline is not None:
                extents = self._outline_page_extents(outline)
                if extents is None:
                    return None
                minx, miny, maxx, maxy = extents
                xs.extend((minx, maxx))
                ys.extend((miny, maxy))
            else:
                raw = [(float(p.x), float(p.y))
                       for p in (getattr(stroke, "points", ()) or ())]
                if not raw:
                    return None
                try:
                    width_px = max(1.0, float(stroke.width_mm)
                                   * 72.0 / 25.4 * self._ui_zoom)
                except Exception:
                    width_px = 1.0
                # ``width_px`` is widget px; dividing by the paint factor
                # returns the page-pt thickness the geometry uses
                # (``factor == ui_zoom`` in the paint path).
                page_width = width_px / scale
                edge = pad / scale
                half = page_width / 2.0 + edge
                # M8 rectangular highlighter band: the footprint is
                # [x +/- t/2] x [y - aspect*t/2, y + aspect*t/2] — the
                # nib's contact point is the rectangle centre, matching
                # the cursor hotspot and the painted band.
                band = self._is_highlighter_band(stroke)
                band_half = page_width * HIGHLIGHTER_BAND_ASPECT / 2.0
                for x, y in raw:
                    xs.extend((x - half, x + half))
                    if band:
                        ys.extend((y - band_half - edge,
                                   y + band_half + edge))
                    else:
                        ys.extend((y - half, y + half))
            if not xs or not ys:
                return None
            for v in xs + ys:
                if v != v or v in (float("inf"), float("-inf")):
                    return None
            return self._map_extents((min(xs), min(ys), max(xs), max(ys)),
                                     ox, oy, factor)
        except Exception:
            return None

    def _live_tail_rect(self, stroke, ox, oy, factor):
        """Incremental invalidation rect for the live stroke, or None.

        B.2.6: O(1) per input sample. Returns a widget-space rect
        covering every pixel the live outline may have changed since
        the previous paint, derived from the last ``_LIVE_TAIL_SAMPLES``
        raw samples only. ``None`` means "cannot bound safely" — the
        caller falls back to the full-outline scan (never lose pixels).

        Why the tail suffices (all facts from
        ``annotations/ink_geometry.py``): ``_resample`` is decimate-only
        (kept points are exact raw samples; the greedy forward pass
        keeps every prefix decision, so appending one sample alters the
        kept set only in its last ~2 entries); each ``_chaikin`` pass
        blends 2 adjacent inputs on-segment, so ``SMOOTH_PASSES``
        passes reach back at most ``SMOOTH_PASSES + 1`` resampled
        steps; rail joints need +/-1 smoothed neighbors, the end cap
        rebuilds at the tip, and the start cap is pinned; widths ride
        the same blends and ``pressure_width`` clamps every width to
        ``base`` (so ``base / 2`` bounds every rail offset, joint disc,
        dot, and cap radius). ``LiveOutline`` splices by verified exact
        match and otherwise derives the same deterministic function, so
        outline geometry beyond the influence radius is bit-identical to
        what the previous paint drew. The changed region therefore lies
        on the raw path within ``(SMOOTH_PASSES + _LIVE_TAIL_EXTRA_STEPS)
        * SMOOTH_MIN_SPACING_PT`` of the tip, +/- ``base / 2`` — and any
        older sample within that arc is within that euclidean distance
        of the tip itself, so expanding the tail-sample bbox by that
        reach is sound at any sample density. Mapping, padding, and the
        pressure/legacy half-width conventions match
        ``_stroke_paint_rect``; vacated pixels are covered by the
        caller's union with the previous rect.
        """
        try:
            scale = max(float(factor), 1e-9)
            raw = getattr(stroke, "points", ()) or ()
            try:
                n = len(raw)
            except TypeError:
                return None
            if n == 0:
                return None
            try:
                spacing = float(SMOOTH_MIN_SPACING_PT)
                steps = int(SMOOTH_PASSES) + int(_LIVE_TAIL_EXTRA_STEPS)
            except (TypeError, ValueError):
                return None
            if not (math.isfinite(spacing) and spacing > 0.0
                    and steps >= 0):
                return None
            # Half-width bound shared by both paint paths: the pressure
            # path never exceeds base / 2 (pressure_width clamps to
            # base); the legacy path uses width_px / 2. Take whichever
            # bounds apply (max when both do) so a paint-path fallback
            # can never paint outside this rect.
            halves = []
            try:
                base_pt = float(stroke.width_mm) * 72.0 / 25.4
            except (TypeError, ValueError):
                base_pt = None
            pressures = getattr(stroke, "pressures", ())
            try:
                pressure_ok = (
                    bool(getattr(stroke, "pressure_enabled", False))
                    and base_pt is not None
                    and math.isfinite(base_pt) and base_pt > 0.0
                    and len(pressures) == n)
            except TypeError:
                pressure_ok = False
            if pressure_ok:
                halves.append(base_pt / 2.0)
            try:
                legacy_px = max(1.0, float(stroke.width_mm)
                                * 72.0 / 25.4 * self._ui_zoom)
                halves.append(legacy_px / 2.0 / scale + 3.0 / scale)
            except Exception:
                pass
            if not halves:
                return None
            half = max(halves)
            if not (math.isfinite(half) and half >= 0.0):
                return None
            reach = steps * spacing + half
            if not (math.isfinite(reach) and reach >= 0.0):
                return None
            tail = raw[max(0, n - _LIVE_TAIL_SAMPLES):]
            xs: list = []
            ys: list = []
            for p in tail:
                xs.append(float(p.x) - reach)
                xs.append(float(p.x) + reach)
                ys.append(float(p.y) - reach)
                ys.append(float(p.y) + reach)
            if not xs or not ys:
                return None
            for v in xs + ys:
                if v != v or v in (float("inf"), float("-inf")):
                    return None
            return self._map_extents((min(xs), min(ys), max(xs),
                                      max(ys)), ox, oy, factor)
        except Exception:
            return None

    def _live_paint_rect(self, stroke, outline, ox, oy, factor):
        """Widget-space bbox for the live stroke, or None.

        B.2.8: same geometry as ``_stroke_paint_rect``, but the
        page-space extents of the live outline are memoized in a
        single identity-keyed slot. The builder returns the identical
        cached outline object until the next state-changing append, so
        an ``is``-identity hit proves the extents are fresh; any append
        installs a new outline object and recomputes. Anything without
        an outline (or not the live stroke) delegates to
        ``_stroke_paint_rect`` unchanged. Cleared on done/cancel/close.
        """
        try:
            if outline is not None and stroke is self._live_stroke:
                memo = self._live_extents_memo
                if memo is not None and memo[0] is outline:
                    extents = memo[1]
                else:
                    extents = self._outline_page_extents(outline)
                    self._live_extents_memo = (outline, extents)
                if extents is None:
                    return None
                return self._map_extents(extents, ox, oy, factor)
            return self._stroke_paint_rect(stroke, outline, ox, oy,
                                           factor)
        except Exception:
            return None

    def _page_origin_for(self, page_key):
        """Widget-space page origin + map factor, or None.

        B.2.6: single-sourced mapping shared by the live-update rect,
        the completion rect, and (via identical inputs) the paint path
        — one place to keep identical, so rects can never drift from
        what painting draws. ``None`` when the page is not visible or
        any input is unusable (callers repaint fully).
        """
        try:
            pipeline = self._pipeline
            layout = pipeline.layout
            scale = self._display_scale()
            scroll_doc = self._scroll_doc()
            scroll_doc_x = self._scroll_doc_x()
            vw_render, vh_render = self._render_viewport()
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return None
            content_width = float(layout.total_width_px)
            factor = bucket * scale
            for index in layout.pages_in(scroll_doc,
                                         scroll_doc + vh_render):
                try:
                    entry = layout.entry(index)
                except Exception:
                    continue
                if getattr(getattr(entry, "page_id", None),
                           "value", None) != page_key:
                    continue
                page_rect = page_screen_rect(
                    float(entry.y_offset_px),
                    float(entry.render_width_px),
                    float(entry.render_height_px), scroll_doc,
                    vw_render, scale, scroll_x_doc=scroll_doc_x,
                    content_width_px=content_width)
                return (float(page_rect.x), float(page_rect.y), factor)
            return None
        except Exception:
            return None

    def _completion_rect(self, done, prev):
        """Widget-space update rect for a just-completed stroke.

        B.2.6: union of the completed stroke's paint bounds (via the
        outline cache — completed geometry is a one-time build that
        also warms the cache for later paints) and the last live rect,
        which covers any stale live pixels where the incremental
        preview and the final rebuild could differ by a pixel. The live
        stroke paints the exact final geometry (B.2.3), with identical
        order, clip, opacity, and composition through the same paint
        path, so this scoped repaint is pixel-identical to a full
        update. ``None`` (page off-view, uncomputable bounds, any
        failure) means the caller repaints fully.
        """
        try:
            page_key = getattr(getattr(done, "page_id", None),
                               "value", None)
            mapped = self._page_origin_for(page_key)
            if mapped is None:
                return None
            ox, oy, factor = mapped
            try:
                outline = self._cached_outline(done)
            except Exception:
                outline = None
            try:
                bounds = self._stroke_paint_rect(
                    done, outline, ox, oy, factor)
            except Exception:
                bounds = None
            rect = None
            if bounds is not None:
                try:
                    rect = QRect(bounds)
                except Exception:
                    rect = None
            if prev is not None:
                try:
                    prev_q = QRect(prev)
                    rect = prev_q if rect is None else rect.united(
                        prev_q)
                except Exception:
                    pass
            return rect
        except Exception:
            return None

    def _live_update_rect(self, stroke):
        """Widget-space update rect for the live stroke, or None.

        B.2.4: locates the stroke's page with the same layout inputs
        the paint path uses (no duplicated origin math — the entry's
        ``page_screen_rect`` is computed identically), then maps the
        live outline (or raw points as fallback) via
        ``_stroke_paint_rect``. B.2.6: tries the O(1) incremental tail
        rect (``_live_tail_rect``) first, so ordinary input samples skip
        both the outline pull and the full-vertex scan; the full scan
        below remains as the safe fallback. ``None`` - caller repaints
        fully.
        """
        try:
            page_key = getattr(getattr(stroke, "page_id", None),
                               "value", None)
            mapped = self._page_origin_for(page_key)
            if mapped is None:
                return None
            ox, oy, factor = mapped
            # B.2.6: incremental tail rect first (O(1) per sample,
            # no outline pull); the full scan below is the safe
            # fallback when the tail cannot be bounded.
            tail = self._live_tail_rect(stroke, ox, oy, factor)
            if tail is not None:
                return tail
            try:
                outline = self._interaction.live_outline()
            except Exception:
                outline = None
            return self._stroke_paint_rect(stroke, outline, ox, oy,
                                           factor)
        except Exception:
            return None

    # -- M6.7 shape rendering -------------------------------------------------

    def _shape_paint_rect(self, shape, page_rect, factor):
        """Widget-space padded bbox of one shape (stroke + marker room).

        M6.8.2 Stage 5.2: for a rotated Rectangle/Ellipse the bound is the
        *rotation-aware* extent, not the stored unrotated box — the
        painted geometry turns with ``rotation_deg``, so a box-only bound
        would leave the rotated corners outside the scoped repaint region
        (stale pixels) and outside the culling test.
        """
        try:
            r = shape.rect
            try:
                width_px = max(1.0, float(shape.width_mm) * 72.0 / 25.4
                               * self._ui_zoom)
            except Exception:
                width_px = 1.0
            pad = width_px * 4.0 + 14.0
            deg = _shape_rotation_deg(shape)
            box = None
            if deg % 360.0 != 0.0:
                try:
                    box = _shape_extent(shape)
                except Exception:
                    box = None
            if box is not None:
                left = float(page_rect.x) + float(box[0]) * factor - pad
                top = float(page_rect.y) + float(box[1]) * factor - pad
                w = (float(box[2]) - float(box[0])) * factor + 2.0 * pad + 2.0
                h = (float(box[3]) - float(box[1])) * factor + 2.0 * pad + 2.0
                return QRect(int(left), int(top), int(w), int(h))
            left = float(page_rect.x) + r.x0 * factor - pad
            top = float(page_rect.y) + r.y0 * factor - pad
            w = r.width * factor + 2.0 * pad + 2.0
            h = r.height * factor + 2.0 * pad + 2.0
            return QRect(int(left), int(top), int(w), int(h))
        except Exception:
            return None

    @staticmethod
    def _draw_ending(painter, ending, tip_x, tip_y, dir_x, dir_y, color,
                     width_px, size) -> None:
        """Draw one ending marker at a tip, pointing along ``dir``."""
        from PySide6.QtCore import QPointF

        ending = str(ending)
        if ending in ("", "none"):
            return
        try:
            length = math.hypot(dir_x, dir_y)
            if length <= 0.0:
                ux, uy = 1.0, 0.0
            else:
                ux, uy = dir_x / length, dir_y / length
            if ending == "dot":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                r = max(2.0, size / 2.0)
                painter.drawEllipse(QPointF(tip_x, tip_y), r, r)
            elif ending == "bar":
                px, py = -uy, ux
                half = max(3.0, size * 0.7)
                pen = QPen(color)
                pen.setWidthF(max(1.0, width_px))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(
                    QPointF(tip_x + px * half, tip_y + py * half),
                    QPointF(tip_x - px * half, tip_y - py * half))
            elif ending == "arrow":
                px, py = -uy, ux
                back = max(6.0, size)
                wing = back * 0.5
                pen = QPen(color)
                pen.setWidthF(max(1.0, width_px))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                bx = tip_x - ux * back
                by = tip_y - uy * back
                painter.drawLine(QPointF(tip_x, tip_y),
                                 QPointF(bx + px * wing, by + py * wing))
                painter.drawLine(QPointF(tip_x, tip_y),
                                 QPointF(bx - px * wing, by - py * wing))
        except Exception:
            pass

    def _draw_shape(self, painter, shape, ox, oy, factor) -> None:
        """Draw one shape (committed or preview) in page widget coords.

        Shared by the live preview and the committed paint path, so the
        preview and the committed result are pixel-identical.
        """
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QBrush

        pen, base, width_px = self._shape_stroke_pen(shape)

        kind = getattr(shape, "kind", ShapeKind.RECTANGLE)
        brush = QBrush(Qt.BrushStyle.NoBrush)
        try:
            closed_ok = (kind != ShapeKind.POLYGON
                         or bool(getattr(shape, "close_path", False)))
            if (bool(getattr(shape, "fill_enabled", False)) and closed_ok
                    and kind in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE,
                                 ShapeKind.POLYGON)):
                fc = QColor(str(getattr(shape, "fill_color", "#ffffff")))
                if not fc.isValid():
                    fc = QColor("#ffffff")
                fc.setAlphaF(max(0.0, min(1.0, float(
                    getattr(shape, "fill_opacity", 1.0)))))
                brush = QBrush(fc)
        except Exception:
            brush = QBrush(Qt.BrushStyle.NoBrush)

        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(pen)
            painter.setBrush(brush)
            r = getattr(shape, "rect", None)
            # M6.8.2 Stage 5.2 corrective: Rectangle/Ellipse store the
            # *unrotated local box* plus a clockwise y-down
            # ``rotation_deg`` about that box's center — the exact
            # convention shape_hit / shape_extent / the transform layer
            # already use. The painter ignored it, so a rotated shape was
            # drawn axis-aligned: during a rotation gesture the content
            # orbited the pivot without turning (reading as squashed /
            # out of step with its rotating frame), and the eraser's
            # rotation-aware hit geometry no longer matched what was on
            # screen. Rotating the painter about the box center — a rigid
            # transform, so stroke width and dimensions are preserved —
            # makes the painted geometry agree with every other layer.
            if kind in (ShapeKind.RECTANGLE, ShapeKind.ELLIPSE) and r is not None:
                deg = _shape_rotation_deg(shape)
                if deg % 360.0 != 0.0:
                    cx = ox + (float(r.x0) + float(r.x1)) / 2.0 * factor
                    cy = oy + (float(r.y0) + float(r.y1)) / 2.0 * factor
                    painter.translate(cx, cy)
                    painter.rotate(deg)
                    painter.translate(-cx, -cy)
            if kind == ShapeKind.RECTANGLE and r is not None:
                painter.drawRect(QRectF(
                    ox + r.x0 * factor, oy + r.y0 * factor,
                    r.width * factor, r.height * factor))
            elif kind == ShapeKind.ELLIPSE and r is not None:
                painter.drawEllipse(QRectF(
                    ox + r.x0 * factor, oy + r.y0 * factor,
                    r.width * factor, r.height * factor))
            elif kind == ShapeKind.POLYGON:
                pts = [QPointF(ox + p.x * factor, oy + p.y * factor)
                       for p in getattr(shape, "points", ())]
                if len(pts) >= 2:
                    if bool(getattr(shape, "close_path", False)):
                        painter.drawPolygon(QPolygonF(pts))
                    else:
                        # Open polyline (no fill): the confirmed-edge
                        # polygon preview, or a Close-shape-off shape.
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                        painter.drawPolyline(QPolygonF(pts))
            else:  # LINE / ARROW
                pts = list(getattr(shape, "points", ()))
                if len(pts) >= 2:
                    p0 = QPointF(ox + pts[0].x * factor,
                                 oy + pts[0].y * factor)
                    p1 = QPointF(ox + pts[1].x * factor,
                                 oy + pts[1].y * factor)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawLine(p0, p1)
                    dx = p1.x() - p0.x()
                    dy = p1.y() - p0.y()
                    size = max(6.0, width_px * 3.0)
                    self._draw_ending(
                        painter, getattr(shape, "start_ending", "none"),
                        p0.x(), p0.y(), -dx, -dy, base, width_px, size)
                    self._draw_ending(
                        painter, getattr(shape, "end_ending", "none"),
                        p1.x(), p1.y(), dx, dy, base, width_px, size)
        finally:
            try:
                painter.restore()
            except Exception:
                pass

    def _shape_stroke_pen(self, shape):
        """Build a shape's outline ``QPen`` from its stroke properties.

        M6.7.2: shared by the committed/preview outline (:meth:`_draw_shape`)
        and the Polygon provisional live segment, so both stroke
        identically. Returns ``(pen, base_color, width_px)``; the last two
        are used by the Line/Arrow ending markers.
        """
        try:
            base = QColor(str(getattr(shape, "color", "#000000")))
            if not base.isValid():
                base = QColor("#000000")
        except Exception:
            base = QColor("#000000")
        try:
            base.setAlphaF(max(0.0, min(1.0, float(
                getattr(shape, "opacity", 1.0)))))
        except Exception:
            pass
        try:
            width_px = max(1.0, float(shape.width_mm) * 72.0 / 25.4
                           * self._ui_zoom)
        except Exception:
            width_px = 1.0
        pen = QPen(base)
        pen.setWidthF(width_px)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        style = str(getattr(shape, "line_style", "solid"))
        try:
            if style == "dashed":
                pen.setDashPattern([3.0, 2.0])
            elif style == "dotted":
                pen.setDashPattern([1.0, 2.0])
        except Exception:
            pass
        return pen, base, width_px

    def _draw_live_shape(self, painter, shape, ox, oy, factor) -> None:
        """Paint the transient in-progress shape preview (M6.7.2).

        For a Polygon gesture four *distinct* paint operations run, so
        their closing behavior cannot interfere: the configured fill
        contour (confirmed vertices + pointer) under everything, the
        confirmed-edge outline, the single provisional edge from the last
        confirmed vertex to the pointer, and the confirmed-vertex markers
        on top. The provisional edge never connects back to the first
        vertex, and the pointer is never a marker.
        """
        preview = self._live_shape_preview
        if preview is not None:
            self._draw_polygon_preview_fill(painter, shape, preview,
                                            ox, oy, factor)
        self._draw_shape(painter, shape, ox, oy, factor)
        if preview is not None:
            self._draw_polygon_provisional_edge(painter, shape, preview,
                                                ox, oy, factor)
            self._draw_polygon_vertices(painter, shape, preview,
                                        ox, oy, factor)
        # The snap ring is shared by the Polygon (provisional-edge endpoint)
        # and the Line/Arrow (drag endpoint) previews; drawn last, on top.
        self._draw_snap_indicator(painter, self._live_shape_snap,
                                  ox, oy, factor)

    def _draw_snap_indicator(self, painter, snap, ox, oy, factor) -> None:
        """Hollow accent ring on the resolved Connect-Lines target (M6.7.2).

        ``snap`` is the transient ``(x, y, kind)`` resolved target from the
        active tool's live preview, or a falsy value when there is none.
        Drawn only for a genuine endpoint/intersection resolution (Polygon
        provisional edge, Line/Arrow drag endpoint) — never for a plain 15°
        angular snap, which also moves the resolved point but does not
        connect to anything. Deliberately distinct from the confirmed-vertex
        markers (larger, hollow, accent-coloured, over a light halo) so an
        active snap reads as "connecting to an existing shape". Paint-only
        and transient: it never becomes a vertex and never enters the
        annotation store.
        """
        target = ()
        if snap:
            try:
                target = (float(snap[0]), float(snap[1]))
            except Exception:
                target = ()
        if len(target) != 2:
            return
        from PySide6.QtCore import QPointF

        try:
            cx = ox + float(target[0]) * factor
            cy = oy + float(target[1]) * factor
            try:
                accent = QColor(self._tool_tokens().accent)
            except Exception:
                accent = QColor("#8B93E0")
            if not accent.isValid():
                accent = QColor("#8B93E0")
            r = _SNAP_RING_PX
            halo = QPen(QColor(255, 255, 255, 210))
            halo.setWidthF(5.0)
            ring = QPen(accent)
            ring.setWidthF(2.5)
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                center = QPointF(cx, cy)
                painter.setPen(halo)
                painter.drawEllipse(center, r, r)
                painter.setPen(ring)
                painter.drawEllipse(center, r, r)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _draw_polygon_provisional_edge(self, painter, shape, preview,
                                       ox, oy, factor) -> None:
        """Stroke the live segment from the last confirmed vertex to the
        pointer (M6.7.2).

        This is the ONLY provisional stroke: a separate paint operation
        from the confirmed outline, so it can never add the closing edge
        back to the first vertex and never becomes a stored vertex. It is
        drawn only when a pointer position differs from the last confirmed
        vertex (``preview.segment`` is empty otherwise).
        """
        seg = getattr(preview, "segment", ()) or ()
        if len(seg) < 2:
            return
        from PySide6.QtCore import QPointF

        try:
            pen, _base, _w = self._shape_stroke_pen(shape)
            p0 = QPointF(ox + float(seg[0].x) * factor,
                         oy + float(seg[0].y) * factor)
            p1 = QPointF(ox + float(seg[1].x) * factor,
                         oy + float(seg[1].y) * factor)
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(p0, p1)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _draw_polygon_preview_fill(self, painter, shape, preview,
                                   ox, oy, factor) -> None:
        """Fill the preview-only Polygon contour (no stroke, no backdrop).

        Only drawn when fill is enabled, so a fill-disabled preview leaves
        the PDF/background fully visible. The contour is never stroked and
        never stored.
        """
        if not bool(getattr(shape, "fill_enabled", False)):
            return
        pts = getattr(preview, "fill", ()) or ()
        if len(pts) < 3:
            return
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QBrush

        try:
            fc = QColor(str(getattr(shape, "fill_color", "#ffffff")))
            if not fc.isValid():
                fc = QColor("#ffffff")
            fc.setAlphaF(max(0.0, min(1.0, float(
                getattr(shape, "fill_opacity", 1.0)))))
            poly = QPolygonF([QPointF(ox + float(p.x) * factor,
                                      oy + float(p.y) * factor)
                              for p in pts])
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(fc))
                painter.drawPolygon(poly)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _draw_polygon_vertices(self, painter, shape, preview,
                               ox, oy, factor) -> None:
        """Draw a constant-screen-size circle at each confirmed vertex.

        Markers are paint-only (they never become vertices) and are shown
        only for confirmed vertices — never for the moving pointer.
        """
        pts = getattr(preview, "vertices", ()) or ()
        if not pts:
            return
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QBrush

        try:
            color = QColor(str(getattr(shape, "color", "#000000")))
            if not color.isValid():
                color = QColor("#000000")
            ring = QPen(QColor(255, 255, 255, 220))
            ring.setWidthF(1.5)
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(ring)
                painter.setBrush(QBrush(color))
                r = _POLYGON_VERTEX_RADIUS_PX
                for p in pts:
                    painter.drawEllipse(
                        QPointF(ox + float(p.x) * factor,
                                oy + float(p.y) * factor), r, r)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _paint_page_shapes(self, painter, layout, entry, page_rect, scale,
                           dirty=None, overlay_rect=None,
                           preview_only=False) -> None:
        """Paint committed + live shape annotations for one page (M6.7).

        Committed shapes are read from the owning document's
        ``AnnotationStore`` (the authoritative state) — never from a
        viewer list — and the transient in-progress preview is appended
        last. Clipping and the dirty-region contract mirror
        :meth:`_paint_page_highlighters`/``_paint_page_strokes``. The
        M6.8.2 gutter overlay (``overlay_rect`` + ``preview_only``)
        repaints ONLY this page's move-preview candidates inside the
        uncovered widget-space band, through the identical per-item
        path and mapping (``page_rect`` stays the real page rect).
        """
        try:
            document = self._document
            if document is None:
                return
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return
            page_key = entry.page_id.value
        except Exception:
            return
        items: list = []
        preview = self._transform_preview
        skip = arrivals = None
        if preview_only:
            # M6.8.2 gutter overlay: only this page's move-preview
            # candidates — never the store items again (their in-page
            # part was painted by the clipped pass).
            dsts = self._transform_dsts
            if not preview or not dsts:
                return
            items = [candidate for annotation_id, candidate in
                     preview.items()
                     if dsts.get(annotation_id) == entry.index]
        else:
            # M6.8.2: cross-page routing — a committed shape whose
            # candidate transferred to another page is skipped here
            # (painted by the destination pass only), and candidates
            # destined for THIS page arrive after the store items
            # (matching the commit's append insertion policy).
            if preview is not None:
                try:
                    skip, arrivals = self._move_preview_route(entry.index)
                except Exception:
                    skip, arrivals = None, None
        try:
            for annotation in document.annotations.page(entry.index):
                if preview_only:
                    break
                if isinstance(annotation, Shape):
                    # M6.8.2: an active group-move preview replaces its
                    # stored annotation (same stable id, same layer,
                    # same order).
                    if skip is not None and annotation.id in skip:
                        continue
                    if preview is not None:
                        annotation = preview.get(annotation.id, annotation)
                    items.append(annotation)
        except Exception:
            pass
        if arrivals:
            try:
                items.extend(arrivals)
            except Exception:
                pass
        live = None
        try:
            candidate = self._live_shape
            if (candidate is not None
                    and getattr(getattr(candidate, "page_id", None),
                                "value", None) == page_key):
                live = candidate
        except Exception:
            live = None
        if not items and live is None:
            return
        try:
            from PySide6.QtCore import QRectF

            painter.save()
            try:
                try:
                    if overlay_rect is not None:
                        # M6.8.2 gutter overlay: clip to the uncovered
                        # widget-space band instead of the page rect.
                        clip = QRectF(overlay_rect)
                    else:
                        clip = QRectF(float(page_rect.x),
                                      float(page_rect.y),
                                      float(page_rect.width),
                                      float(page_rect.height))
                    if dirty is not None:
                        clip = clip.intersected(QRectF(dirty))
                    painter.setClipRect(clip)
                except Exception:
                    painter.setClipRect(QRectF(
                        float(page_rect.x), float(page_rect.y),
                        float(page_rect.width), float(page_rect.height)))
                factor = bucket * scale
                ox = float(page_rect.x)
                oy = float(page_rect.y)
                for annotation in items:
                    if dirty is not None:
                        try:
                            bounds = self._shape_paint_rect(
                                annotation, page_rect, factor)
                        except Exception:
                            bounds = None
                        if bounds is not None and not bounds.intersects(
                                dirty):
                            continue
                    self._draw_shape(painter, annotation, ox, oy, factor)
                if live is not None:
                    # M6.7.2: the live Polygon preview paints its fill
                    # contour beneath the confirmed-edge outline and its
                    # confirmed-vertex markers on top (both preview-only).
                    self._draw_live_shape(painter, live, ox, oy, factor)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _paint_page_highlighters(self, painter, layout, entry, page_rect,
                                 scale, dirty=None, overlay_rect=None,
                                 preview_only=False) -> None:
        """Paint committed + live highlighter annotations for one page.

        M6.3: committed highlighters are read from the owning document's
        ``AnnotationStore`` (the authoritative state) — never from a
        viewer list — and the transient in-progress preview is appended
        last. Geometry, mapping (``factor = bucket * scale``), clipping,
        and the dirty-region contract mirror ``_paint_page_strokes``
        exactly, so a partial repaint composes identically to a full
        one and repeated repaints never double-blend: each translucent
        stroke is drawn once over the refilled base within
        ``dirty ∩ page``.

        M8: the brush is the highlighter cursor's vertical rectangular
        footprint, swept along the smoothed centerline (one hull per
        segment, shared math in ``annotations.ink_geometry``) and filled
        once as a single winding-fill path in the stroke's own color at
        its configured alpha. One path per stroke means overlap regions
        — including self-crossings — are rasterized once and do not
        darken, while separate strokes still blend at their intersection.
        """
        try:
            document = self._document
            if document is None:
                return
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return
        except Exception:
            return
        try:
            page_key = entry.page_id.value
        except Exception:
            return
        items: list = []
        preview = self._transform_preview
        skip = arrivals = None
        if preview_only:
            # M6.8.2 gutter overlay: only this page's move-preview
            # candidates — never the store items again (their in-page
            # part was painted by the clipped pass).
            dsts = self._transform_dsts
            if not preview or not dsts:
                return
            items = [candidate for annotation_id, candidate in
                     preview.items()
                     if dsts.get(annotation_id) == entry.index]
        else:
            # M6.8.2: cross-page routing — a committed highlighter whose
            # candidate transferred to another page is skipped here
            # (painted by the destination pass only), and candidates
            # destined for THIS page arrive after the store items
            # (matching the commit's append insertion policy).
            if preview is not None:
                try:
                    skip, arrivals = self._move_preview_route(entry.index)
                except Exception:
                    skip, arrivals = None, None
        try:
            for annotation in document.annotations.page(entry.index):
                if preview_only:
                    break
                if (isinstance(annotation, Highlighter)
                        and getattr(annotation, "points", ())):
                    # M6.8.2: an active group-move preview replaces its
                    # stored annotation (same stable id, same layer,
                    # same order).
                    if skip is not None and annotation.id in skip:
                        continue
                    if preview is not None:
                        annotation = preview.get(annotation.id, annotation)
                    items.append(annotation)
        except Exception:
            pass
        if arrivals:
            try:
                items.extend(arrivals)
            except Exception:
                pass
        try:
            live = self._live_highlighter
            if (live is not None
                    and getattr(getattr(live, "page_id", None), "value",
                                None) == page_key
                    and len(getattr(live, "points", ())) > 0):
                items.append(live)
        except Exception:
            pass
        if not items:
            return
        try:
            from PySide6.QtCore import QRectF
            from PySide6.QtGui import QPainterPath

            factor = bucket * scale
            painter.save()
            try:
                try:
                    if overlay_rect is not None:
                        # M6.8.2 gutter overlay: clip to the uncovered
                        # widget-space band instead of the page rect.
                        clip = QRectF(overlay_rect)
                    else:
                        clip = QRectF(float(page_rect.x),
                                      float(page_rect.y),
                                      float(page_rect.width),
                                      float(page_rect.height))
                    if dirty is not None:
                        clip = clip.intersected(QRectF(dirty))
                    painter.setClipRect(clip)
                except Exception:
                    painter.setClipRect(QRectF(float(page_rect.x),
                                               float(page_rect.y),
                                               float(page_rect.width),
                                               float(page_rect.height)))
                for annotation in items:
                    try:
                        base = QColor(str(getattr(annotation, "color",
                                                  "#FFFF00")))
                        if not base.isValid():
                            base = QColor("#FFFF00")
                    except Exception:
                        base = QColor("#FFFF00")
                    try:
                        base.setAlphaF(max(0.0, min(1.0, float(
                            getattr(annotation, "opacity", 0.4)))))
                    except Exception:
                        pass
                    # B.2.4: skip strokes fully outside the dirty region
                    # (conservative bbox; a missing bbox paints always).
                    # M7.1: a live morph paints fitted geometry that can lie
                    # outside the raw stroke's bbox, so its own morph rects
                    # drive invalidation and culling is bypassed.
                    morph_live = (self._shape_morph is not None
                                  and annotation is self._live_highlighter)
                    if dirty is not None and not morph_live:
                        try:
                            bounds = self._stroke_paint_rect(
                                annotation, None, float(page_rect.x),
                                float(page_rect.y), factor)
                        except Exception:
                            bounds = None
                        if bounds is not None and not bounds.intersects(
                                dirty):
                            continue
                    # M8 rectangular highlighter: the brush is the cursor's
                    # vertical borderless rectangle, not a round pen.
                    # Footprints are swept in page-local pt (shared aspect
                    # and anchoring with the cursor), mapped with the same
                    # origin + factor as every other annotation layer, and
                    # filled once as one WindingFill path: overlap regions
                    # fill exactly once (no alpha buildup), and consecutive
                    # hulls share the joint footprint (no seams or gaps).
                    hulls = ()
                    try:
                        hulls = self._hl_band_hulls(annotation)
                    except Exception:
                        hulls = ()
                    if not hulls:
                        continue
                    path = QPainterPath()
                    path.setFillRule(Qt.FillRule.WindingFill)
                    ox = float(page_rect.x)
                    oy = float(page_rect.y)
                    for ring in hulls:
                        first = True
                        for x, y in ring:
                            if first:
                                path.moveTo(ox + x * factor,
                                            oy + y * factor)
                                first = False
                            else:
                                path.lineTo(ox + x * factor,
                                            oy + y * factor)
                        if not first:
                            path.closeSubpath()
                    # Dedicated save/restore: the AA hint must never leak
                    # into the following legacy pen strokes.
                    painter.save()
                    try:
                        painter.setRenderHint(
                            QPainter.RenderHint.Antialiasing, True)
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(base)
                        painter.drawPath(path)
                    finally:
                        try:
                            painter.restore()
                        except Exception:
                            pass
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _page_pen_strokes(self, entry) -> list:
        """Committed pen ``InkStroke``s on one page, from the store.

        M6.4: the owning document's ``AnnotationStore`` is the single
        authoritative source for committed strokes — the viewer keeps no
        parallel committed-stroke list. Returned in store order; the
        transient live stroke is appended by the caller. Any failure
        (no document, unknown page) yields an empty list.
        """
        items: list = []
        try:
            document = self._document
            if document is None:
                return items
            for annotation in document.annotations.page(entry.index):
                if (isinstance(annotation, InkStroke)
                        and getattr(annotation, "points", ())):
                    items.append(annotation)
        except Exception:
            return []
        return items

    def _paint_page_strokes(self, painter, layout, entry, page_rect,
                            scale, dirty=None, overlay_rect=None,
                            preview_only=False) -> None:
        """Paint live + completed pen strokes bound to one page.

        ``dirty`` (B.2.4) bounds the work: strokes whose mapped bbox
        misses it are skipped. Order, clip, opacity, and composition
        are untouched, so partial repaints match full ones pixel for
        pixel over the region. ``None`` paints everything (legacy).
        The M6.8.2 gutter overlay (``overlay_rect`` + ``preview_only``)
        repaints ONLY this page's move-preview candidates inside the
        uncovered widget-space band, through the identical per-item
        path and mapping (``page_rect`` stays the real page rect).
        """
        try:
            bucket = float(layout.raster_scale)
            if bucket <= 0:
                return
        except Exception:
            return
        try:
            page_key = entry.page_id.value
        except Exception:
            return
        # M6.4: committed strokes come from the document store (the
        # authoritative state); only the transient live stroke is kept
        # in the viewer and appended last.
        if preview_only:
            # M6.8.2 gutter overlay: only this page's move-preview
            # candidates — never the store items again (their in-page
            # part was painted by the clipped pass).
            dsts = self._transform_dsts
            preview_only_items = self._transform_preview
            if not preview_only_items or not dsts:
                return
            items = [candidate for annotation_id, candidate in
                     preview_only_items.items()
                     if dsts.get(annotation_id) == entry.index]
        else:
            items = self._page_pen_strokes(entry)
            # M6.8.2: an active group-move preview replaces its
            # committed strokes (same stable ids, same order), with
            # cross-page routing: transferred-away slots are skipped
            # here (the destination pass paints them), and candidates
            # destined for THIS page arrive after the store items.
            preview = self._transform_preview
            if preview is not None and items:
                try:
                    ms_skip, ms_arrivals = self._move_preview_route(
                        entry.index)
                    if ms_skip:
                        items = [stroke for stroke in items
                                 if stroke.id not in ms_skip]
                    items = [preview.get(stroke.id, stroke)
                             for stroke in items]
                    if ms_arrivals:
                        items = items + ms_arrivals
                except Exception:
                    pass
        try:
            live = self._live_stroke
            if (live is not None
                    and getattr(getattr(live, "page_id", None), "value", None)
                    == page_key and len(getattr(live, "points", ())) > 0):
                items = items + [live]
        except Exception:
            return
        if not items:
            return
        try:
            from PySide6.QtCore import QPointF, QRectF
            from PySide6.QtGui import QPainterPath

            painter.save()
            try:
                # B.2.4: clip painting to dirty ∩ page. Intersecting
                # strokes repaint ONLY inside dirty (over the refilled
                # opaque base): without this, a translucent stroke's
                # full-extent repaint would double-blend over its own
                # surviving pixels outside dirty. Coverage of inside
                # pixels is geometry-only, so the clip changes nothing
                # versus a full repaint.
                try:
                    if overlay_rect is not None:
                        # M6.8.2 gutter overlay: clip to the uncovered
                        # widget-space band instead of the page rect.
                        clip = QRectF(overlay_rect)
                    else:
                        clip = QRectF(float(page_rect.x),
                                      float(page_rect.y),
                                      float(page_rect.width),
                                      float(page_rect.height))
                    if dirty is not None:
                        clip = clip.intersected(QRectF(dirty))
                    painter.setClipRect(clip)
                except Exception:
                    painter.setClipRect(QRectF(float(page_rect.x),
                                               float(page_rect.y),
                                               float(page_rect.width),
                                               float(page_rect.height)))
                factor = bucket * scale
                for stroke in items:
                    try:
                        base = QColor(str(stroke.color))
                        if not base.isValid():
                            base = QColor("#000000")
                    except Exception:
                        base = QColor("#000000")
                    try:
                        base.setAlphaF(max(0.0, min(1.0, float(
                            stroke.opacity))))
                    except Exception:
                        pass
                    # B.2.3: the live stroke paints from the incremental
                    # builder (pulled once per paint, not per event;
                    # exact match with a full rebuild, bounded cost).
                    # Anything else derives via the full path below
                    # (completed strokes, one-time cost; also the safe
                    # fallback when no live outline is available).
                    outline = None
                    # M7.1: while a hold-to-shape morph targets the live
                    # stroke, the preview is the interpolated geometry —
                    # rendered through the constant-width centerline path
                    # below (the recognized Shape is uniform-width, so the
                    # final frame already matches the commit) and never
                    # through the variable-width outline builder.
                    morph_pts = None
                    if stroke is self._live_stroke:
                        try:
                            morph_pts = self._morph_preview_points(
                                "pen", stroke)
                        except Exception:
                            morph_pts = None
                    if morph_pts is None:
                        if stroke is self._live_stroke:
                            try:
                                outline = self._interaction.live_outline()
                            except Exception:
                                outline = None
                        if outline is None:
                            outline = self._cached_outline(stroke)
                    # B.2.4: skip strokes fully outside the dirty
                    # region. A missing bbox (None) paints always.
                    # Completed outlines memoize their page-space
                    # extents (O(1) skip checks); live strokes scan
                    # once per paint and are never memoized.
                    # M7.1: morphed geometry can leave the raw stroke's
                    # bbox behind, so the morph's own rects drive
                    # invalidation and culling is bypassed.
                    if dirty is not None and morph_pts is None:
                        try:
                            if (outline is not None
                                    and stroke is not self._live_stroke):
                                memo = self._bbox_memo.get(id(outline))
                                if memo is not None and memo[0] is outline:
                                    extents = memo[1]
                                else:
                                    extents = self._outline_page_extents(
                                        outline)
                                    memo_ok = extents is not None and not (
                                        self._is_move_preview(stroke))
                                    if memo_ok:
                                        self._bbox_memo[id(outline)] = (
                                            outline, extents)
                                if extents is None:
                                    bounds = None
                                else:
                                    bounds = self._map_extents(
                                        extents, float(page_rect.x),
                                        float(page_rect.y), factor)
                            else:
                                bounds = self._live_paint_rect(
                                    stroke, outline, float(page_rect.x),
                                    float(page_rect.y), factor)
                        except Exception:
                            bounds = None
                        if bounds is not None and not bounds.intersects(
                                dirty):
                            continue
                    if outline is not None and (
                            outline.polygon or outline.dots
                            or outline.discs):
                        # M5-B.2.1 Slice 3: filled variable-width
                        # outline in page coordinates, mapped with the
                        # same origin + factor as legacy points.
                        # B.2.3: explicit winding fill — folded rails at
                        # retraced turnarounds must not leave OddEven
                        # holes; joint discs paint as filled discs.
                        # Dedicated save/restore per stroke: the AA
                        # hint must never leak into later legacy
                        # strokes (Correction 2).
                        painter.save()
                        try:
                            painter.setRenderHint(
                                QPainter.RenderHint.Antialiasing, True)
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.setBrush(base)
                            if outline.polygon:
                                poly = QPolygonF([QPointF(
                                    float(page_rect.x) + float(p.x) * factor,
                                    float(page_rect.y) + float(p.y) * factor)
                                    for p in outline.polygon])
                                path = QPainterPath()
                                path.addPolygon(poly)
                                path.setFillRule(Qt.FillRule.WindingFill)
                                painter.fillPath(path, base)
                            for dot in (list(outline.dots)
                                        + list(outline.discs)):
                                d = float(dot.radius) * factor
                                c = QPointF(
                                    float(page_rect.x)
                                    + float(dot.center.x) * factor,
                                    float(page_rect.y)
                                    + float(dot.center.y) * factor)
                                painter.drawEllipse(c, d, d)
                        finally:
                            try:
                                painter.restore()
                            except Exception:
                                pass
                        continue
                    try:
                        width_px = max(1.0, float(stroke.width_mm)
                                       * 72.0 / 25.4 * self._ui_zoom)
                    except Exception:
                        width_px = 1.0
                    pen = QPen(base)
                    pen.setWidthF(width_px)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    # B.2.2: same render-geometry smoothing as the
                    # pressure path (raw stroke untouched). B.2.10: the
                    # live stroke is served by the handler-owned
                    # incremental holder (exact-match with the full
                    # derive); a miss or None still falls back to the
                    # full derive, as before. Completed strokes use
                    # the per-stroke cache below.
                    raw = [(float(p.x), float(p.y))
                           for p in stroke.points]
                    try:
                        spts = None
                        if morph_pts is not None:
                            # M7.1: interpolated conversion frame (never
                            # cached — it changes every tick).
                            spts = morph_pts
                        elif stroke is self._live_stroke:
                            try:
                                memo = self._live_smooth_memo
                                if memo is not None and memo[0] is stroke:
                                    spts = memo[1]
                                else:
                                    spts = self._interaction.live_centerline()
                                    if spts is not None:
                                        self._live_smooth_memo = (stroke,
                                                                  spts)
                            except Exception:
                                spts = None
                        else:
                            try:
                                spts = self._off_smooth_cache.get(stroke)
                                if spts is not None:
                                    self.off_smooth_cache_hits += 1
                                else:
                                    self.off_smooth_cache_misses += 1
                            except Exception:
                                spts = None
                        if spts is None:
                            spts, _ = smooth_centerline(raw, None)
                            if (stroke is not self._live_stroke
                                    and not self._is_move_preview(stroke)):
                                try:
                                    self._off_smooth_cache[stroke] = spts
                                except Exception:
                                    pass
                    except ValueError:
                        spts = tuple(raw)
                    pts = [QPointF(float(page_rect.x) + x * factor,
                                   float(page_rect.y) + y * factor)
                           for x, y in spts]
                    # B.2.12: antialias the OFF terminal ops only. The
                    # ON branch above does the same in its own
                    # save/restore (its AA hint must not leak, and
                    # neither must this one): pen, brush, geometry,
                    # and coordinates are unchanged.
                    painter.save()
                    try:
                        painter.setRenderHint(
                            QPainter.RenderHint.Antialiasing, True)
                        if len(pts) == 1:
                            d = max(width_px, 1.0) / 2.0
                            painter.setBrush(base)
                            painter.setPen(Qt.PenStyle.NoPen)
                            painter.drawEllipse(pts[0], d, d)
                        else:
                            painter.drawPolyline(QPolygonF(pts))
                    finally:
                        try:
                            painter.restore()
                        except Exception:
                            pass
                # M7.1 Phase B: the shared Connect-Lines ring for the
                # live-adjusted recognized line is drawn last, above the
                # ink, through the same page mapping as the shape layer.
                self._paint_adjust_snap_indicator(painter, page_key,
                                                  page_rect, factor)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _paint_adjust_snap_indicator(self, painter, page_key, page_rect,
                                     factor) -> None:
        """Paint the shared Connect-Lines ring for adjusted line geometry.

        M7.1 Phase B: a live-adjusted recognized line resolves its moving
        endpoint through the same Connect-Lines context and resolver as the
        shape tools (``ShapeSnapContext`` + :func:`snap_hit`); this renders
        that resolution with the existing :meth:`_draw_snap_indicator` —
        the identical hollow accent ring, halo, radius and page-local →
        widget mapping (so zoom and page rotation are inherited, never
        reimplemented). It is a no-op unless the adjustment is active on
        this page and a genuine endpoint/intersection target is resolved;
        the evidence is paint-only and never stored.
        """
        try:
            state = self._shape_morph
            if state is None or state.get("adjust") is None:
                return
            if state.get("page_id") != page_key:
                return
            snap = state.get("snap")
            if not snap:
                return
            self._draw_snap_indicator(painter, snap,
                                      float(page_rect.x),
                                      float(page_rect.y), factor)
        except Exception:
            pass

    def _paint_selection_frame(self, painter, dirty=None) -> None:
        """Paint the single shared selection frame (M6.8.1).

        Exactly ONE dashed rectangle encloses the whole selection group:
        the union of every selected annotation's rendered extent, each
        mapped through *its own* page transform, then padded outward so
        the loop never touches the enclosed geometry. One annotation
        yields one frame; several yield one frame around all of them
        (intervening empty space included); a cross-page selection still
        yields one frame in viewer coordinates. It is paint-only: no
        annotation geometry, colour, opacity, or stored data is touched,
        and the frame is never stored.

        The loop animates: a marching-ants dash offset plus a bounded
        opacity pulse (see :meth:`_selection_frame_pen`). It is clipped to
        the viewer viewport, so a frame larger than the window is simply
        cropped rather than split per page.
        """
        try:
            if not self._selection_frame_visible():
                return
            corners = self._selection_frame_corners()
            if corners is None:
                return
        except Exception:
            return
        try:
            from PySide6.QtCore import QPointF, QRectF

            painter.save()
            try:
                clip = QRectF(dirty) if dirty is not None else QRectF(self.rect())
                painter.setClipRect(clip)
                painter.setRenderHint(
                    QPainter.RenderHint.Antialiasing, True)
                painter.setPen(self._selection_frame_pen())
                painter.setBrush(Qt.BrushStyle.NoBrush)
                # The frame is the *oriented* quad (a rotated selection
                # draws a rotated frame), never the axis-aligned bounds of
                # the rotated contents.
                painter.drawPolygon(QPolygonF(
                    [QPointF(float(x), float(y)) for x, y in corners]))
                # M6.8.2 Stage 4: interactive grips appear whenever the
                # frame is displayed - under every tool, including a
                # context-menu selection made while Pen/Pan/Eraser is
                # active - but never while a marquee/lasso is being drawn.
                if self._region_gesture is None:
                    self._paint_selection_handles(painter)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def _paint_selection_handles(self, painter) -> None:
        """Paint the eight resize grips and the rotation grip (Stage 4).

        Paint-only, inside the caller's already-clipped, antialiased
        painter: small accent-filled squares at the oriented frame's
        corners and edge midpoints, a *short, fixed-screen-length*
        connector from the top-edge midpoint, and a filled accent circle
        at its end. Grips follow the oriented frame, so they rotate with
        it. The rotation grip and its connector (the "rotation centre")
        are suppressed for the whole of a live *resize* or *move* — an
        edge/corner drag or a body/group move must not trail a rotating
        affordance across the canvas — and restored the instant the
        gesture ends (the decision reads only the live gesture, so no
        stale state can linger). A *rotation* gesture keeps them: the
        grip is the affordance being dragged. Never mutates or stores
        anything.
        """
        points = self._selection_handle_points()
        if points is None:
            return
        gesture = self._move_gesture
        # M6.8.2 Stage 4 visual bugfix: hide the rotation-centre visuals
        # (grip circle + connector) while the selection is being moved or
        # resized. Rotation itself keeps them.
        mode = gesture.get("mode") if gesture is not None else None
        suppress_rotate_grip = mode in ("move", "resize", "vertex")
        try:
            from PySide6.QtCore import QPointF, QRectF

            # Resize grips first: filled squares with a darker edge.
            half = _HANDLE_SIZE_PX / 2.0
            edge = QPen(QColor(_SELECT_ACCENT).darker(150))
            edge.setWidthF(1.0)
            for name in _HANDLE_NAMES:
                hx, hy = points[name]
                painter.setPen(edge)
                painter.setBrush(_SELECT_ACCENT)
                painter.drawRect(QRectF(hx - half, hy - half,
                                        _HANDLE_SIZE_PX, _HANDLE_SIZE_PX))
            # M7.1.4.2: round handles mark a single selected shape's own
            # defining points (ordered vertices / box corners + edge
            # midpoints), distinct from the square frame grips so the two
            # affordances are never confused.
            self._paint_vertex_handles(painter)
            if suppress_rotate_grip:
                return
            # Rotation grip: a short connector from the oriented top-edge
            # midpoint out to the filled circle. The connector is exactly
            # the grip offset (a constant screen length) and can never
            # originate at the frame centre or grow with the frame.
            top_x, top_y = points["n"]
            rx, ry = points["rotate"]
            grip_pen = QPen(_SELECT_ACCENT)
            grip_pen.setWidthF(1.5)
            grip_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(grip_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QPointF(top_x, top_y), QPointF(rx, ry))
            painter.setBrush(_SELECT_ACCENT)
            painter.drawEllipse(QPointF(rx, ry),
                                _ROTATE_HANDLE_RADIUS_PX,
                                _ROTATE_HANDLE_RADIUS_PX)
        except Exception:
            pass

    def _paint_vertex_handles(self, painter) -> None:
        """Paint a single selected shape's editable vertex/box handles.

        Paint-only, in the caller's clipped antialiased painter. Round
        accent dots sit exactly on the displayed geometry (the live
        preview candidate while dragging), so a rotated box's handles
        rotate with it and a dragged vertex visibly follows the pointer.
        Never mutates or stores anything.
        """
        info = self._editable_vertex_target()
        if info is None:
            return
        try:
            from PySide6.QtCore import QPointF

            edge = QPen(QColor(_SELECT_ACCENT).darker(150))
            edge.setWidthF(1.0)
            painter.setPen(edge)
            painter.setBrush(_SELECT_ACCENT)
            for hx, hy in info[2].values():
                painter.drawEllipse(
                    QPointF(hx, hy),
                    _VERTEX_HANDLE_RADIUS_PX, _VERTEX_HANDLE_RADIUS_PX)
        except Exception:
            pass

    # -- M6.8.1 live region overlay (marquee / lasso preview) ---------------

    def _paint_region_gesture_overlay(self, painter, dirty=None) -> None:
        """Paint the transient marquee or lasso outline while dragging.

        A Selection press holds and drags a live rectangle; a Lasso
        press draws a live freehand loop. The overlay is widget-space
        (it can span pages and gaps), paint-only, and vanishes the
        moment the gesture commits — the resulting selection is drawn
        afterwards as one shared frame by
        :meth:`_paint_selection_frame`. Dashed accent, never filled, so
        it can never be mistaken for committed ink.
        """
        try:
            if self._region_gesture is None:
                return
            points = self._region_gesture.get("points") or ()
            if len(points) < 2:
                return
            is_rect = self._region_gesture.get("kind") == "rect"
        except Exception:
            return
        try:
            from PySide6.QtCore import QPointF, QRectF
            from PySide6.QtGui import QPainterPath

            painter.save()
            try:
                clip = QRectF(dirty) if dirty is not None else None
                if clip is not None:
                    painter.setClipRect(clip)
                pen = QPen(_SELECT_ACCENT)
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setRenderHint(
                    QPainter.RenderHint.Antialiasing, True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if is_rect:
                    ax, ay = points[0]
                    bx, by = points[-1]
                    painter.drawRect(QRectF(
                        min(ax, bx), min(ay, by),
                        abs(bx - ax), abs(by - ay)))
                else:
                    # The live loop is shown closed (start joined to the
                    # current end) even mid-drag.
                    loop = list(points) + [points[0]]
                    path = QPainterPath()
                    path.moveTo(QPointF(loop[0][0], loop[0][1]))
                    for px, py in loop[1:]:
                        path.lineTo(QPointF(px, py))
                    painter.drawPath(path)
            finally:
                try:
                    painter.restore()
                except Exception:
                    pass
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        """Widget-level close releases rendering resources."""
        try:
            self._remove_ink_gesture_filter()
        except Exception:
            pass
        try:
            self._remove_native_cursor_filter()
        except Exception:
            pass
        try:
            self._stop_selection_animation()
        except Exception:
            pass
        try:
            self.close_document()
        finally:
            super().closeEvent(event)

    # -- Windows Ink press-and-hold suppression (B.2.0.2 Stage 2) ---------

    def showEvent(self, event) -> None:  # noqa: N802
        """Install the Ink gesture filter once the window exists."""
        try:
            super().showEvent(event)
        except Exception:
            pass
        self._install_ink_gesture_filter()
        self._disable_pen_tap_feedback()
        # M6.8.1: resume the frame animation if a selection is still held.
        self._sync_selection_animation()
        # M7.1.4.2.2: cursor ownership needs the native window handle,
        # which exists from the first show onward.
        self._install_native_cursor_filter()

    # -- WM_SETCURSOR cursor ownership (M7.1.4.2.2) ------------------------

    def _install_native_cursor_filter(self) -> None:
        """Answer ``WM_SETCURSOR`` for this window; see ``_CursorResetFilter``.

        Idempotent; a no-op on non-Windows platforms or before the native
        handle exists (the next ``showEvent`` retries). The filter lives
        as long as the application — a stylus can re-enter the window at
        any time, and uninstalling on hide would recreate the exact gap
        this repairs.
        """
        if sys.platform != "win32":
            return
        if self._cursor_reset_filter is not None:
            return
        top = self.window()
        if not isinstance(top, QWidget) or top.windowHandle() is None:
            return
        try:
            top.winId()
        except Exception:
            return
        try:
            self._cursor_reset_filter = _CursorResetFilter(self)
            QApplication.instance().installNativeEventFilter(
                self._cursor_reset_filter)
        except Exception:
            self._cursor_reset_filter = None

    def _remove_native_cursor_filter(self) -> None:
        """Balanced uninstall (viewer teardown); the filter is otherwise
        applied for the application's lifetime."""
        filt = self._cursor_reset_filter
        if filt is None:
            return
        self._cursor_reset_filter = None
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(filt)
        except Exception:
            pass

    def derive_pointer_cursor_from_global(self, gx: int, gy: int) -> bool:
        """Derive the pointer cursor for a native (screen) point.

        M7.1.4.2.3: called by the ``WM_SETCURSOR`` answerer at the
        instant the pointer enters or moves within the window, so the
        cursor shown on re-entry is derived for the arrival point
        instead of showing the last value from before the stylus
        (or mouse) left. Mirrors the ``tabletEvent`` routing exactly —
        chrome hover first, then the shared canvas derivation over
        :meth:`_update_selection_cursor` and the tool cursor — using
        the same helpers and the same bounded cursor caches; no new
        state, no timing, no override cursor. Returns whether a
        derivation ran; points outside this viewer's geometry (for
        example other chrome bands) do not, and the caller then keeps
        its previous behaviour.
        """
        try:
            local = self.mapFromGlobal(QPoint(int(gx), int(gy)))
        except Exception:
            return False
        try:
            if (local.x() < 0 or local.y() < 0
                    or local.x() >= self.width()
                    or local.y() >= self.height()):
                return False
        except Exception:
            return False
        try:
            target = self._tablet_chrome_target(local.x(), local.y())
        except Exception:
            target = None
        if target is not None:
            self._apply_chrome_cursor(target)
            return True
        self._derive_pointer_cursor(local.x(), local.y())
        return True

    def hideEvent(self, event) -> None:  # noqa: N802
        """Suspend the frame animation while the viewer is not visible."""
        try:
            self._pan_last_tap = None
            self._pan_tablet_open = False
            self._pan_tablet_dbl = False
            self._tablet_hover_pt = None
            # M6.8.2: a hidden viewer abandons an active group move (the
            # pointer stream is gone with it).
            self._move_cancel()
        except Exception:
            pass
        try:
            self._stop_selection_animation()
        finally:
            try:
                super().hideEvent(event)
            except Exception:
                pass

    def _disable_pen_tap_feedback(self) -> bool:
        """Suppress pen tap/double-tap ripples on this top-level window.

        Post-Slice-3 fix C: cosmetic only (delivery, dblclick
        synthesis, hold suppression untouched). Windows-only, safe
        no-op elsewhere or on failure. Unlike the gesture filter this
        API is per-HWND, so toolbar ripples go too — function preserved.
        """
        try:
            if sys.platform != "win32":
                return False
            top_hwnd = self._ink_gesture_top_hwnd()
            if not top_hwnd:
                return False
            from app import ink_gesture as _ig

            return bool(_ig.disable_pen_tap_feedback(top_hwnd))
        except Exception:
            return False

    def _ink_gesture_top_hwnd(self) -> int:
        """Current top-level HWND, resolved live (never cached)."""
        try:
            top = self.window()
            return int(top.winId()) if top is not None else 0
        except Exception:
            return 0

    def _ink_canvas_hit(self, screen_x: int, screen_y: int) -> bool:
        """True only over bare canvas (no chrome under the pen)."""
        try:
            local = self.mapFromGlobal(QPoint(int(screen_x),
                                              int(screen_y)))
            if not self.rect().contains(local):
                return False
            return self.childAt(local) is None
        except Exception:
            return False

    def _ink_gesture_decide(self, msg_hwnd: int, sx: int, sy: int) -> bool:
        """Suppress the hold gesture only on bare canvas of our window."""
        try:
            from app import ink_gesture as _ig

            top_hwnd = self._ink_gesture_top_hwnd()
            ok, _flags = _ig.gesture_query_decision(
                _ig.WM_TABLET_QUERYSYSTEMGESTURESTATUS, msg_hwnd, sx, sy,
                top_hwnd, self._ink_canvas_hit(sx, sy))
            return bool(ok)
        except Exception:
            return False

    def _install_ink_gesture_filter(self) -> bool:
        """Install once on Windows; safe no-op elsewhere or on failure."""
        try:
            if getattr(self, "_ink_gesture_filter", None) is not None:
                return True
            if sys.platform != "win32":
                return False
            if not self._ink_gesture_top_hwnd():
                return False
            from app import ink_gesture as _ig

            filt = _ig.make_native_filter(self._ink_gesture_decide)
            app = QApplication.instance()
            if app is None:
                return False
            app.installNativeEventFilter(filt)
            self._ink_gesture_filter = filt
            return True
        except Exception:
            try:
                self._ink_gesture_filter = None
            except Exception:
                pass
            return False

    def _remove_ink_gesture_filter(self) -> None:
        """Remove the filter; never fails, never retains the viewer."""
        try:
            filt = getattr(self, "_ink_gesture_filter", None)
            self._ink_gesture_filter = None
            if filt is None:
                return
            try:
                app = QApplication.instance()
                if app is not None:
                    app.removeNativeEventFilter(filt)
            except Exception:
                pass
        except Exception:
            pass
