"""New action centered modal (M4.6.5). Library shell only.

Premium action chooser on the shared modal family: ＋ Create Blank
(customize paper in the New Blank dialog, then open an editable unsaved
workspace) and Open PDF (existing native import/open path). No second
loading system, no rendering changes, no animation gating the open.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.icons import icon
from app.modal import CenteredModal
from app.theme.manager import ThemeManager
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["NewModal", "NEW_W", "NEW_H"]

NEW_W = 460
NEW_H = 330


class ActionRow(QWidget):
    """Clickable action card (M4.6.7).

    A ``QPushButton`` must never host child layouts — Qt buttons fight
    foreign layouts for painting/size hints and render broken on some
    styles/DPIs (the M4.6.6 New-modal row bug). This widget owns its
    layout properly: mouse press + Enter/Space activate, TabFocus only
    (mouse clicks never take focus, so no focus ring on click), and a
    QWidget paints no native focus indicator — keyboard focus shows
    only the token ring from the stylesheet.
    """

    activated = Signal()

    def __init__(self, icon_name: str, title: str, hint: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("inkfall-action-row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(f"{title}: {hint}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(14)
        art = QLabel()
        art.setPixmap(icon("plus", "#8B93E0", 22).pixmap(26, 26))
        layout.addWidget(art)
        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(2)
        name = QLabel(title)
        texts.addWidget(name)
        sub = QLabel(hint)
        sub.setWordWrap(True)
        texts.addWidget(sub)
        layout.addLayout(texts, stretch=1)
        self._inkfall_art = art
        self._inkfall_name = name
        self._inkfall_sub = sub
        self._inkfall_icon = icon_name
        self._tokens = None
        self._inkfall_art_dim = None
        self._inkfall_art_lit = None
        # M4.6.9 painted hover: a 140ms OutCubic progress drives wash +
        # border + icon swap (QSS cannot animate :hover on QWidget).
        self._hover_p = 0.0
        self._hover_lit = False
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(140)
        self._hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._hover_anim.setStartValue(0.0)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.valueChanged.connect(self._on_hover_tick)

    def set_hover_art(self, tokens, dim_pm, lit_pm) -> None:
        """Tokens + dim/lit icon variants for the hover crossfade."""
        try:
            self._tokens = tokens
            self._inkfall_art_dim = dim_pm
            self._inkfall_art_lit = lit_pm
        except Exception:
            pass

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self._hover_glide_to(1.0)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self._hover_glide_to(0.0)

    def _hover_glide_to(self, target: float) -> None:
        try:
            self._hover_anim.stop()
            self._hover_anim.setStartValue(float(self._hover_p))
            self._hover_anim.setEndValue(float(target))
            self._hover_anim.start()
        except Exception:
            pass

    def _on_hover_tick(self, value) -> None:
        try:
            self._hover_p = max(0.0, min(1.0, float(value)))
        except Exception:
            self._hover_p = 1.0
        try:
            lit = self._hover_p >= 0.5
            if lit != self._hover_lit:
                self._hover_lit = lit
                art = self._inkfall_art_lit if lit else self._inkfall_art_dim
                if art is not None and not art.isNull():
                    self._inkfall_art.setPixmap(art)
        except Exception:
            pass
        try:
            self.update()
        except Exception:
            pass

    def paintEvent(self, event) -> None:  # noqa: N802
        """Stylesheet base first, then progress-driven hover (M4.6.9)."""
        super().paintEvent(event)
        try:
            p = max(0.0, min(1.0, float(self._hover_p)))
        except Exception:
            return
        if p <= 0.0:
            return
        painter = QPainter(self)
        try:
            if not painter.isActive():
                return
            t = self._tokens
            if t is None:
                return
            path = QPainterPath()
            area = self.rect().adjusted(0, 0, -1, -1)
            path.addRoundedRect(area, 10, 10)
            wash = QColor(t.surface_overlay)
            wash.setAlpha(int(255 * p))
            painter.fillPath(path, wash)
            edge = QColor(t.accent)
            edge.setAlpha(int(220 * p))
            painter.setPen(edge)
            painter.drawPath(path)
        except Exception:
            pass
        finally:
            try:
                if painter.isActive():
                    painter.end()
            except Exception:
                pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # M4.6.8 press response (~100ms opacity dip; QWidget has no
            # :pressed pseudo-state, so the feedback is effect-driven).
            try:
                from app import fx as _fx

                _fx.press_feedback(self, 100)
            except Exception:
                pass
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            # M4.6.9: keyboard press answers too (same dip as mouse).
            try:
                from app import fx as _fx

                _fx.press_feedback(self, 90)
            except Exception:
                pass
            self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class NewModal(CenteredModal):
    """Centered New-action chooser (same modal family as Settings)."""

    createBlankRequested = Signal()
    openPdfRequested = Signal()

    def __init__(self, theme: ThemeManager, tokens: InkfallTokens, parent=None) -> None:
        self._theme = theme
        super().__init__(tokens, NEW_W, NEW_H, parent)
        self.set_title("New")
        self._rows: list = []
        self._rows.append(self._add_row(
            "plus", "Create Blank", "Choose paper, then draw — save when you're ready",
            self.createBlankRequested.emit))
        self._rows.append(self._add_row(
            "doc", "Open PDF", "Choose an existing PDF from your computer",
            self.openPdfRequested.emit))
        self.update_tokens(tokens)

    def _add_row(self, icon_name: str, title: str, hint: str, slot) -> ActionRow:
        """One premium action row (icon + title + hint, proper widget)."""
        row = ActionRow(icon_name, title, hint, self._panel)
        row.activated.connect(slot)
        self.body.addWidget(row)
        return row

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle rows (called by the modal base)."""
        t = tokens
        for row in self._rows:
            row.setFont(role_font(t, "body"))
            # M4.6.9: hover is painted by progress (140ms); the sheet
            # keeps base + keyboard-only focus ring.
            row.setStyleSheet(
                f"#inkfall-action-row {{ background-color: {t.input_background};"
                f" border: 1px solid {t.input_border}; border-radius: 10px; }}"
                f"#inkfall-action-row:focus {{ border: 1px solid {t.input_focus};"
                f" outline: 0; }}"
            )
            try:
                dim_pm = icon(row._inkfall_icon, t.text_secondary, 22).pixmap(26, 26)
                lit_pm = icon(row._inkfall_icon, t.accent, 22).pixmap(26, 26)
                row.set_hover_art(t, dim_pm, lit_pm)
                row._inkfall_art.setPixmap(
                    lit_pm if row._hover_lit else dim_pm)
                row._inkfall_name.setFont(role_font(t, "title"))
                row._inkfall_name.setStyleSheet(f"color: {t.text_primary};")
                row._inkfall_sub.setFont(role_font(t, "meta"))
                row._inkfall_sub.setStyleSheet(f"color: {t.text_muted};")
            except Exception:
                pass

    def close_modal(self, animate: bool = True) -> None:
        """Stop row hover animators so none outlive the close (M4.6.9)."""
        try:
            for row in self._rows:
                try:
                    row._hover_anim.stop()
                except Exception:
                    pass
                try:
                    row._hover_p = 0.0
                    row._hover_lit = False
                    art = row._inkfall_art_dim
                    if art is not None and not art.isNull():
                        row._inkfall_art.setPixmap(art)
                    row.update()
                except Exception:
                    pass
        except Exception:
            pass
        super().close_modal(animate=animate)
