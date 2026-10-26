"""Duplicate-open dialog (Book is Already Open). CenteredModal choice.

Shown when the user tries to open a PDF that already has an open
session. Two clearly differentiated actions — Continue in Current
Session (activate the existing editable session untouched) and Open
Read-Only Copy (an explicit second, non-persisting view) — plus Cancel
via Escape/backdrop/Close, consistent with the shared modal language.

Sizing follows the library dialogs' approach: CenteredModal panels are
fixed-size and never grow with content, so the wrapped body is measured
via ``heightForWidth`` and the panel height is refit in
:meth:`_place_panel` against the width the base actually grants.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.modal import CenteredModal, ModalButton
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["DuplicateOpenModal"]

_PANEL_W = 460
_CHROME_M = 24
_HEADER_H = 32
_BODY_GAP = 8
_FIT_BUFFER = 4


class DuplicateOpenModal(CenteredModal):
    """The Book-is-Already-Open choice dialog."""

    continueRequested = Signal()   # activate the existing session
    readonlyRequested = Signal()   # open the separate read-only copy

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(tokens, _PANEL_W, 210, parent)
        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._continue_btn = ModalButton("Continue in Current Session", radius=8)
        self._readonly_btn = ModalButton("Open Read-Only Copy", radius=8)
        for btn in (self._continue_btn, self._readonly_btn):
            btn.setAccessibleName(btn.text())
        self._continue_btn.clicked.connect(self._choose_continue)
        self._readonly_btn.clicked.connect(self._choose_readonly)
        self.body.addWidget(self._body)
        self._actions = QVBoxLayout()
        self._actions.setSpacing(6)
        self._actions.addWidget(self._continue_btn)
        self._actions.addWidget(self._readonly_btn)
        self.body.addLayout(self._actions)
        self.update_tokens(tokens)

    def open_duplicate(self, title: str, display_name: str,
                       launcher=None) -> None:
        """Configure and open (title + one wrapped body line)."""
        self.set_title(title)
        self._body.setText(display_name)
        self.open_modal(launcher)
        self._continue_btn.setFocus()

    def _choose_continue(self) -> None:
        self.close_modal()
        self.continueRequested.emit()

    def _choose_readonly(self) -> None:
        self.close_modal()
        self.readonlyRequested.emit()

    def _place_panel(self) -> None:
        """Fit the fixed-size panel to its wrapped content (see dialog)."""
        super()._place_panel()
        try:
            width = self._panel.width()
            height = _HEADER_H + 2 * 18 + _FIT_BUFFER + _BODY_GAP
            inner = max(40, width - 2 * _CHROME_M)
            body_h = (self._body.heightForWidth(inner)
                      if self._body.isVisibleTo(self._panel)
                      and self._body.hasHeightForWidth()
                      else max(0, self._body.sizeHint().height()))
            actions_h = self._actions.sizeHint().height()
            height += max(0, body_h) + _BODY_GAP + max(0, actions_h)
            self._panel.setFixedHeight(max(140, height))
            area = self.rect()
            self._panel.move(self._panel.x(),
                             max(0, (area.height() - self._panel.height()) // 2))
        except Exception:
            pass

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        t = tokens
        self._body.setFont(role_font(t, "body"))
        self._body.setStyleSheet(f"color: {t.text_secondary};")
        # Primary action (continue) carries the accent; the read-only
        # copy is the quiet alternative — the safer default wins focus.
        self._continue_btn.set_tokens(t)
        self._continue_btn.setFont(role_font(t, "body"))
        self._continue_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            f" color: {t.text_primary}; border: 1px solid {t.input_border};"
            " border-radius: 8px; padding: 7px 16px; outline: 0; }"
            f"QPushButton:hover {{ border: 1px solid {t.input_focus}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        self._readonly_btn.set_tokens(t)
        self._readonly_btn.setFont(role_font(t, "body"))
        self._readonly_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            f" color: {t.text_secondary}; border: 1px solid transparent;"
            " border-radius: 8px; padding: 7px 16px; outline: 0; }"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
