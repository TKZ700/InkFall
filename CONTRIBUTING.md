# Contributing to Inkfall

Thanks for your interest in improving Inkfall! Bug reports, feature
ideas and pull requests are all welcome. Inkfall is developed in the
open, and community contributions are part of how it improves.

## Getting started

```sh
uv sync
uv run python main.py
uv run pytest tests/test_viewer.py -q
```

- Inkfall targets Python 3.11+ with PySide6/Qt. Only `app/` may import
  PySide6 — `tests/test_import_boundaries.py` enforces this.
- Tests run offscreen under pytest-qt; a focused `uv run pytest` batch is
  the usual check for a change.
- If you touch build configuration or packaging, read
  [BUILDING.md](BUILDING.md) first, and do not claim a binary build
  works unless it was actually built — CI artifacts are the source of
  truth.

## Submitting changes

1. For anything beyond a small fix, open an issue first so the approach
   can be discussed before you invest in a large patch.
2. Keep pull requests focused, and explain what the change does and why
   it is needed.
3. Include tests for behaviour changes, and say plainly what automated
   tests cannot cover (hardware-dependent stylus or pressure behaviour,
   for example).
4. Do not mix unrelated refactoring into a functional change.

## Licensing of contributions

- Inkfall is licensed under the **GNU Affero General Public License
  v3.0 only** (`AGPL-3.0-only`). Contributions are submitted for
  inclusion under that license: by opening a pull request, you agree
  that your contribution is made available under `AGPL-3.0-only` as
  part of Inkfall.
- **You keep the copyright to what you write.** There is no contributor
  license agreement (CLA), no copyright assignment, no relicensing
  agreement, and no signing requirement beyond the PR itself.
- Only submit code and assets you have the right to contribute:
  - Do not paste code from projects whose licenses are incompatible
    with `AGPL-3.0-only`.
  - Flag any third-party code or assets you include, and name their
    license and upstream source, so they can be recorded in
    [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
  - Do not submit code you are not authorized to contribute (for
    example, code owned by an employer without permission).
