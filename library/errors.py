"""Library errors (M4). Qt-free."""

from __future__ import annotations


class LibraryError(Exception):
    """Base class for library failures (never crashes the app)."""


class LibraryNotFoundError(LibraryError):
    """Raised when an item or folder id is unknown."""


class LibraryInvalidParentError(LibraryError):
    """Raised when an organization move would create an invalid parent.

    Currently: moving a folder into itself or into one of its
    descendants (a cycle in the single-parent hierarchy).
    """
