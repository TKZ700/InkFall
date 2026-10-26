"""Settings → Shortcuts section (registry-backed, immediate apply).

Presentation only: every value is read from / written to
:mod:`app.shortcuts`. Rows never hold their own notion of a default, so
the registry stays the single source of truth.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.modal import ModalButton
from app.shortcuts import (
    CATEGORY_ORDER,
    CATALOG,
    ActionSpec,
    canonical_sequence,
    display_sequence,
    registry,
)
from app.theme.typography import role_font

__all__ = ["ShortcutsPage", "build_shortcuts_page"]


class _ShortcutRow(QWidget):
    """One action: name, current keys, recorder, clear, inline feedback."""

    def __init__(self, spec: ActionSpec, parent=None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._tokens = None
        self._committing = False
        self._pending_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(2)

        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)

        self._name = QLabel(spec.title)
        self._name.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Preferred)
        line.addWidget(self._name, stretch=1)

        self._legacy = QLabel("")
        self._legacy.setVisible(False)
        line.addWidget(self._legacy)

        self._value = QPushButton("Unassigned")
        self._value.setCursor(Qt.CursorShape.PointingHandCursor)
        self._value.setMinimumWidth(130)
        self._value.clicked.connect(self._begin_record)
        line.addWidget(self._value)

        self._editor = QKeySequenceEdit()
        self._editor.setMinimumWidth(170)
        self._editor.setVisible(False)
        self._editor.editingFinished.connect(self._commit)
        line.addWidget(self._editor)

        self._clear = QPushButton("Clear")
        self._clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear.clicked.connect(self._clear_binding)
        line.addWidget(self._clear)

        layout.addLayout(line)

        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        if spec.available:
            self._value.setVisible(True)
        else:
            self._value.setVisible(False)
            self._editor.setVisible(False)
            self._clear.setVisible(False)

    # -- state -----------------------------------------------------------

    @property
    def title(self) -> str:
        return self._spec.title

    @property
    def category(self) -> str:
        return self._spec.category

    def refresh(self) -> None:
        if self._committing:
            return
        spec = self._spec
        reg = registry()
        if not spec.available:
            self._value.setVisible(False)
            self._editor.setVisible(False)
            self._clear.setVisible(False)
            self._legacy.setVisible(False)
            self._show_note(
                spec.note or "Not implemented", kind="muted")
            return

        self._editor.setVisible(False)
        self._value.setVisible(True)
        raw = reg.binding(spec.id)
        shown = display_sequence(raw) if raw else ""
        invalid = reg.invalid_for(spec.id)
        if invalid and not shown:
            shown = str(invalid)
        self._value.setText(shown or "Unassigned")
        self._clear.setVisible(bool(raw))

        legacy_text = ""
        if spec.legacy:
            legacy_text = "Also reserved: " + " / ".join(
                display_sequence(sequence) for sequence in spec.legacy)
        self._legacy.setText(legacy_text)
        self._legacy.setVisible(bool(legacy_text))

        conflict = reg.conflict_for(spec.id)
        if self._pending_error:
            self._show_note(self._pending_error, kind="error")
        elif conflict:
            self._show_note(
                f"Conflicts with {conflict} — this binding is disabled. "
                "Record a different shortcut or clear it.",
                kind="error")
        elif invalid:
            self._show_note(
                "Invalid saved shortcut — record a new one or clear it.",
                kind="error")
        else:
            self._note.setVisible(False)
        self._apply_value_style()

    # -- recording -------------------------------------------------------

    def _begin_record(self) -> None:
        reg = registry()
        raw = reg.binding(self._spec.id)
        self._pending_error = ""
        self._value.setVisible(False)
        self._editor.setVisible(True)
        if raw:
            self._editor.setKeySequence(QKeySequence(raw))
        else:
            self._editor.clear()
        self._editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _commit(self) -> None:
        if self._committing:
            return
        self._committing = True
        try:
            keys = self._editor.keySequence()
            text = (keys.toString(QKeySequence.SequenceFormat.PortableText)
                    if not keys.isEmpty() else "")
            if not text:
                self._pending_error = (
                    "Press a key combination, or use Clear to unassign.")
                self._show_note(self._pending_error, kind="error")
                return
            if (canonical_sequence(text)
                    == canonical_sequence(registry().binding(self._spec.id))):
                self._pending_error = ""
                self._note.setVisible(False)
                return
            ok, message = registry().set_binding(self._spec.id, text)
            if ok:
                self._pending_error = ""
                self._note.setVisible(False)
            else:
                self._pending_error = message or "That shortcut is not available."
                self._show_note(self._pending_error, kind="error")
        finally:
            self._committing = False
            self._editor.setVisible(False)
            self._value.setVisible(True)
            self.refresh()

    def _clear_binding(self) -> None:
        self._pending_error = ""
        registry().clear_binding(self._spec.id)
        self.refresh()

    # -- presentation ----------------------------------------------------

    def _show_note(self, text: str, kind: str = "muted") -> None:
        self._note.setText(text)
        self._note.setVisible(bool(text))
        if self._tokens is not None:
            color = (self._tokens.danger if kind == "error"
                     else self._tokens.text_muted)
            self._note.setStyleSheet(f"color: {color};")

    def _apply_value_style(self) -> None:
        t = self._tokens
        if t is None:
            return
        raw = registry().binding(self._spec.id)
        color = t.text_primary if raw else t.text_muted
        self._value.setStyleSheet(
            f"QPushButton {{ color: {color};"
            f" background-color: {t.input_background};"
            f" border: 1px solid {t.input_border}; border-radius: 6px;"
            f" padding: 5px 10px; outline: 0; }}"
            f"QPushButton:hover {{ border: 1px solid {t.input_focus}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        self._clear.setStyleSheet(
            f"QPushButton {{ color: {t.text_muted}; background: transparent;"
            f" border: 1px solid transparent; border-radius: 6px;"
            f" padding: 7px 10px; outline: 0; }}"
            f"QPushButton:hover {{ color: {t.text_primary}; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )

    def apply_tokens(self, tokens) -> None:
        self._tokens = tokens
        self._name.setFont(role_font(tokens, "body"))
        self._name.setStyleSheet(f"color: {tokens.text_primary};")
        self._legacy.setFont(role_font(tokens, "meta"))
        self._legacy.setStyleSheet(f"color: {tokens.text_muted};")
        self._note.setFont(role_font(tokens, "meta"))
        self._editor.setStyleSheet(
            f"QKeySequenceEdit {{ color: {tokens.text_primary};"
            f" background-color: {tokens.input_background};"
            f" border: 1px solid {tokens.input_focus}; border-radius: 6px;"
            f" padding: 2px 6px; }}"
            f"QKeySequenceEdit QLineEdit {{ background: transparent;"
            f" border: none; color: {tokens.text_primary}; }}"
        )
        self._value.setFont(role_font(tokens, "body"))
        self._clear.setFont(role_font(tokens, "meta"))
        self.refresh()


class ShortcutsPage(QWidget):
    """Searchable, categorized shortcut list with reset + inline errors."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tokens = None
        self._rows: list[_ShortcutRow] = []
        self._groups: list[tuple[QLabel, list[_ShortcutRow]]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Search shortcuts…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        header.addWidget(self._filter, stretch=1)
        self._reset = ModalButton("Reset to Defaults", radius=8)
        self._reset.clicked.connect(self._reset_defaults)
        try:
            from app import fx as _fx

            self._reset.pressed.connect(
                lambda: _fx.press_feedback(self._reset, 90))
        except Exception:
            pass
        header.addWidget(self._reset)
        outer.addLayout(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 0, 6, 0)
        self._content_layout.setSpacing(2)
        self._scroll.setWidget(content)
        outer.addWidget(self._scroll, stretch=1)

        self._footer = QLabel(
            "Copy, Cut, Paste and Select All keep their standard editor "
            "shortcuts. Shortcuts are ignored while typing in a text field, "
            "while a modal is open, or while recording.")
        self._footer.setWordWrap(True)
        outer.addWidget(self._footer)

        grouped: dict[str, list[ActionSpec]] = {}
        for spec in CATALOG:
            grouped.setdefault(spec.category, []).append(spec)
        for category in CATEGORY_ORDER:
            specs = grouped.get(category)
            if not specs:
                continue
            if self._content_layout.count():
                spacer = QWidget()
                spacer.setFixedHeight(6)
                self._content_layout.addWidget(spacer)
            heading = QLabel(category)
            self._content_layout.addWidget(heading)
            rows: list[_ShortcutRow] = []
            for spec in specs:
                row = _ShortcutRow(spec, self)
                self._content_layout.addWidget(row)
                self._rows.append(row)
                rows.append(row)
            self._groups.append((heading, rows))
        self._content_layout.addStretch(1)

        registry().bindingsChanged.connect(self.refresh)
        self.refresh()

    # -- behavior --------------------------------------------------------

    def _reset_defaults(self) -> None:
        registry().reset_defaults()
        self.refresh()

    def _apply_filter(self, text: str) -> None:
        needle = str(text or "").strip().lower()
        for heading, rows in self._groups:
            visible = 0
            for row in rows:
                title = row.title.lower()
                category = row.category.lower()
                match = not needle or needle in title or needle in category
                row.setVisible(match)
                if match:
                    visible += 1
            heading.setVisible(visible > 0)

    def refresh(self) -> None:
        for row in self._rows:
            row.refresh()
        self._apply_filter(self._filter.text())

    # -- presentation ----------------------------------------------------

    def apply_tokens(self, tokens) -> None:
        self._tokens = tokens
        self._filter.setFont(role_font(tokens, "body"))
        self._filter.setStyleSheet(
            f"QLineEdit {{ color: {tokens.text_primary};"
            f" background-color: {tokens.input_background};"
            f" border: 1px solid {tokens.input_border}; border-radius: 8px;"
            f" padding: 6px 10px; selection-background-color: "
            f"{tokens.accent_soft}; }}"
            f"QLineEdit:focus {{ border: 1px solid {tokens.input_focus}; }}"
        )
        self._reset.set_tokens(tokens)
        self._reset.setFont(role_font(tokens, "body"))
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        for heading, _rows in self._groups:
            heading.setFont(role_font(tokens, "section"))
            heading.setStyleSheet(
                f"color: {tokens.text_primary}; margin-top: 4px;")
        self._footer.setFont(role_font(tokens, "meta"))
        self._footer.setStyleSheet(f"color: {tokens.text_muted};")
        for row in self._rows:
            row.apply_tokens(tokens)


def build_shortcuts_page(layout, parent=None) -> ShortcutsPage:
    """Attach a ShortcutsPage to an existing settings page layout."""
    page = ShortcutsPage(parent)
    layout.insertWidget(max(0, layout.count() - 1), page, 1)
    return page
