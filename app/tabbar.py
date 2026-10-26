"""Document tab strip, integrated into the window-controls row.

One horizontal row shared with minimize / maximize / close: Inkfall's own
application mark acts as the permanent Home button, followed by one
custom-painted tab per open document (title, dirty dot, close affordance),
then the open-documents menu immediately after the tab sequence. Horizontal
overflow is handled with edge chevrons plus that menu; drag reordering never
changes a tab's identity (ids stay stable).

The strip is presentation only. It owns no documents and no render
pipelines: :class:`~app.main_window.MainWindow` keeps the per-tab session
records and the single shared ``PdfViewer``; this widget only reports
user intent through signals:

* ``activateRequested(str)`` — a tab (or an overflow-menu row) was chosen.
* ``closeRequested(str)``   — that tab's close affordance / middle click /
  the row's X in the overflow menu (the same guarded close path).
* ``homeRequested()``       — the permanent Home control.
* ``tabsReordered(list)``   — committed drag order (list of tab ids).
* ``closeAllRequested()``   — the overflow menu's Close All action.
* ``pointerExited()``       — the pointer left the strip (fullscreen hide).
* ``chromeDragRequested()`` — empty strip background: start a native move.
* ``chromeMaximizeRequested()`` — empty strip background double-clicked.

Tab shapes and the Home control sit flush against the bottom edge of the
row (a small top inset keeps them clear of the window's top grip), so the
sequence reads as one strip connected to the content below rather than a
row of floating pills.

The strip paints no plate and no border of its own: it sits transparently
inside the title bar, which owns the single row background and hairline.
``surface_raised`` / ``surface_overlay`` washes, the one interaction accent
for the active underline, and ``danger`` only for the dirty dot and the
close hover.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QAbstractButton, QWidget, QWidgetAction

from app.icons import app_icon, icon, pixmap
from app.theme.tokens import InkfallTokens, token_rgba
from app.theme.typography import role_font

__all__ = ["TabStrip"]

#: Breathing room between the window's top edge and the tab band. The
#: outer 6px of the row stay owned by the window resize grips; 4px keeps
#: the shapes clear of the very top without detaching them from the row.
_TAB_TOP_INSET = 4
_TAB_MIN_W = 132
_TAB_MAX_W = 216
_TAB_SPACING = 4
_RAIL_PAD = 6
_HOME_W = 34
_HOME_MARGIN = 6
_EDGE_W = 20
_MENU_W = 26
_CLOSE_SIZE = 22
#: Horizontal padding inside a tab: the title's inset from the left edge,
#: and the gap reserved between the title area and the close affordance.
_TAB_PAD_X = 14
_TAB_CLOSE_PAD = 8
_DRAG_THRESHOLD = 7


def _band(height: int) -> tuple[int, int]:
    """``(top, height)`` of the tab band inside a row of ``height`` px.

    The band is anchored to the BOTTOM of the row (tabs connect to the
    content edge) and keeps the top inset whenever the row is tall enough
    to still fit a usable shape; during a fullscreen reveal tween the
    band fills the whole transient height instead of clipping.
    """
    h = max(0, int(height))
    if h < 24:
        return 0, h
    top = min(_TAB_TOP_INSET, h - 24)
    return top, h - top


def _tab_shape(rect: QRectF, radius: float) -> QPainterPath:
    """Tab silhouette: softly rounded top corners, flat flush bottom."""
    path = QPainterPath()
    r = max(0.0, min(float(radius), rect.width() / 2.0, rect.height()))
    bottom = rect.bottom()
    path.moveTo(rect.left(), bottom)
    path.lineTo(rect.left(), rect.top() + r)
    path.quadTo(rect.left(), rect.top(), rect.left() + r, rect.top())
    path.lineTo(rect.right() - r, rect.top())
    path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + r)
    path.lineTo(rect.right(), bottom)
    path.closeSubpath()
    return path


def _qc(value: str) -> QColor:
    """QColor from a token (robust for ``rgba()`` and ``#rrggbb``)."""
    r, g, b, a = token_rgba(value)
    return QColor(r, g, b, int(round(a * 255)))


def _is_dark(tokens: InkfallTokens) -> bool:
    try:
        return str(getattr(tokens, "name", "dark")).casefold() != "light"
    except Exception:
        return True


def _surface_wash(tokens: InkfallTokens) -> QColor:
    """Base wash for the Home mark and idle tab shapes.

    One shared, theme-aware surface treatment layered over the title-row
    background so the strip's shapes read as gently raised tabs rather
    than holes in the row: ``surface_raised`` at a restrained alpha
    (half-strength in dark where the ramp is deep, slightly stronger in
    light so white reads over the dimmed warm row). Active tabs still
    paint full ``surface_raised`` + hairline above this base.
    """
    r, g, b, _a = token_rgba(tokens.surface_raised)
    alpha = 0.50 if _is_dark(tokens) else 0.55
    return QColor(r, g, b, int(round(alpha * 255)))


class _TabCloseButton(QAbstractButton):
    """The per-tab close affordance (danger tint on hover)."""

    def __init__(self, parent: QWidget, tokens: InkfallTokens,
                 focusable: bool = False) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setFixedSize(_CLOSE_SIZE, _CLOSE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Keyboard-focusable only where Tab navigation cannot fight the
        # strip's own focus chain (overflow-menu rows), never inside tabs.
        self.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if focusable
            else Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Close tab")
        self.setAccessibleDescription("Close this document tab")

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        hovered = self.underMouse() or self.isDown()
        if hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.danger) if self.isDown() else _qc(t.surface_overlay))
            p.drawEllipse(rect)
        # Shared focus ring: only the keyboard-focusable variants (the
        # overflow-menu rows) can hold focus; in-tab closes are NoFocus
        # so this never paints there.
        if self.hasFocus():
            from app.focus import draw_focus_ring

            draw_focus_ring(p, QRectF(self.rect()), t, radius=None)
        color = t.danger if hovered else t.text_muted
        pix = pixmap("x", color, 12)
        if not pix.isNull():
            x = int(round((self.width() - 12) / 2.0))
            y = int(round((self.height() - 12) / 2.0))
            p.drawPixmap(x, y, pix)


