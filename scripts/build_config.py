"""Authoritative build configuration for Inkfall preview artifacts.

Both the local CLI (:mod:`scripts.build`) and GitHub Actions consume this
module, so Nuitka arguments, output names and the post-build verification
manifest cannot drift between the two.

The module is deliberately dependency-free (stdlib only). It must stay
importable without Nuitka or PySide6 installed: the build dependency group
is optional, and normal development never needs it.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

__all__ = [
    "APP_NAME",
    "COMPANY_NAME",
    "PRODUCT_DESCRIPTION",
    "QT_PLUGIN_FAMILIES",
    "SOURCE_ROOT",
    "WINDOWS_ICON",
    "BuildError",
    "artifact_path",
    "ensure_native",
    "host_platform",
    "linux_archive_name",
    "linux_archive_root",
    "missing_bundle_entries",
    "nuitka_arguments",
    "platform_requirements",
    "project_version",
    "required_bundle_entries",
    "standalone_binary_name",
    "validate_release_tag",
    "windows_installer_name",
]

APP_NAME = "InkFall"
# Deliberately not "InkFall" or "Inkfall": Nuitka stores the install
# directory under HKCU\Software\<company>\<product>, and QSettings already
# uses HKCU\Software\Inkfall\Inkfall for user preferences. A distinct
# company name keeps the uninstaller from deleting the user's settings.
COMPANY_NAME = "Inkfall Project"
PRODUCT_DESCRIPTION = "InkFall - PDF reader and ink annotation notebook"

#: The Windows icon embedded in ``Inkfall.exe``. Nuitka reuses this same
#: file for the NSIS installer (``MUI_ICON``), and the Desktop/Start Menu
#: shortcuts plus the Apps-list ``DisplayIcon`` point at the executable, so
#: one source file drives every Windows brand surface. Regenerate it with
#: ``uv run python scripts/make_icon.py`` from ``assets/icons/new_icon.png``;
#: ``tests/test_build_icons.py`` fails if it drifts from that artwork.
WINDOWS_ICON = "assets/icons/inkfall.ico"

#: Qt plugin families to bundle explicitly. ``qml`` is not in Nuitka's
#: "sensible plugins" defaults: without it, every ``.qml`` data file emits
#: a "not having Qt qml plugins is unlikely to work" warning, and the
#: plugin is only added later, implicitly, once ``QtQuickWidgets`` is seen.
#: Declaring it keeps the requirement visible and independent of import
#: analysis. Do not add families the app does not use.
QT_PLUGIN_FAMILIES = ("qml",)

SOURCE_ROOT = Path(__file__).resolve().parents[1]

#: Platforms this build system supports. Each one must be built on its
#: own native host: Nuitka compiles C against the host toolchain and Qt,
#: so cross-compilation is not supported here.
PLATFORMS = ("windows", "linux")

_HOST_PREFIXES = {
    "windows": ("win32", "cygwin"),
    "linux": ("linux",),
}

_BINARY_NAMES = {
    "windows": "Inkfall.exe",
    "linux": "Inkfall",
}

WINDOWS_INSTALLER_TEMPLATE = "Inkfall-{version}-windows-x86_64-setup.exe"
LINUX_ARCHIVE_TEMPLATE = "inkfall-{version}-linux-x86_64.tar.gz"

#: Qt/QML files that must survive into a standalone distribution. Patterns
#: may contain glob wildcards (including ``**``). These are checked after
#: the Nuitka step; a missing entry fails the build instead of shipping a
#: half-populated installer or archive.
_COMMON_BUNDLE_ENTRIES = (
    "qml/toolprops/ColorPicker.qml",
    "qml/toolprops/NumericEditor.qml",
    "qml/toolprops/PaletteStrip.qml",
    "assets/fonts/Inter-Regular.ttf",
    "assets/fonts/Inter-Medium.ttf",
    "assets/fonts/Inter-SemiBold.ttf",
    "assets/fonts/Vazirmatn-Regular.ttf",
    "assets/fonts/Vazirmatn-Medium.ttf",
    "assets/fonts/Vazirmatn-SemiBold.ttf",
    "assets/fonts/Vazirmatn-OFL.txt",
    WINDOWS_ICON,
    "assets/icons/new_icon.png",
    # Qt Quick runtime modules: loaded dynamically by the QML engine, so
    # import analysis alone does not bring them in.
    "PySide6/qml/QtQuick/Controls/qmldir",
    "PySide6/qml/QtQuick/Layouts/qmldir",
)

_WINDOWS_BUNDLE_ENTRIES = _COMMON_BUNDLE_ENTRIES + (
    "Inkfall.exe",
    # Nuitka's PySide6 plugin puts Qt plugins below PySide6/qt-plugins.
    "PySide6/qt-plugins/platforms/qwindows.dll",
    # The headless --self-test path selects the offscreen platform plugin.
    "PySide6/qt-plugins/platforms/qoffscreen.dll",
    # Windows Qt libraries have no "lib" prefix (Linux: libQt6Core.so*).
    "**/Qt6Core.dll",
)

_LINUX_BUNDLE_ENTRIES = _COMMON_BUNDLE_ENTRIES + (
    "Inkfall",
    "PySide6/qt-plugins/platforms/libqxcb.so",
    "PySide6/qt-plugins/platforms/libqoffscreen.so",
    "**/libQt6Core.so*",
)

_BUNDLE_ENTRIES = {
    "windows": _WINDOWS_BUNDLE_ENTRIES,
    "linux": _LINUX_BUNDLE_ENTRIES,
}

_REQUIREMENTS = {
    "windows": (
        "Nuitka from the optional build group: uv sync --group build",
        "MSVC C/C++ toolchain (Visual Studio Build Tools with the "
        "'Desktop development with C++' workload)",
        "NSIS 3.x: Nuitka downloads and caches the official release "
        "automatically; install it and pass --windows-nsis-path only if "
        "outbound downloads are blocked",
        "Several GB of free disk space for the Nuitka work directory",
    ),
    "linux": (
        "Nuitka from the optional build group: uv sync --group build",
        "gcc and libc headers (build-essential)",
        "patchelf (required by Nuitka standalone mode)",
        "tar (used to create the release archive)",
        "Qt runtime libraries for the smoke test: libgl1, libegl1, "
        "libxkbcommon-x11-0, libxcb-cursor0, libdbus-1-3, libfontconfig1",
    ),
}


class BuildError(RuntimeError):
    """A build precondition or verification step failed."""


# ---------------------------------------------------------------------------
# Version / naming
# ---------------------------------------------------------------------------


def project_version(source_root: Path = SOURCE_ROOT) -> str:
    """Return the authoritative version declared in ``pyproject.toml``.

    ``pyproject.toml`` is the single source of truth: Nuitka metadata and
    installer/archive filenames are all derived from it, so there is no
    second version constant to forget.
    """
    pyproject = Path(source_root) / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"cannot read {pyproject}: {exc}") from exc
    version = str(data.get("project", {}).get("version", "")).strip()
    if not version:
        raise BuildError(f"{pyproject} declares no [project] version")
    return version


def validate_release_tag(tag: str, *, source_root: Path = SOURCE_ROOT) -> str:
    """Check that a ``v<version>`` release tag encodes the project version.

    Release automation may only publish binaries under a tag whose version
    matches ``pyproject.toml`` exactly (tag ``v0.1.0`` for version
    ``0.1.0``). Anything else fails loudly instead of publishing a pairing
    of one version's tag with another version's artifacts. Returns the
    validated version string.

    Raises:
        BuildError: if ``tag`` is not ``v`` followed by the project version.
    """
    version = tag[1:] if tag.startswith("v") else ""
    project = project_version(source_root)
    if not version or version != project:
        raise BuildError(
            f"release tag {tag!r} does not match the project version "
            f"{project!r} declared in pyproject.toml; the release workflow "
            f"requires the tag 'v{project}'"
        )
    return project


def windows_installer_name(version: str) -> str:
    """Required Windows setup executable filename."""
    return WINDOWS_INSTALLER_TEMPLATE.format(version=version)


def linux_archive_name(version: str) -> str:
    """Required Linux distribution archive filename."""
    return LINUX_ARCHIVE_TEMPLATE.format(version=version)


def linux_archive_root(version: str) -> str:
    """Directory that the Linux archive expands to."""
    return f"inkfall-{version}-linux-x86_64"


def standalone_binary_name(platform: str) -> str:
    """Executable filename Nuitka produces inside the standalone tree."""
    _check_platform(platform)
    return _BINARY_NAMES[platform]


def artifact_path(platform: str, version: str, dist_dir: Path) -> Path:
    """Final artifact path for ``platform``."""
    _check_platform(platform)
    if platform == "windows":
        return Path(dist_dir) / windows_installer_name(version)
    return Path(dist_dir) / linux_archive_name(version)


# ---------------------------------------------------------------------------
# Host validation
# ---------------------------------------------------------------------------


def _check_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise BuildError(
            f"unsupported platform {platform!r}; expected one of "
            f"{', '.join(PLATFORMS)}")


def host_platform() -> str:
    """Return ``windows``/``linux`` for the running host, or its raw name."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def ensure_native(platform: str, host: str | None = None) -> None:
    """Fail unless ``platform`` can be built on ``host`` (default: this one).

    Raises:
        BuildError: with the exact host/platform mismatch and the reason
            (Nuitka needs the target's native compiler and Qt libraries).
    """
    _check_platform(platform)
    host = host if host is not None else sys.platform
    prefixes = _HOST_PREFIXES[platform]
    if any(host.startswith(prefix) for prefix in prefixes):
        return
    raise BuildError(
        f"cannot build the {platform} artifact on host {host!r}: Inkfall is "
        f"built natively with Nuitka (target compilers and Qt libraries are "
        f"host-specific). Run this command on a {platform} machine or use the "
        f"GitHub Actions workflow in .github/workflows/build.yml."
    )


