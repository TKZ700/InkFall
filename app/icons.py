"""Inkfall icon set (M4.5). No third-party dependency.

Small inline SVG icons with a consistent 1.5px stroke on a 24px grid,
tinted at request time so they stay crisp in both themes. Replaces all
unicode UI glyphs (no emoji, no font-dependent symbols).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

__all__ = ["icon", "icon_names", "pixmap", "tool_pixmap", "tool_icon",
           "ensure_contrast_fg", "contrast_ratio", "ink_center_offset"]

_STROKE = "fill='none' stroke='{c}' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'"

_PATHS: dict[str, str] = {
    "library": (
        "<rect x='4' y='4' width='16' height='16' rx='2.5'/>"
        "<path d='M9 4v16M4 9h5'/>"
    ),
    "clock": (
        "<circle cx='12' cy='12' r='8.2'/>"
        "<path d='M12 7.5V12l3 2'/>"
    ),
    "star": (
        "<path d='M12 4.2l2.5 5.2 5.7.7-4.2 4 1.1 5.6-5.1-2.8-5.1 2.8 1.1-5.6-4.2-4 5.7-.7z'/>"
    ),
    "star-filled": (
        "<path d='M12 4.2l2.5 5.2 5.7.7-4.2 4 1.1 5.6-5.1-2.8-5.1 2.8 1.1-5.6-4.2-4 5.7-.7z' "
        "fill='{c}' stroke='none'/>"
    ),
    "folder": (
        "<path d='M3.5 7.2c0-1 .8-1.9 1.9-1.9h4.4l2 2.4h6.8c1 0 1.9.8 1.9 1.9v7.3c0 1-.8 1.9-1.9 "
        "1.9H5.4c-1 0-1.9-.8-1.9-1.9z'/>"
    ),
    "folder-plus": (
        "<path d='M3.5 7.2c0-1 .8-1.9 1.9-1.9h4.4l2 2.4h6.8c1 0 1.9.8 1.9 1.9v7.3c0 1-.8 1.9-1.9 "
        "1.9H5.4c-1 0-1.9-.8-1.9-1.9z'/>"
        "<path d='M12 10.4v5M9.5 12.9h5'/>"
    ),
    "search": (
        "<circle cx='11' cy='11' r='6.5'/>"
        "<path d='M15.8 15.8L20.2 20.2'/>"
    ),
    "grid": (
        "<rect x='4' y='4' width='7' height='7' rx='1.5'/>"
        "<rect x='13' y='4' width='7' height='7' rx='1.5'/>"
        "<rect x='4' y='13' width='7' height='7' rx='1.5'/>"
        "<rect x='13' y='13' width='7' height='7' rx='1.5'/>"
    ),
    "list": (
        "<path d='M8.5 6.5H20M8.5 12H20M8.5 17.5H20'/>"
        "<circle cx='4.8' cy='6.5' r='1.1' fill='{c}' stroke='none'/>"
        "<circle cx='4.8' cy='12' r='1.1' fill='{c}' stroke='none'/>"
        "<circle cx='4.8' cy='17.5' r='1.1' fill='{c}' stroke='none'/>"
    ),
    "plus": "<path d='M12 5v14M5 12h14'/>",
    "pin": (
        "<path d='M9 4h6l-1 7 3.5 3.5V16.5h-11V14.5L10 11z'/>"
        "<path d='M12 16.5V20'/>"
    ),
    "pin-filled": (
        "<path d='M9 4h6l-1 7 3.5 3.5V16.5h-11V14.5L10 11z' "
        "fill='{c}' stroke='none'/>"
        "<path d='M12 16.5V20'/>"
    ),
    "chevron-left": "<path d='M14.5 5.5L8 12l6.5 6.5'/>",
    "chevron-down": "<path d='M5.5 9.5L12 16l6.5-6.5'/>",
    "chevron-right": "<path d='M9.5 5.5L16 12l-6.5 6.5'/>",
    "x": "<path d='M6 6l12 12M18 6L6 18'/>",
    "check": "<path d='M4.5 12.5l5 5L19.5 7'/>",
    "retry": "<path d='M19.5 12a7.5 7.5 0 1 1-2.2-5.3'/><path d='M19.5 3.5v4h-4'/>",
    "fullscreen": "<path d='M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'/>",
    "sun": (
        "<circle cx='12' cy='12' r='4.2'/>"
        "<path d='M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 "
        "1.7M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7'/>"
    ),
    "moon": "<path d='M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z'/>",
    # M7.1.4.3 PDF appearance: inversion/contrast (half-filled disc) —
    # deliberately distinct from the moon at 16 px.
    "contrast": (
        "<circle cx='12' cy='12' r='8.2'/>"
        "<path d='M12 3.8a8.2 8.2 0 0 1 0 16.4z' fill='{c}' stroke='none'/>"
    ),
    "collapse": "<path d='M4 12h16M13 5.5L6.5 12l6.5 6.5'/>",
    "expand": "<path d='M4 12h16M11 5.5l6.5 6.5-6.5 6.5'/>",
    "doc": (
        "<path d='M6.5 3.5h7l4 4v13h-11z'/>"
        "<path d='M13.5 3.5v4h4'/>"
    ),
    "alert": (
        "<path d='M12 4L2.8 19.5h18.4z'/>"
        "<path d='M12 10v4'/>"
        "<circle cx='12' cy='16.8' r='1' fill='{c}' stroke='none'/>"
    ),
    "empty": (
        "<rect x='4' y='5' width='16' height='12' rx='2'/>"
        "<path d='M4 9.5h16M9 5v4.5'/>"
    ),
    "minus": "<path d='M5.5 12h13'/>",
    "maximize": "<rect x='6' y='6' width='12' height='12' rx='1.5'/>",
    "restore": (
        "<path d='M8.5 5.5h10v10'/>"
        "<rect x='5.5' y='8.5' width='10' height='10' rx='1.5'/>"
    ),
    "settings": (
        "<circle cx='12' cy='12' r='3.2'/>"
        "<path d='M19.2 12a7.2 7.2 0 0 0-.1-1.2l2-1.5-2-3.4-2.3 1a7.2 7.2 0 0 0-2-1.2L14.4 3h-4l-.4 "
        "2.7a7.2 7.2 0 0 0-2 1.2l-2.3-1-2 3.4 2 1.5a7.2 7.2 0 0 0 0 2.4l-2 1.5 2 3.4 2.3-1a7.2 "
        "7.2 0 0 0 2 1.2l.4 2.7h4l.4-2.7a7.2 7.2 0 0 0 2-1.2l2.3 1 2-3.4-2-1.5c.06-.4.1-.8.1-1.2z'/>"
    ),
    "pan": (
        "<path d='M8 12V6.5a1.5 1.5 0 0 1 3 0V11m0-5.5v-1a1.5 1.5 0 0 1 3 0V11m0-4.5a1.5 1.5 0 0 1 "
        "3 0V12m0-2.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6h-1.8a6 6 0 0 1-4.7-2.3L4 14.6a1.6 1.6 0 0 1 "
        "2.5-2L8 14z'/>"
    ),
    "lasso": (
        "<path d='M12 4.5c1.2 0 1.8.6 2.8.6 1.9 0 3.4 1.2 3.7 3 1.5.4 2.4 1.7 2 "
        "3.2.9 1.4.1 3.1-1.5 3.4-.3 1.7-1.9 2.9-3.6 2.6-.9 1.2-2.6 1.6-3.9.8-1.3.9-3.1.4-3.8-1-1.7-.2-2.9-1.7-2.7-3.4-1.2-1.2-1-3.2.4-3.8.4-1.7 "
        "2-2.9 3.7-2.6.7-.7 1.9-.6 2.9-.6z'/>"
    ),
    "selection": (
        # Rectangular selection: dashed marquee with a cursor-arrow
        # pointer whose tip crosses the marquee's lower-right corner —
        # one deliberate interaction, not layered symbols. The arrow
        # silhouette (not a hand) keeps it distinct from Pan at 20px;
        # near-white fill + theme-ink outline reads on either theme.
        "<rect x='2.5' y='3' width='13.5' height='10.5' rx='1.2' "
        "fill='none' stroke='{c}' stroke-width='1.6' "
        "stroke-dasharray='2.4 1.7'/>"
        "<path d='M15 12.5L15 20.5L17.2 18.4L18.4 21.2L20.2 20.3L19 17.5"
        "L21.6 17.5Z' "
        "fill='#FFFFFF' stroke='{c}' stroke-width='1.3' "
        "stroke-linejoin='round'/>"
    ),
    "pen": "<path d='M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19z'/><path d='M14.5 6.5l3 3'/>",
    "pen-nib": (
        "<path d='M14.8 3.2l6 6-8.2 8.2-4.6 1.6 1.6-4.6z'/>"
        "<path d='M13.2 8.3l2.6 2.6'/>"
        "<circle cx='9.4' cy='16.6' r='1.4' fill='{c}' stroke='none'/>"
    ),
    "marker": (
        # Drawboard-inspired wide chisel-tip highlighter: translucent rear
        # cap, broad filled barrel, flat chisel end, light section
        # separators. Fills use the configured slot color with an explicit
        # stroke='none' so the contrast/keyline pipeline still applies;
        # separators are interior detail strokes on top of the fill.
        # rotate(45) — not -45: y-down flips the lean, and only +45 puts
        # the chisel tip at the lower-left, leaning up to the upper-right.
        "<g transform='translate(12 12) rotate(45)'>"
        "<path d='M-3.5 -9H3.5V-5.5H-3.5Z' "
        "fill='{c}' fill-opacity='0.58' stroke='none'/>"
        "<path d='M-3.5 -5.5H3.5V5.2H-3.5Z' "
        "fill='{c}' stroke='none'/>"
        "<path d='M-3.5 5.2H3.5L2.25 9H-2.25Z' "
        "fill='{c}' stroke='none'/>"
        "<path d='M-3.5 -5.5H3.5 M-3.5 5.2H3.5' "
        "fill='none' stroke='#FFFFFF' stroke-opacity='0.85' "
        "stroke-width='0.9'/>"
        "</g>"
    ),
    "highlighter": (
        "<g transform='translate(12 12) rotate(45)'>"
        "<path d='M-3.5 -9H3.5V-5.5H-3.5Z' "
        "fill='{c}' fill-opacity='0.58' stroke='none'/>"
        "<path d='M-3.5 -5.5H3.5V5.2H-3.5Z' "
        "fill='{c}' stroke='none'/>"
        "<path d='M-3.5 5.2H3.5L2.25 9H-2.25Z' "
        "fill='{c}' stroke='none'/>"
        "<path d='M-3.5 -5.5H3.5 M-3.5 5.2H3.5' "
        "fill='none' stroke='#FFFFFF' stroke-opacity='0.85' "
        "stroke-width='0.9'/>"
        "</g>"
    ),
    "eraser": (
        "<path d='M7 15.5l7.5-7.5a1.5 1.5 0 0 1 2.1 0l2.4 2.4a1.5 1.5 0 0 1 0 2.1L13.5 18H9.5z'/>"
        "<path d='M7 15.5L4.8 17.7a1 1 0 0 0 .7 1.7h11'/>"
    ),
    # M7.1.4.3 eraser modes: the two user-facing line conventions, not a
    # tool pictogram — Stroke is a solid continuous line, Partial a dashed
    # one. Drawn at 24 px with a heavier stroke so both read at 14 px.
    "line-solid": "<path d='M4 12h16' stroke-width='2.2'/>",
    "line-dashed": (
        "<path d='M4 12h3.2M10.4 12h3.2M16.8 12h3.2' stroke-width='2.2'/>"
    ),
    "line": "<path d='M5 19L19 5'/><circle cx='5' cy='19' r='1.2' fill='{c}' stroke='none'/><circle cx='19' cy='5' r='1.2' fill='{c}' stroke='none'/>",
    "rectangle": "<rect x='4.5' y='7' width='15' height='10' rx='1.5'/>",
    "ellipse": "<ellipse cx='12' cy='12' rx='8' ry='5.5'/>",
    "arrow": "<path d='M4 12h14M13 6.5L18.5 12 13 17.5'/>",
    "polygon": "<path d='M12 4.5l7.5 5.5-2.9 8.5H7.4L4.5 10z'/>",
    "text": "<path d='M5 6.5h14M12 6.5V20'/>",
    "note": (
        "<path d='M6 4h11l3 3v13h-14z'/>"
        "<path d='M17 4v3h3'/>"
        "<path d='M9.5 12h5.5M9.5 15.5h3.5'/>"
    ),
    "trash": (
        "<path d='M5 7h14M10 5h4M8.5 7l.7 13h5.6l.7-13'/>"
        "<path d='M10.2 10.5v6M13.8 10.5v6'/>"
    ),
    "eye": (
        "<path d='M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z'/>"
        "<circle cx='12' cy='12' r='2.8'/>"
    ),
    "undo": "<path d='M8.5 6.5L4.5 10.5l4 4'/><path d='M4.5 10.5H15a5 5 0 0 1 0 10h-5'/>",
    "redo": "<path d='M15.5 6.5l4 4-4 4'/><path d='M19.5 10.5H9a5 5 0 0 0 0 10h5'/>",
    # M8: undo/redo/save capsule save half (disk glyph, same vocabulary).
    "save": (
        "<path d='M5 5h11l3 3v11H5z'/>"
        "<path d='M8 5v4h6.5V5'/>"
        "<path d='M8 19v-6h8v6'/>"
    ),
    # M6.8.2 clipboard slice: scissors, offset sheets, clipboard.
    "cut": (
        "<circle cx='6.6' cy='7' r='2.6'/>"
        "<circle cx='6.6' cy='17' r='2.6'/>"
        "<path d='M8.8 8.5L19.5 18M8.8 15.5L19.5 6'/>"
    ),
    "copy": (
        "<rect x='9' y='3.5' width='10.5' height='12.5' rx='2'/>"
        "<path d='M15 20.5H6.5a2 2 0 0 1-2-2V8'/>"
    ),
    "paste": (
        "<path d='M9.5 4.5H7.5A1.5 1.5 0 0 0 6 6v13a1.5 1.5 0 0 0 1.5 1.5h9A1.5 "
        "1.5 0 0 0 18 19V6a1.5 1.5 0 0 0-1.5-1.5h-2'/>"
        "<rect x='9.5' y='3' width='5' height='4' rx='1.2'/>"
    ),
    "fit-width": (
        "<path d='M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'/>"
        "<path d='M4 12h16'/>"
    ),
    "fit-page": "<rect x='7' y='4' width='10' height='16' rx='1.5'/><path d='M4 9V4h5M20 9V4h-5'/>",
    # M7.1.4.2 View controls: full-height fit, actual size, magnifier
    # zoom, and rotate glyphs (same inline-SVG stroke convention).
    "fit-height": (
        "<path d='M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5'/>"
        "<path d='M12 4v16'/>"
    ),
    "actual-size": (
        "<rect x='5.5' y='5.5' width='13' height='13' rx='1.5'/>"
        "<path d='M9 15l6-6'/>"
        "<circle cx='9' cy='9' r='1.1' fill='{c}' stroke='none'/>"
        "<circle cx='15' cy='15' r='1.1' fill='{c}' stroke='none'/>"
    ),
    "zoom-in": (
        "<circle cx='10.5' cy='10.5' r='6.2'/>"
        "<path d='M15.2 15.2L20.5 20.5'/>"
        "<path d='M7.8 10.5h5.4M10.5 7.8v5.4'/>"
    ),
    "zoom-out": (
        "<circle cx='10.5' cy='10.5' r='6.2'/>"
        "<path d='M15.2 15.2L20.5 20.5'/>"
        "<path d='M7.8 10.5h5.4'/>"
    ),
    "rotate-cw": (
        "<path d='M19.5 12a7.5 7.5 0 1 1-2.2-5.3'/>"
        "<path d='M19.5 3.5v4h-4'/>"
    ),
    "rotate-ccw": (
        "<path d='M4.5 12a7.5 7.5 0 1 0 2.2-5.3'/>"
        "<path d='M4.5 3.5v4h4'/>"
    ),
    "rotate-180": (
        "<path d='M4 12a8 8 0 0 1 16 0'/>"
        "<path d='M8 8.5L4.5 12 8 15.5'/>"
        "<path d='M16 8.5L19.5 12 16 15.5'/>"
    ),
}


def icon_names() -> tuple[str, ...]:
    """All available icon names."""
    return tuple(sorted(_PATHS))


def _svg(name: str, color: str, keyline: str | None = None) -> bytes:
    body = _PATHS[name].format(c=color)
    if keyline:
        # Outline filled silhouettes with the contrasting keyline so a
        # near-background fill stays visible; stroke-drawn icons are
        # unaffected (they carry no stroke='none' override).
        body = body.replace(
            "stroke='none'",
            f"stroke='{keyline}' stroke-width='1.3' "
            f"stroke-linejoin='round' stroke-opacity='0.75'",
        )
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' "
        f"viewBox='0 0 24 24' {_STROKE.format(c=color)}>{body}</svg>".encode()
    )


def _rel_luminance(color) -> float:
    """WCAG relative luminance of a QColor (0 = black, 1 = white)."""
    from PySide6.QtGui import QColor

    c = color if isinstance(color, QColor) else QColor(str(color))
    if not c.isValid():
        return 0.0

    def channel(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return (0.2126 * channel(c.red()) + 0.7152 * channel(c.green())
            + 0.0722 * channel(c.blue()))


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colors (>= 1.0)."""
    la = _rel_luminance(fg)
    lb = _rel_luminance(bg)
    lighter, darker = (la, lb) if la >= lb else (lb, la)
    return (lighter + 0.05) / (darker + 0.05)


