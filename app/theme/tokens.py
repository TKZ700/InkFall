"""Inkfall design tokens (M4.6.3). Qt-free.

Layered neutral ramps (dark depth, light warmth), one premium indigo
interaction accent, gold reserved (never a general accent).
Composition constants follow an 8px rhythm; content has a maximum
width so large monitors keep an editorial composition.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["InkfallTokens", "dark_tokens", "light_tokens", "token_rgba"]


def token_rgba(value: str) -> tuple[int, int, int, float]:
    """Parse a color token into (r, g, b, alpha) — Qt-free.

    M4.6.8: ``QColor("rgba(r,g,b,a)")`` does NOT parse (invalid color
    reading back as opaque black), so no ``QColor(token)`` call may be
    trusted with ``rgba()`` tokens. This parses ``rgba(r,g,b,a)`` with
    float 0-1 alpha plus ``#rrggbb``/``#rrggbbaa`` hex; anything else
    falls back to opaque black.
    """
    import re

    s = (value or "").strip()
    m = re.fullmatch(
        r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", s)
    if m is not None:
        try:
            r = max(0, min(255, int(m.group(1))))
            g = max(0, min(255, int(m.group(2))))
            b = max(0, min(255, int(m.group(3))))
            a = float(m.group(4))
            if a > 1.0:  # tolerate 0-255 alpha as well
                a = a / 255.0
            return r, g, b, max(0.0, min(1.0, a))
        except Exception:
            pass
    h = s.lstrip("#")
    if len(h) in (6, 8):
        try:
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)
            a = int(h[6:8], 16) / 255.0 if len(h) == 8 else 1.0
            return r, g, b, a
        except Exception:
            pass
    return 0, 0, 0, 1.0


@dataclass(frozen=True, slots=True)
class InkfallTokens:
    """One complete theme: every widget reads from here."""

    name: str = "dark"
    # Layered surfaces, M4.6.4 depth ramp (rendered vs M4.6.3:
    # near-black foundation, controlled elevation steps):
    #   L0 background < L1 shell < L2 controls < L3 hover.
    background: str = "#0B0C0F"
    canvas_deep: str = "#07080A"
    surface: str = "#121419"
    surface_raised: str = "#181C23"
    surface_overlay: str = "#20252E"
    border: str = "#262C38"
    border_subtle: str = "#1A1F28"
    divider: str = "rgba(255,255,255,0.07)"
    # Centered modal system (M4.6.5): dimmed backdrop shared by Settings/New.
    modal_dim: str = "rgba(0,0,0,0.45)"
    modal_radius: int = 14
    # Text (neutral; accent never used for body copy)
    text_primary: str = "#F4F5F8"
    text_secondary: str = "#B4BAC7"
    text_muted: str = "#7E8698"
    text_tertiary: str = "#525861"
    # Interaction accent (M4.6.3 winner, re-judged from scratch on the
    # sidebar-free composition: rendered A/B/C at 1440/1920 dark+light.
    # A kept for visible New, dark/light coherence, app-icon harmony;
    # B enhanced blue-gray runner-up (dull light), C teal rejected
    # (brand break, tiring chroma). No D needed: A not weak.)
    accent: str = "#8B93E0"
    accent_hover: str = "#9AA2E8"
    accent_pressed: str = "#767CCB"
    accent_soft: str = "rgba(139,147,224,0.20)"
    danger: str = "#D97A76"
    # Pin/highlight semantic (gold reserved; pins render muted, never gold)
    star: str = "#D7A34A"
    gold_dim: str = "#8A6A34"
    # Cards: borderless editorial (mostly transparent; tints on demand)
    card_background: str = "#00000000"
    card_hover: str = "#20252E"
    card_selected: str = "rgba(139,147,224,0.16)"
    card_border: str = "#00000000"
    thumb_placeholder: str = "#14171E"
    thumb_edge: str = "#262C38"
    # Inputs
    input_background: str = "#121419"
    input_border: str = "#2A3140"
    input_focus: str = "#8B93E0"
    # Typography (bundled Inter Latin + bundled Vazirmatn Persian/Arabic,
    # both via app/fonts.py; Latin stays first so English is unchanged)
    font_family: str = "Inter, Vazirmatn, Segoe UI, Tahoma, sans-serif"
    font_fallback_arabic: str = "Vazirmatn, Inter, Tahoma, Segoe UI, sans-serif"
    font_mono: str = "Consolas, SFMono-Regular, monospace"
    fs_display: int = 21
    fs_section: int = 17
    fs_title: int = 15
    fs_body: int = 14
    fs_meta: int = 12
    fs_caption: int = 11
    fs_eyebrow: int = 10
    # Type weights (Qt QFont.Weight values)
    w_regular: int = 400
    w_medium: int = 500
    w_semibold: int = 600
    w_bold: int = 700
    # Spacing (8px rhythm) / radii / control metrics
    sp_xs: int = 4
    sp_sm: int = 8
    sp_md: int = 12
    sp_lg: int = 16
    sp_xl: int = 24
    sp_xxl: int = 32
    sp_xxxl: int = 48
    radius_sm: int = 6
    radius_md: int = 8
    radius_lg: int = 10
    control_h_sm: int = 28
    control_h_md: int = 34
    control_h_lg: int = 36
    icon_sm: int = 16
    icon_md: int = 20
    icon_lg: int = 24
    thumb_w: int = 180
    thumb_h: int = 264
    card_w: int = 190
    viewer_bar_h: int = 48
    # M4.6.2 responsive composition (8px rhythm, shared grid origin).
    # (M4.6.8: the former content_max_w cap is removed — the Library
    # body owns the full client width; the grid formula reflows live.)
    content_margin: int = 28
    grid_gap_h: int = 16
    grid_gap_v: int = 24
    card_min_w: int = 170
    card_max_w: int = 210
    section_gap: int = 24
    content_inset: int = 0
    # Transitions
    dur_fast_ms: int = 120
    dur_med_ms: int = 180
    dur_slow_ms: int = 240


def dark_tokens() -> InkfallTokens:
    """Layered dark workspace."""
    return InkfallTokens(name="dark")


_LIGHT = {
    "name": "light",
    "background": "#F4F3EE",
    "canvas_deep": "#E9E7E0",
    "surface": "#E9E7E0",
    "surface_raised": "#FFFFFF",
    "surface_overlay": "#EFEEE8",
    "border": "#D8D5CB",
    "border_subtle": "#E7E5DC",
    "divider": "#D8D5CB",
    "text_primary": "#1C1E24",
    "text_secondary": "#5B626E",
    "text_muted": "#8A9098",
    "text_tertiary": "#8F8878",
    "accent": "#4E57B8",
    "accent_hover": "#5D66C4",
    "accent_pressed": "#414A9C",
    "accent_soft": "rgba(78,87,184,0.14)",
    "danger": "#B94F48",
    "star": "#D7A34A",
    "gold_dim": "#8A6A34",
    "card_background": "#00000000",
    "card_hover": "#ECEAE3",
    "card_selected": "rgba(78,87,184,0.10)",
    "card_border": "#00000000",
    "thumb_placeholder": "#FAFAF7",
    "thumb_edge": "#D8D5CB",
    "input_background": "#FFFFFF",
    "input_border": "#D8D5CB",
    "input_focus": "#4E57B8",
}


def light_tokens() -> InkfallTokens:
    """Warm editorial light theme."""
    base = InkfallTokens(name="dark")
    return InkfallTokens(
        **{f: getattr(base, f) for f in base.__dataclass_fields__ if f not in _LIGHT},
        **_LIGHT,  # type: ignore[arg-type]
    )


_DEFAULTS: dict[str, InkfallTokens] = {}


def tokens_for(name: str) -> InkfallTokens:
    """Return cached tokens for a theme name."""
    key = name.casefold()
    if key not in _DEFAULTS:
        _DEFAULTS[key] = light_tokens() if key == "light" else dark_tokens()
    return _DEFAULTS[key]
