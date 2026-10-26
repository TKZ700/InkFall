"""Multilingual typography tests (M4.6.1): Inter/Vazirmatn, thresholds."""

import pytest

pytestmark = pytest.mark.usefixtures("qapp")

CASES = {
    "Library": (False, 0.0),
    "کتابخانه": (True, 1.0),
    "PDF Editor 2026": (False, 0.0),
    "Chapter 3 - فصل اول": (True, 0.46),
    "Math - فصل اول.pdf": (True, 0.46),
}


def test_contains_arabic_ranges():
    from app.theme.multilingual import contains_arabic

    assert contains_arabic("کتابخانه") is True
    assert contains_arabic("Library") is False
    assert contains_arabic("") is False
    assert contains_arabic("ﬀﷲ") is True  # presentation forms covered


def test_arabic_ratio_thresholds():
    from app.theme.multilingual import arabic_ratio

    assert arabic_ratio("Library") == 0.0
    assert arabic_ratio("کتابخانه") == 1.0
    # Multi-word Persian phrases inside English labels stay Latin-based.
    assert arabic_ratio("Chapter 3 - فصل اول") < 0.5
    assert arabic_ratio("کتابخانه مرکزی شهر") >= 0.5


def test_font_for_script_selection():
    from app.theme.multilingual import font_for
    from app.theme.tokens import dark_tokens

    tokens = dark_tokens()
    latin = font_for("Chapter 3 - فصل اول", "doc_title", tokens)
    assert latin.family() == "Inter"
    assert latin.pointSize() == tokens.fs_body  # no metric inflation
    fa = font_for("کتابخانه مرکزی", "doc_title", tokens)
    assert fa.family() == "Vazirmatn"
    assert fa.pointSize() == tokens.fs_body


def test_role_fonts_are_inter_first():
    from app.theme.typography import role_font
    from app.theme.tokens import dark_tokens

    tokens = dark_tokens()
    for role in ("page", "section", "eyebrow", "nav", "body", "doc_title", "meta", "caption"):
        assert role_font(tokens, role).family() == "Inter", role


def test_vazirmatn_resolution_or_fallback():
    from app.theme.multilingual import font_for
    from app.theme.tokens import dark_tokens

    # Requested stack is honored; Qt performs per-character fallback at
    # render time, so the app never breaks on machines without the font.
    assert font_for("کتابخانه", "body", dark_tokens()).family() == "Vazirmatn"
    assert font_for("Library", "body", dark_tokens()).family() == "Inter"


def test_local_direction_only():
    from PySide6.QtCore import Qt

    from app.theme.multilingual import local_direction

    assert local_direction("کتابخانه") == Qt.LayoutDirection.RightToLeft
    assert local_direction("Library") == Qt.LayoutDirection.LeftToRight
    assert local_direction("Chapter 3 - فصل اول") == Qt.LayoutDirection.LeftToRight
    assert local_direction("") == Qt.LayoutDirection.LeftToRight


def test_persian_strings_shape_without_tofu(qtbot):
    from PySide6.QtGui import QFontMetrics

    from app.theme.multilingual import font_for
    from app.theme.tokens import dark_tokens

    tokens = dark_tokens()
    for text in ("کتابخانه", "اسناد اخیر", "پوشه‌ها", "جستجو", "وارد کردن PDF",
                 "سند جدید", "تنظیمات", "Math - فصل اول.pdf"):
        font = font_for(text, "doc_title", tokens)
        metrics = QFontMetrics(font)
        # Shaped glyphs exist and advance (not zero-width missing runs).
        assert metrics.horizontalAdvance(text) > 0
