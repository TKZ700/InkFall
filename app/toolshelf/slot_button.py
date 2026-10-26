"""Tool slot button (M5-A1 native). Inkfall-owned visual truth.

A lightweight ``QAbstractButton`` with a single coherent paint pass —
no QSS composition, no third-party face underneath. Everything reads
from Inkfall tokens + the slot's own ``ToolInstance`` configuration:

* pen: actual-color circle, diameter proportional to width + badge
* highlighter: translucent width-proportional stroke + badge
* shapes/nav: Inkfall vector icons
* selected: one opaque near-black disc (filled, never a border)
* hover: a restrained ink wash of the same disc, pressed: one step
  stronger · disabled: faded

Selected and hover are FILLS and share one statement of the language with
the capsule (:mod:`app.toolshelf.surfaces`): no accent edge, no accent
rim, no circular stroke on any of them.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton

from app.icons import ink_center_offset as _ink_offset
from app.icons import pixmap as _icon_pixmap
from app.icons import tool_pixmap as _tool_pixmap
from app.theme.tokens import InkfallTokens
from app.toolshelf.surfaces import hover_wash, selected_surface
from tools.models import ToolInstance, ToolType, is_core_tool

__all__ = ["SlotButton", "preview_kind", "CELL", "REORDER_HOLD_MS",
           "REORDER_THRESHOLD_PX"]

#: Compact square cell (orientation-adjusted by the shelf).
CELL = 40

#: Press-and-hold duration arming custom-slot reorder.
REORDER_HOLD_MS = 300
#: Pointer travel starting an armed reorder (plain click path otherwise).
REORDER_THRESHOLD_PX = 8

_SHAPE_ICONS = {
    ToolType.LINE: "line",
    ToolType.RECTANGLE: "rectangle",
    ToolType.ELLIPSE: "ellipse",
    ToolType.ARROW: "arrow",
    ToolType.POLYGON: "polygon",
}

#: Pen/Highlighter use dedicated Inkfall glyphs tinted with slot color.
_PEN_ICONS = {
    ToolType.PEN: "pen-nib",
    ToolType.HIGHLIGHTER: "marker",
}

_NAV_ICONS = {
    ToolType.PAN: "pan",
    ToolType.SELECTION: "selection",
    ToolType.LASSO: "lasso",
    ToolType.ERASER: "eraser",
    ToolType.NOTE: "note",
}


def preview_kind(slot: ToolInstance) -> str:
    """Preview family: icon (all tools use real tinted glyphs)."""
    return "icon"


def pen_diameter(slot: ToolInstance) -> int:
    """Circle diameter proportional to configured width (8..26px)."""
    try:
        width = float(slot.width_mm)
    except (TypeError, ValueError):
        width = 0.5
    return max(8, min(26, int(6 + width * 4.0)))


def stroke_height(slot: ToolInstance) -> int:
    """Highlighter stroke height proportional to width (5..16px)."""
    try:
        width = float(slot.width_mm)
    except (TypeError, ValueError):
        width = 3.0
    return max(5, min(16, int(3 + width * 2.0)))


def _with_alpha(color: QColor, alpha: int) -> QColor:
    out = QColor(color)
    out.setAlpha(max(0, min(255, int(alpha))))
    return out


class SlotButton(QAbstractButton):
    """One configured tool slot: native behavior, Inkfall paint."""

    selected = Signal(object)
    editRequested = Signal(object)
    reorderStarted = Signal(object)
    reorderMoved = Signal(object)
    reorderFinished = Signal()
    reorderCancelled = Signal()

    def __init__(self, slot: ToolInstance, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._slot = slot
        self._tokens = tokens
        self._icon_cache: dict[tuple, QPixmap] = {}
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(CELL, CELL)
        self._reorder_timer = QTimer(self)
        self._reorder_timer.setSingleShot(True)
        self._reorder_timer.setInterval(REORDER_HOLD_MS)
        self._reorder_timer.timeout.connect(self._on_reorder_timeout)
        self._reorder_press_pos = None
        self._reorder_armed = False
        self._reordering = False
        # Paint-only content offset: the local animated separation the
        # selected tool gets while the capsule is shown. Never touches this
        # widget's geometry or hit target.
        self._content_offset: tuple[float, float] = (0.0, 0.0)
        # Along-axis extent of this tile's own LAYOUT cell. Normally
        # exactly CELL; while the selected tool's capsule is shown the
        # shelf grows this one cell so the box layout genuinely reserves
        # the capsule's space (every following tool moves down). The
        # painted button stays a CELL square anchored at the tile origin.
        self._cell_extent = CELL
        # The focus ring is a KEYBOARD affordance: a mouse click focuses
        # the button too, and the selected / hovered states must stay free
        # of any ring, so only keyboard focus lights it.
        self._keyboard_focus = False
        # True while the selected-tool capsule paints this tile's active
        # highlight. The capsule's single wash disc sits *behind* the tile,
        # so this tile's own opaque near-black disc would cover it; yielding
        # it keeps exactly ONE highlight visible. Tools without a capsule
        # (Pan / Selection) never set this and keep their disc unchanged.
        self._capsule_active = False
        self._restyle()

    @property
    def keyboard_focused(self) -> bool:
        """True only for keyboard-driven focus (never a mouse click)."""
        try:
            return bool(self._keyboard_focus) and self.hasFocus()
        except Exception:
            return False

    def focusInEvent(self, event) -> None:  # noqa: N802
        try:
            reason = event.reason()
            self._keyboard_focus = reason in (
                Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason)
        except Exception:
            self._keyboard_focus = False
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self.update()

    @property
    def slot(self) -> ToolInstance:
        return self._slot

    def update_slot(self, slot: ToolInstance, tokens: InkfallTokens | None = None) -> None:
        self._slot = slot
        if tokens is not None:
            self._tokens = tokens
        self._icon_cache.clear()
        self._restyle()
        self.update()

    def set_active(self, active: bool) -> None:
        self.setChecked(bool(active))
        self._restyle()

    @property
    def capsule_active(self) -> bool:
        """True while the selected-tool capsule paints this tile's disc."""
        return bool(self._capsule_active)

    def set_capsule_active(self, active: bool) -> None:
        """Yield this tile's selected disc to the capsule's highlight.

        While the selected-tool capsule is shown for this slot it paints the
        ONE active-control highlight behind the icon (and moves it to the
        three-dot control when the properties popup opens). The capsule is a
        backdrop *below* the tiles, so this tile's own opaque near-black disc
        would cover that highlight: the disc is skipped for as long as the
        capsule owns it. The glyph, the hover/pressed wash and the keyboard
        focus ring are all untouched — only the opaque disc yields, and it
        comes back the moment the capsule hides.
        """
        active = bool(active)
        if active == self._capsule_active:
            return
        self._capsule_active = active
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Double-click opens properties only for editable tools."""
        try:
            from tools.models import has_editable_properties

            editable = has_editable_properties(self._slot)
        except Exception:
            editable = False
        try:
            if editable:
                event.accept()
                self.editRequested.emit(self)
                return
            # Pan/Lasso: subtle press feedback only, never a popup.
            self.setDown(True)
            self.update()
            try:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(120, self._release_press)
            except Exception:
                self._release_press()
            event.accept()
            return
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)

    def _release_press(self) -> None:
        try:
            self.setDown(False)
            self.update()
        except Exception:
            pass

    # -- hold-to-reorder (custom slots only; core tools never arm) --------

    def _on_reorder_timeout(self) -> None:
        if self._reorder_press_pos is None or self._reordering:
            return
        try:
            if is_core_tool(self._slot):
                return
        except Exception:
            return
        self._reorder_armed = True

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self._reorder_press_pos = event.position().toPoint()
                self._reorder_armed = False
                self._reordering = False
                self._reorder_timer.start()
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        try:
            if self._reorder_press_pos is not None:
                delta = event.position().toPoint() - self._reorder_press_pos
                travel = abs(delta.x()) + abs(delta.y())
                if self._reordering:
                    self.reorderMoved.emit(event.globalPosition().toPoint())
                    event.accept()
                    return
                if self._reorder_armed and travel >= REORDER_THRESHOLD_PX:
                    self._reordering = True
                    try:
                        self.grabMouse()
                    except Exception:
                        pass
                    self.reorderStarted.emit(self._slot)
                    self.reorderMoved.emit(event.globalPosition().toPoint())
                    event.accept()
                    return
                if not self._reorder_armed and travel >= REORDER_THRESHOLD_PX:
                    # Plain click-drag: never a reorder.
                    self._reorder_timer.stop()
                    self._reorder_press_pos = None
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        try:
            was_reordering = self._reordering
            self._reorder_timer.stop()
            self._reorder_press_pos = None
            self._reorder_armed = False
            self._reordering = False
            if was_reordering:
                try:
                    self.releaseMouse()
                except Exception:
                    pass
                try:
                    self.setDown(False)
                except Exception:
                    pass
                self.update()
                self.reorderFinished.emit()
                event.accept()
                return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        try:
            self.update()
        except Exception:
            pass
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        try:
            self.update()
        except Exception:
            pass
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Esc during an armed/dragging reorder cancels it."""
        try:
            if event.key() == Qt.Key.Key_Escape and (
                    self._reorder_armed or self._reordering):
                self._reorder_timer.stop()
                self._reorder_press_pos = None
                self._reorder_armed = False
                self._reordering = False
                try:
                    self.releaseMouse()
                except Exception:
                    pass
                try:
                    self.setDown(False)
                except Exception:
                    pass
                self.update()
                self.reorderCancelled.emit()
                event.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    @property
    def is_reordering(self) -> bool:
        return self._reordering

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._icon_cache.clear()
        self._restyle()

    # -- identity (no QSS; paint reads tokens + state directly) -------------

    def _restyle(self) -> None:
        slot = self._slot
        name = f"{slot.name} ({slot.kind.value})"
        self.setAccessibleName(name)
        self.setAccessibleDescription(f"Select tool {name}")
        self.update()

    def _shelf_bg(self) -> str:
        """Shelf surface behind the glyph (contrast reference, both themes)."""
        try:
            return str(self._tokens.surface_raised)
        except Exception:
            return "#181C23"

    # -- local separation offset (paint-only) ------------------------------

    @property
    def content_offset(self) -> tuple[float, float]:
        """This tile's paint-only content offset (never its geometry)."""
        try:
            return (float(self._content_offset[0]),
                    float(self._content_offset[1]))
        except Exception:
            return (0.0, 0.0)

    def set_content_offset(self, dx: float = 0.0, dy: float = 0.0) -> None:
        """Slide this tile's painted content, keeping its cell in place.

        Drives the selected tool's own small, animated rise while the
        capsule is shown. The cell, the 43px pitch and this widget's hit
        target are untouched — only the ink inside the cell moves — so no
        layout and no click target ever changes. Room for the capsule
        itself comes from the layout instead (see ``set_cell_extent``).
        """
        try:
            self._content_offset = (float(dx), float(dy))
        except (TypeError, ValueError):
            self._content_offset = (0.0, 0.0)
        self.update()

    # -- layout cell extent (the capsule's reserved space) ------------------

    def cell_extent(self) -> int:
        """This tile's along-axis layout extent (``CELL`` unless grown)."""
        try:
            return int(self._cell_extent)
        except Exception:
            return CELL

    def set_cell_extent(self, extent: int, horizontal: bool) -> None:
        """Grow/shrink this tile's own layout cell along the toolbar axis.

        The extra run sits *after* the button and is the space the selected
        tool's capsule needs: because it belongs to the cell, the box
        layout really moves every following tool down instead of leaving
        them under a paint-only offset. The button keeps its CELL size, its
        normal spacing to its neighbours and its own hit target; only empty
        run is added, so nothing is compressed, scaled or overlapped.
        """
        try:
            extent = int(max(CELL, extent))
        except (TypeError, ValueError):
            extent = CELL
        self._cell_extent = extent
        try:
            if horizontal:
                self.setFixedSize(extent, CELL)
            else:
                self.setFixedSize(CELL, extent)
        except Exception:
            pass
        self.update()

    def _glyph(self, name: str, color: str, size: int,
               background: str | None = None) -> QPixmap:
        key = (name, color, size, background)
        cached = self._icon_cache.get(key)
        if cached is None or cached.isNull():
            try:
                if background is None:
                    cached = _icon_pixmap(name, color, size)
                else:
                    # Configured tool color: never near-invisible, either theme.
                    cached = _tool_pixmap(name, color, background, size)
            except Exception:
                cached = QPixmap()
            self._icon_cache[key] = cached
        return cached

    # -- single coherent paint pass ----------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        slot = self._slot
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Tactile language in paint: 1px lift on hover, 1px
            # compression while pressed; halo wash unchanged.
            try:
                lift = 1 if self.isDown() else (
                    -1 if self.underMouse() else 0)
                if lift:
                    painter.translate(0, lift)
            except Exception:
                pass
            # Local separation offset (paint-only): the selected tool's own
            # content rises with the capsule, never its cell. The halo
            # below is capped to keep its whole silhouette inside the cell
            # whatever the offset, so the ink can never be cut by the tile.
            ox = oy = 0.0
            try:
                ox, oy = self.content_offset
                if ox or oy:
                    painter.translate(ox, oy)
            except Exception:
                ox = oy = 0.0
            if not self.isEnabled():
                painter.setOpacity(0.4)
            # The painted button is always ONE CELL square anchored at the
            # tile's own origin: a grown cell (the capsule's reserved run)
            # adds empty space *after* the button, never a larger button.
            rect = QRect(0, 0, CELL, CELL).adjusted(3, 3, -3, -3)

            # Face: transparent at rest; ONE filled circular state language
            # shared with the capsule (no borders anywhere):
            #   selected        -> opaque near-black disc
            #   hover / pressed -> a restrained ink wash over it
            # The accent-soft selection wash, its accent edge and the
            # hover accent rim are gone: three border answers to a
            # question that is answered with fills.
            halo = rect.adjusted(2, 2, -2, -2)
            # Cap the halo's own shift so the whole 30px circle always fits
            # inside the cell: an uncapped halo would lose its lower arc at
            # the tile's edge. The glyph keeps the entire offset — the halo
            # is the state fill behind it, and staying whole matters more
            # than following the last few pixels.
            try:
                base = QRect(0, 0, CELL, CELL).adjusted(5, 5, -5, -5)
                bounds = QRect(0, 0, CELL, CELL)
                cx = max(bounds.left() - base.left(),
                         min(bounds.right() - base.right(), ox))
                cy = max(bounds.top() - base.top(),
                         min(bounds.bottom() - base.bottom(), oy))
                if cx or cy:
                    halo = halo.translated(cx - ox, cy - oy)
            except Exception:
                pass
            side = min(halo.width(), halo.height())
            halo = halo.adjusted((halo.width() - side) // 2,
                                 (halo.height() - side) // 2,
                                 -((halo.width() - side + 1) // 2),
                                 -((halo.height() - side + 1) // 2))
            enabled = self.isEnabled()
            hovered = self.underMouse()
            down = self.isDown()
            painter.setPen(Qt.PenStyle.NoPen)
            if enabled and self.isChecked() and not self._capsule_active:
                # The same near-black disc the capsule's upper control
                # uses, so a selected tool reads identically with and
                # without the capsule (Pan / Selection included). While the
                # capsule paints this tile's highlight the disc is skipped:
                # it is the same colour as the capsule's body, so yielding
                # it changes nothing except that the capsule's single
                # highlight disc is no longer covered.
                painter.setBrush(selected_surface(t))
                painter.drawEllipse(halo)
            if enabled and (hovered or down):
                painter.setBrush(hover_wash(t, pressed=down))
                painter.drawEllipse(halo)
            if self.keyboard_focused:
                # Keyboard-only focus affordance. A mouse click focuses the
                # button too, and neither the selected nor the hovered tool
                # may wear a ring, so this is deliberately not shown for a
                # mouse-driven focus.
                try:
                    focus = QColor(t.input_focus)
                except Exception:
                    focus = QColor("#8B93E0")
                pen = QPen(_with_alpha(focus, 160))
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(halo.adjusted(2, 2, -2, -2))

            try:
                base = QColor(slot.color)
                if not base.isValid():
                    base = QColor("#000000")
            except Exception:
                base = QColor("#000000")

            if slot.kind == ToolType.PEN:
                self._paint_pen(painter, rect, base)
            elif slot.kind == ToolType.HIGHLIGHTER:
                self._paint_highlighter(painter, rect, base, slot)
            else:
                self._paint_icon(painter, rect, slot)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass

    def _glyph_color(self, slot: ToolInstance) -> str:
        """Configured color verbatim: contrast treatment lives in icons.py.

        The theme-aware keyline/luminance handling in ``tool_pixmap`` keeps
        near-background colors visible on both dark and light surfaces, so
        the slot color is passed through untouched (identity preserved).
        """
        try:
            base = QColor(slot.color)
            if not base.isValid():
                base = QColor("#1b1e26")
        except Exception:
            base = QColor("#1b1e26")
        return base.name()

    def _blit_centered(self, painter: QPainter, rect, glyph: QPixmap,
                         cache_key=None, extra_dy: float = 0.0) -> None:
        """Draw ``glyph`` with its painted ink optically centered on ``rect``.

        Uses measured alpha bounds (not viewBox center), so asymmetric
        silhouettes, strokes, and keylines all sit truly centered.
        The anchor is a float center: QRect.center() truncates on
        even-sized rects (e.g. 34px at x=3 reports 19, true 20.0),
        which parked every glyph 1px up-left inside its halo.
        """
        try:
            dx, dy = _ink_offset(glyph, cache_key)
        except Exception:
            dx, dy = (0.0, 0.0)
        try:
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            x = cx - glyph.width() / 2.0 + dx
            y = cy - glyph.height() / 2.0 + dy + extra_dy
            # The neighbour separation is a content offset larger than this
            # cell's own padding, so clamp the drawn pixmap into the tile:
            # only the last row of a maximum-size glyph is ever affected,
            # and the glyph is never cut by the cell edge.
            try:
                x = max(0.0, min(float(CELL) - glyph.width(), x))
                y = max(0.0, min(float(CELL) - glyph.height(), y))
            except Exception:
                pass
            painter.drawPixmap(int(round(x)), int(round(y)), glyph)
        except Exception:
            pass

    def _paint_pen(self, painter: QPainter, rect, base: QColor) -> None:
        """Dedicated pen-nib glyph in the configured color + width cue."""
        try:
            width = float(self._slot.width_mm)
        except (TypeError, ValueError):
            width = 0.5
        size = max(18, min(24, int(18 + width * 2.0)))
        color = self._glyph_color(self._slot)
        glyph = self._glyph("pen-nib", color, size,
                            self._shelf_bg())
        if glyph.isNull():
            return
        self._blit_centered(painter, rect, glyph,
                            ("pen-nib", color, size, self._shelf_bg()))

    def _paint_highlighter(self, painter: QPainter, rect, base: QColor,
                           slot: ToolInstance) -> None:
        """Marker glyph in the configured color over a faint stroke wash."""
        try:
            opacity = max(0.15, min(1.0, float(slot.opacity)))
        except (TypeError, ValueError):
            opacity = 0.4
        wash = QColor(slot.color if QColor(slot.color).isValid()
                      else "#ffd60a")
        wash.setAlphaF(opacity * 0.45)
        height = stroke_height(slot) - 4
        # Float center (matches _blit_centered): wash aligns with glyph.
        cy = int(rect.top() + rect.height() / 2.0) + 8
        band = max(3, height)
        try:
            # Same guard as the glyph: clamp the wash band into the tile so
            # the deeper neighbour separation cannot cut its lower edge.
            cy = min(cy, (CELL - 1) - band + height // 2)
        except Exception:
            pass
        left, right = rect.left() + 8, rect.right() - 8
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(wash)
        painter.drawRoundedRect(left, cy - height // 2, right - left,
                                max(3, height), 3, 3)
        glyph = self._glyph("marker", self._glyph_color(slot), 22,
                            self._shelf_bg())
        if glyph.isNull():
            return
        # Ink-centered; the stroke wash sits low (+8y), so the glyph
        # composition is balanced with a gentle -1 lift (not the old -3).
        self._blit_centered(painter, rect, glyph,
                            ("marker", self._glyph_color(slot), 22,
                             self._shelf_bg()),
                            extra_dy=-1.0)

    def _paint_icon(self, painter: QPainter, rect, slot: ToolInstance) -> None:
        t = self._tokens
        if slot.kind in _SHAPE_ICONS:
            # Shape/line tools paint their live configuration natively
            # (fill + style + endings), not a static kind glyph — the
            # icon is the ToolInstance, not just its type.
            self._paint_shape_tool(painter, rect, slot)
            return
        icon_name = _NAV_ICONS.get(slot.kind, "pen")
        color = t.accent if self.isChecked() else t.text_secondary
        glyph = self._glyph(icon_name, color, 20)
        key = (icon_name, color, 20, None)
        if glyph.isNull():
            return
        self._blit_centered(painter, rect, glyph, key)

    def _shape_pen(self, slot: ToolInstance) -> QPen:
        """Contrast-safe outline pen honoring width + line style."""
        from app.icons import ensure_contrast_fg as _ecfg

        try:
            width = max(1.5, min(2.5, 1.0 + float(slot.width_mm) * 0.8))
        except (TypeError, ValueError):
            width = 2.0
        try:
            color = _ecfg(str(slot.color), self._shelf_bg())
        except Exception:
            color = str(slot.color)
        pen = QPen(QColor(color))
        pen.setWidthF(width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        try:
            style = str(slot.line_style)
        except Exception:
            style = "solid"
        try:
            if style == "dashed":
                pen.setStyle(Qt.PenStyle.DashLine)
            elif style == "dotted":
                pen.setStyle(Qt.PenStyle.DotLine)
            else:
                pen.setStyle(Qt.PenStyle.SolidLine)
        except Exception:
            pass
        return pen

    def _shape_ending(self, painter: QPainter, p, direction,
                      ending: str, color: QColor) -> None:
        """Ending marker at point ``p`` facing ``direction`` (unit x,y)."""
        try:
            dx, dy = direction
            px, py = float(p[0]), float(p[1])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            if ending == "dot":
                painter.drawEllipse(int(round(px - 2.5)),
                                    int(round(py - 2.5)), 5, 5)
            elif ending == "bar":
                nx, ny = -dy, dx
                painter.setBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(color)
                pen.setWidth(2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(int(round(px - nx * 4)),
                                 int(round(py - ny * 4)),
                                 int(round(px + nx * 4)),
                                 int(round(py + ny * 4)))
            elif ending == "arrow":
                painter.setBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(color)
                pen.setWidth(2)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                nx, ny = -dy, dx
                for s in (-1.0, 1.0):
                    painter.drawLine(
                        int(round(px)), int(round(py)),
                        int(round(px - dx * 6 + nx * 3 * s)),
                        int(round(py - dy * 6 + ny * 3 * s)))
        except Exception:
            pass

    def _paint_shape_tool(self, painter: QPainter, rect,
                          slot: ToolInstance) -> None:
        """Live shape/line/arrow icon: fill + style + endings + contrast."""
        try:
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            pen = self._shape_pen(slot)
            brush_color = QColor(pen.color())
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            kind = slot.kind
            fillable = kind in (ToolType.RECTANGLE, ToolType.ELLIPSE,
                                ToolType.POLYGON)
            if fillable:
                try:
                    fill_on = bool(slot.fill_enabled)
                except Exception:
                    fill_on = False
                if fill_on:
                    try:
                        fill = QColor(slot.fill_color)
                        if not fill.isValid():
                            raise ValueError(slot.fill_color)
                        try:
                            op = max(0.0, min(1.0, float(slot.fill_opacity)))
                        except (TypeError, ValueError):
                            op = 1.0
                        fill.setAlphaF(op)
                    except Exception:
                        fill = None
                    # Faithful fill color; the contrast-safe outline drawn
                    # over it guarantees the silhouette reads even when
                    # fill ~ background or fill ~ stroke.
                    if fill is not None:
                        painter.setBrush(fill)
            if kind == ToolType.RECTANGLE:
                painter.drawRoundedRect(int(round(cx - 8)),
                                        int(round(cy - 6)), 16, 12, 2, 2)
            elif kind == ToolType.ELLIPSE:
                painter.drawEllipse(int(round(cx - 8)),
                                    int(round(cy - 6)), 16, 12)
            elif kind == ToolType.POLYGON:
                try:
                    import math as _math

                    pts = []
                    for i in range(5):
                        a = -_math.pi / 2.0 + i * 2.0 * _math.pi / 5.0
                        pts.append((cx + 8.0 * _math.cos(a),
                                    cy + 8.0 * _math.sin(a)))
                    from PySide6.QtGui import QPolygonF as _QF
                    from PySide6.QtCore import QPointF as _QP

                    painter.drawPolygon(_QF([_QP(x, y) for x, y in pts]))
                except Exception:
                    painter.drawEllipse(int(round(cx - 8)),
                                        int(round(cy - 8)), 16, 16)
            else:  # LINE / ARROW: diagonal shaft + configured endings
                x0, y0 = cx - 7.0, cy + 7.0
                x1, y1 = cx + 7.0, cy - 7.0
                painter.drawLine(int(round(x0)), int(round(y0)),
                                 int(round(x1)), int(round(y1)))
                try:
                    import math as _math

                    length = max(1e-6, _math.hypot(x1 - x0, y1 - y0))
                    direction = ((x1 - x0) / length, (y1 - y0) / length)
                    self._shape_ending(painter, (x0, y0),
                                       (-direction[0], -direction[1]),
                                       str(slot.start_ending), brush_color)
                    self._shape_ending(painter, (x1, y1), direction,
                                       str(slot.end_ending), brush_color)
                except Exception:
                    pass
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------

    def sizeHint(self):  # noqa: N802
        return QSize(CELL, CELL)
