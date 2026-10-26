"""Bundled UI fonts: Inter for Latin, Vazirmatn for Persian/Arabic.

Loads ``assets/fonts/Inter-{Regular,Medium,SemiBold}.ttf`` and
``assets/fonts/Vazirmatn-{Regular,Medium,SemiBold}.ttf`` via
QFontDatabase before UI construction, so English resolves to Inter and
Persian/Arabic glyphs resolve to Vazirmatn even on machines where neither
font is installed.

The application font is a *family stack* (Inter first, Vazirmatn next):
Latin text keeps the existing Inter typography, while Arabic-script runs
inside mixed strings fall back per character to Vazirmatn instead of an
arbitrary system font.  Vazirmatn is distributed under the SIL Open Font
License; the notice ships beside the files as ``Vazirmatn-OFL.txt``.

Nuitka inclusion: the build script passes
``--include-data-dir=assets=assets`` so the font files land beside the
frozen executable; :func:`app.resources.resource_path` resolves them in
both frozen and source layouts.  Never raises.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "INTER_FAMILY",
    "VAZIRMATN_FAMILY",
    "UI_FONT_FAMILIES",
    "apply_application_font",
    "application_font",
    "bundled_arabic_family_available",
    "bundled_family_available",
    "bundled_font_dir",
    "bundled_license_path",
    "load_bundled_fonts",
]

INTER_FAMILY = "Inter"
VAZIRMATN_FAMILY = "Vazirmatn"

#: Order matters: Inter keeps the established Latin UI; Vazirmatn supplies
#: the Persian/Arabic glyphs Qt cannot find in Inter.  The remaining
#: entries are the historical system fallbacks.
UI_FONT_FAMILIES = (
    INTER_FAMILY,
    VAZIRMATN_FAMILY,
    "Segoe UI",
    "Tahoma",
)

_BUNDLED_FILES = (
    # Inter (Latin)
    "Inter-Regular.ttf",
    "Inter-Medium.ttf",
    "Inter-SemiBold.ttf",
    # Vazirmatn (Persian/Arabic)
    "Vazirmatn-Regular.ttf",
    "Vazirmatn-Medium.ttf",
    "Vazirmatn-SemiBold.ttf",
)

#: SIL OFL notice that must ship with the bundled Vazirmatn files.
VAZIRMATN_LICENSE = "Vazirmatn-OFL.txt"


def bundled_font_dir() -> Path | None:
    """Locate ``assets/fonts`` in frozen and source layouts.

    Delegates layout knowledge to :mod:`app.resources`: the executable
    directory in a frozen build, the repository root in a checkout.
    """
    from app.resources import resource_path

    try:
        directory = resource_path("assets", "fonts")
        if directory.is_dir() and any(
            (directory / name).is_file() for name in _BUNDLED_FILES
        ):
            return directory
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def bundled_license_path() -> Path | None:
    """Path to the bundled Vazirmatn OFL notice, when present."""
    directory = bundled_font_dir()
    if directory is None:
        return None
    try:
        license_path = directory / VAZIRMATN_LICENSE
        return license_path if license_path.is_file() else None
    except OSError:
        return None


def load_bundled_fonts() -> str:
    """Register every bundled face; returns the Latin family name.

    Safe to call before or after QApplication construction (Qt handles
    both); safe to call twice (QFontDatabase dedupes by file). Each
    file must register (``addApplicationFont`` id != -1) — a failure
    is recorded on the function object as ``load_errors`` for tests
    and diagnostics instead of silently falling back to system fonts.
    """
    directory = bundled_font_dir()
    errors: list[str] = []
    if directory is None:
        errors.append("font directory not found")
        load_bundled_fonts.load_errors = errors  # type: ignore[attr-defined]
        return INTER_FAMILY
    try:
        from PySide6.QtGui import QFontDatabase
    except Exception:
        errors.append("QFontDatabase import failed")
        load_bundled_fonts.load_errors = errors  # type: ignore[attr-defined]
        return INTER_FAMILY
    loaded: set[str] = set()
    for name in _BUNDLED_FILES:
        try:
            path = directory / name
            if not path.is_file():
                errors.append(f"missing {name}")
                continue
            font_id = QFontDatabase.addApplicationFont(str(path))
            if int(font_id) < 0:
                errors.append(f"registration failed: {name}")
                continue
            loaded.update(
                str(family)
                for family in QFontDatabase.applicationFontFamilies(font_id)
            )
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
    load_bundled_fonts.load_errors = errors  # type: ignore[attr-defined]
    load_bundled_fonts.loaded_families = loaded  # type: ignore[attr-defined]
    return INTER_FAMILY


load_bundled_fonts.load_errors = []  # type: ignore[attr-defined]
load_bundled_fonts.loaded_families = set()  # type: ignore[attr-defined]


def application_font(point_size: int | None = None):
    """Return the UI font with the bundled Latin/Arabic family stack."""
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(list(UI_FONT_FAMILIES))
    if point_size is not None:
        try:
            size = int(point_size)
            if size > 0:
                font.setPointSize(size)
        except (TypeError, ValueError):
            pass
    return font


def apply_application_font(point_size: int | None = None) -> bool:
    """Install the family stack as the application default font.

    Called after :func:`load_bundled_fonts` and before windows are
    constructed.  Returns ``False`` when no QApplication exists yet.
    """
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return False
        app.setFont(application_font(point_size))
        return True
    except Exception:
        return False


def bundled_family_available() -> bool:
    """True when the Inter family resolves in the font database."""
    return _family_available(INTER_FAMILY)


def bundled_arabic_family_available() -> bool:
    """True when the bundled Vazirmatn family resolves."""
    return _family_available(VAZIRMATN_FAMILY)


def _family_available(family: str) -> bool:
    try:
        from PySide6.QtGui import QFontDatabase

        return family in QFontDatabase.families()
    except Exception:
        return False
