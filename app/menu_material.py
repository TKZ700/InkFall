"""Frosted menu material: one shared acrylic treatment (M6.8.2 Stage 5.2).

The annotation and page context menus (and every submenu they own)
share a single compact, premium surface: a translucent *tinted*
background, rounded corners, one restrained hairline border, refined
hover/pressed/disabled states, and the platform's popup shadow.

Blur, honestly
--------------
An alpha colour and ``WA_TranslucentBackground`` are *not* background
blur — they only let whatever is behind show through. Real blur behind
the surface comes from the native backdrop attempted by
:func:`enable_native_backdrop` (Windows 11 ``DwmSetWindowAttribute``
with ``DWMWA_SYSTEMBACKDROP_TYPE``). When that is unavailable — older
Windows, a compositor without a usable backdrop, a non-Windows
platform — the menu keeps the *same* translucent tinted material
rather than falling back to the old opaque solid surface.

The stylesheet itself lives in the theme (see
:func:`menu_material_qss`, embedded by
:class:`~app.theme.manager.ThemeManager`) so the material follows the
active light/dark tokens; this module only tags menus, keeps their
popup windows translucent, and applies the native backdrop when it is
supported.

Qt-native only: no new dependency, no undocumented API.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu

__all__ = [
    "MENU_MATERIAL_PROPERTY",
    "InkfallMenu",
    "add_submenu",
    "apply_menu_material",
    "enable_native_backdrop",
    "menu_material_qss",
]

#: Dynamic property that opts a ``QMenu`` into the acrylic stylesheet
#: block (``QMenu[inkfallAcrylic="true"]`` in the app stylesheet).
MENU_MATERIAL_PROPERTY = "inkfallAcrylic"

#: ``DWMWA_SYSTEMBACKDROP_TYPE`` value for the acrylic "transient
#: window" material (Windows 11 22H2+).
_DWMSBT_TRANSIENTWINDOW = 3


def _alpha(token_value, alpha: float) -> str:
    """``rgba(r,g,b,a)`` from any theme token (Qt-free token parsing)."""
    from app.theme.tokens import token_rgba

    r, g, b, _a = token_rgba(str(token_value))
    return f"rgba({r},{g},{b},{alpha:.3f})"


def _is_dark(tokens) -> bool:
    try:
        return str(getattr(tokens, "name", "dark")).casefold() != "light"
    except Exception:
        return True


def menu_material_qss(tokens) -> str:
    """The acrylic ``QMenu`` block for one theme's tokens.

    Only menus tagged with :data:`MENU_MATERIAL_PROPERTY` use it, so the
    toolshelf/library menus keep the established solid surface. The
    attribute selector outranks the global ``QMenu`` rule (and each of
    its sub-controls) in the app stylesheet.
    """
    sel = f'QMenu[{MENU_MATERIAL_PROPERTY}="true"]'
    # Slightly more translucent in dark (where the tint stays legible) so
    # a native backdrop has something to show through.
    surface_alpha = 0.80 if _is_dark(tokens) else 0.86
    border_alpha = 0.70 if _is_dark(tokens) else 0.85
    return f"""
{sel} {{
  background-color: {_alpha(tokens.surface_raised, surface_alpha)};
  color: {tokens.text_primary};
  border: 1px solid {_alpha(tokens.border, border_alpha)};
  border-radius: 10px;
  padding: 6px;
}}
{sel}::item {{
  background: transparent; color: {tokens.text_primary};
  border-radius: 7px; padding: 6px 26px 6px 12px; margin: 1px 2px;
}}
{sel}::item:selected {{
  background-color: {_alpha(tokens.surface_overlay, 0.90)};
  color: {tokens.text_primary};
}}
{sel}::item:pressed {{
  background-color: {_alpha(tokens.accent, 0.24)};
  color: {tokens.text_primary};
}}
{sel}::item:disabled {{ color: {tokens.text_tertiary}; }}
{sel}::separator {{
  height: 1px; background-color: {tokens.divider}; margin: 5px 10px;
}}
{sel}::right-arrow {{ width: 9px; height: 9px; margin-right: 8px; }}
{sel}::icon {{ margin-right: 8px; }}
"""


def apply_menu_material(menu, tokens=None) -> bool:
    """Tag ``menu`` with the shared acrylic material; True when tagged.

    Sets the dynamic property, re-polishes the style so the stylesheet
    rule applies, and makes the popup window translucent so the rounded
    corners are genuinely transparent (and so a native backdrop, when
    available, is visible through the tint). Structure, icons, actions,
    shortcuts, animations and popup placement are untouched. Failure is
    contained: an untagged/opaque menu still works.
    """
    if menu is None:
        return False
    try:
        from PySide6.QtCore import Qt

        menu.setProperty(MENU_MATERIAL_PROPERTY, True)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        style = menu.style()
        if style is not None:
            style.unpolish(menu)
            style.polish(menu)
        if tokens is not None:
            menu._inkfall_material_tokens = tokens
        menu.update()
        return True
    except Exception:
        return False


def enable_native_backdrop(widget, dark: bool = True) -> bool:
    """Ask Windows for a real blurred backdrop behind ``widget``.

    Uses the documented Windows 11 DWM attribute
    ``DWMWA_SYSTEMBACKDROP_TYPE`` (acrylic "transient window" material),
    plus immersive dark mode and the rounded-corner preference. Returns
    ``True`` only when the platform accepted the backdrop attribute; any
    other result (older Windows, non-Windows, no usable compositor, or a
    window the DWM declines) leaves the translucent tinted material in
    place and reports ``False``. Never raises.
    """
    try:
        import ctypes
        import sys

        if not sys.platform.startswith("win"):
            return False
        try:
            hwnd = int(widget.winId())
        except Exception:
            return False
        if not hwnd:
            return False
        dwm = ctypes.WinDLL("dwmapi")
        set_attr = dwm.DwmSetWindowAttribute
        set_attr.restype = ctypes.c_long
        set_attr.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

        def _set(attribute: int, value: int) -> int:
            data = ctypes.c_int(int(value))
            return int(set_attr(ctypes.c_void_p(hwnd),
                                ctypes.c_uint(attribute),
                                ctypes.byref(data), ctypes.sizeof(data)))

        _set(20, 1 if dark else 0)          # DWMWA_USE_IMMERSIVE_DARK_MODE
        _set(33, 2)                         # ...CORNER_PREFERENCE = ROUND
        return _set(38, _DWMSBT_TRANSIENTWINDOW) == 0  # ...BACKDROP_TYPE
    except Exception:
        return False


class InkfallMenu(QMenu):
    """A ``QMenu`` painted with the shared frosted material.

    The native backdrop can only be applied once the popup owns a
    platform window, so it runs from ``showEvent`` (after ``popup()`` /
    ``show()`` has created that window). ``Qt.NoDropShadowWindowHint`` is
    never set, so the platform popup shadow that gives the surface its
    subtle lift is preserved.
    """

    def __init__(self, parent=None, tokens=None) -> None:
        super().__init__(parent)
        self._inkfall_material_tokens = tokens
        self._inkfall_backdrop = None
        apply_menu_material(self, tokens)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            tokens = self._inkfall_material_tokens
            self._inkfall_backdrop = enable_native_backdrop(
                self, dark=_is_dark(tokens) if tokens is not None else True)
        except Exception:
            self._inkfall_backdrop = False

    def backdrop_applied(self):
        """``True``/``False`` once shown, ``None`` before the first show."""
        return self._inkfall_backdrop


def add_submenu(menu, title: str, tokens=None):
    """Add a submenu sharing the parent's material; returns it.

    Submenus are ordinary popups too, so they must carry the same
    translucent material to avoid an opaque slab inside a frosted menu.
    """
    if tokens is None:
        tokens = getattr(menu, "_inkfall_material_tokens", None)
    sub = InkfallMenu(menu, tokens)
    sub.setTitle(title)
    try:
        menu.addMenu(sub)
    except Exception:
        pass
    return sub