class _TabButton(QAbstractButton):
    """One document tab (custom painted, draggable, keyboard focusable)."""

    def __init__(self, strip: "TabStrip", tab_id: str, title: str,
                 tokens: InkfallTokens) -> None:
        super().__init__(strip._rail)
        self._strip = strip
        self.tab_id = str(tab_id)
        self._tokens = tokens
        self._title = "Untitled"
        self._active = False
        self._dirty = False
        self._press_point = QPoint()
        self._press_offset = 0
        self._drag_armed = False
        self._dragging = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setFont(self._title_font(tokens))
        self._close = _TabCloseButton(self, tokens)
        self._close.clicked.connect(
            lambda: self._strip._emit_close(self.tab_id))
        self.clicked.connect(
            lambda: self._strip._emit_activate(self.tab_id))
        self.update_title(title)
        self._close.raise_()

    @staticmethod
    def _title_font(tokens: InkfallTokens) -> QFont:
        font = role_font(tokens, "nav")
        font.setPointSize(tokens.fs_meta)
        try:
            font.setWeight(QFont.Weight(tokens.w_medium))
        except Exception:
            pass
        return font

    # -- public state ---------------------------------------------------

    def _sync_accessibility(self) -> None:
        """One source for the tab's accessible text (title + dirty state)."""
        self.setAccessibleName(self._title)
        self.setAccessibleDescription(
            "Document tab with unsaved changes" if self._dirty
            else "Document tab")

    def update_title(self, title: str) -> None:
        self._title = str(title or "Untitled")
        self._sync_accessibility()
        self.update()

    def set_dirty(self, dirty: bool) -> None:
        dirty = bool(dirty)
        if dirty != self._dirty:
            self._dirty = dirty
            self._sync_accessibility()
            self.update()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active != self._active:
            self._active = active
            self.update()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.setFont(self._title_font(tokens))
        self._close.update_tokens(tokens)
        self.update()

    # -- geometry -------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._close.move(self.width() - _CLOSE_SIZE - _TAB_CLOSE_PAD,
                         max(0, (self.height() - _CLOSE_SIZE) // 2))
        self._close.raise_()

    # -- input ----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_point = event.position().toPoint()
            self._press_offset = max(0, int(event.position().x()))
            self._drag_armed = True
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_armed and (event.buttons() & Qt.MouseButton.LeftButton):
            moved = (event.position().toPoint() - self._press_point
                     ).manhattanLength()
            if not self._dragging and moved >= _DRAG_THRESHOLD:
                self._dragging = True
                self._strip._drag_started(self)
            if self._dragging:
                self._strip._drag_moved(
                    self, event.globalPosition().toPoint())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._strip._emit_close(self.tab_id)
            event.accept()
            return
        if self._dragging:
            self._dragging = False
            self._drag_armed = False
            self._strip._drag_finished(self)
            event.accept()
            return
        self._drag_armed = False
        super().mouseReleaseEvent(event)

    # -- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Flush bottom: only the sides/top get the half-pixel inset so a
        # 1px stroke stays inside the widget; the bottom edge stays exact.
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, 0.0)
        shape = _tab_shape(rect, float(t.radius_sm))
        hovered = self.underMouse()
        if self._drag_armed and self.isDown():
            background = _qc(t.surface_overlay)
        elif self._active:
            background = _qc(t.surface_raised)
        elif hovered:
            background = _qc(t.surface_overlay)
        else:
            # Idle tabs keep a subtle raised wash over the row instead of
            # transparency (active tabs paint the full raised surface).
            background = _surface_wash(t)
        if background.alpha() > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(background)
            p.drawPath(shape)
        if self._active:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(t.border_subtle), 1.0))
            p.drawPath(shape)
            underline = QRectF(
                rect.left() + 9.0, rect.bottom() - 2.4,
                max(4.0, rect.width() - 18.0), 2.4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.accent))
            p.drawRoundedRect(underline, 1.2, 1.2)
        # Title (elided; dirty dot before the close affordance).
        metrics = QFontMetrics(self.font())
        left = rect.left() + _TAB_PAD_X
        right = rect.right() - float(_CLOSE_SIZE + _TAB_CLOSE_PAD)
        if self._dirty:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.danger))
            p.drawEllipse(QRectF(right - 9.0, rect.center().y() - 2.1,
                                 4.2, 4.2))
            right -= 12.0
        width = max(0.0, right - left)
        elided = metrics.elidedText(
            self._title, Qt.TextElideMode.ElideRight, int(width))
        color = (t.text_primary if (self._active or hovered)
                 else t.text_secondary)
        p.setFont(self.font())
        p.setPen(_qc(color))
        p.drawText(
            QRectF(left, rect.top(), width, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(t.input_focus), 1.4))
            p.drawPath(_tab_shape(rect.adjusted(1.0, 1.0, -1.0, 0.0),
                                  float(t.radius_sm)))


