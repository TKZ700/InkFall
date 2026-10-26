"""Floating view controls for the PDF viewer (M7.0).

One compact trigger in the viewer's bottom-right corner opens a small
self-painted panel with three sections: page navigation, zoom, and the
three one-shot view modes (Fit Width / Fit Height / Actual Size). Both
widgets are plain children of :class:`PdfViewer` — the same
overlay pattern as the tool shelf and the undo capsule — so they survive
fullscreen, never touch the render pipeline, and never alter the document
layout, page dimensions, or toolbar spacing.

The component is deliberately stateless about the document: it renders
what the viewer tells it (``sync_state``) and reports user intent through
signals. The viewer stays the single source of truth for the current
page, the zoom, and the active fit mode, so opening/closing the panel or
switching documents can never leave a stale count behind.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIntValidator, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import fx as _fx
from app.icons import pixmap as _pixmap
from app.theme.tokens import InkfallTokens
from app.toolshelf.surfaces import (
    hover_wash,
    ink_color,
    selected_surface,
    token_color,
)

__all__ = ["ViewControls", "ViewControlsPanel", "ViewControlsTrigger"]


def _font(tokens: InkfallTokens, px: int, weight: int = 500) -> QFont:
    """Panel font: one bundled family, explicit pixel size."""
    font = QFont()
    try:
        family = str(tokens.font_family).split(",")[0].strip()
        if family:
            font.setFamily(family)
    except Exception:
        pass
    try:
        font.setPixelSize(max(8, int(px)))
        font.setWeight(QFont.Weight(int(weight)))
    except Exception:
        pass
    return font


def _color(tokens: InkfallTokens, name: str) -> QColor:
    return token_color(tokens, name, (180, 186, 199, 255))


def _selected_fill(tokens: InkfallTokens) -> QColor:
    """Fill of a selected segment (the popup's own checked wash)."""
    return token_color(tokens, "accent_soft", (139, 147, 224, 51))


def _stop_anims(widget: QWidget, clear_effect: bool = True) -> None:
    """Stop and retire every animation owned by ``widget`` (idempotent).

    M7.0.1.3: stopped groups are also scheduled for deletion so repeated
    open/close cycles cannot accumulate stale animations on the panel.
    ``clear_effect=False`` keeps an in-flight close's opacity effect alive
    so a reopen can continue from its current opacity (no jump).
    """
    try:
        from PySide6.QtCore import QAbstractAnimation

        for anim in widget.findChildren(QAbstractAnimation):
            try:
                anim.stop()
            except Exception:
                pass
            try:
                anim.deleteLater()
            except Exception:
                pass
    except Exception:
        pass
    if not clear_effect:
        return
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass


class _FlatButton(QAbstractButton):
    """Fill-only button: hover/pressed wash + shared keyboard-focus ring.

    Painted here rather than styled so the hover language is exactly the
    one the toolbar already uses (the theme's own ink at a low alpha),
    and so the panel needs no QSS plugin surface of its own.
    """

    def __init__(self, text: str = "", icon_name: str | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._label = text
        self._icon_name = icon_name
        self._tokens: InkfallTokens | None = None
        self._hover = False
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setStyleSheet(
            "QAbstractButton { background: transparent; border: none;"
            " outline: 0; }")
        try:
            self.pressed.connect(self.update)
            self.released.connect(self.update)
        except Exception:
            pass

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def set_selected(self, selected: bool) -> None:
        """Selected state — owned by the viewer, never toggled here.

        The panel only ever renders the state it is given, so a segment
        can never disagree with the viewer (no second source of truth).
        """
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.EnabledChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            if tokens is None:
                return
            rect = self.rect().adjusted(1, 1, -1, -1)
            enabled = self.isEnabled()
            down = bool(self.isDown()) and enabled
            hover = bool(self._hover) and enabled
            radius = min(8.0, rect.height() / 2.0)
            # Selected fill first, then the hover/pressed wash on top: the
            # state language is a FILL on this surface (no ring), exactly
            # like the toolbar's selected disc and the popup's checked
            # control. Ink stays text_primary while selected.
            if enabled and self._selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_selected_fill(tokens))
                painter.drawRoundedRect(rect, radius, radius)
            if enabled and (down or hover):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover_wash(tokens, down))
                painter.drawRoundedRect(rect, radius, radius)
            if self.hasFocus():
                from app.focus import draw_focus_ring

                draw_focus_ring(painter, rect, tokens, radius=radius,
                                inset=0.0)
            if self._icon_name and self._label:
                # M7.1.4.2: icon + label rows (View menu section items).
                tint = ("text_primary"
                        if (self._selected or down or hover)
                        else "text_secondary")
                if not enabled:
                    tint = "text_tertiary"
                compact = rect.width() < 72
                icon_size = 12 if compact else max(
                    12, int(getattr(tokens, "icon_sm", 16)))
                pad = 4 if compact else 9
                gap = 4 if compact else 6
                pix = _pixmap(self._icon_name, _color(tokens, tint).name(),
                              icon_size)
                if not pix.isNull():
                    painter.drawPixmap(
                        rect.left() + pad,
                        int(round(rect.center().y() - icon_size / 2.0)),
                        pix)
                painter.setFont(
                    _font(tokens, getattr(tokens, "fs_meta", 12),
                          getattr(tokens, "w_medium", 500)))
                painter.setPen(QPen(_color(tokens, tint)))
                text_left = rect.left() + pad + icon_size + gap
                text_rect = QRect(text_left, rect.top(),
                                  max(0, rect.right() - pad - text_left + 1),
                                  rect.height())
                metrics = painter.fontMetrics()
                text = metrics.elidedText(
                    self._label, Qt.TextElideMode.ElideRight,
                    max(0, text_rect.width()))
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text)
            elif self._icon_name:
                name = ("text_primary" if (self._selected or down or hover)
                        else "text_secondary")
                if not enabled:
                    name = "text_tertiary"
                size = max(12, int(getattr(tokens, "icon_sm", 16)))
                pix = _pixmap(self._icon_name, _color(tokens, name).name(),
                              size)
                if not pix.isNull():
                    painter.drawPixmap(
                        rect.center().x() - size // 2,
                        rect.center().y() - size // 2, pix)
            elif self._label:
                name = ("text_primary" if (self._selected or down or hover)
                        else "text_secondary")
                if not enabled:
                    name = "text_tertiary"
                painter.setFont(
                    _font(tokens, getattr(tokens, "fs_meta", 12),
                          getattr(tokens, "w_medium", 500)))
                painter.setPen(QPen(_color(tokens, name)))
                painter.drawText(self.rect(),
                                 Qt.AlignmentFlag.AlignCenter, self._label)
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _AppearanceCard(_FlatButton):
    """Small vertical appearance card: icon above its name (M7.1.4.3).

    Reuses ``_FlatButton``'s state language (selected fill wash, hover
    wash, disabled ink) but lays the icon out above the label so the
    three PDF appearance modes read as one compact grouped set.
    """

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            if tokens is None:
                return
            rect = self.rect().adjusted(1, 1, -1, -1)
            enabled = self.isEnabled()
            down = bool(self.isDown()) and enabled
            hover = bool(self._hover) and enabled
            if enabled and self._selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_selected_fill(tokens))
                painter.drawRoundedRect(rect, 7, 7)
            if enabled and (down or hover):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover_wash(tokens, down))
                painter.drawRoundedRect(rect, 7, 7)
            tint = ("text_primary" if (self._selected or down or hover)
                    else "text_secondary")
            if not enabled:
                tint = "text_tertiary"
            icon_size = max(12, int(getattr(tokens, "icon_sm", 16)))
            if self._icon_name:
                pix = _pixmap(self._icon_name, _color(tokens, tint).name(),
                              icon_size)
                if not pix.isNull():
                    painter.drawPixmap(
                        int(round(rect.center().x() - icon_size / 2.0)),
                        rect.top() + 4, pix)
            painter.setFont(_font(
                tokens, getattr(tokens, "fs_caption", 11),
                getattr(tokens, "w_medium", 500)))
            painter.setPen(QPen(_color(tokens, tint)))
            text_rect = QRect(rect.left(), rect.top() + 4 + icon_size,
                              rect.width(),
                              max(0, rect.height() - icon_size - 6))
            label = painter.fontMetrics().elidedText(
                self._label, Qt.TextElideMode.ElideRight,
                max(0, text_rect.width() - 4))
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                label)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _AppearanceGroup(QWidget):
    """One bordered group containing the three appearance cards."""

    #: Inner padding between the group border and the cards.
    _INSET = 4

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        row = QHBoxLayout(self)
        row.setContentsMargins(self._INSET, self._INSET,
                               self._INSET, self._INSET)
        row.setSpacing(4)

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            if tokens is None:
                return
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(_color(tokens, "input_border"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 9, 9)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _IconLineEdit(QLineEdit):
    """Numeric field with a decorative leading glyph (M7.1.4.2).

    The glyph is painted by the field itself rather than installed as a
    ``QLineEdit`` action: actions added through ``addAction`` also appear
    in the widget's standard context menu, which would add icon-only
    entries unrelated to editing. Painting keeps the value, validator,
    selection, I-beam and keyboard behavior completely untouched; the
    left text margin reserves the glyph's space so the digits never
    overlap it.
    """

    #: Left inset of the glyph inside the field frame.
    ICON_LEFT = 5
    #: Space reserved for the glyph + gap before the centered value.
    TEXT_MARGIN_LEFT = 20

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._field_icon = None
        self.setTextMargins(self.TEXT_MARGIN_LEFT, 0, 2, 0)

    def set_field_icon(self, pixmap) -> None:
        """Install the glyph pixmap (already tinted for the theme)."""
        self._field_icon = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        pix = self._field_icon
        if pix is None:
            return
        try:
            if pix.isNull():
                return
            painter = QPainter(self)
            try:
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True)
                if not self.isEnabled():
                    painter.setOpacity(0.45)
                y = int(round((self.height() - pix.height()) / 2.0))
                painter.drawPixmap(self.ICON_LEFT, y, pix)
            finally:
                painter.end()
        except Exception:
            pass


