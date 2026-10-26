"""Install/extract smoke tests for Inkfall release artifacts.

Run by GitHub Actions after ``scripts/build.py`` on each native runner.
It never builds anything: it installs the artifact that already exists,
launches it headlessly with ``--self-test``, and verifies that uninstall
leaves user data alone.

Usage::

    uv run python scripts/smoke_test.py --platform windows
    uv run python scripts/smoke_test.py --platform linux

Windows: silent per-user NSIS install (the installer chooses the
per-user directory; the uninstaller entry records where it went) ->
launch ``--self-test`` -> uninstall -> confirm the install directory, its
Desktop and Start Menu shortcuts and its registry entry are gone and
``%APPDATA%\\Inkfall`` survived.

Linux: extract the archive -> run the launcher with an isolated ``HOME``
-> confirm the tree is complete and executable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_config import (  # noqa: E402
    BuildError,
    SOURCE_ROOT,
    artifact_path,
    ensure_native,
    project_version,
)

_WINDOWS_INSTALLED_FILES = (
    "Inkfall.exe",
    "qml/toolprops/ColorPicker.qml",
    "qml/toolprops/NumericEditor.qml",
    "assets/fonts/Inter-Regular.ttf",
    "assets/fonts/Inter-SemiBold.ttf",
    "assets/fonts/Vazirmatn-Regular.ttf",
    "assets/fonts/Vazirmatn-SemiBold.ttf",
    "assets/fonts/Vazirmatn-OFL.txt",
    "assets/icons/new_icon.png",
    "PySide6/qt-plugins/platforms/qwindows.dll",
    "PySide6/qt-plugins/platforms/qoffscreen.dll",
    "PySide6/qml/QtQuick/Controls/qmldir",
)

_LINUX_INSTALLED_FILES = (
    "Inkfall",
    "inkfall",
    "qml/toolprops/ColorPicker.qml",
    "assets/fonts/Inter-Regular.ttf",
    "assets/fonts/Vazirmatn-Regular.ttf",
    "assets/fonts/Vazirmatn-OFL.txt",
    "assets/icons/new_icon.png",
    "PySide6/qt-plugins/platforms/libqxcb.so",
    "PySide6/qt-plugins/platforms/libqoffscreen.so",
    "PySide6/qml/QtQuick/Controls/qmldir",
)

#: Nuitka derives the Apps-list key and shortcut names from PRODUCT_NAME.
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\InkFall"
#: QSettings location for organization/application "Inkfall"/"Inkfall".
_SETTINGS_KEY = r"Software\Inkfall\Inkfall"
_SETTINGS_PROBE = "InkfallSmokeTestProbe"
_SHORTCUT_NAME = "InkFall.lnk"


def log(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def _run(command: list[str], *, env: dict[str, str] | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess:
    log("run: " + " ".join(command))
    try:
        return subprocess.run(
            command,
            env=env,
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"command timed out after {timeout}s: {' '.join(command)}") from exc


def _self_test_binary(binary: Path, work_dir: Path,
                      env_overrides: dict[str, str] | None = None) -> dict:
    """Run ``--self-test`` and return the parsed JSON report."""
    report_path = work_dir / "self-test.json"
    env = dict(os.environ)
    env.update(env_overrides or {})
    # Headless: no compositor on CI. The app forces the software Qt Quick
    # backend itself; the platform plugin must still be offscreen.
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.setdefault("XDG_RUNTIME_DIR", str(work_dir / "runtime"))
    Path(env["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)

    result = _run(
        [str(binary), "--self-test", f"--self-report={report_path}"],
        env=env,
        timeout=300,
    )
    if result.returncode != 0:
        raise BuildError(
            f"packaged self-test failed (exit {result.returncode}).\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    if not report_path.is_file():
        raise BuildError(
            "packaged self-test produced no report file.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("ok"):
        raise BuildError(
            "packaged self-test reported failures:\n"
            + json.dumps(report, indent=2, sort_keys=True))
    log(f"self-test OK (platform={report['checks'].get('qt_platform')!r}, "
        f"frozen={report.get('frozen')})")
    brand = report["checks"].get("brand_icon") or {}
    if brand:
        log("brand icon verified against new_icon.png "
            f"(ico delta={brand.get('ico_delta')}, "
            f"runtime delta={brand.get('runtime_delta')})")
    return report


def _require_files(root: Path, entries: tuple[str, ...], label: str) -> None:
    missing = [entry for entry in entries if not (root / entry).exists()]
    if missing:
        raise BuildError(f"{label} is missing: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _registry_value(hive, key: str, name: str):
    """Read one registry value, or ``None`` when absent."""
    import winreg

    try:
        with winreg.OpenKey(hive, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def _registry_install_entry():
    """Return ``(hive, DisplayName, InstallLocation)`` for an install.

    The uninstaller entry is the authority on where NSIS actually put the
    files: Nuitka's MultiUser template picks the per-user directory itself
    and ignores a ``/D=`` override.
    """
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        display_name = _registry_value(hive, _UNINSTALL_KEY, "DisplayName")
        if display_name is not None:
            return (
                hive,
                display_name,
                _registry_value(hive, _UNINSTALL_KEY, "InstallLocation"),
            )
    return None, None, None


def _registry_key_gone() -> bool:
    hive, _name, _location = _registry_install_entry()
    return hive is None


def _write_settings_probe() -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _SETTINGS_KEY) as handle:
        winreg.SetValueEx(
            handle, _SETTINGS_PROBE, 0, winreg.REG_SZ, "must survive")


def _settings_probe_survived() -> bool:
    import winreg

    return (
        _registry_value(winreg.HKEY_CURRENT_USER, _SETTINGS_KEY, _SETTINGS_PROBE)
        == "must survive"
    )


def _clear_settings_probe() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _SETTINGS_KEY, 0, winreg.KEY_SET_VALUE
        ) as handle:
            winreg.DeleteValue(handle, _SETTINGS_PROBE)
    except OSError:
        pass


def _wait_for_removal(path: Path, timeout: int = 300) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(1.0)
    return not path.exists()


def _start_menu_shortcut() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise BuildError("APPDATA is not set; cannot locate the Start Menu")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / _SHORTCUT_NAME
    )


def _desktop_shortcut() -> Path:
    """Per-user Desktop shortcut path (handles redirected Desktops)."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY, SHGFP_TYPE_CURRENT
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, 0x0010, None, 0, buf)
        if result == 0 and buf.value:
            return Path(buf.value) / _SHORTCUT_NAME
    except Exception:
        pass
    return Path.home() / "Desktop" / _SHORTCUT_NAME


