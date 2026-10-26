"""Build the Inkfall preview artifacts with Nuitka.

Usage::

    uv sync --group build
    uv run python scripts/build.py --platform windows
    uv run python scripts/build.py --platform linux

Windows produces ``dist/Inkfall-<version>-windows-x86_64-setup.exe``
(a real NSIS setup program). Linux produces
``dist/inkfall-<version>-linux-x86_64.tar.gz`` (a standalone tree plus an
executable launcher). Each target must be built on its own native host;
the script refuses to cross-compile.

Nuitka is an *optional* dependency: this script is the only thing that
needs it, and it is never imported by the application.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

if __package__ in (None, ""):  # ``python scripts/build.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_config import (  # noqa: E402
    BuildError,
    SOURCE_ROOT,
    WINDOWS_ICON,
    artifact_path,
    ensure_native,
    host_platform,
    linux_archive_root,
    missing_bundle_entries,
    nuitka_arguments,
    platform_requirements,
    project_version,
    required_bundle_entries,
    standalone_binary_name,
)

#: Launcher/normaliser shipped inside the Linux archive.
LINUX_LAUNCHER_SOURCE = SOURCE_ROOT / "packaging" / "linux" / "inkfall.sh"
LINUX_README_SOURCE = SOURCE_ROOT / "packaging" / "linux" / "README.txt"

#: Files that must exist before Nuitka starts.
_REQUIRED_SOURCES = (
    "main.py",
    "pyproject.toml",
    "qml/toolprops/ColorPicker.qml",
    "qml/toolprops/NumericEditor.qml",
    "qml/toolprops/PaletteStrip.qml",
    "assets/fonts/Inter-Regular.ttf",
    WINDOWS_ICON,
    "assets/icons/new_icon.png",
)

_MIN_ARTIFACT_BYTES = 1_000_000


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def _require_sources() -> None:
    missing = [rel for rel in _REQUIRED_SOURCES if not (SOURCE_ROOT / rel).exists()]
    if missing:
        raise BuildError(
            "the checkout is missing required build inputs: " + ", ".join(missing))


def _require_nuitka() -> None:
    if importlib.util.find_spec("nuitka") is None:
        raise BuildError(
            "Nuitka is not installed in this environment. It is an optional "
            "build dependency:\n"
            "    uv sync --group build\n"
            "then re-run this command. Normal development does not need it.")


def _require_tools(platform: str) -> None:
    if platform == "linux":
        missing = [tool for tool in ("cc", "patchelf", "tar") if shutil.which(tool) is None]
        if missing:
            raise BuildError(
                "missing native build tools: " + ", ".join(missing) + ".\n"
                "Install them with your distribution's package manager, e.g.\n"
                "    sudo apt-get install build-essential patchelf tar")
    else:
        # NSIS is fetched and cached by Nuitka when it is not on PATH; the
        # MSVC toolchain is located by Nuitka itself.
        if shutil.which("makensis") is None:
            log("makensis not on PATH; Nuitka will use its cached NSIS download")


def _preflight(platform: str) -> None:
    if sys.version_info < (3, 11):
        raise BuildError("Inkfall requires Python 3.11 or newer to build")
    _require_sources()
    _require_nuitka()
    _require_tools(platform)


def _clean(platform: str, build_root: Path, dist_dir: Path, version: str) -> None:
    """Remove only this project's generated directories and artifact."""
    targets = [
        build_root / "archive",
        build_root / "logs",
        build_root / "smoke",
    ]
    for target in targets:
        _remove_tree(target, build_root)
    for pattern in ("*.dist", "*.build", "*.onefile-build"):
        for target in sorted(build_root.glob(pattern)):
            _remove_tree(target, build_root)
    artifact = artifact_path(platform, version, dist_dir)
    if artifact.is_file():
        log(f"removing previous artifact {artifact}")
        artifact.unlink()


def _remove_tree(path: Path, build_root: Path) -> None:
    """Delete ``path`` only if it is safely inside the build directory."""
    try:
        resolved = path.resolve()
        root = build_root.resolve()
    except OSError:
        return
    if root not in resolved.parents:
        raise BuildError(f"refusing to delete {resolved}: outside {root}")
    if resolved.exists():
        log(f"removing {resolved}")
        shutil.rmtree(resolved, ignore_errors=False)


