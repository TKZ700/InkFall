"""Anchored tool popup (M5-A1 refinement). The ONLY tool-properties UI.

A lightweight floating panel positioned near the launcher slot,
clamped to the viewer: hosts either a capability-filtered live
``PropertiesForm`` inspector or the ``AddToolGrid``. Opens with fx
slide/fade (reduced-motion aware), closes on Escape/outside-click.

Live-inspector semantics: every control change immediately produces an
updated ToolInstance, applies it to the slot button + preview, and
notifies the viewer (which persists debounced). There is no Save/Cancel
and no hidden draft — closing keeps already-applied changes.
"""

from __future__ import annotations

import dataclasses

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app import fx as _fx
from app.icons import pixmap as _icon_pixmap
from app.icons import ink_center_offset as _ink_offset
from app.icons import tool_pixmap as _tool_pixmap
from app.modal import ModalButton
from app.theme.tokens import InkfallTokens
from app.toolshelf.store import ADDABLE_KINDS, DEFAULT_COLOR_PRESETS
from tools.models import (
    ERASER_MODE_LABELS,
    ERASER_MODES,
    LINE_ENDINGS,
    LINE_STYLES,
    ToolInstance,
    ToolType,
    editable_properties,
    is_core_tool,
    normalize_eraser_mode,
    size_family,
    size_presets_for,
)

__all__ = [
    "ToolPopup",
    "PropertiesForm",
    "AddToolGrid",
    "ADD_GRID_META",
    "stop_widget_anims",
]

# Widget-side fallback only: model-owned store defaults, never a
# hardcoded list. The live source is store.get_color_presets().
_PRESETS = tuple(DEFAULT_COLOR_PRESETS)

_OPACITY_PRESETS = (0.2, 0.4, 0.7, 1.0)

_ADD_ICONS = {
    ToolType.PEN: "pen",
    ToolType.HIGHLIGHTER: "highlighter",
    ToolType.ERASER: "eraser",
    ToolType.LASSO: "lasso",
    ToolType.LINE: "line",
    ToolType.RECTANGLE: "rectangle",
    ToolType.ELLIPSE: "ellipse",
    ToolType.ARROW: "arrow",
    ToolType.POLYGON: "polygon",
    ToolType.NOTE: "note",
}

ADD_GRID_META: tuple[tuple[ToolType, str], ...] = (
    (ToolType.PEN, "Pen"),
    (ToolType.HIGHLIGHTER, "Highlighter"),
    (ToolType.ERASER, "Eraser"),
    (ToolType.LASSO, "Lasso"),
    (ToolType.LINE, "Line"),
    (ToolType.RECTANGLE, "Rectangle"),
    (ToolType.ELLIPSE, "Ellipse"),
    (ToolType.ARROW, "Arrow"),
    (ToolType.POLYGON, "Polygon"),
    (ToolType.NOTE, "Note"),
)


def _refit_popup(widget) -> None:
    """Resize the owning ToolPopup after a section expands/collapses."""
    try:
        parent = widget.parentWidget()
        while parent is not None:
            try:
                if parent.__class__.__name__ == "ToolPopup":
                    parent.refit_to_content()
                    return
            except Exception:
                pass
            parent = parent.parentWidget()
    except Exception:
        pass


def _style_numeric_spin(spin, tokens: InkfallTokens) -> None:
    """Shared Custom-editor spin styling (size + opacity identical)."""
    try:
        from app.theme.typography import role_font

        spin.setFont(role_font(tokens, "body"))
    except Exception:
        pass
    try:
        spin.setStyleSheet(
            f"color: {tokens.text_primary};"
            f" background-color: {tokens.input_background};"
            f" border: 1px solid {tokens.input_border}; border-radius: 6px;"
            f" padding: 4px 6px;")
    except Exception:
        pass


class _SpinKeyFilter(QObject):
    """Predictable spin keyboard behavior: select-all on focus-in so
    typing replaces the value, Enter commits natively, Escape restores
    the pre-edit value (``begin`` snapshots it on focus-in, ``cancel``
    re-applies it) instead of leaving a half-typed commit behind."""

    def __init__(self, spin, begin, cancel, parent=None) -> None:
        super().__init__(parent)
        self._spin = spin
        self._begin = begin
        self._cancel = cancel

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        try:
            etype = event.type()
            if etype == QEvent.Type.FocusIn:
                try:
                    if self._begin is not None:
                        self._begin()
                except Exception:
                    pass
                try:
                    edit = self._spin.lineEdit()
                except Exception:
                    edit = None
                if edit is not None:
                    try:
                        from PySide6.QtCore import QTimer

                        QTimer.singleShot(0, edit.selectAll)
                    except Exception:
                        pass
            elif etype == QEvent.Type.KeyPress:
                try:
                    key = int(event.key())
                except Exception:
                    key = 0
                if key == int(Qt.Key.Key_Escape):
                    try:
                        if self._cancel is not None:
                            self._cancel()
                    except Exception:
                        pass
                    try:
                        self._spin.clearFocus()
                    except Exception:
                        pass
                    return True
        except Exception:
            pass
        return False


def _install_spin_keyboard(spin, begin, cancel) -> None:
    """Attach focus-select + Escape-revert to a Custom-editor spin.

    The filter is parented to ``spin`` (Qt-owned for the spin's life).
    """
    try:
        keeper = _SpinKeyFilter(spin, begin, cancel, spin)
        spin.installEventFilter(keeper)
        try:
            edit = spin.lineEdit()
            if edit is not None:
                edit.installEventFilter(keeper)
        except Exception:
            pass
    except Exception:
        pass


