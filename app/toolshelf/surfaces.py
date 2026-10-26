"""Shared circular-surface language for the toolbar's controls (M6.9.5).

ONE statement of the selected / hover / pressed fills, used by both the
tool slot buttons (:mod:`app.toolshelf.slot_button`) and the selected-tool
capsule (:mod:`app.toolshelf.capsule`), so the two controls can never
drift apart again.

The language is Drawboard's: FILLS, never borders.

* **selected** — one opaque near-black disc (:func:`selected_surface`).
  The same surface is used for the tool nested in the capsule and for a
  selected tool that has no capsule at all (Pan / Selection).
* **hover** — a restrained wash of the theme's own ink over that shape
  (:func:`hover_wash`), read against whatever is behind it: the shelf
  surface, the black selected disc, or the capsule's body. Never an
  outline, an accent ring or a circular stroke.
* **pressed / open** — the same wash one step stronger. No new shape.

Removed by this module: the accent-soft selection wash + accent edge on the
slot button, the accent rim on the slot button's hover, and the capsule's
pressed accent ring. Those were three different border answers to one
question, and the specification asks for none of them.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from app.theme.tokens import InkfallTokens, token_rgba

__all__ = [
    "SELECTED_TOKENS",
    "INK_TOKENS",
    "HOVER_ALPHA",
    "PRESSED_ALPHA",
    "token_color",
    "luminance",
    "mix",
    "blend_over",
    "theme_extreme",
    "selected_surface",
    "ink_color",
    "hover_wash",
]

#: Candidate tokens for the selected surface: the deepest the active theme
#: offers, so the selected disc is near-black in BOTH themes. The dark
#: theme resolves to ``canvas_deep``; the light theme's surfaces are all
#: pale, so it resolves to ``text_primary``.
SELECTED_TOKENS = ("canvas_deep", "background", "surface", "surface_raised",
                   "surface_overlay", "text_primary")
#: Candidate tokens for the lightest ink — what must stay readable on that
#: near-black disc (the capsule's three dots).
INK_TOKENS = ("text_primary", "surface_raised", "background")

#: Strength of the ink wash for hover and for pressed/open. Low enough to
#: stay restrained, high enough to read on the shelf surface AND on the
#: black selected disc (the same alpha therefore works everywhere).
HOVER_ALPHA = 0.09
PRESSED_ALPHA = 0.18


def token_color(tokens: InkfallTokens, name: str, fallback: tuple) -> QColor:
    """A token as ``QColor`` (rgba() strings included), else ``fallback``."""
    try:
        r, g, b, a = token_rgba(getattr(tokens, name))
        color = QColor(int(r), int(g), int(b),
                       max(0, min(255, int(round(a * 255)))))
        if color.isValid():
            return color
    except Exception:
        pass
    try:
        color = QColor(str(getattr(tokens, name)))
        if color.isValid():
            return color
    except Exception:
        pass
    return QColor(*fallback)


def luminance(color: QColor) -> float:
    return (0.2126 * color.redF() + 0.7152 * color.greenF()
            + 0.0722 * color.blueF())


def mix(base: QColor, top: QColor, t: float) -> QColor:
    """Linear blend ``base -> top`` by ``t`` (0..1), alpha included."""
    try:
        t = max(0.0, min(1.0, float(t)))
    except (TypeError, ValueError):
        t = 0.0
    return QColor(
        int(round(base.red() + (top.red() - base.red()) * t)),
        int(round(base.green() + (top.green() - base.green()) * t)),
        int(round(base.blue() + (top.blue() - base.blue()) * t)),
        int(round(base.alpha() + (top.alpha() - base.alpha()) * t)),
    )


def blend_over(base: QColor, top: QColor) -> QColor:
    """Composite ``top`` (at its own alpha) over opaque ``base``.

    The same arithmetic Qt applies when an alpha wash is painted over an
    opaque fill — exposed so tests can state the expected colour instead
    of hard-coding one.
    """
    a = top.alphaF()
    return QColor(
        int(round(top.red() * a + base.red() * (1.0 - a))),
        int(round(top.green() * a + base.green() * (1.0 - a))),
        int(round(top.blue() * a + base.blue() * (1.0 - a))),
        255,
    )


def theme_extreme(tokens: InkfallTokens, names: tuple,
                  darkest: bool, fallback: tuple) -> QColor:
    """The darkest (or lightest) opaque colour among ``names``."""
    best: QColor | None = None
    best_key = 0.0
    for name in names:
        try:
            color = token_color(tokens, name, fallback)
        except Exception:
            continue
        key = luminance(color)
        if best is None or (key < best_key if darkest else key > best_key):
            best, best_key = color, key
    out = QColor(best) if best is not None else QColor(*fallback)
    out.setAlpha(255)
    return out


def selected_surface(tokens: InkfallTokens) -> QColor:
    """The opaque near-black disc of the selected state (both themes)."""
    face = theme_extreme(tokens, SELECTED_TOKENS, True, (9, 11, 15, 255))
    face.setAlpha(255)
    return face


def ink_color(tokens: InkfallTokens) -> QColor:
    """The theme's lightest ink, opaque (the capsule's three dots)."""
    return theme_extreme(tokens, INK_TOKENS, False, (244, 245, 248, 255))


def hover_wash(tokens: InkfallTokens, pressed: bool = False) -> QColor:
    """The hover / pressed fill: the theme's ink at a low, fixed alpha.

    A translucent wash — not an outline — is what makes ONE rule read
    correctly on every surface this toolbar has (shelf, black disc,
    capsule body) in both themes.
    """
    wash = token_color(tokens, "text_primary", (244, 245, 248, 255))
    alpha = (PRESSED_ALPHA if pressed else HOVER_ALPHA) * 255.0
    wash.setAlpha(max(0, min(255, int(round(alpha)))))
    return wash
