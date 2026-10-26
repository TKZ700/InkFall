"""Inkfall entry point."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Launch Inkfall (or run the packaged-build self test).

    ``--self-test`` is a build-verification path used by the installer
    and release-archive smoke tests; it checks resource resolution,
    fonts, icons and QML loading without opening a window. Normal
    launches ignore it and start the application.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        from app.selftest import run_self_test

        return run_self_test(args)

    from app.application import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