def _shortcut_details(path: Path) -> dict:
    """Read a ``.lnk``'s target and icon through the Windows shell."""
    escaped = str(path).replace("'", "''")
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{escaped}'); "
        "[pscustomobject]@{TargetPath=$s.TargetPath; "
        "WorkingDirectory=$s.WorkingDirectory; "
        "IconLocation=$s.IconLocation} | ConvertTo-Json -Compress"
    )
    result = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=60,
    )
    if result.returncode != 0:
        raise BuildError(
            f"could not inspect shortcut {path} (exit "
            f"{result.returncode}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise BuildError(
            f"could not parse shortcut details for {path}: "
            f"{result.stdout!r}") from exc


def _verify_shortcut(path: Path, executable: Path, label: str) -> None:
    if not path.is_file():
        raise BuildError(f"installer did not create the {label} shortcut: {path}")
    details = _shortcut_details(path)
    target = details.get("TargetPath")
    if not target or Path(target).resolve() != executable.resolve():
        raise BuildError(
            f"{label} shortcut points at {target!r}, expected {executable}")
    icon = str(details.get("IconLocation") or "")
    if icon not in ("", ",0", "0") and \
            "inkfall.exe" not in icon.lower() and \
            "inkfall.ico" not in icon.lower():
        raise BuildError(
            f"{label} shortcut uses an unexpected icon: {icon!r}")
    log(f"{label} shortcut verified: {path} -> {target}")


