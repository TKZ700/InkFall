"""Theme manager: applies InkfallTokens to a QApplication (M4).

Qt-native: builds a QPalette plus a compact QSS string from tokens so
every widget shares one visual language. Persists the choice with
QSettings; transitions stay lightweight (no animation blocking I/O).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from app.theme.tokens import InkfallTokens, dark_tokens, light_tokens, tokens_for

__all__ = ["ThemeManager", "ThemeMode"]


class ThemeMode:
    """Theme identifiers."""

    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"


class ThemeManager(QObject):
    """Owns the active theme; emits changed(tokens) on switch.

    ``system`` mode resolves through ``QApplication.styleHints()``
    and follows the OS live via ``colorSchemeChanged``: only tokens
    are swapped (tokens-only path), so Library data, filters, the
    viewer session and thumbnails are never disturbed.
    """

    changed = Signal(object)

    SETTINGS_KEY = "inkfall/theme/mode"

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mode = ThemeMode.DARK
        self._tokens = dark_tokens()
        try:
            from PySide6.QtCore import QSettings

            saved = str(QSettings().value(self.SETTINGS_KEY, ThemeMode.DARK))
            if saved.casefold() in (ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.SYSTEM):
                self._mode = saved.casefold()
        except Exception:
            pass
        self._tokens = self._resolve_tokens()
        self._watch_system()

    @property
    def mode(self) -> str:
        """Active theme name (dark | light | system)."""
        return self._mode

    @property
    def tokens(self) -> InkfallTokens:
        """Active token set."""
        return self._tokens

    def set_mode(self, mode: str) -> InkfallTokens:
        """Switch theme and apply it application-wide (persisted)."""
        normalized = str(mode).casefold()
        if normalized not in (ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.SYSTEM):
            normalized = ThemeMode.DARK
        self._mode = normalized
        try:
            from PySide6.QtCore import QSettings

            QSettings().setValue(self.SETTINGS_KEY, normalized)
        except Exception:
            pass
        self._tokens = self._resolve_tokens()
        self.apply()
        self.changed.emit(self._tokens)
        return self._tokens

    def toggle(self) -> InkfallTokens:
        """Flip between light and dark (explicit modes only)."""
        return self.set_mode(ThemeMode.LIGHT if self._mode == ThemeMode.DARK else ThemeMode.DARK)

    @staticmethod
    def _system_is_dark() -> bool:
        """Ask Qt for the current OS color scheme (dark unless known light)."""
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                return True
            scheme = app.styleHints().colorScheme()
            return scheme != Qt.ColorScheme.Light
        except Exception:
            return True

    def _resolve_tokens(self) -> InkfallTokens:
        if self._mode == ThemeMode.LIGHT:
            return light_tokens()
        if self._mode == ThemeMode.SYSTEM:
            return dark_tokens() if self._system_is_dark() else light_tokens()
        return dark_tokens()

    def _watch_system(self) -> None:
        """Follow the OS scheme live while System mode is selected."""
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                return
            hints = app.styleHints()
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
        except Exception:
            pass

    def _on_system_scheme_changed(self, _scheme) -> None:
        """OS changed: re-resolve tokens only (no data/viewer disturbance)."""
        if self._mode != ThemeMode.SYSTEM:
            return
        self._tokens = self._resolve_tokens()
        self.apply()
        self.changed.emit(self._tokens)

    def apply(self) -> None:
        """Apply palette + stylesheet to the running QApplication (if any)."""
        app = QApplication.instance()
        if app is None:
            return
        # Application font first: the stack carries Inter (Latin) + the
        # bundled Vazirmatn (Persian/Arabic) for every widget and for
        # QML's default font. The stylesheet repeats the same stack.
        try:
            from app.fonts import apply_application_font

            apply_application_font(self._tokens.fs_body)
        except Exception:
            pass
        app.setPalette(self._palette())
        app.setStyleSheet(self._stylesheet())

    # -- Qt-native building blocks --------------------------------------

    def _palette(self) -> QPalette:
        t = self._tokens
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor(t.background))
        pal.setColor(QPalette.ColorRole.Base, QColor(t.input_background))
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor(t.surface))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(t.text_primary))
        pal.setColor(QPalette.ColorRole.Text, QColor(t.text_primary))
        pal.setColor(QPalette.ColorRole.Button, QColor(t.surface_raised))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(t.text_primary))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(t.accent))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(t.text_muted))
        return pal

    def _stylesheet(self) -> str:
        t = self._tokens
        try:
            from app.menu_material import menu_material_qss

            menu_material = menu_material_qss(t)
        except Exception:
            menu_material = ""
        return f"""
QWidget {{ font-family: {t.font_family}; font-size: {t.fs_body}pt; color: {t.text_primary}; }}
QMainWindow, QDialog {{ background-color: {t.background}; }}
QLineEdit {{
  background-color: {t.input_background}; color: {t.text_primary};
  border: 1px solid {t.input_border}; border-radius: 8px;
  min-height: {t.control_h_md - 12}px; padding: 4px 10px;
  selection-background-color: {t.accent}; }}
QLineEdit:focus {{ border: 1px solid {t.input_focus}; }}
QPushButton {{
  background-color: transparent; color: {t.text_primary};
  border: none; border-radius: 8px;
  min-height: {t.control_h_md - 8}px; padding: 4px 12px; }}
QPushButton:hover {{ background-color: {t.surface_overlay}; }}
QPushButton:pressed {{ background-color: {t.surface_raised}; }}
QPushButton:disabled {{ color: {t.text_tertiary}; }}
QPushButton:focus {{ outline: 0; }}
QToolButton:focus {{ outline: 0; }}
QMenu {{ background-color: {t.surface_raised}; color: {t.text_primary};
  border: 1px solid {t.border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ border-radius: 6px; padding: 6px 24px 6px 12px; }}
QMenu::item:selected {{ background-color: {t.surface_overlay}; }}
QMenu::separator {{ height: 1px; background-color: {t.divider}; margin: 4px 8px; }}
{menu_material}
QListView {{
  background: transparent; color: {t.text_primary};
  border: none; outline: 0; show-decoration-selected: 1; }}
QListView::item {{ border: none; padding: 2px; background: transparent; }}
QListView::item:hover {{ background: transparent; }}
QListView::item:selected {{ background: transparent; color: {t.text_primary}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 2px 2px 2px; }}
QScrollBar::handle:vertical {{ background: {t.text_tertiary}; border-radius: 3px; min-height: 36px; margin: 0 2px 0 2px; }}
QScrollBar::handle:vertical:hover {{ background: {t.text_muted}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 5px; min-width: 40px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
QSplitter::handle {{ background: {t.divider}; }}
QLabel[class="muted"] {{ color: {t.text_muted}; }}
QLabel[class="secondary"] {{ color: {t.text_secondary}; }}
"""

    @staticmethod
    def available_modes() -> tuple[str, str, str]:
        """Supported theme names."""
        return (ThemeMode.DARK, ThemeMode.LIGHT, ThemeMode.SYSTEM)
