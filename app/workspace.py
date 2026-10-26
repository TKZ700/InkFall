"""PDF workspace top bar (M4.5). Minimal 44px chrome.

Left: single back affordance (chevron + "Library"). Center: elided
document title. Right: fullscreen button only. A documented empty
stretch marks the reserved slot for the future M5 toolbar — no fake
controls. The bar stays visible in windowed mode; fullscreen hides it
along with the rest of the application chrome.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from app.icons import icon
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["ViewerBar"]


class ViewerBar(QWidget):
    """Minimal workspace chrome above the PDF viewer."""

    backRequested = Signal()
    fullscreenRequested = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setFixedHeight(tokens.viewer_bar_h)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        self._back = QPushButton("Library")
        self._back.setFlat(True)
        self._back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back.setAccessibleName("Back to library")
        self._back.setAccessibleDescription("Return to the library")
        self._back.clicked.connect(self.backRequested.emit)
        layout.addWidget(self._back)

        # Reserved slot for the future M5 Drawboard-inspired toolbar.
        # Intentionally empty: no fake controls.
        layout.addStretch(1)
        self._title = QLabel("Viewer")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._title, stretch=2)
        layout.addStretch(3)

        self._fullscreen = QPushButton()
        self._fullscreen.setFlat(True)
        self._fullscreen.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fullscreen.setAccessibleName("Toggle fullscreen")
        self._fullscreen.setAccessibleDescription("Toggle fullscreen (F11)")
        self._fullscreen.clicked.connect(self.fullscreenRequested.emit)
        layout.addWidget(self._fullscreen)

        self._apply_tokens(tokens)

    def set_title(self, title: str) -> None:
        """Set the document title (elided by paint, full in accessible name)."""
        self._title.setText(title or "Untitled")
        self._title.setAccessibleName(title or "Untitled")
        self._title.setAccessibleDescription("Open document title")

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Apply a theme switch live."""
        self._tokens = tokens
        self._apply_tokens(tokens)

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        t = tokens
        self._back.setFont(role_font(t, "nav"))
        self._back.setIcon(icon("chevron-left", t.text_secondary, t.icon_md))
        self._back.setStyleSheet(
            f"QPushButton {{ color: {t.text_secondary}; border-radius: {t.radius_md}px; padding: 6px 10px; }}"
            f"QPushButton:hover {{ color: {t.text_primary}; background-color: {t.surface_overlay}; }}"
        )
        self._title.setFont(role_font(t, "doc_title"))
        self._title.setStyleSheet(f"color: {t.text_primary};")
        self._fullscreen.setIcon(icon("fullscreen", t.text_secondary, t.icon_md))
        self._fullscreen.setFixedSize(32, 32)
        self._fullscreen.setStyleSheet(
            f"QPushButton {{ border-radius: {t.radius_md}px; }}"
            f"QPushButton:hover {{ background-color: {t.surface_overlay}; }}"
        )
        self.setStyleSheet(
            f"ViewerBar {{ background-color: {t.canvas_deep};"
            f" border-bottom: 1px solid {t.border_subtle}; }}"
        )
        _ = QFontMetrics(self._title.font())
