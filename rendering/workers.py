"""Worker-local PDF handles (M2). May import PyMuPDF (approved module).

Ownership rule: the application ``DocumentManager``/``PdfBackend`` handle
is NEVER shared with renderer workers. Each worker thread lazily opens its
own handle from the normalized source path on first use. Handles are
thread-confined: a handle created on thread T is only ever used on T.

Lifecycle: handles are registered globally (under a lock) purely so that
engine shutdown can close them deterministically after the pool has been
joined. No document is opened in every worker up front.

Note: the engine may neutralize per-page intrinsic rotation in memory
(``set_rotation(0)``) so raster math stays in unrotated media space. This
mutates ONLY the worker-local handle, is never saved, and never affects
the application backend or the file on disk.
"""

from __future__ import annotations

import threading

import pymupdf

from rendering.errors import RenderFailedError

__all__ = ["WorkerHandles"]


class WorkerHandles:
    """Thread-confined registry of worker-local ``pymupdf`` documents."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: set[pymupdf.Document] = set()
        self._closed = False

    def acquire(self, path: str) -> pymupdf.Document:
        """Return this thread's handle for ``path``, opening it if needed."""
        if self._closed:
            raise RenderFailedError("worker handles are shut down")
        docs: dict[str, pymupdf.Document] | None = getattr(self._local, "docs", None)
        if docs is None:
            docs = {}
            self._local.docs = docs
        doc = docs.get(path)
        if doc is not None:
            return doc
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            raise RenderFailedError(f"Worker cannot open PDF: {path} ({e})") from e
        # Encrypted documents need a password flow that M2 does not provide.
        try:
            if doc.needs_pass:
                doc.close()
                raise RenderFailedError(f"Encrypted PDF needs password (M2 unsupported): {path}")
        except RenderFailedError:
            raise
        except Exception as e:
            try:
                doc.close()
            except Exception:
                pass
            raise RenderFailedError(f"Cannot inspect PDF: {path} ({e})") from e
        docs[path] = doc
        with self._lock:
            self._all.add(doc)
        return doc

    def prepare_page(self, path: str, index: int) -> tuple[pymupdf.Document, pymupdf.Page, int]:
        """Fetch a page neutralized to unrotated media geometry.

        Returns ``(doc, page, intrinsic_rotation)``. The intrinsic ``/Rotate``
        is read ONCE per (thread, path, page) and cached thread-locally:
        neutralization (``set_rotation(0)``) is sticky on the page object, so
        re-reading ``page.rotation`` would wrongly report 0 on repeat
        renders. Callers must use the returned intrinsic, never re-read it.
        """
        doc = self.acquire(path)
        rots: dict[tuple[str, int], int] | None = getattr(self._local, "rots", None)
        if rots is None:
            rots = {}
            self._local.rots = rots
        cached = rots.get((path, index))
        try:
            page = doc[index]
        except IndexError:
            raise
        except Exception as e:
            raise RenderFailedError(f"Cannot access page {index}: {path} ({e})") from e
        if cached is not None:
            return doc, page, cached
        try:
            intrinsic = int(page.rotation)
        except Exception as e:
            raise RenderFailedError(f"Cannot read rotation: {path} p{index} ({e})") from e
        if intrinsic:
            try:
                page.set_rotation(0)
            except Exception as e:
                raise RenderFailedError(
                    f"Cannot neutralize rotation: {path} p{index} ({e})"
                ) from e
        rots[(path, index)] = intrinsic
        return doc, page, intrinsic

    def live_count(self) -> int:
        """Number of registered worker handles (for tests/metrics)."""
        with self._lock:
            return len(self._all)

    def close_all(self) -> None:
        """Close every worker handle. Idempotent; call after pool join."""
        with self._lock:
            docs = list(self._all)
            self._all.clear()
            self._closed = True
        for doc in docs:
            try:
                doc.close()
            except Exception:
                pass