#: Minimum icon-ink vs surface contrast before treatment kicks in.
MIN_ICON_CONTRAST = 2.2


def ensure_contrast_fg(fg_hex: str, bg_hex: str,
                       minimum: float = MIN_ICON_CONTRAST) -> str:
    """Shift ``fg_hex`` lightness away from ``bg_hex`` until readable.

    Hue is preserved; the shift is bounded so the configured tool color
    stays the primary identity. Falls back to black/white when the hue
    cannot reach the minimum on its own.
    """
    from PySide6.QtGui import QColor

    try:
        fg = QColor(str(fg_hex))
        if not fg.isValid():
            fg = QColor("#1b1e26")
    except Exception:
        fg = QColor("#1b1e26")
    if contrast_ratio(fg.name(), str(bg_hex)) >= minimum:
        return fg.name()
    try:
        bg_lum = _rel_luminance(bg_hex)
    except Exception:
        bg_lum = 0.0
    darken_first = bg_lum > 0.5
    # Bounded lightness walk: factors grow until the ratio is met. The
    # ladder reaches far enough that near-black/near-white hues resolve
    # to a visible tone of themselves (slate, not pole-flipped white)
    # before the black/white last resort below.
    for factor in (112, 130, 155, 185, 220, 260, 300):
        try:
            candidate = QColor(fg)
            candidate = (candidate.darker(factor) if darken_first
                         else candidate.lighter(factor))
        except Exception:
            continue
        if contrast_ratio(candidate.name(), str(bg_hex)) >= minimum:
            return candidate.name()
    # Last resort: whichever pole contrasts (never transparent/blank).
    white = contrast_ratio("#FFFFFF", str(bg_hex))
    black = contrast_ratio("#000000", str(bg_hex))
    return "#FFFFFF" if white >= black else "#000000"


