# Inkfall

Inkfall is a stylus-first PDF annotation and study application for
Windows and Linux. It pairs a continuous PDF reader with a responsive
handwriting canvas, hold-to-recognize smart shapes, and a Library built
around organizing the documents you actually study.

> **Status: `0.1.1` early public preview.**
> The feature scope for this preview is implemented in source, but the
> project is not release-ready: no binary has been published, the
> third-party notice and redistribution obligations tracked in
> [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) are not yet complete,
> and manual QA on real hardware is still required. See
> [Known limitations](#known-limitations).

## Overview

Inkfall is designed for sustained reading and annotation: a focused
workspace built around the document, a configurable tool shelf instead of
a crowded fixed toolbar, and reading modes that keep long sessions
comfortable. Everything you write is stored inside the PDF itself, so an
annotated document carries its Inkfall notes with it.

The ink engine is built for natural handwriting: strokes are rendered
from real input samples with smoothing and per-event incremental
updates, and stylus pressure can modulate stroke width where the device
provides it. There are no published latency or frame-rate figures — the
goal is simply an inking experience that keeps up with the pen.

## Key features

### Inking

- **Pen and highlighter** with smooth, pressure-aware stroke rendering
  (per-tool pressure toggle, with a selectable pressure profile in
  settings; devices without pressure fall back to a constant width).
- **Eraser** with whole-stroke and partial-erase modes.
- **Selection, move, copy and paste** for ink, with full undo/redo.
- Strokes are stored as vector ink with their input history, not
  flattened bitmaps.

### Hold-to-recognize smart shapes

Draw a rough shape and **hold the stylus down briefly** (about a third
of a second, no need to be perfectly still). Inkfall replaces the rough
stroke with a clean recognized shape:

- **Recognized kinds:** lines, rectangles, circles, triangles, and
  polygons (polygons may be drawn as several connected strokes and are
  merged into one shape).
- **Live adjustment:** with the stylus still down after recognition,
  keep moving to adjust the recognized geometry before releasing —
  recognized lines and closed shapes (rectangle, circle, triangle,
  polygon) support this; line endpoints can snap to nearby geometry.
- Highlighter conversions are not adjustable: moving the pen after a
  highlighter hold cancels the conversion instead.

### A toolbar you configure

Inkfall's tool shelf holds your tools as configurable instances rather
than a fixed set:

- Add, duplicate, remove and drag-reorder tool slots; keep a wide red
  pen, a thin blue pen, and a soft highlighter side by side.
- Per-tool properties: color, width, opacity, fill (color, opacity,
  on/off for closed shapes), line style, endings, angle snapping and
  more, depending on the tool.
- Reusable color, size and opacity presets, including a permanent
  **No Fill** slot on fill strips.
- Dock the shelf on the left, right, top or bottom edge; Pan and
  Selection are always present as built-ins.

### Reading modes for every environment

Three distinct PDF appearance modes — **Light** (original colors),
**Dark** (night reading: lightness inverted while hue and saturation are
preserved), and **Inverted** (full-color inversion) — so on-screen
reading and studying stay comfortable in bright rooms and at night.
Selection happens in the view controls panel and applies to the rendered
pages, not just the UI chrome.

### A study-focused workspace

- A clean reading surface: the document fills the workspace, and
  scrollbars, HUD and shelf fade away when idle.
- **Library** home for your study materials: import PDFs, organize them
  in folders (with drag-and-drop), recent/favorites/recency views, List
  and Grid layouts, global search, and lazy thumbnails.
- Reading position is remembered per document and restored on reopen.
- Page navigator with thumbnails, continuous and paged layouts, zoom
  and rotation, fit-width/fit-page modes, and a fullscreen reading mode.
- Read-only sessions: opening a document that is already open offers a
  non-persisting read-only copy instead of a second editor.

### Annotations that travel with the PDF

Saving embeds an Inkfall annotation manifest inside the PDF
(`inkfall.annotations.v1.json`) alongside native PDF annotations, so a
saved document carries its Inkfall ink with it; the original file is
never moved or modified while you work.

### Language and typography

The UI ships with the **Inter** typeface, and **Vazirmatn** is bundled
so Persian/Arabic text renders correctly even where neither font is
installed; Arabic-script runs inside mixed strings fall back to
Vazirmatn per character.

This list describes what the source implements. It is not a performance
or stability guarantee, and much of it has only been exercised by
automated tests, not by a broad manual test pass.

## Install the Windows preview build

The preview installer is produced by GitHub Actions, not published as a
release:

1. Open the repository's **Actions** tab and select the **Build preview
   artifacts** workflow.
2. Run it with **Run workflow** (or open a completed manual run).
3. Download the `inkfall-windows-x86_64` artifact and extract
   `Inkfall-0.1.1-windows-x86_64-setup.exe`.
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
tar -xzf inkfall-0.1.1-linux-x86_64.tar.gz
cd inkfall-0.1.1-linux-x86_64
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
| Windows | `dist/Inkfall-0.1.1-windows-x86_64-setup.exe` |
| Linux | `dist/inkfall-0.1.1-linux-x86_64.tar.gz` |

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
  synthetic events, not by a hardware test matrix. Pressure behaviour
  therefore varies by device and driver; Inkfall does not guarantee
  uniform behaviour across every stylus or tablet.
- **Shape recognition** covers lines, rectangles, circles, triangles and
  polygons; it does not recognize arrows or ovals (an ellipse drawn with
  the shape tool is a drawn shape, not a hold-to-recognize target).
- **Packaging.** Wheels produced by `uv build` are not a supported
  distribution format; the standalone installers and archive are.

## License

Inkfall is free software released under the **GNU Affero General Public
License v3.0 only** (SPDX identifier: `AGPL-3.0-only`). The complete
license text is in [LICENSE](LICENSE), and the GNU Project publishes the
official copy at <https://www.gnu.org/licenses/agpl-3.0.txt> (see
<https://www.gnu.org/licenses/#AGPL> for background on the license).

- Inkfall is available free of charge, subject to the license's terms.
- You may inspect, modify and redistribute Inkfall's covered source
  according to the AGPL.
- **Commercial use is permitted.** The AGPL does not prohibit commercial
  use; it requires that its distribution conditions are met.
- If you distribute modified versions, they must comply with the AGPL's
  requirements, including keeping the license and copyright intact and
  making corresponding source available (section 13 extends source
  obligations to users interacting with a modified version over a
  network).

Inkfall also bundles third-party components — PyMuPDF, PySide6/Qt, NumPy,
the Python runtime and the Inter and Vazirmatn fonts — whose licenses
impose their own notice, source and redistribution obligations. That
audit, including which obligations are verified and which remain open, is
in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Completing it is a
prerequisite for a public binary release.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

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