def _find_standalone_dir(build_root: Path, platform: str) -> Path:
    expected = build_root / f"{Path(standalone_binary_name(platform)).stem}.dist"
    if expected.is_dir():
        return expected
    candidates = sorted(p for p in build_root.glob("*.dist") if p.is_dir())
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise BuildError(
            f"Nuitka produced no standalone directory in {build_root}; "
            "see the compiler output above")
    raise BuildError(
        "ambiguous standalone directories in "
        f"{build_root}: {', '.join(p.name for p in candidates)}")


def _run_nuitka(platform: str, version: str, build_root: Path, dist_dir: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    build_root.mkdir(parents=True, exist_ok=True)
    args = nuitka_arguments(
        platform,
        version,
        source_root=SOURCE_ROOT,
        build_root=build_root,
        dist_dir=dist_dir,
    )
    log("running Nuitka standalone build")
    log("  " + " ".join(args))

    # Stream the compiler output and keep a copy: CI uploads this log when
    # the job fails, where the raw console is hard to search.
    log_path = build_root / "logs" / f"nuitka-{platform}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "nuitka", *args],
            cwd=str(SOURCE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log_file.write(line)
        returncode = process.wait()

    if returncode != 0:
        raise BuildError(
            f"Nuitka failed with exit code {returncode}. Full output: {log_path}")
    log(f"Nuitka output saved to {log_path}")


def _verify_standalone(platform: str, standalone_dir: Path) -> None:
    missing = missing_bundle_entries(platform, standalone_dir)
    if missing:
        raise BuildError(
            "the standalone build is incomplete; missing:\n  "
            + "\n  ".join(missing)
            + "\nCheck the --include-data-dir arguments and the PySide6 plugin.")
    log(f"standalone tree verified: {standalone_dir}")


def _finalize_windows(version: str, build_root: Path, dist_dir: Path) -> Path:
    artifact = artifact_path("windows", version, dist_dir)
    if not artifact.is_file():
        # Nuitka honours --windows-installer-output; if its naming ever
        # changes, adopt the single setup executable it did produce rather
        # than silently shipping nothing.
        candidates = sorted(build_root.glob("*-setup.exe"))
        if len(candidates) != 1:
            raise BuildError(
                "Nuitka did not create the Windows installer. Expected "
                f"{artifact}; look for NSIS errors above.")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidates[0]), str(artifact))
    _verify_artifact(artifact)
    return artifact