def _keyline_for(background_hex: str) -> str:
    """Contrasting keyline tone for a surface (light on dark, dark on light)."""
    try:
        return "#FFFFFF" if _rel_luminance(background_hex) <= 0.5 else "#14161B"
    except Exception:
        return "#FFFFFF"


#: Cache of measured ink-center offsets per pixmap cache key.
_INK_OFFSET_CACHE: dict[tuple, tuple[float, float]] = {}


def ink_center_offset(pix, cache_key=None) -> tuple[float, float]:
    """Optical-centering delta for a rendered glyph pixmap.

    Measures actual painted alpha bounds (not the 24x24 viewBox) and
    returns ``(dx, dy)`` such that drawing the pixmap at
    ``(cx - w/2 + dx, cy - h/2 + dy)`` centers the *ink* on ``(cx, cy)``.
    Accounts for stroke width, keylines, and asymmetric silhouettes.
    Never raises; falls back to ``(0.0, 0.0)``.
    """
    try:
        if cache_key is not None and cache_key in _INK_OFFSET_CACHE:
            return _INK_OFFSET_CACHE[cache_key]
        w = int(pix.width())
        h = int(pix.height())
        if w <= 0 or h <= 0:
            return (0.0, 0.0)
        try:
            img = pix.toImage().convertToFormat(
                pix.toImage().Format.Format_ARGB32)
        except Exception:
            img = pix.toImage()
        x0, y0, x1, y1 = w, h, -1, -1
        for y in range(h):
            for x in range(w):
                try:
                    alpha = img.pixelColor(x, y).alpha()
                except Exception:
                    alpha = 0
                if alpha > 8:
                    if x < x0:
                        x0 = x
                    if x > x1:
                        x1 = x
                    if y < y0:
                        y0 = y
                    if y > y1:
                        y1 = y
        if x1 < x0 or y1 < y0:
            return (0.0, 0.0)
        dx = (w / 2.0) - ((x0 + x1 + 1) / 2.0)
        dy = (h / 2.0) - ((y0 + y1 + 1) / 2.0)
        # Clamp pathological shifts (mostly-empty pixmaps) to a sane range.
        dx = max(-8.0, min(8.0, dx))
        dy = max(-8.0, min(8.0, dy))
        result = (dx, dy)
        if cache_key is not None:
            try:
                if len(_INK_OFFSET_CACHE) < 4096:
                    _INK_OFFSET_CACHE[cache_key] = result
            except Exception:
                pass
        return result
    except Exception:
        return (0.0, 0.0)


