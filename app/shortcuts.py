"""Central keyboard shortcut registry.

One :class:`QAction` per application action owns that action's active key
sequences. Settings edits this registry instead of individual widgets, so
defaults live in exactly one place and a rebound shortcut genuinely
replaces the old binding (there is no second hardcoded handler).

Design notes
------------
* Action ids are stable dotted strings and are the persistence keys.
* Bindings are stored as one JSON object in QSettings (portable Qt text),
  matching the existing ``inkfall/...`` settings conventions.
* ``""`` means the user cleared the binding; a missing key means "use the
  default". Reset to Defaults removes the stored object entirely.
* Conflicting persisted mappings are never overwritten: the deterministic
  winner (catalog order) keeps the sequence, the loser is disabled and the
  Settings section shows the conflict.
* Dispatcher guards + enabled state protect text editing, modal dialogs,
  shortcut recording, and context-sensitive plain keys (Space/Delete/Esc).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QKeySequenceEdit,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

__all__ = [
    "ActionSpec",
    "CATALOG",
    "CATEGORY_ORDER",
    "SETTINGS_KEY",
    "ShortcutRegistry",
    "canonical_sequence",
    "display_sequence",
    "registry",
]

#: Single QSettings key holding every custom mapping as JSON.
SETTINGS_KEY = "inkfall/shortcuts/bindings"

_PORTABLE = QKeySequence.SequenceFormat.PortableText
_NATIVE = QKeySequence.SequenceFormat.NativeText

#: Friendlier Settings labels for Qt's short native key names.
_DISPLAY_NAMES: dict[str, str] = {
    "Esc": "Escape",
    "Del": "Delete",
    "PgUp": "Page Up",
    "PgDown": "Page Down",
    "Ins": "Insert",
}

#: Category presentation order in Settings.
CATEGORY_ORDER: tuple[str, ...] = (
    "Navigation", "Tools", "Editing", "Document", "Application")

#: Standard editor commands that stay native in text fields and are never
#: assignable to an application action.
_RESERVED_STANDARD: dict[str, str] = {
    "Ctrl+C": "Copy (standard editor shortcut)",
    "Ctrl+X": "Cut (standard editor shortcut)",
    "Ctrl+V": "Paste (standard editor shortcut)",
    "Ctrl+A": "Select All (standard editor shortcut)",
}


@dataclass(frozen=True)
class ActionSpec:
    """One registry entry (definition only; never holds widget state)."""

    id: str
    title: str
    category: str
    default: str = ""
    scope: str = "workspace"
    legacy: tuple[str, ...] = ()
    available: bool = True
    note: str = ""


def _tool_slot_actions() -> tuple[ActionSpec, ...]:
    out = []
    for number in range(1, 11):
        key = "0" if number == 10 else str(number)
        out.append(ActionSpec(
            id=f"tools.slot_{number}",
            title=f"Switch to Tool {number}",
            category="Tools",
            default=f"Ctrl+Alt+{key}",
            scope="workspace",
            note="Resolves the current toolbar slot ordering at press time.",
        ))
    return tuple(out)


CATALOG: tuple[ActionSpec, ...] = (
    # -- Navigation ------------------------------------------------------
    ActionSpec("view.zoom_in", "Zoom In", "Navigation", "Ctrl+=",
               legacy=("+", "=")),
    ActionSpec("view.zoom_out", "Zoom Out", "Navigation", "Ctrl+-",
               legacy=("-",)),
    ActionSpec("view.fit_width", "Fit Width", "Navigation", "Ctrl+Shift+W"),
    ActionSpec("view.fit_height", "Fit Height", "Navigation", "Ctrl+Shift+H"),
    ActionSpec("view.actual_size", "Actual Size", "Navigation", "Ctrl+0"),
    ActionSpec("view.next_page", "Next Page", "Navigation", "PgDown"),
    ActionSpec("view.prev_page", "Previous Page", "Navigation", "PgUp"),
    # -- Tools -----------------------------------------------------------
    *_tool_slot_actions(),
    ActionSpec("tools.toggle_pan", "Toggle Pan / Previous Tool", "Tools",
               "Space", scope="canvas"),
    # -- Editing ---------------------------------------------------------
    ActionSpec("edit.undo", "Undo", "Editing", "Ctrl+Z"),
    ActionSpec("edit.redo", "Redo", "Editing", "Ctrl+Shift+Z"),
    ActionSpec("edit.delete", "Delete Selected Annotation", "Editing",
               "Del", scope="canvas"),
    ActionSpec("edit.deselect", "Deselect / Cancel Selection", "Editing",
               "Esc", scope="deselect"),
    # -- Document --------------------------------------------------------
    ActionSpec("file.save", "Save", "Document", "Ctrl+S", scope="global"),
    ActionSpec("file.save_all", "Save All", "Document", "Ctrl+Shift+S"),
    ActionSpec("file.open", "Open…", "Document", "Ctrl+O", scope="global"),
    ActionSpec("file.import", "Import into Library…", "Document", "Ctrl+I",
               scope="global"),
    ActionSpec("file.next_pdf", "Next PDF", "Document", "Ctrl+Tab",
               scope="global",
               note="Cycles document tabs; Home is not a document."),
    ActionSpec("file.prev_pdf", "Previous PDF", "Document", "Ctrl+Shift+Tab",
               scope="global",
               note="Cycles document tabs; Home is not a document."),
    ActionSpec("file.close_tab", "Close Document Tab", "Document", "Ctrl+W",
               scope="global"),
    # -- Application -----------------------------------------------------
    ActionSpec("app.library", "Back to Library", "Application", "Ctrl+L",
               scope="global"),
    ActionSpec("app.theme", "Toggle Theme", "Application", "Ctrl+T",
               scope="global"),
    ActionSpec("app.fullscreen", "Fullscreen", "Application", "F11",
               scope="global"),
)

_SPECS_BY_ID: dict[str, ActionSpec] = {spec.id: spec for spec in CATALOG}


def canonical_sequence(sequence) -> str:
    """Portable canonical text for a key sequence; ``""`` if invalid/empty."""
    try:
        keys = (sequence if isinstance(sequence, QKeySequence)
                else QKeySequence(str(sequence)))
    except Exception:
        return ""
    if keys.isEmpty():
        return ""
    return keys.toString(_PORTABLE)


def display_sequence(sequence) -> str:
    """Human-facing native text for a stored sequence."""
    text = canonical_sequence(sequence)
    if not text:
        return ""
    try:
        native = QKeySequence(text).toString(_NATIVE)
    except Exception:
        native = text
    return _DISPLAY_NAMES.get(native, native)


def _spec(action_id: str) -> ActionSpec | None:
    return _SPECS_BY_ID.get(action_id)


def _focus_widget() -> QWidget | None:
    app = QApplication.instance()
    return None if app is None else app.focusWidget()


def _qml_item_is_text_input(item) -> bool:
    """True for editable QML text/numeric controls (and their children)."""
    node = item
    depth = 0
    while node is not None and depth < 64:
        try:
            name = node.metaObject().className()
        except Exception:
            name = ""
        for token in ("TextInput", "TextField", "TextEdit", "TextArea",
                      "SpinBox", "KeySequenceEdit"):
            if token in name:
                return True
        try:
            if (node.property("readOnly") is not None
                    and node.property("text") is not None):
                return True
        except Exception:
            pass
        try:
            node = node.parentItem()
        except Exception:
            node = None
        depth += 1
    return False


def _qml_widget_has_text_focus(widget) -> bool:
    """True only when this QQuickWidget hosts a focused text control.

    Non-editable QML chrome (swatches, buttons, the palette grid) does not
    disable application shortcuts.
    """
    try:
        from PySide6.QtQuickWidgets import QQuickWidget
    except Exception:
        return False
    if not isinstance(widget, QQuickWidget):
        return False
    try:
        window = widget.quickWindow()
        item = window.activeFocusItem() if window is not None else None
    except Exception:
        item = None
    return _qml_item_is_text_input(item)


def _is_typing_widget(widget) -> bool:
    """True when focus is inside any editable text/numeric control."""
    node = widget
    depth = 0
    while node is not None and depth < 64:
        if isinstance(node, (QLineEdit, QTextEdit, QPlainTextEdit,
                             QKeySequenceEdit, QAbstractSpinBox)):
            return True
        if _qml_widget_has_text_focus(node):
            return True
        try:
            node = node.parentWidget()
        except Exception:
            node = None
        depth += 1
    return False


class ShortcutRegistry(QObject):
    """Single source of truth for customizable application shortcuts."""

    bindingsChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._window = None
        self._viewer = None
        self._handlers: dict[str, Callable[[], None]] = {}
        self._actions: dict[str, QAction] = {}
        self._workspace_provider: Callable[[], bool] | None = None
        self._custom: dict[str, str] = self._load_custom()
        self._conflicts: dict[str, str] = {}
        self._invalid: dict[str, str] = {}
        self._installed = False
        self._focus_app = None
        self._recompute_conflicts()

    # -- persistence -----------------------------------------------------

    @staticmethod
    def _load_custom() -> dict[str, str]:
        try:
            from PySide6.QtCore import QSettings

            raw = QSettings().value(SETTINGS_KEY, "")
        except Exception:
            return {}
        if not isinstance(raw, str) or not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        clean: dict[str, str] = {}
        for action_id, value in data.items():
            if action_id in _SPECS_BY_ID and isinstance(value, str):
                clean[action_id] = value
        return clean

    def _save_custom(self) -> None:
        try:
            from PySide6.QtCore import QSettings

            settings = QSettings()
            if not self._custom:
                settings.remove(SETTINGS_KEY)
                return
            settings.setValue(
                SETTINGS_KEY, json.dumps(self._custom, ensure_ascii=True))
        except Exception:
            pass

    # -- public queries --------------------------------------------------

    def binding(self, action_id: str) -> str:
        """Effective stored sequence (``""`` = unassigned)."""
        spec = _spec(action_id)
        if spec is None:
            return ""
        if action_id in self._custom:
            raw = self._custom[action_id]
            if not raw:
                return ""
            return canonical_sequence(raw) or raw
        return spec.default if spec.available else ""

    def is_custom(self, action_id: str) -> bool:
        return action_id in self._custom

    def conflict_for(self, action_id: str) -> str | None:
        """Owner label when this persisted binding is blocked, else None."""
        return self._conflicts.get(action_id)

    def invalid_for(self, action_id: str) -> str | None:
        """Raw invalid persisted value, else None."""
        return self._invalid.get(action_id)

    def action(self, action_id: str) -> QAction | None:
        return self._actions.get(action_id)

    def custom_mappings(self) -> dict[str, str]:
        return dict(self._custom)

    # -- installation / dispatch ----------------------------------------

    def install(self, window, viewer, handlers: dict[str, Callable],
                workspace_provider: Callable[[], bool]) -> None:
        """Create one QAction per catalog entry and start dispatching."""
        self._teardown_actions()
        self._window = window
        self._viewer = viewer
        self._handlers = dict(handlers)
        self._workspace_provider = workspace_provider
        for spec in CATALOG:
            action = QAction(spec.title, window)
            action.setShortcutContext(
                Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(
                lambda _checked=False, sid=spec.id: self._dispatch(sid))
            window.addAction(action)
            self._actions[spec.id] = action
        self._installed = True
        app = QApplication.instance()
        if app is not None and self._focus_app is not app:
            try:
                app.focusChanged.connect(self._on_focus_changed)
                self._focus_app = app
            except Exception:
                pass
        try:
            viewer.installEventFilter(self)
        except Exception:
            pass
        try:
            viewer.documentStateChanged.connect(self.refresh)
        except Exception:
            pass
        try:
            viewer.tool_shelf.toolSelected.connect(
                lambda _slot=None: self.refresh())
        except Exception:
            pass
        self.rebuild()

    def _teardown_actions(self) -> None:
        if self._window is not None:
            for action in self._actions.values():
                try:
                    self._window.removeAction(action)
                    action.deleteLater()
                except Exception:
                    pass
            try:
                self._viewer.removeEventFilter(self)
            except Exception:
                pass
        self._actions.clear()
        self._installed = False

    def _on_focus_changed(self, *_args) -> None:
        self.refresh()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        try:
            if obj is self._viewer and event.type() in (
                    QEvent.Type.MouseButtonPress,
                    QEvent.Type.MouseButtonRelease,
                    QEvent.Type.KeyPress):
                self.refresh()
        except Exception:
            pass
        return False

    def _dispatch(self, action_id: str) -> None:
        spec = _spec(action_id)
        if spec is None or not spec.available or not self._installed:
            return
        if action_id in self._conflicts or action_id in self._invalid:
            return
        if not self._guard(spec):
            return
        handler = self._handlers.get(action_id)
        if handler is None:
            return
        try:
            handler()
        except Exception:
            pass
        self.refresh()

    # -- guards ----------------------------------------------------------

    def _workspace_active(self) -> bool:
        if self._workspace_provider is None:
            return False
        try:
            return bool(self._workspace_provider())
        except Exception:
            return False

    def _canvas_focus(self) -> bool:
        return self._viewer is not None and _focus_widget() is self._viewer

    def _window_fullscreen(self) -> bool:
        try:
            return bool(self._window is not None
                        and self._window.isFullScreen())
        except Exception:
            return False

    def _modal_open(self) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        try:
            if app.activeModalWidget() is not None:
                return True
        except Exception:
            pass
        # Inkfall's overlays are child widgets (not application-modal), so
        # also honor their visibility: shortcuts must not fire during the
        # open/close animation before focus has moved.
        window = self._window
        if window is not None:
            for name in ("_settings_modal", "_new_modal", "_blank_modal"):
                overlay = getattr(window, name, None)
                if overlay is None:
                    continue
                try:
                    if overlay.isVisible():
                        return True
                except Exception:
                    pass
        try:
            from app.modal import CenteredModal
        except Exception:
            return False
        nodes = [app.focusWidget(), app.focusObject()]
        for start in nodes:
            node = start if isinstance(start, QWidget) else None
            depth = 0
            while node is not None and depth < 64:
                if isinstance(node, CenteredModal):
                    return True
                node = node.parentWidget()
                depth += 1
        return False

    def _typing_focus(self) -> bool:
        app = QApplication.instance()
        if app is None:
            return False
        try:
            focus_object = app.focusObject()
        except Exception:
            focus_object = None
        if focus_object is not None and not isinstance(focus_object, QWidget):
            if _qml_item_is_text_input(focus_object):
                return True
        return _is_typing_widget(app.focusWidget())

    def _guard(self, spec: ActionSpec) -> bool:
        if not self._installed:
            return False
        if self._typing_focus() or self._modal_open():
            return False
        if spec.scope == "global":
            return True
        if not self._workspace_active():
            return False
        if spec.scope == "workspace":
            return True
        if self._viewer is None:
            return False
        if spec.scope == "canvas":
            return self._canvas_focus()
        if spec.scope == "deselect":
            # Escape keeps its context ladder. In fullscreen the action is
            # disabled so the existing window-level handling (exit
            # fullscreen) keeps priority; modals/text fields never reach
            # the registry either. Windowed canvas: cancel a move/deselect
            # first, then fall back to the existing Back-to-Library step.
            return (not self._window_fullscreen()
                    and self._canvas_focus())
        return True

    # -- conflict resolution --------------------------------------------

    def _recompute_conflicts(self) -> None:
        self._conflicts = {}
        self._invalid = {}
        owners: dict[str, str] = dict(_RESERVED_STANDARD)
        for spec in CATALOG:
            if not spec.available:
                continue
            raw = self.binding(spec.id)
            if not raw:
                continue
            primary = canonical_sequence(raw)
            if not primary:
                self._invalid[spec.id] = raw
                continue
            owner = owners.get(primary)
            if owner is not None and owner != spec.id:
                self._conflicts[spec.id] = self._owner_label(owner)
                continue
            owners.setdefault(primary, spec.id)
            for extra in spec.legacy:
                extra_key = canonical_sequence(extra)
                if not extra_key:
                    continue
                owner = owners.get(extra_key)
                if owner is None or owner == spec.id:
                    owners.setdefault(extra_key, spec.id)

    @staticmethod
    def _owner_label(owner: str) -> str:
        spec = _SPECS_BY_ID.get(owner)
        return spec.title if spec is not None else owner

    def _clash_for(self, action_id: str, sequence: str) -> str | None:
        """Owner label if ``sequence`` is already taken, else None."""
        reserved = _RESERVED_STANDARD.get(sequence)
        if reserved is not None:
            return reserved
        for spec in CATALOG:
            if spec.id == action_id or not spec.available:
                continue
            own = canonical_sequence(self.binding(spec.id))
            if own and own == sequence:
                return spec.title
            for extra in spec.legacy:
                if canonical_sequence(extra) == sequence:
                    return spec.title
        return None

    # -- mutation --------------------------------------------------------

    def set_binding(self, action_id: str,
                    sequence: str) -> tuple[bool, str]:
        """Validate + persist one binding. Returns (ok, message)."""
        spec = _spec(action_id)
        if spec is None or not spec.available:
            return False, "This action is not available."
        text = canonical_sequence(sequence)
        if not text:
            return False, "Press a key combination, or use Clear."
        clash = self._clash_for(action_id, text)
        if clash is not None:
            return False, f"Already used by {clash}."
        self._custom[action_id] = text
        self._save_custom()
        self.rebuild()
        return True, ""

    def clear_binding(self, action_id: str) -> None:
        """Unassign one action (kept as an explicit empty custom value)."""
        if _spec(action_id) is None:
            return
        self._custom[action_id] = ""
        self._save_custom()
        self.rebuild()

    def reset_defaults(self) -> None:
        """Remove every custom override and restore default dispatch."""
        self._custom = {}
        self._save_custom()
        self.rebuild()

    # -- rebuild / refresh ----------------------------------------------

    def rebuild(self) -> None:
        """Recompute conflicts and reinstall every QAction's sequences."""
        if not self._installed:
            self._recompute_conflicts()
            self.bindingsChanged.emit()
            return
        self._recompute_conflicts()
        for spec in CATALOG:
            action = self._actions.get(spec.id)
            if action is None:
                continue
            keys: list[QKeySequence] = []
            blocked = (spec.id in self._conflicts
                       or spec.id in self._invalid)
            if spec.available and not blocked:
                primary = canonical_sequence(self.binding(spec.id))
                if primary:
                    keys.append(QKeySequence(primary))
                seen = {key.toString(_PORTABLE) for key in keys}
                for extra in spec.legacy:
                    key = QKeySequence(extra)
                    canonical = key.toString(_PORTABLE)
                    if not key.isEmpty() and canonical not in seen:
                        keys.append(key)
                        seen.add(canonical)
            action.setShortcuts(keys)
        self.refresh()
        self.bindingsChanged.emit()

    def refresh(self) -> None:
        """Update each QAction's enabled state from the current context."""
        if not self._installed:
            return
        for spec in CATALOG:
            action = self._actions.get(spec.id)
            if action is None:
                continue
            if not spec.available:
                enabled = False
            elif spec.id in self._conflicts or spec.id in self._invalid:
                enabled = False
            else:
                try:
                    enabled = bool(self._guard(spec))
                except Exception:
                    enabled = False
            try:
                action.setEnabled(enabled)
            except Exception:
                pass


_REGISTRY: ShortcutRegistry | None = None


def registry() -> ShortcutRegistry:
    """Process-wide registry singleton."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ShortcutRegistry()
    return _REGISTRY
