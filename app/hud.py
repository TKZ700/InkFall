"""Undo/Redo floating capsule (M5-A1 UI shell only).

One cohesive bottom-left split capsule: solid rounded body, central
divider, independently clickable halves with circular hover/press
highlights plus a brief click pulse. There is no annotation command
engine in M5-A1, so both actions start disabled and stay disabled —
the ``undoRequested``/``redoRequested`` signals plus
``set_can_undo``/``set_can_redo`` are the future connection points.
The capsule is a plain child of ``PdfViewer`` (like the shelf), so it
survives fullscreen, and it never touches the render pipeline.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QWidget

from app.icons import icon as _icon
from app.theme.tokens import InkfallTokens

__all__ = ["UndoRedoCapsule", "HUD_MARGIN"]

#: Distance from the workspace edges (matches shelf margin language).
HUD_MARGIN = 12


class _CapsuleHalf(QAbstractButton):
    """One capsule half: transparent face, circular hover/press/pulse."""

    def __init__(self, icon_name: str, accessible: str, parent=None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._tokens = None
        self._pulse = 0.0
        self._pulse_anim = None
        self.setFixedSize(36, 32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(accessible)
        self.setStyleSheet(
            "QAbstractButton { background: transparent; border: none; outline: 0; }"
            "QAbstractButton:focus { border: none; outline: 0; }"
            "QAbstractButton:disabled { background: transparent; border: none; }")
        self.toggled.connect(lambda _c: self.update())
        try:
            self.pressed.connect(self.update)
            self.released.connect(self.update)
        except Exception:
            pass
        try:
            self.clicked.connect(self._play_pulse)
        except Exception:
            pass

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def _play_pulse(self) -> None:
        """Brief circular click pulse (visual only, never gates)."""
        try:
            from PySide6.QtCore import QVariantAnimation, QEasingCurve

            if self._pulse_anim is not None:
                try:
                    self._pulse_anim.stop()
                except Exception:
                    pass
            anim = QVariantAnimation(self)
            anim.setDuration(140)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            anim.valueChanged.connect(self._on_pulse_tick)
            self._pulse_anim = anim
            anim.start()
        except Exception:
            pass

    def _on_pulse_tick(self, value) -> None:
        try:
            self._pulse = max(0.0, min(1.0, float(value)))
        except Exception:
            self._pulse = 1.0
        self.update()

    def _glyph_color(self) -> QColor:
        try:
            key = "text_primary" if self.isEnabled() else "text_tertiary"
            return QColor(getattr(self._tokens, key, "#8b93a7"))
        except Exception:
            return QColor("#8b93a7")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect()
            side = min(rect.width(), rect.height()) - 6
            cx, cy = rect.center().x(), rect.center().y()
            circle = (cx - side // 2, cy - side // 2, side, side)
            down = False
            try:
                down = bool(self.isDown())
            except Exception:
                pass
            if self.isEnabled() and (down or self.underMouse()):
                try:
                    from app.theme.tokens import token_rgba as _rgba

                    try:
                        ar, ag, ab, aa = _rgba(
                            getattr(self._tokens, "accent_soft",
                                    "rgba(139,147,224,0.2)"))
                    except Exception:
                        ar, ag, ab, aa = (139, 147, 224, 0.35)
                    wash = QColor(ar, ag, ab)
                    wash.setAlphaF(aa if not down else min(1.0, aa + 0.25))
                except Exception:
                    wash = QColor(139, 147, 224, 90)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(wash)
                painter.drawEllipse(*circle)
            if self._pulse > 0.0 and self.isEnabled():
                try:
                    ring = QColor(getattr(self._tokens, "accent", "#8b93e0"))
                    ring.setAlphaF(0.55 * (1.0 - self._pulse))
                    pen = QPen(ring)
                    pen.setWidth(2)
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    grow = int(3 * self._pulse)
                    painter.drawEllipse(circle[0] - grow, circle[1] - grow,
                                        circle[2] + 2 * grow,
                                        circle[3] + 2 * grow)
                except Exception:
                    pass
            # Keyboard-focus ring (UI-02/UI-16): TabFocus halves had no
            # visible focus state; the shared ring follows the painted
            # circle silhouette.
            if self.hasFocus() and self.isEnabled() \
                    and self._tokens is not None:
                from app.focus import draw_focus_ring

                draw_focus_ring(
                    painter,
                    QRectF(float(circle[0]), float(circle[1]),
                           float(circle[2]), float(circle[3])),
                    self._tokens, radius=None)
            try:
                glyph = _icon(self._icon_name,
                              self._glyph_color().name(), 18).pixmap(18, 18)
                if not glyph.isNull():
                    painter.drawPixmap(cx - 9, cy - 9, glyph)
            except Exception:
                pass
        finally:
            try:
                painter.end()
            except Exception:
                pass


class UndoRedoCapsule(QWidget):
    """Floating Undo/Redo split capsule (UI-only until M5-B history)."""

    undoRequested = Signal()
    redoRequested = Signal()
    saveRequested = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._can_undo = False
        self._can_redo = False
        self._can_save = False
        self.setObjectName("inkfall-undo-redo")
        self.setAccessibleName("Undo redo controls")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(0)
        self._undo_btn = _CapsuleHalf("undo", "Undo", self)
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(self.undoRequested.emit)
        layout.addWidget(self._undo_btn)
        self._redo_btn = _CapsuleHalf("redo", "Redo", self)
        self._redo_btn.setEnabled(False)
        self._redo_btn.clicked.connect(self.redoRequested.emit)
        layout.addWidget(self._redo_btn)
        # M8: save half — enabled only while an unsaved blank is live.
        self._save_btn = _CapsuleHalf("save", "Save", self)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self.saveRequested.emit)
        layout.addWidget(self._save_btn)
        self.setFixedSize(112, 40)
        self.update_tokens(tokens)
        self._sync_enabled()

    # -- future connection points (M5-B command stack) --------------------

    def set_can_undo(self, allowed: bool) -> None:
        self._can_undo = bool(allowed)
        self._sync_enabled()

    def set_can_redo(self, allowed: bool) -> None:
        self._can_redo = bool(allowed)
        self._sync_enabled()

    def can_undo(self) -> bool:
        return self._can_undo

    def can_redo(self) -> bool:
        return self._can_redo

    def set_can_save(self, allowed: bool) -> None:
        """Save-half enablement (present when an unsaved blank is in edit)."""
        self._can_save = bool(allowed)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        try:
            self._undo_btn.setEnabled(self._can_undo)
        except Exception:
            pass
        try:
            self._redo_btn.setEnabled(self._can_redo)
        except Exception:
            pass
        try:
            self._save_btn.setEnabled(self._can_save)
        except Exception:
            pass
        try:
            self.update()
        except Exception:
            pass

    # -- paint + theming --------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        """One cohesive body: solid rounded capsule + central divider."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            t = self._tokens
            body = self.rect().adjusted(1, 1, -1, -1)
            try:
                bg = QColor(t.surface_raised)
            except Exception:
                bg = QColor("#23262f")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(body, 19, 19)
            try:
                edge = QColor(t.border)
            except Exception:
                edge = QColor("#3a3e4d")
            pen = QPen(edge)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(body, 19, 19)
            try:
                div = QColor(t.border_subtle)
            except Exception:
                div = QColor("#2c313d")
            dpen = QPen(div)
            dpen.setWidth(1)
            painter.setPen(dpen)
            # Three caps (undo | redo | save): separators at half bounds.
            for x in (38, 74):
                painter.drawLine(x, body.top() + 9, x, body.bottom() - 8)
        finally:
            try:
                painter.end()
            except Exception:
                pass

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        try:
            self._undo_btn.set_tokens(tokens)
            self._redo_btn.set_tokens(tokens)
            self._save_btn.set_tokens(tokens)
        except Exception:
            pass
        self.update()
