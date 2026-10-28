# Building Inkfall

This document covers producing the `0.1.0` preview artifacts: a Windows x64
NSIS setup installer and a Linux x86_64 standalone `.tar.gz`.

Builds are **native-only**. Nuitka compiles C against the host toolchain and
links the host's Qt libraries, so there is no supported cross-compilation
path. The build script refuses to build a platform that does not match the
host and points at GitHub Actions instead.

## Version and naming

`pyproject.toml` is the only version source. `scripts/build_config.py` reads
it and derives everything else:

| Output | Filename |
|---|---|
| Windows setup installer | `Inkfall-<version>-windows-x86_64-setup.exe` |
| Linux archive | `inkfall-<version>-linux-x86_64.tar.gz` |

For `0.1.0`:

```
dist/Inkfall-0.1.0-windows-x86_64-setup.exe
dist/inkfall-0.1.0-linux-x86_64.tar.gz
```

## Python dependencies

Nuitka is an optional dependency group, not a runtime dependency:

```sh
uv sync                 # normal development, no Nuitka
uv sync --group build   # adds the locked Nuitka version
```

The group is pinned to `nuitka==4.3.1` in `pyproject.toml` and locked in
`uv.lock`. Do not install Nuitka globally; always go through `uv`.

## Platform prerequisites

These are OS-level requirements and are **not** installed by `uv`.

### Windows

- MSVC C/C++ toolchain: Visual Studio Build Tools with the
  **Desktop development with C++** workload (the GitHub `windows-2022`
  runner includes it).
- NSIS 3.x. If `makensis` is not on `PATH`, Nuitka downloads and caches the
  official NSIS release. If outbound downloads are blocked, install NSIS
  system-wide so `makensis` is on `PATH`.
- Several GB of free disk space for the Nuitka work directory.

### Linux (x86_64)

- `gcc` and libc headers (`build-essential`).
- `patchelf` (Nuitka standalone mode requires it).
- `tar`.
- Qt runtime libraries used by the headless smoke test:
  `libgl1`, `libegl1`, `libxkbcommon-x11-0`, `libxcb-cursor0`,
  `libdbus-1-3`, `libfontconfig1`.

On Debian/Ubuntu:

```sh
sudo apt-get install build-essential patchelf tar \
  libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-keysyms1 libxcb-render-util0 libxcb-image0 \
  libxcb-util1 libdbus-1-3 libfontconfig1
```

Other distributions: install the equivalents; the package names above are
Debian/Ubuntu-specific.

## Build commands

Windows (from the repository root):

```sh
uv sync --group build
uv run python scripts/build.py --platform windows
```

Linux (from the repository root):

```sh
uv sync --group build
uv run python scripts/build.py --platform linux
```

Both commands:

1. verify the host matches the requested platform;
2. verify the required source resources exist;
3. verify Nuitka (and, on Linux, `cc`/`patchelf`/`tar`) are available;
4. run Nuitka in **standalone** mode with an explicit resource manifest;
5. verify the standalone tree contains the executable, Qt plugins, QML
   modules, fonts and icons;
6. produce the installer or archive in `dist/`.

Useful flags:

| Flag | Effect |
|---|---|
| `--clean` | remove previous `build/archive`, `build/logs`, `build/smoke`, Nuitka `*.dist`/`*.build` directories and the artifact first |
| `--verify-only` | re-check an existing artifact; never invokes Nuitka |
| `--build-dir PATH` | alternate intermediate directory (default `build/`) |
| `--dist-dir PATH` | alternate artifact directory (default `dist/`) |

Exit code `0` means the artifact was produced and passed verification.
Failures print the exact cause and, for Nuitka, the path to the full compiler
log under `build/logs/`.

## What gets bundled

`scripts/build_config.py` is the authoritative list. The important part is
that nothing dynamic is left to import analysis:

- `--mode=standalone` — a directory with the Python runtime and extension
  modules, not a single executable.
- `--enable-plugins=pyside6` — PySide6 libraries and Nuitka's default Qt
  plugin families (platforms, image formats, styles, TLS, …).
