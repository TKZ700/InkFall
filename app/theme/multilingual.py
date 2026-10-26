"""Script-aware typography (M4.6.1). No binaries, no global RTL.

Latin-dominant text uses the Inter-first role stack (bundled via
app/fonts.py); Persian/Arabic runs use Vazirmatn-first. Mixed strings
are decided by dominant-script scoring with a 0.5 threshold so Persian
minority runs never flip an otherwise-English label; below-threshold
mixes stay Inter and rely on Qt per-character fallback for correct
Arabic shaping.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from app.theme.tokens import InkfallTokens
from app.theme.typography import role_font

__all__ = ["contains_arabic", "arabic_ratio", "font_for", "local_direction", "LATIN_STACK", "ARABIC_STACK"]

_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)

LATIN_STACK = ("Inter", "Vazirmatn", "Segoe UI", "Tahoma", "sans-serif")
ARABIC_STACK = ("Vazirmatn", "Inter", "Tahoma", "Segoe UI", "sans-serif")

#: Below-threshold mixes stay Latin (fallback shapes the Arabic runs).
#: Set so examples like "Chapter 3 - فصل اول" (0.46) keep Inter metrics
#: while Persian-dominant strings switch to Vazirmatn.
ARABIC_DOMINANCE_THRESHOLD = 0.5


def _is_arabic(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _ARABIC_RANGES)


def contains_arabic(text: str) -> bool:
    """True if any character falls in an Arabic-script Unicode block."""
    return any(_is_arabic(ch) for ch in (text or ""))


def arabic_ratio(text: str) -> float:
    """Fraction of letters that are Arabic-script (digits/neutral ignored)."""
    arabic = latin = 0
    for ch in text or "":
        if _is_arabic(ch):
            arabic += 1
        elif ch.isalpha():
            latin += 1
    total = arabic + latin
    return (arabic / total) if total else 0.0


def font_for(text: str, role: str, tokens: InkfallTokens) -> QFont:
    """Role font with the script-appropriate family stack.

    Persian/Arabic-dominant text -> Vazirmatn-first; everything else ->
    Inter-first (Qt falls back per-character for minority Arabic runs).
    Point size and weight always come from the role (no metric inflation).
    """
    font = role_font(tokens, role)
    stack = ARABIC_STACK if arabic_ratio(text) >= ARABIC_DOMINANCE_THRESHOLD else LATIN_STACK
    font.setFamily(stack[0])
    try:
        font.setFamilies(list(stack))
    except Exception:
        pass
    return font


def local_direction(text: str) -> Qt.LayoutDirection:
    """Per-string direction from the first strong directional character."""
    for ch in text or "":
        if _is_arabic(ch):
            return Qt.LayoutDirection.RightToLeft
        if ch.isalpha():
            return Qt.LayoutDirection.LeftToRight
    return Qt.LayoutDirection.LeftToRight