def smoke_windows(version: str, work_dir: Path) -> None:
    installer = artifact_path("windows", version, SOURCE_ROOT / "dist")
    if not installer.is_file():
        raise BuildError(f"installer not found: {installer}")

    hive, existing_name, _location = _registry_install_entry()
    if hive is not None:
        raise BuildError(
            "Inkfall is already installed on this machine "
            f"({existing_name!r}); uninstall it before running the smoke test")

    shortcut = _start_menu_shortcut()
    desktop = _desktop_shortcut()
    for label, path in (("Desktop", desktop), ("Start Menu", shortcut)):
        if path.exists():
            raise BuildError(
                f"a stale {label} shortcut exists: {path}; remove it first")

    # The installer is per-user, so it chooses the directory itself; the
    # uninstaller entry records where it went.
    log("silent per-user install")
    result = _run([str(installer), "/S"], timeout=900)
    if result.returncode != 0:
        raise BuildError(
            f"installer failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    hive, display_name, install_location = _registry_install_entry()
    if hive is None:
        raise BuildError("installer did not register an uninstall entry")
    if display_name != "InkFall":
        raise BuildError(f"unexpected uninstall DisplayName: {display_name!r}")
    display_version = _registry_value(hive, _UNINSTALL_KEY, "DisplayVersion")
    if display_version != version:
        raise BuildError(
            f"uninstall entry reports version {display_version!r}, "
            f"expected {version!r}")
    if not install_location:
        raise BuildError(
            "uninstall entry has no InstallLocation; cannot verify the install")
    install_dir = Path(install_location)
    if not install_dir.is_absolute() or not install_dir.is_dir():
        raise BuildError(
            f"uninstall entry points at an invalid install directory: "
            f"{install_location!r}")
    log(f"installed at {install_dir}")
    log(f"uninstall entry registered ({display_name!r})")

    _require_files(install_dir, _WINDOWS_INSTALLED_FILES, "installed application")

    executable = install_dir / "Inkfall.exe"
    _verify_shortcut(desktop, executable, "Desktop")
    _verify_shortcut(shortcut, executable, "Start Menu")

    uninstaller = install_dir / "uninstall.exe"
    if not uninstaller.is_file():
        raise BuildError("installer did not create uninstall.exe")

    report = _self_test_binary(executable, work_dir)
    resource_root = report.get("resource_root")
    if not resource_root:
        raise BuildError("packaged self-test did not report a resource root")
    if not Path(resource_root).resolve().is_relative_to(install_dir.resolve()):
        raise BuildError(
            "packaged build resolved resources outside itself: "
            f"{resource_root} (expected under {install_dir})")

    # Persistent user data lives in %APPDATA%\Inkfall\Inkfall. Create a
    # marker so uninstall can be proven not to touch it.
    appdata = Path(os.environ.get("APPDATA", tempfile.gettempdir()))
    user_data = appdata / "Inkfall" / "Inkfall"
    user_data.mkdir(parents=True, exist_ok=True)
    marker = user_data / "smoke-marker.txt"
    marker.write_text("user data", encoding="utf-8")

    _write_settings_probe()

    log("silent uninstall")
    try:
        result = _run([str(uninstaller), "/S"], timeout=600)
        # NSIS uninstallers often return before the removal finishes.
        if not _wait_for_removal(install_dir):
            raise BuildError(
                "uninstall did not remove the installation directory: "
                f"{install_dir}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        if not marker.is_file():
            raise BuildError(
                "uninstall removed persistent user data at "
                f"{user_data}; it must leave user documents and settings alone")
        marker.unlink()
        if not _settings_probe_survived():
            raise BuildError(
                "uninstall removed the user's QSettings key "
                f"(HKCU\\{_SETTINGS_KEY})")
        for label, path in (("Desktop", desktop), ("Start Menu", shortcut)):
            if not _wait_for_removal(path, timeout=60):
                raise BuildError(
                    f"uninstall left the {label} shortcut behind: {path}")
        if not _registry_key_gone():
            raise BuildError("uninstall left the uninstall registry entry behind")
    finally:
        _clear_settings_probe()
    log("uninstall clean; user data preserved")


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def smoke_linux(version: str, work_dir: Path) -> None:
    archive = artifact_path("linux", version, SOURCE_ROOT / "dist")
    if not archive.is_file():
        raise BuildError(f"archive not found: {archive}")

    extract_dir = work_dir / "extract"
    shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True)
    log(f"extract {archive.name}")
    with tarfile.open(archive, "r:gz") as tar:
        # Our own CI artifact: keep the recorded Unix modes (exec bits).
        tar.extractall(extract_dir, filter="fully_trusted")  # noqa: S202

    roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
    if len(roots) != 1:
        raise BuildError(
            f"archive should contain exactly one directory, found: "
            f"{[entry.name for entry in roots]}")
    root = roots[0]

    _require_files(root, _LINUX_INSTALLED_FILES, "extracted distribution")

    for executable in (root / "Inkfall", root / "inkfall"):
        if not os.access(executable, os.X_OK):
            raise BuildError(f"{executable.name} is not executable after extraction")

    home = (work_dir / "home").resolve()
    if home == Path.home().resolve():
        raise BuildError("refusing to run the smoke test with the real HOME")
    home.mkdir(parents=True, exist_ok=True)

    log(f"launch via launcher (isolated HOME={home})")
    report = _self_test_binary(root / "inkfall", work_dir,
                               {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                                "XDG_DATA_HOME": str(home / ".local" / "share")})
    resource_root = report.get("resource_root")
    if not resource_root:
        raise BuildError("packaged self-test did not report a resource root")
    if not Path(resource_root).resolve().is_relative_to(root.resolve()):
        raise BuildError(
            "packaged build resolved resources outside itself: "
            f"{resource_root} (expected under {root})")


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a built Inkfall artifact.")
    parser.add_argument("--platform", choices=("windows", "linux"), default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    options = parser.parse_args(argv)
    platform = options.platform or ("windows" if sys.platform.startswith("win")
                                    else "linux")
    work_dir = options.work_dir or (SOURCE_ROOT / "build" / "smoke")
    work_dir.mkdir(parents=True, exist_ok=True)
    version = project_version()
    try:
        ensure_native(platform)
        if platform == "windows":
            smoke_windows(version, work_dir)
        else:
            smoke_linux(version, work_dir)
    except BuildError as exc:
        print(f"\n[smoke] FAILED: {exc}", file=sys.stderr)
        return 1
    log("all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
