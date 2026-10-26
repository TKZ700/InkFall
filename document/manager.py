"""Document lifecycle manager (M1, single-threaded).

Duplicate-open policy (frozen): opening the same normalized absolute path
returns the EXISTING managed :class:`Document`. This keeps handle
ownership deterministic without inventing a multi-view system. The single
sanctioned exception is :meth:`DocumentManager.open_readonly` — an
explicit user-requested second VIEW of an already-open file (read-only,
registered by ID only, never the path owner).

Path identity: ``Path(path).expanduser().resolve()`` + ``normcase`` so
``relative path``, ``C:\\foo\\bar.pdf`` and ``C:\\FOO\\BAR.pdf`` map to one
key on Windows. A normalized absolute path string is sufficient for M1.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from core.types import DocumentId
from document.backend import PdfBackend, create_blank_backend
from document.document import Document
from document.errors import DocumentClosedError
from document.models import DocumentInfo
from document.paper import PaperSpec

__all__ = ["DocumentManager", "normalize_path"]

#: Registry-key scheme for an unsaved blank (never a filesystem path, so
#: it can never collide with a real normalized path).
BLANK_KEY_PREFIX = "inkfall-blank:"


def normalize_path(path: str) -> str:
    """Return the registry key for a path (absolute + normcase)."""
    resolved = Path(path).expanduser().resolve()
    return os.path.normcase(str(resolved))


class DocumentManager:
    """Owns the registry of open documents and their lifecycle."""

    def __init__(self) -> None:
        self._by_id: dict[str, Document] = {}
        self._by_path: dict[str, str] = {}  # normalized path -> DocumentId.value

    def open(self, path: str, password: str | None = None) -> Document:
        """Open a PDF, or return the existing document for the same file.

        Failures (missing/invalid/encrypted/wrong password) raise without
        creating a partially-live managed object.
        """
        key = normalize_path(path)
        existing_id = self._by_path.get(key)
        if existing_id is not None:
            existing = self._by_id.get(existing_id)
            if existing is not None and not existing.is_closed:
                return existing
            # Stale registry entry (closed without removal): drop it.
            self._by_path.pop(key, None)
            if existing_id in self._by_id:
                try:
                    self._by_id[existing_id].close()
                except Exception:
                    pass
                self._by_id.pop(existing_id, None)

        backend = PdfBackend.open(path, password=password)
        doc = Document(backend, normalized_path=key)
        try:
            doc.restore_persisted_annotations()
        except Exception:
            # Restoration is deliberately fail-safe: an unsafe manifest
            # blocks saving but does not make the PDF unopenable.
            pass
        self._by_id[doc.id.value] = doc
        self._by_path[key] = doc.id.value
        return doc

    def open_readonly(self, path: str, password: str | None = None) -> Document:
        """Open an explicit second, non-persisting view of one file.

        This is the ONLY sanctioned way to have two live documents for
        one normalized path: the duplicate is a separate Document (its
        own backend handle, annotation store, history, and reading
        position) constructed with ``read_only=True``. It is registered
        by ID only — ``_by_path`` keeps pointing at the PRIMARY
        (editable) session, so the frozen duplicate-open policy for
        ordinary opens is untouched. Every mutating document operation
        raises :class:`ReadOnlyDocumentError` from the document itself,
        and :meth:`save_document` refuses it twice over (``can_save``
        is False and the path key belongs to another document).
        """
        key = normalize_path(path)
        backend = PdfBackend.open(path, password=password)
        doc = Document(backend, normalized_path=key, read_only=True)
        try:
            doc.restore_persisted_annotations()
        except Exception:
            pass
        self._by_id[doc.id.value] = doc
        return doc

    def get(self, document_id: DocumentId) -> Document | None:
        """Return the open document for an ID, or None if unknown/closed."""
        doc = self._by_id.get(document_id.value)
        if doc is None or doc.is_closed:
            return None
        return doc

    # -- M8 unsaved blank workspaces ------------------------------------

    def create_blank(self, spec: PaperSpec) -> Document:
        """Create a managed in-memory blank document for a paper spec.

        The document is a first-class managed session from the start:
        it gets a synthetic registry key (never a filesystem path), a
        private render working copy (the render engine requires a real
        file), an empty annotation store, and the ``unsaved`` flag. It
        is fully editable before any destination exists.
        """
        try:
            spec = spec.normalized()
        except Exception:
            spec = PaperSpec()
        key = BLANK_KEY_PREFIX + uuid.uuid4().hex
        backend = create_blank_backend(spec, key)
        doc = Document(backend, normalized_path=key, unsaved=True,
                       paper_spec=spec)
        try:
            doc.ensure_render_file()
        except Exception:
            try:
                doc.close()
            except Exception:
                pass
            raise
        self._by_id[doc.id.value] = doc
        self._by_path[key] = doc.id.value
        return doc

    def save_document(self, doc: Document, target_path: str) -> str:
        """Export a session to ``target_path`` and adopt the file identity.

        The export is a full, verified snapshot (template + native
        annotations + versioned manifest) built from the live in-memory
        document. An opened PDF saves back to its source path; a blank
        adopts the chosen destination. The session stays open and keeps
        editing with its existing backend, store, history, and private
        render working copy. On success the registry key moves to the
        normalized target path and the document records ``saved_path``.
        A failure raises before any identity change, so the workspace
        stays dirty and intact.
        """
        if doc is None or doc.is_closed:
            raise DocumentClosedError("Cannot save a closed document")
        if not target_path or not isinstance(target_path, str):
            raise ValueError(f"invalid save path: {target_path!r}")
        key = normalize_path(target_path)
        old_key = doc.normalized_path
        existing_id = self._by_path.get(key)
        if existing_id is not None and existing_id != doc.id.value:
            raise ValueError(
                "That file is already open in another document; "
                "choose a different name.")
        if not doc.can_save:
            raise ValueError(
                doc.persistence_error
                or "This document cannot be saved safely.")
        # The viewer normally performs this before manager.save_document so
        # it can rebind the active render pipeline. Doing it here as well
        # covers non-active documents during Save All; it is idempotent.
        doc.prepare_for_save(target_path)
        doc.write_export(target_path)
        if self._by_path.get(old_key) == doc.id.value:
            self._by_path.pop(old_key, None)
        self._by_path[key] = doc.id.value
        doc.commit_saved(key, target_path)
        return key

    def close(self, document_id: DocumentId) -> None:
        """Close one document and remove it from the registry (idempotent).

        The path mapping is dropped only when it actually points at THIS
        document: a read-only duplicate shares the primary session's
        normalized path, and closing it must never unregister the
        primary (editable) document.
        """
        doc = self._by_id.pop(document_id.value, None)
        if doc is None:
            return
        if self._by_path.get(doc.normalized_path) == document_id.value:
            self._by_path.pop(doc.normalized_path, None)
        doc.close()

    def close_all(self) -> None:
        """Close every managed document. Safe to call repeatedly."""
        docs = list(self._by_id.values())
        self._by_id.clear()
        self._by_path.clear()
        for doc in docs:
            try:
                doc.close()
            except Exception:
                pass

    def list_open(self) -> tuple[DocumentInfo, ...]:
        """Snapshot descriptors of currently open documents."""
        infos: list[DocumentInfo] = []
        for doc in list(self._by_id.values()):
            if doc.is_closed:
                continue
            try:
                infos.append(doc.info())
            except DocumentClosedError:
                continue
        return tuple(infos)

    def documents(self) -> tuple[Document, ...]:
        """Live managed documents (stable snapshot, closed ones excluded)."""
        result = []
        for doc in list(self._by_id.values()):
            try:
                if not doc.is_closed:
                    result.append(doc)
            except Exception:
                continue
        return tuple(result)

    def __len__(self) -> int:
        """Number of currently open (non-closed) documents."""
        return len(self.list_open())

    def __enter__(self) -> DocumentManager:
        return self

    def __exit__(self, *args: object) -> None:
        self.close_all()

    def shutdown(self) -> None:
        """Alias for :meth:`close_all` (manager shutdown path)."""
        self.close_all()
