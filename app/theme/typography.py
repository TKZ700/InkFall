"""Typographic roles built from InkfallTokens (M4.5).

Single place mapping hierarchy -> QFont: application title, section
title, document/card title, metadata, navigation label, button label,
eyebrow (letterspaced caps). Uses the token family stack so nothing
hardcodes a font that may not exist.
"""

from __future__ import annotations

from PySide6.QtGui import QFont

from app.theme.tokens import InkfallTokens

__all__ = ["role_font"]


def _families(tokens: InkfallTokens) -> list[str]:
    """Parsed family stack from the token string (order preserved)."""
    out: list[str] = []
    for part in str(tokens.font_family).split(","):
        family = part.strip().strip('"').strip("'")
        if family and family not in out:
            out.append(family)
    return out


def role_font(tokens: InkfallTokens, role: str) -> QFont:
    """Return a QFont for a typographic role."""
    font = QFont()
    families = _families(tokens)
    if families:
        # Inter stays first (unchanged Latin typography); Vazirmatn in
        # the stack supplies Persian/Arabic glyphs per character.
        font.setFamily(families[0])
        try:
            font.setFamilies(families)
        except Exception:
            pass

    def _weight(value: int) -> None:
        try:
            font.setWeight(QFont.Weight(value))
        except Exception:
            font.setBold(value >= tokens.w_bold)

    if role == "app_title":
        font.setPointSize(tokens.fs_display)
        _weight(tokens.w_bold)
    elif role == "page":
        font.setPointSize(tokens.fs_display)
        _weight(tokens.w_semibold)
    elif role == "section":
        font.setPointSize(tokens.fs_section)
        _weight(tokens.w_semibold)
    elif role == "doc_title":
        font.setPointSize(tokens.fs_body)
        _weight(tokens.w_semibold)
    elif role == "meta":
        font.setPointSize(tokens.fs_meta)
        _weight(tokens.w_regular)
    elif role == "caption":
        font.setPointSize(tokens.fs_caption)
        _weight(tokens.w_regular)
    elif role == "eyebrow":
        font.setPointSize(tokens.fs_eyebrow)
        _weight(tokens.w_semibold)
        font.setCapitalization(QFont.Capitalization.AllUppercase)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112.0)
    elif role == "nav":
        font.setPointSize(tokens.fs_body)
        _weight(tokens.w_medium)
    else:  # body / button
        font.setPointSize(tokens.fs_body)
        _weight(tokens.w_regular)
    return font
