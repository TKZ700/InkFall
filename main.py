"""Inkfall entry point."""

from __future__ import annotations

import os
import sys


def _enable_fatal_diagnostics() -> None:
    """Dump native/Python fatal stacks to a file instead of dying silently.

    The windowed build has no console and no crash reporter, so a native
    abort (e.g. inside the QML scene graph) previously vanished without a
    trace. faulthandler writes the faulting thread's Python stack to a
    per-process file in the system temp directory; the file is also
    removed on clean exits so only real crashes leave diagnostics behind.
    Set INKFALL_NO_FATAL_LOG=1 to disable.
    """
    try:
        if os.environ.get("INKFALL_NO_FATAL_LOG"):
            return
        import atexit
        import faulthandler
        import tempfile

        path = os.path.join(
            tempfile.gettempdir(), f"inkfall-fatal-{os.getpid()}.log")
        handle = open(path, "w", encoding="utf-8")  # noqa: SIM115 - lives for process
        faulthandler.enable(file=handle, all_threads=True)

        def _cleanup() -> None:
            try:
                handle.close()
                os.remove(path)
            except OSError:
                pass

        atexit.register(_cleanup)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    """Launch Inkfall (or run the packaged-build self test).

    ``--self-test`` is a build-verification path used by the installer
    and release-archive smoke tests; it checks resource resolution,
    fonts, icons and QML loading without opening a window. Normal
    launches ignore it and start the application.
    """
    # Frozen-build multiprocessing bootstrap: standard for standalone
    # executables, a no-op in development. Must precede everything else.
    import multiprocessing

    multiprocessing.freeze_support()
    # Owner-side backstop: every helper process this app spawns (render
    # decoders, save-export child) dies with it, even on a hard crash.
    from core.proc_lifetime import assign_parent_job

    assign_parent_job()
    _enable_fatal_diagnostics()
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        from app.selftest import run_self_test

        return run_self_test(args)

    from app.application import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
