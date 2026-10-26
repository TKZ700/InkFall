"""Resource resolution for source checkouts and frozen distributions.

Inkfall ships two resource trees that Python's import system cannot
discover on its own: ``qml/`` (QML components loaded at runtime by URL)
and ``assets/`` (fonts, icons, branding PNGs). In a source checkout they
live at the repository root; in a frozen Nuitka standalone build they
are copied beside the executable.

This module is the single place that knows the difference, so callers
never guess at ``__file__`` shapes and both layouts keep working:

* source checkout -> ``<repo>/qml``, ``<repo>/assets``
* frozen build    -> ``<install dir>/qml``, ``<install dir>/assets``

Resolution is by existence, not by ``sys.frozen``: a development run
whose ``sys.executable`` directory happens to contain an ``assets/``
folder must still fall through to the checkout. The module is Qt-free
on purpose (it is imported by font/icon/QML helpers before Qt is ready).
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "candidate_roots",
    "is_frozen",
    "resource_path",
    "resource_root",
]


def _append(roots: list[Path], path: Path | None) -> None:
    """Append a resolved, de-duplicated candidate root (never raises)."""
    if path is None:
        return
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if resolved not in roots:
        roots.append(resolved)


def candidate_roots() -> tuple[Path, ...]:
    """Return the resource roots to probe, in priority order.

    Frozen layouts come first so an installed application never reads
    resources from an unrelated working directory. The source checkout
    is last and is what development runs resolve to.
    """
    roots: list[Path] = []
    try:
        _append(roots, Path(sys.executable).parent)
    except (AttributeError, TypeError):
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        _append(roots, Path(str(meipass)))
    try:
        # <repo>/app/resources.py -> <repo>
        _append(roots, Path(__file__).resolve().parents[1])
    except (IndexError, OSError, RuntimeError):
        pass
    return tuple(roots)


def resource_root() -> Path:
    """First candidate root that actually contains ``qml/`` or ``assets/``.

    Falls back to the last candidate (the source checkout) so callers
    always receive a usable path even in an incomplete deployment; the
    caller's ``is_file``/``is_dir`` checks then fail loudly.
    """
    roots = candidate_roots()
    for root in roots:
        try:
            if (root / "qml").is_dir() or (root / "assets").is_dir():
                return root
        except OSError:
            continue
    if roots:
        return roots[-1]
    return Path.cwd()


def resource_path(*parts: str) -> Path:
    """Resolve ``parts`` against the active resource root."""
    return resource_root().joinpath(*parts)


def is_frozen() -> bool:
    """True when running from a compiled/frozen build (Nuitka or PyInstaller)."""
    if getattr(sys, "frozen", False):
        return True
    # Nuitka injects ``__compiled__`` into every compiled module namespace.
    return "__compiled__" in globals()