class _IconButton(QAbstractButton):
    """Small icon-only chrome button (Home mark, overflow menu)."""

    def __init__(self, icon_name: str, tokens: InkfallTokens, parent=None,
                 accessible_name: str = "", brand: bool = False) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._tokens = tokens
        self._checked_look = False
        self._icon_size = 16
        self._brand = bool(brand)
        self._brand_icon = app_icon() if self._brand else None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Initial size only: the strip re-seats the button to the live
        # tab-band geometry on every relayout.
        self.setFixedSize(_HOME_W, _HOME_W)
        self.setAccessibleName(accessible_name)

    def set_checked_look(self, on: bool) -> None:
        on = bool(on)
        if on != self._checked_look:
            self._checked_look = on
            self.update()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def _draw_icon(self, painter: QPainter, color: str) -> None:
        if self._brand and self._brand_icon is not None:
            # The brand mark is used exactly as supplied (never recolored).
            pm = self._brand_icon.pixmap(self._icon_size, self._icon_size)
            if not pm.isNull():
                painter.drawPixmap(
                    int(round((self.width() - pm.width()) / 2.0)),
                    int(round((self.height() - pm.height()) / 2.0)), pm)
                return
        pix = pixmap(self._icon_name, color, self._icon_size)
        if not pix.isNull():
            x = int(round((self.width() - self._icon_size) / 2.0))
            y = int(round((self.height() - self._icon_size) / 2.0))
            painter.drawPixmap(x, y, pix)

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._brand:
            # The Home mark shares the tab silhouette (rounded top, flush
            # bottom) so it reads as the first shape of the same strip.
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, 0.0)
        else:
            rect = QRectF(self.rect()).adjusted(0.5, 2.5, -0.5, -2.5)
        hovered = self.underMouse()
        background = None
        if self.isDown():
            background = _qc(t.surface_overlay)
        elif self._checked_look:
            background = _qc(t.accent_soft)
        elif hovered:
            background = _qc(t.surface_overlay)
        if background is None and self._brand:
            # The Home mark carries the same base wash as the tab shapes:
            # a constant navigation control, identical in the Library and
            # in the PDF workspace (never a page-selection indicator).
            background = _surface_wash(t)
        if background is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(background)
            if self._brand:
                # Slightly rounder top corners than the tabs: the Home
                # mark leads the strip (bottom stays flush with the band).
                p.drawPath(_tab_shape(rect, float(t.radius_md)))
            else:
                p.drawRoundedRect(rect, float(t.radius_sm),
                                  float(t.radius_sm))
        if self._checked_look:
            color = t.accent
        elif hovered:
            color = t.text_primary
        else:
            color = t.text_secondary
        self._draw_icon(p, color)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(t.input_focus), 1.4))
            if self._brand:
                p.drawPath(_tab_shape(rect.adjusted(1.0, 1.0, -1.0, 0.0),
                                      float(t.radius_md)))
            else:
                p.drawRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0),
                                  float(t.radius_sm), float(t.radius_sm))


