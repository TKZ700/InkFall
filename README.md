# Inkfall

Inkfall is a desktop PDF reader and ink annotation notebook. It combines a
PDF page renderer with a pressure-aware pen/highlighter canvas, shape
recognition with live adjustment, and a document library with folders.

> **Status: `0.1.0` early public preview.**
> The feature scope for this preview is implemented in source, but the
> project is not release-ready: no binary has been published, PyMuPDF
> distribution licensing is unresolved, and manual QA on real hardware is
> still required. See [Known limitations](#known-limitations).

## What Inkfall does

- **PDF reading** — open documents in tabs, page navigation, continuous and
  paged layouts, zoom, rotation, and a read-only session mode.
- **Ink** — pen and highlighter strokes with pressure-sensitive width,
  eraser, selection, move/copy/paste, and undo/redo.
- **Shapes** — freehand recognition of lines, arrows, rectangles, circles,
  triangles and polygons; recognised lines and closed shapes can be
  adjusted by dragging handles, with endpoint snapping to nearby geometry.
- **Library** — import PDFs, create folders, List and Grid views, search,
  sorting, thumbnails, and drag-and-drop of books between folders.
- **Persistence** — annotations are saved into the PDF as an embedded
  Inkfall manifest (`inkfall.annotations.v1.json`), so a saved document
  carries its Inkfall annotations with it.

This list describes what the source implements. It is not a performance or
stability guarantee, and much of it has only been exercised by automated
tests, not by a broad manual test pass.

## Install the Windows preview build

The preview installer is produced by GitHub Actions, not published as a
release:

1. Open the repository's **Actions** tab and select the **Build preview
   artifacts** workflow.
2. Run it with **Run workflow** (or open a completed manual run).
3. Download the `inkfall-windows-x86_64` artifact and extract
   `Inkfall-0.1.0-windows-x86_64-setup.exe`.
4. Run the installer. It installs for the current user only (no
   administrator rights and no UAC prompt) and creates Desktop and
   Start Menu shortcuts for that user.

The install location is `%LOCALAPPDATA%\Programs\InkFall`. Uninstall from
**Settings → Apps → InkFall**, or by running `uninstall.exe` in the install
directory. Uninstalling removes the installation, both shortcuts and its
registry entry, and leaves your documents and settings alone.

The installer bundles the Python runtime, PySide6/Qt, the QML components and
Inkfall's fonts and icons. Python and Nuitka are **not** required on the
target machine.

### Windows SmartScreen

The preview installers are **not code-signed**. Windows may show a
"Windows protected your PC" prompt; choose **More info → Run anyway** only
if you trust the build source. A signed installer is future work.

## Use the Linux preview build

The Linux artifact is a standalone archive, not a distribution package:

```sh
tar -xzf inkfall-0.1.0-linux-x86_64.tar.gz
cd inkfall-0.1.0-linux-x86_64
./inkfall
```

`inkfall` is a launcher for the bundled `Inkfall` executable. Keep the
extracted directory intact; the executable loads `PySide6/`, `qml/` and
`assets/` from beside itself.

The archive bundles Python, PySide6/Qt and the application resources, but
**not** the system libraries Qt expects: `libgl1`, `libegl1`,
`libxkbcommon-x11-0`, `libxcb-cursor0`, `libdbus-1-3` and `libfontconfig1`
(package names vary by distribution). It also requires a glibc at least as
new as the distribution the archive was built on — the workflow builds on
`ubuntu-24.04`, so archives from it require glibc 2.39 or newer. Older
distributions are not expected to work and have not been tested.

## Run from source

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run python main.py
```

`uv sync` installs runtime and development dependencies only. Nuitka lives
in the optional `build` group and is never needed for normal development.

## Build installers locally

Builds are native-only: Windows artifacts must be built on Windows, Linux
artifacts on Linux. There is no cross-compilation path.

```sh
uv sync --group build
uv run python scripts/build.py --platform windows
uv run python scripts/build.py --platform linux
```

Outputs (with the version taken from `pyproject.toml`):

| Platform | File |
|---|---|
| Windows | `dist/Inkfall-0.1.0-windows-x86_64-setup.exe` |
| Linux | `dist/inkfall-0.1.0-linux-x86_64.tar.gz` |

The command for the other platform fails fast with an explanation rather
than producing a broken artifact. See [BUILDING.md](BUILDING.md) for
prerequisites, verification steps and CI details.

## Where data is stored

| What | Windows | Linux |
|---|---|---|
| Library database | `%APPDATA%\Inkfall\Inkfall\inkfall\library.db` | `~/.local/share/Inkfall/Inkfall/inkfall/library.db` |
| Settings (Qt `QSettings`) | registry `HKEY_CURRENT_USER\Software\Inkfall\Inkfall` | `~/.config/Inkfall/Inkfall.conf` |
| Annotations | embedded in each saved PDF | embedded in each saved PDF |

Installers and archives never delete these locations. On Windows the
uninstaller removes only the installation directory, its Desktop and
Start Menu shortcuts and its uninstall registry entry. It uses a separate
registry key for the install directory, so uninstalling does not remove
Inkfall's settings.

Your original PDFs are never moved. Importing a document records its path
in the library database; reading position and settings are stored
separately.

## Known limitations

- **Early preview.** Expect rough edges; features may change before `1.0`.
- **No code signing** on the Windows installer, and no macOS build.
- **Windows installs are per-user only** in this preview; there is no
  all-users/Program Files install option.
- **No auto-update, crash reporting, file association, or "Open with"
  integration.** Launch documents from inside Inkfall.
- **First-run library layout.** Imported root-level books appear under an
  **Ungrouped Books** section that is collapsed at launch; expand the
  section header to see them.
- **Linux desktop behaviour is unverified.** CI only runs a headless
  `--self-test`; window managers, tablet input and HiDPI on Linux have not
  been manually tested.
- **Linux compatibility is bounded** by the glibc of the build distribution
  (see above).
- **Native stylus/pressure input** is exercised by automated tests with
  synthetic events, not by a hardware test matrix.
- **Packaging.** Wheels produced by `uv build` are not a supported
  distribution format; the standalone installers and archive are.

## Licensing — unresolved before public distribution

The project's `pyproject.toml` declares MIT, but there is no `LICENSE` file
and no distribution decision has been made. This must be resolved before
binaries are published publicly:

- **PyMuPDF** (PDF backend) is offered under **AGPL-3.0 or a commercial
  licence**. Shipping Inkfall binaries without an AGPL-compatible release or
  a commercial PyMuPDF licence is a blocker.
- **PySide6 / Qt** is offered under LGPL-3.0/GPL-3.0/commercial terms;
  LGPL redistribution has relinking and notice obligations.
- **NumPy** (BSD-3-Clause) and the bundled **Inter** and **Vazirmatn**
  fonts (both SIL OFL-1.1) also require notices.

No third-party notice file exists yet. Private CI builds do not make public
distribution legally cleared.

## Development

```sh
uv sync
uv run pytest tests/test_build_config.py -q   # build configuration
uv run python -m compileall -q app core document library rendering annotations tools
```

Project layout: `app/` (Qt UI), `annotations/` (Qt-free annotation model),
`rendering/` (tile renderer), `document/` (PDF lifecycle), `library/`
(library service and store), `core/` (shared types), `qml/` and `assets/`
(runtime resources). Only `app/` is allowed to import PySide6; see
`tests/test_import_boundaries.py`.
