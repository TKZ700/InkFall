"""Build-configuration tests (no Nuitka, no Qt, no artifacts produced).

These cover the parts of the release pipeline that are pure configuration:
version derivation, artifact naming, Nuitka arguments, the post-build
manifest and the native-host guard. They are intentionally fast so CI and
local development can run them without the optional build group.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

from scripts import build_config
from scripts.build import required_entries
from scripts.build_config import (
    BuildError,
    SOURCE_ROOT,
    ensure_native,
    linux_archive_name,
    linux_archive_root,
    missing_bundle_entries,
    nuitka_arguments,
    project_version,
    required_bundle_entries,
    standalone_binary_name,
    windows_installer_name,
)


def test_installer_registry_key_does_not_collide_with_qsettings() -> None:
    """Nuitka's InstallDir key must not be the QSettings key.

    QSettings for organization/application "Inkfall"/"Inkfall" lives at
    HKCU\\Software\\Inkfall\\Inkfall. Nuitka writes InstallDir (and the
    uninstaller deletes the whole key) under
    HKCU\\Software\\<company>\\<product>, so the company name must differ.
    """
    installer_key = f"Software\\{build_config.COMPANY_NAME}\\{build_config.APP_NAME}"
    assert installer_key.lower() != "software\\inkfall\\inkfall"


def test_visible_product_name_is_exact() -> None:
    """Desktop, Start Menu and Apps-list all show exactly ``InkFall``."""
    assert build_config.APP_NAME == "InkFall"
    assert build_config.PRODUCT_DESCRIPTION.startswith("InkFall - ")
    # The CLI executable keeps its established filename; only the
    # user-visible product name changes.
    assert standalone_binary_name("windows") == "Inkfall.exe"
    args = nuitka_arguments(
        "windows", project_version(), source_root=SOURCE_ROOT,
        build_root=Path("build"), dist_dir=Path("dist"))
    assert "--product-name=InkFall" in args
    assert "--file-description=InkFall" in args


def test_windows_installer_creates_both_shortcuts() -> None:
    args = nuitka_arguments(
        "windows", project_version(), source_root=SOURCE_ROOT,
        build_root=Path("build"), dist_dir=Path("dist"))
    shortcuts = [
        arg.split("=", 1)[1] for arg in args
        if arg.startswith("--windows-installer-shortcuts=")
    ]
    assert shortcuts == ["desktop,start-menu"]


def test_project_version_is_preview_version() -> None:
    assert project_version() == "0.1.0"


def test_artifact_names_are_exact() -> None:
    version = project_version()
    assert windows_installer_name(version) == (
        "Inkfall-0.1.0-windows-x86_64-setup.exe")
    assert linux_archive_name(version) == "inkfall-0.1.0-linux-x86_64.tar.gz"
    assert linux_archive_root(version) == "inkfall-0.1.0-linux-x86_64"


def test_artifact_paths_use_version_and_dist_dir(tmp_path: Path) -> None:
    version = project_version()
    windows = build_config.artifact_path("windows", version, tmp_path)
    linux = build_config.artifact_path("linux", version, tmp_path)
    assert windows.name == windows_installer_name(version)
    assert linux.name == linux_archive_name(version)
    assert windows.parent == tmp_path
    assert linux.parent == tmp_path


def test_nuitka_stays_out_of_runtime_dependencies() -> None:
    data = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = " ".join(data["project"]["dependencies"]).lower()
    assert "nuitka" not in runtime
    assert data["dependency-groups"]["build"] == ["nuitka==4.3.1"]


def test_windows_nuitka_arguments(tmp_path: Path) -> None:
    version = project_version()
    build_root = tmp_path / "build"
    dist_dir = tmp_path / "dist"
    args = nuitka_arguments(
        "windows", version, source_root=SOURCE_ROOT,
        build_root=build_root, dist_dir=dist_dir)

    assert "--mode=standalone" in args
    assert "--enable-plugins=pyside6" in args
    assert "--assume-yes-for-downloads" in args
    assert f"--output-dir={build_root}" in args
    assert "--output-filename=Inkfall.exe" in args
    assert f"--include-data-dir={SOURCE_ROOT / 'qml'}=qml" in args
    assert f"--include-data-dir={SOURCE_ROOT / 'assets'}=assets" in args
    assert f"--product-version={version}" in args
    assert "--product-name=InkFall" in args
    assert "--file-description=InkFall" in args
    assert "--windows-create-installer" in args
    assert "--windows-installer-shortcuts=desktop,start-menu" in args
    assert "--windows-installer-mode=user" in args
    assert "--windows-console-mode=disable" in args
    assert (f"--windows-icon-from-ico="
            f"{SOURCE_ROOT / build_config.WINDOWS_ICON}") in args
    assert (f"--windows-installer-output="
            f"{dist_dir / windows_installer_name(version)}") in args
    assert args[-1] == str(SOURCE_ROOT / "main.py")


def test_windows_low_memory_c_compilation_is_requested(tmp_path: Path) -> None:
    """MSVC must not fan out C jobs that exhaust cl's pass-2 heap (C1002).

    On windows-2022 the numpy/pymupdf generated TUs exhaust the cl.exe
    optimizer heap when Nuitka compiles in parallel. ``--low-memory``
    selects memory-frugal C options and implies a single C compiler job
    in Nuitka 4.3.1. Linux GCC never hit this, so its arguments must
    stay unchanged.
    """
    version = project_version()
    windows_args = nuitka_arguments(
        "windows", version, source_root=SOURCE_ROOT,
        build_root=tmp_path / "win", dist_dir=tmp_path / "win")
    linux_args = nuitka_arguments(
        "linux", version, source_root=SOURCE_ROOT,
        build_root=tmp_path / "linux", dist_dir=tmp_path / "linux")

    assert "--low-memory" in windows_args
    assert "--low-memory" not in linux_args
    assert not [arg for arg in linux_args if arg.startswith("--jobs")]


def test_qml_plugin_family_is_requested_explicitly(tmp_path: Path) -> None:
    """QML data files need the qml plugin family declared up front."""
    for platform in ("windows", "linux"):
        args = nuitka_arguments(
            platform, project_version(), source_root=SOURCE_ROOT,
            build_root=tmp_path / platform, dist_dir=tmp_path / platform)
        assert "--include-qt-plugins=qml" in args
        assert build_config.QT_PLUGIN_FAMILIES == ("qml",)


def test_windows_icon_argument_points_at_the_generated_ico() -> None:
    """One ICO drives the executable, installer and shortcut branding."""
    icon = SOURCE_ROOT / build_config.WINDOWS_ICON
    assert icon == SOURCE_ROOT / "assets" / "icons" / "inkfall.ico"
    assert icon.is_file()
    assert build_config.WINDOWS_ICON in required_bundle_entries("windows")


def test_linux_nuitka_arguments_contain_no_windows_or_package_flags(
        tmp_path: Path) -> None:
    args = nuitka_arguments(
        "linux", project_version(), source_root=SOURCE_ROOT,
        build_root=tmp_path / "build", dist_dir=tmp_path / "dist")

    assert "--mode=standalone" in args
    assert "--enable-plugins=pyside6" in args
    assert not [arg for arg in args if arg.startswith("--windows-")]
    joined = " ".join(args).lower()
    assert ".deb" not in joined
    assert "rpm" not in joined
    assert standalone_binary_name("linux") == "Inkfall"


@pytest.mark.parametrize(
    ("platform", "host"),
    [("windows", "win32"), ("linux", "linux")],
)
def test_ensure_native_accepts_matching_host(platform: str, host: str) -> None:
    ensure_native(platform, host=host)


@pytest.mark.parametrize(
    ("platform", "host"),
    [("linux", "win32"), ("windows", "linux")],
)
def test_ensure_native_rejects_cross_platform(platform: str, host: str) -> None:
    with pytest.raises(BuildError) as excinfo:
        ensure_native(platform, host=host)
    message = str(excinfo.value)
    assert platform in message
    assert "natively" in message


def test_unknown_platform_is_rejected() -> None:
    with pytest.raises(BuildError):
        ensure_native("macos", host="darwin")


def test_source_checkout_contains_every_static_data_entry() -> None:
    """Every non-glob data entry must exist in the checkout."""
    for platform in ("windows", "linux"):
        for entry in required_bundle_entries(platform):
            if any(char in entry for char in "*?["):
                continue
            if entry.startswith(("qml/", "assets/")):
                assert (SOURCE_ROOT / entry).is_file(), entry


def test_required_entries_exist_for_qt_runtime_modules() -> None:
    for platform in ("windows", "linux"):
        entries = required_bundle_entries(platform)
        assert "PySide6/qml/QtQuick/Controls/qmldir" in entries
        assert "PySide6/qml/QtQuick/Layouts/qmldir" in entries
        assert "assets/fonts/Inter-Regular.ttf" in entries
        assert "assets/fonts/Vazirmatn-Regular.ttf" in entries
        assert "assets/fonts/Vazirmatn-OFL.txt" in entries


def test_qt_core_library_pattern_matches_each_platform_naming() -> None:
    windows = required_bundle_entries("windows")
    linux = required_bundle_entries("linux")
    # Windows Qt DLLs have no "lib" prefix; Linux shared objects do.
    assert "**/Qt6Core.dll" in windows
    assert "**/libQt6Core.dll" not in windows
    assert "**/libQt6Core.so*" in linux


def test_qt_plugins_live_below_the_pyside6_package() -> None:
    """Nuitka's PySide6 plugin copies Qt plugins to PySide6/qt-plugins."""
    assert (
        "PySide6/qt-plugins/platforms/qwindows.dll"
        in required_bundle_entries("windows")
    )
    assert (
        "PySide6/qt-plugins/platforms/libqxcb.so"
        in required_bundle_entries("linux")
    )