class ViewControlsTrigger(QAbstractButton):
    """Collapsed trigger: one dark disc with the view/document glyph."""

    #: Size of the hit target and inset of the visible disc inside it.
    SIZE = 44
    DISC_INSET = 2

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._hover = False
        # Capsule segment mode: when the page navigator shares this
        # control row, the shared capsule body paints the background and
        # this trigger contributes only its wash + glyph (see
        # app/page_navigator.py). Behavior and signals are unchanged.
        self._capsule_segment = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("View controls")
        self.setStyleSheet(
            "QAbstractButton { background: transparent; border: none;"
            " outline: 0; }")
        try:
            self.pressed.connect(self.update)
            self.released.connect(self.update)
        except Exception:
            pass

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def set_capsule_segment(self, on: bool) -> None:
        """Paint as the LEFT segment of the shared View/Pages capsule.

        The capsule body (owned by the page navigator) paints the shared
        rounded background; this button suppresses its own disc + rim so
        the two controls read as one container. Menu behavior, signals,
        and hit target are untouched.
        """
        self._capsule_segment = bool(on)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            if self._capsule_segment:
                # Left segment of the shared [View | Pages] capsule: the
                # capsule body paints the background; the wash is painted
                # as the FULL capsule silhouette in this button's local
                # coordinates — the widget's own bounds clip the right
                # half, so the outer corners round and the internal edge
                # is straight (no incorrectly rounded internal edge).
                down = bool(self.isDown())
                if down or self._hover:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(hover_wash(tokens, down))
                    painter.drawRoundedRect(
                        QRectF(0.0, 0.0, float(self.SIZE * 2),
                               float(self.SIZE)),
                        float(self.SIZE / 2.0), float(self.SIZE / 2.0))
                if self.hasFocus():
                    # Ring on the same full-capsule silhouette; the
                    # widget's own bounds clip the right half.
                    from app.focus import draw_focus_ring

                    draw_focus_ring(
                        painter,
                        QRectF(0.0, 0.0, float(self.SIZE * 2),
                               float(self.SIZE)),
                        tokens, radius=float(self.SIZE / 2.0))
                size = 18
                pix = _pixmap("eye", ink_color(tokens).name(), size)
                if not pix.isNull():
                    painter.drawPixmap(
                        self.SIZE // 2 - size // 2,
                        self.SIZE // 2 - size // 2, pix)
                return
            side = float(self.SIZE - 2 * self.DISC_INSET)
            disc = QRectF(float(self.DISC_INSET), float(self.DISC_INSET),
                          side, side)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(selected_surface(tokens))
            painter.drawEllipse(disc)
            # Faint rim so the near-black disc also reads on dark pages;
            # no accent outline (the fill is the state language). Same
            # hairline token as the capsule body.
            rim = token_color(tokens, "border", (38, 44, 56, 255))
            rim.setAlpha(255)
            painter.setPen(QPen(rim, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(disc.adjusted(0.5, 0.5, -0.5, -0.5))
            down = bool(self.isDown())
            if down or self._hover:
                painter.save()
                painter.setClipPath(_ellipse_path(disc))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover_wash(tokens, down))
                painter.drawEllipse(disc)
                painter.restore()
            if self.hasFocus():
                from app.focus import draw_focus_ring

                draw_focus_ring(painter, disc, tokens, radius=None,
                                inset=2.0)
            size = 18
            # M7.0.1: a document-viewing glyph (the one the app already
            # ships). The previous two-slider mark read as "adjustments",
            # which is not what this control is.
            pix = _pixmap("eye", ink_color(tokens).name(), size)
            if not pix.isNull():
                painter.drawPixmap(
                    int(disc.center().x()) - size // 2,
                    int(disc.center().y()) - size // 2, pix)
        finally:
            try:
                painter.end()
            except Exception:
                pass


def _ellipse_path(rect: QRectF):
    """A clipping path for the trigger's circular hover wash."""
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    path.addEllipse(rect)
    return path


class ViewControlsPanel(QWidget):
    """Expanded panel: page / zoom / view-mode sections on one surface."""

    prevRequested = Signal()
    nextRequested = Signal()
    pageRequested = Signal(int)
    zoomOutRequested = Signal()
    zoomInRequested = Signal()
    zoomPercentRequested = Signal(int)
    modeRequested = Signal(str)
    appearanceRequested = Signal(str)
    dismissRequested = Signal()

    #: Soft-shadow padding around the painted body (part of geometry).
    _SHADOW = 12
    #: Content width; the panel is one fixed, compact column.
    _CONTENT_W = 232

    #: M7.1.4.3: exactly three fit modes. Fit Page is intentionally no
    #: longer offered here; the viewer keeps its internal "page" fitting
    #: implementation untouched for any future non-selector use.
    _MODES = (
        ("width", "Fit Width", "fit-width"),
        ("height", "Fit Height", "fit-height"),
        ("actual", "Actual Size", "actual-size"),
    )

    #: PDF appearance modes (M7.1.4.3): genuinely distinct render modes,
    #: not labels for one toggle. Light = original, Dark = night reading
    #: (HSL lightness inverted, hue/saturation preserved), Inverted = the
    #: original full-color inversion.
    _APPEARANCES = (
        ("light", "Light", "sun"),
        ("dark", "Dark", "moon"),
        ("inverted", "Inverted", "contrast"),
    )

    def __init__(self, tokens: InkfallTokens, min_zoom: float,
                 max_zoom: float, parent=None) -> None:
        super().__init__(parent)
        # Child overlay of the viewer: same translucent, self-painted
        # body + manual shadow as the tool popup (no window, no effect).
        self.setObjectName("inkfall-view-controls")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Tool cursors: the panel owns its pointer (an inherited cursor would
        # leak the viewer's tool cursor into the panel's own background and
        # labels).
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._tokens = tokens
        self._min_pct = int(round(float(min_zoom) * 100.0))
        self._max_pct = int(round(float(max_zoom) * 100.0))
        self._page: int | None = None
        self._count = 0
        self._percent: int | None = None
        self._valid_count = -1
        self._dividers: list[QWidget] = []
        self._appearance: dict[str, _AppearanceCard] = {}
        self._appearance_group: _AppearanceGroup | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(self._SHADOW, self._SHADOW,
                                 self._SHADOW, self._SHADOW)
        outer.setSpacing(0)
        self._body = QWidget(self)
        self._body.setStyleSheet("background: transparent;")
        self._body.setFixedWidth(self._CONTENT_W)
        outer.addWidget(self._body)
        inner = QVBoxLayout(self._body)
        inner.setContentsMargins(12, 12, 12, 12)
        inner.setSpacing(10)

        # -- page navigation ------------------------------------------
        page_row = QHBoxLayout()
        page_row.setSpacing(6)
        self._prev = _FlatButton(icon_name="chevron-left", parent=self._body)
        self._prev.setFixedSize(28, 28)
        self._prev.setAccessibleName("Previous page")
        self._page_edit = _IconLineEdit(self._body)
        self._page_edit.setFixedSize(58, 26)
        self._page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_edit.setMaxLength(7)
        self._page_edit.setValidator(QIntValidator(1, 1, self._page_edit))
        self._page_edit.setAccessibleName("Current page")
        self._total = QLabel("/ —", self._body)
        self._total.setAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
        self._total.setFixedWidth(52)
        self._next = _FlatButton(icon_name="chevron-right", parent=self._body)
        self._next.setFixedSize(28, 28)
        self._next.setAccessibleName("Next page")
        page_row.addWidget(self._prev)
        page_row.addStretch(1)
        page_row.addWidget(self._page_edit)
        page_row.addSpacing(2)
        page_row.addWidget(self._total)
        page_row.addStretch(1)
        page_row.addWidget(self._next)
        inner.addLayout(page_row)
        inner.addWidget(self._divider())

        # -- zoom -----------------------------------------------------
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(6)
        self._zoom_out = _FlatButton(icon_name="zoom-out", parent=self._body)
        self._zoom_out.setFixedSize(28, 28)
        self._zoom_out.setAccessibleName("Zoom out")
        self._zoom_edit = _IconLineEdit(self._body)
        self._zoom_edit.setFixedSize(56, 26)
        self._zoom_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_edit.setMaxLength(5)
        self._zoom_edit.setValidator(
            QIntValidator(self._min_pct, self._max_pct, self._zoom_edit))
        self._zoom_edit.setAccessibleName("Zoom percentage")
        self._pct = QLabel("%", self._body)
        self._pct.setFixedWidth(14)
        self._zoom_in = _FlatButton(icon_name="zoom-in", parent=self._body)
        self._zoom_in.setFixedSize(28, 28)
        self._zoom_in.setAccessibleName("Zoom in")
        zoom_row.addWidget(self._zoom_out)
        zoom_row.addStretch(1)
        zoom_row.addWidget(self._zoom_edit)
        zoom_row.addSpacing(2)
        zoom_row.addWidget(self._pct)
        zoom_row.addStretch(1)
        zoom_row.addWidget(self._zoom_in)
        inner.addLayout(zoom_row)
        inner.addWidget(self._divider())

        # -- view modes: exactly three, stacked full-width rows -------
        # Fit Page is intentionally absent (M7.1.4.3); three rows keep the
        # full labels readable inside the unchanged 232 px panel width.
        modes = QVBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(6)
        self._mode_buttons: dict[str, _FlatButton] = {}
        for key, label, icon_name in self._MODES:
            button = _FlatButton(text=label, icon_name=icon_name,
                                 parent=self._body)
            button.setFixedHeight(30)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
            button.setAccessibleName(label)
            button.clicked.connect(
                lambda _checked=False, mode=key: self.modeRequested.emit(mode))
            modes.addWidget(button)
            self._mode_buttons[key] = button
        inner.addLayout(modes)
        inner.addWidget(self._divider())

        # -- pdf appearance: three grouped cards -----------------------
        # One bordered group, three equally sized cards (icon above
        # label), single exclusive selection. The viewer owns the actual
        # render mode; this row only renders it.
        appearance_row = QHBoxLayout()
        appearance_row.setSpacing(6)
        self._appearance_label = QLabel("PDF Appearance", self._body)
        self._appearance_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        appearance_row.addWidget(self._appearance_label)
        appearance_row.addStretch(1)
        inner.addLayout(appearance_row)
        group = _AppearanceGroup(tokens, self._body)
        group.setFixedHeight(48)
        self._appearance_group = group
        for key, label, icon_name in self._APPEARANCES:
            card = _AppearanceCard(text=label, icon_name=icon_name,
                                   parent=group)
            card.setFixedHeight(40)
            card.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Fixed)
            card.setAccessibleName(f"PDF appearance: {label}")
            card.clicked.connect(
                lambda _checked=False, mode=key:
                self.appearanceRequested.emit(mode))
            group.layout().addWidget(card)
            self._appearance[key] = card
        inner.addWidget(group)

        self._prev.clicked.connect(self.prevRequested.emit)
        self._next.clicked.connect(self.nextRequested.emit)
        self._zoom_out.clicked.connect(self.zoomOutRequested.emit)
        self._zoom_in.clicked.connect(self.zoomInRequested.emit)
        self._page_edit.editingFinished.connect(self._commit_page)
        self._zoom_edit.editingFinished.connect(self._commit_zoom)

        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.update_tokens(tokens)
        # Light is the default appearance, so the group renders from that
        # state until the viewer says otherwise.
        self.sync_state(None, 0, None, appearance="light")

    # -- accessors -----------------------------------------------------

    @property
    def body_inset(self) -> int:
        """Shadow padding between the widget box and the painted body."""
        return int(self._SHADOW)

    # -- construction helpers -----------------------------------------

    def _divider(self) -> QWidget:
        line = QWidget(self._body)
        line.setFixedHeight(1)
        line.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self._dividers.append(line)
        return line

    # -- theming ------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        for button in self.findChildren(_FlatButton):
            try:
                button.set_tokens(tokens)
            except Exception:
                pass
        muted = _color(tokens, "text_muted").name()
        primary = _color(tokens, "text_primary").name()
        meta = int(getattr(tokens, "fs_meta", 12))
        for label in (self._total, self._pct, self._appearance_label):
            label.setFont(_font(tokens, meta, 500))
            label.setStyleSheet(
                f"color: {muted}; background: transparent;"
                f" font-size: {meta}px;")
        bg = _color(tokens, "input_background").name()
        edge = _color(tokens, "input_border").name()
        focus = _color(tokens, "input_focus").name()
        select = _color(tokens, "accent_soft").name(
            QColor.NameFormat.HexArgb)
        edit_qss = (
            f"QLineEdit {{ background: {bg}; color: {primary};"
            f" border: 1px solid {edge}; border-radius: 6px;"
            f" padding: 0 4px; font-size: {meta}px;"
            f" selection-background-color: {select}; }}"
            f"QLineEdit:focus {{ border-color: {focus}; }}"
            f"QLineEdit:disabled {{ color: {muted}; }}")
        for edit in (self._page_edit, self._zoom_edit):
            edit.setFont(_font(tokens, meta, 500))
            edit.setStyleSheet(edit_qss)
        divider_color = _color(tokens, "divider").name(
            QColor.NameFormat.HexArgb)
        for line in self._dividers:
            line.setStyleSheet(f"background: {divider_color};")
        try:
            if self._appearance_group is not None:
                self._appearance_group.set_tokens(tokens)
        except Exception:
            pass
        self._sync_field_icons(tokens)
        self.update()

    def _sync_field_icons(self, tokens: InkfallTokens) -> None:
        """Refresh the numeric fields' leading glyphs for the theme.

        The glyphs are painted by the ``_IconLineEdit`` fields themselves,
        so values, validators, alignment, field sizes, context menus and
        the surrounding row layout are untouched.
        """
        tint = _color(tokens, "text_secondary").name()
        for edit, name in ((self._page_edit, "doc"),
                           (self._zoom_edit, "search")):
            try:
                pix = _pixmap(name, tint, 14)
                if not pix.isNull():
                    edit.set_field_icon(pix)
            except Exception:
                pass

    # -- state --------------------------------------------------------

    def sync_state(self, page: int | None, count: int,
                   percent: int | None,
                   appearance: str | None = None) -> None:
        """Render the viewer's actual state (never a second copy)."""
        # Legacy callers passed the old boolean toggle (True meant the
        # full inversion, which is now the "inverted" mode).
        if isinstance(appearance, bool):
            appearance = "inverted" if appearance else "light"
        try:
            total = max(0, int(count))
        except Exception:
            total = 0
        index = int(page) if (page is not None and total > 0) else None
        if index is not None:
            index = max(0, min(total - 1, index))
        try:
            pct = int(percent) if percent is not None else None
        except Exception:
            pct = None
        self._page = index
        self._count = total
        self._percent = pct
        available = total > 0

        if total != self._valid_count:
            self._valid_count = total
            self._page_edit.setValidator(
                QIntValidator(1, max(1, total), self._page_edit))

        self._page_edit.setEnabled(available)
        self._zoom_edit.setEnabled(available and pct is not None)
        self._prev.setEnabled(bool(available and index is not None
                                   and index > 0))
        self._next.setEnabled(bool(available and index is not None
                                   and index < total - 1))
        for button in (self._zoom_out, self._zoom_in,
                       *self._mode_buttons.values(),
                       *self._appearance.values()):
            button.setEnabled(available)
        if appearance is not None:
            mode = str(appearance).strip().lower()
            for key, card in self._appearance.items():
                try:
                    card.set_selected(key == mode)
                except Exception:
                    pass

        if not self._page_edit.hasFocus():
            self._page_edit.setText(
                str(index + 1) if index is not None else "")
        self._page_edit.setPlaceholderText("—")
        self._total.setText(f"/ {total}" if total else "/ —")
        if not self._zoom_edit.hasFocus():
            self._zoom_edit.setText(
                str(pct) if (available and pct is not None) else "")
        self._zoom_edit.setPlaceholderText("—")

    def _commit_page(self) -> None:
        restore = str(self._page + 1) if self._page is not None else ""
        if self._count <= 0:
            self._page_edit.setText(restore)
            return
        try:
            value = int(self._page_edit.text().strip())
        except Exception:
            self._page_edit.setText(restore)
            return
        value = max(1, min(self._count, value))
        if value - 1 != self._page:
            # Optimistic echo: the field (and the restore below) show the
            # requested page immediately, and a follow-up editingFinished
            # (Enter re-emits it on focus-out) is a no-op.
            self._page = value - 1
            self.pageRequested.emit(value - 1)
            restore = str(value)
        self._page_edit.setText(restore)

    def _commit_zoom(self) -> None:
        restore = str(self._percent) if self._percent is not None else ""
        try:
            value = int(self._zoom_edit.text().strip())
        except Exception:
            self._zoom_edit.setText(restore)
            return
        value = max(self._min_pct, min(self._max_pct, value))
        if value != self._percent:
            self._percent = value
            self.zoomPercentRequested.emit(value)
            restore = str(value)
        self._zoom_edit.setText(restore)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.dismissRequested.emit()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Own presses on the panel surface: never a document click.

        Interactive children (buttons, fields) handle their own input;
        a press on the padding would otherwise ignore-and-propagate to
        the viewer canvas beneath and could start a stroke there.
        """
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        event.accept()

    # -- painting -----------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            body = QRectF(self.rect()).adjusted(
                self._SHADOW, self._SHADOW, -self._SHADOW, -self._SHADOW)
            try:
                radius = float(getattr(tokens, "modal_radius", 14))
            except Exception:
                radius = 14.0
            # Manual soft shadow: the same layered rounded-rect recipe as
            # the tool popup (no QGraphicsDropShadowEffect, which forces a
            # square backing on a translucent overlay).
            painter.setPen(Qt.PenStyle.NoPen)
            for i, alpha in ((6, 40), (4, 48), (2, 56)):
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(
                    body.adjusted(-i, -i + 2, i, i + 2),
                    radius + i, radius + i)
            painter.setBrush(token_color(tokens, "surface_raised",
                                         (24, 28, 35, 255)))
            painter.drawRoundedRect(body, radius, radius)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(token_color(tokens, "border",
                                            (38, 44, 56, 255)), 1.0))
            painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5),
                                    radius, radius)
        finally:
            try:
                painter.end()
            except Exception:
                pass


class ViewControls(QObject):
    """Owns the trigger and the panel; positions, opens, and dismisses them.

    Dismissal follows the application's popup conventions: a press
    anywhere outside the panel/trigger closes it, the click itself is
    never consumed (``eventFilter`` always returns ``False`` for pointer
    input), and Escape closes the panel. An application-level filter is
    used — like the tool popup's hover passthrough and the modal's focus
    trap — because a host-level filter cannot see presses on sibling
    chrome such as the viewer's scrollbars.
    """

    prevRequested = Signal()
    nextRequested = Signal()
    pageRequested = Signal(int)
    zoomInRequested = Signal()
    zoomOutRequested = Signal()
    zoomPercentRequested = Signal(int)
    modeRequested = Signal(str)
    appearanceRequested = Signal(str)

    #: Gap between the trigger and the panel.
    _GAP = 8
    #: Minimum distance from the viewer's edges when clamping.
    _EDGE = 8

    def __init__(self, host: QWidget, tokens: InkfallTokens,
                 min_zoom: float, max_zoom: float,
                 insets=None, obstacles=None, row=None, parent=None) -> None:
        super().__init__(parent or host)
        self._host = host
        self._insets = insets
        self._obstacles = obstacles
        #: M7.0.1.2: the viewer's shared bottom-row origin for the trigger
        #: (the same row the undo capsule sits on). Optional so the
        #: component still anchors itself when used on its own.
        self._row = row
        self._filter_on = False
        #: M7.0.1.3: true while an animated close is in flight (the panel is
        #: still visible, on its way out). Makes repeated closes idempotent
        #: and lets a reopen interrupt the close cleanly.
        self._closing = False
        #: Strong ref to a running "cancel the close" reversal animation
        #: (held for lifetime only; never dereferenced).
        self._resume_anim = None
        self._available = False
        self._trigger = ViewControlsTrigger(tokens, host)
        self._panel = ViewControlsPanel(tokens, min_zoom, max_zoom, host)
        self._panel.hide()
        self._trigger.clicked.connect(self.toggle)
        self._panel.prevRequested.connect(self.prevRequested.emit)
        self._panel.nextRequested.connect(self.nextRequested.emit)
        self._panel.pageRequested.connect(self.pageRequested.emit)
        self._panel.zoomInRequested.connect(self.zoomInRequested.emit)
        self._panel.zoomOutRequested.connect(self.zoomOutRequested.emit)
        self._panel.zoomPercentRequested.connect(
            self.zoomPercentRequested.emit)
        self._panel.modeRequested.connect(self.modeRequested.emit)
        self._panel.appearanceRequested.connect(
            self.appearanceRequested.emit)
        self._panel.dismissRequested.connect(
            lambda: self.close_panel())
        self._trigger.setVisible(False)
        self.reposition()

    # -- accessors -----------------------------------------------------

    @property
    def trigger(self) -> ViewControlsTrigger:
        return self._trigger

    @property
    def panel(self) -> ViewControlsPanel:
        return self._panel

    @property
    def is_open(self) -> bool:
        return self._panel.isVisible()

    # -- lifecycle -----------------------------------------------------

    def set_available(self, available: bool) -> None:
        """Show the trigger only for a live document session."""
        available = bool(available)
        self._available = available
        if not available:
            self.close_panel(animate=False)
        try:
            self._trigger.setVisible(available)
        except Exception:
            pass
        if available:
            self.reposition()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._trigger.set_tokens(tokens)
        self._panel.update_tokens(tokens)

    def sync_state(self, page: int | None, count: int,
                   percent: int | None,
                   appearance: str | None = None) -> None:
        self._panel.sync_state(page, count, percent, appearance)

    # -- positioning ---------------------------------------------------

    def _inset_values(self) -> tuple[int, int]:
        try:
            right, bottom = self._insets()
            return int(right), int(bottom)
        except Exception:
            return self._EDGE, self._EDGE

    def _trigger_origin(self) -> QPoint:
        right, bottom = self._inset_values()
        width = max(0, self._host.width())
        height = max(0, self._host.height())
        x = width - self._trigger.width() - max(self._EDGE, right)
        y = height - self._trigger.height() - max(self._EDGE, bottom)
        if self._row is not None:
            # M7.0.1.2: the viewer owns the shared bottom row (trigger +
            # undo capsule), including the horizontal clearance from a
            # docked shelf. Its answer wins outright so the two controls
            # can never drift apart; the local maths above stays as the
            # standalone fallback.
            try:
                origin = self._row()
                if origin is not None:
                    return QPoint(int(origin.x()), int(origin.y()))
            except Exception:
                pass
        return QPoint(max(self._EDGE, x), max(self._EDGE, y))

    def _panel_origin(self) -> QPoint:
        """Top-left of the panel widget, aligning its *painted body*.

        The widget carries the shadow pad on every side, so the body (not
        the widget box) is what lines up with the visible trigger disc and
        with the viewer edges.
        """
        panel = self._panel
        inset = int(getattr(panel, "body_inset", 0))
        body_w = max(1, panel.width() - 2 * inset)
        body_h = max(1, panel.height() - 2 * inset)
        trigger = self._trigger
        disc_inset = int(getattr(trigger, "DISC_INSET", 0))
        host_w = self._host.width()
        host_h = self._host.height()
        anchor_right = trigger.x() + trigger.width() - disc_inset
        anchor_top = trigger.y() + disc_inset
        left = max(self._EDGE,
                   min(max(self._EDGE, host_w - body_w - self._EDGE),
                       anchor_right - body_w))
        top = anchor_top - self._GAP - body_h
        if top < self._EDGE:
            # Not enough room above: open below the trigger instead.
            top = anchor_top + (trigger.height() - 2 * disc_inset) + self._GAP
        top = max(self._EDGE,
                  min(max(self._EDGE, host_h - body_h - self._EDGE), top))
        body = self._clear_of_chrome(QRect(left, top, body_w, body_h))
        return QPoint(body.x() - inset, body.y() - inset)

    def _obstacle_rects(self) -> list:
        """Live chrome rectangles the expanded panel must never cover."""
        if self._obstacles is None:
            return []
        try:
            rects = []
            for rect in self._obstacles():
                try:
                    rects.append(QRect(rect))
                except Exception:
                    pass
            return rects
        except Exception:
            return []

    def _clear_of_chrome(self, body: QRect) -> QRect:
        """Slide the panel clear of the permanent chrome.

        The shelf and the undo capsule are anchored chrome; the panel is a
        transient overlay, so it is the one that moves. Two passes (clearing
        one obstacle can enter another's band): prefer straight *up* — the
        panel's own growth direction — then to the *left* of the obstacle's
        column. The host bounds are never left, and an obstacle with no free
        side is left as-is (the caller's clamped origin stands).
        """
        try:
            obstacles = self._obstacle_rects()
        except Exception:
            return body
        if not obstacles:
            return body
        for _ in range(2):
            moved = False
            for obstacle in obstacles:
                if not obstacle.intersects(body):
                    continue
                up = obstacle.top() - self._GAP - body.height()
                left = obstacle.left() - self._GAP - body.width()
                if up >= self._EDGE:
                    body.moveTop(up)
                    moved = True
                elif left >= self._EDGE:
                    body.moveLeft(left)
                    moved = True
            if not moved:
                break
        return body

    def reposition(self) -> None:
        """Re-anchor to the bottom-right corner (viewer resizes/theme)."""
        origin = self._trigger_origin()
        self._trigger.move(origin)
        try:
            self._trigger.raise_()
        except Exception:
            pass
        if not self._panel.isVisible():
            return
        if self._closing:
            # M7.0.1.3: never drag a panel whose close animation is running.
            return
        try:
            self._panel.adjustSize()
        except Exception:
            pass
        self._panel.move(self._panel_origin())
        try:
            self._panel.raise_()
        except Exception:
            pass

    # -- open / close --------------------------------------------------

    def toggle(self) -> None:
        if self._closing:
            # Closing: the trigger cancels the close and brings the panel
            # back (open_panel resumes from the current opacity/position).
            self.open_panel()
        elif self._panel.isVisible():
            self.close_panel()
        else:
            self.open_panel()

    def open_panel(self) -> None:
        if not self._available:
            return
        resuming = bool(self._closing) and self._panel.isVisible()
        self._closing = False
        # Capture the interrupted close's visual state before teardown so
        # the reopen continues from exactly where the close left off.
        resume_opacity = None
        resume_pos = None
        if resuming:
            try:
                effect = self._panel.graphicsEffect()
                if effect is not None:
                    resume_opacity = float(effect.opacity())
            except Exception:
                resume_opacity = None
            try:
                resume_pos = QPoint(self._panel.pos())
            except Exception:
                resume_pos = None
        _stop_anims(self._panel, clear_effect=not resuming)
        try:
            self._panel.adjustSize()
        except Exception:
            pass
        origin = self._panel_origin()
        self._panel.move(origin)
        self._panel.show()
        try:
            self._panel.raise_()
        except Exception:
            pass
        self._install_filter(True)
        if resuming:
            if self._resume_from_close(resume_opacity, resume_pos, origin):
                return
            # Nothing left to reverse (already fully visible): settle in
            # place instead of flashing through a fresh entrance fade.
            try:
                self._panel.setGraphicsEffect(None)
            except Exception:
                pass
            return
        try:
            _fx.slide_fade_in(self._panel, 0, 160, self._panel, dy_px=6)
        except Exception:
            pass

    def _resume_from_close(self, opacity, from_pos, to_pos) -> bool:
        """Reverse an interrupted close in place.

        Returns True when a resume animation was started, in which case the
        caller skips the normal entrance animation. The panel keeps the
        close's own opacity effect, so its current opacity (and any slide
        offset) is animated back to full/anchored — no opacity jump and no
        geometry snap.
        """
        try:
            from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup,
                                        QPropertyAnimation)

            effect = self._panel.graphicsEffect()
            group = QParallelAnimationGroup(self._panel)
            added = 0
            if effect is not None and opacity is not None and opacity < 1.0:
                fade = QPropertyAnimation(effect, b"opacity", group)
                fade.setDuration(110)
                fade.setEasingCurve(QEasingCurve.Type.OutCubic)
                fade.setStartValue(float(opacity))
                fade.setEndValue(1.0)
                group.addAnimation(fade)
                added += 1
            if from_pos is not None and from_pos != to_pos:
                self._panel.move(from_pos)
                slide = QPropertyAnimation(self._panel, b"pos", group)
                slide.setDuration(110)
                slide.setEasingCurve(QEasingCurve.Type.OutCubic)
                slide.setStartValue(from_pos)
                slide.setEndValue(to_pos)
                group.addAnimation(slide)
                added += 1
            if not added:
                return False
            group.finished.connect(lambda: self._panel.setGraphicsEffect(None))
            self._resume_anim = group
            group.start()
            return True
        except Exception:
            return False

    def close_panel(self, animate: bool = True) -> None:
        self._install_filter(False)
        if not self._panel.isVisible():
            self._closing = False
            return
        if self._closing and animate:
            # A close is already running to the same (hidden) end state;
            # restarting it would snap the fading panel back to opaque.
            return
        _stop_anims(self._panel)
        if animate:
            group = None
            try:
                # hide=True binds the hide to the animation's own end, so no
                # fixed timer can leave an invisible-but-visible remnant and
                # reduced motion resolves straight to hidden.
                group = _fx.slide_fade_out(self._panel, 0, 140, self._panel,
                                           dy_px=4, hide=True)
            except Exception:
                group = None
            if group is not None:
                self._closing = True
                try:
                    group.finished.connect(self._on_close_finished)
                except Exception:
                    pass
                return
        # Immediate path: non-animated close, reduced motion, or failure.
        self._closing = False
        try:
            self._panel.hide()
        except Exception:
            pass

    def _on_close_finished(self) -> None:
        """The animated close reached its (hidden) end state."""
        self._closing = False

    # -- dismissal -----------------------------------------------------

    def _install_filter(self, install: bool) -> None:
        app = QApplication.instance()
        if app is None:
            return
        try:
            if install and not self._filter_on:
                app.installEventFilter(self)
                self._filter_on = True
            elif not install and self._filter_on:
                app.removeEventFilter(self)
                self._filter_on = False
        except Exception:
            self._filter_on = False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Dismiss on an outside press / Escape; never eat pointer input."""
        try:
            if not self._panel.isVisible():
                return False
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress:
                widget = watched if isinstance(watched, QWidget) else None
                inside = bool(widget is not None and (
                    widget is self._panel or self._panel.isAncestorOf(widget)
                    or widget is self._trigger
                    or self._trigger.isAncestorOf(widget)))
                if not inside:
                    try:
                        point = event.globalPosition().toPoint()
                        inside = self._panel.rect().contains(
                            self._panel.mapFromGlobal(point))
                    except Exception:
                        inside = False
                if not inside:
                    self.close_panel()
            elif kind == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape:
                    self.close_panel()
                    return True
        except Exception:
            pass
        return False
