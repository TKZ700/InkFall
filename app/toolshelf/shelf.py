"""Floating tool shelf (M5-A1 native refinement). Drawboard-density.

Floating bounded child of ``PdfViewer`` (never inside ``ViewerBar``),
survives fullscreen, docks left/right/top/bottom. Layout per dock:

    [Pan][Selection] | [customs] | [Add][Move]

Move is a clean circular Inkfall app-icon handle with zero dot
decoration, directly below Add (vertical) / after Add (horizontal).

* handle: holdable Inkfall app-icon cell (reposition affordance, never
  a tool, never navigation)
* slots: native Inkfall cells (:class:`SlotButton`, single-pass token
  paint)
* add: ``+`` cell opening the anchored AddToolGrid popup

Slot actions use the native Inkfall menu pattern (``QMenu`` +
``fx.animate_menu_open``). Double-click a configurable slot opens the
anchored properties popup; Pan/Lasso only flash press feedback.

M6.9 / M6.9.3: the *selected* slot also carries an integrated three-dot
capsule (:class:`~app.toolshelf.capsule.ToolCapsule`) — a mouse-transparent
overlay child of the shelf, so it never reflows the toolbar and never
steals a click. It wraps the selected tool's own circular button and
paints a separate black circular three-dot button directly beneath it,
which opens the same properties popup. Hidden for slots without editable
properties.

The control is slot-local: it is sized and positioned from the selected
slot's own cell (:func:`~app.toolshelf.capsule.capsule_size` plus
:func:`~app.toolshelf.capsule.capsule_advance`), so the toolbar keeps
its original pitch and no neighbouring slot ever moves. Only the
options *face* may visually graze the next slot's empty padding;
the options pointer target stays inside the selected cell plus the
inter-slot gap (:class:`~app.toolshelf.capsule.CapsuleOptionsButton`), so
clicks always resolve to the tool actually being aimed at.

M6.9.6: while the capsule is shown it makes room for itself in the real
layout. The selected slot's own cell grows along the toolbar axis by the
capsule's overhang (``_reserved_extent``), so the box layout genuinely
moves every following tool down by that amount — their CELL size, their
normal spacing to one another and their own hit targets are untouched,
and nothing is compressed or overlapped. The selected tool itself rises
``SEPARATION_ACTIVE`` by a paint-only content offset. Both effects animate
with the capsule's own arrival/departure and collapse back completely when
it hides, so a capsule-free toolbar is exactly the plain 43px pitch
(40px cell + 3px gap).

No compact mode. Never imports rendering internals.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QBoxLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from app import fx as _fx
from app.icons import app_icon
from app.icons import ink_center_offset as _ink_offset
from app.icons import pixmap as _icon_pixmap
from app.theme.tokens import InkfallTokens
from app.toolshelf.capsule import (
    CAPSULE_DOTS_D,
    CAPSULE_PAD,
    CAPSULE_TOOL_D,
    CapsuleOptionsButton,
    ToolCapsule,
    capsule_advance,
    capsule_size,
)
from app.toolshelf.slot_button import CELL, SlotButton
from app.toolshelf.store import DOCK_POSITIONS, ToolShelfStore
from app.toolshelf.surfaces import hover_wash
from tools.models import ToolInstance, has_editable_properties, is_core_tool

__all__ = ["DRAG_THRESHOLD_PX", "HOLD_MS", "ToolShelf"]

_MARGIN = 5
#: Core/end-section rhythm (Pan, Selection, boundaries, Add, Move).
#: Also the custom-slot rhythm: the selected-tool control is slot-local
#: (it fits the selected cell plus this gap), so no slot needs a wider
#: reserved pitch and selecting a tool never moves its neighbours.
_SPACING = 3

#: Small, LOCAL, animated separation while the capsule is shown.
#: ``SEPARATION_ACTIVE`` is the selected tool's own paint-only content
#: rise (its cell never moves; the capsule is derived from the lifted
#: circle, so the top cap stays concentric with the tool's halo).
#: ``SEPARATION_NEIGHBOUR`` is now redundant: the capsule's room is
#: reserved in the real layout instead (the selected cell grows by
#: ``_reserved_extent()``, so every following tool genuinely moves down),
#: and stacking the old paint-only drop on top of that would be double
#: movement. Kept defined because other modules reference it.
SEPARATION_ACTIVE = 6
SEPARATION_NEIGHBOUR = 12
#: Duration of the separation glide (matches the capsule's own entrance).
SEPARATION_MS = 170

#: Along-axis size of each morphing boundary (separator line idle,
#: tail-less chevron when overflow exists). Tight: the boundary sits
#: close to the adjacent custom tools rather than floating in gaps.
_EDGE_ALONG = 14

#: Separator half-span (px) at rest and chevron arm half-span at full
#: reveal: the single morphing path gathers the 24px line into the
#: compact arrow (and spreads back) as the reveal travels 0 -> 1.
_MORPH_HALF_REST = 12.0
_MORPH_HALF_ARROW = 5.0
#: Chevron tip travel (px) along the boundary axis at full reveal.
#: 5 + 1px pen = 6 <= 7px half-budget: the tip never touches the edge.
_MORPH_TIP_TRAVEL = 5.0


def _morph_path_points(
    reveal: float, horizontal: bool, forward: bool, cx: float, cy: float
):
    """Single-path morph geometry: endpoints A/B + tip T + pen width.

    At reveal 0 the three points are collinear on the separator axis
    (a plain line); as reveal grows the tip slides along-axis while the
    endpoints gather toward the chevron arm span, so the separator
    itself bends into the arrow — never a line with an arrow on top.
    Pure function of its inputs (unit-testable, no widgets).
    """
    try:
        r = max(0.0, min(1.0, float(reveal)))
    except Exception:
        r = 1.0
    half = _MORPH_HALF_REST + (_MORPH_HALF_ARROW - _MORPH_HALF_REST) * r
    tip = _MORPH_TIP_TRAVEL * r
    direction = 1.0 if forward else -1.0
    if horizontal:
        ax, ay = cx, cy - half
        tx, ty = cx + direction * tip, cy
        bx, by = cx, cy + half
    else:
        ax, ay = cx - half, cy
        tx, ty = cx, cy + direction * tip
        bx, by = cx + half, cy
    return (ax, ay), (tx, ty), (bx, by), 1.0 + r


#: Visible custom-tool window before the custom region scrolls.
#: Adaptive: derived from the actual parent canvas budget (floor 8 so
#: small windows stay compact, ceiling 12 so the shelf never sprawls),
#: keeping the thin silhouette — overflow scrolls instead of widening.
MIN_VISIBLE_CUSTOMS = 8
MAX_VISIBLE_CUSTOMS = 12
#: Share of the parent along-axis the custom strip may occupy.
_CUSTOM_BUDGET_FRACTION = 0.55

#: Press-and-hold duration entering reposition mode.
HOLD_MS = 275
#: Pointer travel cancelling a pending hold (plain click path).
DRAG_THRESHOLD_PX = 8

#: Minimum separator-line vs shelf-surface contrast. A 1px divider needs
#: more than the icon-ink floor (2.2): dark ``border_subtle`` measured
#: 1.03:1 against ``surface_raised`` (light 1.26:1), so the always-painted
#: idle line was imperceptible and the line-to-chevron morph read as a
#: chevron fading in from empty space.
_SEPARATOR_MIN_CONTRAST = 2.6


def _separator_color(tokens: InkfallTokens) -> QColor:
    """1px boundary-line tone derived from existing theme tokens.

    ``border_subtle`` is unusable here (near-identical to the shelf's own
    ``surface_raised``). Instead the subtlest blend of ``text_muted`` /
    ``text_secondary`` toward ``surface_raised`` that still measures at
    least ``_SEPARATOR_MIN_CONTRAST`` is returned — no new token, no
    hardcoded theme color; odd token sets fall back to the strongest
    candidate. The result is opaque (no compositing surprises).
    """
    try:
        bg_hex = str(tokens.surface_raised)
        bg = QColor(bg_hex)
        if not bg.isValid():
            raise ValueError(bg_hex)
    except Exception:
        return QColor(126, 134, 152, 255)
    try:
        from app.icons import contrast_ratio as _cr
    except Exception:
        _cr = None
    candidates: list[QColor] = []
    try:
        for attr, alphas in (
            ("text_muted", (0.70, 0.85, 1.0)),
            ("text_secondary", (0.55, 0.70, 0.85, 1.0)),
        ):
            try:
                fg = QColor(str(getattr(tokens, attr)))
            except Exception:
                continue
            if not fg.isValid():
                continue
            for a in alphas:
                try:
                    candidates.append(
                        QColor(
                            int(round(fg.red() * a + bg.red() * (1.0 - a))),
                            int(round(fg.green() * a + bg.green() * (1.0 - a))),
                            int(round(fg.blue() * a + bg.blue() * (1.0 - a))),
                        )
                    )
                except Exception:
                    continue
    except Exception:
        pass
    if not candidates:
        return QColor(126, 134, 152, 255)
    if _cr is not None:
        for color in candidates:
            try:
                if _cr(color.name(), bg_hex) >= _SEPARATOR_MIN_CONTRAST:
                    return color
            except Exception:
                continue
    return candidates[-1]


class _HandleCell(QAbstractButton):
    """Holdable Inkfall-icon reposition handle (not a tool)."""

    holdStarted = Signal()
    dragMoved = Signal(object)
    dragReleased = Signal()
    dragCancelled = Signal()
    pressCancelled = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(CELL, CELL)
        self.setAccessibleName("Reposition toolbar")
        self.setAccessibleDescription("Press and hold, then drag to a docking target")
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(HOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        self._press_pos = None
        self._holding = False
        self._dragging = False

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def _on_hold_timeout(self) -> None:
        if self._press_pos is None or self._dragging:
            return
        self._holding = True
        try:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        except Exception:
            pass
        # Grab the mouse: fast drags must keep flowing to the handle even
        # when the pointer leaves its small cell mid-gesture.
        try:
            self.grabMouse()
        except Exception:
            pass
        self.update()
        self.holdStarted.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self._press_pos = event.position().toPoint()
                self._holding = False
                self._dragging = False
                self._hold_timer.start()
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        try:
            if self._press_pos is not None:
                delta = event.position().toPoint() - self._press_pos
                if (
                    not self._holding
                    and not self._dragging
                    and (abs(delta.x()) + abs(delta.y())) >= DRAG_THRESHOLD_PX
                ):
                    # Moved before hold elapsed: plain click path, no dock.
                    self._hold_timer.stop()
                    self._press_pos = None
                    self.pressCancelled.emit()
                    event.accept()
                    return
                if self._holding and not self._dragging:
                    self._dragging = True
                    self.update()
                if self._dragging:
                    self.dragMoved.emit(event.globalPosition().toPoint())
                    event.accept()
                    return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        try:
            was_dragging = self._dragging
            self._hold_timer.stop()
            self._press_pos = None
            self._holding = False
            self._dragging = False
            try:
                self.releaseMouse()
            except Exception:
                pass
            try:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            except Exception:
                pass
            self.update()
            # Short click: feedback only — never navigation/selection.
            self.dragReleased.emit()
            event.accept()
            if not was_dragging:
                return
            return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        """Esc during hold/drag cancels repositioning (reverts)."""
        try:
            if event.key() == Qt.Key.Key_Escape and (self._holding or self._dragging):
                self._hold_timer.stop()
                self._press_pos = None
                self._holding = False
                self._dragging = False
                try:
                    self.releaseMouse()
                except Exception:
                    pass
                try:
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                except Exception:
                    pass
                self.update()
                self.dragCancelled.emit()
                event.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    @property
    def is_dock_dragging(self) -> bool:
        return self._dragging

    def paintEvent(self, event) -> None:
        t = self._tokens
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            # Float center: QRect.center() truncates on even sizes
            # (40px reports 19, true 20.0) — keep the circle on the
            # true cell center.
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            # Black circular move surface (end-cap twin of Add):
            # fixed near-black body both themes, hold/drag answers
            # with an accent ring inside the same circle.
            body = QColor("#101216")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(body)
            painter.drawEllipse(cx - 15, cy - 15, 30, 30)
            if self._dragging or self._holding or self.isDown():
                try:
                    edge = QColor(t.accent)
                except Exception:
                    edge = QColor("#8B93E0")
                pen = QPen(edge)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(cx - 15, cy - 15, 30, 30)
            elif self.underMouse():
                try:
                    hover = QColor(t.surface_overlay)
                except Exception:
                    hover = QColor(255, 255, 255, 22)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover)
                painter.drawEllipse(cx - 13, cy - 13, 26, 26)
            # App mark only — zero dot/grip decoration by design.
            # Affordance is hold/drag interaction + circular hover/press,
            # never painted dots. Icon centered in the 30px circle.
            mark = app_icon().pixmap(20, 20)
            if not mark.isNull():
                painter.drawPixmap(
                    int(round(cx - mark.width() / 2.0)),
                    int(round(cy - mark.height() / 2.0)),
                    mark,
                )
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _AddCell(QAbstractButton):
    """``+`` affordance opening the AddToolGrid popup (not a tool)."""

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(CELL, CELL)
        self.setAccessibleName("Add tool")
        self.setAccessibleDescription("Open the Add Tool picker")
        self._glyph: QPixmap | None = None

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._glyph = None
        self.update()

    def paintEvent(self, event) -> None:
        t = self._tokens
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            # Float center (see _HandleCell): circle and glyph share
            # the true cell center.
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            # Black circular action surface (theme-safe by construction:
            # fixed near-black body, fixed near-white glyph, both themes).
            body = QColor("#101216")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(body)
            painter.drawEllipse(cx - 15, cy - 15, 30, 30)
            if self.isDown() or self.underMouse():
                # Same filled hover language as the tool slots and the
                # capsule's options disc: one restrained ink wash, never
                # the accent ring this used to stroke.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover_wash(t, pressed=self.isDown()))
                painter.drawEllipse(cx - 15, cy - 15, 30, 30)
            if self._glyph is None or self._glyph.isNull():
                try:
                    self._glyph = _icon_pixmap("plus", "#F2F3F7", 20)
                except Exception:
                    self._glyph = QPixmap()
            if not self._glyph.isNull():
                try:
                    _dx, _dy = _ink_offset(self._glyph)
                except Exception:
                    _dx, _dy = (0.0, 0.0)
                x = cx - self._glyph.width() / 2 + _dx
                y = cy - self._glyph.height() / 2 + _dy
                painter.drawPixmap(int(round(x)), int(round(y)), self._glyph)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _ScrollInner(QWidget):
    """Inner widget of the custom viewport: reports every applied layout.

    Reserving the capsule's run grows one tile's cell, so the box layout
    moves the tiles — asynchronously, when this widget handles its own
    layout request. Anything anchored to a tile's frame (the capsule and
    its options target) therefore has to be re-anchored *when that layout
    lands*, not when the size was requested; this widget calls back right
    after its layout has been applied.
    """

    def __init__(self, parent=None, on_layout=None) -> None:
        super().__init__(parent)
        self._on_layout = on_layout

    def event(self, event) -> bool:  # noqa: N802
        try:
            handled = bool(super().event(event))
        except Exception:
            handled = False
        try:
            if (event.type() == QEvent.Type.LayoutRequest
                    and self._on_layout is not None):
                # Deterministic ordering: this pass is what places the
                # tiles, so apply it before anything re-anchors to a tile's
                # own frame (idempotent if it was already applied).
                layout = self.layout()
                if layout is not None:
                    layout.activate()
                self._on_layout()
        except Exception:
            pass
        return handled


class _CustomScroll(QScrollArea):
    """Bounded custom-tool viewport: wheel scroll, no visible bars."""

    def __init__(self, horizontal: bool, parent=None) -> None:
        super().__init__(parent)
        self._horizontal = bool(horizontal)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("background: transparent; border: none;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        inner = _ScrollInner(self, getattr(parent, "_reposition_capsule", None))
        inner.setStyleSheet("background: transparent; border: none;")
        layout = QBoxLayout(
            QBoxLayout.Direction.LeftToRight
            if self._horizontal
            else QBoxLayout.Direction.TopToBottom,
            inner,
        )
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_SPACING)
        inner.setLayout(layout)
        self._inner_layout = layout
        self.setWidget(inner)

    def set_horizontal(self, horizontal: bool) -> None:
        self._horizontal = bool(horizontal)
        self._inner_layout.setDirection(
            QBoxLayout.Direction.LeftToRight
            if self._horizontal
            else QBoxLayout.Direction.TopToBottom
        )

    def wheelEvent(self, event) -> None:
        """Route wheel/trackpad deltas to the along-axis scrollbar."""
        try:
            from app import fx as _fx

            delta = event.angleDelta()
            steps = delta.y() if delta.y() != 0 else delta.x()
            bar = (
                self.horizontalScrollBar()
                if self._horizontal
                else self.verticalScrollBar()
            )
            target = bar.value() - steps // 8
            try:
                parent = self.parentWidget()
                try:
                    settle = parent._sync_overflow_edges
                except AttributeError:
                    settle = None
                _fx.smooth_scroll_to(
                    bar, target, 120, parent or self, on_finished=settle
                )
            except Exception:
                bar.setValue(target)
            event.accept()
            return
        except Exception:
            pass
        super().wheelEvent(event)


class _OverflowEdge(QAbstractButton):
    """Boundary between shelf sections: separator line morphing into a
    directional scroll arrow.

    Idle (no overflow in its direction): a subtle separator line that
    matches the toolbar's divider language. Overflow: an animated
    tail-less chevron pointing where more tools exist; clicking steps
    the custom region by a page. At the scroll end it collapses back
    to the line — no oversized control ever remains.
    """

    stepRequested = Signal(int)

    def __init__(
        self, forward: bool, horizontal: bool, tokens: InkfallTokens, parent=None
    ) -> None:
        super().__init__(parent)
        self._forward = bool(forward)
        self._horizontal = bool(horizontal)
        self._tokens = tokens
        self._has_more = False
        self._reveal = 0.0
        # Glide target currently being animated toward (None = settled).
        # The reveal animation is (re)started only when the desired state
        # flips or the in-flight glide heads the wrong way — never once
        # per scrollbar tick (that restarted the 140ms glide every frame
        # and stranded the chevron mid-morph on long scrolls).
        self._reveal_target: float | None = None
        self._reveal_anim = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(
            "Scroll tools forward" if forward else "Scroll tools back"
        )
        self.clicked.connect(self._on_clicked)
        self.toggled.connect(lambda _c: self.update())
        self._apply_size()

    def _apply_size(self) -> None:
        if self._horizontal:
            self.setFixedSize(_EDGE_ALONG, CELL)
        else:
            self.setFixedSize(CELL, _EDGE_ALONG)

    def set_horizontal(self, horizontal: bool) -> None:
        self._horizontal = bool(horizontal)
        self._apply_size()
        self.update()

    def has_more(self) -> bool:
        return self._has_more

    def set_overflow(self, has_more: bool) -> None:
        # Desired state derives purely from the caller's live scrollbar
        # reading (see _sync_overflow_edges) — never from _reveal.
        has_more = bool(has_more)
        target = 1.0 if has_more else 0.0
        if has_more == self._has_more and (
            self._reveal_target == target
            or (self._reveal_target is None and self._reveal == target)
        ):
            return
        self._has_more = has_more
        self._glide_to(target)

    def _snap_reveal(self, target: float) -> None:
        """Settle the boundary instantly (reduced motion / fallback)."""
        try:
            if self._reveal_anim is not None:
                self._reveal_anim.stop()
        except Exception:
            pass
        self._reveal = max(0.0, min(1.0, float(target)))
        self._reveal_target = None
        self._reveal_anim = None
        self.update()

    def _glide_to(self, target: float) -> None:
        target = float(target)
        try:
            from app import fx as _fx

            reduce = bool(_fx.reduced_motion_enabled())
        except Exception:
            reduce = False
        if reduce:
            self._snap_reveal(target)
            return
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation

            if self._reveal_anim is not None:
                try:
                    self._reveal_anim.stop()
                except Exception:
                    pass
            self._reveal_target = target
            anim = QVariantAnimation(self)
            anim.setDuration(140)
            anim.setStartValue(float(self._reveal))
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            anim.valueChanged.connect(self._on_reveal_tick)
            anim.finished.connect(
                lambda _t=target, _a=anim: self._on_reveal_done(_t, _a)
            )
            self._reveal_anim = anim
            anim.start()
        except Exception:
            self._snap_reveal(target)

    def _on_reveal_done(self, target: float, anim) -> None:
        """Snap a naturally-finished glide to its exact terminal state."""
        try:
            if self._reveal_anim is not anim:
                return  # superseded mid-flight; a newer glide owns state
            self._reveal = max(0.0, min(1.0, float(target)))
            self._reveal_target = None
            self._reveal_anim = None
        except Exception:
            pass
        self.update()

    def _on_reveal_tick(self, value) -> None:
        try:
            self._reveal = max(0.0, min(1.0, float(value)))
        except Exception:
            self._reveal = 1.0
        self.update()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def _on_clicked(self) -> None:
        try:
            self.stepRequested.emit(1 if self._forward else -1)
        except Exception:
            pass

    def paintEvent(self, event) -> None:
        t = self._tokens
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            # Float center: the 14px boundary's true along-center is
            # 7.0 (QRect.center() reports 6) — line and chevron share it.
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            # Single morphing path (M5-A2.1): the separator line itself
            # bends into the directional chevron and straightens back —
            # never a stationary line with an arrow drawn over it. Tone
            # blends from the measured separator color to the accent, and
            # the stroke grows 1px -> 2px along the same reveal.
            try:
                line_color = _separator_color(t)
            except Exception:
                try:
                    line_color = QColor(t.text_secondary)
                except Exception:
                    line_color = QColor(126, 134, 152, 255)
            try:
                accent = QColor(t.accent)
                if not accent.isValid():
                    raise ValueError(accent)
            except Exception:
                accent = QColor("#8B93E0")
            try:
                r = max(0.0, min(1.0, float(self._reveal)))
            except Exception:
                r = 0.0
            morph = QColor(
                int(round(line_color.red() * (1.0 - r) + accent.red() * r)),
                int(round(line_color.green() * (1.0 - r) + accent.green() * r)),
                int(round(line_color.blue() * (1.0 - r) + accent.blue() * r)),
            )
            (ax, ay), (tx, ty), (bx, by), width = _morph_path_points(
                r, self._horizontal, self._forward, cx, cy
            )
            pen = QPen(morph)
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(
                int(round(ax)), int(round(ay)), int(round(tx)), int(round(ty))
            )
            painter.drawLine(
                int(round(tx)), int(round(ty)), int(round(bx)), int(round(by))
            )
            # Circular hover/press halo (only meaningful as a button).
            if self._reveal > 0.05 and self.isEnabled():
                down = False
                try:
                    down = bool(self.isDown())
                except Exception:
                    pass
                if down or self.underMouse():
                    try:
                        wash = QColor(t.surface_overlay)
                    except Exception:
                        wash = QColor(255, 255, 255, 22)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(wash)
                    side = min(rect.width(), rect.height()) - 6
                    painter.drawEllipse(cx - side // 2, cy - side // 2, side, side)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class ToolShelf(QWidget):
    """Floating shelf: handle + native slot buttons + add cell."""

    toolSelected = Signal(object)
    dockChanged = Signal(str)
    editRequested = Signal(object, object)
    duplicateRequested = Signal(object)
    removeRequested = Signal(object)
    addRequested = Signal(object)
    repositionStarted = Signal()
    repositionMoved = Signal(object)
    repositionFinished = Signal()
    repositionCancelled = Signal()
    slotReorderStarted = Signal(object)
    slotReorderMoved = Signal(object)
    slotReorderFinished = Signal()
    slotReorderCancelled = Signal()

    def __init__(
        self, store: ToolShelfStore, tokens: InkfallTokens, parent=None
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._tokens = tokens
        self._buttons: dict[str, SlotButton] = {}
        self._handle: _HandleCell | None = None
        self._add: _AddCell | None = None
        self._scroll: _CustomScroll | None = None
        self._edge_prev: _OverflowEdge | None = None
        self._edge_next: _OverflowEdge | None = None
        self._menu = None
        self._in_fix = False
        self._fit_visible: int | None = None
        # Read-only availability (viewer-driven): None = every tool
        # enabled; otherwise only slots whose ToolType kind is listed
        # stay enabled (read-only sessions grey out mutating tools).
        self._available_kinds: tuple | None = None
        # Uncommitted docking preview (reposition drag); store untouched.
        self._preview_dock: str | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Selected-tool capsule (M6.9.3): ONE integrated control made of
        # a shared outer capsule visual — mouse-transparent, so it never
        # intercepts a click — that also paints the options face, plus the
        # pointer target in front of it (the slot's own pitch-sized band
        # of that face) and the slot button itself. Selecting a different
        # tool moves it; the layout never changes and the toolbar never
        # jumps.
        self._capsule = ToolCapsule(tokens, self)
        self._capsule.setVisible(False)
        self._dots = CapsuleOptionsButton(tokens, self)
        self._dots.clicked.connect(self._on_capsule_clicked)
        self._dots.setVisible(False)
        self._capsule.bind_options_button(self._dots)
        self._capsule_shown = False
        # Clearance bookkeeping for the shown capsule (M6.9.7): while the
        # capsule arrives it is scrolled fully into the custom viewport so
        # it can never reach the Custom|Add arrow; a scroll the *user*
        # drives disarms that so the tools above stay reachable, and
        # `_adjusting_scroll` marks this shelf's own scrollbar writes.
        self._keep_capsule_clear = False
        self._adjusting_scroll = False
        # Local separation state: which tiles currently carry a content
        # offset, and their in-flight glides (one per tile at most).
        self._separated: set = set()
        self._tile_offset_anims: dict = {}
        self._tile_offset_targets: dict = {}
        # Reserved layout space for the shown capsule: which single slot's
        # cell carries it, and that cell's in-flight extent glides.
        self._reserved_button = None
        self._reserve_anims: list = []
        # Which tile currently yields its selected disc to the capsule's
        # single active-control highlight.
        self._highlight_owner = None
        self._build()
        self.refresh()
        self.update_tokens(tokens)

    @property
    def capsule(self) -> ToolCapsule:
        """The shared outer capsule visual (inside the toolbar)."""
        return self._capsule

    @property
    def dots(self) -> CapsuleOptionsButton:
        """The three-dot hit target inside the shared capsule."""
        return self._dots

    def set_options_open(self, open_: bool) -> None:
        """Reflect the properties popup's open state on the options control.

        Part of the same restrained fill language: the options face takes
        one stronger wash while its popup is open — never a ring.
        """
        try:
            self._dots.set_open(bool(open_))
        except Exception:
            pass

    # -- structure ------------------------------------------------------

    def _orientation(self) -> str:
        dock = self._preview_dock or self._store.dock
        return "horizontal" if dock in ("top", "bottom") else "vertical"

    def preview_dock(self, dock: str | None) -> None:
        """Preview a dock orientation WITHOUT touching the store.

        Lightweight: flips layout direction + divider + size only. Never
        rebuilds buttons mid-drag — the grabbed handle must stay alive
        for the whole gesture.
        """
        if dock is not None and dock not in DOCK_POSITIONS:
            return
        self._preview_dock = dock
        self._build()
        self._fix_content_size()
        self._sync_capsule()

    def cancel_preview(self) -> None:
        self._preview_dock = None
        self._build()
        self._fix_content_size()
        self._sync_capsule()

    def _build(self) -> None:
        """Create the box layout once; orientation flips via setDirection."""
        if self.layout() is None:
            layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
            layout.setContentsMargins(_MARGIN, _MARGIN, _MARGIN, _MARGIN)
            layout.setSpacing(_SPACING)
            self._layout = layout
        self._apply_direction()

    def _apply_direction(self) -> None:
        """Flip outer + custom-region direction (no widget rebuild)."""
        horizontal = self._orientation() == "horizontal"
        try:
            self._layout.setDirection(
                QBoxLayout.Direction.LeftToRight
                if horizontal
                else QBoxLayout.Direction.TopToBottom
            )
        except Exception:
            pass
        if self._scroll is not None:
            try:
                self._scroll.set_horizontal(horizontal)
            except Exception:
                pass
        for edge in (
            getattr(self, "_edge_prev", None),
            getattr(self, "_edge_next", None),
        ):
            if edge is not None:
                try:
                    edge.set_horizontal(horizontal)
                except Exception:
                    pass
        # A reservation belongs to one axis only: flipping direction drops
        # any current one rather than leaving a wrongly-shaped cell.
        try:
            self._reset_cell_extents()
        except Exception:
            pass
        try:
            self._sync_overflow_edges()
        except Exception:
            pass

    def _clear_buttons(self) -> None:
        # Detach synchronously (deleteLater alone can leave visible
        # zombies that keep painting stale state when the event loop
        # is pumped sparingly, e.g. offscreen/tests).
        layout = self.layout()
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                try:
                    widget.setParent(None)
                except Exception:
                    pass
                try:
                    widget.deleteLater()
                except Exception:
                    pass
        self._handle = None
        self._add = None
        self._scroll = None
        self._edge_prev = None
        self._edge_next = None
        # Drop the separation state with the tiles it referred to: the
        # rebuilt buttons start at rest, and no glide keeps a deleted
        # widget alive.
        self._stop_tile_offsets()
        self._separated = set()
        self._highlight_owner = None
        self._reset_cell_extents()

    def refresh(self) -> None:
        """Rebuild core + boundaries + customs + end-cap (store order).

        Layout is exactly: [core tools] boundary(Core|Custom)
        [custom scroll viewport] boundary(Custom|Add) [Add][Move].
        The two boundaries ARE the overflow elements — each one paints
        the separator line idle and morphs into the tail-less scroll
        arrow where overflow exists. No separate divider widgets, no
        permanent arrow buttons. Customs live inside the bounded scroll
        viewport; core tools, both boundaries, Add and the move handle
        stay fixed. The end-cap cluster is Add immediately followed by
        the move handle (under Add in vertical docks, after Add in
        horizontal flow — never stacked).
        """
        self._clear_buttons()
        self._buttons.clear()
        self._scroll = None
        self._edge_prev = None
        self._edge_next = None
        self._fit_visible = None
        self._build()
        horizontal = self._orientation() == "horizontal"
        slots = list(self._store.slots)
        core = [s for s in slots if is_core_tool(s)]
        customs = [s for s in slots if not is_core_tool(s)]
        # Fixed core block first: core tools, never the move handle.
        for slot in core:
            self._layout.addWidget(self._make_button(slot))
        # Boundary 1 (Core|Custom): separator line, arrow on overflow.
        scroll = _CustomScroll(horizontal, self)
        self._scroll = scroll
        for slot in customs:
            scroll._inner_layout.addWidget(self._make_button(slot))
        edge_prev = _OverflowEdge(False, horizontal, self._tokens, self)
        edge_prev.setAccessibleName("Boundary between core and custom tools")
        edge_prev.stepRequested.connect(self._step_custom_scroll)
        self._edge_prev = edge_prev
        self._layout.addWidget(edge_prev)
        self._layout.addWidget(scroll)
        edge_next = _OverflowEdge(True, horizontal, self._tokens, self)
        edge_next.setAccessibleName("Boundary between custom tools and Add")
        edge_next.stepRequested.connect(self._step_custom_scroll)
        self._edge_next = edge_next
        self._layout.addWidget(edge_next)
        try:
            scroll.horizontalScrollBar().valueChanged.connect(
                self._on_custom_scrolled
            )
            scroll.horizontalScrollBar().rangeChanged.connect(
                lambda *_a: self._sync_overflow_edges()
            )
            scroll.verticalScrollBar().valueChanged.connect(
                self._on_custom_scrolled
            )
            scroll.verticalScrollBar().rangeChanged.connect(
                lambda *_a: self._sync_overflow_edges()
            )
        except Exception:
            pass
        # Boundary 2 (Custom|Add): separator line, arrow on overflow.
        add = _AddCell(self._tokens, self)
        add.clicked.connect(lambda: self.addRequested.emit(add))
        self._add = add
        self._layout.addWidget(add)
        handle = _HandleCell(self._tokens, self)
        handle.holdStarted.connect(self.repositionStarted.emit)
        handle.dragMoved.connect(self.repositionMoved.emit)
        handle.dragReleased.connect(self.repositionFinished.emit)
        handle.dragCancelled.connect(self.repositionCancelled.emit)
        self._handle = handle
        self._layout.addWidget(handle)
        # Bounded natural size from actual contents (explicit math —
        # never a sidebar column, never a stale-hint collapse; customs
        # cap at the adaptive visible count, overflow scrolls inside).
        self._fix_content_size()
        self._restyle_chrome()
        # Settle child geometry synchronously: layout-managed children
        # apply positions/visibility on the posted-event pass, so a
        # popup measuring its launcher right after a rebuild would
        # otherwise anchor to a stale (0,0) rect. Scoped to this shelf's
        # subtree only — never an app-wide flush.
        try:
            from PySide6.QtWidgets import QApplication, QWidget

            for child in self.findChildren(QWidget):
                try:
                    QApplication.sendPostedEvents(child, 0)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._sync_overflow_edges()
        except Exception:
            pass
        # A rebuild changes the shelf's own size; re-anchor in the parent
        # so the position never goes stale (off-canvas shelf after
        # add/remove while the window sits at another size).
        try:
            self._position_in_parent()
        except Exception:
            pass
        try:
            self._sync_capsule()
        except Exception:
            pass

    def _along_scrollbar(self):
        """Along-axis scrollbar of the custom viewport (or None)."""
        if self._scroll is None:
            return None
        try:
            if self._orientation() == "horizontal":
                return self._scroll.horizontalScrollBar()
            return self._scroll.verticalScrollBar()
        except Exception:
            return None

    def _step_custom_scroll(self, direction: int) -> None:
        """Animated page-step toward an overflow edge (fx glide, no jump)."""
        try:
            bar = self._along_scrollbar()
            if bar is None:
                return
            page = max(CELL * 2, bar.pageStep())
            target = bar.value() + int(direction) * page
            try:
                # Settle hook: once the glide lands, resync the boundary
                # arrows from the actual final position (covers natural
                # completion; retargets resync through their own ticks
                # plus their own settle hook).
                _fx.smooth_scroll_to(
                    bar, target, 180, self, on_finished=self._sync_overflow_edges
                )
            except Exception:
                bar.setValue(target)
        except Exception:
            pass

    def _on_custom_scrolled(self, _value: int | None = None) -> None:
        """The custom viewport moved: resync boundaries and the capsule.

        A scroll this shelf performed itself (``_adjusting_scroll`` — the
        capsule's own clearance nudge) is part of the capsule's arrival, so
        the clearance stays armed. Anything else is the user moving the
        strip, and that hands the scroll back to them: pinning the region
        to the capsule's fitting position would make the tools above the
        selected one unreachable, so the capsule simply follows its tool
        instead (its options target never reaches the boundary — see
        ``_capsule_geometry``).
        """
        try:
            self._sync_overflow_edges()
        except Exception:
            pass
        allow = bool(getattr(self, "_adjusting_scroll", False))
        try:
            self._sync_capsule(animate=False, scrolled=not allow)
        except Exception:
            pass

    def _sync_overflow_edges(self) -> None:
        """Reveal arrows exactly where overflow exists (else lines)."""
        try:
            bar = self._along_scrollbar()
            if bar is None or self._edge_prev is None or self._edge_next is None:
                return
            self._edge_prev.set_overflow(bar.value() > bar.minimum())
            self._edge_next.set_overflow(bar.value() < bar.maximum())
        except Exception:
            pass

    def _make_button(self, slot: ToolInstance) -> SlotButton:
        """Build one wired slot button (core and customs share the cell)."""
        button = SlotButton(slot, self._tokens, self)
        button.set_active(slot.id.value == self._store.active_id)
        button.clicked.connect(lambda _c=False, s=slot: self._select_slot(s))
        button.editRequested.connect(lambda _b, s=slot: self._on_slot_edit(s, _b))
        button.reorderStarted.connect(self.slotReorderStarted.emit)
        button.reorderMoved.connect(self.slotReorderMoved.emit)
        button.reorderFinished.connect(self.slotReorderFinished.emit)
        button.reorderCancelled.connect(self.slotReorderCancelled.emit)
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.customContextMenuRequested.connect(
            lambda pos, s=slot, b=button: self._open_slot_menu(s, b, pos)
        )
        # Rebuilt shelves (refresh / add / remove / reorder) inherit the
        # current availability instead of silently re-enabling tools.
        if self._available_kinds is not None:
            try:
                button.setEnabled(slot.kind in self._available_kinds)
            except Exception:
                pass
        self._buttons[slot.id.value] = button
        return button

    def _on_slot_edit(self, slot: ToolInstance, button: QWidget) -> None:
        if has_editable_properties(slot):
            self.editRequested.emit(slot, button)

    # -- selected-tool capsule --------------------------------------------

    def _active_button(self) -> SlotButton | None:
        try:
            return self._buttons.get(self._store.active_id)
        except Exception:
            return None

    def _on_capsule_clicked(self) -> None:
        """Open properties for the active slot — never change selection."""
        try:
            slot = self._store.active()
        except Exception:
            slot = None
        if slot is None or not has_editable_properties(slot):
            return
        self.editRequested.emit(slot, self._dots)

    def raise_capsule_stack(self) -> None:
        """Z-order the two overlay children around the slot tiles.

        The capsule is a *backdrop*: it must stay BELOW every slot tile, so
        the tiles keep painting their own surface and glyph straight over
        it. That is what keeps the selected tool's icon visible with no
        hole punched in the capsule, and what stops the capsule from ever
        covering a neighbouring tool's glyph. The options pointer target,
        by contrast, must sit ABOVE the tiles or it could never be
        clicked; it paints nothing, so raising it hides nothing.
        """
        capsule = getattr(self, "_capsule", None)
        if capsule is not None:
            try:
                capsule.lower()
            except Exception:
                pass
        dots = getattr(self, "_dots", None)
        if dots is not None:
            try:
                dots.raise_()
            except Exception:
                pass

    def _capsule_geometry(self):
        """Authoritative shelf-local geometry of the selected control.

        Returns ``(capsule_rect, tool_center, dots_center, dots_rect)`` —
        one single calculation drives the shared outer capsule, the
        options face it paints, *and* the options pointer target, so the
        three can never disagree. ``None`` when the slot is scrolled fully
        out of the custom viewport.

        Vertical docks (the default left toolbar included): the dots sit
        directly *below* the tool. Horizontal docks: the cross-axis has
        no room below the tool (a fixed 50px pill with a 40px cell), so
        the deliberate adaptation places the dots after the tool along
        the toolbar axis, still inside the shelf's bounds.

        The whole control is derived from the selected slot's own circle,
        so it never depends on (or changes) the toolbar's pitch.
        """
        button = self._active_button()
        if button is None:
            return None
        horizontal = self._orientation() == "horizontal"
        try:
            tl = button.mapTo(self, QPoint(0, 0))
        except Exception:
            return None
        # Always the tile's own CELL square — never its grown layout cell:
        # a reserved run must not move the control it reserves room for.
        centre = QPointF(tl.x() + CELL / 2.0, tl.y() + CELL / 2.0)
        # The selected tool's own local separation (it rises while the
        # capsule is shown) is part of this ONE authoritative
        # calculation: the capsule is derived from the *lifted* circle
        # from the first frame, so it is aligned with the tool for the
        # whole transition and never has to move to catch up. The offset
        # itself is paint-only (see SlotButton.set_content_offset).
        axis_lift = SEPARATION_ACTIVE
        if horizontal:
            centre = QPointF(centre.x() - axis_lift, centre.y())
        else:
            centre = QPointF(centre.x(), centre.y() - axis_lift)
        # End of this slot's own run along the toolbar axis: its 40px cell
        # (plus the 3px inter-slot gap, plus any run reserved for the
        # capsule, which is this slot's own space). The options target may
        # use the reserved run — the neighbouring tool has genuinely moved
        # past it — while a capsule-free toolbar ends exactly at cell+gap.
        cell_along_end = _SPACING + float(
            (tl.x() + button.width()) if horizontal else (tl.y() + button.height())
        )

        # Hide — never clamp — when the selected circle is scrolled out
        # of the custom viewport: a clamped capsule would no longer wrap
        # the tool it belongs to.
        v_lo = v_hi = None
        try:
            viewport = self._scroll.viewport() if self._scroll else None
        except Exception:
            viewport = None
        if viewport is not None and viewport.isAncestorOf(button):
            try:
                vtl = viewport.mapTo(self, QPoint(0, 0))
                v_lo = float(vtl.x() if horizontal else vtl.y())
                v_hi = v_lo + float(
                    viewport.width() if horizontal else viewport.height()
                )
                c = centre.x() if horizontal else centre.y()
                tool_r = CAPSULE_TOOL_D / 2.0
                if c + tool_r <= v_lo or c - tool_r >= v_hi:
                    return None
            except Exception:
                v_lo = v_hi = None

        w, h = capsule_size(horizontal)
        tool_r = CAPSULE_TOOL_D / 2.0
        if horizontal:
            x = centre.x() - CAPSULE_PAD - tool_r
            y = centre.y() - h / 2.0
        else:
            x = centre.x() - w / 2.0
            y = centre.y() - CAPSULE_PAD - tool_r

        # Stay inside the toolbar's own visible bounds.
        bounds = self.rect()
        x = max(0.0, min(max(0.0, bounds.width() - w), x))
        y = max(0.0, min(max(0.0, bounds.height() - h), y))
        capsule = QRectF(x, y, w, h)

        # Tool circle centre in capsule-local coordinates. The capsule is a
        # backdrop *below* the tiles, so this is a reference point only: the
        # contour's top end is concentric with it and the slot button paints
        # its halo and glyph straight over the capsule there. Nothing is
        # punched out of the capsule's body.
        tool_center = QPointF(centre.x() - x, centre.y() - y)

        # Options circle centre on the capsule's long axis, right after the
        # tool circle (the two controls are CAPSULE_MERGE apart).
        advance = capsule_advance()
        if horizontal:
            dots_center = QPointF(tool_center.x() + advance, tool_center.y())
        else:
            dots_center = QPointF(tool_center.x(), tool_center.y() + advance)
        # Pointer target for the painted disc: the disc across the toolbar
        # axis, clipped on the axis to this slot's own pitch. The painted
        # disc is a full 30px circle, and its lowest rows deliberately reach
        # into the next slot's empty top padding, so the pointer target
        # would follow it there without this clip — but over that padding
        # the neighbouring tool keeps its own hit target, so aiming at the
        # next tool can never open this slot's properties, and nothing
        # global had to be reserved to make that true. The dots themselves
        # sit well inside the clipped band, so the affordance is always
        # clickable.
        #
        # The band is additionally clipped to the custom viewport: past its
        # end sits the Custom|Add scroll arrow, whose own hit target and
        # clicks are never something this control may take over.
        half = CAPSULE_DOTS_D / 2.0
        lo = dots_center.x() if horizontal else dots_center.y()
        band_end = cell_along_end
        if v_hi is not None:
            band_end = min(band_end, float(v_hi))
        if horizontal:
            x0 = x + lo - half
            if v_lo is not None:
                x0 = max(x0, float(v_lo))
            x1 = min(x + lo + half, band_end)
            dots = QRectF(
                x0, y + dots_center.y() - half, max(0.0, x1 - x0), CAPSULE_DOTS_D
            )
        else:
            y0 = y + lo - half
            if v_lo is not None:
                y0 = max(y0, float(v_lo))
            y1 = min(y + lo + half, band_end)
            dots = QRectF(
                x + dots_center.x() - half, y0, CAPSULE_DOTS_D, max(0.0, y1 - y0)
            )
        return capsule, tool_center, dots_center, dots

    def _reposition_capsule(self) -> None:
        """Re-apply the overlay geometry from the selected tile's LIVE frame.

        The capsule's anchor is the selected tile's own mapped position, and
        that position is not fixed while the reservation moves: releasing
        the previously reserved slot's run shrinks the cell above this one,
        so every following tool — the newly selected one included — slides
        along the toolbar for the whole transfer glide. A frame captured once
        when the selection changed is therefore stale by the released run,
        which leaves the tool poking out through the capsule's top edge (and
        reads worst for slots far down the toolbar, since more of the shifted
        run sits above them). Re-reading the button's current frame and
        re-running the same single authoritative calculation keeps the
        capsule, the options disc and its pointer target locked to the button
        at every instant of the animation.

        Geometry only: the fades, the separation state and the reservation
        glides themselves are never touched, so this can be called from a
        glide tick without disturbing it. The one exception is the capsule's
        own clearance from the Custom|Add arrow, which is a scroll of the
        reserved run while the capsule is arriving (see
        :meth:`_keep_capsule_in_viewport`).
        """
        if not bool(getattr(self, "_capsule_shown", False)):
            return
        cap = getattr(self, "_capsule", None)
        dots = getattr(self, "_dots", None)
        if cap is None or dots is None:
            return
        try:
            geom = self._capsule_geometry()
            if geom is None:
                return
            capsule_rect, tool_center, dots_center, dots_rect = geom
            cap.set_circles(tool_center, dots_center)
            cap.setGeometry(capsule_rect.toAlignedRect())
            dots.setGeometry(dots_rect.toAlignedRect())
            # The capsule's lower end reaches into the run its own cell
            # reserves. Keep that run inside the usable custom viewport, so
            # the capsule can never paint into — or sit over — the
            # Custom|Add scroll arrow.
            self._keep_capsule_in_viewport(capsule_rect)
        except Exception:
            pass

    def _keep_capsule_in_viewport(self, capsule_rect) -> None:
        """Scroll the reserved run into view so the capsule clears a boundary.

        The capsule's lower end sits ``capsule length - SEPARATION_ACTIVE``
        below the selected cell's top edge — the run ``_reserved_extent``
        reserves — and its upper end starts one ``SEPARATION_ACTIVE`` above
        it, so a tool near *either* end of the custom viewport would
        otherwise put the capsule into the boundaries: over the Core|Custom
        separator at the top when the strip is scrolled down, or over the
        Custom|Add arrow at the bottom when it is scrolled up. In both cases
        the options target would also sit over that control's hit target.

        The available space is measured from the real bounds — the capsule's
        own rectangle against the custom viewport's — and *both* boundaries
        are considered, so the correction is symmetric: the region moves
        along the axis by exactly the deficit at whichever end the capsule
        has crossed, clamped to the range the reservation itself created, so
        the capsule ends fully inside the usable viewport with the layout's
        own inter-slot gap between its outer edge and that separator.

        Only a boundary that can actually be satisfied is corrected, and
        only while the capsule is *arriving*: a scroll the user drives
        disarms that (``_on_custom_scrolled``) — pinning the region to the
        capsule's fitting position would make the tools above the selected
        one unreachable.
        """
        if not bool(getattr(self, "_keep_capsule_clear", False)):
            return
        scroll = getattr(self, "_scroll", None)
        bar = self._along_scrollbar()
        if scroll is None or bar is None or capsule_rect is None:
            return
        try:
            viewport = scroll.viewport()
        except Exception:
            return
        horizontal = self._orientation() == "horizontal"
        try:
            vtl = viewport.mapTo(self, QPoint(0, 0))
            v_lo = float(vtl.x() if horizontal else vtl.y())
            v_hi = v_lo + float(
                viewport.width() if horizontal else viewport.height())
            cap_lo = float(
                capsule_rect.left() if horizontal else capsule_rect.top())
            cap_hi = float(
                capsule_rect.right() if horizontal else capsule_rect.bottom())
        except Exception:
            return
        if cap_hi - cap_lo > v_hi - v_lo:
            # Degenerate case (a viewport narrower than the capsule, e.g. a
            # single visible custom): no scroll position clears both
            # boundaries, so the scroll is left entirely to the user rather
            # than thrashed between the two corrections.
            return
        if cap_lo < v_lo:
            # Crossed the Core|Custom separator: bring the reserved run down.
            overflow = cap_lo - v_lo
        elif cap_hi > v_hi:
            # Crossed the Custom|Add arrow: push the reserved run up.
            overflow = cap_hi - v_hi
        else:
            return
        try:
            target = int(round(float(bar.value()) + overflow))
            target = max(int(bar.minimum()), min(target, int(bar.maximum())))
            if target == int(bar.value()):
                return
            self._adjusting_scroll = True
            try:
                bar.setValue(target)
            finally:
                self._adjusting_scroll = False
        except Exception:
            self._adjusting_scroll = False
        # The scrollbar's own change signal already re-anchored the
        # overlay; land it on the settled frame regardless (idempotent, and
        # bounded — the overflow is gone after the move above).
        self._reposition_capsule()

    # -- local separation (paint-only; the pitch never changes) ------------

    def _next_custom_button(self) -> SlotButton | None:
        """The slot tile immediately after the active one, in its own layout.

        Only a real neighbouring *tool* qualifies: the Custom|Add boundary
        after the last custom is a divider, not a tool, and is left where
        it is.
        """
        scroll = getattr(self, "_scroll", None)
        active = self._active_button()
        if scroll is None or active is None:
            return None
        try:
            layout = scroll._inner_layout
            index = layout.indexOf(active)
        except Exception:
            return None
        if index < 0:
            return None
        try:
            for i in range(index + 1, layout.count()):
                item = layout.itemAt(i)
                widget = item.widget() if item is not None else None
                if isinstance(widget, SlotButton):
                    return widget
        except Exception:
            pass
        return None

    def _tile_offset(self, rise: bool, distance: float):
        """Along-axis paint offset for a tile: ``rise`` = toward toolbar 0."""
        sign = -float(distance) if rise else float(distance)
        if self._orientation() == "horizontal":
            return (sign, 0.0)
        return (0.0, sign)

    def _stop_tile_offsets(self) -> None:
        """Stop every separation glide (no state left behind)."""
        for anim in list(getattr(self, "_tile_offset_anims", {}).values()):
            try:
                anim.stop()
                anim.deleteLater()
            except Exception:
                pass
        try:
            self._tile_offset_anims = {}
            self._tile_offset_targets = {}
        except Exception:
            pass

    def _finish_tile_offset(self, button, offset) -> None:
        """Land a naturally-finished glide on its exact terminal offset."""
        try:
            anim = self._tile_offset_anims.pop(button, None)
            if anim is not None:
                anim.deleteLater()
            self._tile_offset_targets.pop(button, None)
        except Exception:
            pass
        try:
            button.set_content_offset(*offset)
        except Exception:
            pass

    def _glide_tile_offset(self, button, offset, animate: bool) -> None:
        """Move one tile's *content* to ``offset`` (never its cell)."""
        offset = (float(offset[0]), float(offset[1]))
        if not animate:
            self._stop_tile_offset(button)
            try:
                button.set_content_offset(*offset)
            except Exception:
                pass
            return
        try:
            if button.content_offset == offset and (
                button not in self._tile_offset_anims
            ):
                return
            if self._tile_offset_targets.get(button) == offset:
                return  # already gliding exactly there
        except Exception:
            pass
        self._stop_tile_offset(button)
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation
            from PySide6.QtCore import QPointF as _QPointF

            start = button.content_offset
            anim = QVariantAnimation(self)
            anim.setDuration(SEPARATION_MS)
            anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            anim.setStartValue(_QPointF(start[0], start[1]))
            anim.setEndValue(_QPointF(offset[0], offset[1]))

            def _tick(value, _b=button) -> None:
                try:
                    _b.set_content_offset(value.x(), value.y())
                except Exception:
                    pass

            anim.valueChanged.connect(_tick)
            anim.finished.connect(
                lambda _b=button, _o=offset: self._finish_tile_offset(_b, _o)
            )
            self._tile_offset_anims[button] = anim
            self._tile_offset_targets[button] = offset
            anim.start()
        except Exception:
            self._finish_tile_offset(button, offset)

    def _stop_tile_offset(self, button) -> None:
        try:
            anim = self._tile_offset_anims.pop(button, None)
            if anim is not None:
                anim.stop()
                anim.deleteLater()
            self._tile_offset_targets.pop(button, None)
        except Exception:
            pass

    def _sync_separation(self, animate: bool = True) -> None:
        """The local, animated room the capsule makes for itself.

        Two coordinated effects, both animated only with the capsule's own
        arrival and departure:

        * the selected tool's own content rises ``SEPARATION_ACTIVE``
          (paint-only, inside its unchanged square cell), so the capsule's
          top cap stays concentric with the tool's halo;
        * the selected slot's *layout cell* grows along the toolbar axis by
          the capsule's overhang (``_reserved_extent``), so the box layout
          really moves every following tool down by that amount and the
          button sizes, spacing and hit targets of those tools are
          untouched. Nothing today rides on a paint offset any more; the
          old ``SEPARATION_NEIGHBOUR`` shift is redundant, since reserving
          the space in the layout is exactly what it was faking.
        """
        shown = bool(getattr(self, "_capsule_shown", False))
        active = self._active_button() if shown else None
        # Exactly one tile yields its selected disc to the capsule's single
        # highlight while the capsule is shown (and takes it straight back
        # when the capsule hides or the selection changes).
        self._set_highlight_owner(active)
        targets: dict = {}
        if active is not None:
            targets[active] = self._tile_offset(True, SEPARATION_ACTIVE)
        try:
            reduce = bool(_fx.reduced_motion_enabled())
        except Exception:
            reduce = False
        animate = bool(animate) and not reduce
        previous = set(getattr(self, "_separated", set()))
        for button in previous - set(targets):
            self._glide_tile_offset(button, (0.0, 0.0), animate)
        for button, offset in targets.items():
            self._glide_tile_offset(button, offset, animate)
        self._separated = set(targets)
        self._sync_reserve(active, animate)

    # -- reserved layout space (real, animated) ----------------------------

    def _set_highlight_owner(self, button) -> None:
        """Let exactly one tile yield its selected disc to the capsule.

        The capsule paints the ONE active-control highlight behind the
        selected tool's icon, and it lives *below* the tiles, so that tile's
        own opaque near-black disc would hide it. Only that one tile yields
        its disc, and it takes it back the instant the capsule hides, the
        selection moves, or the tile is rebuilt — tools without a capsule
        (Pan / Selection) are never touched.
        """
        previous = getattr(self, "_highlight_owner", None)
        if previous is button:
            return
        self._highlight_owner = button
        for tile in (previous, button):
            if tile is None:
                continue
            try:
                tile.set_capsule_active(tile is button)
            except (RuntimeError, AttributeError):
                pass

    def _reserved_extent(self) -> int:
        """Along-axis cell size that holds the shown capsule in full.

        The capsule is derived from the selected cell's own top edge, so
        the run it needs past that edge is its drawn length minus the
        ``SEPARATION_ACTIVE`` lift the cell already absorbs. Everything
        after the selected slot therefore starts below the capsule's lower
        contour, with the normal inter-slot gap to spare.
        """
        horizontal = self._orientation() == "horizontal"
        size = capsule_size(horizontal)
        along = size[0] if horizontal else size[1]
        return int(along) - int(SEPARATION_ACTIVE)

    def _stop_reserve(self) -> None:
        """Stop every reserve glide (safe to call at any time)."""
        try:
            for anim in list(getattr(self, "_reserve_anims", [])):
                try:
                    anim.stop()
                    anim.deleteLater()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._reserve_anims = []
        except Exception:
            pass

    def _drop_reserve_anim(self, anim) -> None:
        """Forget a finished glide (its tile keeps the terminal extent)."""
        try:
            if anim in self._reserve_anims:
                self._reserve_anims.remove(anim)
        except Exception:
            pass
        try:
            anim.deleteLater()
        except Exception:
            pass
        # Land the overlay on the settled frame, not the last tick's.
        self._reposition_capsule()

    def _glide_cell_extent(
        self, button, start: int, end: int, horizontal: bool, animate: bool
    ) -> None:
        """Animate one tile's layout-cell extent (real layout space)."""
        end = int(max(CELL, end))
        start = int(max(CELL, start))
        if not animate or start == end:
            try:
                button.set_cell_extent(end, horizontal)
            except Exception:
                pass
            self._reposition_capsule()
            return
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation

            anim = QVariantAnimation(self)
            anim.setDuration(SEPARATION_MS)
            anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            anim.setStartValue(start)
            anim.setEndValue(end)

            def _tick(value, _b=button) -> None:
                try:
                    _b.set_cell_extent(int(round(float(value))), horizontal)
                except Exception:
                    pass
                # The selected tile rides along with this glide (any release
                # above it shifts it too), so the capsule has to be re-anchored
                # to its live frame in the same beat — never left behind.
                self._reposition_capsule()

            anim.valueChanged.connect(_tick)
            anim.finished.connect(
                lambda _a=anim: self._drop_reserve_anim(_a)
            )
            self._reserve_anims.append(anim)
            anim.start()
        except Exception:
            try:
                button.set_cell_extent(end, horizontal)
            except Exception:
                pass

    def _sync_reserve(self, active, animate: bool) -> None:
        """Reserve (or release) the capsule's run in the real layout.

        Exactly one cell ever carries it: the selected slot's. Switching
        tools releases the previous cell first, so no reservation and no
        glide can accumulate or linger on a tile that is no longer active.
        """
        horizontal = self._orientation() == "horizontal"
        target = self._reserved_extent() if active is not None else CELL
        previous = getattr(self, "_reserved_button", None)
        running = list(getattr(self, "_reserve_anims", []))
        if not animate and previous is active and running:
            # Never snap a glide that is already heading exactly where this
            # re-sync wants it (scroll clamps arrive this way): interrupting
            # it mid-flight is the one visible jump possible.
            try:
                if any(int(a.endValue()) == int(target) for a in running):
                    return
            except Exception:
                pass
        self._stop_reserve()
        # Release and reserve are one coordinated transfer: both glides run
        # together on the same duration/easing, so switching tools never
        # leaves a tool double-moved or a run that has to catch up.
        if previous is not None and previous is not active:
            try:
                start = int(previous.cell_extent())
            except Exception:
                start = CELL
            self._glide_cell_extent(previous, start, CELL, horizontal, animate)
        self._reserved_button = active
        if active is None:
            return
        try:
            start = int(active.cell_extent()) if previous is active else CELL
        except Exception:
            start = CELL
        self._glide_cell_extent(active, start, target, horizontal, animate)

    def _reset_cell_extents(self) -> None:
        """Every tile back to its plain square cell (no reservation)."""
        self._stop_reserve()
        self._reserved_button = None
        horizontal = self._orientation() == "horizontal"
        for button in self.findChildren(SlotButton):
            try:
                button.set_cell_extent(CELL, horizontal)
            except Exception:
                pass

    def _stop_capsule_anims(self, widget) -> None:
        try:
            from PySide6.QtCore import QAbstractAnimation

            for anim in widget.findChildren(QAbstractAnimation):
                try:
                    anim.stop()
                    anim.deleteLater()
                except (RuntimeError, AttributeError):
                    pass
        except Exception:
            pass
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass

    def _set_capsule_visible(self, visible: bool) -> None:
        self._capsule_shown = False
        for widget in (getattr(self, "_capsule", None), getattr(self, "_dots", None)):
            if widget is None:
                continue
            self._stop_capsule_anims(widget)
            try:
                widget.setVisible(bool(visible))
            except Exception:
                pass
        # A hidden shelf keeps no separation either.
        self._sync_separation(False)

    def _hide_capsule(self, animate: bool = True) -> None:
        cap = getattr(self, "_capsule", None)
        dots = getattr(self, "_dots", None)
        if cap is None or dots is None:
            return
        if not (cap.isVisible() or dots.isVisible()):
            self._capsule_shown = False
            self._sync_separation(animate)
            return
        if animate and not getattr(self, "_capsule_shown", False):
            # Already fading out: restarting the exit animation on every
            # shelf settle would keep the control on screen indefinitely.
            return
        self._capsule_shown = False
        self._sync_separation(animate)
        self._stop_capsule_anims(cap)
        self._stop_capsule_anims(dots)
        if animate:
            try:
                _fx.fade_out(cap, 120, cap)
                _fx.fade_out(dots, 120, dots)
                return
            except Exception:
                pass
        # Immediate path (ineligible selection): the control is gone in
        # the very frame the new tool is selected, even mid-fade.
        cap.setVisible(False)
        dots.setVisible(False)

    def _sync_capsule(self, animate: bool = True,
                      scrolled: bool = False) -> None:
        """Attach the integrated capsule to the selected slot.

        Hidden for slots without editable properties (Pan/Selection) and
        whenever the shelf itself is not visible, so the affordance never
        offers an empty popup. The final geometry is established *before*
        the capsule is shown, and the reveal animation is opacity-only:
        a selection can therefore never flash or transport the control
        through an incorrect location, and rapid tool changes only ever
        leave one capsule at the newest slot.

        Eligibility is resolved *before* anything is exposed: an
        ineligible selection (or one whose slot is scrolled out) drops
        the previous control immediately — never an exit animation that
        would keep the old tool's dots on screen under the new tool.
        """
        cap = getattr(self, "_capsule", None)
        dots = getattr(self, "_dots", None)
        if cap is None or dots is None:
            return
        # The clearance nudge is armed whenever the capsule is
        # (re)established — a selection, a rebuild, a resize, a dock flip —
        # and disarmed by a scroll the user drives (`scrolled`).
        self._keep_capsule_clear = not bool(scrolled)
        try:
            slot = self._store.active()
            eligible = (
                slot is not None and has_editable_properties(slot) and self.isVisible()
            )
            geom = self._capsule_geometry() if eligible else None
            if geom is None:
                # `eligible` is False for Pan/Selection: hide in the same
                # frame. An eligible slot that merely left the viewport
                # may still animate out.
                self._hide_capsule(animate and eligible)
                return
            capsule_rect, tool_center, dots_center, dots_rect = geom
            was_visible = cap.isVisible()
            self._stop_capsule_anims(cap)
            self._stop_capsule_anims(dots)
            cap.update_tokens(self._tokens)
            dots.update_tokens(self._tokens)
            # Establish the final position *first*; nothing is shown at a
            # stale or clamped location.
            cap.set_circles(tool_center, dots_center)
            cap.setGeometry(capsule_rect.toAlignedRect())
            dots.setGeometry(dots_rect.toAlignedRect())
            cap.show()
            dots.show()
            self.raise_capsule_stack()
            if animate and not was_visible:
                try:
                    _fx.fade_in(cap, 150, cap)
                    _fx.fade_in(dots, 150, dots)
                except Exception:
                    pass
            self._capsule_shown = True
            # The selected tool gets its own local rise — and its cell the
            # run the capsule needs — in the same beat as the entrance.
            self._sync_separation(animate)
            # That separation can move this very tile (a released run above
            # it) and can snap rather than glide, so the overlay is re-anchored
            # to the resulting frame instead of the pre-separation one.
            self._reposition_capsule()
        except Exception:
            self._set_capsule_visible(False)

    def moveEvent(self, event) -> None:
        try:
            super().moveEvent(event)
        except Exception:
            pass
        self._sync_capsule()

    def showEvent(self, event) -> None:
        try:
            super().showEvent(event)
        except Exception:
            pass
        self._sync_capsule()

    def hideEvent(self, event) -> None:
        self._set_capsule_visible(False)
        try:
            super().hideEvent(event)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self._set_capsule_visible(False)
        try:
            super().closeEvent(event)
        except Exception:
            pass

    def _visible_custom_cap(self) -> int:
        """Adaptive visible-custom count from the parent canvas budget."""
        try:
            parent = self.parentWidget()
            if parent is not None:
                along = (
                    parent.width()
                    if self._orientation() == "horizontal"
                    else parent.height()
                )
                cell = CELL + _SPACING
                if along > 0 and cell > 0:
                    cap = int((_CUSTOM_BUDGET_FRACTION * along) // cell)
                    return max(MIN_VISIBLE_CUSTOMS, min(MAX_VISIBLE_CUSTOMS, cap))
        except Exception:
            pass
        return MIN_VISIBLE_CUSTOMS

    def _natural_along(self, visible: int) -> int:
        """Along-axis length for ``visible`` customs in the viewport."""
        try:
            core = [s for s in self._store.slots if is_core_tool(s)]
        except Exception:
            core = []
        fixed_cells = 2 + len(core)  # add + move handle + core buttons
        view_along = visible * CELL + max(0, visible - 1) * _SPACING
        # Margins + fixed cells + two morphing boundaries + custom view
        # + one spacing gap between each adjacent pair.
        return (
            _MARGIN * 2
            + fixed_cells * CELL
            + 2 * _EDGE_ALONG
            + view_along
            + (fixed_cells + 2) * _SPACING
        )

    def fit_to_available(self, max_along: int) -> None:
        """Shrink the custom viewport so the shelf fits ``max_along``.

        Lowers the visible-custom count below the adaptive cap (down to
        a single cell) until the natural length fits. Core tools, both
        morphing boundaries, Add and Move are always preserved; excess
        customs scroll inside the viewport instead of escaping it.
        """
        try:
            customs = [s for s in self._store.slots if not is_core_tool(s)]
        except Exception:
            return
        if not customs:
            self._fit_visible = 0
            self._fix_content_size()
            return
        try:
            cap = self._visible_custom_cap()
        except Exception:
            cap = MIN_VISIBLE_CUSTOMS
        visible = min(len(customs), cap)
        try:
            budget = int(max_along)
        except Exception:
            budget = 0
        if budget > 0:
            floor = 1
            while visible > floor and self._natural_along(visible) > budget:
                visible -= 1
        self._fit_visible = visible
        self._fix_content_size()

    def _clamp_scroll(self) -> None:
        """Keep the custom scroll offset inside the valid range."""
        try:
            bar = self._along_scrollbar()
            if bar is None:
                return
            value = max(bar.minimum(), min(bar.value(), bar.maximum()))
            if value != bar.value():
                bar.setValue(value)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        """Recompute the viewport on every geometry change (no escape).

        Any resize — parent shrink, orientation flip, viewport squash —
        recomputes the scroll viewport, clamps the scroll offset to the
        new valid range, and resyncs the boundary arrow states, so no
        child tool can paint outside the bounded region.
        """
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        try:
            if self._in_fix:
                return
            # Fit to the actual geometry (not just recompute the cap):
            # a squash from the parent shrinks the custom viewport and
            # clamps the scroll offset instead of compressing children
            # outside the toolbar pill.
            try:
                along = (
                    self.width()
                    if self._orientation() == "horizontal"
                    else self.height()
                )
            except Exception:
                along = 0
            if along and along > 0:
                self.fit_to_available(int(along))
            else:
                self._fix_content_size()
            self._clamp_scroll()
            self._sync_overflow_edges()
            self._sync_capsule()
        except Exception:
            pass

    def _fix_content_size(self) -> None:
        # Fixed: core buttons + Add + move handle (CELL each), two
        # morphing boundaries (separator line idle, arrow on overflow).
        # Core always fully visible; customs show at most the adaptive
        # cap (or the fit-to-available count after a narrow resize)
        # inside the scroll viewport.
        if self._in_fix:
            return
        self._in_fix = True
        try:
            customs = [s for s in self._store.slots if not is_core_tool(s)]
            if self._fit_visible is not None:
                visible = max(0, min(len(customs), int(self._fit_visible)))
            else:
                visible = min(len(customs), self._visible_custom_cap())
            along = self._natural_along(visible)
            thick = _MARGIN * 2 + CELL
            horizontal = self._orientation() == "horizontal"
            if (horizontal and (self.width() != along or self.height() != thick)) or (
                not horizontal and (self.width() != thick or self.height() != along)
            ):
                if horizontal:
                    self.setFixedSize(along, thick)
                else:
                    self.setFixedSize(thick, along)
            # Size the scroll viewport to exactly the visible customs.
            if self._scroll is not None:
                try:
                    view_along = visible * CELL + max(0, visible - 1) * _SPACING
                    if horizontal:
                        self._scroll.setFixedSize(max(CELL, view_along), CELL)
                    else:
                        self._scroll.setFixedSize(CELL, max(CELL, view_along))
                except Exception:
                    pass
            self._clamp_scroll()
        finally:
            self._in_fix = False

    # -- behavior ---------------------------------------------------------

    def _select_slot(self, slot: ToolInstance) -> None:
        try:
            self._store.set_active(slot.id.value)
        except (KeyError, ValueError):
            return
        for slot_id, button in self._buttons.items():
            button.set_active(slot_id == slot.id.value)
        self._sync_capsule()
        self.toolSelected.emit(slot)

    def select_slot(self, slot_id: str) -> ToolInstance | None:
        slot = self._store.find(slot_id)
        if slot is None:
            return None
        self._select_slot(slot)
        return slot

    def set_tool_availability(self, allowed_kinds) -> None:
        """Enable only slots whose kind is in ``allowed_kinds``.

        ``None`` restores every tool. A disabled tile keeps its slot and
        selection state but cannot be activated by mouse, stylus or
        keyboard (widget-level disabled state; the tile's own paint
        already renders the faded look and suppresses the selected
        disc/hover wash). Read-only sessions use this to grey out every
        annotation-creating and document-mutating tool; add, move,
        scroll and capsule controls are toolbar configuration and stay
        available. Idempotent.
        """
        self._available_kinds = (
            None if allowed_kinds is None else tuple(allowed_kinds))
        self._apply_tool_availability()

    def _apply_tool_availability(self) -> None:
        """Push the stored availability onto every existing tile."""
        allowed = self._available_kinds
        for button in self._buttons.values():
            try:
                enabled = (True if allowed is None
                           else button.slot.kind in allowed)
            except Exception:
                enabled = True
            button.setEnabled(enabled)

    def set_dock(self, dock: str) -> None:
        self._preview_dock = None
        self._store.set_dock(dock)
        self.refresh()
        try:
            self._position_in_parent()
        except Exception:
            pass
        self.dockChanged.emit(dock)

    def _position_in_parent(self) -> None:
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "_position_shelf"):
            parent._position_shelf()

    def _open_slot_menu(self, slot: ToolInstance, button: QWidget, pos) -> None:
        """Native Inkfall menu: edit / duplicate / remove for one slot."""
        menu = self.build_slot_menu(slot)
        if menu is None:
            return
        try:
            if self._menu is not None:
                try:
                    self._menu.close()
                except Exception:
                    pass
            self._menu = menu
            anchor = button.mapToGlobal(button.rect().bottomLeft())
            menu.popup(anchor)
            _fx.animate_menu_open(menu, 140, -6, self)
        except Exception:
            pass

    def build_slot_menu(self, slot: ToolInstance):
        """Construct the native slot menu (popup is done by the caller)."""
        try:
            menu = QMenu(self)
            if has_editable_properties(slot):
                edit = menu.addAction(f"Edit {slot.name}")
                edit.triggered.connect(
                    lambda: self.editRequested.emit(
                        slot, self._buttons.get(slot.id.value)
                    )
                )
            if is_core_tool(slot):
                # Core tools: no Duplicate/Remove — permanent, fixed, unique.
                return menu
            duplicate = menu.addAction("Duplicate")
            duplicate.triggered.connect(lambda: self.duplicateRequested.emit(slot))
            remove = menu.addAction("Remove")
            remove.triggered.connect(lambda: self.removeRequested.emit(slot))
            return menu
        except Exception:
            return None

    def show_animated(self) -> None:
        self.setVisible(True)
        try:
            _fx.slide_fade_in(self, 0, 160, self, dy_px=-6)
        except Exception:
            pass

    def hide_animated(self) -> None:
        try:
            _fx.slide_fade_out(self, 0, 140, self, dy_px=4)
        except Exception:
            self.setVisible(False)

    # -- theming ----------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        for button in self._buttons.values():
            button.update_tokens(tokens)
        for extra in (
            self._handle,
            self._add,
            getattr(self, "_edge_prev", None),
            getattr(self, "_edge_next", None),
        ):
            if extra is not None:
                try:
                    extra.update_tokens(tokens)
                except Exception:
                    pass
        self._restyle_chrome()
        try:
            self._sync_overflow_edges()
        except Exception:
            pass
        try:
            if getattr(self, "_capsule", None) is not None:
                self._capsule.update_tokens(tokens)
            if getattr(self, "_dots", None) is not None:
                self._dots.update_tokens(tokens)
            self._sync_capsule()
        except Exception:
            pass

    def _restyle_chrome(self) -> None:
        t = self._tokens
        # Pill-like ends: radius at half the cross thickness curves the
        # START/END toward a half-circle while the middle stays straight.
        try:
            thick = min(max(1, self.width()), max(1, self.height()))
            radius = max(16, min(34, thick // 2))
        except Exception:
            radius = 26
        self.setStyleSheet(
            f"ToolShelf {{ background-color: {t.surface_raised};"
            f" border: 1px solid {t.border}; border-radius: {radius}px; }}"
        )
        try:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(18)
            shadow.setOffset(0, 4)
            from PySide6.QtGui import QColor as _QColor

            shadow.setColor(_QColor(0, 0, 0, 90))
            self.setGraphicsEffect(shadow)
        except Exception:
            pass
        try:
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