class _EdgeButton(QAbstractButton):
    """Overflow scroll chevron (hidden when its direction has no content)."""

    stepRequested = Signal(int)

    def __init__(self, forward: bool, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._forward = bool(forward)
        self._tokens = tokens
        self._has_more = False
        # Initial size only: _sync_edges re-seats the chevron to the live
        # tab-band geometry.
        self.setFixedSize(_EDGE_W, 28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(
            "Scroll tabs forward" if forward else "Scroll tabs back")
        self.clicked.connect(
            lambda: self.stepRequested.emit(1 if self._forward else -1))

    def has_more(self) -> bool:
        return self._has_more

    def set_overflow(self, has_more: bool) -> None:
        has_more = bool(has_more)
        if has_more != self._has_more:
            self._has_more = has_more
            self.setCursor(Qt.CursorShape.PointingHandCursor if has_more
                           else Qt.CursorShape.ArrowCursor)
            self.update()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        if not self._has_more:
            # No overflow in this direction: the control is hidden, so no
            # divider/placeholder is painted (the old hairline pair was the
            # stray double line at the trailing edge).
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.underMouse() or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.surface_overlay))
            p.drawRoundedRect(
                QRectF(self.rect()).adjusted(1.0, 3.0, -1.0, -3.0),
                float(t.radius_sm), float(t.radius_sm))
        name = "chevron-right" if self._forward else "chevron-left"
        pix = pixmap(name, t.text_secondary, 12)
        if not pix.isNull():
            x = int(round((self.width() - 12) / 2.0))
            y = int(round((self.height() - 12) / 2.0))
            p.drawPixmap(x, y, pix)


class _MenuRow(QWidget):
    """One open-document row inside the overflow menu.

    Painted (not a QMenuItem) so a row can carry its own X affordance:
    activating the row body requests the document; the X requests that
    tab's close through the SAME guarded path as the tab strip
    (MainWindow owns the dirty guard — the strip only reports intent).
    Keyboard: Tab/arrow traversal between rows, Enter/Space activates,
    Right focuses the X, Left returns focus to the row, Escape closes
    the popup natively.
    """

    activated = Signal(str)
    closeRequested = Signal(str)

    def __init__(self, tab_id: str, title: str, dirty: bool, active: bool,
                 tokens: InkfallTokens) -> None:
        super().__init__()
        self.tab_id = str(tab_id)
        self._title = str(title or "Untitled")
        self._dirty = bool(dirty)
        self._active = bool(active)
        self._tokens = tokens
        self.setFixedSize(268, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFont(self._title_font(tokens))
        self._close = _TabCloseButton(self, tokens, focusable=True)
        self._close.clicked.connect(
            lambda: self.closeRequested.emit(self.tab_id))
        self._sync_accessibility()
        self._close.raise_()

    @staticmethod
    def _title_font(tokens: InkfallTokens) -> QFont:
        font = role_font(tokens, "nav")
        font.setPointSize(tokens.fs_meta)
        try:
            font.setWeight(QFont.Weight(tokens.w_medium))
        except Exception:
            pass
        return font

    def _sync_accessibility(self) -> None:
        name = self._title
        if self._active:
            name += ", active"
        if self._dirty:
            name += ", unsaved changes"
        self.setAccessibleName(name)
        self.setAccessibleDescription("Open this document tab")
        self._close.setAccessibleName(f"Close {self._title}")
        self._close.setAccessibleDescription(
            "Close this document tab (asks before discarding unsaved "
            "changes)")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._close.move(self.width() - _CLOSE_SIZE - _TAB_CLOSE_PAD,
                         max(0, (self.height() - _CLOSE_SIZE) // 2))
        self._close.raise_()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.tab_id)
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            self._close.setFocus()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                if not isinstance(self.childAt(event.position().toPoint()),
                                  QAbstractButton):
                    self.activated.emit(self.tab_id)
                    event.accept()
                    return
            except Exception:
                pass
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        t = self._tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hovered = self.underMouse()
        rect = QRectF(self.rect()).adjusted(2.0, 1.0, -2.0, -1.0)
        if self._active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.accent_soft))
            p.drawRoundedRect(rect, 7.0, 7.0)
        elif hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.surface_overlay))
            p.drawRoundedRect(rect, 7.0, 7.0)
        # Document glyph: accent on the active row, muted otherwise.
        color = (t.accent if self._active
                 else (t.text_primary if hovered else t.text_secondary))
        pix = pixmap("doc", color, 16)
        if not pix.isNull():
            p.drawPixmap(12, int((self.height() - 16) / 2), pix)
        metrics = QFontMetrics(self.font())
        left = 36.0
        right = float(self.width() - _CLOSE_SIZE - _TAB_CLOSE_PAD - 4)
        if self._dirty:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_qc(t.danger))
            p.drawEllipse(QRectF(right - 9.0, rect.center().y() - 2.1,
                                 4.2, 4.2))
            right -= 12.0
        width = max(0.0, right - left)
        elided = metrics.elidedText(
            self._title, Qt.TextElideMode.ElideRight, int(width))
        p.setFont(self.font())
        p.setPen(_qc(t.text_primary if (self._active or hovered)
                     else t.text_secondary))
        p.drawText(
            QRectF(left, rect.top(), width, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_qc(t.input_focus), 1.2))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                              7.0, 7.0)