def test_missing_bundle_entries_reports_everything(tmp_path: Path) -> None:
    missing = missing_bundle_entries("linux", tmp_path)
    assert "Inkfall" in missing
    assert len(missing) == len(required_bundle_entries("linux"))


def test_missing_bundle_entries_accepts_glob_matches(tmp_path: Path) -> None:
    (tmp_path / "libQt6Core.so.6").write_bytes(b"")
    (tmp_path / "Inkfall").write_bytes(b"")
    for entry in required_bundle_entries("linux"):
        if entry.startswith(("qml/", "assets/", "PySide6/")):
            path = tmp_path / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

    assert missing_bundle_entries("linux", tmp_path) == []


@pytest.mark.parametrize(
    "missing_asset",
    ["assets/icons/new_icon.png", build_config.WINDOWS_ICON],
)
def test_missing_current_brand_asset_fails_standalone_verification(
        tmp_path: Path, missing_asset: str) -> None:
    """Either current brand asset is genuinely required at runtime."""
    for entry in required_bundle_entries("windows"):
        if entry == missing_asset:
            continue
        target = tmp_path / (
            "Qt6Core.dll" if entry == "**/Qt6Core.dll" else entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")

    assert missing_bundle_entries("windows", tmp_path) == [missing_asset]


def test_archive_required_entries_include_launcher() -> None:
    entries = required_entries("linux", "inkfall-0.1.0-linux-x86_64")
    assert "inkfall-0.1.0-linux-x86_64/inkfall" in entries
    assert "inkfall-0.1.0-linux-x86_64/Inkfall" in entries
    assert all("inkfall-0.1.0-linux-x86_64/" in entry for entry in entries)


def test_linux_build_code_never_mentions_deb_or_rpm() -> None:
    """Scope guard: the Linux output is a .tar.gz, not a package."""
    text = (SOURCE_ROOT / "scripts" / "build.py").read_text(encoding="utf-8")
    assert ".deb" not in text
    assert "dpkg" not in text
    assert "rpmbuild" not in text


def test_source_resources_resolve_to_checkout() -> None:
    from app.resources import is_frozen, resource_path, resource_root

    assert is_frozen() is False
    assert resource_root() == SOURCE_ROOT
    assert resource_path("qml", "toolprops", "ColorPicker.qml").is_file()
    assert resource_path("assets", "fonts", "Inter-Regular.ttf").is_file()
    assert resource_path("assets", "fonts", "Vazirmatn-Regular.ttf").is_file()
    assert resource_path("assets", "fonts", "Vazirmatn-OFL.txt").is_file()
    assert resource_path("assets", "icons", "inkfall.ico").is_file()


def test_main_routes_self_test() -> None:
    """``main.main`` must not need Qt to recognise the self-test flag."""
    import main as entry

    source = Path(entry.__file__).read_text(encoding="utf-8")
    assert '"--self-test"' in source
    assert "run_self_test" in source


@pytest.mark.skipif(
    sys.platform != "win32", reason="host-specific default behaviour")
def test_host_platform_is_detected() -> None:
    assert build_config.host_platform() == "windows"
