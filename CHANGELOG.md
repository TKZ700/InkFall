# Changelog

All notable changes to this preview are recorded here. The project version
lives in `pyproject.toml`; entries below use that version.

## 0.1.0 — preview (not yet published)

First preview build of Inkfall as a packaged desktop application.

### Build and packaging

- Added an optional, locked Nuitka build dependency group (`nuitka==4.3.1`);
  normal development does not require it.
- Added `scripts/build.py`, a native-only build CLI that produces:
  - `Inkfall-0.1.0-windows-x86_64-setup.exe` (NSIS setup for the current
    user, standalone tree, Desktop and Start Menu shortcuts, uninstaller,
    user-data-safe uninstall);
  - `inkfall-0.1.0-linux-x86_64.tar.gz` (standalone tree with an executable
    `inkfall` launcher, Unix modes preserved).
- Added explicit resource bundling for `qml/` and `assets/`, plus post-build
  verification of the executable, Qt platform plugins, Qt Quick Controls
  modules, fonts and icons.
- Added `--include-qt-plugins=qml` to the shared Nuitka arguments. Nuitka's
  defaults omit the QML plugin family, which made every bundled `.qml` file
  warn that QML plugins were missing; the requirement is now declared
  explicitly instead of relying on later automatic inclusion.
- Added `scripts/make_icon.py`, which regenerates the Windows
  `assets/icons/inkfall.ico` (10 frames, 16–256 px, transparency preserved)
  from the authoritative `assets/icons/new_icon.png`. The executable,
  installer wizard and Desktop/Start Menu shortcuts now all use the current
  mark instead of the retired artwork.
- Added `tests/test_build_icons.py`, which compares decoded ICO pixels with
  the resized PNG and checks the build arguments and bundle manifest.
- Added `app/resources.py` so fonts, icons and QML resolve both from a source
  checkout and from an installed/frozen directory.
- Added `--self-test` for headless verification of a packaged build
  (resources, fonts, brand-icon pixel match, QML component loading).
- Added `.github/workflows/build.yml` with native Windows and Linux jobs,
  manual dispatch with artifact upload, pull-request builds, and
  least-privilege permissions.
- Set the project version to `0.1.0`.

### Application features present in this preview

- PDF reading with tabs, page navigation, zoom, rotation and a read-only
  session mode.
- Pen and highlighter ink with pressure-sensitive width, eraser, selection,
  clipboard operations and undo/redo.
- Shape recognition (line, arrow, rectangle, circle, triangle, polygon) with
  live line adjustment, connector snapping, and closed-shape adjustment.
- Library with folders, List/Grid views, search, sorting, thumbnails and
  book drag-and-drop between folders.
- Annotation persistence as an embedded manifest inside the saved PDF.

### Known gaps

- No code signing, auto-update, crash reporting or file associations.
- The Windows installer is per-user only (no all-users install option).
- Linux desktop behaviour is not manually verified; CI runs a headless
  self-test only.
- Packaging output is limited to the two artifacts above; wheels from
  `uv build` are not a supported distribution format.
- Project licensing decided: `AGPL-3.0-only`, with a root `LICENSE`
  (GNU AGPL-3.0 text), `CONTRIBUTING.md` and `THIRD_PARTY_NOTICES.md`.
- Third-party distribution obligations (notably PyMuPDF's AGPL-3.0
  notice/source terms and Qt's LGPL-3.0 notice obligations) are tracked
  in `THIRD_PARTY_NOTICES.md` and not yet complete. Do not publish
  binaries publicly yet.
