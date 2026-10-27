# Third-party notices and distribution audit

Inkfall bundles and redistributes third-party software in its packaged
application. This file records what those components are, which license
applies to the exact versions Inkfall uses, and what distribution
obligations follow from them.

**Status of this file** — prepared on 2026-10-27 against the source
checkout at commit `0dd3431` (Python 3.11.15, Windows host). Component
versions and license fields were read from `uv.lock`, the installed
wheels in the project virtualenv (`.venv/Lib/site-packages`), and a
locally produced standalone tree (`build/main.dist`). Statements below
are an engineering audit, **not legal advice**, and Inkfall makes **no
claim that its packaged distribution is already fully compliant** — see
[Open obligations](#open-obligations-before-a-public-binary-release).

## How Inkfall itself is licensed

Inkfall is released under the **GNU Affero General Public License
v3.0 only** (`AGPL-3.0-only`); the complete text is in
[LICENSE](LICENSE). Choosing AGPL-3.0 is what makes distributing
Inkfall together with PyMuPDF possible under PyMuPDF's open-source
option (see below). It does not by itself discharge any third-party
obligation listed here.

## Bundled components

| Component | Version | License | Verified from |
| --- | --- | --- | --- |
| PyMuPDF (with the MuPDF engine, `mupdfcpp64.dll`) | 1.28.2 | AGPL-3.0, or an Artifex commercial license | wheel METADATA; upstream `COPYING` (full AGPL-3.0 text); [official about page](https://pymupdf.readthedocs.io/en/latest/about.html) |
| PySide6, PySide6-Essentials, PySide6-Addons, Shiboken6 (Qt 6.11.2) | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only (open-source Qt modules: LGPL-3.0) | wheel METADATA of all four packages |
| NumPy | 2.4.6 (lock resolves 2.5.3 for Python ≥ 3.12) | BSD-3-Clause, with bundled parts under 0BSD, MIT, Zlib, CC0-1.0 | wheel `License-Expression` |
| CPython runtime | 3.11.15 | Python Software Foundation License Version 2 | `python311.dll` FileVersion; python.org licensing |
| OpenSSL | 3.5.7 (`libssl-3-x64.dll`, `libcrypto-3-x64.dll`) | Apache-2.0 or the OpenSSL License (dual) | DLL FileVersion; openssl.org licensing |
| MSVC runtime | 14.51.36247.0 (`msvcp140*.dll`, `vcruntime140*.dll`) | Microsoft Visual C++ Redistributable terms | DLL FileVersion |
| Inter font (`Inter-Regular/Medium/SemiBold.ttf`) | upstream snapshot | SIL Open Font License 1.1 — Copyright (c) 2016 The Inter Project Authors | upstream `LICENSE.txt` (rsms/inter), bundled as `assets/fonts/Inter-OFL.txt` |
| Vazirmatn font (`Vazirmatn-Regular/Medium/SemiBold.ttf`) | v33.003 | SIL Open Font License 1.1 | `assets/fonts/Vazirmatn-OFL.txt` (bundled) |
| Qt-bundled third-party libraries (`opengl32sw.dll` — Mesa-based software OpenGL; zlib, libpng, freetype, harfbuzz and others inside the Qt DLLs) | varies | various, per Qt's third-party licensing documentation | presence verified in the standalone tree |
| Qt WebEngine (`qt6webengine*.dll`, `QtWebEngineCore.pyd` + Chromium) | 6.11.2 | LGPL-3.0 with Chromium's license set; **unused by Inkfall's code** | present in the standalone tree and installed PySide6 |

Development-only dependencies (pytest, pytest-qt, Nuitka 4.3.1,
hatchling and friends) are not distributed inside Inkfall artifacts and
carry no redistribution obligation for the packaged application.

## Component-specific obligations

### PyMuPDF / MuPDF (Artifex Software, Inc.)

- PyMuPDF is dual-licensed: **AGPL-3.0** (open source) or a commercial
  license from Artifex. Inkfall uses the **AGPL-3.0 option**; this is
  permitted because Inkfall itself is `AGPL-3.0-only`.
- Distributing Inkfall binaries therefore triggers the AGPL's conveyance
  obligations (sections 4 and 6): keep copyright and license notices
  intact, include the license text, and provide the corresponding source
  for the covered work. Separately, section 13 adds a further source
  obligation **when a covered modified version supports remote
  interaction with users over a computer network** (for example, a
  modified Inkfall offered as a network service): whoever operates it
  must make its corresponding source available to those network users.
  A desktop distribution that offers no network interaction does not
  invoke section 13 — but it still owes the section 6 corresponding-
  source obligations above.
- The AGPL text itself is in Inkfall's `LICENSE` and upstream ships the
  same text as `COPYING`. MuPDF's upstream source is published by
  Artifex/PyMuPDF; the corresponding-source story for a binary release
  should point at both Inkfall's own repository and the PyMuPDF/MuPDF
  sources used for the build.
- If Inkfall ever wants to move away from AGPL-3.0 (or distribute a
  non-AGPL build), a commercial PyMuPDF license from Artifex would be
  required instead.

### PySide6 / Qt (Qt Company / Qt Project)

- The PySide6 and Shiboken6 wheels declare
  `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; the bundled Qt 6.11.2
  open-source libraries are LGPL-3.0.
- LGPL-3.0 obligations for a combined desktop application: include the
  LGPL-3.0 text and Qt copyright notices with the distribution, state
  that the Qt libraries are covered by the LGPL-3.0, and let users
  replace the Qt libraries. Inkfall's Nuitka standalone tree ships the
  Qt DLLs as separate, unmodified dynamic libraries, which keeps
  replacement technically possible; the distribution should document
  how to swap them.
- **Verified gap:** the PySide6 6.11.2 wheel carries only a
  `LicenseRef-Qt-Commercial.txt` reference — no LGPL/GPL license texts
  and no Qt third-party notices — and the current standalone tree
  contains no license files at all. License texts must be added to the
  distributed tree before release (see open obligations).
- Some Qt modules historically ship under GPL rather than LGPL; the
  exact set of Qt modules/plugins included in the bundle must be
  checked per module before release.

### Qt WebEngine (bundled but unused)

- The installed PySide6 and the local standalone tree contain
  QtWebEngine libraries (`qt6webenginecore.dll`,
  `qt6webenginequick*.dll`, `qt6webenginewidgets.dll`) even though
  Inkfall's code never imports QtWebEngine. Nuitka bundles them as part
  of the PySide6 package.
- WebEngine embeds Chromium and carries its own licensing
  documentation requirements. Either the packaging must exclude
  WebEngine, or the distribution must carry WebEngine's required
  license material. This decision needs a packaging change and has not
  been made yet.

### Fonts (SIL OFL 1.1)

- OFL-licensed fonts may be bundled and redistributed provided the
  copyright notice and the OFL text accompany them.
- Vazirmatn: satisfied — `assets/fonts/Vazirmatn-OFL.txt` ships beside
  the font files, and the packaged self-test fails when it is missing.
- Inter: satisfied as of this change — `assets/fonts/Inter-OFL.txt` now
  ships beside the font files (previously missing).

### Runtime pieces bundled by the packaging chain

- CPython 3.11.15 (`python311.dll` and the stdlib extension modules):
  PSF License Version 2; CPython's own `LICENSE` also covers the
  libraries it bundles (OpenSSL, libffi, bzip2, zlib, and others). That
  license text should ship with the distribution.
- OpenSSL 3.5.7 (from the Python build): dual Apache-2.0 / OpenSSL
  License; covered transitively by CPython's LICENSE, but listing it
  explicitly avoids ambiguity.
- MSVC runtime 14.51.36247.0: redistribution is permitted under
  Microsoft's Visual C++ Redistributable terms, which impose their own
  notice requirements. Not yet reviewed in detail.
- The Windows installer's uninstaller is generated by NSIS (3.x, fetched
  by Nuitka). NSIS-generated installers may be redistributed; NSIS's own
  components have license terms that should be reviewed once before
  release.

## Open obligations before a public binary release

None of these have been silently assumed done; each needs a deliberate
change or decision:

1. **Bundle license texts in the distributed trees.** Add at minimum:
   the AGPL-3.0 text (also satisfied by Inkfall's own `LICENSE`),
   LGPL-3.0/GPL texts and Qt copyright notices, NumPy's `LICENSE.txt`,
   CPython's `LICENSE` (PSF-2.0 + bundled libraries), and the MSVC
   redistributable notice. This is a `scripts/build_config.py` /
   packaging change and was deliberately **not** made here.
2. **Decide on Qt WebEngine:** exclude it from bundles (preferred —
   Inkfall does not use it) or include WebEngine/Chromium license
   documentation.
3. **Collect Qt's third-party notices** (opengl32sw/Mesa, zlib, libpng,
   freetype, harfbuzz, etc.) from the Qt 6.11.2 distribution and ship
   them.
4. **Verify per-module Qt licenses** for every module/plugin actually
   bundled (a few Qt modules are GPL, not LGPL).
5. **Review the Microsoft VC++ redistributable terms** and NSIS terms
   for the installer path.
6. **Document the corresponding-source path** for AGPL-3.0 compliance
   in the distribution (link to the public source repository plus the
   PyMuPDF/MuPDF sources used).
7. **Final legal review.** This file is an engineering audit and is not
   legal advice.
