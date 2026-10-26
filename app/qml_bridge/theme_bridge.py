"""ThemeBridge: exposes frozen InkfallTokens to QML as bindable properties."""

from PySide6.QtCore import QObject, Property, Signal

from app.theme.tokens import InkfallTokens


def _token_prop(name: str, notify_signal) -> Property:
    """Build a read-only string property for token ``name``.

    The signal must be passed in: class-body names are not visible
    inside nested function scopes, so referencing ``_changed`` from a
    helper defined outside the class body would raise NameError, and
    assigning Property objects after class creation leaves them
    unregistered with the meta-object (invisible to QML).
    """
    return Property(str, lambda self, _n=name: self._get(_n),
                    notify=notify_signal)


class ThemeBridge(QObject):
    """Read-only token projection. Call update_tokens() on theme switch."""

    _changed = Signal()

    surface = _token_prop("surface_raised", _changed)
    surfaceOverlay = _token_prop("surface_overlay", _changed)
    textPrimary = _token_prop("text_primary", _changed)
    textSecondary = _token_prop("text_secondary", _changed)
    textMuted = _token_prop("text_muted", _changed)
    inputBg = _token_prop("input_background", _changed)
    inputBorder = _token_prop("input_border", _changed)
    inputFocus = _token_prop("input_focus", _changed)
    accent = _token_prop("accent", _changed)
    danger = _token_prop("danger", _changed)
    fontFamily = _token_prop("font_family", _changed)

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._changed.emit()

    def _get(self, name: str) -> str:
        try:
            return str(getattr(self._tokens, name))
        except Exception:
            return "#000000"
