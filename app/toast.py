"""Transient toast notifications (M4.6). Replaces the status bar.

Bottom-center pill, theme-integrated surface, SVG check art (never
unicode), 13px text, 150-200ms entrance/exit, 2-2.5s lifetime,
reduced-motion aware, strictly non-blocking (single-shot timers only).
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from app import fx
from app.icons import icon
from app.theme.multilingual import font_for, local_direction
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["ToastHost", "TOAST_MAX_WIDTH_PX"]

_LIFETIME_MS = 2200
_LIFETIME_ERROR_MS = 2600
TOAST_MAX_WIDTH_PX = 420


class ToastHost(QWidget):
    """Overlay host; position it over the content area (bottom-center)."""

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._queue: deque[str] = deque()
        self._busy = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 24)
        self._pill = QWidget()
        self._pill.setMaximumWidth(TOAST_MAX_WIDTH_PX)
        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(14, 8, 16, 8)
        pill_layout.setSpacing(8)
        pill_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._mark = QLabel()
        self._mark.setFixedSize(16, 16)
        self._label = QLabel("")
        self._label.setWordWrap(False)
        pill_layout.addWidget(self._mark)
        pill_layout.addWidget(self._label)
        layout.addWidget(
            self._pill, 0,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
        )
        self._pill.setVisible(False)
        self._apply_tokens(tokens)

    def show_message(self, title: str, detail: str | None = None, kind: str = "info") -> None:
        """Queue a transient message (never blocks, never raises).

        ``title`` is one compact line (elided); ``detail`` adds a muted
        second line (elided to two lines). Raw paths/exceptions belong in
        ``detail`` — the pill never exceeds 420px. ``kind`` selects the
        status icon (``info``/``error``); errors linger slightly longer.
        """
        from PySide6.QtGui import QFontMetrics

        title = (title or "").strip()
        if not title:
            return
        metrics = QFontMetrics(role_font(self._tokens, "body"))
        short_title = metrics.elidedText(title, Qt.TextElideMode.ElideRight, 360)
        text = short_title
        full_tip = title
        if detail and str(detail).strip():
            clean = " ".join(str(detail).split())
            second = metrics.elidedText(clean, Qt.TextElideMode.ElideRight, 340)
            text = f"{short_title}\n{second}"
            full_tip = f"{title}\n{clean}"
        self._queue.append((text, full_tip, kind))
        if not self._busy:
            self._pump()

    def pending_count(self) -> int:
        """Queued + visible messages (for tests)."""
        return len(self._queue) + (1 if self._busy else 0)

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Apply a theme switch live."""
        self._tokens = tokens
        self._apply_tokens(tokens)

    # -- internals ----------------------------------------------------------

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        t = tokens
        self._mark.setPixmap(icon("check", t.text_primary, 14).pixmap(16, 16))
        self._pill.setStyleSheet(
            f"background-color: {t.surface_overlay}; color: {t.text_primary};"
            f" border: 1px solid {t.border}; border-radius: 10px;"
        )

    def _pump(self) -> None:
        if not self._queue:
            self._busy = False
            return
        self._busy = True
        text, full_tip, kind = self._queue.popleft()
        self._label.setText(text)
        self._label.setAccessibleDescription(full_tip)
        self._label.setFont(font_for(text, "body", self._tokens))
        self._label.setLayoutDirection(local_direction(text))
        self._mark.setPixmap(icon(
            "alert" if kind == "error" else "check", self._tokens.text_primary, 14).pixmap(16, 16))
        self._pill.setVisible(True)
        fx.fade_in(self._pill, 160, self)
        lifetime = _LIFETIME_ERROR_MS if kind == "error" else _LIFETIME_MS
        QTimer.singleShot(lifetime, self._dismiss)

    def _dismiss(self) -> None:
        fx.fade_out(self._pill, 160, self)
        QTimer.singleShot(180, self._pump)
