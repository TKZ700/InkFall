"""Library dialogs (Home Library redesign). Centered prompt + confirm.

Two small :class:`~app.modal.CenteredModal` subclasses that replace
``QInputDialog``/``QMessageBox`` in every library flow, so organization
dialogs speak the same dimmed-backdrop, centered-panel language as
Settings/New. Both are page-local overlays (parented to the LibraryPage,
which fills the same client area), focus-trapped by the base class, and
closed by Escape/backdrop.

Sizing: CenteredModal panels are FIXED-size and never grow with their
content, so a wrapped message can overflow and paint over neighboring
controls. Both dialogs therefore measure their content here (word-wrapped
labels via ``heightForWidth``) and fit the panel height in
:meth:`_place_panel`, using the width the base actually grants after its
clamp — the layout can never overflow again, at any window size.
Runtime behavior is owned by the caller: the modals only collect/confirm
intent and emit signals.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from app.modal import CenteredModal, ModalButton
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["LibraryPromptModal", "LibraryConfirmModal"]

_PANEL_W = 440
_CHROME_M = 24     # CenteredModal chrome left/right margin
_HEADER_H = 32     # header row: title + 32px close button
_BODY_GAP = 8      # body top margin / inter-item spacing
_FIT_BUFFER = 4


def _label_height(label: QLabel, panel: QWidget, width: int) -> int:
    """Wrapped height of a (possibly hidden) label at ``width``.

    ``isVisibleTo(panel)`` — not ``isVisible()`` — because the modal
    overlay itself is still hidden while the panel is being sized
    during ``open_modal``; only the label's own visibility flag
    matters here.
    """
    try:
        if not label.isVisibleTo(panel):
            return 0
    except Exception:
        pass
    inner = max(40, width - 2 * _CHROME_M)
    if label.wordWrap() and label.hasHeightForWidth():
        h = label.heightForWidth(inner)
        if h is not None and h > 0:
            return h
    return max(0, label.sizeHint().height())


class LibraryPromptModal(CenteredModal):
    """One-line text prompt (new folder, rename) with Cancel/confirm.

    Vertical hierarchy: title (panel header) → explanatory message →
    name field → validation error row → action row. Every region owns
    its own layout space; the panel height follows the wrapped content.
    """

    confirmed = Signal(str)

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(tokens, _PANEL_W, 200, parent)
        self._required = False
        self._message = QLabel("")
        self._message.setWordWrap(True)
        self._field = QLineEdit()
        self._field.setClearButtonEnabled(True)
        self._field.setFixedHeight(34)
        self._field.setAccessibleName("Name")
        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        self._cancel_btn = ModalButton("Cancel", radius=8)
        self._ok_btn = ModalButton("OK", radius=8)
        self._ok_btn.setAccessibleName("Confirm")
        self._cancel_btn.clicked.connect(self.close_modal)
        self._ok_btn.clicked.connect(self._confirm)
        self._field.returnPressed.connect(self._confirm)
        self._field.textEdited.connect(lambda _t: self._clear_error())
        self.body.addWidget(self._message)
        self.body.addWidget(self._field)
        self.body.addWidget(self._error)
        self._actions = QHBoxLayout()
        self._actions.addStretch(1)
        self._actions.addWidget(self._cancel_btn)
        self._actions.addWidget(self._ok_btn)
        self.body.addLayout(self._actions)
        self.update_tokens(tokens)

    def open_prompt(self, title: str, message: str, value: str = "",
                    placeholder: str = "", confirm_label: str = "Save",
                    launcher=None, required: bool = False) -> None:
        """Configure and open (focus + preselect the field).

        ``required`` blocks an empty confirm with an inline error
        instead of closing (folder creation); optional names fall back
        through the service (renames).
        """
        self._required = bool(required)
        self.set_title(title)
        self._message.setText(message)
        self._message.setVisible(bool(message))
        self._clear_error()
        self._field.setText(value)
        self._field.setPlaceholderText(placeholder)
        self._ok_btn.setText(confirm_label)
        self.open_modal(launcher)
        self._field.setFocus()
        self._field.selectAll()

    def _confirm(self) -> None:
        text = self._field.text().strip()
        if self._required and not text:
            self._show_error("Enter a name first.")
            return
        self.close_modal()
        self.confirmed.emit(text)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(True)
        self._place_panel()
        self._field.setFocus()

    def _clear_error(self) -> None:
        if self._error.isVisible():
            self._error.setVisible(False)
            self._place_panel()

    def _place_panel(self) -> None:
        """Fit the fixed-size panel to its wrapped content.

        The base clamps the panel into the overlay; after it runs, the
        height is re-measured against the width actually granted so the
        layout can never overflow.
        """
        super()._place_panel()
        try:
            width = self._panel.width()
            height = _HEADER_H + 2 * 18 + _FIT_BUFFER  # header + chrome margins
            height += _BODY_GAP
            heights = [
                _label_height(self._message, self._panel, width),
                self._field.height(),
                _label_height(self._error, self._panel, width),
                self._actions.sizeHint().height(),
            ]
            visible = sum(1 for h in heights if h > 0)
            height += sum(heights) + _BODY_GAP * max(0, visible - 1)
            self._panel.setFixedHeight(max(120, height))
            self._place_panel_y()
        except Exception:
            pass

    def _place_panel_y(self) -> None:
        """Re-center the panel vertically after a height change."""
        try:
            area = self.rect()
            self._panel.move(self._panel.x(),
                             max(0, (area.height() - self._panel.height()) // 2))
        except Exception:
            pass

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        t = tokens
        self._message.setFont(role_font(t, "body"))
        self._message.setStyleSheet(f"color: {t.text_secondary};")
        self._error.setFont(role_font(t, "meta"))
        self._error.setStyleSheet(f"color: {t.danger};")
        self._field.setFont(role_font(t, "body"))
        self._field.setStyleSheet(
            f"QLineEdit {{ background-color: {t.input_background};"
            f" color: {t.text_primary}; border: 1px solid {t.input_border};"
            f" border-radius: 8px; padding: 0 10px; }}"
            f"QLineEdit:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        for btn in (self._cancel_btn, self._ok_btn):
            btn.set_tokens(t)
            btn.setFont(role_font(t, "body"))
            btn.setStyleSheet(
                "QPushButton { background: transparent;"
                f" color: {t.text_primary}; border: 1px solid {t.input_border};"
                " border-radius: 8px; padding: 6px 16px; outline: 0; }"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )


class LibraryConfirmModal(CenteredModal):
    """Centered confirmation with an optional destructive emphasis.

    Title and message body own separate layout regions; long bodies and
    filesystem paths wrap, and the panel height follows the content.
    """

    confirmed = Signal()

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(tokens, _PANEL_W + 20, 200, parent)
        self._danger = False
        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._cancel_btn = ModalButton("Cancel", radius=8)
        self._confirm_btn = ModalButton("Confirm", radius=8)
        self._confirm_btn.setAccessibleName("Confirm")
        self._cancel_btn.clicked.connect(self.close_modal)
        self._confirm_btn.clicked.connect(self._confirm)
        self.body.addWidget(self._body)
        self._actions = QHBoxLayout()
        self._actions.addStretch(1)
        self._actions.addWidget(self._cancel_btn)
        self._actions.addWidget(self._confirm_btn)
        self.body.addLayout(self._actions)
        self.update_tokens(tokens)

    def open_confirm(self, title: str, body: str, confirm_label: str = "Confirm",
                     danger: bool = False, launcher=None) -> None:
        """Configure and open. ``danger`` tints the confirm button."""
        self.set_title(title)
        self._body.setText(body)
        self._danger = bool(danger)
        self._confirm_btn.setText(confirm_label)
        self.open_modal(launcher)
        # Safe default focus: Cancel owns Enter (a destructive confirm
        # should never fire from a bare Enter press).
        self._cancel_btn.setFocus()

    def _confirm(self) -> None:
        self.close_modal()
        self.confirmed.emit()

    def _place_panel(self) -> None:
        """Fit the fixed-size panel to the wrapped body (see prompt)."""
        super()._place_panel()
        try:
            width = self._panel.width()
            height = _HEADER_H + 2 * 18 + _FIT_BUFFER + _BODY_GAP
            heights = [
                _label_height(self._body, self._panel, width),
                self._actions.sizeHint().height(),
            ]
            visible = sum(1 for h in heights if h > 0)
            height += sum(heights) + _BODY_GAP * max(0, visible - 1)
            self._panel.setFixedHeight(max(120, height))
            self._place_panel_y()
        except Exception:
            pass

    def _place_panel_y(self) -> None:
        """Re-center the panel vertically after a height change."""
        try:
            area = self.rect()
            self._panel.move(self._panel.x(),
                             max(0, (area.height() - self._panel.height()) // 2))
        except Exception:
            pass

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Enter confirms when nothing focused consumes it first (the
        # focused Cancel button handles its own Return/Enter).
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = self.focusWidget()
            if focused is not self._cancel_btn:
                self._confirm()
                event.accept()
                return
        super().keyPressEvent(event)

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        t = tokens
        self._body.setFont(role_font(t, "body"))
        self._body.setStyleSheet(f"color: {t.text_secondary};")
        self._cancel_btn.set_tokens(t)
        self._cancel_btn.setFont(role_font(t, "body"))
        self._cancel_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            f" color: {t.text_secondary}; border: 1px solid {t.input_border};"
            " border-radius: 8px; padding: 6px 16px; outline: 0; }"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        self._confirm_btn.set_tokens(t)
        self._confirm_btn.setFont(role_font(t, "body"))
        # Danger confirmations carry the danger token on the label AND
        # the painted red hover wash (ModalButton._danger drives the
        # wash color; flipped here so one button serves both modes).
        danger = bool(self._danger)
        try:
            self._confirm_btn._danger = danger
        except Exception:
            pass
        label = t.danger if danger else t.text_primary
        edge = "transparent" if danger else t.input_border
        self._confirm_btn.setStyleSheet(
            "QPushButton { background: transparent;"
            f" color: {label}; border: 1px solid {edge};"
            " border-radius: 8px; padding: 6px 16px; outline: 0; }"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