- `--include-qt-plugins=qml` — the Qt QML plugin family, requested
  explicitly. Nuitka's defaults do **not** include `qml`, so without this
  flag every bundled `.qml` file logs *“not having Qt qml plugins is unlikely
  to work”*; the family used to be added only later, implicitly, once the
  build encountered `QtQuickWidgets`. Nuitka 4.3.1 evaluates this option in
  `onCompilationStartChecks`, which runs **before** `--include-data-dir`
  files are registered, so the explicit request also prevents that warning
  instead of merely arriving afterwards. No other plugin family is added.
- `--include-data-dir=qml=qml` — every `.qml` component, copied to `<dist>/qml`.
- `--include-data-dir=assets=assets` — Inter and Vazirmatn fonts, the
  Vazirmatn OFL notice, brand icons and PNGs, copied to `<dist>/assets`.
- Windows metadata (`--company-name`, `--product-name`, `--file-description`,
  `--product-version`, `--file-version`) and the `.ico`.
- Windows installer options: `--windows-create-installer`,
  `--windows-installer-shortcuts=desktop,start-menu`,
  `--windows-installer-mode=user`, `--windows-installer-output=…`.

After Nuitka finishes, the build verifies the standalone tree against a
manifest including:

- the executable;
- `PySide6/qt-plugins/platforms/qwindows.dll` (Windows) or
  `PySide6/qt-plugins/platforms/libqxcb.so` (Linux);
- `PySide6/qml/QtQuick/Controls/qmldir` and `…/Layouts/qmldir`;
- `qml/toolprops/*.qml`;
- `assets/fonts/Inter-*.ttf`, `assets/fonts/Vazirmatn-*.ttf`,
  `assets/fonts/Vazirmatn-OFL.txt`, `assets/icons/inkfall.ico`,
  `assets/icons/new_icon.png`.

A missing entry fails the build instead of shipping a half-populated
artifact.

### Application icon

`assets/icons/new_icon.png` is the authoritative Inkfall brand mark, and the
running application draws its window/taskbar icon from it (`app/icons.py`).
Windows additionally needs a multi-resolution ICO for the executable, the
installer and the Desktop/Start Menu shortcuts, so `assets/icons/inkfall.ico`
is **generated** from that PNG:

```sh
uv run python scripts/make_icon.py           # rewrite the ICO
uv run python scripts/make_icon.py --check   # fail if it is stale
```

The generator never modifies the PNG, preserves its transparency, colours
and framing, and refuses to upscale. It writes 10 frames (16, 20, 24, 32,
40, 48, 64, 96, 128, 256 px) at 32 bpp: the 256 px frame is PNG-compressed
inside the ICO (the Windows convention for large icons) and the smaller ones
are BGRA DIBs with an AND mask. Qt's PNG encoder derives `pHYs` from the
active screen, so the generator pins 96 dpi to keep the committed bytes
reproducible.

One file drives every Windows brand surface:

- `--windows-icon-from-ico` embeds it in `Inkfall.exe`;
- Nuitka reuses the same path as the NSIS `MUI_ICON`, i.e. the setup
  executable's icon and wizard branding;
- the Desktop and Start Menu shortcuts and the uninstall entry's
  `DisplayIcon` point at `Inkfall.exe`, so they inherit the embedded icon.

Changing the artwork therefore means regenerating and committing the ICO;
`tests/test_build_icons.py` decodes the ICO and compares it pixel-by-pixel
with the resized PNG, so a valid-but-obsolete icon fails the build checks.
Windows caches shell icons: after installing a rebuilt setup, run
`ie4uinit.exe -show` (or reinstall into a clean directory) if Explorer still
shows the previous artwork.

### Bundled fonts

`assets/fonts/` ships Inter (Latin UI) and Vazirmatn (Persian/Arabic),
both regular/medium/semibold. `app/fonts.py` registers all six files with
`QFontDatabase` and installs an application font stack with Inter first and
Vazirmatn second; `app/theme/typography.py` uses the same stack for role
fonts, and QML controls inherit it (they no longer request a single family).
Vazirmatn is pinned to upstream release `v33.003` and its SIL OFL notice
ships as `assets/fonts/Vazirmatn-OFL.txt`.

Visual glyph shaping/line spacing cannot be fully proven headlessly. After a
packaged build, manually open a document whose name and labels mix Persian
and Latin text and confirm both scripts render without missing or clipped
glyphs.