def platform_requirements(platform: str) -> tuple[str, ...]:
    """Human-readable native prerequisites for ``platform``."""
    _check_platform(platform)
    return _REQUIREMENTS[platform]


# ---------------------------------------------------------------------------
# Nuitka invocation
# ---------------------------------------------------------------------------


def nuitka_arguments(
    platform: str,
    version: str,
    *,
    source_root: Path = SOURCE_ROOT,
    build_root: Path,
    dist_dir: Path,
) -> list[str]:
    """Return the complete Nuitka argument list for ``platform``.

    Everything is explicit: standalone mode, the PySide6 plugin, both
    runtime resource trees (``qml/`` and ``assets/``), Windows metadata and
    the installer options. Nothing depends on files being auto-detected.
    """
    _check_platform(platform)
    source_root = Path(source_root)
    build_root = Path(build_root)
    dist_dir = Path(dist_dir)

    args = [
        "--mode=standalone",
        "--enable-plugins=pyside6",
        # Explicit Qt plugin families (see QT_PLUGIN_FAMILIES). This runs
        # before Nuitka scans the included ``.qml`` data files, which is
        # what silences the "not having Qt qml plugins" warning.
        f"--include-qt-plugins={','.join(QT_PLUGIN_FAMILIES)}",
        "--assume-yes-for-downloads",
        "--remove-output",
        f"--output-dir={build_root}",
        f"--output-filename={standalone_binary_name(platform)}",
        # Dynamic data: never inferred from imports.
        f"--include-data-dir={source_root / 'qml'}=qml",
        f"--include-data-dir={source_root / 'assets'}=assets",
        # Executable metadata (installer version info on Windows).
        f"--company-name={COMPANY_NAME}",
        f"--product-name={APP_NAME}",
        f"--file-description={APP_NAME}",
        f"--product-version={version}",
        f"--file-version={version}",
    ]

    if platform == "windows":
        args += [
            # MSVC cl 14.3 runs out of heap in optimization pass 2 on the
            # giant generated TUs (numpy einsumfunc, pymupdf mupdf) when
            # Nuitka fans out C compilation across every core. Low-memory
            # mode selects memory-frugal C options and implies a single
            # C compiler job (the Nuitka 4.3.1 --jobs default under low
            # memory is 1), which keeps peak cl.exe heap usage bounded.
            # Linux GCC is unaffected; do not slow that build down.
            "--low-memory",
            "--windows-console-mode=disable",
            f"--windows-icon-from-ico={source_root / WINDOWS_ICON}",
            "--windows-create-installer",
            f"--windows-installer-output={artifact_path(platform, version, dist_dir)}",
            "--windows-installer-shortcuts=desktop,start-menu",
            # Per-user install: no UAC prompt, and Nuitka 4.3.1's uninstaller
            # reliably removes the same HKCU entry and both shortcuts.
            # (In multiuser mode Nuitka does not record the chosen scope in
            # the uninstall string, so a per-user uninstall on an admin
            # account can leave its HKCU uninstall entry behind.)
            "--windows-installer-mode=user",
        ]

    args.append(str(source_root / "main.py"))
    return args


# ---------------------------------------------------------------------------
# Post-build verification
# ---------------------------------------------------------------------------


def required_bundle_entries(platform: str) -> tuple[str, ...]:
    """Glob patterns that must exist inside the standalone tree."""
    _check_platform(platform)
    return _BUNDLE_ENTRIES[platform]


def _entry_present(root: Path, pattern: str) -> bool:
    try:
        if any(char in pattern for char in "*?["):
            return any(root.glob(pattern))
        return (root / pattern).exists()
    except OSError:
        return False


def missing_bundle_entries(platform: str, root: Path) -> list[str]:
    """Return the required entries absent from a standalone tree."""
    _check_platform(platform)
    root = Path(root)
    if not root.is_dir():
        return [f"<standalone directory missing: {root}>"]
    return [
        pattern
        for pattern in required_bundle_entries(platform)
        if not _entry_present(root, pattern)
    ]