class _Preview(QWidget):
    """Live visual preview of the draft tool (primary, not text)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._slot: ToolInstance | None = None
        self.setFixedHeight(36)

    def set_slot(self, slot: ToolInstance | None) -> None:
        self._slot = slot
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._slot is None:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect().adjusted(10, 8, -10, -8)
            try:
                base = QColor(self._slot.color)
                if not base.isValid():
                    base = QColor("#000000")
            except Exception:
                base = QColor("#000000")
            if self._slot.kind == ToolType.PEN:
                try:
                    width = float(self._slot.width_mm)
                except (TypeError, ValueError):
                    width = 0.5
                try:
                    tint = QColor(base)
                    if tint.lightness() < 40:
                        tint = tint.lighter(165)
                except Exception:
                    tint = base
                # Contrast-safe against the popup surface (same treatment
                # as the shelf glyphs — never blends in, either theme).
                surface_bg = "#181C23"
                try:
                    node = self.parentWidget()
                    for _ in range(6):
                        if node is None:
                            break
                        toks = getattr(node, "_tokens", None)
                        if toks is not None and hasattr(
                                toks, "surface_raised"):
                            surface_bg = str(toks.surface_raised)
                            break
                        node = node.parentWidget()
                except Exception:
                    pass
                try:
                    from app.icons import ensure_contrast_fg as _ecfg

                    tint = QColor(_ecfg(tint.name(), surface_bg))
                except Exception:
                    pass
                try:
                    size = max(24, min(34, int(26 + width * 3.0)))
                    glyph = _tool_pixmap("pen-nib", tint.name(),
                                         surface_bg, size)
                except Exception:
                    glyph = QPixmap()
                if not glyph.isNull():
                    # Float center (matches shelf cells): QRect.center()
                    # truncates on even sizes, parking ink 1px off.
                    cx = rect.left() + rect.width() / 2.0
                    cy = rect.top() + rect.height() / 2.0
                    try:
                        dx, dy = _ink_offset(glyph)
                    except Exception:
                        dx, dy = (0.0, 0.0)
                    painter.drawPixmap(int(round(cx - glyph.width() / 2 + dx)),
                                       int(round(cy - glyph.height() / 2 + dy)),
                                       glyph)
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(base)
                    painter.drawEllipse(rect.center().x() - 8,
                                        rect.center().y() - 8, 16, 16)
            elif self._slot.kind == ToolType.HIGHLIGHTER:
                try:
                    opacity = max(0.15, min(1.0, float(self._slot.opacity)))
                except (TypeError, ValueError):
                    opacity = 0.4
                base.setAlphaF(opacity)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(base)
                try:
                    width = float(self._slot.width_mm)
                except (TypeError, ValueError):
                    width = 3.0
                height = max(10, min(26, int(8 + width * 3.0)))
                painter.drawRoundedRect(rect.x(), rect.center().y() - height // 2,
                                        rect.width(), height, 5, 5)
            elif self._slot.kind in (ToolType.LINE, ToolType.RECTANGLE,
                                      ToolType.ELLIPSE, ToolType.ARROW,
                                      ToolType.POLYGON):
                try:
                    w = max(1.0, min(4.0, float(self._slot.width_mm)))
                except (TypeError, ValueError):
                    w = 1.0
                try:
                    base.setAlphaF(max(0.15, min(1.0,
                                                 float(self._slot.opacity))))
                except (TypeError, ValueError):
                    pass
                try:
                    from tools.models import LINE_STYLES
                    style = self._slot.line_style
                    if style not in LINE_STYLES:
                        style = "solid"
                except Exception:
                    style = "solid"
                pen = _style_pen(base, style, max(1.5, w * 2.0))
                painter.setPen(pen)
                try:
                    fill_on = bool(self._slot.fill_enabled)
                except Exception:
                    fill_on = False
                if (fill_on and self._slot.kind in
                        (ToolType.RECTANGLE, ToolType.ELLIPSE,
                         ToolType.POLYGON)):
                    try:
                        wash = QColor(self._slot.fill_color)
                        if not wash.isValid():
                            wash = QColor("#ffffff")
                        wash.setAlphaF(max(0.0, min(1.0, float(
                            self._slot.fill_opacity))))
                    except (TypeError, ValueError):
                        wash = QColor(255, 255, 255, 255)
                    painter.setBrush(wash)
                else:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                cx, cy = rect.center().x(), rect.center().y()
                bw, bh = rect.width(), rect.height()
                # Padded drawing box inside the fixed-height preview: the
                # pad contains the stroke half-width plus the tallest
                # ending marker (bar = marker size + 1 each side) with
                # margin, so nothing clips at any configured thickness.
                import math as _math

                try:
                    _pw = max(1.5, float(self._slot.width_mm) * 2.0)
                except (TypeError, ValueError):
                    _pw = 3.0
                _pad = max(int(_math.ceil(_pw / 2.0)) + 3, 9)
                _full = self.rect()
                box = _full.adjusted(14, _pad, -14, -_pad)
                if box.width() < 8 or box.height() < 8:
                    box = _full.adjusted(14, 9, -14, -9)
                bcx, bcy = box.center().x(), box.center().y()
                if self._slot.kind in (ToolType.LINE, ToolType.ARROW):
                    x0, y0 = box.left(), box.bottom()
                    x1, y1 = box.right(), box.top()
                    painter.drawLine(x0, y0, x1, y1)
                    try:
                        _paint_ending_marker(
                            painter, self._slot.start_ending, True,
                            x0, y0, base, size=6)
                        _paint_ending_marker(
                            painter, self._slot.end_ending, False,
                            x1, y1, base, size=6)
                    except Exception:
                        pass
                elif self._slot.kind == ToolType.RECTANGLE:
                    # Capped card aspect: a full-width strip would read
                    # as a stroke, not a rectangle; an unconstrained
                    # chip would look lost. 3:1 keeps shape identity
                    # with visual weight, centered in the preview.
                    _cw = min(box.width(), int(box.height() * 3.0))
                    _cbox = QRect(0, 0, _cw, box.height())
                    _cbox.moveCenter(box.center())
                    painter.drawRoundedRect(_cbox, 4, 4)
                elif self._slot.kind == ToolType.ELLIPSE:
                    _cw = min(box.width(), int(box.height() * 3.0))
                    _cbox = QRect(0, 0, _cw, box.height())
                    _cbox.moveCenter(box.center())
                    painter.drawEllipse(_cbox)
                else:  # POLYGON: pentagon outline in tool color.
                    r = min(box.width(), box.height()) / 2.0 - 1.0
                    pts = []
                    for i in range(5):
                        a = -_math.pi / 2 + i * 2 * _math.pi / 5
                        pts.append(QPointF(bcx + r * _math.cos(a),
                                           bcy + r * _math.sin(a)))
                    try:
                        painter.drawPolygon(QPolygonF(pts))
                    except Exception:
                        painter.drawRect(box)
            else:
                painter.setPen(base)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                                 self._slot.kind.value.title())
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


def _tc(tokens, name: str, fallback: tuple) -> QColor:
    """Token color as QColor (handles hex and rgba() token strings)."""
    try:
        from app.theme.tokens import token_rgba

        parsed = token_rgba(getattr(tokens, name))
        return QColor(parsed[0], parsed[1], parsed[2],
                      max(0, min(255, int(round(parsed[3] * 255)))))
    except Exception:
        pass
    try:
        color = QColor(getattr(tokens, name))
        if color.isValid():
            return color
    except Exception:
        pass
    return QColor(*fallback)


class _VisualOption(QAbstractButton):
    """One visual preset: custom paint + selected ring + keyboard focus."""

    def __init__(self, value, paint_fn, tokens: InkfallTokens,
                 width: int, height: int, parent=None, label: str = "") -> None:
        super().__init__(parent)
        self._value = value
        self._paint_fn = paint_fn
        self._tokens = tokens
        self.setCheckable(True)
        # M7.1.4: popup property controls use the neutral arrow (the
        # popup surface owns it) instead of inheriting the canvas tool
        # cursor or forcing a link-hand cursor.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(width, height)
        if label:
            self.setAccessibleName(label)
        self.toggled.connect(lambda _c: self.update())

    @property
    def value(self):
        return self._value

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

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

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Tactile language in paint: 1px lift on hover, 1px
            # compression while pressed; washes/rings unchanged.
            try:
                dy = 1 if self.isDown() else (
                    -1 if self.underMouse() else 0)
                if dy:
                    painter.translate(0, dy)
            except Exception:
                pass
            rect = self.rect()
            if self.isChecked():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(self._tokens, "accent_soft", (139, 147, 224, 51)))
                painter.drawRoundedRect(rect, 8, 8)
                pen = QPen(_tc(self._tokens, "accent", (139, 147, 224, 255)))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)
            elif self.underMouse():
                painter.setPen(Qt.PenStyle.NoPen)
                wash = _tc(self._tokens, "surface_overlay", (255, 255, 255, 22))
                painter.setBrush(wash)
                painter.drawRoundedRect(rect, 8, 8)
            if self.hasFocus():
                pen = QPen(_tc(self._tokens, "input_focus", (139, 147, 224, 255)))
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            try:
                self._paint_fn(painter, rect.adjusted(5, 5, -5, -5), self._tokens)
            except Exception:
                pass
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _QmlColorCard(QWidget):
    """QML square-picker card with a stable live API.

    Visuals (SV square, hue slider, markers, preview, hex) live in
    ``qml/toolprops/ColorPicker.qml``; HSV state and math live in
    ``SquarePickerBridge`` (declarative gradients, no image provider).
    ``picked(QColor)`` / ``hueChanged(int)`` / ``set_hsv`` / ``_sat`` /
    ``_val`` keep every strip/test integration working unchanged.

    Initialization failures raise (never a silent half-built card):
    see ``_validate_view``.
    """

    picked = Signal(object)
    hueChanged = Signal(int)

    SIZE = 200  # SV square edge (QML 200x200 space)

    def __init__(self, tokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._view = None
        self._bridge = None
        self._theme_bridge = None
        try:
            from PySide6.QtQuickWidgets import QQuickWidget
        except Exception as exc:
            raise RuntimeError(
                "Square picker requires PySide6 QtQuickWidgets, "
                f"which could not be imported: {exc}") from exc
        from app.qml_bridge.color_provider import SquarePickerBridge
        from app.qml_bridge.hosting import qml_url
        from app.qml_bridge.theme_bridge import ThemeBridge

        try:
            from app.qml_bridge.hosting import (
                _ensure_control_style as _style)
            _style()
        except Exception:
            pass
        theme = ThemeBridge(tokens, self)
        bridge = SquarePickerBridge(self)
        if theme is None or bridge is None:  # pragma: no cover
            raise RuntimeError(
                "Square picker bridges failed to construct "
                f"(theme={theme}, bridge={bridge})")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        view = QQuickWidget(self)
        view.setObjectName("qml-color-card")
        view.setResizeMode(
            QQuickWidget.ResizeMode.SizeRootObjectToView)
        view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        try:
            view.setClearColor(Qt.GlobalColor.transparent)
        except Exception:
            pass
        engine = view.engine()
        engine.rootContext().setContextProperty("bridge", bridge)
        engine.rootContext().setContextProperty("theme", theme)
        view.setSource(qml_url("toolprops/ColorPicker.qml"))
        self._validate_view(view, "toolprops/ColorPicker.qml")
        # Pre-Ready minimum so the open animation measures the real
        # height even if QML hasn't finished loading; refined by
        # sync_height on Ready.
        try:
            view.setMinimumSize(240, 270)
        except Exception:
            pass
        bridge.picked.connect(self._on_bridge_picked)
        bridge.huePicked.connect(self.hueChanged.emit)
        try:
            view.statusChanged.connect(self._on_view_status)
        except Exception:
            pass
        lay.addWidget(view)
        self._view = view
        self._bridge = bridge
        self._theme_bridge = theme
        try:
            self._on_view_status()
        except Exception:
            pass

    @staticmethod
    def _validate_view(view, source: str) -> None:
        """Fail loudly on QML load errors (never silent null bindings)."""
        try:
            from PySide6.QtQuickWidgets import QQuickWidget as _QW

            if view.status() == _QW.Error:
                try:
                    details = "; ".join(
                        e.toString() for e in view.errors())
                except Exception:
                    details = "<unreadable engine errors>"
                raise RuntimeError(
                    f"QML picker failed to load {source}: {details}")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"QML picker validation failed for {source}: {exc}") from exc

    # -- live API --------------------------------------------------------
    @property
    def _hue(self) -> int:
        try:
            if self._bridge is not None:
                return int(self._bridge.hue)
        except Exception:
            pass
        return 0

    @property
    def _sat(self) -> float:
        try:
            if self._bridge is not None:
                return float(self._bridge.sat)
        except Exception:
            pass
        return 1.0

    @property
    def _val(self) -> float:
        try:
            if self._bridge is not None:
                return float(self._bridge.val)
        except Exception:
            pass
        return 1.0

    def set_hsv(self, hue: int, sat: float, val: float) -> None:
        try:
            if self._bridge is not None:
                self._bridge.setHSV(int(hue), float(sat), float(val))
        except Exception:
            pass

    def set_hue(self, hue: int) -> None:
        try:
            self.set_hsv(int(hue), self._sat, self._val)
        except Exception:
            pass

    def errors(self):
        try:
            if self._view is not None:
                return self._view.errors()
        except Exception:
            pass
        return []

    def status(self):
        try:
            if self._view is not None:
                return self._view.status()
        except Exception:
            pass
        return None

    def update_tokens(self, tokens) -> None:
        self._tokens = tokens
        try:
            if self._theme_bridge is not None:
                self._theme_bridge.update_tokens(tokens)
        except Exception:
            pass

    # -- pointer forwarding (QTest/compat: presses on the card land on
    # the inner Quick view, which delivers them to the QML MouseArea) --
    def _forward_to_view(self, event) -> None:
        try:
            view = self._view
            if view is None:
                return
            from PySide6.QtCore import QCoreApplication as _CA

            pos = view.mapFrom(self, event.position().toPoint())
            clone = type(event)(
                event.type(), pos, event.globalPosition().toPoint(),
                event.button(), event.buttons(), event.modifiers())
            _CA.sendEvent(view, clone)
            event.accept()
        except Exception:
            pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._view is not None:
            self._forward_to_view(event)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._view is not None:
            self._forward_to_view(event)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._view is not None:
            self._forward_to_view(event)
            return
        super().mouseReleaseEvent(event)

    # -- internals -------------------------------------------------------
    def _on_bridge_picked(self, color: str) -> None:
        try:
            self.picked.emit(QColor(str(color)))
        except Exception:
            pass

    def _on_view_status(self) -> None:
        try:
            from PySide6.QtQuickWidgets import QQuickWidget as _QW

            from app.qml_bridge.hosting import sync_height as _sh
            if (self._view is not None
                    and self._view.status() == _QW.Ready):
                _sh(self._view, fallback=284)
        except Exception:
            pass

    def teardown(self) -> None:
        """Blank the QML source while the bridge is alive (see
        PropertiesForm.teardown_qml)."""
        try:
            from app.qml_bridge.hosting import (
                shutdown_quick_view as _sd)
            _sd(self._view)
        except Exception:
            pass


class _SwatchButton(QAbstractButton):
    """Painted circular color swatch: fill circle + accent ring + hover-X.

    No rectangular boxes: true drawEllipse circle. Hover reveals a small
    X affordance (top-right) that removes the preset; otherwise click
    selects. Drag (>8px) reorders within the preset grid via the parent
    section. Context menu also removes.
    """

    removeRequested = Signal(str)
    dragReorder = Signal(object, object)

    def __init__(self, color: str, tokens, parent=None,
                 removable: bool = True) -> None:
        super().__init__(parent)
        self._color = color
        self._tokens = tokens
        self._removable = bool(removable)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(26, 26)
        self.setProperty("swatch_color", color)
        self.setAccessibleName(f"Color {color}")
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda _p: self.removeRequested.emit(self._color))
        self._press_pos = None
        self._dragging = False
        self.toggled.connect(lambda _c: self.update())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                from PySide6.QtCore import QPoint as _QP
                self._press_pos = event.position().toPoint()
                self._dragging = False
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        try:
            if self._press_pos is not None:
                delta = event.position().toPoint() - self._press_pos
                if abs(delta.x()) + abs(delta.y()) >= 8:
                    self._dragging = True
                    self.dragReorder.emit(self._color,
                                         event.globalPosition().toPoint())
                    event.accept()
                    return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        try:
            was_drag = self._dragging
            self._press_pos = None
            self._dragging = False
            if was_drag:
                event.accept()
                self.update()
                return
            # Hover-X hit: small circle top-right removes the preset.
            if self._removable and self._under_x(event.position().toPoint()):
                self.removeRequested.emit(self._color)
                event.accept()
                return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def _under_x(self, pos) -> bool:
        try:
            dx, dy = pos.x() - 20, pos.y() - 6
            return dx * dx + dy * dy <= 36
        except Exception:
            return False

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

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Circular lift on hover, compression while pressed.
            try:
                if str(self.property("swatch_color")) != "__add__":
                    dy = 1 if self.isDown() else (
                        -1 if self.underMouse() else 0)
                    if dy:
                        painter.translate(0, dy)
            except Exception:
                pass
            rect = self.rect()
            cx, cy = rect.center().x(), rect.center().y()
            try:
                is_add = str(self.property("swatch_color")) == "__add__"
            except Exception:
                is_add = False
            if is_add:
                try:
                    edge = QColor(self._tokens.input_border)
                except Exception:
                    edge = QColor("#6b7280")
                pen = QPen(edge)
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(cx - 11, cy - 11, 22, 22)
                pen2 = QPen(edge)
                pen2.setWidth(2)
                pen2.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen2)
                painter.drawLine(cx - 5, cy, cx + 5, cy)
                painter.drawLine(cx, cy - 5, cx, cy + 5)
                return
            try:
                fill = QColor(self._color)
                if not fill.isValid():
                    fill = QColor("#000000")
            except Exception:
                fill = QColor("#000000")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawEllipse(cx - 11, cy - 11, 22, 22)
            # Thin light edge for dark fills + selected accent ring.
            try:
                accent = QColor(self._tokens.accent)
            except Exception:
                accent = QColor("#8B93E0")
            if self.isChecked():
                pen = QPen(accent)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(cx - 12, cy - 12, 24, 24)
                # Clear center dot to signal selection.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(cx - 2, cy - 2, 4, 4)
            if self._removable and self.underMouse():
                # Hover-X badge.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(20, 22, 28, 230))
                painter.drawEllipse(14, 0, 12, 12)
                pen = QPen(QColor("#ffffff"))
                pen.setWidth(1.5)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawLine(17, 3, 23, 9)
                painter.drawLine(23, 3, 17, 9)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


def _palette_surface(tokens: InkfallTokens) -> str:
    """Rounded-rect palette container tone (measured, never overpowering).

    Derived from existing tokens: ``surface_overlay`` lifted toward
    ``text_secondary`` until it reads as a clearly distinct group
    against the popup ``surface_raised`` (>= 2.4:1) without dominating
    the circular swatches (first tone meeting the floor wins).
    """
    try:
        from app.icons import contrast_ratio as _cr
    except Exception:
        _cr = None
    try:
        bg_hex = str(tokens.surface_raised)
        base = QColor(str(tokens.surface_overlay))
        lift = QColor(str(tokens.text_secondary))
    except Exception:
        return "#2A3140"
    if not base.isValid():
        return "#2A3140"
    if lift.isValid():
        for a in (0.16, 0.24, 0.32, 0.42, 0.52):
            try:
                mixed = QColor(
                    int(round(base.red() * (1.0 - a) + lift.red() * a)),
                    int(round(base.green() * (1.0 - a) + lift.green() * a)),
                    int(round(base.blue() * (1.0 - a) + lift.blue() * a)),
                )
                if _cr is None or _cr(mixed.name(), bg_hex) >= 2.4:
                    return mixed.name()
            except Exception:
                continue
    return base.name() if base.isValid() else "#2A3140"


def _palette_well(tokens: InkfallTokens) -> tuple:
    """Recessed palette well: darker fill + distinct boundary.

    M5-A2.2 answers the "stronger than the 2.4 floor" preference with a
    two-part rule instead of one ratio. A single fill-vs-popup floor
    cannot express "darker" in the dark theme (deepening a dark tone
    toward black lowers that ratio), so the rule is: the fill takes a
    plainly perceptible darker step past the M5-A2.1 container tone
    (>= 1.5:1 vs ``_palette_surface``; in the light theme it must also
    stand off the white popup by >= 2.5:1), and the edge must stand off
    from BOTH the popup (>= 2.0:1) and the fill (>= 1.6:1), so the
    boundary stays visible in both themes. First mix meeting its floors
    wins; the circles stay the primary elements.
    """
    try:
        from app.icons import contrast_ratio as _cr
    except Exception:
        _cr = None
    try:
        popup = str(tokens.surface_raised)
        prev = _palette_surface(tokens)
        base = QColor(str(tokens.surface_overlay))
        black = QColor("#000000")
        edge_base = QColor(str(tokens.input_border))
        white = QColor("#ffffff")

        def _mix(a, b, t):
            return QColor(
                int(round(a.red() * (1.0 - t) + b.red() * t)),
                int(round(a.green() * (1.0 - t) + b.green() * t)),
                int(round(a.blue() * (1.0 - t) + b.blue() * t)))

        def _ok(hex_a, hex_b, floor):
            if _cr is None:
                return True
            try:
                return _cr(hex_a, hex_b) >= floor
            except Exception:
                return True
    except Exception:
        return ("#14171d", "#3d4659")
    fill = None
    if base.isValid():
        for t in (0.35, 0.45, 0.55, 0.6):
            try:
                cand = _mix(base, black, t).name()
                if _ok(cand, prev, 1.5) and (
                        popup.lower() != "#ffffff"
                        or _ok(cand, popup, 2.5)):
                    fill = cand
                    break
            except Exception:
                continue
        if fill is None:
            fill = _mix(base, black, 0.6).name()
    else:
        fill = "#14171d"
    edge = None
    if edge_base.isValid():
        # Try lifting the token border toward white (dark theme) and
        # toward black (light theme); first edge distinct from both the
        # popup surface and the well fill wins.
        for lift, steps in ((white, (0.15, 0.3, 0.45, 0.6)),
                            (black, (0.15, 0.3, 0.45, 0.6))):
            for t in steps:
                try:
                    cand = _mix(edge_base, lift, t).name()
                    if _ok(cand, popup, 2.0) and _ok(cand, fill, 1.6):
                        edge = cand
                        break
                except Exception:
                    continue
            if edge is not None:
                break
        if edge is None:
            edge = edge_base.name()
    else:
        edge = "#3d4659"
    return (fill, edge)


def _swatch_outline(color: str, tokens: InkfallTokens, well_bg: str) -> str:
    """Contrasting outline for swatches that would vanish on the well.

    White/black/gray on a near-matching well get a theme-aware hairline
    (light outline on dark wells, dark outline on light wells); every
    other color stays edge-clean (transparent).
    """
    try:
        from app.icons import contrast_ratio as _cr

        if _cr(str(color), str(well_bg)) >= 1.8:
            return "transparent"
    except Exception:
        return "transparent"
    try:
        well_l = QColor(str(well_bg)).lightnessF()
    except Exception:
        well_l = 0.5
    if well_l < 0.5:
        return "rgba(255, 255, 255, 0.30)"
    return "rgba(20, 22, 28, 0.38)"


class _SwatchProxy(QObject):
    """QWidget-free stand-in for a palette swatch (QML grid compat).

    Exposes the historical ``_SwatchButton`` query API used across the
    suites — ``property("swatch_color")``, ``isChecked()``,
    ``click()`` — backed by the PresetBridge (no duplicated state).
    """

    def __init__(self, color: str, checked: bool, click, parent=None) -> None:
        super().__init__(parent)
        self._color = str(color)
        self._checked = bool(checked)
        self._click = click
        try:
            self.setProperty("swatch_color", str(color))
        except Exception:
            pass
        try:
            self.setObjectName(f"swatch-{color}")
        except Exception:
            pass

    def property(self, name):  # noqa: N802
        try:
            if str(name) == "swatch_color":
                return self._color
        except Exception:
            pass
        try:
            return super().property(name)
        except Exception:
            return None

    def isChecked(self):  # noqa: N802
        return bool(self._checked)

    def click(self):  # noqa: N802
        try:
            if self._click is not None:
                self._click()
        except Exception:
            pass

    # No-op QWidget API used by legacy sync paths.
    def setChecked(self, checked):  # noqa: N802
        self._checked = bool(checked)

    def blockSignals(self, _block):  # noqa: N802
        return False

    def update(self):  # noqa: N802
        pass


class _PresetStrip(QWidget):
    """Unified color-preset strip: presets + add/custom + SV/hue + hex.

    One preset store (``store.get/set_color_presets``), one palette
    implementation, one set of color-management rules — bound to a
    property target (``"color"`` stroke or ``"fill"`` fill color) so
    stroke and fill pickers share presets while keeping independent
    selected values. Emits the picked hex on ``changed``; the owner maps
    it onto its own field. ``_ColorSection`` subclasses this for the
    stroke target (name kept for compatibility).
    """

    changed = Signal(str)
    presetsChanged = Signal()
    #: Fill strips: the permanent No Fill slot was activated (M7.1.4.3).
    noFillSelected = Signal()

    #: QSettings key for reusable custom swatches (stays inside the popup).
    CUSTOM_KEY = "inkfall/m5/toolbar/custom_colors"
    MAX_CUSTOM = 8

    def __init__(self, color: str, tokens: InkfallTokens, parent=None,
                 target: str = "color") -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._color = color
        self._target = "fill" if str(target) == "fill" else "color"
        self._swatches: list = []
        # One-time migration: legacy per-popup customs join the unified
        # store palette, then the legacy key is retired (no split UI).
        self._migrate_legacy_customs()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        try:
            from app.toolshelf.store import get_color_presets
        except Exception:
            get_color_presets = None
        try:
            from app.toolshelf.store import get_color_presets as _gcp

            presets = list(_gcp())
        except Exception:
            presets = list(_PRESETS)
        self._presets = presets
        # Full-QML palette grid (PaletteStrip.qml): the well, circular
        # swatches and add tile render in QML; PresetBridge delegates
        # every mutation to the shared store (no QML-side state).
        self._grid_view = None
        self._preset_bridge = None
        self._grid_theme = None
        self._palette_add = None
        self._grid_wrap = QWidget(self)
        self._grid_wrap.setStyleSheet("background: transparent;")
        grid_host = QVBoxLayout(self._grid_wrap)
        grid_host.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._grid_wrap)
        self._root_layout = root
        self._ensure_qml_grid()
        self._rebuild_unified_grid()
        # The circular + tile inside the unified grid is the ONLY add
        # affordance: closed it opens the picker; open it commits the
        # live color into the palette (select + live-apply) and
        # collapses. No text buttons by design.
        picker = QVBoxLayout()
        picker.setContentsMargins(0, 0, 0, 0)
        picker.setSpacing(6)
        # QML square-picker card is built lazily on first picker open
        # so popup construction stays cheap. _ring keeps the stable
        # live API (set_hsv / picked / hueChanged / _sat / _val).
        self._ring = None
        self._picker_layout = picker
        hex_row = QHBoxLayout()
        hex_row.setContentsMargins(0, 0, 0, 0)
        hex_label = QLabel("Hex", self)
        hex_label.setObjectName("tool-popup-meta")
        hex_label.setStyleSheet("background: transparent;")
        self._hex_label = hex_label
        hex_row.addWidget(hex_label)
        hex_edit = QLineEdit(self)
        hex_edit.setMaxLength(7)
        hex_edit.setAccessibleName("Hex color")
        # The edit only ever holds "#rrggbb": cap its width so the
        # picker's size hint (and the popup width) is driven by the
        # swatch grid, not by the line edit's default ~345px hint.
        hex_edit.setMaximumWidth(120)
        hex_edit.editingFinished.connect(self._on_hex_finished)
        hex_edit.returnPressed.connect(self._on_add_custom_collapse)
        self._hex = hex_edit
        hex_row.addWidget(hex_edit, stretch=1)
        hex_wrap = QWidget(self)
        hex_wrap.setStyleSheet("background: transparent;")
        hex_wrap.setLayout(hex_row)
        picker.addWidget(hex_wrap)
        picker_wrap = QWidget(self)
        picker_wrap.setLayout(picker)
        root.addWidget(picker_wrap)
        self._picker_wrap = picker_wrap
        self._picker_open = False
        picker_wrap.setVisible(False)
        try:
            self._style_palette_container()
        except Exception:
            pass
        try:
            self._style_picker_wrap()
        except Exception:
            pass
        self.set_color(color)

    # -- live API -------------------------------------------------------

    def _ensure_qml_picker(self) -> None:
        """Build the QML color card on first picker open (lazy).

        Loud on failure: records ``_qml_error`` and re-raises so the
        cause surfaces instead of cascading into null bindings.
        """
        if self._ring is not None:
            return
        self._qml_error = None
        try:
            card = _QmlColorCard(self._tokens, self)
        except Exception as exc:
            self._qml_error = str(exc)
            self._ring = None
            raise
        try:
            card.picked.connect(self._on_sv_picked)
            card.hueChanged.connect(self._on_hue_changed)
            self._picker_layout.insertWidget(0, card)
            self._ring = card
            try:
                self.set_color(self._color)
            except Exception:
                pass
            # QML card is the sole rounded surface: the QWidget wrap goes
            # transparent so no square backdrop frames the card.
            try:
                if card._view is not None:
                    self._picker_wrap.setStyleSheet(
                        "background: transparent; border: none;")
            except Exception:
                pass
            # If the card finishes loading after the open animation
            # measured, release constraints and refit to its real size.
            # A late Error is recorded loudly (never silent).
            try:
                view = card._view
                if view is not None:
                    def _heal_on_ready() -> None:
                        try:
                            from PySide6.QtQuickWidgets import (
                                QQuickWidget as _QW2)

                            from app.qml_bridge.hosting import (
                                check_view_error as _cve)
                            err = _cve(view, "toolprops/ColorPicker.qml")
                            if err is not None:
                                self._qml_error = err
                                return
                            if view.status() != _QW2.Ready:
                                return
                            if not self._picker_open:
                                return
                            self._clear_picker_constraints()
                            try:
                                self._picker_wrap.setVisible(True)
                            except Exception:
                                pass
                            _refit_popup(self)
                        except Exception:
                            pass
                    view.statusChanged.connect(_heal_on_ready)
                    _heal_on_ready()
            except Exception:
                pass
        except Exception as exc:
            self._qml_error = str(exc)
            self._ring = None
            raise

    def set_color(self, color: str) -> None:
        """Reflect an externally applied color (no echo signal)."""
        self._color = color
        try:
            parsed = QColor(color)
            if parsed.isValid():
                h, s, v, _a = parsed.getHsv()
                if h < 0:
                    h = 0
                if self._ring is not None:
                    self._ring.set_hsv(h, s / 255.0, v / 255.0)
        except Exception:
            pass
        try:
            self._hex.blockSignals(True)
            if self._hex.text().lower() != color.lower():
                self._hex.setText(color)
        except Exception:
            pass
        finally:
            try:
                self._hex.blockSignals(False)
            except Exception:
                pass
        self._sync_swatch_styles()

    def set_fill_state(self, enabled: bool) -> None:
        """Fill strips: the permanent No Fill slot mirrors ``fill_enabled``.

        Stroke strips ignore this (no leading slot exists). Selection is
        still owned by the bridge; this only reflects the live state.
        """
        try:
            if self._preset_bridge is not None:
                self._preset_bridge.sync_no_fill(not bool(enabled))
        except Exception:
            pass

    def _on_no_fill_picked(self) -> None:
        try:
            self.noFillSelected.emit()
        except Exception:
            pass

    def _emit(self, color: str) -> None:
        self.set_color(color)
        self.changed.emit(color)

    def _on_sv_picked(self, color: QColor) -> None:
        try:
            self._emit(color.name())
        except Exception:
            pass

    def _on_hue_changed(self, hue: int) -> None:
        # Derive SV from the ring's live drag state — the strip's stored
        # color is stale mid-drag (it predates this gesture), which used
        # to collapse every hue touch toward black/gray.
        try:
            s = v = None
            try:
                ring = self._ring
                s, v = float(ring._sat), float(ring._val)
            except Exception:
                pass
            if s is None or v is None:
                current = QColor(self._color)
                if not current.isValid():
                    current = QColor("#000000")
                _h, s, v, _a = current.getHsv()
            self._emit(QColor.fromHsv(hue, max(0, s), max(0, v)).name())
        except Exception:
            pass

    def _on_hex_finished(self) -> None:
        try:
            text = self._hex.text().strip()
            if not text.startswith("#"):
                text = f"#{text}"
            if QColor(text).isValid() and len(text) == 7:
                self._emit(text.lower())
            else:
                self.set_color(self._color)
        except Exception:
            pass

    # -- reusable custom swatches (persisted, popup-local) ---------------

    @classmethod
    def _settings(cls):
        from PySide6.QtCore import QCoreApplication, QSettings

        # Production sets these in app/application.py; tests/smoke
        # scripts may not — fall back so swatches persist everywhere.
        try:
            if not QCoreApplication.organizationName():
                QCoreApplication.setOrganizationName("Inkfall")
            if not QCoreApplication.applicationName():
                QCoreApplication.setApplicationName("Inkfall")
        except Exception:
            pass
        return QSettings()

    def _well_colors(self) -> tuple:
        """Current (well_bg, well_edge) for the QML strip."""
        try:
            return _palette_well(self._tokens)
        except Exception:
            return ("#14171d", "#3d4659")

    def _swatch_edge_for(self, color: str) -> str:
        try:
            return _swatch_outline(
                str(color), self._tokens, self._well_colors()[0])
        except Exception:
            return "transparent"

    def _ensure_qml_grid(self) -> None:
        """Build the QML palette grid + bridge (eager: always visible)."""
        if self._grid_view is not None:
            return
        try:
            from PySide6.QtQuickWidgets import QQuickWidget

            from app.qml_bridge.hosting import (
                make_quick_editor, qml_url, sync_height)
            from app.qml_bridge.preset_bridge import PresetBridge
            from app.qml_bridge.theme_bridge import ThemeBridge
            from app.toolshelf.store import (
                get_color_presets as _get, set_color_presets as _set)

            theme = ThemeBridge(self._tokens, self)
            bridge = PresetBridge(
                target=self._target, get_colors=_get, set_colors=_set,
                well_colors=self._well_colors,
                swatch_edge=self._swatch_edge_for,
                on_select=self._emit,
                on_no_fill=self._on_no_fill_picked,
                no_fill=(self._target == "fill"),
                parent=self)
            bridge.sync_selected(self._color)
            bridge.addTileClicked.connect(self._on_palette_add_tile)
            bridge.orderChanged.connect(self._on_bridge_order)
            bridge.contextMenuRequested.connect(self._show_swatch_menu)
            view = make_quick_editor("toolprops/PaletteStrip.qml",
                                     bridge, theme,
                                     f"palette-strip-{self._target}",
                                     self._grid_wrap)
            try:
                view.setMinimumHeight(60)
            except Exception:
                pass
            try:
                view.statusChanged.connect(self._on_grid_status)
            except Exception:
                pass
            layout = self._grid_wrap.layout()
            if layout is not None:
                layout.addWidget(view)
            self._grid_view = view
            self._preset_bridge = bridge
            self._grid_theme = theme
            try:
                self._on_grid_status()
            except Exception:
                pass
        except Exception as exc:
            # Loud: keep the popup alive but record the cause (tests and
            # diagnostics read _qml_error; no silent dead grid).
            try:
                from PySide6.QtCore import qWarning as _qw

                _qw(f"PaletteStrip QML build failed: {exc}")
            except Exception:
                pass
            self._qml_error = str(exc)
            self._grid_view = None

    def _on_grid_status(self) -> None:
        try:
            from PySide6.QtQuickWidgets import QQuickWidget as _QW

            from app.qml_bridge.hosting import sync_height as _sh
            if (self._grid_view is not None
                    and self._grid_view.status() == _QW.Ready):
                _sh(self._grid_view, fallback=80)
        except Exception:
            pass

    def _on_bridge_order(self) -> None:
        """QML-side preset mutation: re-sync proxies + notify siblings."""
        try:
            from app.toolshelf.store import get_color_presets

            self._presets = list(get_color_presets())
        except Exception:
            pass
        try:
            self._rebuild_proxies()
        except Exception:
            pass
        try:
            self.presetsChanged.emit()
        except Exception:
            pass

    def teardown_qml(self) -> None:
        """Blank QML sources while bridges are alive (teardown path)."""
        try:
            from app.qml_bridge.hosting import (
                shutdown_quick_view as _sd)
            _sd(getattr(self, "_grid_view", None))
        except Exception:
            pass
        try:
            ring = getattr(self, "_ring", None)
            if ring is not None and hasattr(ring, "teardown"):
                ring.teardown()
        except Exception:
            pass

    def _rebuild_proxies(self) -> None:
        """Refresh the _swatches compat proxies (selection included)."""
        current = (self._color or "").lower()
        proxies = []
        for preset in list(self._presets):
            try:
                proxies.append(_SwatchProxy(
                    str(preset),
                    str(preset).lower() == current,
                    lambda c=str(preset): self._emit(c),
                    self))
            except Exception:
                continue
        self._swatches = proxies

    def _swatch_center(self, index: int):
        """View coordinates of a grid cell center (tests + tooling).

        Mirrors PaletteStrip.qml metrics: 6 columns, 26px cells, 5px
        spacing, 8px padding, grid horizontally centered; index counts
        preset swatches, with the add tile trailing them. Fill strips
        prepend the permanent No Fill slot (M7.1.4.3), so visual cells
        shift by one there while this index stays store-aligned.
        """
        from PySide6.QtCore import QPoint as _QP

        try:
            view = self._grid_view
            w = max(197, int(view.width()))
            x0 = max(8, (w - 181) / 2.0)
            leading = 1 if self._target == "fill" else 0
            visual = int(index) + leading
            col, row = visual % 6, visual // 6
            return _QP(int(round(x0 + col * 31 + 13)),
                       int(round(8 + row * 31 + 13)))
        except Exception:
            return None

    def _rebuild_unified_grid(self) -> None:
        """Sync the QML grid + compat proxies with the shared store.

        The QML Repeater renders ``bridge.colors`` (no widget rebuild);
        ``_swatches`` stays a same-length proxy list so historical query
        APIs keep working.
        """
        try:
            from app.toolshelf.store import get_color_presets

            self._presets = list(get_color_presets())
        except Exception:
            pass
        try:
            if self._preset_bridge is not None:
                self._preset_bridge.sync_colors()
        except Exception:
            pass
        self._sync_swatch_styles()

    def _remove_preset(self, color: str) -> None:
        try:
            from app.toolshelf.store import set_color_presets
            kept = [p for p in self._presets if p.lower() != color.lower()]
            if not kept:
                return  # keep at least one fallback preset
            self._presets = set_color_presets(kept)
            self._rebuild_unified_grid()
            try:
                self.presetsChanged.emit()
            except Exception:
                pass
        except Exception:
            pass

    def _show_swatch_menu(self, index: int, x: float, y: float) -> None:
        """Themed swatch context menu (M7.1.4.3).

        Replaces the default-styled QML ``Menu`` with the app's shared
        ``InkfallMenu`` material: one named, icon-bearing action with a
        readable disabled state (the palette always keeps at least one
        color), anchored at the clicked swatch and clamped on screen.
        The permanent No Fill slot never reaches this path.
        """
        try:
            from PySide6.QtGui import QAction, QGuiApplication, QIcon

            from app.menu_material import InkfallMenu

            colors = list(self._presets)
            idx = int(index)
            if not 0 <= idx < len(colors):
                return
            color = str(colors[idx])
            menu = InkfallMenu(self, self._tokens)
            action = QAction("Remove color", menu)
            try:
                size = int(getattr(self._tokens, "icon_sm", 16))
                tint = str(getattr(self._tokens, "text_secondary",
                                    "#9aa2b1"))
                icon = _icon_pixmap("trash", tint, size)
                if not icon.isNull():
                    action.setIcon(QIcon(icon))
            except Exception:
                pass
            action.setEnabled(len(colors) > 1)
            action.triggered.connect(
                lambda _checked=False, c=color: self._remove_preset(c))
            menu.addAction(action)
            view = self._grid_view
            if view is None:
                return
            anchor = view.mapToGlobal(QPoint(int(round(x)), int(round(y))))
            pos = anchor
            try:
                screen = (QGuiApplication.screenAt(anchor)
                          or QGuiApplication.primaryScreen())
                if screen is not None:
                    avail = screen.availableGeometry()
                    hint = menu.sizeHint()
                    px = min(max(anchor.x(), avail.left()),
                             max(avail.left(),
                                 avail.right() - hint.width()))
                    py = min(max(anchor.y(), avail.top()),
                             max(avail.top(),
                                 avail.bottom() - hint.height()))
                    pos = QPoint(int(px), int(py))
            except Exception:
                pos = anchor
            menu.exec(pos)
        except Exception:
            pass

    def _move_preset(self, src: int, dst: int) -> None:
        """QML drag-reorder path: persist + sync + notify."""
        try:
            from app.toolshelf.store import set_color_presets

            order = list(self._presets)
            src, dst = int(src), int(dst)
            if 0 <= src < len(order) and 0 <= dst < len(order) \
                    and src != dst:
                item = order.pop(src)
                order.insert(dst, item)
                self._presets = set_color_presets(order)
                self._rebuild_unified_grid()
                try:
                    self.presetsChanged.emit()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_preset_drag(self, color: str, global_pos) -> None:
        try:
            local = self._grid_wrap.mapFromGlobal(global_pos)
            target = None
            for i, sw in enumerate(self._swatches):
                try:
                    if sw.geometry().contains(sw.parentWidget().mapFromGlobal(global_pos)):
                        target = i
                        break
                except Exception:
                    continue
            if target is None:
                return
            order = list(self._presets)
            try:
                src = next(i for i, p in enumerate(order)
                           if p.lower() == str(color).lower())
            except StopIteration:
                return
            if src == target:
                return
            item = order.pop(src)
            order.insert(target, item)
            from app.toolshelf.store import set_color_presets
            self._presets = set_color_presets(order)
            self._rebuild_unified_grid()
            try:
                self.presetsChanged.emit()
            except Exception:
                pass
        except Exception:
            pass

    def _migrate_legacy_customs(self) -> None:
        """Fold pre-unification per-popup customs into the ONE palette.

        Runs once per section construction; after merging, the legacy key
        is retired so no split UI can reappear.
        """
        try:
            from app.toolshelf.store import (get_color_presets,
                                             set_color_presets)
            raw = self._settings().value(self.CUSTOM_KEY, "")
            if not raw:
                return
            import json as _json

            items = _json.loads(str(raw))
            legacy = []
            for item in items if isinstance(items, list) else []:
                try:
                    if isinstance(item, str) and QColor(item).isValid():
                        text = item.lower()
                        if text not in legacy:
                            legacy.append(text)
                except Exception:
                    continue
            if legacy:
                have = [p.lower() for p in get_color_presets()]
                merged = get_color_presets() + [c for c in legacy
                                                if c not in have]
                set_color_presets(merged)
            try:
                self._settings().remove(self.CUSTOM_KEY)
            except Exception:
                pass
        except Exception:
            pass

    @classmethod
    def _settings(cls):
        from PySide6.QtCore import QCoreApplication, QSettings

        # Production sets these in app/application.py; tests/smoke
        # scripts may not — fall back so presets persist everywhere.
        try:
            if not QCoreApplication.organizationName():
                QCoreApplication.setOrganizationName("Inkfall")
            if not QCoreApplication.applicationName():
                QCoreApplication.setApplicationName("Inkfall")
        except Exception:
            pass
        return QSettings()

    def custom_colors(self) -> list[str]:
        """Unified palette contents (legacy name kept for compatibility)."""
        try:
            from app.toolshelf.store import get_color_presets

            return list(get_color_presets())
        except Exception:
            return list(self._presets)

    def _commit_live_color(self) -> bool:
        """Add the live color to the unified palette and select it."""
        try:
            from app.toolshelf.store import set_color_presets

            current = (self._color or "").lower()
            if not QColor(current).isValid():
                return False
            if current not in [p.lower() for p in self._presets]:
                self._presets = set_color_presets([*self._presets, current])
                self._rebuild_unified_grid()
                try:
                    self.presetsChanged.emit()
                except Exception:
                    pass
            self._emit(current)
            return True
        except Exception:
            return False

    #: Picker reveal/collapse duration (fx easing + reduced-motion aware).
    _PICKER_ANIM_MS = 160

    def _set_picker_open(self, show: bool) -> None:
        # Explicit flag: isVisible() depends on ancestors (popup may
        # be mid-layout), so the toggle state is tracked directly.
        self._picker_open = bool(show)
        try:
            if self._palette_add is not None:
                self._palette_add.setAccessibleName(
                    "Add current color to the palette" if show
                    else "Add color — open the color picker")
        except Exception:
            pass
        if bool(show):
            # Loud build: _ensure_qml_picker raises with the cause and
            # records _qml_error instead of opening a dead picker.
            self._ensure_qml_picker()
            try:
                self._animate_picker_open()
            except Exception:
                pass
        else:
            try:
                self._animate_picker_close()
            except Exception:
                pass

    def _picker_generation(self) -> int:
        try:
            self._picker_gen = int(getattr(self, "_picker_gen", 0)) + 1
        except Exception:
            self._picker_gen = 1
        return self._picker_gen

    def _kill_picker_anim(self) -> None:
        """Stop any in-flight picker animation (re-trigger safety)."""
        try:
            wrap = self._picker_wrap
        except AttributeError:
            return
        try:
            previous = wrap.property("_inkfall_tween_h")
            if previous is not None:
                try:
                    previous.stop()
                except Exception:
                    pass
                wrap.setProperty("_inkfall_tween_h", None)
        except Exception:
            pass
        try:
            wrap.setGraphicsEffect(None)
        except Exception:
            pass
        # Reset QML card opacity so a killed hide-fade can never leave
        # the card stuck transparent on rapid re-trigger (the next
        # open replays the reveal from 0 anyway).
        try:
            root = self._picker_qml_root()
            if root is not None:
                root.setProperty("opacity", 1.0)
        except Exception:
            pass

    def _picker_qml_root(self):
        """QML root item of the picker card (None until Ready)."""
        try:
            ring = getattr(self, "_ring", None)
            view = getattr(ring, "_view", None)
            if view is None:
                return None
            return view.rootObject()
        except Exception:
            return None

    def _play_picker_reveal(self, duration_ms: int) -> bool:
        """QML-side picker fade (M5-A2.2.2).

        QWidget opacity effects must never cover the picker wrap's
        QQuickWidget (effect-source reentrant paint). Returns True when
        the QML reveal animation was started; False = caller uses the
        motion-only (tween_height) fallback with identical timing.
        """
        try:
            root = self._picker_qml_root()
            if root is None:
                return False
            from PySide6.QtCore import QMetaObject, Qt

            ok = QMetaObject.invokeMethod(
                root, "playReveal", Qt.ConnectionType.QueuedConnection,
                int(max(1, int(duration_ms))))
            return bool(ok)
        except Exception:
            return False

    def _play_picker_hide(self, duration_ms: int) -> bool:
        """QML-side picker hide fade (mirror of reveal)."""
        try:
            root = self._picker_qml_root()
            if root is None:
                return False
            from PySide6.QtCore import QMetaObject, Qt

            ok = QMetaObject.invokeMethod(
                root, "playHide", Qt.ConnectionType.QueuedConnection,
                int(max(1, int(duration_ms))))
            return bool(ok)
        except Exception:
            return False

    def _picker_full_height(self) -> int:
        """Measured open height of the picker wrap (layout-activated)."""
        wrap = self._picker_wrap
        try:
            wrap.setVisible(True)
            try:
                wrap.layout().activate()
            except Exception:
                pass
            full = max(int(wrap.sizeHint().height()),
                       int(wrap.minimumSizeHint().height()))
        except Exception:
            full = 0
        return max(1, full)

    def _clear_picker_constraints(self) -> None:
        """Release height constraints so later content/layout changes
        (theme, fonts, refits) never inherit a stale animation height."""
        try:
            wrap = self._picker_wrap
            wrap.setMinimumHeight(0)
            wrap.setMaximumHeight(16777215)
        except Exception:
            pass

    def _animate_picker_open(self) -> None:
        """Reserve-then-reveal: popup anchors for the final size first
        (never escapes/clips mid-flight), then the picker grows inside
        the reserved space over ~160ms with fx easing plus a QML-side
        opacity reveal (M5-A2.2.2: no QWidget effect over QML)."""
        from app import fx as _fx

        gen = self._picker_generation()
        self._kill_picker_anim()
        wrap = self._picker_wrap
        full = self._picker_full_height()
        wrap.setMinimumHeight(0)
        wrap.setMaximumHeight(0)
        wrap.setVisible(True)
        # Reserve final geometry before revealing (anchored, clamped).
        wrap.setMinimumHeight(full)
        wrap.setMaximumHeight(full)
        _refit_popup(self)
        wrap.setMinimumHeight(0)
        wrap.setMaximumHeight(0)
        try:
            reduced = bool(_fx.reduced_motion_enabled())
        except Exception:
            reduced = False
        if reduced:
            self._clear_picker_constraints()
            wrap.setVisible(True)
            _refit_popup(self)
            return

        def _healed() -> None:
            try:
                if int(getattr(self, "_picker_gen", -1)) != gen:
                    return
                if not self._picker_open:
                    return
                self._clear_picker_constraints()
                try:
                    wrap.setVisible(True)
                except Exception:
                    pass
                _refit_popup(self)
            except Exception:
                pass

        _fx.tween_height(wrap, full, self._PICKER_ANIM_MS, self,
                         finished=_healed)
        # QML-side opacity reveal (same duration/easing); QWidget
        # fade_in(wrap) is deliberately NOT used — an opacity effect on
        # this wrap would capture the QQuickWidget via the effect-source
        # pixmap path and log QPainter reentrancy errors.
        try:
            self._play_picker_reveal(self._PICKER_ANIM_MS)
        except Exception:
            pass
        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self._PICKER_ANIM_MS + 250, _healed)
        except Exception:
            pass

    def _animate_picker_close(self) -> None:
        """Collapse: picker shrinks over ~140ms with a QML-side hide fade,
        then hides and the popup refits (anchored, inside the window).
        No QWidget opacity effect over the QML wrap (M5-A2.2.2)."""
        from app import fx as _fx

        gen = self._picker_generation()
        self._kill_picker_anim()
        wrap = self._picker_wrap
        try:
            showing = bool(wrap.isVisible())
        except Exception:
            showing = True
        try:
            full = max(int(wrap.height()),
                       int(wrap.sizeHint().height()), 1)
        except Exception:
            full = 1
        if not showing:
            try:
                wrap.setVisible(False)
            except Exception:
                pass
            self._clear_picker_constraints()
            _refit_popup(self)
            return
        try:
            reduced = bool(_fx.reduced_motion_enabled())
        except Exception:
            reduced = False
        if reduced:
            try:
                wrap.setVisible(False)
            except Exception:
                pass
            self._clear_picker_constraints()
            _refit_popup(self)
            return

        def _healed() -> None:
            try:
                if int(getattr(self, "_picker_gen", -1)) != gen:
                    return
                if self._picker_open:
                    return
                try:
                    wrap.setVisible(False)
                except Exception:
                    pass
                self._clear_picker_constraints()
                _refit_popup(self)
            except Exception:
                pass

        _fx.tween_height(wrap, 0, 140, self, finished=_healed)
        # QML-side hide fade; never fx.fade_out(wrap) here (see open).
        try:
            self._play_picker_hide(140)
        except Exception:
            pass
        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(140 + 250, _healed)
        except Exception:
            pass

    def _on_palette_add_tile(self) -> None:
        """Circular + tile: open the picker, or commit + collapse."""
        try:
            if self._picker_open:
                self._on_add_custom_collapse()
            else:
                self._set_picker_open(True)
        except Exception:
            pass

    def _on_add_custom_collapse(self) -> None:
        """Save live color into the unified palette, select, collapse."""
        try:
            self._commit_live_color()
        except Exception:
            pass
        try:
            if self._picker_open:
                self._set_picker_open(False)
        except Exception:
            pass

    def _sync_swatch_styles(self) -> None:
        """Reflect selection into the bridge + compat proxies (no echo)."""
        try:
            if self._preset_bridge is not None:
                self._preset_bridge.sync_selected(self._color or "")
        except Exception:
            pass
        try:
            self._rebuild_proxies()
        except Exception:
            pass

    def refresh_presets(self) -> None:
        """Re-read the shared preset store; rebuild only on real change.

        Keeps sibling strips (stroke/fill) and reopened popups in sync
        without disturbing selection or picker state when unchanged.
        """
        try:
            from app.toolshelf.store import get_color_presets
            live = get_color_presets()
        except Exception:
            return
        try:
            if ([p.lower() for p in live]
                    != [p.lower() for p in self._presets]):
                self._presets = live
                self._rebuild_unified_grid()
        except Exception:
            pass

    def showEvent(self, event) -> None:  # noqa: N802
        try:
            super().showEvent(event)
        except Exception:
            pass
        try:
            self.refresh_presets()
        except Exception:
            pass

    def _style_palette_container(self) -> None:
        """Grid host stays transparent: the QML well (PaletteStrip.qml)
        is the sole rounded-rectangle container behind the swatches."""
        try:
            self._grid_wrap.setStyleSheet("background: transparent;")
        except Exception:
            pass

    def _style_picker_wrap(self) -> None:
        """Rounded editor panel behind the hue ring + hex row, so the
        open picker reads as one integrated card (never a square
        native-looking patch inside the rounded popup)."""
        try:
            wrap = self._picker_wrap
        except AttributeError:
            return
        try:
            bg = _palette_surface(self._tokens)
            try:
                border = str(self._tokens.input_border)
            except Exception:
                border = "#2A3140"
            wrap.setStyleSheet(
                f"background-color: {bg}; border: 1px solid {border};"
                f" border-radius: 12px;")
            try:
                layout = wrap.layout()
                if layout is not None:
                    layout.setContentsMargins(10, 10, 10, 10)
            except Exception:
                pass
        except Exception:
            pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            self._style_palette_container()
        except Exception:
            pass
        try:
            if self._grid_theme is not None:
                self._grid_theme.update_tokens(tokens)
            if self._preset_bridge is not None:
                self._preset_bridge.refreshWell()
        except Exception:
            pass
        try:
            self._style_picker_wrap()
        except Exception:
            pass
        try:
            if self._ring is not None:
                self._ring.update_tokens(tokens)
        except Exception:
            pass
        try:
            from app.theme.typography import role_font

            self._hex_label.setFont(role_font(tokens, "meta"))
            self._hex_label.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
            self._hex.setFont(role_font(tokens, "body"))
            self._hex.setStyleSheet(
                f"color: {tokens.text_primary};"
                f" background-color: {tokens.input_background};"
                f" border: 1px solid {tokens.input_border}; border-radius: 6px;"
                f" padding: 4px 8px;")
        except Exception:
            pass
        self._sync_swatch_styles()


class _ColorSection(_PresetStrip):
    """Stroke-color strip: the shared preset implementation, stroke-bound.

    Name and constructor preserved for compatibility; all behavior lives
    on :class:`_PresetStrip` with ``target="color"`.
    """

    def __init__(self, color: str, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(color, tokens, parent, target="color")


class _SizePreview(QWidget):
    """Small live size preview: dot (pen/eraser) or stroke (other)."""

    def __init__(self, kind: ToolType, color: str, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._color = color
        self._width = 0.5
        self._tokens = None

    def set_width(self, width: float) -> None:
        self._width = float(width)
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def update_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            try:
                base = QColor(self._color)
                if not base.isValid():
                    base = QColor("#000000")
            except Exception:
                base = QColor("#000000")
            if self._kind == ToolType.ERASER:
                base = QColor("#9aa0aa")
            if self._kind == ToolType.HIGHLIGHTER:
                base.setAlpha(140)
            else:
                # Keep the preview swatch visible on the popup surface
                # (hue-preserving shift, same as line-style samples).
                try:
                    from app.icons import ensure_contrast_fg as _ecfg2

                    surface = "#181C23"
                    try:
                        if (self._tokens is not None and hasattr(
                                self._tokens, "surface_raised")):
                            surface = str(self._tokens.surface_raised)
                    except Exception:
                        pass
                    keep_alpha = base.alpha()
                    base = QColor(_ecfg2(base.name(), surface))
                    base.setAlpha(keep_alpha)
                except Exception:
                    pass
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(base)
            if self._kind in (ToolType.PEN, ToolType.ERASER):
                d = max(6, min(24, int(6 + self._width * 8.0)))
                cx, cy = rect.center().x(), rect.center().y()
                painter.drawEllipse(cx - d // 2, cy - d // 2, d, d)
            else:
                h = max(3, min(16, int(3 + self._width * 2.5)))
                painter.drawRoundedRect(2, rect.center().y() - h // 2,
                                        rect.width() - 4, h, 3, 3)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _SizeSection(QWidget):
    """Compact direct size: live preview + slider + mm readout.

    No generic icon preset tiles (tiles are reserved for Line/Start/End).
    Slider 0-100 maps per-kind width range with slight non-linear bias
    for fine small-width control. Exact numeric input lives behind
    Custom (hidden by default). Size preset collections stay mutable
    in the store layer (add/remove/reorder via API).
    """

    changed = Signal(float)

    def __init__(self, kind: ToolType, width: float, color: str,
                 tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._kind = kind
        self._family = size_family(kind)
        self._min_w, self._max_w = self._range_for(kind)
        self._value = width
        self._color = color
        self._options: list = []
        self._presets: list[float] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._preview_dot = _SizePreview(kind, color, self)
        self._preview_dot.setFixedSize(40, 28)
        row.addWidget(self._preview_dot)
        from PySide6.QtWidgets import QSlider as _QSlider
        slider = _QSlider(Qt.Orientation.Horizontal, self)
        slider.setRange(0, 100)
        slider.setAccessibleName("Thickness slider" if kind not in (
            ToolType.PEN, ToolType.HIGHLIGHTER, ToolType.ERASER)
            else "Size slider")
        slider.valueChanged.connect(self._on_slider)
        self._slider = slider
        row.addWidget(slider, stretch=1)
        self._readout = QLabel(self)
        self._readout.setMinimumWidth(48)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._readout.setObjectName("tool-popup-meta")
        row.addWidget(self._readout)
        row_wrap = QWidget(self)
        row_wrap.setLayout(row)
        row_wrap.setStyleSheet("background: transparent;")
        root.addWidget(row_wrap)
        # Numeric precision on demand: hidden by default, revealed via
        # the subtle Custom affordance (visual presets are primary).
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(0, 0, 0, 0)
        custom_row.addStretch(1)
        custom_btn = QPushButton("Custom…", self)
        custom_btn.setCursor(Qt.CursorShape.ArrowCursor)
        custom_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        custom_btn.setAccessibleName("Show custom size editor")
        custom_btn.setFlat(True)
        self._custom_btn = custom_btn
        _fx.install_hover_lift(custom_btn)
        custom_row.addWidget(custom_btn)
        custom_wrap = QWidget(self)
        custom_wrap.setStyleSheet("background: transparent;")
        custom_wrap.setLayout(custom_row)
        root.addWidget(custom_wrap)
        num_row = QHBoxLayout()
        num_row.setContentsMargins(0, 0, 0, 0)
        num_label = QLabel("Exact", self)
        num_label.setObjectName("tool-popup-meta")
        num_label.setStyleSheet("background: transparent;")
        self._num_label = num_label
        num_row.addWidget(num_label)
        num_row.addStretch(1)
        # Exact numeric input is an inline QML drawer (NumericEditor.qml),
        # built lazily on first open so popup-open stays cheap. The Python
        # ToolInstance remains authoritative: the bridge forwards validated
        # commits through _on_spin and Escape through _cancel_edit.
        self._editor = None
        self._bridge = None
        self._theme_bridge = None
        self._revert_value = float(width)
        num_wrap = QWidget(self)
        num_wrap.setStyleSheet("background: transparent;")
        num_wrap.setLayout(num_row)
        root.addWidget(num_wrap)
        self._num_wrap = num_wrap
        num_wrap.setVisible(False)
        self._numeric_open = False
        custom_btn.clicked.connect(self._toggle_numeric)
        self.set_value(width)

    # -- live API -------------------------------------------------------

    @staticmethod
    def _range_for(kind: ToolType) -> tuple[float, float]:
        try:
            presets = tuple(size_presets_for(kind))
            if presets:
                return (max(0.1, min(presets)), max(presets))
        except Exception:
            pass
        if kind == ToolType.HIGHLIGHTER:
            return (1.5, 5.0)
        if kind == ToolType.ERASER:
            return (1.0, 5.0)
        return (0.3, 2.0)

    def _to_slider(self, width: float) -> int:
        try:
            lo, hi = self._min_w, self._max_w
            t = (max(lo, min(hi, width)) - lo) / max(1e-6, hi - lo)
            return max(0, min(100, int(round((t ** (1.0 / 1.5)) * 100))))
        except Exception:
            return 50

    def _from_slider(self, pos: int) -> float:
        try:
            lo, hi = self._min_w, self._max_w
            t = max(0.0, min(1.0, pos / 100.0)) ** 1.5
            return lo + (hi - lo) * t
        except Exception:
            return self._value

    def set_value(self, width: float) -> None:
        self._value = width
        try:
            lo, hi = self._min_w, self._max_w
        except Exception:
            lo, hi = 0.1, 20.0
        try:
            if self._bridge is not None:
                self._bridge.sync_external(max(lo, min(hi, float(width))))
        except Exception:
            pass
        try:
            self._slider.blockSignals(True)
            self._slider.setValue(self._to_slider(width))
        except Exception:
            pass
        finally:
            try:
                self._slider.blockSignals(False)
            except Exception:
                pass
        try:
            self._readout.setText(f"{float(width):.1f} mm")
        except Exception:
            pass
        try:
            self._preview_dot.set_width(float(width))
            self._preview_dot.set_color(self._color)
        except Exception:
            pass

    def set_color(self, color: str) -> None:
        self._color = color
        try:
            self._preview_dot.set_color(color)
        except Exception:
            pass

    def _emit(self, value: float) -> None:
        self.set_value(value)
        self.changed.emit(float(value))

    def _on_slider(self, pos: int) -> None:
        value = self._from_slider(int(pos))
        self.set_value(value)
        self.changed.emit(float(value))
        # Slider moves are committed values: a later Escape restores
        # this, not a stale focus-in snapshot.
        try:
            self._revert_value = float(value)
        except Exception:
            pass

    def _ensure_editor(self) -> None:
        """Lazily build the QML exact-size drawer + bridge."""
        if self._editor is not None:
            return
        try:
            from app.qml_bridge.hosting import make_quick_editor, sync_height
            from app.qml_bridge.numeric_bridge import NumericBridge
            from app.qml_bridge.theme_bridge import ThemeBridge
        except Exception:
            return
        try:
            lo, hi = self._min_w, self._max_w
            start = max(lo, min(hi, float(self._value)))
            self._theme_bridge = ThemeBridge(self._tokens, self)
            self._bridge = NumericBridge(
                value=start, minimum=lo, maximum=hi,
                decimals=1, step=0.1, unit="mm", label="Exact size",
                on_commit=self._on_editor_commit,
                on_cancel=self._cancel_edit,
                on_begin=self._begin_revert_point, parent=self)
            view = make_quick_editor("toolprops/NumericEditor.qml",
                                     self._bridge, self._theme_bridge,
                                     "exact-size-editor", self._num_wrap)
            try:
                view.statusChanged.connect(self._on_editor_status)
            except Exception:
                pass
            layout = self._num_wrap.layout()
            if layout is not None:
                layout.addWidget(view)
            self._editor = view
            # statusChanged may have fired before we connected (local
            # file loads fast): sync now and re-check once delayed.
            try:
                self._on_editor_status()
                from PySide6.QtCore import QTimer

                QTimer.singleShot(120, self._on_editor_status)
            except Exception:
                pass
        except Exception as exc:
            # Loud: record the cause (no silent dead drawer).
            try:
                from PySide6.QtCore import qWarning as _qw

                _qw(f"NumericEditor QML build failed (size): {exc}")
            except Exception:
                pass
            self._qml_error = str(exc)
            self._editor = None
            raise

    def _on_editor_status(self) -> None:
        try:
            from PySide6.QtQuickWidgets import QQuickWidget

            from app.qml_bridge.hosting import (
                check_view_error as _cve, sync_height)
            if self._editor is not None:
                err = _cve(self._editor, "toolprops/NumericEditor.qml")
                if err is not None:
                    self._qml_error = err
                    return
            if (self._editor is not None
                    and self._editor.status() == QQuickWidget.Ready):
                sync_height(self._editor)
                if self._numeric_open:
                    _refit_popup(self)
        except Exception:
            pass

    def teardown_qml(self) -> None:
        """Blank the drawer source while the bridge is alive."""
        try:
            from app.qml_bridge.hosting import (
                shutdown_quick_view as _sd)
            _sd(getattr(self, "_editor", None))
        except Exception:
            pass

    def _on_editor_commit(self, value: float) -> None:
        self._on_spin(float(value))

    def _toggle_numeric(self) -> None:
        show = not self._numeric_open
        if show:
            # Loud build first: on QML failure the drawer stays closed
            # and _qml_error carries the cause (no half-built editor).
            self._ensure_editor()
        self._numeric_open = show
        try:
            if show:
                try:
                    if self._bridge is not None:
                        lo, hi = self._min_w, self._max_w
                        self._bridge.sync_external(
                            max(lo, min(hi, float(self._value))))
                except Exception:
                    pass
            self._num_wrap.setVisible(show)
            self._custom_btn.setText("Hide" if show else "Custom…")
            _refit_popup(self)
            if show:
                try:
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(
                        0, lambda: self._bridge.requestFocus()
                        if self._bridge is not None else None)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_spin(self, value: float) -> None:
        # Clamp to the tool's own supported range (the spin widget is
        # already ranged, but typed text can momentarily exceed it).
        try:
            value = max(self._min_w, min(self._max_w, float(value)))
        except Exception:
            pass
        self.set_value(float(value))
        self.changed.emit(float(value))
        # Editor commits end the edit session: Escape after this
        # restores the just-committed value (the prior committed one).
        try:
            self._revert_value = float(value)
        except Exception:
            pass

    def _begin_revert_point(self) -> None:
        """Snapshot the model value when editing starts (Escape target)."""
        try:
            self._revert_value = float(self._value)
        except Exception:
            pass

    def _cancel_edit(self) -> None:
        """Escape: restore the focus-in value through the normal path."""
        try:
            base = float(getattr(self, "_revert_value", self._value))
        except Exception:
            return
        self._emit(base)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            from app.theme.typography import role_font

            self._num_label.setFont(role_font(tokens, "meta"))
            self._num_label.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
            self._readout.setFont(role_font(tokens, "meta"))
            self._readout.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
            self._custom_btn.setFont(role_font(tokens, "meta"))
            self._custom_btn.setStyleSheet(
                f"color: {tokens.text_muted}; background: transparent;"
                " border: none; padding: 2px 4px;")
            try:
                if self._theme_bridge is not None:
                    self._theme_bridge.update_tokens(tokens)
            except Exception:
                pass
            self._slider.setStyleSheet(
                f"QSlider::groove:horizontal {{ height: 4px;"
                f" background-color: {tokens.input_background};"
                " border-radius: 2px; }"
                f"QSlider::handle:horizontal {{ width: 16px; height: 16px;"
                " margin: -6px 0; border-radius: 8px;"
                f" background-color: {tokens.accent};"
                f" border: 1px solid {tokens.input_border}; }}"
                f"QSlider::sub-page:horizontal {{ background-color:"
                f" {tokens.accent}; border-radius: 2px; }}")
        except Exception:
            pass
        try:
            self._preview_dot.update_tokens(tokens)
        except Exception:
            pass


class _OpacityPreview(QWidget):
    """Small live opacity bar in the configured color."""

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self._color = color
        self._opacity = 1.0
        self._tokens = None

    def set_opacity(self, value: float) -> None:
        self._opacity = max(0.0, min(1.0, value))
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def update_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            try:
                base = QColor(self._color)
                if not base.isValid():
                    base = QColor("#000000")
            except Exception:
                base = QColor("#000000")
            base.setAlphaF(self._opacity)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(base)
            painter.drawRoundedRect(2, rect.center().y() - 7, rect.width() - 4,
                                    14, 4, 4)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _OpacitySection(QWidget):
    """Compact direct opacity: live bar preview + slider + readout.

    No generic icon preset tiles. Exact percent lives behind Custom.
    Opacity preset stops stay mutable in the store layer.
    """

    changed = Signal(float)

    def __init__(self, color: str, opacity: float,
                 tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._color = color
        self._value = opacity
        self._options: list = []
        self._presets: list[float] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._bar = _OpacityPreview(color, self)
        self._bar.setFixedSize(40, 28)
        row.addWidget(self._bar)
        from PySide6.QtWidgets import QSlider as _QSlider2
        slider = _QSlider2(Qt.Orientation.Horizontal, self)
        slider.setRange(0, 100)
        slider.setAccessibleName("Opacity slider")
        slider.valueChanged.connect(self._on_slider)
        self._slider = slider
        row.addWidget(slider, stretch=1)
        self._readout = QLabel(self)
        self._readout.setMinimumWidth(44)
        self._readout.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._readout.setObjectName("tool-popup-meta")
        row.addWidget(self._readout)
        row_wrap = QWidget(self)
        row_wrap.setLayout(row)
        row_wrap.setStyleSheet("background: transparent;")
        root.addWidget(row_wrap)
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(0, 0, 0, 0)
        custom_row.addStretch(1)
        custom_btn = QPushButton("Custom…", self)
        custom_btn.setCursor(Qt.CursorShape.ArrowCursor)
        custom_btn.setFlat(True)
        custom_btn.setAccessibleName("Show custom opacity editor")
        self._custom_btn = custom_btn
        _fx.install_hover_lift(custom_btn)
        custom_row.addWidget(custom_btn)
        custom_wrap = QWidget(self)
        custom_wrap.setStyleSheet("background: transparent;")
        custom_wrap.setLayout(custom_row)
        root.addWidget(custom_wrap)
        num_row = QHBoxLayout()
        num_row.setContentsMargins(0, 0, 0, 0)
        num_label = QLabel("Exact", self)
        num_label.setObjectName("tool-popup-meta")
        num_label.setStyleSheet("background: transparent;")
        self._num_label = num_label
        num_row.addWidget(num_label)
        num_row.addStretch(1)
        # Exact percent is the shared inline QML drawer (NumericEditor.qml),
        # built lazily on first open. Display units are percent; the model
        # stays a 0..1 fraction converted by the bridge.
        self._editor = None
        self._bridge = None
        self._theme_bridge = None
        self._revert_value = float(max(0.0, min(1.0, opacity)))
        num_wrap = QWidget(self)
        num_wrap.setStyleSheet("background: transparent;")
        num_wrap.setLayout(num_row)
        root.addWidget(num_wrap)
        self._num_wrap = num_wrap
        num_wrap.setVisible(False)
        self._numeric_open = False

        def _toggle() -> None:
            show = not self._numeric_open
            if show:
                # Loud build first (see size section): drawer stays
                # closed and _qml_error carries the cause on failure.
                self._ensure_editor()
            self._numeric_open = show
            try:
                if show:
                    try:
                        if self._bridge is not None:
                            self._bridge.sync_external(
                                max(0.0, min(100.0, self._value * 100.0)))
                    except Exception:
                        pass
                num_wrap.setVisible(show)
                custom_btn.setText("Hide" if show else "Custom…")
                _refit_popup(self)
                if show:
                    try:
                        from PySide6.QtCore import QTimer

                        QTimer.singleShot(
                            0, lambda: self._bridge.requestFocus()
                            if self._bridge is not None else None)
                    except Exception:
                        pass
            except Exception:
                pass

        custom_btn.clicked.connect(_toggle)
        self.set_value(opacity)

    def set_value(self, opacity: float) -> None:
        self._value = max(0.0, min(1.0, opacity))
        try:
            if self._bridge is not None:
                self._bridge.sync_external(
                    max(0.0, min(100.0, self._value * 100.0)))
        except Exception:
            pass
        try:
            self._slider.blockSignals(True)
            self._slider.setValue(int(round(self._value * 100)))
        except Exception:
            pass
        finally:
            try:
                self._slider.blockSignals(False)
            except Exception:
                pass
        try:
            self._readout.setText(f"{int(round(self._value * 100))}%")
        except Exception:
            pass
        try:
            self._bar.set_opacity(self._value)
            self._bar.set_color(self._color)
        except Exception:
            pass

    def set_color(self, color: str) -> None:
        self._color = color
        try:
            self._bar.set_color(color)
        except Exception:
            pass

    def _emit(self, value: float) -> None:
        self.set_value(value)
        self.changed.emit(float(value))

    def _on_slider(self, pos: int) -> None:
        fraction = max(0.0, min(1.0, int(pos) / 100.0))
        self.set_value(fraction)
        self.changed.emit(fraction)
        try:
            self._revert_value = float(fraction)
        except Exception:
            pass

    def _on_spin(self, value: int) -> None:
        fraction = max(0.0, min(1.0, int(value) / 100.0))
        self.set_value(fraction)
        self.changed.emit(fraction)
        try:
            self._revert_value = float(fraction)
        except Exception:
            pass

    def _ensure_editor(self) -> None:
        """Lazily build the QML exact-percent drawer + bridge."""
        if self._editor is not None:
            return
        try:
            from app.qml_bridge.hosting import make_quick_editor
            from app.qml_bridge.numeric_bridge import NumericBridge
            from app.qml_bridge.theme_bridge import ThemeBridge
        except Exception:
            return
        try:
            start = max(0.0, min(100.0, float(self._value) * 100.0))
            self._theme_bridge = ThemeBridge(self._tokens, self)
            self._bridge = NumericBridge(
                value=start, minimum=0.0, maximum=100.0,
                decimals=0, step=1.0, unit="%", label="Exact opacity",
                to_display=lambda v: v, from_display=lambda v: v / 100.0,
                on_commit=self._on_editor_commit,
                on_cancel=self._cancel_edit,
                on_begin=self._begin_revert_point, parent=self)
            view = make_quick_editor("toolprops/NumericEditor.qml",
                                     self._bridge, self._theme_bridge,
                                     "exact-opacity-editor", self._num_wrap)
            try:
                view.statusChanged.connect(self._on_editor_status)
            except Exception:
                pass
            layout = self._num_wrap.layout()
            if layout is not None:
                layout.addWidget(view)
            self._editor = view
            try:
                self._on_editor_status()
                from PySide6.QtCore import QTimer as _QTimer2

                _QTimer2.singleShot(120, self._on_editor_status)
            except Exception:
                pass
        except Exception as exc:
            try:
                from PySide6.QtCore import qWarning as _qw2

                _qw2(f"NumericEditor QML build failed (opacity): {exc}")
            except Exception:
                pass
            self._qml_error = str(exc)
            self._editor = None
            raise

    def _on_editor_status(self) -> None:
        try:
            from PySide6.QtQuickWidgets import QQuickWidget as _QQuickWidget2

            from app.qml_bridge.hosting import (
                check_view_error as _cve2, sync_height as _sync_height2)
            if self._editor is not None:
                err = _cve2(self._editor, "toolprops/NumericEditor.qml")
                if err is not None:
                    self._qml_error = err
                    return
            if (self._editor is not None
                    and self._editor.status() == _QQuickWidget2.Ready):
                _sync_height2(self._editor)
                if self._numeric_open:
                    _refit_popup(self)
        except Exception:
            pass

    def teardown_qml(self) -> None:
        """Blank the drawer source while the bridge is alive."""
        try:
            from app.qml_bridge.hosting import (
                shutdown_quick_view as _sd2)
            _sd2(getattr(self, "_editor", None))
        except Exception:
            pass

    def _on_editor_commit(self, value: float) -> None:
        # Bridge already converted display percent to the 0..1 model
        # fraction via from_display; emit through the normal path.
        try:
            fraction = max(0.0, min(1.0, float(value)))
        except Exception:
            return
        self.set_value(fraction)
        self.changed.emit(fraction)
        try:
            self._revert_value = float(fraction)
        except Exception:
            pass

    def _begin_revert_point(self) -> None:
        """Snapshot the model value when editing starts (Escape target)."""
        try:
            self._revert_value = float(self._value)
        except Exception:
            pass

    def _cancel_edit(self) -> None:
        """Escape: restore the focus-in value through the normal path."""
        try:
            base = float(getattr(self, "_revert_value", self._value))
        except Exception:
            return
        self._emit(base)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            from app.theme.typography import role_font

            self._num_label.setFont(role_font(tokens, "meta"))
            self._num_label.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
            self._readout.setFont(role_font(tokens, "meta"))
            self._readout.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
            self._custom_btn.setFont(role_font(tokens, "meta"))
            self._custom_btn.setStyleSheet(
                f"color: {tokens.text_muted}; background: transparent;"
                " border: none; padding: 2px 4px;")
            try:
                if self._theme_bridge is not None:
                    self._theme_bridge.update_tokens(tokens)
            except Exception:
                pass
            self._slider.setStyleSheet(
                f"QSlider::groove:horizontal {{ height: 4px;"
                f" background-color: {tokens.input_background};"
                " border-radius: 2px; }"
                f"QSlider::handle:horizontal {{ width: 16px; height: 16px;"
                " margin: -6px 0; border-radius: 8px;"
                f" background-color: {tokens.accent};"
                f" border: 1px solid {tokens.input_border}; }}"
                f"QSlider::sub-page:horizontal {{ background-color:"
                f" {tokens.accent}; border-radius: 2px; }}")
        except Exception:
            pass
        try:
            self._bar.update_tokens(tokens)
        except Exception:
            pass


def _style_pen(base: QColor, style: str, width: float = 3.0) -> QPen:
    """QPen with a visual line style (solid/dashed/dotted)."""
    pen = QPen(base)
    pen.setWidthF(max(1.0, width))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
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


def _paint_ending_marker(painter: QPainter, ending: str, start_side: bool,
                         x: int, cy: int, color: QColor,
                         size: int = 7) -> None:
    """Draw one ending marker (dot/bar/arrow chevron) at (x, cy)."""
    try:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        if ending == "dot":
            painter.drawEllipse(x - size // 2, cy - size // 2, size, size)
        elif ending == "bar":
            painter.drawRoundedRect(x - 2, cy - size - 1, 4, 2 * size + 2, 2, 2)
        elif ending == "arrow":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(color)
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            direction = -1 if start_side else 1
            painter.drawLine(x - direction * size, cy - size // 2,
                             x, cy)
            painter.drawLine(x - direction * size, cy + size // 2,
                             x, cy)
    except Exception:
        pass


class _StyleDrawerPopup(QWidget):
    """Floating style-options drawer that opens from a control row.

    Mirrors ``ToolPopup``'s floating-shell language (painted 14 px body,
    1 px hairline, connector nub and soft shadow; clamped to the host)
    but is dedicated to the compact line-style controls: it opens from
    the clicked control towards the free space, preferring the space
    *above* the control row, and flips below (nub on top) when there is
    not enough room above. Placement is computed from the actual host
    (the containing tool popup, or the window if none) and the control's
    live geometry, so it never assumes free space.

    QML-free and lightweight: one panel widget is hosted at a time.
    Dismissal follows the tool popup's conventions (outside press, wheel
    elsewhere, Escape, host move/resize); the owning control buttons are
    treated as internal so their click handler owns open/close.
    """

    closed = Signal()

    #: Same metrics as ToolPopup's connector/shell.
    _NUB = 10
    _GAP = 6
    _EDGE = 8
    _SHADOW = 10

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*([self._SHADOW] * 4))
        self._panel: QWidget | None = None
        self._nub_side = "bottom"
        self._nub_pos = 20
        self._triggers: tuple = ()
        self._filter_on = False
        self.setVisible(False)

    # -- lifecycle ----------------------------------------------------

    def is_open(self) -> bool:
        return bool(self.isVisible())

    def show_panel(self, trigger: QWidget, host: QWidget,
                   panel: QWidget, triggers=()) -> None:
        """Open ``panel`` next to ``trigger`` inside ``host``.

        Prefers the space above the control row (body above it, nub at
        the body's bottom edge); flips below when the above space does
        not fit, and finally picks the roomier side and clamps to the
        host rect. Nothing here assumes room above the inspector.
        """
        if self._panel is not panel:
            if self._panel is not None:
                self._layout.removeWidget(self._panel)
                self._panel.setVisible(False)
            self._panel = panel
            self._layout.addWidget(panel)
        try:
            panel.setVisible(True)
        except Exception:
            pass
        try:
            host_rect = host.rect()
            top_left = trigger.mapTo(host, QPoint(0, 0))
        except Exception:
            return
        trigger_rect = QRect(top_left, trigger.size())
        hint = panel.sizeHint()
        content_w = max(1, int(hint.width()))
        content_h = max(1, int(hint.height()))
        shadow, nub, gap, edge = (
            self._SHADOW, self._NUB, self._GAP, self._EDGE)
        total_w = content_w + shadow * 2
        total_h = content_h + shadow * 2 + nub
        max_w = max(1, host_rect.width() - edge * 2)
        max_h = max(1, host_rect.height() - edge * 2)
        total_w = min(total_w, max_w)
        total_h = min(total_h, max_h)
        x = trigger_rect.center().x() - total_w // 2
        x = max(edge, min(max(edge, host_rect.width() - total_w - edge), x))
        above_y = trigger_rect.top() - gap - total_h
        below_y = trigger_rect.bottom() + 1 + gap
        fits_above = above_y >= edge
        fits_below = below_y + total_h <= host_rect.height() - edge
        if fits_above:
            side, y = "bottom", above_y
        elif fits_below:
            side, y = "top", below_y
        else:
            # Neither side fits: use the roomier side, then clamp inside
            # the host so the drawer is never clipped by the popup/view.
            room_above = trigger_rect.top() - gap - edge
            room_below = (host_rect.height() - edge
                          - (trigger_rect.bottom() + 1 + gap))
            if room_above >= room_below:
                side, y = "bottom", above_y
            else:
                side, y = "top", below_y
            y = max(edge, min(max(edge, host_rect.height() - total_h - edge),
                              y))
        self._nub_side = side
        # Body inset from the widget edge by the shadow pad, so the nub
        # points exactly at the control's centre.
        self._nub_pos = int(max(
            20, min(content_w - 20,
                    trigger_rect.center().x() - (x + shadow))))
        if side == "bottom":
            margins = (shadow, shadow, shadow, shadow + nub)
        else:
            margins = (shadow, shadow + nub, shadow, shadow)
        try:
            self._layout.setContentsMargins(*margins)
        except Exception:
            pass
        try:
            if self.parentWidget() is not host:
                self.setParent(host)
        except Exception:
            return
        self.move(x, int(y))
        self.setFixedSize(total_w, total_h)
        self._triggers = tuple(t for t in triggers if t is not None)
        self._install_filter()
        self.setVisible(True)
        try:
            self.raise_()
            self.setFocus()
        except Exception:
            pass
        self.update()

    def dismiss(self) -> None:
        """Hide the drawer and tell the owner (single source of truth)."""
        if not self.isVisible():
            return
        self._remove_filter()
        self.setVisible(False)
        try:
            self.closed.emit()
        except Exception:
            pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    # -- dismissal ----------------------------------------------------

    def _install_filter(self) -> None:
        try:
            if not self._filter_on:
                from PySide6.QtWidgets import QApplication

                app = QApplication.instance()
                if app is not None:
                    app.installEventFilter(self)
                    self._filter_on = True
        except Exception:
            pass

    def _remove_filter(self) -> None:
        if not self._filter_on:
            return
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        except Exception:
            pass
        self._filter_on = False

    def _is_internal(self, widget) -> bool:
        if not isinstance(widget, QWidget):
            return False
        try:
            if widget is self or self.isAncestorOf(widget):
                return True
            for trigger in self._triggers:
                if widget is trigger or trigger.isAncestorOf(widget):
                    return True
        except Exception:
            pass
        return False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Dismiss on an outside press/wheel or a host move/resize."""
        try:
            if not self.isVisible():
                return False
            etype = event.type()
            if etype in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
                if not self._is_internal(watched):
                    self.dismiss()
            elif etype in (QEvent.Type.Move, QEvent.Type.Resize):
                if watched is self.parentWidget():
                    self.dismiss()
        except Exception:
            pass
        return False

    def hideEvent(self, event) -> None:  # noqa: N802
        self._remove_filter()
        super().hideEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.dismiss()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    # -- paint (ToolPopup shell language) ----------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            t = self._tokens
            try:
                bg = QColor(t.surface_raised)
                if not bg.isValid():
                    bg = QColor("#22242e")
            except Exception:
                bg = QColor("#22242e")
            try:
                edge = QColor(t.border)
                if not edge.isValid():
                    edge = QColor("#3a3e4d")
            except Exception:
                edge = QColor("#3a3e4d")
            rect = self.rect()
            nub = self._NUB
            pad = self._SHADOW
            side = self._nub_side
            if side == "top":
                body = rect.adjusted(pad, pad + nub - 1, -pad, -pad)
            else:
                body = rect.adjusted(pad, pad, -pad, -(pad + nub - 1))
            painter.setPen(Qt.PenStyle.NoPen)
            for i, alpha in ((6, 40), (4, 48), (2, 56)):
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(
                    body.adjusted(-i, -i + 2, i, i + 2), 14 + i, 14 + i)
            painter.setBrush(bg)
            painter.drawRoundedRect(body, 14, 14)
            pos = max(20, min(body.width() - 20, self._nub_pos))
            half = 7
            painter.setBrush(bg)
            if side == "bottom":
                cx = body.left() + pos
                painter.drawPolygon(QPolygonF([
                    QPointF(cx - half, body.bottom() - 1),
                    QPointF(cx + half, body.bottom() - 1),
                    QPointF(cx, body.bottom() + (nub - 1))]))
            else:
                cx = body.left() + pos
                painter.drawPolygon(QPolygonF([
                    QPointF(cx - half, body.top() + 1),
                    QPointF(cx + half, body.top() + 1),
                    QPointF(cx, body.top() - (nub - 1))]))
            pen = QPen(edge)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(body.adjusted(0, 0, -1, -1), 14, 14)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _StyleDrawerButton(QAbstractButton):
    """Compact style control: caption + live preview + drawer chevron.

    One implementation backs every line-style property: the Line/Arrow
    ``[Start] [Line] [End]`` controls and the single ``[Line]`` control
    used by line-styled shapes. The preview is dynamic (``set_preview``)
    so a pick inside the drawer is reflected immediately without
    rebuilding the widget tree; wash/ring/focus styling mirrors the
    popup's existing ``_VisualOption`` tile language.
    """

    def __init__(self, caption: str, tokens: InkfallTokens,
                 parent=None, width: int = 60, height: int = 32) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._caption = str(caption)
        self._preview = None
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(width, height)
        self.setAccessibleName(f"{self._caption} style")
        self.toggled.connect(lambda _c: self.update())

    def set_preview(self, paint_fn) -> None:
        """Install the paint callable for the current value (or None)."""
        self._preview = paint_fn
        self.update()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            if self.isChecked():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(
                    _tc(self._tokens, "accent_soft", (139, 147, 224, 51)))
                painter.drawRoundedRect(rect, 8, 8)
                pen = QPen(_tc(self._tokens, "accent", (139, 147, 224, 255)))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 7, 7)
            elif self.underMouse():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(
                    _tc(self._tokens, "surface_overlay",
                        (255, 255, 255, 22)))
                painter.drawRoundedRect(rect, 8, 8)
            if self.hasFocus():
                pen = QPen(_tc(self._tokens, "input_focus",
                               (139, 147, 224, 255)))
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            # Caption (preserved labels: Start / Line / End).
            try:
                from app.theme.typography import role_font

                painter.setFont(role_font(self._tokens, "caption"))
            except Exception:
                pass
            painter.setPen(
                _tc(self._tokens, "text_secondary", (150, 156, 170, 255)))
            cap_rect = QRect(rect.left() + 5, rect.top() + 2,
                             max(0, rect.width() - 15), 12)
            painter.drawText(
                cap_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._caption)
            # Live miniature of the current value.
            preview_rect = QRect(rect.left() + 4, rect.top() + 14,
                                 max(0, rect.width() - 15),
                                 max(0, rect.height() - 17))
            if self._preview is not None:
                try:
                    self._preview(painter, preview_rect, self._tokens)
                except Exception:
                    pass
            # Drawer affordance: points toward the opened panel. The
            # options open upward, so a closed control shows an up
            # chevron; an open one points back down at its control.
            pen = QPen(_tc(self._tokens, "text_secondary",
                           (126, 134, 152, 255)))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            cx = rect.right() - 6
            cy = rect.center().y()
            if self.isChecked():
                painter.drawLine(cx - 4, cy - 2, cx, cy + 2)
                painter.drawLine(cx + 4, cy - 2, cx, cy + 2)
            else:
                painter.drawLine(cx - 4, cy + 2, cx, cy - 2)
                painter.drawLine(cx + 4, cy + 2, cx, cy - 2)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


#: Visible names for the style-drawer rows. The preview glyph stays the
#: primary identifier; these labels name the value beside it (and keep
#: near-identical glyphs — solid vs dashed, none vs dot — unambiguous).
_STYLE_OPTION_LABELS = {
    "solid": "Solid",
    "dashed": "Dashed",
    "dotted": "Dotted",
    "none": "None",
    "dot": "Dot",
    "bar": "Bar",
    "arrow": "Arrow",
}


class _LineStyleSection(QWidget):
    """Shared Line/Arrow style system: compact per-property drawers.

    Every property is one compact control carrying its caption and a
    live miniature of the current value: ``[Start] [Line] [End]`` for
    Line/Arrow, a single ``[Line]`` for line-styled shapes. Clicking a
    control opens a floating options drawer *above* it (flipping below
    only when the host has no room above), anchored to that control by
    the shared connector-nub shell; choosing applies immediately and
    collapses the drawer (one drawer open at a time). Emits partial
    field dicts consumed by PropertiesForm._apply.
    """

    changed = Signal(dict)

    #: Compact control metrics: three fit inside the popup content row
    #: (3 * 60 + 2 * 6 = 192 px, same order as the old tile rows).
    _BUTTON_W = 60
    _BUTTON_H = 32
    #: Drawer option rows: one full-width row per value, shared by every
    #: panel so the drawer keeps a single width / row rhythm while the
    #: user switches between Start, Line and End.
    _OPTION_W = 150
    _OPTION_H = 30

    def __init__(self, style: str, start: str, end: str, color: str,
                 tokens: InkfallTokens, parent=None,
                 endings: bool = True) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._color = color
        self._style = style if style in LINE_STYLES else "solid"
        self._start = start if start in LINE_ENDINGS else "none"
        self._end = end if end in LINE_ENDINGS else "none"
        self._with_endings = bool(endings)
        self._style_options: dict[str, _VisualOption] = {}
        self._start_options: dict[str, _VisualOption] = {}
        self._end_options: dict[str, _VisualOption] = {}
        self._buttons: dict[str, _StyleDrawerButton] = {}
        self._panels: dict[str, QWidget] = {}
        self._open_key: str | None = None
        # One floating drawer, parented to the containing tool popup on
        # first open (the ancestor chain only exists once ToolPopup has
        # installed this content).
        self._drawer = _StyleDrawerPopup(tokens)
        self._drawer.closed.connect(self._on_drawer_closed)
        # Legacy attribute kept for external probes: the old header row
        # is gone, the controls now own their captions.
        self._head_label = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        keys = (("start", "line", "end") if self._with_endings
                else ("line",))
        captions = {"start": "Start", "line": "Line", "end": "End"}
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addStretch(1)
        for key in keys:
            button = _StyleDrawerButton(
                captions[key], tokens, self,
                self._BUTTON_W, self._BUTTON_H)
            button.clicked.connect(
                lambda _c=False, k=key: self._toggle_drawer(k))
            self._buttons[key] = button
            row.addWidget(button)
        row.addStretch(1)
        row_wrap = QWidget(self)
        row_wrap.setStyleSheet("background: transparent;")
        row_wrap.setLayout(row)
        root.addWidget(row_wrap)
        # One floating panel per property, built from vertically stacked
        # full-width visual rows (all supported choices, no inline header:
        # the triggering control already names the property). Panels live
        # inside the floating drawer; only the panel of the clicked
        # control is shown, above the control row.
        for key in keys:
            panel = QWidget()
            panel.setStyleSheet("background: transparent;")
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            panel_layout.setSpacing(6)
            if key == "line":
                panel_layout.addWidget(
                    self._options_panel(
                        LINE_STYLES, self._style_options, self._paint_style,
                        self._on_style, "Line style"))
            elif key == "start":
                panel_layout.addWidget(
                    self._options_panel(
                        LINE_ENDINGS, self._start_options,
                        lambda e: self._paint_ending(e, True),
                        self._on_start, "Line start"))
            else:
                panel_layout.addWidget(
                    self._options_panel(
                        LINE_ENDINGS, self._end_options,
                        lambda e: self._paint_ending(e, False),
                        self._on_end, "Line end"))
            panel.setVisible(False)
            self._panels[key] = panel
        self._sync_checks()
        self._refresh_previews()

    # -- live API -----------------------------------------------------

    def is_drawer_open(self) -> bool:
        return bool(self._open_key is not None and self._drawer.is_open())

    def set_values(self, style: str, start: str = "none",
                   end: str = "none") -> None:
        if style in LINE_STYLES:
            self._style = style
        if start in LINE_ENDINGS:
            self._start = start
        if end in LINE_ENDINGS:
            self._end = end
        self._sync_checks()
        self._refresh_previews()

    def set_color(self, color: str) -> None:
        self._color = color
        for option in (list(self._style_options.values())
                       + list(self._start_options.values())
                       + list(self._end_options.values())):
            option.update()
        self._refresh_previews()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        for option in (list(self._style_options.values())
                       + list(self._start_options.values())
                       + list(self._end_options.values())):
            option.update_tokens(tokens)
        for button in self._buttons.values():
            button.update_tokens(tokens)
        self._drawer.update_tokens(tokens)
        self._refresh_previews()

    # -- internals ----------------------------------------------------

    def _options_panel(self, choices, options: dict, paint_fn,
                       on_pick, label_prefix: str) -> QWidget:
        """Vertically stacked, full-width option rows (no title/header).

        One option per row, top-to-bottom, in a single column; every row
        is the same fixed size and every panel uses it, so the drawer
        keeps one width while switching between Start, Line and End.
        ``label_prefix`` is accessibility text only — the triggering
        control already names the property visually.
        """
        wrap = QWidget(self)
        wrap.setStyleSheet("background: transparent;")
        column = QVBoxLayout(wrap)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for choice in choices:
            option = _VisualOption(
                choice, self._option_paint(paint_fn(choice), choice),
                self._tokens, self._OPTION_W, self._OPTION_H, wrap,
                label=f"{label_prefix} {choice}")
            option.clicked.connect(
                lambda _c=False, v=choice: on_pick(v))
            group.addButton(option)
            options[choice] = option
            column.addWidget(option, 0, Qt.AlignmentFlag.AlignHCenter)
        return wrap

    def _option_paint(self, preview, choice):
        """One drawer row: the existing preview on the left, its label beside it.

        The preview stays the primary identifier (the same painter the
        control miniatures use); the text disambiguates options whose
        glyphs are visually close and names the value for accessibility.
        Colours stay in the popup's theme language.
        """

        def paint(painter, rect, tokens) -> None:
            try:
                preview(painter, QRect(rect.left(), rect.top(), 42,
                                       rect.height()), tokens)
                painter.setPen(
                    _tc(tokens, "text_primary", (24, 26, 32, 255)))
                painter.drawText(
                    QRect(rect.left() + 48, rect.top(),
                          max(0, rect.width() - 48), rect.height()),
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                    _STYLE_OPTION_LABELS.get(
                        str(choice),
                        str(choice or "").replace("_", " ").title()))
            except Exception:
                pass
        return paint

    def _drawer_host(self) -> QWidget:
        """Nearest floating parent (the tool popup), else the window.

        Resolved at open time: sections are constructed before
        ``ToolPopup.set_content`` reparents them, so the ancestor chain
        only exists once the control is actually clickable.
        """
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, ToolPopup):
                return parent
            parent = parent.parentWidget()
        return self.window()

    def _toggle_drawer(self, key: str) -> None:
        """Open ``key``'s drawer above its control; any other closes."""
        try:
            if (self._open_key == key and self._drawer.is_open()):
                self._close_drawers()
                return
            button = self._buttons.get(key)
            panel = self._panels.get(key)
            if button is None or panel is None:
                return
            host = self._drawer_host()
            self._open_key = key
            self._sync_button_checks()
            self._drawer.show_panel(
                button, host, panel, tuple(self._buttons.values()))
            if not self._drawer.is_open():
                self._open_key = None
                self._sync_button_checks()
        except Exception:
            self._open_key = None
            try:
                self._drawer.dismiss()
            except Exception:
                pass
            self._sync_button_checks()

    def _on_drawer_closed(self) -> None:
        """The drawer dismissed itself (outside click/Escape): sync."""
        self._open_key = None
        self._sync_button_checks()

    def _close_drawers(self) -> None:
        try:
            self._open_key = None
            self._drawer.dismiss()
            self._sync_button_checks()
        except Exception:
            pass

    def _sync_button_checks(self) -> None:
        for key, button in self._buttons.items():
            try:
                button.blockSignals(True)
                button.setChecked(key == self._open_key)
            except Exception:
                pass
            finally:
                try:
                    button.blockSignals(False)
                except Exception:
                    pass
            button.update()

    def _refresh_previews(self) -> None:
        """Re-bind each control's miniature to the current values."""
        previews = {
            # The Line control carries the complete result (pattern plus
            # both decorations); Start/End focus on their own marker.
            "line": self._paint_line_control(),
            "start": self._paint_ending(self._start, True),
            "end": self._paint_ending(self._end, False),
        }
        for key, button in self._buttons.items():
            try:
                button.set_preview(previews.get(key))
            except Exception:
                pass

    def _on_style(self, style: str) -> None:
        self._style = style
        self._sync_checks()
        self._refresh_previews()
        self._close_drawers()
        self.changed.emit({"line_style": style})

    def _on_start(self, ending: str) -> None:
        self._start = ending
        self._sync_checks()
        self._refresh_previews()
        self._close_drawers()
        self.changed.emit({"start_ending": ending})

    def _on_end(self, ending: str) -> None:
        self._end = ending
        self._sync_checks()
        self._refresh_previews()
        self._close_drawers()
        self.changed.emit({"end_ending": ending})

    def _sync_checks(self) -> None:
        for value, options in ((self._style, self._style_options),
                              (self._start, self._start_options),
                              (self._end, self._end_options)):
            for choice, option in options.items():
                try:
                    option.blockSignals(True)
                    option.setChecked(choice == value)
                except Exception:
                    pass
                finally:
                    try:
                        option.blockSignals(False)
                    except Exception:
                        pass

    # -- paint --------------------------------------------------------

    def _base_color(self) -> QColor:
        """Stroke sample: slot color when visible, contrast-safe otherwise.

        Line/ending samples must never blend into the popup background,
        so the color is run through the shared contrast treatment against
        the popup surface (dark popup -> light sample, light -> dark).
        """
        try:
            base = QColor(self._color)
            if not base.isValid():
                base = QColor("#000000")
        except Exception:
            base = QColor("#000000")
        try:
            from app.icons import ensure_contrast_fg

            bg = str(self._tokens.surface_raised)
            return QColor(ensure_contrast_fg(base.name(), bg))
        except Exception:
            return base

    def _paint_style(self, style: str):
        def paint(painter: QPainter, rect: QRect,
                  tokens: InkfallTokens) -> None:
            try:
                painter.setPen(_style_pen(self._base_color(), style, 3.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(rect.left() + 4, rect.center().y(),
                                 rect.right() - 4, rect.center().y())
            except Exception:
                pass
        return paint

    def _paint_line_control(self):
        """Compact preview of the complete result: both decorations + line."""
        def paint(painter: QPainter, rect: QRect,
                  tokens: InkfallTokens) -> None:
            try:
                base = self._base_color()
                cy = rect.center().y()
                painter.setPen(_style_pen(base, self._style, 2.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(rect.left() + 8, cy, rect.right() - 8, cy)
                _paint_ending_marker(painter, self._start, True,
                                     rect.left() + 6, cy, base, size=5)
                _paint_ending_marker(painter, self._end, False,
                                     rect.right() - 6, cy, base, size=5)
            except Exception:
                pass
        return paint

    def _paint_ending(self, ending: str, start_side: bool):
        def paint(painter: QPainter, rect: QRect,
                  tokens: InkfallTokens) -> None:
            try:
                base = self._base_color()
                painter.setPen(_style_pen(base, "solid", 2.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                cy = rect.center().y()
                if start_side:
                    painter.drawLine(rect.left() + 12, cy, rect.right() - 4, cy)
                    _paint_ending_marker(painter, ending, True,
                                         rect.left() + 10, cy, base)
                else:
                    painter.drawLine(rect.left() + 4, cy, rect.right() - 12, cy)
                    _paint_ending_marker(painter, ending, False,
                                         rect.right() - 10, cy, base)
            except Exception:
                pass
        return paint


class _FillSection(QWidget):
    """Shape fill: shared preset strip (with No Fill slot) + opacity bars.

    The fill palette is the same preset-strip implementation as the
    stroke picker (same store, same add/remove/drag/custom workflows),
    bound to the fill target: picking a swatch enables fill with that
    color, the permanent first slot (No Fill) disables it. The slot lives
    inside the palette (never a separate toggle outside it), and is not
    part of the persisted color list, so it cannot be removed or
    reordered. Stroke palettes never receive it. Emits partial field
    dicts.
    """

    changed = Signal(dict)

    def __init__(self, enabled: bool, color: str, opacity: float,
                 tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._enabled = bool(enabled)
        self._color = color
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self._palette = _PresetStrip(color, tokens, self, target="fill")
        self._palette.changed.connect(
            lambda c: self._emit_enabled(True, str(c)))
        # M7.1.4.3: No Fill is a permanent item INSIDE the fill palette.
        self._palette.noFillSelected.connect(
            lambda: self._emit_enabled(False, None))
        root.addWidget(self._palette)
        self._opacity = _OpacitySection(color, opacity, tokens, self)
        self._opacity.changed.connect(
            lambda v: self.changed.emit({"fill_opacity": float(v)}))
        root.addWidget(self._opacity)
        self._sync_checks()

    # -- live API -----------------------------------------------------

    def set_values(self, enabled: bool, color: str,
                   opacity: float) -> None:
        self._enabled = bool(enabled)
        self._color = color
        try:
            self._opacity.blockSignals(True)
            self._opacity.set_color(color)
            self._opacity.set_value(float(opacity))
        except Exception:
            pass
        finally:
            try:
                self._opacity.blockSignals(False)
            except Exception:
                pass
        # _sync_checks owns BOTH palette states: the selected color and the
        # permanent No Fill slot. Calling set_color afterwards would clear
        # the No Fill selection again (sync_selected), so it must stay the
        # single state owner here.
        self._sync_checks()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            self._palette.update_tokens(tokens)
        except Exception:
            pass
        self._opacity.update_tokens(tokens)

    def refresh_presets(self) -> None:
        """Sync the fill palette with the shared preset store."""
        try:
            self._palette.refresh_presets()
        except Exception:
            pass

    def teardown_qml(self) -> None:
        """Cascade to the fill palette strip + opacity drawer."""
        for child in (getattr(self, "_palette", None),
                      getattr(self, "_opacity", None)):
            try:
                if child is not None and hasattr(child, "teardown_qml"):
                    child.teardown_qml()
            except Exception:
                pass

    # -- internals ----------------------------------------------------

    def _sync_checks(self) -> None:
        # Fill swatch + permanent No Fill slot: the strip owns both
        # checked states, derived from the live fill fields (never a
        # second source of truth).
        try:
            self._palette.set_color(self._color)
        except Exception:
            pass
        try:
            self._palette.set_fill_state(self._enabled)
        except Exception:
            pass

    def _emit_enabled(self, enabled: bool, color: str | None) -> None:
        payload: dict = {"fill_enabled": bool(enabled)}
        if color is not None:
            payload["fill_color"] = color
        self._enabled = bool(enabled)
        if color is not None:
            self._color = color
        self._sync_checks()
        self.changed.emit(payload)


class _ModeSection(QWidget):
    """Compact two-option segmented selector (M6.6 Eraser mode).

    One option per mode, side by side, drawn with the shared
    ``_VisualOption`` tile language so the selected state reads exactly
    like the rest of the property inspector (accent ring on the active
    option). Emits the chosen mode string; the form persists it on the
    tool slot.
    """

    changed = Signal(str)

    #: Icon per eraser mode (M7.1.4.3). The glyph states the mode directly:
    #: Stroke = solid continuous line, Partial = dashed line. Labels stay
    #: next to the icon (never icon-only).
    _MODE_ICONS = {
        "partial": "line-dashed",
        "whole": "line-solid",
    }

    def __init__(self, modes, labels, value, tokens: InkfallTokens,
                 parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._labels = dict(labels)
        self._options: dict[str, _VisualOption] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        for mode in modes:
            label = str(self._labels.get(mode, mode))
            option = _VisualOption(
                mode, self._paint_label(label, mode), tokens, 150, 34, self,
                label=label)
            option.clicked.connect(
                lambda _checked=False, m=mode: self._on_pick(m))
            self._group.addButton(option)
            self._options[mode] = option
            root.addWidget(option, stretch=1)
        self.set_value(value)

    def value(self) -> str:
        for mode, option in self._options.items():
            try:
                if option.isChecked():
                    return mode
            except Exception:
                continue
        return ""

    def set_value(self, value) -> None:
        mode = normalize_eraser_mode(value)
        for key, option in self._options.items():
            try:
                option.blockSignals(True)
                option.setChecked(key == mode)
            except Exception:
                pass
            finally:
                try:
                    option.blockSignals(False)
                except Exception:
                    pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        for option in self._options.values():
            try:
                option.update_tokens(tokens)
            except Exception:
                pass

    def _on_pick(self, mode: str) -> None:
        # The exclusive QButtonGroup already unchecked the sibling.
        self.changed.emit(mode)

    def _paint_label(self, text: str, mode: str = ""):
        def paint(painter: QPainter, rect: QRect,
                  tokens: InkfallTokens) -> None:
            try:
                # M7.1.4.3: one meaningful glyph + the preserved label,
                # centered as a single group so both modes read at a
                # glance (solid line = Stroke, dashed = Partial).
                color = _tc(tokens, "text_primary", (24, 26, 32, 255))
                painter.setPen(color)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                metrics = painter.fontMetrics()
                icon_name = self._MODE_ICONS.get(str(mode))
                icon_size = 14 if icon_name else 0
                gap = 5 if icon_name else 0
                text_w = metrics.horizontalAdvance(text)
                left = rect.center().x() - (icon_size + gap + text_w) // 2
                if icon_name:
                    pix = _icon_pixmap(icon_name, color.name(), icon_size)
                    if not pix.isNull():
                        painter.drawPixmap(
                            left,
                            int(round(rect.center().y() - icon_size / 2.0)),
                            pix)
                text_rect = QRect(left + icon_size + gap, rect.top(),
                                  text_w + 2, rect.height())
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text)
            except Exception:
                pass

        return paint


class _ConstraintToggle(QWidget):
    """Visual boolean drawing-assist toggle (one field, one tile).

    Modes: square/circle constraints, snap_angle ("Snap to 15°"),
    straight_line ("Ink to Line"), pressure ("Pressure"), and the
    connect_* flags used inside the Connect Lines group. Emits
    ``{field: active}`` for PropertiesForm._apply. Gesture behavior
    itself lands with the future annotation engine; this control owns
    configuration + persistence.
    """

    changed = Signal(dict)

    _LABELS = {
        "square": "Square",
        "circle": "Circle",
        "snap_angle": "Snap to 15°",
        "straight_line": "Ink to Line",
        "pressure": "Pressure",
        # M6.7.1: a single user-facing Connect Lines setting. The legacy
        # endpoint/intersection labels remain only so stored/unknown
        # modes still render a sensible glyph.
        "connect_lines": "Connect Lines",
        "connect_endpoints": "Connect Lines",
        "connect_intersections": "Connect Lines",
        "close_path": "Close shape",
    }

    def __init__(self, mode: str, active: bool, tokens: InkfallTokens,
                 parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._mode = mode  # square | circle | snap_angle | straight_line
        # connect_endpoints | connect_intersections | close_path
        self._field = mode
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self._LABELS.get(mode, mode), self)
        label.setObjectName("tool-popup-meta")
        label.setStyleSheet("background: transparent;")
        self._label = label
        root.addWidget(label)
        root.addStretch(1)
        option = _VisualOption(
            mode, self._paint_shape, tokens, 56, 36, self,
            label=f"{self._LABELS.get(mode, mode)} toggle")
        option.setCheckable(True)
        option.setChecked(bool(active))
        option.clicked.connect(self._on_toggle)
        self._option = option
        root.addWidget(option)

    def set_active(self, active: bool) -> None:
        try:
            self._option.blockSignals(True)
            self._option.setChecked(bool(active))
        except Exception:
            pass
        finally:
            try:
                self._option.blockSignals(False)
            except Exception:
                pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            from app.theme.typography import role_font

            self._label.setFont(role_font(tokens, "meta"))
            self._label.setStyleSheet(
                f"color: {tokens.text_secondary}; background: transparent;")
        except Exception:
            pass
        self._option.update_tokens(tokens)

    def _on_toggle(self) -> None:
        try:
            active = bool(self._option.isChecked())
        except Exception:
            active = False
        self.changed.emit({self._field: active})

    def _paint_shape(self, painter: QPainter, rect: QRect,
                      tokens: InkfallTokens) -> None:
        try:
            pen = QPen(_tc(tokens, "text_secondary", (126, 134, 152, 255)))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            cx, cy = rect.center().x(), rect.center().y()
            mode = self._mode
            if mode == "square":
                side = min(rect.width(), rect.height()) - 6
                x = rect.center().x() - side // 2
                y = rect.center().y() - side // 2
                painter.drawRoundedRect(x, y, side, side, 3, 3)
            elif mode == "snap_angle":
                # 15° wedge: horizontal ray + tilted ray + arc.
                import math as _math

                r = min(rect.width(), rect.height()) // 2 - 4
                painter.drawLine(cx - r, cy, cx + r, cy)
                a = _math.radians(15.0)
                painter.drawLine(cx, cy,
                                 int(round(cx + r * _math.cos(a))),
                                 int(round(cy - r * _math.sin(a))))
                painter.drawArc(cx - 8, cy - 8, 16, 16,
                                0, int(round(15.0 * 16)))
            elif mode == "straight_line":
                # Squiggle resolving into a straight segment + arrowhead.
                painter.drawLine(cx - 14, cy + 6, cx - 6, cy + 6)
                painter.drawLine(cx - 6, cy + 6, cx - 3, cy + 2)
                painter.drawLine(cx - 3, cy + 2, cx, cy + 6)
                painter.drawLine(cx, cy + 6, cx + 5, cy + 6)
                painter.drawLine(cx - 14, cy - 6, cx + 10, cy - 6)
                painter.drawLine(cx + 10, cy - 6, cx + 5, cy - 9)
                painter.drawLine(cx + 10, cy - 6, cx + 5, cy - 3)
            elif mode == "pressure":
                # Tapered stroke: thin nib widening to a full nib.
                painter.drawLine(cx - 13, cy, cx + 11, cy)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(tokens, "text_secondary",
                                      (126, 134, 152, 255)))
                painter.drawEllipse(cx - 13, cy - 1, 3, 3)
                painter.drawEllipse(cx - 5, cy - 2, 5, 5)
                painter.drawEllipse(cx + 4, cy - 4, 8, 8)
            elif mode == "connect_lines":
                # Combined glyph: a segment snapping to a highlighted
                # endpoint plus a crossed pair (endpoint + intersection).
                painter.drawLine(cx - 14, cy + 8, cx - 4, cy + 1)
                painter.drawLine(cx + 14, cy - 6, cx + 2, cy + 1)
                painter.drawLine(cx - 3, cy - 9, cx + 6, cy + 3)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(tokens, "text_secondary",
                                      (126, 134, 152, 255)))
                painter.drawEllipse(cx - 2, cy - 2, 6, 6)
            elif mode == "connect_endpoints":
                painter.drawLine(cx - 13, cy + 7, cx - 2, cy - 1)
                painter.drawLine(cx + 13, cy + 7, cx + 2, cy - 1)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(tokens, "text_secondary",
                                      (126, 134, 152, 255)))
                painter.drawEllipse(cx - 3, cy - 4, 6, 6)
            elif mode == "connect_intersections":
                painter.drawLine(cx - 11, cy - 8, cx + 11, cy + 8)
                painter.drawLine(cx - 11, cy + 8, cx + 11, cy - 8)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(tokens, "text_secondary",
                                      (126, 134, 152, 255)))
                painter.drawEllipse(cx - 3, cy - 3, 6, 6)
            elif mode == "close_path":
                painter.drawLine(cx - 11, cy + 8, cx - 11, cy - 6)
                painter.drawLine(cx - 11, cy - 6, cx + 11, cy - 6)
                painter.drawLine(cx + 11, cy - 6, cx + 11, cy + 8)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_tc(tokens, "text_secondary",
                                      (126, 134, 152, 255)))
                painter.drawEllipse(cx - 3, cy + 5, 6, 6)
            else:  # circle and unknown modes: ellipse outline
                side = min(rect.width(), rect.height()) - 6
                x = rect.center().x() - side // 2
                y = rect.center().y() - side // 2
                painter.drawEllipse(x, y, side, side)
        except Exception:
            pass


class _ConnectSection(QWidget):
    """The single Connect Lines control (M6.7.2).

    Exactly ONE user-facing ``connect_lines`` toggle. Enabling it turns
    on the complete supported auto-connect behavior (endpoint and
    intersection snapping, resolved by the handler from the single
    setting); it is never split into separate controls. It is shown only
    for the tools that expose ``connect_lines`` — never for Polygon,
    which is always-on internally. The toggle is a real _ConstraintToggle
    emitting its own field dict; this group forwards it unchanged.
    """

    changed = Signal(dict)

    def __init__(self, connect: bool, tokens: InkfallTokens,
                 parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._toggles: list[_ConstraintToggle] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        toggle = _ConstraintToggle("connect_lines", bool(connect), tokens,
                                   self)
        toggle.changed.connect(self.changed.emit)
        self._toggles.append(toggle)
        root.addWidget(toggle)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        for toggle in self._toggles:
            try:
                toggle.update_tokens(tokens)
            except Exception:
                pass


class _TrashButton(QAbstractButton):
    """Header trash action: remove this tool instance from the toolbar.

    Painted like the rest of the inspector chrome (circular wash, theme
    ink) with the theme's danger tint on hover/press. Core tools disable
    it — the library definition is never deleted.
    """

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Inkfall has no application tooltips (accessibility is carried by
        # the accessible name/description, which assistive tools read).
        self.setAccessibleName("Remove from toolbar")
        self.setAccessibleDescription(
            "Remove this tool instance from the toolbar")
        self.setObjectName("tool-properties-trash")

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

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

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if not self.isEnabled():
                painter.setOpacity(0.38)
            rect = self.rect()
            cx = rect.left() + rect.width() / 2.0
            cy = rect.top() + rect.height() / 2.0
            hover = self.underMouse() and self.isEnabled()
            pressed = self.isDown()
            if hover or pressed:
                wash = _tc(self._tokens, "danger", (217, 122, 118, 255))
                wash.setAlpha(54 if pressed else 30)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(wash)
                painter.drawEllipse(QRectF(cx - 11.5, cy - 11.5, 23, 23))
            if self.hasFocus():
                focus = _tc(self._tokens, "input_focus",
                            (139, 147, 224, 255))
                pen = QPen(focus)
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QRectF(cx - 11.5, cy - 11.5, 23, 23))
            color = (_tc(self._tokens, "danger", (217, 122, 118, 255))
                     if (hover or pressed)
                     else _tc(self._tokens, "text_muted",
                              (126, 134, 152, 255)))
            try:
                glyph = _icon_pixmap("trash", color.name(), 16)
            except Exception:
                glyph = QPixmap()
            if not glyph.isNull():
                try:
                    _dx, _dy = _ink_offset(glyph)
                except Exception:
                    _dx, _dy = (0.0, 0.0)
                painter.drawPixmap(
                    int(round(cx - glyph.width() / 2.0 + _dx)),
                    int(round(cy - glyph.height() / 2.0 + _dy)), glyph)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class PropertiesForm(QWidget):
    """Live property inspector; every edit applies immediately (no drafts)."""

    changed = Signal(object)
    #: Remove the current toolbar instance (never the library definition).
    removeRequested = Signal(object)

    def __init__(self, slot: ToolInstance, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._slot = slot
        self._tokens = tokens
        self._props = editable_properties(slot.kind)
        self._color_section: _ColorSection | None = None
        self._size_section: _SizeSection | None = None
        self._opacity_section: _OpacitySection | None = None
        self._fill_section: _FillSection | None = None
        self._line_section: _LineStyleSection | None = None
        self._constraint: _ConstraintToggle | None = None
        self._snap: _ConstraintToggle | None = None
        self._straight: _ConstraintToggle | None = None
        self._pressure: _ConstraintToggle | None = None
        self._connect: _ConnectSection | None = None
        self._mode_section: _ModeSection | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(4)
        # Substantial inspector width (content 340 + shadow pad ~= 360
        # total): wider and less cramped, still a compact panel.
        self.setMinimumWidth(340)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = QLabel(slot.name)
        title.setObjectName("tool-popup-title")
        top.addWidget(title, stretch=1)
        # Header trash: removes THIS instance from the toolbar. Core
        # tools stay permanent (disabled, explained by the accessible
        # description — Inkfall ships no application tooltips).
        trash = _TrashButton(tokens, self)
        removable = True
        try:
            removable = not is_core_tool(slot)
        except Exception:
            removable = True
        trash.setEnabled(removable)
        if not removable:
            trash.setAccessibleDescription(
                "Core tools cannot be removed from the toolbar")
        trash.clicked.connect(self._on_remove_clicked)
        top.addWidget(trash)
        top_wrap = QWidget(self)
        top_wrap.setStyleSheet("background: transparent;")
        top_wrap.setLayout(top)
        root.addWidget(top_wrap)
        self._title = title
        self._trash = trash
        self._preview = _Preview(self)
        root.addWidget(self._preview)
        if "color" in self._props:
            root.addWidget(self._section("Color"))
            section = _ColorSection(slot.color, tokens, self)
            section.changed.connect(lambda c: self._apply(color=c))
            self._color_section = section
            root.addWidget(section)
        if "eraser_mode" in self._props:
            # M6.6: two-option mode selector at the top of the Eraser
            # inspector (Whole Stroke = the compatible default).
            root.addWidget(self._section("Mode"))
            section = _ModeSection(ERASER_MODES, ERASER_MODE_LABELS,
                                   slot.eraser_mode, tokens, self)
            section.changed.connect(
                lambda mode: self._apply(eraser_mode=mode))
            self._mode_section = section
            root.addWidget(section)
        if "width_mm" in self._props:
            root.addWidget(self._section(
                "Size" if slot.kind in (ToolType.PEN, ToolType.HIGHLIGHTER,
                                       ToolType.ERASER) else "Thickness"))
            section = _SizeSection(slot.kind, float(slot.width_mm),
                                   slot.color, tokens, self)
            section.changed.connect(lambda v: self._apply(width_mm=float(v)))
            self._size_section = section
            root.addWidget(section)
        if "opacity" in self._props:
            root.addWidget(self._section("Opacity"))
            section = _OpacitySection(slot.color, float(slot.opacity),
                                      tokens, self)
            section.changed.connect(lambda v: self._apply(opacity=float(v)))
            self._opacity_section = section
            root.addWidget(section)
        if slot.kind in (ToolType.PEN, ToolType.HIGHLIGHTER):
            # Per-tool pressure toggle (B.2.2): persisted on the slot,
            # latched into strokes at press time. Highlighter stores
            # the flag but has no inking path yet, so it stays inert
            # there until the highlighter milestone.
            section = _ConstraintToggle("pressure", bool(slot.pressure),
                                        tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._pressure = section
            root.addWidget(section)
        if "line_style" in self._props:
            is_line = slot.kind in (ToolType.LINE, ToolType.ARROW)
            # M7.1.4.3: no separate section header — the compact controls
            # carry their own captions. Line/Arrow get [Start][Line][End];
            # line-styled shapes get the single [Line] control.
            section = _LineStyleSection(
                slot.line_style, slot.start_ending, slot.end_ending,
                slot.color, tokens, self, endings=is_line)
            section.changed.connect(lambda d: self._apply(**d))
            self._line_section = section
            root.addWidget(section)
        if "fill_enabled" in self._props:
            root.addWidget(self._section("Fill"))
            section = _FillSection(bool(slot.fill_enabled), slot.fill_color,
                                   float(slot.fill_opacity), tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._fill_section = section
            root.addWidget(section)
        # Stroke/fill strips share one preset store: a mutation on either
        # side refreshes the other (no stale sibling palettes).
        try:
            if (self._color_section is not None
                    and self._fill_section is not None):
                self._color_section.presetsChanged.connect(
                    self._fill_section.refresh_presets)
                self._fill_section._palette.presetsChanged.connect(
                    self._color_section.refresh_presets)
        except Exception:
            pass
        if "square" in self._props:
            section = _ConstraintToggle("square", bool(slot.square),
                                        tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._constraint = section
            root.addWidget(section)
        elif "circle" in self._props:
            section = _ConstraintToggle("circle", bool(slot.circle),
                                        tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._constraint = section
            root.addWidget(section)
        if "snap_angle" in self._props:
            section = _ConstraintToggle("snap_angle", bool(slot.snap_angle),
                                        tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._snap = section
            root.addWidget(section)
        if "straight_line" in self._props:
            section = _ConstraintToggle("straight_line",
                                        bool(slot.straight_line),
                                        tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._straight = section
            root.addWidget(section)
        if "connect_lines" in self._props:
            # M6.7.2: one Connect Lines control, no redundant section
            # heading (the toggle already carries the "Connect Lines"
            # label). Polygon no longer exposes this at all.
            section = _ConnectSection(bool(slot.connect_lines), tokens, self)
            section.changed.connect(lambda d: self._apply(**d))
            self._connect = section
            root.addWidget(section)
        # No per-tool smoothing row is instantiated (the slider helpers
        # below are currently unused); pressure has its own toggle.
        self._smooth = None
        self._preview.set_slot(slot)
        self.update_tokens(tokens)

    # -- live apply -------------------------------------------------------

    def _on_remove_clicked(self) -> None:
        """Remove the current toolbar instance (not the library tool)."""
        try:
            if is_core_tool(self._slot):
                return
        except Exception:
            pass
        try:
            self.removeRequested.emit(self._slot)
        except Exception:
            pass

    def _apply(self, **fields) -> None:
        """Apply one edit immediately: instance + preview + notify."""
        try:
            updated = dataclasses.replace(self._slot, **fields)
        except (TypeError, ValueError):
            return
        self._slot = updated
        self._preview.set_slot(updated)
        if "color" in fields:
            for section in (self._size_section, self._opacity_section):
                if section is not None:
                    try:
                        section.set_color(updated.color)
                    except Exception:
                        pass
            if self._line_section is not None:
                try:
                    self._line_section.set_color(updated.color)
                except Exception:
                    pass
        if "fill_color" in fields and self._fill_section is not None:
            try:
                self._fill_section.set_values(
                    updated.fill_enabled, updated.fill_color,
                    updated.fill_opacity)
            except Exception:
                pass
        self.changed.emit(updated)

    # -- widgets --------------------------------------------------------

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("tool-popup-section")
        return label

    def _add_slider_row(self, root, label: str, minimum: int, maximum: int,
                        value: int, fmt):
        root.addWidget(self._section(label))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal, self)
        slider.setRange(minimum, maximum)
        slider.setValue(max(minimum, min(maximum, value)))
        readout = QLabel(self)
        readout.setMinimumWidth(64)
        readout.setAlignment(Qt.AlignmentFlag.AlignRight)
        readout.setText(fmt(slider.value()))
        slider.valueChanged.connect(self._on_smooth_moved)
        row.addWidget(slider, stretch=1)
        row.addWidget(readout)
        wrap = QWidget(self)
        wrap.setLayout(row)
        root.addWidget(wrap)
        return slider, readout, fmt

    def _on_smooth_moved(self, value: int) -> None:
        try:
            slider, readout, fmt = self._smooth
            readout.setText(fmt(value))
        except Exception:
            pass
        self._apply(smoothing=max(0.0, min(1.0, int(value) / 100.0)))

    # -- theming ----------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            from app.theme.typography import role_font

            title_font = role_font(tokens, "section")
            meta_font = role_font(tokens, "meta")
        except Exception:
            title_font = self.font()
            meta_font = self.font()
        t = tokens
        try:
            self._title.setFont(title_font)
            self._title.setStyleSheet(
                f"color: {t.text_primary}; background: transparent;")
        except Exception:
            pass
        try:
            self._trash.update_tokens(tokens)
        except Exception:
            pass
        for child in self.findChildren(QLabel):
            if child is self._title:
                continue
            try:
                if child.objectName() in ("tool-popup-section", "tool-popup-meta"):
                    child.setFont(meta_font)
                child.setStyleSheet(
                    f"color: {t.text_secondary}; background: transparent;")
            except Exception:
                pass
        try:
            self._preview.setStyleSheet(
                f"background-color: {t.input_background};"
                f" border: 1px solid {t.input_border}; border-radius: 8px;")
        except Exception:
            pass
        if getattr(self, "_smooth", None) is not None:
            try:
                slider, readout, _fmt = self._smooth
                slider.setStyleSheet(
                    f"QSlider::groove:horizontal {{ height: 4px;"
                    f" background-color: {t.input_background}; border-radius: 2px; }}"
                    f"QSlider::handle:horizontal {{ width: 16px; height: 16px;"
                    f" margin: -6px 0; border-radius: 8px;"
                    f" background-color: {t.accent};"
                    f" border: 1px solid {t.input_border}; }}"
                    f"QSlider::sub-page:horizontal {{ background-color: {t.accent};"
                    f" border-radius: 2px; }}")
                readout.setFont(meta_font)
                readout.setStyleSheet(f"color: {t.text_secondary};")
            except Exception:
                pass
        for section in (self._color_section, self._size_section,
                        self._opacity_section, self._fill_section,
                        self._line_section, self._constraint,
                        self._snap, self._straight, self._pressure,
                        self._connect, self._mode_section):
            if section is not None:
                try:
                    section.update_tokens(tokens)
                except Exception:
                    pass

    def teardown_qml(self) -> None:
        """Destroy QML components synchronously while bridges are alive.

        Called on close (before hide/delete): blanking the QML sources
        destroys bindings first, so engine teardown can never evaluate
        them against already-deleted bridges (the null-TypeError class).
        """
        for section in (self._color_section, self._size_section,
                        self._opacity_section, self._fill_section):
            try:
                if section is not None and hasattr(section,
                                                   "teardown_qml"):
                    section.teardown_qml()
            except Exception:
                pass

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.teardown_qml()
        except Exception:
            pass
        try:
            super().closeEvent(event)
        except Exception:
            pass


class AddToolGrid(QWidget):
    """Tabbed tool library: Freehand | Shapes | Other + scrolling cards."""

    chosen = Signal(object)

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        # Library width: substantial but bounded (content 356..376 +
        # shadow pad ~= 376..406 total, inside the 360..420 band).
        # Category tabs always fit; cards scroll vertically, never clip.
        self.setMinimumWidth(356)
        self.setMaximumWidth(376)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        title = QLabel("Tools Library")
        title.setStyleSheet("background: transparent;")
        self._title = title
        root.addWidget(title)
        sub = QLabel("Choose a tool to add to the toolbar")
        sub.setObjectName("tool-popup-meta")
        sub.setStyleSheet("background: transparent;")
        self._sub = sub
        root.addWidget(sub)
        try:
            from app.toolshelf.store import ADDABLE_GROUPS

            groups = ADDABLE_GROUPS
        except Exception:
            groups = (("Tools", tuple(ADDABLE_KINDS)),)
        # Category tab bar (compact, exclusive, animated content swap).
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(4)
        tab_row.addStretch(1)
        from PySide6.QtWidgets import QButtonGroup as _QBG

        tab_group = _QBG(self)
        tab_group.setExclusive(True)
        self._tab_group = tab_group
        self._tabs: list[QPushButton] = []
        self._tab_names: list[str] = []
        for group_name, _kinds in groups:
            tab = QPushButton(group_name, self)
            tab.setCheckable(True)
            tab.setCursor(Qt.CursorShape.ArrowCursor)
            tab.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            tab.setFlat(True)
            tab.setAccessibleName(f"{group_name} tools")
            tab_group.addButton(tab)
            self._tabs.append(tab)
            self._tab_names.append(group_name)
            tab_row.addWidget(tab)
            tab.toggled.connect(lambda _c=False: self._sync_tab_styles())
            # Arrow-key category navigation without any dismissal:
            # internal focus/navigation is never an outside click.
            tab.installEventFilter(self)
        tab_row.addStretch(1)
        tab_wrap = QWidget(self)
        tab_wrap.setStyleSheet("background: transparent;")
        tab_wrap.setLayout(tab_row)
        root.addWidget(tab_wrap)
        # Scrollable card viewport (vertical scroll when needed).
        from PySide6.QtWidgets import QStackedWidget as _QStack

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("background: transparent; border: none;")
        scroll.setMaximumHeight(320)
        self._scroll = scroll
        stack = _QStack(scroll)
        self._stack = stack
        self._pages: list[QWidget] = []
        for index, (group_name, kinds) in enumerate(groups):
            page = QWidget(stack)
            grid = QGridLayout()
            grid.setSpacing(8)
            grid.setContentsMargins(0, 0, 0, 0)
            # One tile grid for every category (3 columns): consistent
            # card size, alignment and spacing regardless of membership.
            columns = 3
            for i, kind in enumerate(kinds):
                assert kind in ADDABLE_KINDS
                label = next((lb for k, lb in ADD_GRID_META if k == kind),
                             kind.value.title())
                cell = _AddCell(kind, label, tokens, page)
                cell.activated.connect(lambda k=kind: self.chosen.emit(k))
                grid.addWidget(cell, i // columns, i % columns)
            # Empty cells collapse; remaining width stretches so the
            # tiles stay left-aligned instead of floating mid-page.
            grid.setColumnStretch(columns, 1)
            page.setLayout(grid)
            page.setStyleSheet("background: transparent;")
            stack.addWidget(page)
            self._pages.append(page)
            self._tabs[index].clicked.connect(
                lambda _c=False, idx=index: self._switch_tab(idx))
        scroll.setWidget(stack)
        root.addWidget(scroll)
        if self._tabs:
            self._tabs[0].setChecked(True)
            stack.setCurrentIndex(0)
        self.update_tokens(tokens)

    def current_tab(self) -> str:
        try:
            return self._tab_names[self._stack.currentIndex()]
        except Exception:
            return ""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Left/Right on a category tab moves between categories."""
        try:
            if watched in self._tabs and event.type() == QEvent.Type.KeyPress:
                key = event.key()
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                    index = self._stack.currentIndex()
                    step = -1 if key == Qt.Key.Key_Left else 1
                    target = (index + step) % max(1, len(self._tabs))
                    self._switch_tab(target)
                    self._tabs[target].setFocus(
                        Qt.FocusReason.TabFocusReason)
                    event.accept()
                    return True
        except Exception:
            pass
        return super().eventFilter(watched, event)

    def _switch_tab(self, index: int) -> None:
        try:
            if index < 0 or index >= len(self._tabs):
                return
            # Keyboard navigation can call this without a click, so the
            # exclusive checked state is synced explicitly.
            self._tabs[index].setChecked(True)
            if index == self._stack.currentIndex():
                return
            self._stack.setCurrentIndex(index)
            page = self._stack.widget(index)
            if page is not None:
                try:
                    _fx.fade_in(page, 120, self)
                except Exception:
                    pass
        except Exception:
            pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            from app.theme.typography import role_font

            self._title.setFont(role_font(tokens, "section"))
            self._title.setStyleSheet(
                f"color: {tokens.text_primary}; background: transparent;")
            self._sub.setFont(role_font(tokens, "meta"))
            self._sub.setStyleSheet(
                f"color: {tokens.text_muted}; background: transparent;")
            for tab in self._tabs:
                tab.setFont(role_font(tokens, "meta"))
                tab.setStyleSheet(
                    f"color: {tokens.text_secondary}; background: transparent;"
                    " border: none; border-bottom: 2px solid transparent;"
                    " padding: 4px 10px;")
        except Exception:
            pass
        self._sync_tab_styles()
        for cell in self.findChildren(_AddCell):
            cell.update_tokens(tokens)

    def _sync_tab_styles(self) -> None:
        # Selected tab carries the accent underline (called on switch).
        try:
            t = self._tokens
            for tab in self._tabs:
                selected = bool(tab.isChecked())
                color = t.accent if selected else t.text_muted
                tab.setStyleSheet(
                    f"color: {color}; background: transparent;"
                    f" border: none; border-bottom: 2px solid"
                    f" {t.accent if selected else 'transparent'};"
                    " padding: 4px 10px;")
        except Exception:
            pass


class _AddCell(QAbstractButton):
    """One library card: monochrome glyph + label, shared hover/focus."""

    activated = Signal(object)

    def __init__(self, kind: ToolType, label: str, tokens: InkfallTokens,
                 parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self._label_text = label
        self._tokens = tokens
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(88, 70)
        self._glyph: QPixmap | None = None
        self.clicked.connect(lambda: self.activated.emit(self._kind))
        self.setAccessibleName(f"Add {label} tool")

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

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._glyph = None
        self.update()

    def _label(self) -> str:
        return self._label_text

    def paintEvent(self, event) -> None:  # noqa: N802
        from app.toolshelf.slot_button import (
            _NAV_ICONS,
            _PEN_ICONS,
            _SHAPE_ICONS,
        )

        t = self._tokens
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Tactile language: 1px lift on hover, 1px compression pressed.
            try:
                dy = 1 if self.isDown() else (
                    -1 if self.underMouse() else 0)
                if dy:
                    painter.translate(0, dy)
            except Exception:
                pass
            rect = self.rect()
            # Card face: transparent at rest; hover/focus/press supply
            # the shared Inkfall wash. No per-tool tint, badge or colored
            # backing anywhere in the library.
            card = rect.adjusted(1, 1, -1, -1)
            active = self.underMouse() or self.hasFocus() or self.isDown()
            if self.isDown():
                face = _tc(t, "surface_overlay", (255, 255, 255, 36))
            elif active:
                face = _tc(t, "surface_overlay", (255, 255, 255, 22))
            else:
                face = None
            if face is not None:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(face)
                painter.drawRoundedRect(card, 9, 9)
            if active:
                pen = QPen(_tc(t, "accent", (139, 147, 224, 255)))
            else:
                edge = _tc(t, "border_subtle", (60, 66, 80, 255))
                edge.setAlpha(170)
                pen = QPen(edge)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(card.adjusted(0, 0, -1, -1), 9, 9)
            if self.hasFocus():
                focus = QPen(_tc(t, "input_focus", (139, 147, 224, 255)))
                focus.setWidth(1)
                focus.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(focus)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(card.adjusted(2, 2, -2, -2), 7, 7)
            # One monochrome glyph treatment for EVERY tool in the
            # library: theme ink, fixed 24px artwork, fixed icon zone,
            # no per-kind colored tile. The card owns hover/focus.
            icon_name = _PEN_ICONS.get(
                self._kind, _SHAPE_ICONS.get(
                    self._kind, _NAV_ICONS.get(self._kind, "pen")))
            glyph_color = _tc(
                t, "text_primary" if active else "text_secondary",
                (126, 134, 152, 255)).name()
            try:
                glyph = _icon_pixmap(icon_name, glyph_color, 24)
            except Exception:
                glyph = QPixmap()
            icon_zone = QRect(card.left() + 18, card.top() + 8, 52, 38)
            if not glyph.isNull():
                try:
                    _dx, _dy = _ink_offset(glyph)
                except Exception:
                    _dx, _dy = (0.0, 0.0)
                # Float zone center (see _Preview): no truncation shift.
                _tcx = icon_zone.left() + icon_zone.width() / 2.0
                _tcy = icon_zone.top() + icon_zone.height() / 2.0
                gx = _tcx - glyph.width() / 2 + _dx
                gy = _tcy - glyph.height() / 2 + _dy
                painter.drawPixmap(int(round(gx)), int(round(gy)), glyph)
            # Add affordance: the same small monochrome plus on every
            # card (brighter on hover/focus); the whole card remains the
            # click target.
            try:
                plus = _icon_pixmap(
                    "plus",
                    _tc(t, "text_secondary" if active else "text_muted",
                        (126, 134, 152, 255)).name(),
                    12)
                if not plus.isNull():
                    painter.drawPixmap(card.right() - 17,
                                       card.top() + 6, plus)
            except Exception:
                pass
            # Short label.
            try:
                from app.theme.typography import role_font

                painter.setFont(role_font(t, "meta"))
            except Exception:
                pass
            painter.setPen(_tc(t, "text_secondary", (126, 134, 152, 255)))
            painter.drawText(card.left(), icon_zone.bottom() + 2,
                             card.width(),
                             card.bottom() - icon_zone.bottom() - 2,
                             Qt.AlignmentFlag.AlignHCenter
                             | Qt.AlignmentFlag.AlignVCenter,
                             self._label_text)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


def stop_widget_anims(widget) -> None:
    """Stop fx-driven animators targeting a widget (teardown safety).

    Fire-and-forget fx helpers parent their animators to the caller,
    so a widget deleted while its show/hide animation still runs gets
    ticked after C++ destruction. Stopping first makes close/teardown
    deterministic.
    """
    try:
        from PySide6.QtCore import QAbstractAnimation

        for anim in widget.findChildren(QAbstractAnimation):
            try:
                anim.stop()
            except (RuntimeError, AttributeError):
                pass
    except Exception:
        pass
    try:
        widget.setGraphicsEffect(None)
    except (RuntimeError, AttributeError):
        pass
    except Exception:
        pass


class ToolPopup(QWidget):
    """Anchored floating popup hosting one content widget at a time.

    Owns its show/hide animations: they are stopped before any hide
    or destruction so a deleted C++ object is never ticked by a
    still-running animator (teardown-safe by construction).
    """

    closed = Signal()

    #: Nub margin (connector zone) on the launcher side.
    _NUB = 10
    #: Preferred placement order: below, above, right, left.
    _GAP = 6
    _EDGE = 8
    #: Soft-shadow padding around the body (painted, part of geometry).
    _SHADOW = 10

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        # Child overlay of the viewer (never a desktop-level window):
        # moves/minimizes with the app, always clipped to host bounds.
        super().__init__(parent, Qt.WindowType.Widget
                         | Qt.WindowType.FramelessWindowHint)
        self._tokens = tokens
        # True rounded silhouette: translucent background, no opaque
        # backing widget, shadow painted manually inside _SHADOW pad
        # (a QGraphicsDropShadowEffect on this widget would force a
        # square backing and the black-corner artifact).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Cursor ownership: this popup is a child overlay of the viewer, so
        # without its own cursor its passive areas would inherit the active
        # drawing tool's pen/highlighter/eraser/lasso cursor. An explicit
        # arrow keeps the popup neutral; the popup's ordinary controls
        # (buttons, swatches, presets, tabs/cards) also set the arrow, while
        # genuine text entry keeps the native I-beam and scrollbars/resize
        # surfaces keep their native cursors.
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(2, 2, 2, 2)
        # Scroll viewport: tall inspectors (Pen) never overflow a short
        # host — the popup stays inside the window and scrolls instead.
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }")
        self._scroll.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._layout.addWidget(self._scroll)
        self._content: QWidget | None = None
        self._anchor: QRect | None = None
        self._last_launcher: QRect | None = None
        self._last_host: QWidget | None = None
        self._last_prefer: bool | None = None
        # Connector state: which edge faces the launcher + offset along it.
        self._nub_side: str | None = None
        self._nub_pos: int = 0
        self.setVisible(False)
        # Close lifecycle (M5-A2.2.6): QML teardown is deferred to the
        # end of the animated close so the palette stays rendered through
        # the slide-out. _close_gen invalidates a pending _finish_close
        # timer when a newer close supersedes it (interrupt path).
        self._qml_torn_down = False
        self._close_gen = 0
        # Hover passthrough (M6.7.2 follow-up): an app-level event filter
        # mirrors button-free moves over this child overlay to the host
        # while the popup is visible, so the interactive tools' live
        # preview is never swallowed. Installed once per showing.
        self._app_hover_filter_on = False
        # Optional toggle control (the selected-tool capsule). A press on
        # it must never dismiss this popup: the control's own click
        # handler decides open/close, so the toggle never fights the
        # outside-click filter or the close animation lifecycle.
        self._toggle_source = None

    def set_toggle_source(self, widget) -> None:
        """Register a control whose press is treated as internal.

        Used by the selected-tool capsule so its press does not dismiss
        the popup it is about to toggle; ``None`` clears it.
        """
        self._toggle_source = widget

    def _apply_arrow_cursor(self) -> None:
        """Re-assert the popup's neutral arrow on its chrome surfaces.

        The popup is a child overlay of the drawing canvas, so a surface
        that never sets a cursor of its own would inherit the active tool
        cursor through the parent chain. This is called after the content
        is installed and again when the popup is shown (a tool switch may
        have changed the canvas cursor meanwhile). Child widgets keep
        their native cursors — text entry still shows the I-beam, and
        scrollbars keep theirs — because a widget's own cursor wins over
        the inherited one.
        """
        arrow = Qt.CursorShape.ArrowCursor
        for widget in (self, self._scroll, self._scroll.viewport(),
                       self._content):
            try:
                if widget is not None:
                    widget.setCursor(arrow)
            except Exception:
                pass

    def set_content(self, content: QWidget) -> None:
        try:
            old = self._scroll.takeWidget()
            if old is not None:
                try:
                    old.setParent(None)
                    old.deleteLater()
                except Exception:
                    pass
        except Exception:
            pass
        self._content = content
        # New content starts a new close lifecycle: any pending animated
        # close belongs to the previous content and must not touch this
        # one when its timer fires (see _finish_close generation check).
        try:
            self._qml_torn_down = False
            self._close_gen = int(getattr(self, "_close_gen", 0)) + 1
        except Exception:
            pass
        try:
            self._scroll.setWidget(content)
        except Exception:
            pass
        self._apply_arrow_cursor()
        try:
            content.update_tokens(self._tokens)
        except Exception:
            pass

    def show_near(self, launcher_global: QRect, host: QWidget,
                  prefer_horizontal: bool | None = None,
                  animate: bool = True) -> QPoint:
        """Place near the launcher, always inside the host; returns origin.

        The popup body never overlaps the launcher: candidates keep a
        ``_GAP`` minimum separation, clamping is re-checked against the
        launcher, and placement prefers the axis across from the shelf
        (horizontal shelf -> below/above first, vertical -> right/left).
        A connector nub on the launcher side keeps the visual relation.
        """
        self._apply_arrow_cursor()
        self._layout.setContentsMargins(2, 2, 2, 2)
        try:
            inner = self._scroll.widget() or self._content
            hint = inner.sizeHint() if inner is not None else self.sizeHint()
        except Exception:
            hint = self.sizeHint()
        cw, ch = max(1, hint.width()), max(1, hint.height())
        # Inspector width: substantial panel (340..400 content), never a
        # cramped strip and never a giant form; host clamping below keeps
        # it inside the window on small hosts. Explicitly bounded content
        # (e.g. the Add library) caps the width from above.
        cw = min(max(cw, 340), 400)
        try:
            maxw = inner.maximumWidth() if inner is not None else 0
            if maxw and maxw < 16000:
                cw = min(cw, max(280, int(maxw)))
        except Exception:
            pass
        # Painted shadow pad is part of the window geometry.
        pad = self._SHADOW
        cw, ch = cw + pad * 2, ch + pad * 2
        host_rect = host.rect()
        try:
            launcher_local = QRect(host.mapFromGlobal(launcher_global.topLeft()),
                                   launcher_global.size())
        except Exception:
            launcher_local = QRect(12, 12, 52, 52)
        nub, gap, edge = self._NUB, self._GAP, self._EDGE
        hw, hh = max(1, host_rect.width()), max(1, host_rect.height())
        cx = launcher_local.center().x()
        cy = launcher_local.center().y()

        def fits(w: int, h: int, x: int, y: int) -> bool:
            return (x >= edge and y >= edge
                    and x + w <= hw - edge and y + h <= hh - edge)

        def clamp_x(w: int, x: int) -> int:
            return max(edge, min(max(edge, hw - w - edge), x))

        def clamp_y(h: int, y: int) -> int:
            return max(edge, min(max(edge, hh - h - edge), y))

        total_w_v, total_h_v = cw, ch + nub
        total_w_h, total_h_h = cw + nub, ch
        # Never larger than the host: tall inspectors scroll inside.
        max_w, max_h = max(1, hw - edge * 2), max(1, hh - edge * 2)
        total_w_v, total_h_v = min(total_w_v, max_w), min(total_h_v, max_h)
        total_w_h, total_h_h = min(total_w_h, max_w), min(total_h_h, max_h)
        bx = clamp_x(total_w_v, cx - total_w_v // 2)
        # QRect bottom/right are inclusive: +1 keeps a true _GAP visual gap.
        by = launcher_local.bottom() + gap + 1
        ax = clamp_x(total_w_v, cx - total_w_v // 2)
        ay = launcher_local.top() - gap - total_h_v
        rx = launcher_local.right() + gap + 1
        ry = clamp_y(total_h_h, cy - total_h_h // 2)
        lx = launcher_local.left() - gap - total_w_h
        ly = clamp_y(total_h_h, cy - total_h_h // 2)

        def clear_of_launcher(w: int, h: int, x: int, y: int) -> bool:
            """True when the popup keeps its gap from the launcher."""
            try:
                keep = launcher_local.adjusted(-4, -4, 4, 4)
                return not QRect(x, y, w, h).intersects(keep)
            except Exception:
                return True

        # Orientation-aware order: across from the shelf first, then the
        # rest; every candidate must fit AND stay clear of the launcher.
        if prefer_horizontal:
            order = (("top", total_w_v, total_h_v, bx, by),
                     ("bottom", total_w_v, total_h_v, ax, ay),
                     ("left", total_w_h, total_h_h, rx, ry),
                     ("right", total_w_h, total_h_h, lx, ly))
        else:
            order = (("left", total_w_h, total_h_h, rx, ry),
                     ("right", total_w_h, total_h_h, lx, ly),
                     ("top", total_w_v, total_h_v, bx, by),
                     ("bottom", total_w_v, total_h_v, ax, ay))
        side = x = y = None  # type: ignore[assignment]
        total_w = total_h = 0
        for cand_side, w, h, cx0, cy0 in order:
            if fits(w, h, cx0, cy0) and clear_of_launcher(w, h, cx0, cy0):
                side, x, y = cand_side, cx0, cy0
                break
        if side is None:
            # Cramped host: below, clamped (never escapes the window);
            # still pushed clear of the launcher when possible.
            side, x, y = "top", bx, clamp_y(total_h_v, by)
            if not clear_of_launcher(total_w_v, total_h_v, x, y):
                y = min(y, launcher_local.top() - gap - total_h_v)
                y = max(edge, y)
        if side in ("top", "bottom"):
            total_w, total_h = total_w_v, total_h_v
            nub_pos = max(20, min(total_w - 20, cx - x))
            margins = ((pad, pad + nub, pad, pad) if side == "top"
                       else (pad, pad, pad, pad + nub))
        else:
            total_w, total_h = total_w_h, total_h_h
            nub_pos = max(20, min(total_h - 20, cy - y))
            margins = ((pad + nub, pad, pad, pad) if side == "left"
                       else (pad, pad, pad + nub, pad))
        self._nub_side = side
        self._nub_pos = int(nub_pos)
        try:
            self._layout.setContentsMargins(*margins)
        except Exception:
            pass
        try:
            self.setParent(host)
        except Exception:
            pass
        self.move(x, y)
        self.resize(total_w, total_h)
        self._last_launcher = (QRect(launcher_global)
                               if launcher_global is not None else None)
        self._last_host = host
        self._last_prefer = prefer_horizontal
        self.setVisible(True)
        try:
            self.raise_()
            if animate:
                self.setFocus()
        except Exception:
            pass
        # M6.7.2 follow-up: never swallow the host's button-free hovers.
        self._enable_hover_passthrough()
        try:
            host.installEventFilter(self)
        except Exception:
            pass
        if animate:
            try:
                _fx.slide_fade_in(self, 0, 160, self, dy_px=-6)
            except Exception:
                pass
        self._anchor = QRect(x, y, total_w, total_h)
        self.update()
        return QPoint(x, y)

    def refit_to_content(self) -> None:
        """Re-run placement for grown/shrunk content (no re-animation).

        Called when a collapsed section (Custom size, color picker)
        expands: the popup resizes to fit instead of only scrolling.
        """
        try:
            if (self._last_launcher is None or self._last_host is None
                    or not self.isVisible()):
                return
            from app.toolshelf.tool_popup import stop_widget_anims

            stop_widget_anims(self)
        except Exception:
            pass
        try:
            self.show_near(self._last_launcher, self._last_host,
                           self._last_prefer, animate=False)
        except Exception:
            pass

    def _enable_hover_passthrough(self) -> None:
        """Mirror button-free canvas hovers across this child overlay.

        The popup is a viewer *child* overlay created after MainWindow's
        construction-time ``findChildren(QWidget)`` mouse-tracking sweep,
        so neither it nor its subtree receive button-free move events.
        Those moves are delivered to the widget under the cursor, so an
        open popup silently swallows them — killing the Polygon live
        preview over its rect (the Line is unaffected: its button-held
        drag always reaches the viewer via Qt's implicit grab). Track the
        subtree and install one app-level filter that forwards those
        moves to the host, so the preview keeps following the pointer.
        Clicks, sliders and scrolling are untouched (never consumed).
        """
        try:
            for w in [self, *self.findChildren(QWidget)]:
                try:
                    w.setMouseTracking(True)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if not self._app_hover_filter_on:
                from PySide6.QtWidgets import QApplication

                app = QApplication.instance()
                if app is not None:
                    app.installEventFilter(self)
                    self._app_hover_filter_on = True
        except Exception:
            pass

    def _disable_hover_passthrough(self) -> None:
        """Remove the app-level hover filter (idempotent)."""
        if getattr(self, "_app_hover_filter_on", False):
            try:
                from PySide6.QtWidgets import QApplication

                app = QApplication.instance()
                if app is not None:
                    app.removeEventFilter(self)
            except Exception:
                pass
        self._app_hover_filter_on = False

    def hideEvent(self, event) -> None:  # noqa: N802
        self._disable_hover_passthrough()
        super().hideEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        """Outside-click dismissal (never consumes the click itself)."""
        try:
            from PySide6.QtCore import QEvent

            # Hover passthrough first: mirror a button-free move that
            # landed on this overlay (or a descendant) to the host so the
            # active tool's live preview keeps tracking. Return False so
            # the original still reaches this popup's own widgets.
            if (self._app_hover_filter_on and self.isVisible()
                    and event.type() == QEvent.Type.MouseMove
                    and event.buttons() == Qt.MouseButton.NoButton):
                try:
                    from PySide6.QtCore import QPointF as _QPointF
                    from PySide6.QtGui import QMouseEvent
                    from PySide6.QtWidgets import QApplication

                    is_desc = (watched is self
                               or (isinstance(watched, QWidget)
                                   and self.isAncestorOf(watched)))
                    parent = self.parentWidget()
                    if (is_desc and parent is not None
                            and watched is not parent):
                        pos = watched.mapTo(
                            parent, event.position().toPoint())
                        fwd = QMouseEvent(
                            QEvent.Type.MouseMove, _QPointF(pos),
                            Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton, event.modifiers())
                        QApplication.sendEvent(parent, fwd)
                except Exception:
                    pass

            if (self.isVisible()
                    and event.type() == QEvent.Type.MouseButtonPress):
                # Inside test: a press on this popup OR any descendant is
                # always internal (category tabs, cards, swatches, the
                # embedded picker and its QML view). Only a press that
                # lands on something else dismisses. The old test mapped
                # the global point into the *watched child's* local frame
                # and compared it with the popup's parent-frame geometry,
                # so almost every internal press looked like an outside
                # click and closed the popup.
                try:
                    widget = watched if isinstance(watched, QWidget) else None
                    inside = bool(widget is not None and (
                        widget is self or self.isAncestorOf(widget)))
                    if not inside:
                        # The toggle control (selected-tool capsule) is
                        # internal to this dismissal contract: its click
                        # handler owns open/close, so a press there must
                        # not pre-empt the toggle by closing first.
                        source = getattr(self, "_toggle_source", None)
                        if (widget is not None and source is not None
                                and (widget is source
                                     or source.isAncestorOf(widget))):
                            inside = True
                    if not inside:
                        inside = self.rect().contains(
                            self.mapFromGlobal(
                                event.globalPosition().toPoint()))
                except Exception:
                    inside = False
                if not inside:
                    self.close_popup()
        except Exception:
            pass
        return False

    def _teardown_content_qml(self) -> None:
        """Guarded QML teardown: blank sources while bridges are alive.

        Idempotent across animated/immediate dismissal, closeEvent,
        repeated close requests and interrupted animations (M5-A2.2.6).
        The blanking-while-alive invariant holds at every call site:
        content is still parented here, never yet deleted.
        """
        try:
            if bool(getattr(self, "_qml_torn_down", False)):
                return
            self._qml_torn_down = True
        except Exception:
            pass
        try:
            content = getattr(self, "_content", None)
            if content is not None and hasattr(content, "teardown_qml"):
                content.teardown_qml()
        except Exception:
            pass

    def close_popup(self, animate: bool = True) -> None:
        if not self.isVisible():
            return
        # Every close request gets a fresh generation: a pending
        # animated-close timer from an earlier request is stale and must
        # not tear down, hide, or emit for this newer close.
        try:
            self._close_gen = int(getattr(self, "_close_gen", 0)) + 1
            gen = int(self._close_gen)
        except Exception:
            gen = 0
        try:
            parent = self.parent()
            if parent is not None:
                parent.removeEventFilter(self)
        except Exception:
            pass
        stop_widget_anims(self)
        if animate:
            # Keep QML alive and rendered through the slide-out so the
            # palette stays visible for the whole closing transition.
            # Teardown runs in _finish_close while bridges are still
            # alive; closed is emitted only AFTER the hide finishes:
            # deleting the popup (viewer does this on closed) while the
            # animator still references it destroys the C++ object
            # mid-flight.
            try:
                from PySide6.QtCore import QTimer

                _fx.slide_fade_out(self, 0, 140, self, dy_px=4)
                QTimer.singleShot(200, lambda: self._finish_close(gen))
                return
            except Exception:
                pass
        # Immediate dismissal (and animate-path fallback): synchronous
        # teardown, then hide + emit.
        self._teardown_content_qml()
        self.setVisible(False)
        self.closed.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        stop_widget_anims(self)
        # Window-manager close mid-animation: _finish_close may never run,
        # so tear down here rather than destroying live QML sources.
        self._teardown_content_qml()
        super().closeEvent(event)

    def _finish_close(self, gen: int | None = None) -> None:
        # Stale timers (superseded by a newer close request or by new
        # content) do nothing: no duplicate teardown, no second hide, no
        # second emit, and no interference with a newly opened popup.
        try:
            if gen is not None and int(gen) != int(
                    getattr(self, "_close_gen", gen)):
                return
        except Exception:
            pass
        # End of transition: tear down QML while content and bridges are
        # still alive, then hide and emit closed in that order.
        self._teardown_content_qml()
        try:
            if self.isVisible():
                self.setVisible(False)
        except (RuntimeError, AttributeError):
            return
        try:
            self.closed.emit()
        except (RuntimeError, AttributeError):
            pass

    def keyPressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.close_popup()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        if self._content is not None:
            try:
                self._content.update_tokens(tokens)
            except Exception:
                pass
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Elevated inspector surface: 14px body + hairline + nub.

        True rounded silhouette over a translucent background: corners
        outside the radius stay transparent (no black square), and the
        soft shadow is painted manually inside the _SHADOW pad so it
        follows the rounded shape.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            t = self._tokens
            try:
                bg = QColor(t.surface_raised)
                if not bg.isValid():
                    bg = QColor("#22242e")
            except Exception:
                bg = QColor("#22242e")
            try:
                edge = QColor(t.border)
                if not edge.isValid():
                    edge = QColor("#3a3e4d")
            except Exception:
                edge = QColor("#3a3e4d")
            rect = self.rect()
            nub = self._NUB
            pad = self._SHADOW
            side = self._nub_side
            # Body rect: inset by shadow pad, offset by nub zone.
            if side == "top":
                body = rect.adjusted(pad, pad + nub - 1, -pad, -pad)
            elif side == "bottom":
                body = rect.adjusted(pad, pad, -pad, -(pad + nub - 1))
            elif side == "left":
                body = rect.adjusted(pad + nub - 1, pad, -pad, -pad)
            elif side == "right":
                body = rect.adjusted(pad, pad, -(pad + nub - 1), -pad)
            else:
                body = rect.adjusted(pad, pad, -pad, -pad)
            # Manual soft shadow: layered rounded rects, alpha falloff.
            # Alphas read clearly elevated on both themes without turning
            # the surface into a default-dialog bright panel.
            painter.setPen(Qt.PenStyle.NoPen)
            for i, alpha in ((6, 40), (4, 48), (2, 56)):
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(
                    body.adjusted(-i, -i + 2, i, i + 2), 14 + i, 14 + i)
            painter.setBrush(bg)
            painter.drawRoundedRect(body, 14, 14)
            # Connector nub toward the launcher slot.
            pos = max(20, min((body.width() if side in ("top", "bottom")
                               else body.height()) - 20, self._nub_pos))
            half = 7
            try:
                from PySide6.QtGui import QPolygon

                _polygon = QPolygon
            except Exception:
                _polygon = None
            painter.setBrush(bg)
            if side == "top" and _polygon is not None:
                cx = body.left() + pos
                painter.drawPolygon(_polygon([
                    QPoint(cx - half, body.top() + 1),
                    QPoint(cx + half, body.top() + 1),
                    QPoint(cx, body.top() - (nub - 1))]))
            elif side == "bottom" and _polygon is not None:
                cx = body.left() + pos
                painter.drawPolygon(_polygon([
                    QPoint(cx - half, body.bottom() - 1),
                    QPoint(cx + half, body.bottom() - 1),
                    QPoint(cx, body.bottom() + (nub - 1))]))
            elif side == "left" and _polygon is not None:
                cy = body.top() + pos
                painter.drawPolygon(_polygon([
                    QPoint(body.left() + 1, cy - half),
                    QPoint(body.left() + 1, cy + half),
                    QPoint(body.left() - (nub - 1), cy)]))
            elif side == "right" and _polygon is not None:
                cy = body.top() + pos
                painter.drawPolygon(_polygon([
                    QPoint(body.right() - 1, cy - half),
                    QPoint(body.right() - 1, cy + half),
                    QPoint(body.right() + (nub - 1), cy)]))
            pen = QPen(edge)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(body.adjusted(0, 0, -1, -1), 14, 14)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass
