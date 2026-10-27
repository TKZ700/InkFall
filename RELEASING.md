# Releasing Inkfall

How a versioned release is produced, and the gate that must be cleared
before one is allowed to happen.

## How releases work

1. `pyproject.toml` holds the project version; it is the single source of
   truth (`scripts/build_config.py` reads it and derives every artifact
   name from it).
2. Pushing a tag `v<version>` (for example `v0.1.0`) triggers
   `.github/workflows/build.yml`:
   - the Windows installer job and the Linux archive job run exactly as
     for any other push: focused build tests → Nuitka build → bundle
     verification → install/launch smoke test;
   - the `publish-release` job runs only after **both** builds succeed. It
     re-validates the tag against the project version, downloads both
     uploaded workflow artifacts, verifies the two expected distribution
     files are present and non-empty, then creates a GitHub Release for
     the existing tag and uploads both files as release assets with
     GitHub-generated release notes.
3. Release assets (exact names, from `scripts/build_config.py`), for
   example for `v0.1.0`:

   ```
   Inkfall-0.1.0-windows-x86_64-setup.exe
   inkfall-0.1.0-linux-x86_64.tar.gz
   ```

4. While the project is an early preview (see the status callout in
   `README.md`), releases are published as **prereleases**. Remove the
   `--prerelease` flag in the workflow when a stable version ships.
5. Release notes are GitHub's generated notes ("Full Changelog" link).
   Refine them on the release page afterwards if wanted; the workflow
   never invents notes.

Ordinary runs — manual dispatch, pull requests, branch pushes — never
publish anything; they upload ephemeral 14-day workflow artifacts only.

## Version rule

The tag must be exactly `v` followed by the version in `pyproject.toml`.
All three jobs verify this (`scripts.build_config.validate_release_tag`)
and the run fails before anything is published if they disagree. A tag
such as `v0.2.0` while `pyproject.toml` says `0.1.0` cannot release, and a
tag like `v0.1.0-rc1` is rejected the same way — bump the project version
first if you want a differently spelled release.

## Release-readiness gate — read this before pushing any `v*` tag

Pushing a version tag publishes installable binaries publicly the moment
both builds finish. The repository's own audit says this must not happen
yet: [THIRD_PARTY_NOTICES.md → "Open obligations before a public binary
release"](THIRD_PARTY_NOTICES.md#open-obligations-before-a-public-binary-release)
lists obligations that were all still open as of 2026-10-27:

1. Bundle license texts in the distributed trees (AGPL-3.0, LGPL-3.0/GPL
   texts plus Qt copyright notices, NumPy's `LICENSE.txt`, CPython's
   `LICENSE`, the MSVC redistributable notice).
2. Decide on Qt WebEngine: exclude it from the bundles (preferred —
   Inkfall does not use it) or ship its required license documentation.
3. Collect and ship Qt's third-party notices (Mesa/opengl32sw, zlib,
   libpng, freetype, harfbuzz, …).
4. Verify per-module Qt licenses for everything actually bundled.
5. Review the Microsoft VC++ redistributable terms and the NSIS terms.
6. Document the AGPL corresponding-source path for the distribution.
7. Final legal review (the audit is an engineering document, not legal
   advice).

A workflow run must never be mistaken for having closed any of these.
Do not push a `v*` tag until every item above is closed and updated in
`THIRD_PARTY_NOTICES.md`.

## Scope of automation and recovery

- The workflow never deletes or overwrites an existing release or its
  assets, and never creates a git tag: `gh release create --verify-tag`
  aborts unless the tag already exists, and the release job first fails
  with a clear error if a release for the tag already exists.
- Re-running the workflow for the same tag rebuilds both artifacts and
  then refuses to publish, because the release already exists. That is
  intentional: silently replacing published binaries would make new bytes
  indistinguishable from the reviewed ones.
- Recovery from a broken or partial release (for example an interrupted
  asset upload) is a deliberate, manual step:
  1. Look at the release page and confirm what actually happened.
  2. Delete only the incomplete release:
     `gh release delete v0.1.0 --yes` (this keeps the `v0.1.0` git tag).
  3. Re-run the failed jobs from the Actions run for the tag
     (Actions → Build preview artifacts → the tag run → Re-run failed
     jobs); the rebuild then publishes normally.
  - Never edit or replace an asset of an already-published release in
    place; delete the release deliberately and republish via the steps
    above instead.