class TabStrip(QWidget):
    """The document tab strip (Home + tabs + overflow + reordering)."""

    activateRequested = Signal(str)
    closeRequested = Signal(str)
    homeRequested = Signal()
    tabsReordered = Signal(list)
    closeAllRequested = Signal()
    pointerExited = Signal()
    chromeDragRequested = Signal()
    chromeMaximizeRequested = Signal()
    menuClosed = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._tabs: list[_TabButton] = []
        self._active_id: str | None = None
        self._scroll = 0
        self._drag_tab: _TabButton | None = None
        self._drag_order_before: list[str] = []
        self._menu_open = False
        self._menu = None
        # Overflow-menu rows by tab id (the QWidgetAction hosting each
        # row); kept in step with remove_tab/clear_tabs so a row can
        # never outlive its tab or linger after a cancelled close guard.
        self._menu_rows: dict = {}
        # Lives inside the title-bar row: transparent, no plate/no border.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        # Inkfall's own brand mark doubles as the permanent Home control.
        self._home = _IconButton(
            "library", tokens, self, "Home / Library", brand=True)
        self._home._icon_size = 18
        self._home.clicked.connect(self.homeRequested.emit)
        self._viewport = QWidget(self)
        self._viewport.setMouseTracking(True)
        self._viewport.setAttribute(
            Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._rail = QWidget(self._viewport)
        self._rail.setMouseTracking(True)
        self._rail.setAttribute(
            Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._edge_back = _EdgeButton(False, tokens, self)
        self._edge_fwd = _EdgeButton(True, tokens, self)
        self._edge_back.stepRequested.connect(lambda _d: self._step_scroll(-1))
        self._edge_fwd.stepRequested.connect(lambda _d: self._step_scroll(1))
        self._menu_btn = _IconButton(
            "chevron-down", tokens, self, "All document tabs")
        self._menu_btn.setFixedSize(_MENU_W, _MENU_W)
        self._menu_btn._icon_size = 14
        self._menu_btn.clicked.connect(self._show_menu)
        self._relayout()

    # -- theme ----------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._home.update_tokens(tokens)
        self._menu_btn.update_tokens(tokens)
        self._edge_back.update_tokens(tokens)
        self._edge_fwd.update_tokens(tokens)
        for tab in self._tabs:
            tab.update_tokens(tokens)
        self.update()

    def set_home_active(self, active: bool) -> None:
        """Kept for call compatibility; the argument has no visual effect.

        The Home mark is a constant app-navigation control, not a
        page-selection indicator: its base surface, hover, pressed and
        focus styling are identical whether the Library or a PDF
        workspace is showing. Clicking it still navigates Home.
        """
        return

    # -- tab API --------------------------------------------------------

    def tab_ids(self) -> list[str]:
        return [tab.tab_id for tab in self._tabs]

    def add_tab(self, tab_id: str, title: str, dirty: bool = False,
                active: bool = False) -> None:
        tab_id = str(tab_id)
        if any(tab.tab_id == tab_id for tab in self._tabs):
            return
        tab = _TabButton(self, tab_id, title, self._tokens)
        tab.set_dirty(dirty)
        self._tabs.append(tab)
        if active:
            self._active_id = tab_id
        tab.set_active(self._active_id == tab_id)
        tab.show()
        self._relayout()
        self.ensure_visible(tab_id)

    def remove_tab(self, tab_id: str) -> None:
        tab_id = str(tab_id)
        for index, tab in enumerate(self._tabs):
            if tab.tab_id != tab_id:
                continue
            self._tabs.pop(index)
            if self._active_id == tab_id:
                self._active_id = None
            tab.setParent(None)
            tab.deleteLater()
            break
        self._relayout()
        # The overflow menu keeps itself in step: the row leaves the live
        # popup only when the guarded close actually completed (a
        # cancelled Save/Discard/Cancel keeps the row, and the tab).
        self._drop_menu_row(tab_id)

    def _drop_menu_row(self, tab_id: str) -> None:
        action = self._menu_rows.pop(str(tab_id), None)
        if action is None:
            return
        if self._menu is not None:
            try:
                self._menu.removeAction(action)
            except Exception:
                pass
        try:
            widget = action.defaultWidget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        except Exception:
            pass
        if self._menu is not None and not self._menu_rows:
            self._dismiss_menu()

    def clear_tabs(self) -> None:
        for tab in self._tabs:
            tab.setParent(None)
            tab.deleteLater()
        self._tabs = []
        self._active_id = None
        self._drag_tab = None
        self._scroll = 0
        self._relayout()
        for tab_id in list(self._menu_rows):
            self._drop_menu_row(tab_id)

    def activate_tab(self, tab_id: str) -> None:
        tab_id = str(tab_id)
        if self._active_id != tab_id:
            self._active_id = tab_id
            for tab in self._tabs:
                tab.set_active(tab.tab_id == tab_id)
        self.ensure_visible(tab_id)
        self.update()

    def update_tab(self, tab_id: str, title: str | None = None,
                   dirty: bool | None = None) -> None:
        tab_id = str(tab_id)
        for tab in self._tabs:
            if tab.tab_id != tab_id:
                continue
            if title is not None:
                tab.update_title(title)
            if dirty is not None:
                tab.set_dirty(dirty)
            break

    def ensure_visible(self, tab_id: str) -> None:
        """Scroll the minimum amount needed to show ``tab_id``."""
        for tab in self._tabs:
            if tab.tab_id != str(tab_id):
                continue
            x = tab.x()
            width = tab.width()
            view_width = self._viewport.width()
            if x < self._scroll:
                self._set_scroll(x - _RAIL_PAD)
            elif x + width > self._scroll + view_width:
                self._set_scroll(x + width - view_width + _RAIL_PAD)
            return

    # -- layout ---------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        self._layout_chrome()
        self._layout_slots()

    def _layout_chrome(self) -> None:
        """Home at the left, tab viewport, menu reserved at the far right.

        The menu resumes its "immediately after the last tab" position in
        :meth:`_layout_menu` once the rail width is known; reserving its
        slot here keeps the tabs from ever running under it (or under the
        window controls in the title bar).
        """
        height = self.height()
        top, band_h = _band(height)
        # Home shares the tab band exactly: same top inset, same flush
        # bottom edge, same silhouette family (see _IconButton paint).
        self._home.setFixedSize(_HOME_W, band_h)
        self._home.move(_HOME_MARGIN, top)
        has_tabs = bool(self._tabs)
        self._menu_btn.setVisible(has_tabs)
        self._menu_btn.setFixedSize(_MENU_W, _MENU_W)
        self._menu_btn.move(
            max(0, self.width() - 2 - _MENU_W),
            max(0, height - _MENU_W))
        left = _HOME_MARGIN + _HOME_W + 6
        right = self.width() - 2 - _MENU_W - 6
        self._viewport.setGeometry(
            left, top, max(0, right - left), band_h)
        self._viewport.setVisible(height > 0 and has_tabs)
        if not has_tabs:
            self._edge_back.setVisible(False)
            self._edge_fwd.setVisible(False)

    def _tab_width(self) -> int:
        count = len(self._tabs)
        if count <= 0:
            return _TAB_MAX_W
        available = max(160, self._viewport.width())
        raw = (available - 2 * _RAIL_PAD
               - (count - 1) * _TAB_SPACING) / float(count)
        return int(max(_TAB_MIN_W, min(_TAB_MAX_W, raw)))

    def _layout_slots(self, skip: _TabButton | None = None) -> None:
        height = self._viewport.height()
        tab_width = self._tab_width()
        for index, tab in enumerate(self._tabs):
            x = _RAIL_PAD + index * (tab_width + _TAB_SPACING)
            # Tabs fill the band: flush to the row's bottom edge (the
            # viewport already starts at the band top).
            tab.setFixedSize(tab_width, height)
            if tab is not skip:
                tab.move(x, 0)
        count = len(self._tabs)
        rail_width = (2 * _RAIL_PAD + count * tab_width
                      + max(0, count - 1) * _TAB_SPACING)
        self._rail.setFixedSize(max(0, rail_width), max(0, height))
        self._rail.move(-self._scroll, 0)
        self._clamp_scroll()

    def _clamp_scroll(self) -> None:
        maximum = max(0, self._rail.width() - self._viewport.width())
        self._scroll = max(0, min(maximum, int(self._scroll)))
        self._rail.move(-self._scroll, 0)
        self._sync_edges()
        self._layout_menu()

    def _set_scroll(self, value) -> None:
        maximum = max(0, self._rail.width() - self._viewport.width())
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self._scroll = max(0, min(maximum, value))
        self._rail.move(-self._scroll, 0)
        self._sync_edges()
        self._layout_menu()

    def _step_scroll(self, direction: int) -> None:
        amount = max(80, int(self._viewport.width() * 0.7))
        self._set_scroll(self._scroll + (amount if direction > 0 else -amount))

    def _sync_edges(self) -> None:
        """Chevrons only when that scroll direction has content.

        The controls are overlaid on the viewport edges (no width is
        reserved for them), so they never leave a gap before the open-files
        menu when the tabs fit. With no overflow in a direction the control
        is hidden and paints nothing; this removed the two trailing
        hairline dividers.
        """
        maximum = max(0, self._rail.width() - self._viewport.width())
        has_back = self._scroll > 0
        has_fwd = self._scroll < maximum
        self._edge_back.setFixedSize(_EDGE_W, self._viewport.height())
        self._edge_fwd.setFixedSize(_EDGE_W, self._viewport.height())
        self._edge_back.set_overflow(has_back)
        self._edge_fwd.set_overflow(has_fwd)
        self._edge_back.move(self._viewport.x(), self._viewport.y())
        self._edge_fwd.move(
            self._viewport.x() + self._viewport.width() - _EDGE_W,
            self._viewport.y())
        self._edge_back.setVisible(has_back)
        self._edge_fwd.setVisible(has_fwd)
        if has_back:
            self._edge_back.raise_()
        if has_fwd:
            self._edge_fwd.raise_()

    def _layout_menu(self) -> None:
        """Seat the open-files button right after the last tab.

        When the tabs overflow, the same expression is clamped to the right
        limit, keeping the menu permanently reachable. No stretch/spacer is
        involved and reordering/resizing simply re-runs this.
        """
        if not self._tabs:
            self._menu_btn.setVisible(False)
            return
        self._menu_btn.setVisible(True)
        count = len(self._tabs)
        tab_width = self._tab_width()
        content_right = (_RAIL_PAD + count * tab_width
                         + max(0, count - 1) * _TAB_SPACING)
        right_limit = max(0, self.width() - 2 - _MENU_W)
        x = self._viewport.x() + content_right - self._scroll + 6
        x = max(self._viewport.x(), min(x, right_limit))
        self._menu_btn.move(
            x, max(0, self.height() - _MENU_W))
        self._menu_btn.raise_()

    # -- interaction ----------------------------------------------------

    def _emit_activate(self, tab_id: str) -> None:
        if any(tab.tab_id == str(tab_id) for tab in self._tabs):
            self.activateRequested.emit(str(tab_id))

    def _emit_close(self, tab_id: str) -> None:
        if any(tab.tab_id == str(tab_id) for tab in self._tabs):
            self.closeRequested.emit(str(tab_id))

    def _drag_started(self, tab: _TabButton) -> None:
        self._drag_tab = tab
        self._drag_order_before = [item.tab_id for item in self._tabs]
        tab.raise_()

    def _drag_moved(self, tab: _TabButton, global_point) -> None:
        if self._drag_tab is not tab:
            return
        local = self._viewport.mapFromGlobal(global_point)
        x = local.x() + self._scroll - tab._press_offset
        maximum = max(0, self._rail.width() - tab.width())
        x = max(0, min(maximum, x))
        centre = x + tab.width() / 2.0
        target = 0
        for other in self._tabs:
            if other is tab:
                continue
            if other.x() + other.width() / 2.0 < centre:
                target += 1
        current = self._tabs.index(tab)
        if target != current:
            self._tabs.pop(current)
            self._tabs.insert(min(target, len(self._tabs)), tab)
            self._layout_slots(skip=tab)
        tab.move(int(x), tab.y())

    def _drag_finished(self, tab: _TabButton) -> None:
        if self._drag_tab is not tab:
            return
        self._drag_tab = None
        self._layout_slots()
        order = [item.tab_id for item in self._tabs]
        if order != self._drag_order_before:
            self.tabsReordered.emit(list(order))

    # -- overflow menu --------------------------------------------------

    def _show_menu(self) -> None:
        if not self._tabs:
            return
        # One live popup at a time: dismiss the previous one FIRST (its
        # aboutToHide teardown resets the row registry) before rebuilding.
        previous = self._menu
        if previous is not None:
            try:
                previous.close()
            except Exception:
                pass
        try:
            from app.menu_material import InkfallMenu

            menu = InkfallMenu(self, self._tokens)
        except Exception:
            from PySide6.QtWidgets import QMenu

            menu = QMenu(self)
        t = self._tokens
        # One focusable row per open document: activation, dirty dot and
        # a dedicated X (the guarded per-tab close — never a plain
        # activation or an unguarded close).
        self._menu_rows = {}
        for tab in self._tabs:
            row = _MenuRow(
                tab.tab_id, tab._title, tab._dirty,
                tab.tab_id == self._active_id, t)
            row.activated.connect(self._menu_row_activated)
            row.closeRequested.connect(self._menu_row_close_requested)
            action = QWidgetAction(menu)
            action.setDefaultWidget(row)
            menu.addAction(action)
            self._menu_rows[str(tab.tab_id)] = action
        menu.addSeparator()
        close_all = menu.addAction("Close All")
        try:
            close_all.setIcon(icon("trash", t.text_secondary, 16))
        except Exception:
            pass
        close_all.setEnabled(bool(self._tabs))
        close_all.triggered.connect(self._menu_close_all)

        self._menu = menu
        try:
            menu.aboutToHide.connect(self._on_menu_hidden)
        except Exception:
            pass

        anchor = self._menu_anchor(menu)
        self._menu_open = True
        try:
            # Anchor first, animate second. The old order animated from the
            # menu's pre-show position (~screen origin) and exec(anchor)
            # then dragged it back there.
            menu.popup(anchor)
        except Exception:
            self._menu_open = False
            self._menu = None
            self._menu_rows = {}
            try:
                menu.deleteLater()
            except Exception:
                pass
            return
        try:
            from app import fx as _fx

            _fx.animate_menu_open(menu, 130, -6, self)
        except Exception:
            pass

    def _menu_row_activated(self, tab_id: str) -> None:
        """Row body chosen: dismiss the popup, then activate the tab."""
        self._dismiss_menu()
        self.activateRequested.emit(str(tab_id))

    def _menu_row_close_requested(self, tab_id: str) -> None:
        """Row X chosen: the SAME guarded close path as the tab strip.

        The row itself is NOT removed here — MainWindow's dirty guard may
        cancel; the row leaves only via :meth:`remove_tab` once the close
        actually completed. X clicks never activate their row.
        """
        self.closeRequested.emit(str(tab_id))

    def _menu_close_all(self) -> None:
        """Final Close All action: dismiss, then report intent."""
        self._dismiss_menu()
        self.closeAllRequested.emit()

    def _dismiss_menu(self) -> None:
        """Close the live popup (state teardown runs via aboutToHide)."""
        menu = self._menu
        if menu is not None:
            try:
                menu.close()
            except Exception:
                pass

    def _menu_anchor(self, menu) -> QPoint:
        """Below/left-aligned with the menu button, kept on-screen.

        Uses live widget-to-global mapping, so moving/resizing the window,
        entering fullscreen, or scrolling the rail cannot leave a stale
        coordinate behind. No screen-space constants are baked in.
        """
        btn = self._menu_btn
        anchor = btn.mapToGlobal(QPoint(0, btn.height() + 2))
        try:
            screen = (QGuiApplication.screenAt(anchor)
                      or QGuiApplication.primaryScreen())
            if screen is None:
                return anchor
            bounds = screen.availableGeometry()
            hint = menu.sizeHint()
            x = anchor.x()
            if x + hint.width() > bounds.right():
                x = bounds.right() - hint.width()
            x = max(bounds.left(), x)
            y = anchor.y()
            if y + hint.height() > bounds.bottom():
                y = btn.mapToGlobal(QPoint(0, 0)).y() - hint.height() - 2
                y = max(bounds.top(), y)
            return QPoint(x, y)
        except Exception:
            return anchor

    def _on_menu_hidden(self) -> None:
        """Safe immediate teardown (native popup hide is not intercepted)."""
        self._menu_open = False
        menu = self._menu
        self._menu = None
        self._menu_rows = {}
        if menu is not None:
            try:
                menu.deleteLater()
            except Exception:
                pass
        try:
            self.menuClosed.emit()
        except Exception:
            pass

    # -- events ---------------------------------------------------------

    def _is_background(self, event) -> bool:
        """True when the press landed on empty strip space (not a control)."""
        try:
            child = self.childAt(event.position().toPoint())
        except Exception:
            child = None
        return not isinstance(child, QAbstractButton)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # The outer 6px top band stays owned by the window resize grips:
        # let it bubble to TitleBar (and its MainWindow event filter).
        try:
            if event.position().toPoint().y() <= 6:
                super().mousePressEvent(event)
                return
        except Exception:
            pass
        if (event.button() == Qt.MouseButton.LeftButton
                and self._is_background(event)):
            self.chromeDragRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        try:
            if event.position().toPoint().y() <= 6:
                super().mouseDoubleClickEvent(event)
                return
        except Exception:
            pass
        if (event.button() == Qt.MouseButton.LeftButton
                and self._is_background(event)):
            self.chromeMaximizeRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        try:
            delta = event.angleDelta()
            steps = delta.y() if delta.y() != 0 else delta.x()
            if steps:
                self._set_scroll(self._scroll - int(steps // 3))
                event.accept()
                return
        except Exception:
            pass
        super().wheelEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._menu_open:
            try:
                self.pointerExited.emit()
            except Exception:
                pass
        super().leaveEvent(event)

