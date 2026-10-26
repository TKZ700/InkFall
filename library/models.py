"""Library data models (M4). Qt-free, PyMuPDF-free.

A library entry describes a PDF *file* without opening it. Filesystem
facts (size, mtimes) are captured at import/refresh from ``os.stat``;
user state (favorite, last-opened, folder) lives in the store.
``missing`` is always derived transiently (never persisted).
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field

__all__ = ["LibraryFolder", "LibraryItem", "new_id", "strip_pdf_suffix"]


def new_id() -> str:
    """Return a stable random hex id."""
    return uuid.uuid4().hex


def strip_pdf_suffix(name: str) -> str:
    """Display-title derivation: remove only the FINAL ``.pdf`` suffix.

    Case-insensitive and applied once: ``"Linear Algebra.pdf"`` and
    ``"Calculus.PDF"`` become ``"Linear Algebra"`` / ``"Calculus"``,
    ``"Report.pdf.pdf"`` becomes ``"Report.pdf"``, and a ``.pdf`` in
    the middle of a name is never touched. The filesystem file is
    unaffected — this is a display-level derivation only. The single
    shared entry point for every library surface.
    """
    text = (name or "").strip()
    if len(text) > 4 and text.casefold().endswith(".pdf"):
        return text[:-4].rstrip() or text
    return text


@dataclass(slots=True)
class LibraryFolder:
    """A named container for library items (may nest via parent_id)."""

    id: str = field(default_factory=new_id)
    name: str = "Untitled folder"
    parent_id: str | None = None
    created_ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip() or "Untitled folder"


@dataclass(slots=True)
class LibraryItem:
    """Metadata for one PDF file in the library (no document handle)."""

    id: str = field(default_factory=new_id)
    normalized_path: str = ""
    display_name: str = "Untitled"
    parent_id: str | None = None
    created_ts: float = field(default_factory=time.time)
    modified_ts: float = field(default_factory=time.time)
    size_bytes: int = 0
    favorite: bool = False
    last_opened_ts: float | None = None
    page_count: int | None = None
    added_ts: float = field(default_factory=time.time)
    # Transient only (never persisted): True when the file is absent.
    missing: bool = False

    def __post_init__(self) -> None:
        self.display_name = (self.display_name or "").strip() or "Untitled"
        if self.size_bytes < 0:
            self.size_bytes = 0

    @property
    def file_name(self) -> str:
        """Base file name derived from the stored path."""
        path = self.normalized_path.replace("\\", "/")
        return path.rsplit("/", 1)[-1] if path else self.display_name

    @property
    def display_title(self) -> str:
        """Library display title (display_name without the final .pdf).

        Derived at read time via :func:`strip_pdf_suffix` so legacy rows
        stored before the naming rule render cleanly too; the stored
        ``display_name`` (and the file on disk) are never rewritten.
        """
        return strip_pdf_suffix(self.display_name)

    def refresh_missing(self) -> bool:
        """Update the transient ``missing`` flag. Returns the new value."""
        try:
            self.missing = not os.path.isfile(self.normalized_path)
        except Exception:
            self.missing = True
        return self.missing
