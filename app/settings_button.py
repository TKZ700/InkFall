"""Floating settings control (M4.6.5). No sidebar.

One circular bottom-right button owned by the application shell. It never
participates in page layout (overlaid, fixed relative to the window) and
toggles the centered settings modal. Library only: hidden, with the
modals, in the PDF workspace.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from app.icons import icon
from app.theme.manager import ThemeManager
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["SettingsFab", "FAB_DIAMETER"]

FAB_DIAMETER = 42


class SettingsFab(QPushButton):
    """Circular floating settings button (bottom-right, above content)."""

    def __init__(self, theme: ThemeManager, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(FAB_DIAMETER, FAB_DIAMETER)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Settings: theme and motion")
        self.setAccessibleDescription("Open settings")
        self.update_tokens(tokens)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle for a theme switch (surface; accent on focus only)."""
        self._tokens = tokens
        t = tokens
        self.setIcon(icon("settings", t.text_secondary, t.icon_md))
        self.setFont(role_font(t, "body"))
        # Visually secondary: themed surface + soft border + subtle
        # elevation; accent appears only for focus/active states.
        self.setStyleSheet(
            f"QPushButton {{ background-color: {t.surface_raised};"
            f" color: {t.text_secondary}; border: 1px solid {t.border};"
            f" border-radius: {FAB_DIAMETER // 2}px; padding: 0px; outline: 0; }}"
            f"QPushButton:hover {{ background-color: {t.surface_overlay};"
            f" color: {t.text_primary}; }}"
            f"QPushButton:pressed {{ background-color: {t.surface_overlay}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus};"
            f" color: {t.text_primary}; }}"
        )
        try:
            from PySide6.QtWidgets import QGraphicsDropShadowEffect

            shadow = QGraphicsDropShadowEffect(self)
            from PySide6.QtGui import QColor

            shadow.setColor(QColor(0, 0, 0, 70))
            shadow.setBlurRadius(14)
            shadow.setOffset(0, 3)
            self.setGraphicsEffect(shadow)
        except Exception:
            pass