### Resource resolution

Runtime code resolves `qml/` and `assets/` through `app/resources.py`:

- frozen build → beside the executable (`sys.executable` directory);
- source checkout → repository root.

`app/fonts.py`, `app/icons.py` and `app/qml_bridge/hosting.py` all use it, so
the same code works from a checkout and from an installed directory.

### Windows installer behaviour

- Installs the complete standalone tree for the current user under
  `%LOCALAPPDATA%\Programs\InkFall` (no administrator rights, no UAC prompt).
- Creates **Desktop** and **Start Menu** shortcuts named `InkFall`; both
  launch the installed `Inkfall.exe` and inherit its embedded icon.
- Registers the application in the Windows Apps list under the exact
  display name `InkFall`
  (`Software\Microsoft\Windows\CurrentVersion\Uninstall\InkFall`).
- Uninstalling removes the installation directory, both shortcuts and the
  uninstall registry entry. It does **not** touch
  `%APPDATA%\Inkfall` or the QSettings key `Software\Inkfall\Inkfall`.

### Linux archive behaviour

- Expands to a single `inkfall-<version>-linux-x86_64/` directory.
- Contains the standalone tree plus an executable `inkfall` launcher and a
  `README.txt`.
- Preserves Unix executable bits (`/Inkfall`, `/inkfall`) — the build
  verifies them from inside the archive.

## Verification and smoke tests

The build verifies structure only. The smoke test additionally proves the
artifact installs and launches:

```sh
uv run python scripts/smoke_test.py --platform windows
uv run python scripts/smoke_test.py --platform linux
```

**Windows:** silent per-user install (no `/D=` — the installer chooses
`%LOCALAPPDATA%\Programs\InkFall` and records it as `InstallLocation`), verify
the uninstall entry (`DisplayName`, `DisplayVersion`), the installed tree,
the Desktop and Start Menu shortcuts and their target/icon, run
`Inkfall.exe --self-test` with
`QT_QPA_PLATFORM=offscreen` and confirm it resolved resources from inside the
install directory, write a marker in `%APPDATA%\Inkfall\Inkfall` plus a probe
value under the QSettings key `HKCU\Software\Inkfall\Inkfall`, run the
uninstaller, then confirm the installation directory, both shortcuts and the
uninstall entry are gone while the marker and the QSettings probe survive.

**Linux:** extract the archive, check files and executable bits, run the
`inkfall` launcher with `QT_QPA_PLATFORM=offscreen` and an isolated `HOME`,
and confirm the self-test resolved resources inside the extracted directory.

`--self-test` is a build-verification path, not a user feature. It loads the
bundled fonts, compares the shipped ICO's largest frame **and** the runtime
`app_icon()` render against the bundled `assets/icons/new_icon.png`
(pixel-mean delta), loads the three tool-shelf QML components, then writes a
JSON report (`--self-report PATH`) and exits `0`/`1`. The smoke test logs the
measured icon deltas, so CI shows that branding was checked rather than
merely that a file exists.

A headless self-test is **not** a desktop-compositor test. Window managers,
HiDPI, stylus input and X11/Wayland differences still need manual testing.

## GitHub Actions

Workflow: `.github/workflows/build.yml` (**Build preview artifacts**).

| Job | Runner | Output |
|---|---|---|
| `windows-installer` | `windows-2022` | `Inkfall-0.1.0-windows-x86_64-setup.exe` |
| `linux-archive` | `ubuntu-24.04` | `inkfall-0.1.0-linux-x86_64.tar.gz` |
| `publish-release` | `ubuntu-24.04` | GitHub Release with both files (version tags only) |

Triggers:

- `workflow_dispatch` — manual run; uploads both artifacts.
- `pull_request` — builds and smoke-tests, uploads nothing.
- `push` to `main` — builds and smoke-tests, uploads nothing.
- `push` of a version tag `v<version>` (e.g. `v0.1.0`) — the same two
  builds, plus the `publish-release` job described below.

