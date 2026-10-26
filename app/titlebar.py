"""Custom minimal window chrome (M4.6.4). Visual shell only.

A 38px title bar replaces the native frame (``Qt.FramelessWindowHint``).
The document tab strip is hosted in this same row, immediately left of the
minimize / maximize-restore / close controls, so Inkfall has one unified
top bar rather than stacked navigation rows. Dragging and edge-resizing use
Qt-native ``startSystemMove`` / ``startSystemResize`` — no manual
coordinate math, DPI-safe, Windows snap preserved. All window
functionality (taskbar, shortcuts, fullscreen, Esc) is retained.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from app.icons import app_icon, icon
from app.theme.tokens import InkfallTokens, token_rgba
from app.theme.typography import role_font

__all__ = ["TitleBar", "TITLE_BAR_H", "CONTROL_W"]

TITLE_BAR_H = 38
CONTROL_W = 36


def _dimmed_surface(tokens: InkfallTokens) -> str:
    """A clearly distinct, still-restrained dim of the shell surface.

    The top row reads as its own band behind Home, tabs and the window
    controls. Per-theme multiplicative dims of ``surface`` (the ramps are
    not symmetric: the dark ramp is already deep so it takes a stronger
    step to read; the warm light ramp needs a gentler one to stay
    editorial rather than turning gray). Tab strips inside the row stay
    transparent and inherit it; the single bottom hairline is unchanged.
    """
    r, g, b, _a = token_rgba(tokens.surface)
    factor = 0.80 if str(getattr(tokens, "name", "dark")).casefold() != "light" \
        else 0.92
    return (f"rgb({int(round(r * factor))},"
            f"{int(round(g * factor))},{int(round(b * factor))})")


class TitleBar(QWidget):
    """Frameless title bar: document tabs + window controls."""

    minimizeRequested = Signal()
    maximizeToggleRequested = Signal()
    fullscreenToggleRequested = Signal()
    closeRequested = Signal()
    pointerExited = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setObjectName("inkfall-titlebar")
        self.setFixedHeight(TITLE_BAR_H)
        self._active = True
        self._maximized = False
        self._fullscreen = False
        self._tab_strip = None
        self._strip_slot = True

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.setSpacing(6)
        self._layout = layout

        # Compatibility labels: existing code/tests still read the app icon
        # pixmap and the subordinate context text. They are deliberately not
        # in the visible row anymore — the tab strip's Home mark and the tab
        # titles carry that information now.
        self._icon = QLabel(self)
        self._sync_icon()
        self._icon.setVisible(False)
        self._wordmark = QLabel("Inkfall", self)
        self._wordmark.setVisible(False)
        self._context = QLabel("", self)
        self._context.setVisible(False)

        # Placeholder that the tab strip replaces via set_tab_strip().
        layout.addStretch(1)

        self._min_btn = self._make_control("minus", "Minimize",
                                           self.minimizeRequested.emit)
        self._max_btn = self._make_control("maximize", "Maximize",
                                           self.maximizeToggleRequested.emit)
        self._fs_btn = self._make_control(
            "fullscreen", "Fullscreen mode",
            self.fullscreenToggleRequested.emit)
        self._close_btn = self._make_control("x", "Close", self.closeRequested.emit,
                                            danger=True)
        self._close_btn.setObjectName("inkfall-titlebar-close")
        layout.addWidget(self._fs_btn)
        layout.addWidget(self._min_btn)
        layout.addWidget(self._max_btn)
        layout.addWidget(self._close_btn)
        # No startup focus artifact: chrome never grabs focus on show.
        # Keyboard remains operable via Tab (TabFocus kept); focus ring
        # is fully transparent by stylesheet (hover/press paint only).
        for _b in (self._fs_btn, self._min_btn, self._max_btn, self._close_btn):
            try:
                _b.clearFocus()
            except Exception:
                pass
        self.update_tokens(tokens)

    # -- tab-strip host -------------------------------------------------------

    def set_tab_strip(self, strip) -> None:
        """Install the document tab strip into the shared control row."""
        if strip is None or self._tab_strip is not None:
            return
        try:
            if strip.parentWidget() is not self:
                strip.setParent(self)
            if self._strip_slot:
                item = self._layout.takeAt(0)
                if item is not None:
                    del item
                self._strip_slot = False
            self._layout.insertWidget(0, strip, 1)
            strip.setVisible(True)
            self._tab_strip = strip
        except Exception:
            pass

    def begin_system_move(self) -> bool:
        """Start the native window move (used by the empty tab-strip area)."""
        try:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                return True
        except Exception:
            pass
        return False

    def _make_control(self, icon_name: str, name: str, slot, danger: bool = False) -> QPushButton:
        from app.modal import ModalButton

        btn = ModalButton("", bar=True, danger=danger)
        btn.setFixedSize(CONTROL_W, TITLE_BAR_H)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        btn.setAccessibleName(name)
        btn.clicked.connect(slot)
        btn.setProperty("icon_name", icon_name)
        try:
            btn.set_tokens(self._tokens)
        except Exception:
            pass
        btn.setStyleSheet(
            "QPushButton { border: 1px solid transparent; border-radius: 0px;"
            " min-height: 0px; outline: 0; background: transparent; }"
            "QPushButton:focus { border: 1px solid transparent; outline: 0; }"
            "QPushButton:pressed { border: 1px solid transparent; outline: 0; }"
        )
        try:
            from app import fx as _fx

            # M4.6.11: smooth press dip (visual only, never gates).
            btn.pressed.connect(lambda: _fx.press_feedback(btn, 90))
        except Exception:
            pass
        return btn

    def _sync_icon(self) -> None:
        pm = app_icon().pixmap(20, 20)
        if pm.isNull():
            pm = icon("library", "#8B93E0", 20).pixmap(20, 20)
        self._icon.setPixmap(pm)

    # -- state ---------------------------------------------------------------

    def set_context(self, text: str) -> None:
        """Subordinate context after the wordmark ('' for Library)."""
        self._context.setText(text)

    def set_active(self, active: bool) -> None:
        """Dim the bar when the window loses focus."""
        self._active = bool(active)
        self.update_tokens(self._tokens)

    def set_maximized(self, maximized: bool) -> None:
        """Swap the maximize/restore glyph + tooltip."""
        self._maximized = bool(maximized)
        self._max_btn.setProperty("icon_name",
                                  "restore" if maximized else "maximize")
        self._max_btn.setAccessibleName("Restore" if maximized else "Maximize")
        self._max_btn.setIcon(icon("restore" if maximized else "maximize",
                                   self._tokens.text_secondary,
                                   self._tokens.icon_sm))

    def set_fullscreen(self, fullscreen: bool) -> None:
        """Keep the Fullscreen Mode control in step with the app state.

        One toggle, one state machine: the button keeps the same
        ``fullscreen`` glyph in both directions (like the ViewerBar
        control it succeeds) and only its accessible name mirrors the
        current state. The window's ``toggle_fullscreen`` remains the
        single owner of the transition.
        """
        self._fullscreen = bool(fullscreen)
        self._fs_btn.setAccessibleName(
            "Exit fullscreen" if self._fullscreen else "Fullscreen mode")

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle for a theme switch or active-state change."""
        self._tokens = tokens
        t = tokens
        self.setStyleSheet(
            f"#inkfall-titlebar {{ background-color: {_dimmed_surface(t)};"
            f" border-bottom: 1px solid {t.border_subtle}; }}"
        )
        self._wordmark.setFont(role_font(t, "title"))
        self._wordmark.setStyleSheet(
            f"color: {t.text_primary if self._active else t.text_muted};")
        self._context.setFont(role_font(t, "meta"))
        self._context.setStyleSheet(
            f"color: {t.text_muted if self._active else t.text_tertiary};")
        for btn in (self._fs_btn, self._min_btn, self._max_btn, self._close_btn):
            name = btn.property("icon_name") or "minus"
            btn.setIcon(icon(name, t.text_secondary, t.icon_sm))
            btn.setFont(role_font(t, "body"))
            try:
                btn.set_tokens(t)
            except Exception:
                pass
            # M4.6.11: transparent idle (hover/press painted by
            # ModalButton progress); focus ring swaps color only.
            # min-height 0 so the app QSS never fights the 36x38 bar.
            # Window controls never show a focus/pressed border: keyboard
            # focus stays operable via ModalButton paint, not CSS.
            btn.setStyleSheet(
                "QPushButton { border: 1px solid transparent; border-radius: 0px;"
                " min-height: 0px; outline: 0; background: transparent; }"
                "QPushButton:focus { border: 1px solid transparent; outline: 0; }"
                "QPushButton:pressed { border: 1px solid transparent; outline: 0; }"
            )
        if self._maximized:
            self._max_btn.setIcon(icon("restore", t.text_secondary, t.icon_sm))

    # -- drag / double-click (Qt-native, never manual geometry) ---------------

    def press_should_drag(self, pos) -> bool:
        """True when a press at bar-local pos may start a system move.

        Interactive controls never drag: any press landing on a child
        button is rejected; empty chrome and passive labels may drag.
        M4.6.11: the outer 6px top strip is owned by resize — never
        start a system move there. Pure helper, safe to test.
        """
        try:
            if pos.y() <= 6:
                return False
        except Exception:
            pass
        return not isinstance(self.childAt(pos), QPushButton)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Left press on empty bar chrome starts a native system move."""
        try:
            _pos = event.position().toPoint()
        except Exception:
            _pos = None
        if _pos is not None and _pos.y() <= 6:
            # M4.6.11: top 6px strip belongs to resize — let the
            # MainWindow pre-empt (eventFilter) or ignore; never move.
            super().mousePressEvent(event)
            return
        if (event.button() == Qt.MouseButton.LeftButton
                and self.press_should_drag(event.position().toPoint())):
            if self.begin_system_move():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Double-click empty chrome toggles maximize/restore."""
        try:
            if event.position().toPoint().y() <= 6:
                super().mouseDoubleClickEvent(event)
                return
        except Exception:
            pass
        if (event.button() == Qt.MouseButton.LeftButton
                and self.press_should_drag(event.position().toPoint())):
            self.maximizeToggleRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        """Pointer left the whole row (fullscreen conceal trigger)."""
        try:
            self.pointerExited.emit()
        except Exception:
            pass
        super().leaveEvent(event)