def _render(name: str, color: str, size: int,
            keyline: str | None = None):
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(QByteArray(_svg(name, color, keyline)))
    image = QPixmap(size, size)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return image


def tool_pixmap(name: str, color: str = "#1b1e26",
                background: str = "#181C23", size: int = 20):
    """Contrast-safe tool glyph: configured color stays the identity.

    When the configured color is too close to ``background``, the tone
    is luminance-shifted along an extended ladder (hue preserved, so a
    dark slate stays slate instead of flipping to white) and filled
    silhouettes additionally gain a subtle contrasting keyline. Stroke
    geometry benefits from the shift directly. The result is never
    near-invisible, never blank, on either theme.
    """
    try:
        original_ok = contrast_ratio(str(color), str(background)) >= MIN_ICON_CONTRAST
    except Exception:
        original_ok = True
    fill = ensure_contrast_fg(str(color), str(background))
    keyline = None if original_ok else _keyline_for(str(background))
    try:
        return _render(name, fill, size, keyline)
    except Exception:
        return QPixmap()


def tool_icon(name: str, color: str = "#1b1e26",
              background: str = "#181C23", size: int = 20) -> QIcon:
    """QIcon variant of :func:`tool_pixmap`."""
    return QIcon(tool_pixmap(name, color, background, size))