def _finalize_linux(version: str, build_root: Path, dist_dir: Path,
                    standalone_dir: Path) -> Path:
    archive_root_name = linux_archive_root(version)
    staging_parent = build_root / "archive"
    staging_root = staging_parent / archive_root_name
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_parent.mkdir(parents=True, exist_ok=True)
    log(f"staging {archive_root_name}")
    shutil.copytree(standalone_dir, staging_root, symlinks=True)

    if not LINUX_LAUNCHER_SOURCE.is_file():
        raise BuildError(f"missing Linux launcher template: {LINUX_LAUNCHER_SOURCE}")
    launcher = staging_root / "inkfall"
    shutil.copy2(LINUX_LAUNCHER_SOURCE, launcher)
    launcher.chmod(0o755)
    if LINUX_README_SOURCE.is_file():
        shutil.copy2(LINUX_README_SOURCE, staging_root / "README.txt")

    artifact = artifact_path("linux", version, dist_dir)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.exists():
        artifact.unlink()
    log(f"creating {artifact.name}")
    try:
        subprocess.run(
            [
                "tar",
                "-czf", str(artifact),
                "--owner=0", "--group=0",
                "-C", str(staging_parent),
                archive_root_name,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"tar failed with exit code {exc.returncode}") from exc

    _verify_archive(platform="linux", archive=artifact, root_name=archive_root_name)
    return artifact


def _verify_archive(*, platform: str, archive: Path, root_name: str) -> None:
    """Check archive contents and Unix executable bits."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = {member.name: member for member in tar.getmembers()}
    except (OSError, tarfile.TarError) as exc:
        raise BuildError(f"cannot read {archive}: {exc}") from exc

    missing = [entry for entry in required_entries(platform, root_name)
               if entry not in members]
    if missing:
        raise BuildError(
            "the archive is missing required entries:\n  " + "\n  ".join(missing))

    binary = members[f"{root_name}/Inkfall"]
    if not binary.mode & 0o111:
        raise BuildError(f"{root_name}/Inkfall is not executable in the archive")
    launcher = members.get(f"{root_name}/inkfall")
    if launcher is None:
        raise BuildError(f"{root_name}/inkfall launcher missing from the archive")
    if not launcher.mode & 0o111:
        raise BuildError(f"{root_name}/inkfall is not executable in the archive")
    log(f"archive verified: {len(members)} entries")


def required_entries(platform: str, root_name: str) -> list[str]:
    """Archive member names that must be present (expanded from patterns)."""
    entries = [f"{root_name}/{name}" for name in required_bundle_entries(platform)
               if not any(char in name for char in "*?[")]
    entries.append(f"{root_name}/inkfall")
    return entries


def _verify_artifact(artifact: Path) -> None:
    if not artifact.is_file():
        raise BuildError(f"expected artifact not found: {artifact}")
    size = artifact.stat().st_size
    if size < _MIN_ARTIFACT_BYTES:
        raise BuildError(
            f"{artifact} is only {size} bytes; the build did not produce a "
            "usable artifact")
    log(f"artifact: {artifact} ({size / 1_048_576:.1f} MiB)")


def _build(platform: str, *, clean: bool, build_root: Path, dist_dir: Path,
           verify_only: bool = False) -> Path:
    ensure_native(platform)
    version = project_version()
    log(f"Inkfall {version} / {platform} (host {sys.platform})")
    log(f"build directory: {build_root}")
    log(f"output directory: {dist_dir}")

    if verify_only:
        artifact = artifact_path(platform, version, dist_dir)
        if not artifact.is_file():
            raise BuildError(f"artifact not found: {artifact}")
        if platform == "windows":
            _verify_artifact(artifact)
        else:
            _verify_archive(platform="linux", archive=artifact,
                            root_name=linux_archive_root(version))
        return artifact

    _preflight(platform)
    if clean:
        _clean(platform, build_root, dist_dir, version)

    started = time.monotonic()
    _run_nuitka(platform, version, build_root, dist_dir)
    standalone_dir = _find_standalone_dir(build_root, platform)
    _verify_standalone(platform, standalone_dir)

    if platform == "windows":
        artifact = _finalize_windows(version, build_root, dist_dir)
    else:
        artifact = _finalize_linux(version, build_root, dist_dir, standalone_dir)

    log(f"done in {time.monotonic() - started:.1f}s")
    return artifact


def _parser() -> argparse.ArgumentParser:
    prerequisite_lines: list[str] = []
    for name in ("windows", "linux"):
        prerequisite_lines.append(f"{name} prerequisites:")
        prerequisite_lines.extend(f"  {item}" for item in platform_requirements(name))
        prerequisite_lines.append("")
    parser = argparse.ArgumentParser(
        description="Build an Inkfall preview artifact with Nuitka (native host only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(prerequisite_lines).rstrip(),
    )
    parser.add_argument(
        "--platform",
        choices=("windows", "linux"),
        default=None,
        help="target platform (default: this host)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove previous build/artifact for this platform first",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=SOURCE_ROOT / "build",
        help="intermediate build directory (default: build/)",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=SOURCE_ROOT / "dist",
        help="artifact output directory (default: dist/)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="only re-check an existing artifact; never invokes Nuitka",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    platform = options.platform or host_platform()
    if platform not in ("windows", "linux"):
        print(
            f"[build] error: unsupported host {sys.platform!r}; build on "
            "Windows or Linux (or use GitHub Actions).",
            file=sys.stderr,
        )
        return 1
    try:
        artifact = _build(
            platform,
            clean=options.clean,
            build_root=options.build_dir.resolve(),
            dist_dir=options.dist_dir.resolve(),
            verify_only=options.verify_only,
        )
    except BuildError as exc:
        print(f"\n[build] FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"\n[build] artifact ready: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
