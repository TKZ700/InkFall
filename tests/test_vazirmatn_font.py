"""Vazirmatn bundling and UI font-fallback tests.

These prove the bundled files/license exist, both families register, the
Qt Widgets role stack keeps Inter first with Vazirmatn behind it, and QML
inherits the same application font. Visual shaping quality still needs a
manual packaged-app check (see BUILDING.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.fonts import (
    INTER_FAMILY,
    VAZIRMATN_FAMILY,
    apply_application_font,
    application_font,
    bundled_arabic_family_available,
    bundled_family_available,
    bundled_font_dir,
    bundled_license_path,
    load_bundled_fonts,
)
from app.theme.multilingual import font_for
from app.theme.tokens import dark_tokens
from app.theme.typography import role_font

pytestmark = pytest.mark.usefixtures("qapp")

_VAZIRMATN_FILES = (
    "Vazirmatn-Regular.ttf",
    "Vazirmatn-Medium.ttf",
    "Vazirmatn-SemiBold.ttf",
)


def test_vazirmatn_assets_and_license_are_bundled() -> None:
    directory = bundled_font_dir()
    assert directory is not None
    for name in _VAZIRMATN_FILES:
        path = Path(str(directory)) / name
        assert path.is_file(), name
        assert path.stat().st_size > 50_000, name

    notice = bundled_license_path()
    assert notice is not None and notice.is_file()
    text = notice.read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE" in text
    assert "Vazirmatn" in text


def test_both_families_register_without_errors() -> None:
    from PySide6.QtGui import QFontDatabase

    assert load_bundled_fonts() == INTER_FAMILY
    assert getattr(load_bundled_fonts, "load_errors", []) == []
    assert bundled_family_available()
    assert bundled_arabic_family_available()
    families = set(QFontDatabase.families())
    assert {INTER_FAMILY, VAZIRMATN_FAMILY} <= families


def test_application_font_keeps_inter_first_and_adds_vazirmatn() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    previous = app.font()
    try:
        assert apply_application_font(14) is True
        families = list(app.font().families())
        assert families[:2] == [INTER_FAMILY, VAZIRMATN_FAMILY]
        assert app.font().family() == INTER_FAMILY
    finally:
        app.setFont(previous)


def test_role_and_script_fonts_use_the_same_stack() -> None:
    tokens = dark_tokens()
    body = role_font(tokens, "body")
    assert body.family() == INTER_FAMILY
    assert list(body.families())[:2] == [INTER_FAMILY, VAZIRMATN_FAMILY]

    # Persian-dominant text switches to the Vazirmatn family (existing
    # behavior), while Latin stays on Inter.
    assert font_for("سلام دنیا", "body", tokens).family() == VAZIRMATN_FAMILY
    assert font_for("Chapter 3 - سلام", "doc_title", tokens).family() == INTER_FAMILY

    font = application_font(14)
    assert list(font.families())[:2] == [INTER_FAMILY, VAZIRMATN_FAMILY]


def test_theme_stylesheet_declares_the_fallback_stack() -> None:
    from app.theme.manager import ThemeManager

    stylesheet = ThemeManager()._stylesheet()
    assert "font-family: Inter, Vazirmatn, Segoe UI, Tahoma, sans-serif;" in stylesheet


def test_qml_text_inherits_the_application_font_stack() -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    previous = app.font()
    try:
        assert apply_application_font(14) is True
        engine = QQmlEngine()
        component = QQmlComponent(engine)
        component.setData(
            b'import QtQuick\n'
            b'Text { text: "Hello \xd8\xb3\xd9\x84\xd8\xa7\xd9\x85"; '
            b'font.pixelSize: 13 }\n',
            QUrl("qrc:/inkfall-font-test.qml"),
        )
        assert component.isReady(), component.errorString()
        item = component.create()
        assert item is not None
        try:
            families = list(item.property("font").families())
            assert families[:2] == [INTER_FAMILY, VAZIRMATN_FAMILY]
        finally:
            item.deleteLater()
    finally:
        app.setFont(previous)


def test_qml_sources_do_not_pin_a_single_family() -> None:
    """QML cannot express ``font.families``; pinning ``font.family`` would
    discard the application fallback, so the components must inherit."""
    from scripts.build_config import SOURCE_ROOT

    for path in (SOURCE_ROOT / "qml").rglob("*.qml"):
        text = path.read_text(encoding="utf-8")
        assert "font.family" not in text, path