def pixmap(name: str, color: str = "#a7aebb", size: int = 20) -> QPixmap:
    """Render an icon to a pixmap (for delegates and custom painting)."""
    try:
        return _render(name, color, size)
    except Exception:
        return QPixmap()


def icon(name: str, color: str = "#a7aebb", size: int = 20) -> QIcon:
    """Build a QIcon for buttons and actions."""
    return QIcon(pixmap(name, color, size))


def _brand_dir() -> object:
    from app.resources import resource_path

    return resource_path("assets", "icons")


def app_icon() -> QIcon:
    """The canonical Inkfall brand mark (M4.6.5: user-supplied PNG).

    Uses ``assets/icons/new_icon.png`` exactly as supplied — no
    restyle, no recolor, aspect preserved. Builds a multi-resolution
    QIcon (16–256px) so the mark stays crisp at 100/125/150% scaling.
    Falls back to the legacy ``inkfall.ico`` bundle, then null.
    Never raises.
    """
    try:
        from PySide6.QtGui import QPixmap

        brand = _brand_dir()
        png = brand / "new_icon.png"
        if png.is_file():
            base = QPixmap(str(png))
            if not base.isNull():
                icon_out = QIcon()
                for size in (16, 20, 24, 32, 48, 64, 128, 256):
                    scaled = base.scaled(
                        size, size, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    icon_out.addPixmap(scaled)
                if not icon_out.isNull():
                    return icon_out
        ico = brand / "inkfall.ico"
        if ico.is_file():
            loaded = QIcon(str(ico))
            if not loaded.isNull():
                return loaded
    except Exception:
        pass
    return QIcon()
