"""Settings centered modal (M4.6.5). Library shell only.

Split-pane application settings on the shared modal family: nav rail
(Appearance, Interface, Shortcuts) + stacked content. Only supported
settings are implemented:

* Appearance: System / Dark / Light (System follows the OS live)
* Interface: motion + pressure response
* Shortcuts: registry-backed key bindings (search, record, clear, reset)

``SettingsModal.add_section(title)`` appends a nav entry + blank page
for future groups. No fictional settings are implemented.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.modal import CenteredModal, ModalButton
from app.theme.manager import ThemeManager, ThemeMode
from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["SettingsModal", "SETTINGS_W", "SETTINGS_H",
           "pressure_profile", "PROFILE_KEY", "DEFAULT_PROFILE"]

SETTINGS_W = 800
SETTINGS_H = 540

#: Persisted pressure-response profile name (B.2.3). "normal" is the
#: default for existing users; unknown stored values read back as
#: "normal". Independent from the per-tool Pressure toggle (curve
#: selection only — never enables pressure by itself).
PROFILE_KEY = "inkfall/m5/inking/profile"
DEFAULT_PROFILE = "normal"
PROFILE_CHOICES: tuple[str, ...] = ("soft", "normal", "hard")


def pressure_profile() -> str:
    """Current response profile name, sanitized (never raises)."""
    try:
        from PySide6.QtCore import QSettings

        value = str(QSettings().value(PROFILE_KEY, DEFAULT_PROFILE))
    except Exception:
        return DEFAULT_PROFILE
    return value if value in PROFILE_CHOICES else DEFAULT_PROFILE


def set_pressure_profile(profile: str) -> None:
    """Persist a response profile name (sanitized, never raises)."""
    try:
        from PySide6.QtCore import QSettings

        name = str(profile)
        QSettings().setValue(
            PROFILE_KEY, name if name in PROFILE_CHOICES else DEFAULT_PROFILE)
    except Exception:
        pass


class SettingsModal(CenteredModal):
    """Centered settings panel (split-pane, extensible, modal)."""

    def __init__(self, theme: ThemeManager, tokens: InkfallTokens, parent=None) -> None:
        self._theme = theme
        super().__init__(tokens, SETTINGS_W, SETTINGS_H, parent)
        self.set_title("Settings")

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(16)

        # -- nav rail --------------------------------------------------
        rail = QVBoxLayout()
        rail.setContentsMargins(0, 0, 0, 0)
        rail.setSpacing(4)
        self._rail = rail
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons: list = []
        self._pages = QStackedWidget()
        self._page_layouts: dict[str, QVBoxLayout] = {}
        for title in ("Appearance", "Interface"):
            self._add_nav_page(title, rail)
        rail.addStretch(1)
        rail_widget = QWidget()
        rail_widget.setLayout(rail)
        rail_widget.setFixedWidth(200)
        content.addWidget(rail_widget)

        # -- pages -------------------------------------------------------
        self._build_appearance_page()
        self._build_interface_page()
        shortcuts_layout = self.add_section("Shortcuts")
        from app.shortcuts_settings import build_shortcuts_page

        self._shortcuts_page = build_shortcuts_page(shortcuts_layout, self)
        content.addWidget(self._pages, stretch=1)
        self.body.addLayout(content, stretch=1)

        self._nav_buttons[0].setChecked(True)
        self._pages.setCurrentIndex(0)
        self._sync_state()
        self._theme.changed.connect(self._on_theme_changed)
        self._nav_group.buttonClicked.connect(lambda _b: self._sync_nav_styles())
        self.update_tokens(tokens)

    # -- structure -----------------------------------------------------------

    def _wire_press(self, btn) -> None:
        """Immediate press acknowledgment for modal controls (M4.6.9).

        Effect-driven opacity dip (self-clearing, parented to the
        button) alongside the `:pressed` stylesheet rule — no dead
        buttons, no layout shift.
        """
        try:
            from app import fx as _fx

            btn.pressed.connect(lambda: _fx.press_feedback(btn, 90))
        except Exception:
            pass

    def _add_nav_page(self, title: str, rail=None) -> QVBoxLayout:
        """Append a nav entry + blank stacked page; returns page layout."""
        btn = ModalButton(title, radius=8)
        btn.setCheckable(True)
        btn.setFixedHeight(36)
        index = len(self._nav_buttons)
        btn.clicked.connect(lambda _c=False, i=index: self._pages.setCurrentIndex(i))
        self._wire_press(btn)
        self._nav_group.addButton(btn)
        self._nav_buttons.append(btn)
        if rail is not None:
            # Insert above the bottom stretch.
            rail.insertWidget(max(0, len(self._nav_buttons) - 1), btn)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)
        self._pages.addWidget(page)
        self._page_layouts[title] = layout
        return layout

    def add_section(self, title: str) -> QVBoxLayout:
        """Extension point: nav entry + page for a future settings group."""
        return self._add_nav_page(title, self._rail)

    def _build_appearance_page(self) -> None:
        layout = self._page_layouts["Appearance"]
        # Insert above the bottom stretch.
        theme_title = QLabel("Theme")
        theme_title.setObjectName("inkfall-theme-title")
        layout.insertWidget(0, theme_title)
        self._theme_title = theme_title
        seg = QWidget()
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(0)
        self._seg = seg
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            (ThemeMode.SYSTEM, "System"),
            (ThemeMode.DARK, "Dark"),
            (ThemeMode.LIGHT, "Light"),
        ):
            btn = ModalButton(label, radius=6)
            btn.setCheckable(True)
            # M4.6.10: no FixedHeight — identical font/padding/sheet
            # already yields equal heights, and a fixed value fights
            # Qt's hint-tracked minimum (min>max conflict).
            self._mode_group.addButton(btn)
            self._mode_buttons[mode] = btn
            seg_layout.addWidget(btn, stretch=1)
            btn.clicked.connect(lambda _c=False, m=mode: self._theme.set_mode(m))
            self._wire_press(btn)
        layout.insertWidget(1, seg)
        note = QLabel("System follows your OS appearance automatically.")
        note.setObjectName("inkfall-theme-note")
        note.setWordWrap(True)
        layout.insertWidget(2, note)
        self._theme_note = note
        self._mode_group.buttonClicked.connect(lambda _b: self._sync_seg_styles())

    def _build_interface_page(self) -> None:
        layout = self._page_layouts["Interface"]
        motion_title = QLabel("Motion")
        motion_title.setObjectName("inkfall-motion-title")
        layout.insertWidget(0, motion_title)
        self._motion_title = motion_title
        seg = QWidget()
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(2, 2, 2, 2)
        seg_layout.setSpacing(0)
        self._motion_seg = seg
        self._motion_group = QButtonGroup(self)
        self._motion_group.setExclusive(True)
        self._motion_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("follow", "Follow system"),
            ("reduced", "Reduced"),
            ("full", "Full"),
        ):
            btn = ModalButton(label, radius=6)
            btn.setCheckable(True)
            self._motion_group.addButton(btn)
            self._motion_buttons[mode] = btn
            seg_layout.addWidget(btn, stretch=1)
            btn.clicked.connect(lambda _c=False, m=mode: self._set_motion(m))
            self._wire_press(btn)
        layout.insertWidget(1, seg)
        note = QLabel("Follow system respects your OS reduced-motion preference. "
                      "Reduced resolves animations immediately.")
        note.setObjectName("inkfall-motion-note")
        note.setWordWrap(True)
        layout.insertWidget(2, note)
        self._motion_note = note
        self._motion_group.buttonClicked.connect(lambda _b: self._sync_motion_styles())
        self._sync_motion_state()
        # -- Pressure response (B.2.3): curve selection only; never
        # enables pressure by itself (per-tool toggle owns that).
        profile_title = QLabel("Pressure response")
        profile_title.setObjectName("inkfall-profile-title")
        layout.insertWidget(3, profile_title)
        self._profile_title = profile_title
        pseg = QWidget()
        pseg_layout = QHBoxLayout(pseg)
        pseg_layout.setContentsMargins(2, 2, 2, 2)
        pseg_layout.setSpacing(0)
        self._profile_seg = pseg
        self._profile_group = QButtonGroup(self)
        self._profile_group.setExclusive(True)
        self._profile_buttons: dict[str, QPushButton] = {}
        for choice, label in (
            ("soft", "Soft"),
            ("normal", "Normal"),
            ("hard", "Hard"),
        ):
            btn = ModalButton(label, radius=6)
            btn.setCheckable(True)
            btn.setAccessibleName(f"Pressure response {label}")
            self._profile_group.addButton(btn)
            self._profile_buttons[choice] = btn
            pseg_layout.addWidget(btn, stretch=1)
            btn.clicked.connect(
                lambda _c=False, c=choice: self._set_profile(c))
            self._wire_press(btn)
        layout.insertWidget(4, pseg)
        pnote = QLabel("Soft answers light pressure sooner; Hard needs "
                       "firm pressure for full width. Applies to the next "
                       "stroke; the active stroke keeps its curve.")
        pnote.setObjectName("inkfall-profile-note")
        pnote.setWordWrap(True)
        layout.insertWidget(5, pnote)
        self._profile_note = pnote
        self._profile_group.buttonClicked.connect(
            lambda _b: self._sync_profile_styles())
        self._sync_profile_state()

    def _set_profile(self, choice: str) -> None:
        set_pressure_profile(choice)
        self._sync_profile_state()

    def _sync_profile_state(self) -> None:
        current = pressure_profile()
        for choice, btn in self._profile_buttons.items():
            try:
                btn.setChecked(choice == current)
            except Exception:
                pass
        self._sync_profile_styles()

    def _sync_profile_styles(self) -> None:
        try:
            t = self._tokens
        except Exception:
            return
        for btn in self._profile_buttons.values():
            active = btn.isChecked()
            fg = t.text_primary if active else t.text_muted
            btn.set_tokens(t)
            btn.setStyleSheet(
                f"QPushButton {{ color: {fg}; background-color: transparent;"
                f" border: 1px solid transparent; border-radius: 6px;"
                f" padding: 6px 0; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )

    def _set_motion(self, mode: str) -> None:
        try:
            from app import fx as _fx

            _fx.set_motion_mode(mode)
        except Exception:
            pass
        self._sync_motion_state()

    def _sync_motion_state(self) -> None:
        try:
            from app import fx as _fx

            current = _fx.motion_mode()
        except Exception:
            current = "follow"
        for mode, btn in self._motion_buttons.items():
            try:
                btn.setChecked(mode == current)
            except Exception:
                pass
        self._sync_motion_styles()

    def _sync_motion_styles(self) -> None:
        try:
            t = self._tokens
        except Exception:
            return
        for btn in self._motion_buttons.values():
            active = btn.isChecked()
            fg = t.text_primary if active else t.text_muted
            btn.set_tokens(t)
            btn.setStyleSheet(
                f"QPushButton {{ color: {fg}; background-color: transparent;"
                f" border: 1px solid transparent; border-radius: 6px;"
                f" padding: 6px 0; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )

    # -- state -----------------------------------------------------------------

    def _sync_state(self) -> None:
        for mode, btn in self._mode_buttons.items():
            btn.setChecked(self._theme.mode == mode)
        self._sync_seg_styles()

    def _on_theme_changed(self, tokens) -> None:
        self.update_tokens(tokens)
        self._sync_state()

    def _sync_seg_styles(self) -> None:
        t = self._tokens
        for btn in self._mode_buttons.values():
            active = btn.isChecked()
            fg = t.text_primary if active else t.text_muted
            # M4.6.10: hover/press are painted by ModalButton progress.
            btn.set_tokens(t)
            btn.setStyleSheet(
                f"QPushButton {{ color: {fg}; background-color: transparent;"
                f" border: 1px solid transparent; border-radius: 6px;"
                f" padding: 6px 0; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )

    def _sync_nav_styles(self) -> None:
        t = self._tokens
        for btn in self._nav_buttons:
            btn.setFont(role_font(t, "body"))
            fg = t.text_primary if btn.isChecked() else t.text_muted
            btn.set_tokens(t)
            btn.setStyleSheet(
                f"QPushButton {{ color: {fg}; background-color: transparent;"
                f" border: 1px solid transparent; border-radius: 8px;"
                f" padding: 6px 12px; text-align: left; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle nav rail + pages (called by the modal base)."""
        t = tokens
        self._sync_nav_styles()
        self._theme_title.setFont(role_font(t, "section"))
        self._theme_title.setStyleSheet(
            f"color: {t.text_primary}; margin-bottom: 4px;")
        self._theme_note.setFont(role_font(t, "meta"))
        self._theme_note.setStyleSheet(f"color: {t.text_muted};")
        self._motion_title.setFont(role_font(t, "section"))
        self._motion_title.setStyleSheet(
            f"color: {t.text_primary}; margin-bottom: 4px;")
        self._motion_note.setFont(role_font(t, "meta"))
        self._motion_note.setStyleSheet(f"color: {t.text_muted};")
        self._profile_title.setFont(role_font(t, "section"))
        self._profile_title.setStyleSheet(
            f"color: {t.text_primary}; margin-bottom: 4px;")
        self._profile_note.setFont(role_font(t, "meta"))
        self._profile_note.setStyleSheet(f"color: {t.text_muted};")
        self._seg.setStyleSheet(
            f"background-color: {t.input_background};"
            f" border: 1px solid {t.input_border}; border-radius: 8px;"
        )
        try:
            self._motion_seg.setStyleSheet(
                f"background-color: {t.input_background};"
                f" border: 1px solid {t.input_border}; border-radius: 8px;"
            )
        except Exception:
            pass
        try:
            self._profile_seg.setStyleSheet(
                f"background-color: {t.input_background};"
                f" border: 1px solid {t.input_border}; border-radius: 8px;"
            )
        except Exception:
            pass
        for _mode, btn in self._mode_buttons.items():
            btn.setFont(role_font(t, "body"))
            btn.set_tokens(t)
            try:
                btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
            except Exception:
                pass
        for _mode, btn in self._motion_buttons.items():
            btn.setFont(role_font(t, "body"))
            btn.set_tokens(t)
            try:
                btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
            except Exception:
                pass
        for _choice, btn in self._profile_buttons.items():
            btn.setFont(role_font(t, "body"))
            btn.set_tokens(t)
            try:
                btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
            except Exception:
                pass
        self._sync_seg_styles()
        self._sync_motion_state()
        self._sync_profile_state()
        try:
            self._shortcuts_page.apply_tokens(tokens)
        except Exception:
            pass
        for nav in self._nav_buttons:
            try:
                nav.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
            except Exception:
                pass