Each job uses `uv sync --locked --group build`, i.e. the same lockfile and
the same `scripts/build.py` as local builds. `--locked` makes the job fail
if `pyproject.toml` and `uv.lock` disagree. Both jobs pin Python 3.11 in
`setup-uv`, matching the development interpreter and the locked PySide6
wheel used for testing. Both also run the focused packaging checks
(`tests/test_build_config.py`, `tests/test_build_icons.py`,
`tests/test_vazirmatn_font.py`, `tests/test_preview_backgrounds.py`) before
Nuitka starts, so a stale ICO, a missing font/licence, a preview-background
regression, a missing `--windows-icon-from-ico`, or a dropped
`--include-qt-plugins=qml` fails the run before the long compile.

Permissions are `contents: read` at the workflow level. Ordinary runs
(manual dispatch, pull requests, branch pushes) never create a tag, release
or public asset. The only exception is a pushed version tag `v<version>`
that matches the project version: after both builds and smoke tests
succeed, the `publish-release` job — the only job granted `contents: write`
— creates a GitHub Release for that existing tag, uploads the two
distribution files as release assets and attaches GitHub-generated release
notes. The release is marked as a prerelease while the project is an early
preview. The workflow never deletes or overwrites an existing release or
asset, and never creates a git tag. See [RELEASING.md](RELEASING.md) for
the release-readiness gate (the open obligations in
`THIRD_PARTY_NOTICES.md` must be complete first) and the recovery
procedure for a partial release. On failure the workflow uploads the
Nuitka log and the self-test JSON so the two jobs stay independently
diagnosable.

The Linux job installs the Qt runtime libraries listed above before the
smoke test; the Linux build baseline is therefore **Ubuntu 24.04 (glibc
2.39)**. Do not claim broader compatibility without testing it.

### Downloading artifacts

1. Open **Actions → Build preview artifacts**.
2. Pick a run started with **Run workflow**.
3. Download from **Artifacts** at the bottom of the run page:
   - `inkfall-windows-x86_64`
   - `inkfall-linux-x86_64`

Artifacts are retained for 14 days and are not public releases. Permanent,
public downloads come from GitHub Releases created by the
`publish-release` job on a version-tag push (see [RELEASING.md](RELEASING.md)).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Nuitka is not installed` | run `uv sync --group build` |
| `cannot build the <platform> artifact on host …` | build on that platform (or use CI); cross-compilation is unsupported |
| `missing native build tools: patchelf` | install `patchelf` (Linux) |
| `fatal error C1002: compiler is out of heap space in pass 2` (Windows) | MSVC cl.exe exhausted its optimizer heap on a large generated TU. For `pymupdf` this is structural: the SWIG-generated `pymupdf.mupdf` module compiles into a ~2.4M-line C file that no `--jobs`/`--low-memory` setting can shrink, so the shared build config includes the whole `pymupdf` package as frozen bytecode (`--noinclude-custom-mode=pymupdf:bytecode`, Windows only) — the module ships and imports normally, just never as a C translation unit. `--low-memory` is also passed to bound C-compilation fan-out; if a hand-written invocation still hits C1002 on another TU, add `--low-memory` (not `-j` alone) or `--clang` |
| NSIS download fails | install NSIS 3.x and point Nuitka at it |
| `the standalone build is incomplete; missing: …` | a resource or Qt plugin was not bundled; check the Nuitka log in `build/logs/` |
| self-test fails on QML | Qt Quick Controls modules missing from the bundle; inspect `build/smoke/self-test.json` |
| self-test fails on the brand icon | the packaged ICO drifted from `new_icon.png`; run `uv run python scripts/make_icon.py` and rebuild |
| installer builds but install fails | read the NSIS output in the job log, or run the installer interactively |

## Status of these builds

No application build has been executed on the development machine for this
change. The Windows installer and Linux archive are produced by GitHub
Actions on native runners; until a workflow run completes successfully, both
artifacts must be treated as **unverified**.

## Licensing

Building these artifacts privately does **not** resolve distribution
licensing. Inkfall is `AGPL-3.0-only` (see `LICENSE`), PyMuPDF is AGPL-3.0
or commercial, and Qt is LGPL-3.0; the outstanding distribution
obligations are tracked in `THIRD_PARTY_NOTICES.md` and are not yet
complete. Do not publish the artifacts publicly until they are resolved.
