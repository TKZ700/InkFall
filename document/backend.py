"""PyMuPDF backend (M1).

This is the ONLY production module allowed to import PyMuPDF. It owns the
raw document handle and translates everything into Inkfall models/errors.
No ``pymupdf``/``fitz`` object ever leaves this module.

Lazy by design: ``open`` establishes the handle + basic metadata only.
No rasterization, no text extraction, no pixmaps, no full-page scan.
``page_info`` reads a single page's media-box + rotation on demand.
"""

from __future__ import annotations

import errno
import os
import tempfile
import time
from queue import Empty

import pymupdf

from annotations.persistence import EMBEDDED_NAME, MAX_MANIFEST_BYTES
from core.types import DocumentId, Size
from document.annotation_pdf import NATIVE_SUBJECT_PREFIX, NATIVE_TITLE
from document.errors import (
    DocumentClosedError,
    DocumentNotFoundError,
    EncryptedPdfError,
    InvalidPageIndexError,
    InvalidPdfError,
    WrongPasswordError,
)
from document.models import DocumentMetadata, PageInfo, page_id_for
from document.paper import (
    PaperSpec,
    page_size_pt,
    paper_metadata_value,
    pattern_primitives,
)

__all__ = ["PdfBackend", "render_cover_preview", "create_blank_pdf",
           "create_blank_backend", "enumerate_page_infos",
           "render_page_preview", "EncryptedRewriteError",
           "NativeAnnotationError", "SaveLockedError",
           "SaveVerificationError"]

#: MuPDF's "keep the source encryption" write option. Reading it through the
#: ``mupdf`` namespace works across PyMuPDF builds; MuPDF's value is 0
#: (``PDF_ENCRYPT_NONE`` is 1, PyMuPDF's default ``save()`` argument).
_PDF_ENCRYPT_KEEP = getattr(getattr(pymupdf, "mupdf", None),
                            "PDF_ENCRYPT_KEEP", 0)

_ENCRYPTED_REWRITE_MESSAGE = (
    "This PDF is encrypted. Inkfall cannot rewrite an encrypted file "
    "without changing its protection, so it was not saved. The original "
    "file is unchanged.")


class SaveLockedError(InvalidPdfError):
    """The destination exists but another handle prevents replacing it."""


class EncryptedRewriteError(InvalidPdfError):
    """An encrypted source cannot be rewritten with its protection intact."""


class NativeAnnotationError(InvalidPdfError):
    """A required native annotation or its appearance could not be produced."""


class SaveVerificationError(InvalidPdfError):
    """The temporary output failed post-write verification."""


def _clean(value: object) -> str | None:
    """Map backend metadata values to ``str | None`` ("" -> None)."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def enumerate_page_infos(
    render_path: str, document_id: DocumentId
) -> list[PageInfo]:
    """Enumerate page geometry via a PRIVATE short-lived fitz handle.

    Background tab-switch preparation must not touch the GUI-owned
    ``PdfBackend`` (its single ``pymupdf`` handle is thread-confined and
    documented single-threaded). This opens its own handle on the
    document's render working copy — the same path the render workers
    already read with worker-local handles — enumerates ``PageInfo`` for
    every page, and closes the handle before returning. No ``pymupdf``
    object leaves this module, per the backend contract.
    """
    if not render_path or not os.path.isfile(render_path):
        raise DocumentNotFoundError(f"File does not exist: {render_path!r}")
    try:
        doc = pymupdf.open(render_path)
    except Exception as e:
        raise InvalidPdfError(f"Cannot read PDF: {render_path} ({e})") from e
    try:
        pages: list[PageInfo] = []
        for index in range(len(doc)):
            try:
                page = doc[index]
                rect = page.rect
                rotation = int(page.rotation)
                width = float(rect.width)
                height = float(rect.height)
            except IndexError as e:
                raise InvalidPageIndexError(
                    f"Invalid page index {index!r}") from e
            except Exception as e:
                raise InvalidPdfError(
                    f"Cannot read page {index}: {render_path} ({e})") from e
            pages.append(PageInfo(
                id=page_id_for(document_id, index),
                document_id=document_id,
                index=index,
                size=Size(width, height),
                rotation=rotation,
            ))
        return pages
    finally:
        try:
            doc.close()
        except Exception:
            pass


class PdfBackend:
    """Thin, single-threaded owner of one ``pymupdf`` document handle."""

    def __init__(
        self, doc: pymupdf.Document, path: str, document_id: DocumentId,
        encrypted_at_open: bool = False, password: str | None = None,
    ) -> None:
        self._doc = doc
        self._path = path
        self._document_id = document_id
        self._closed = False
        self._encrypted_at_open = encrypted_at_open
        self._password = password
        # Tri-state cache for the encrypted-rewrite capability probe.
        self._rewrite_supported: bool | None = None
        # Snapshot file backing the live handle after a same-path save
        # (see export_pdf). Deleted when the handle it backs is closed.
        self._snapshot_pending: str | None = None
        # Snapshot scalar header state at open time so basic accessors do
        # not need to touch pages. len() reads the trailer (O(1)).
        try:
            self._page_count = len(doc)
        except Exception as e:
            raise InvalidPdfError(f"Cannot read page count: {path}") from e

    # -- construction ----------------------------------------------------

    @classmethod
    def open(cls, path: str, password: str | None = None) -> PdfBackend:
        """Open a PDF lazily. Never returns a partially-live object on failure."""
        if not os.path.isfile(path):
            raise DocumentNotFoundError(f"File does not exist: {path}")
        try:
            doc = pymupdf.open(path)
        except FileNotFoundError as e:
            raise DocumentNotFoundError(f"File does not exist: {path}") from e
        except Exception as e:
            if "password" in str(e).lower() or "encrypt" in str(e).lower():
                raise EncryptedPdfError(f"Encrypted PDF requires a password: {path}") from e
            raise InvalidPdfError(f"Cannot open PDF: {path} ({e})") from e

        try:
            # Snapshot file-level encryption BEFORE authenticating: PyMuPDF
            # may report is_encrypted=False once access is granted, but the
            # document factually is encrypted and must stay observable.
            encrypted_at_open = bool(doc.is_encrypted or doc.needs_pass)
            if doc.needs_pass:
                if password is None:
                    doc.close()
                    raise EncryptedPdfError(f"Encrypted PDF requires a password: {path}")
                try:
                    ok = doc.authenticate(password)
                except Exception as e:
                    doc.close()
                    raise WrongPasswordError(f"Cannot authenticate PDF: {path}") from e
                if not ok:
                    doc.close()
                    raise WrongPasswordError(f"Incorrect password for PDF: {path}")
            return cls(doc, path, DocumentId(), encrypted_at_open, password)
        except (EncryptedPdfError, WrongPasswordError, InvalidPdfError):
            raise
        except Exception as e:
            try:
                doc.close()
            except Exception:
                pass
            raise InvalidPdfError(f"Cannot open PDF: {path} ({e})") from e

    # -- state -----------------------------------------------------------

    @property
    def document_id(self) -> DocumentId:
        """Stable ID assigned at open time."""
        return self._document_id

    @property
    def path(self) -> str:
        """Source path as passed to :meth:`open`."""
        return self._path

    @property
    def is_closed(self) -> bool:
        """True after :meth:`close` has been called."""
        return self._closed

    @property
    def is_encrypted(self) -> bool:
        """True if the underlying file is encrypted (snapshot at open)."""
        self._ensure_open()
        try:
            live = bool(self._doc.is_encrypted)
        except Exception:
            live = False
        return bool(self._encrypted_at_open or live)

    @property
    def encrypted_at_open(self) -> bool:
        """True when the source file was encrypted when it was opened."""
        return bool(self._encrypted_at_open)

    @property
    def needs_password(self) -> bool:
        """True if the document still requires a password."""
        self._ensure_open()
        try:
            return bool(self._doc.needs_pass)
        except Exception as e:
            raise InvalidPdfError(f"Cannot read encryption state: {self._path}") from e

    @property
    def page_count(self) -> int:
        """Number of pages (trailer read, no page processing)."""
        self._ensure_open()
        return self._page_count

    @property
    def metadata(self) -> DocumentMetadata:
        """Basic PDF metadata. Missing values are None."""
        self._ensure_open()
        try:
            raw = self._doc.metadata or {}
        except Exception as e:
            raise InvalidPdfError(f"Cannot read metadata: {self._path}") from e
        try:
            encrypted = bool(self._encrypted_at_open or self._doc.is_encrypted)
        except Exception:
            encrypted = self._encrypted_at_open
        return DocumentMetadata(
            title=_clean(raw.get("title")),
            author=_clean(raw.get("author")),
            subject=_clean(raw.get("subject")),
            keywords=_clean(raw.get("keywords")),
            creator=_clean(raw.get("creator")),
            producer=_clean(raw.get("producer")),
            creation_date=_clean(raw.get("creationDate")),
            modification_date=_clean(raw.get("modDate")),
            is_encrypted=encrypted,
        )

    # -- pages (lazy) ----------------------------------------------------

    def page_info(self, index: int) -> PageInfo:
        """Return geometry + rotation for one page. No rendering/text."""
        self._ensure_open()
        if not isinstance(index, int) or index < 0 or index >= self._page_count:
            raise InvalidPageIndexError(
                f"Invalid page index {index!r} (page_count={self._page_count})"
            )
        try:
            page = self._doc[index]
            rect = page.rect
            rotation = int(page.rotation)
            width = float(rect.width)
            height = float(rect.height)
        except InvalidPageIndexError:
            raise
        except IndexError as e:
            raise InvalidPageIndexError(f"Invalid page index {index!r}") from e
        except Exception as e:
            raise InvalidPdfError(f"Cannot read page {index}: {self._path} ({e})") from e
        return PageInfo(
            id=page_id_for(self._document_id, index),
            document_id=self._document_id,
            index=index,
            size=Size(width, height),
            rotation=rotation,
        )

    def _check_index(self, index: int) -> int:
        """Validate a page index against the live page count."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise InvalidPageIndexError(
                f"Invalid page index {index!r} (page_count={self._page_count})")
        if not 0 <= index < self._page_count:
            raise InvalidPageIndexError(
                f"Invalid page index {index!r} (page_count={self._page_count})")
        return index

    # -- M6.8.2 Stage 5.1 page operations (in-memory, never saved here) ---

    def rotate_page(self, index: int, degrees: int) -> int:
        """Rotate one page's intrinsic ``/Rotate`` cumulatively (clockwise).

        ``degrees`` must be a quarter turn in ``{90, 180, 270}``; the
        stored value is normalized to ``[0, 360)``. Returns the new
        intrinsic rotation. This mutates only the in-memory page tree —
        the source file on disk is never written.
        """
        self._ensure_open()
        self._check_index(index)
        if isinstance(degrees, bool) or not isinstance(degrees, int):
            raise ValueError(f"degrees must be an int quarter turn: {degrees!r}")
        if degrees % 90 != 0:
            raise ValueError(f"degrees must be a multiple of 90: {degrees!r}")
        try:
            page = self._doc[index]
            page.set_rotation((int(page.rotation) + int(degrees)) % 360)
            return int(page.rotation)
        except InvalidPageIndexError:
            raise
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot rotate page {index}: {self._path} ({e})") from e

    def extract_page(self, index: int) -> bytes:
        """Serialise one page (content + ``/Rotate`` + annots) to a blob.

        Used to make a page deletion reversible: the blob is a standalone
        single-page PDF that :meth:`insert_page` can put back at the same
        index. PyMuPDF object never leaves this module — bytes only.
        """
        self._ensure_open()
        self._check_index(index)
        try:
            return _serialize_page(self._doc, index)
        except InvalidPageIndexError:
            raise
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot snapshot page {index}: {self._path} ({e})") from e

    def delete_page(self, index: int) -> None:
        """Remove one page from the in-memory document.

        The last remaining page may not be deleted (an empty document is
        invalid); raises ``ValueError`` rather than substituting a blank
        page. Never writes the source file.
        """
        self._ensure_open()
        self._check_index(index)
        if self._page_count <= 1:
            raise ValueError("cannot delete the only page of a document")
        try:
            self._doc.delete_page(index)
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot delete page {index}: {self._path} ({e})") from e
        self._page_count -= 1

    def insert_page(self, index: int, blob: bytes) -> None:
        """Insert a previously extracted page blob at ``index``.

        ``index`` may equal ``page_count`` (append). The inserted page
        keeps its content, intrinsic rotation, and metadata.
        """
        self._ensure_open()
        if isinstance(index, bool) or not isinstance(index, int):
            raise InvalidPageIndexError(f"Invalid page index {index!r}")
        if not 0 <= index <= self._page_count:
            raise InvalidPageIndexError(
                f"Invalid page index {index!r} (page_count={self._page_count})")
        if not blob:
            raise InvalidPdfError("page blob is empty")
        holder = pymupdf.open("pdf", bytes(blob))
        try:
            self._doc.insert_pdf(holder, from_page=0, to_page=0, start_at=index)
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot insert page at {index}: {self._path} ({e})") from e
        finally:
            try:
                holder.close()
            except Exception:
                pass
        self._page_count += 1

    def insert_blank_page(self, index: int, spec: PaperSpec | None,
                          width: float, height: float) -> None:
        """Insert one generated blank/template page at ``index``.

        ``width`` / ``height`` are the target page's *displayed* size in
        points, so a landscape or rotated target gets a visually matching
        page. The page is created upright with those dimensions and the
        template is drawn for them (never inferred from baked content).
        ``spec`` is the document's recoverable Inkfall paper definition,
        or ``None`` for a plain white fallback on an ordinary PDF.
        ``index`` may equal ``page_count`` (append). Never writes the
        source file.
        """
        self._ensure_open()
        if isinstance(index, bool) or not isinstance(index, int):
            raise InvalidPageIndexError(f"Invalid page index {index!r}")
        if not 0 <= index <= self._page_count:
            raise InvalidPageIndexError(
                f"Invalid page index {index!r} (page_count={self._page_count})")
        try:
            page_width = float(width)
            page_height = float(height)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"invalid blank page size: {width!r} x {height!r}") from e
        if page_width <= 0.0 or page_height <= 0.0:
            raise ValueError(
                f"blank page size must be positive: {page_width} x {page_height}")
        try:
            paper = (spec.normalized() if spec is not None
                     else PaperSpec(template="plain"))
        except Exception:
            paper = PaperSpec(template="plain")
        try:
            page = self._doc.new_page(
                pno=index, width=page_width, height=page_height)
            page.draw_rect(
                pymupdf.Rect(0.0, 0.0, page_width, page_height),
                color=None, fill=_hex_rgb(paper.paper_color),
                width=0, fill_opacity=1.0)
            _draw_template_items(
                page,
                pattern_primitives(
                    paper, width_pt=page_width, height_pt=page_height),
                paper.pattern_color)
        except Exception as e:
            # A partially-created page must not survive a failed draw.
            try:
                if len(self._doc) > self._page_count:
                    self._doc.delete_page(index)
            except Exception:
                pass
            raise InvalidPdfError(
                f"Cannot insert blank page at {index}: {self._path} ({e})") from e
        self._page_count += 1

    def save_copy(self, path: str) -> None:
        """Write the current in-memory document to a working file.

        Deliberately a *different* path from the source: Inkfall never
        overwrites the user's original PDF for an in-memory edit. The
        working copy is what the render pipeline reads.
        """
        self._ensure_open()
        if not path or not isinstance(path, str):
            raise ValueError(f"invalid working path: {path!r}")
        try:
            self._doc.save(path)
        except Exception as e:
            raise InvalidPdfError(f"Cannot write working copy: {e}") from e

    def read_annotation_manifest(self) -> bytes | None:
        """Read the constant Inkfall manifest attachment, if present.

        Unrelated embedded attachments are ignored. A malformed/unsupported
        manifest is diagnosed and handled by the document layer, which must
        refuse to overwrite it.
        """
        self._ensure_open()
        try:
            names = list(self._doc.embfile_names() or ())
            if EMBEDDED_NAME not in names:
                return None
            try:
                info = self._doc.embfile_info(EMBEDDED_NAME) or {}
                size = info.get("size")
                if isinstance(size, int) and size > MAX_MANIFEST_BYTES:
                    raise InvalidPdfError(
                        "Inkfall annotation manifest is too large")
            except InvalidPdfError:
                raise
            except Exception:
                pass
            data = self._doc.embfile_get(EMBEDDED_NAME)
            return None if data is None else bytes(data)
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot read Inkfall annotation manifest: {self._path} "
                f"({e})") from e

    def remove_native_annotations(self, annotation_ids) -> int:
        """Delete *only* native annotations carrying matching Inkfall IDs.

        The subject marker is the ownership record. Anything without a
        marker, or with an identity not in ``annotation_ids``, is left
        untouched, including arbitrary annotations created by other apps.
        """
        self._ensure_open()
        wanted = {str(value) for value in (annotation_ids or ())}
        if not wanted:
            return 0
        removed = 0
        failures = []
        for page_index in range(self._page_count):
            try:
                page = self._doc[page_index]
                annotations = list(page.annots() or ())
            except Exception as e:
                failures.append(f"page {page_index}: {e}")
                continue
            for annot in annotations:
                try:
                    subject = str((annot.info or {}).get("subject") or "")
                except Exception:
                    continue
                if not subject.startswith(NATIVE_SUBJECT_PREFIX):
                    continue
                identity = subject[len(NATIVE_SUBJECT_PREFIX):]
                if identity not in wanted:
                    continue
                try:
                    page.delete_annot(annot)
                    removed += 1
                except Exception as e:
                    failures.append(f"page {page_index}: {e}")
        if failures:
            raise InvalidPdfError(
                "Cannot remove Inkfall native annotations safely: "
                + "; ".join(failures[:3]))
        return removed

    def ensure_export_supported(self) -> None:
        """Refuse an encrypted source that cannot keep its protection.

        The probe writes the document to a throwaway file with
        ``PDF_ENCRYPT_KEEP`` — the same write used by the export — and
        therefore fails only when this PyMuPDF build cannot carry the
        source encryption into the snapshot. The probe is disk-backed,
        never a full in-memory serialization, so probing an encrypted
        document does not duplicate it in RAM. Saving is refused
        explicitly instead of silently producing a differently protected
        PDF.
        """
        self._ensure_open()
        if not self._encrypted_at_open:
            return
        if self._rewrite_supported is None:
            fd, probe_path = tempfile.mkstemp(
                prefix="inkfall-probe-", suffix=".pdf")
            os.close(fd)
            try:
                os.remove(probe_path)
            except OSError:
                pass
            try:
                self._doc.save(probe_path, encryption=_PDF_ENCRYPT_KEEP)
                self._rewrite_supported = True
            except Exception:
                self._rewrite_supported = False
            finally:
                try:
                    if os.path.isfile(probe_path):
                        os.remove(probe_path)
                except Exception:
                    pass
        if not self._rewrite_supported:
            raise EncryptedRewriteError(_ENCRYPTED_REWRITE_MESSAGE)

    def _open_snapshot(self, snapshot_path: str):
        """Open an export snapshot file; authenticate if it kept encryption.

        Returns ``(document, was_encrypted)``. The file must outlive the
        returned handle (PyMuPDF keeps it open). The encryption state is
        sampled before authenticating because PyMuPDF stops reporting it
        once access is granted.
        """
        return _open_export_snapshot(snapshot_path, self._password)

    def export_pdf(self, target_path: str, native_pages: list,
                   manifest_bytes: bytes, expected_page_count: int,
                   expected_paper_keywords: str | None = None,
                   *, progress=None) -> None:
        """Atomically write a verified PDF with native annotations + manifest.

        The export is built from a fresh snapshot of the live document,
        written to a private on-disk temp file so peak memory stays bounded
        on large PDFs (the snapshot is never held as a bytes object).
        Native annotations are added once to the snapshot with a
        regenerated appearance stream, the constant manifest attachment is
        added or updated (never duplicated), and the result is written to a
        unique temporary file in the target directory. The temporary file
        is reopened and checked for the expected page count, manifest
        bytes, retained PaperSpec keyword, and the exact expected native
        annotations with renderable appearance streams before
        ``os.replace`` makes it the target.

        Same-path saves release the live PyMuPDF handle before replacement
        (Windows/worker locks), then install a prepared replacement handle
        built from the same pre-save snapshot — file-backed like the
        snapshot itself — so the live session remains usable without
        reloading the file; the snapshot file is deleted once no handle
        needs it (at the latest when the backend closes). Encrypted sources
        are snapshotted and written with ``PDF_ENCRYPT_KEEP`` so the output
        keeps the same protection; if that cannot be guaranteed the save is
        refused. On any failure the original file is untouched and the
        live handle remains valid.
        """
        self._ensure_open()
        if not target_path or not isinstance(target_path, str):
            raise ValueError(f"invalid save path: {target_path!r}")
        if not isinstance(manifest_bytes, (bytes, bytearray)):
            raise ValueError("manifest_bytes must be bytes")
        manifest_bytes = bytes(manifest_bytes)
        if not manifest_bytes:
            raise ValueError("manifest_bytes must not be empty")
        if isinstance(expected_page_count, bool) or not isinstance(
                expected_page_count, int) or expected_page_count < 0:
            raise ValueError("invalid expected page count")

        self.ensure_export_supported()

        # Disk-backed snapshot. The previous full in-memory serialization
        # approach held two to three full document copies in RAM at once
        # (the snapshot bytes, the in-memory annotation holder, and for
        # same-path saves the replacement handle), which exhausted memory
        # on large PDFs. Saving the same serialization to a private temp
        # file and opening file-backed handles from it keeps peak memory
        # bounded: PyMuPDF reads objects lazily from disk. Encryption
        # semantics are identical to the old in-memory bytes snapshot.
        fd, snapshot_path = tempfile.mkstemp(
            prefix="inkfall-snapshot-", suffix=".pdf")
        os.close(fd)
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        _emit_progress(progress, "snapshot")
        try:
            self._doc.save(snapshot_path, encryption=_PDF_ENCRYPT_KEEP)
        except Exception as e:
            try:
                if os.path.isfile(snapshot_path):
                    os.remove(snapshot_path)
            except Exception:
                pass
            if self._encrypted_at_open:
                raise EncryptedRewriteError(
                    _ENCRYPTED_REWRITE_MESSAGE) from e
            raise InvalidPdfError(
                f"Cannot snapshot document for save: {e}") from e

        holder = None
        replacement = None
        temp_path = None
        try:
            holder, holder_protected = self._open_snapshot(snapshot_path)
            if self._encrypted_at_open and not holder_protected:
                raise EncryptedRewriteError(
                    "The exported copy of this encrypted PDF would lose its "
                    "protection. The save was refused and the original file "
                    "is unchanged.")
            protect_output = bool(holder_protected)

            target_abs = os.path.abspath(os.path.expanduser(target_path))
            target_dir = os.path.dirname(target_abs) or "."
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as e:
                if _is_destination_lock_error(e):
                    raise SaveLockedError(
                        _unwritable_message(target_abs, e)) from e
                raise InvalidPdfError(
                    f"Cannot prepare save directory: {e}") from e

            try:
                original_attachments = tuple(holder.embfile_names() or ())
            except Exception:
                original_attachments = ()

            _emit_progress(progress, "annotations")
            expected_markers = _apply_export_annotations(
                holder, native_pages)

            _write_manifest_attachment(holder, manifest_bytes)

            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=".inkfall-save-", suffix=".pdf", dir=target_dir)
            except Exception as e:
                if _is_destination_lock_error(e):
                    raise SaveLockedError(
                        _unwritable_message(target_abs, e)) from e
                raise
            os.close(fd)
            try:
                os.remove(temp_path)
            except OSError:
                pass
            try:
                _emit_progress(progress, "write")
                holder.save(temp_path, garbage=3, deflate=True,
                            encryption=_PDF_ENCRYPT_KEEP)
            except Exception as exc:
                text = str(exc).lower()
                if self._encrypted_at_open and (
                        "encrypt" in text or "password" in text):
                    raise EncryptedRewriteError(
                        _ENCRYPTED_REWRITE_MESSAGE) from exc
                raise
            holder.close()
            holder = None

            expected_permissions = None
            if protect_output:
                try:
                    expected_permissions = int(self._doc.permissions)
                except Exception:
                    expected_permissions = None
            _emit_progress(progress, "verify")
            _verify_saved_pdf(
                temp_path, expected_page_count, manifest_bytes,
                expected_paper_keywords, original_attachments,
                expected_markers=expected_markers,
                password=self._password if protect_output else None,
                expect_encrypted=protect_output,
                expected_permissions=expected_permissions)
            same_target = _same_path(target_abs, self._path)
            if same_target:
                replacement, replacement_protected = self._open_snapshot(
                    snapshot_path)
                if protect_output and not replacement_protected:
                    raise EncryptedRewriteError(
                        "The live replacement copy of this encrypted PDF "
                        "would lose its protection. The save was refused and "
                        "the original file is unchanged.")
                old_doc = self._doc
                self._doc = None
                try:
                    old_doc.close()
                except Exception:
                    pass
                # The closed handle may have been backed by a previous
                # save's snapshot file; nothing needs that file any more.
                retired, self._snapshot_pending = self._snapshot_pending, None
                if retired is not None:
                    try:
                        if os.path.isfile(retired):
                            os.remove(retired)
                    except Exception:
                        pass
                try:
                    _emit_progress(progress, "replace")
                    _replace_target(temp_path, target_abs)
                except Exception:
                    self._doc = replacement
                    replacement = None
                    self._snapshot_pending = snapshot_path
                    raise
                self._doc = replacement
                replacement = None
                # The live handle now reads lazily from the snapshot file;
                # it is deleted when the handle it backs closes (close()).
                self._snapshot_pending = snapshot_path
            else:
                _emit_progress(progress, "replace")
                _replace_target(temp_path, target_abs)
            temp_path = None
        except InvalidPdfError:
            raise
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot save PDF: {target_path} "
                f"({type(e).__name__}: {e})") from e
        finally:
            if holder is not None:
                try:
                    holder.close()
                except Exception:
                    pass
            if replacement is not None:
                try:
                    replacement.close()
                except Exception:
                    pass
            if temp_path is not None:
                try:
                    if os.path.isfile(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
            if self._doc is None:
                # Defensive: a same-path failure before replacement was
                # installed must leave a usable live handle.
                try:
                    self._doc, _protected = self._open_snapshot(snapshot_path)
                    self._snapshot_pending = snapshot_path
                except Exception:
                    self._closed = True
            if self._snapshot_pending != snapshot_path:
                # No surviving handle reads the snapshot file: it can go.
                try:
                    if os.path.isfile(snapshot_path):
                        os.remove(snapshot_path)
                except Exception:
                    pass

    def export_pdf_in_process(self, target_path: str, native_pages: list,
                              manifest_bytes: bytes, expected_page_count: int,
                              expected_paper_keywords: str | None = None,
                              *, progress=None, pump=None) -> None:
        """Export through a spawned child process (the UI save path).

        Same contract and integrity checks as :meth:`export_pdf`, but the
        annotation insertion, the ``garbage=3``/deflate rewrite and the
        verification run in a child process, because PyMuPDF holds the GIL
        for an entire ``save`` call (a thread cannot keep Qt's event loop
        alive). The parent still creates the snapshot (a fast plain copy),
        performs the atomic replacement and installs the replacement
        handle, so destination-lock classification and the live-document
        handoff stay in-process.

        ``pump`` is the UI's event-pumping wait: ``pump(poll)`` must call
        ``poll() -> (done, payload)`` until done and return the final
        payload. Without it the wait is a plain blocking loop.
        """
        self._ensure_open()
        if not target_path or not isinstance(target_path, str):
            raise ValueError(f"invalid save path: {target_path!r}")
        if not isinstance(manifest_bytes, (bytes, bytearray)):
            raise ValueError("manifest_bytes must be bytes")
        manifest_bytes = bytes(manifest_bytes)
        if not manifest_bytes:
            raise ValueError("manifest_bytes must not be empty")
        if isinstance(expected_page_count, bool) or not isinstance(
                expected_page_count, int) or expected_page_count < 0:
            raise ValueError("invalid expected page count")

        self.ensure_export_supported()

        fd, snapshot_path = tempfile.mkstemp(
            prefix="inkfall-snapshot-", suffix=".pdf")
        os.close(fd)
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        _emit_progress(progress, "snapshot")
        try:
            self._doc.save(snapshot_path, encryption=_PDF_ENCRYPT_KEEP)
        except Exception as e:
            try:
                if os.path.isfile(snapshot_path):
                    os.remove(snapshot_path)
            except Exception:
                pass
            if self._encrypted_at_open:
                raise EncryptedRewriteError(
                    _ENCRYPTED_REWRITE_MESSAGE) from e
            raise InvalidPdfError(
                f"Cannot snapshot document for save: {e}") from e

        target_abs = os.path.abspath(os.path.expanduser(target_path))
        target_dir = os.path.dirname(target_abs) or "."
        same_target = _same_path(target_abs, self._path)
        expected_permissions = None
        if self._encrypted_at_open:
            try:
                expected_permissions = int(self._doc.permissions)
            except Exception:
                expected_permissions = None
        job = {
            "snapshot": snapshot_path,
            "target": target_abs,
            "target_dir": target_dir,
            "native_pages": list(native_pages or ()),
            "manifest": manifest_bytes,
            "page_count": int(expected_page_count),
            "paper_keywords": expected_paper_keywords,
            "password": self._password,
            "encrypted_at_open": bool(self._encrypted_at_open),
            "permissions": expected_permissions,
        }

        temp_path = None
        snapshot_pending = False
        try:
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as e:
                if _is_destination_lock_error(e):
                    raise SaveLockedError(
                        _unwritable_message(target_abs, e)) from e
                raise InvalidPdfError(
                    f"Cannot prepare save directory: {e}") from e

            if same_target:
                old_doc = self._doc
                self._doc = None
                try:
                    old_doc.close()
                except Exception:
                    pass
                retired, self._snapshot_pending = self._snapshot_pending, None
                if retired is not None and retired != snapshot_path:
                    try:
                        if os.path.isfile(retired):
                            os.remove(retired)
                    except Exception:
                        pass

            result = self._run_export_process(
                job, progress=progress, pump=pump)
            if not result or not result.get("ok"):
                raise _export_error_from_result(result or {})
            temp_path = result.get("temp")
            if not isinstance(temp_path, str) or not os.path.isfile(temp_path):
                raise InvalidPdfError(
                    "The save worker produced no output file.")
            _emit_progress(progress, "replace")
            _replace_target(temp_path, target_abs)
            temp_path = None
            if same_target:
                replacement, _protected = self._open_snapshot(snapshot_path)
                self._doc = replacement
                self._snapshot_pending = snapshot_path
                snapshot_pending = True
        except InvalidPdfError:
            raise
        except Exception as e:
            raise InvalidPdfError(
                f"Cannot save PDF: {target_path} "
                f"({type(e).__name__}: {e})") from e
        finally:
            if temp_path is not None:
                try:
                    if os.path.isfile(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
            if self._doc is None:
                # A same-path failure must leave a usable live handle.
                try:
                    self._doc, _protected = self._open_snapshot(snapshot_path)
                    self._snapshot_pending = snapshot_path
                    snapshot_pending = True
                except Exception:
                    self._closed = True
            if not snapshot_pending:
                try:
                    if os.path.isfile(snapshot_path):
                        os.remove(snapshot_path)
                except Exception:
                    pass

    def _run_export_process(self, job: dict, *, progress=None,
                            pump=None) -> dict:
        """Spawn the export child and wait for its result.

        The child never touches the live handle; the parent keeps the GUI
        thread free by polling the result queue inside ``pump`` (or by a
        plain sleep loop for non-UI callers).
        """
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_export_child_main, args=(job, result_queue),
            name="inkfall-save")
        try:
            process.start()
        except Exception as exc:
            raise InvalidPdfError(
                f"Cannot start the save worker: {exc}") from exc
        try:
            if pump is not None:
                return pump(lambda: _poll_export_process(
                    process, result_queue)) or {}
            while True:
                done, payload = _poll_export_process(process, result_queue)
                stage = payload.get("stage") if payload else None
                if stage:
                    _emit_progress(progress, stage)
                if done:
                    return payload
                time.sleep(0.05)
        finally:
            try:
                process.join(20)
            except Exception:
                pass
            if process.is_alive():
                try:
                    process.terminate()
                except Exception:
                    pass
                try:
                    process.join(5)
                except Exception:
                    pass
            try:
                result_queue.close()
            except Exception:
                pass

    def authenticate(self, password: str) -> bool:
        """Authenticate an encrypted document. Raises on failure."""
        self._ensure_open()
        try:
            ok = bool(self._doc.authenticate(password))
        except Exception as e:
            raise WrongPasswordError(f"Cannot authenticate PDF: {self._path}") from e
        if not ok:
            raise WrongPasswordError(f"Incorrect password for PDF: {self._path}")
        return True

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Release the handle. Idempotent and safe to repeat."""
        if self._closed:
            return
        self._closed = True
        doc, self._doc = self._doc, None  # type: ignore[assignment]
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass
        # The handle that pinned the pending snapshot file is gone now.
        snapshot, self._snapshot_pending = self._snapshot_pending, None
        if snapshot is not None:
            try:
                if os.path.isfile(snapshot):
                    os.remove(snapshot)
            except Exception:
                pass

    def _ensure_open(self) -> None:
        if self._closed or self._doc is None:
            raise DocumentClosedError(f"Document is closed: {self._path}")

    def __enter__(self) -> PdfBackend:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _serialize_page(handle, index: int) -> bytes:
    """Serialise one page of a PDF handle to a standalone single-page PDF.

    Module-level (like :func:`render_cover_preview`): it opens its own
    short-lived handle and never participates in the lazy ``PdfBackend``
    document-handle path (open/page_count/metadata/page_info). Used only
    by the explicit M6.8.2 page-deletion snapshot, where the whole point
    is to capture a page eagerly.
    """
    holder = pymupdf.open()
    try:
        holder.insert_pdf(handle, from_page=index, to_page=index)
        return bytes(holder.tobytes())
    finally:
        try:
            holder.close()
        except Exception:
            pass


def create_blank_pdf(path: str) -> str:
    """Write a minimal one-page blank PDF (M4.6.5 New > Create Blank).

    Isolated file-creation helper: one blank A4 page, nothing else. No
    rendering, scheduler, worker, cache, decoder, or ownership changes —
    the file flows through the existing import, DocumentManager, and
    viewer path like any other PDF. Raises on failure (caller toasts).
    """
    doc = pymupdf.open()
    try:
        doc.new_page()
        doc.save(path)
    finally:
        doc.close()
    return path


def _hex_rgb(value) -> tuple[float, float, float]:
    """``#rrggbb`` -> normalized RGB triple for pymupdf (never raises)."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (0.0, 0.0, 0.0)
    try:
        return (int(text[0:2], 16) / 255.0,
                int(text[2:4], 16) / 255.0,
                int(text[4:6], 16) / 255.0)
    except Exception:
        return (0.0, 0.0, 0.0)


def _same_path(first: str, second: str) -> bool:
    try:
        return (os.path.normcase(os.path.abspath(os.path.expanduser(str(first))))
                == os.path.normcase(os.path.abspath(
                    os.path.expanduser(str(second)))))
    except Exception:
        return False


def _locked_message(target_abs: str, exc: BaseException) -> str:
    return ("The destination file is in use and could not be replaced: "
            f"{target_abs}. Close it in other applications (PDF viewer, "
            f"sync client, antivirus) and try again. ({exc})")


def _unwritable_message(target_abs: str, exc: BaseException) -> str:
    return ("The destination could not be written: "
            f"{target_abs}. The location may be read-only, full, or "
            f"protected; save to a different location instead. ({exc})")


#: errno values that mean the destination (file or its directory) cannot
#: be written: access denied, permission problem, read-only filesystem,
#: or a resource temporarily held by another handle.
_DESTINATION_LOCK_ERRNO = frozenset(
    {errno.EACCES, errno.EPERM, errno.EROFS, errno.EBUSY})


def _is_destination_lock_error(exc: BaseException) -> bool:
    """Classify destination-related write failures without message matching.

    Windows sharing violations (WinError 32/33) and access-denied errors
    (WinError 5), ``PermissionError`` (including POSIX EACCES/EPERM), and
    read-only/busy filesystem errnos all mean the destination location
    cannot be written and are recoverable (Save As a copy). Everything
    else is a genuine export failure.
    """
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in (5, 32, 33):
            return True
        return exc.errno in _DESTINATION_LOCK_ERRNO
    return False


def _emit_progress(progress, stage: str) -> None:
    """Report a save stage to an optional listener; never fail the save.

    Stages are opaque names ("snapshot", "annotations", "write", "verify",
    "replace"); no document data or paths cross the callback boundary, so
    it is safe to call from a save worker thread (the UI only emits a Qt
    signal from it).
    """
    if progress is None:
        return
    try:
        progress(stage)
    except Exception:
        pass


def _open_export_snapshot(snapshot_path: str, password: str | None = None):
    """Open an export snapshot file; authenticate if it kept encryption.

    Shared by the parent backend and the save child process. Returns
    ``(document, was_encrypted)``. The file must outlive the returned
    handle (PyMuPDF keeps it open). The encryption state is sampled before
    authenticating because PyMuPDF stops reporting it once access is
    granted.
    """
    doc = pymupdf.open(snapshot_path)
    try:
        protected = bool(doc.needs_pass or doc.is_encrypted)
        if doc.needs_pass:
            if not password:
                raise EncryptedRewriteError(
                    "The exported copy of this encrypted PDF requires a "
                    "password that is not available; the save was "
                    "refused and the original file is unchanged.")
            try:
                ok = bool(doc.authenticate(password))
            except Exception as exc:
                raise EncryptedRewriteError(
                    "The exported copy of this encrypted PDF could not "
                    "be authenticated; the save was refused and the "
                    "original file is unchanged.") from exc
            if not ok:
                raise EncryptedRewriteError(
                    "The exported copy of this encrypted PDF could not "
                    "be authenticated with the password used to open it; "
                    "the save was refused and the original file is "
                    "unchanged.")
        return doc, protected
    except Exception:
        try:
            doc.close()
        except Exception:
            pass
        raise


def _apply_export_annotations(holder, native_pages: list) -> dict:
    """Insert the expected native annotations into ``holder``.

    Returns the expected marker map used by verification. Shared by the
    synchronous export and the save child process.
    """
    expected_markers: dict[int, list[str]] = {}
    for index, specs in enumerate(native_pages or ()):
        if not specs:
            continue
        if index >= len(holder):
            raise NativeAnnotationError(
                f"Cannot save native annotations for page "
                f"{index + 1}: the exported document has "
                f"{len(holder)} pages.")
        page = holder[index]
        markers = [_add_native_annotation(page, spec) for spec in specs]
        if markers:
            expected_markers[index] = markers
    return expected_markers


def _write_export_temp(holder, target_dir: str, target_abs: str) -> str:
    """Write the annotated holder to a private temp file in ``target_dir``.

    Kept beside the destination so the parent's ``os.replace`` stays on
    one volume. Destination lock failures are classified here so the
    child can report them without the parent having to re-derive them.
    """
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=".inkfall-save-", suffix=".pdf", dir=target_dir)
    except Exception as e:
        if _is_destination_lock_error(e):
            raise SaveLockedError(_unwritable_message(target_abs, e)) from e
        raise
    os.close(fd)
    try:
        os.remove(temp_path)
    except OSError:
        pass
    try:
        holder.save(temp_path, garbage=3, deflate=True,
                    encryption=_PDF_ENCRYPT_KEEP)
    except Exception:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise
    return temp_path


def _export_error_kind(exc: BaseException) -> str:
    """Map an export exception to a cross-process kind string."""
    if isinstance(exc, SaveLockedError):
        return "locked"
    if isinstance(exc, EncryptedRewriteError):
        return "encrypted"
    if isinstance(exc, NativeAnnotationError):
        return "native"
    if isinstance(exc, SaveVerificationError):
        return "verify"
    return "other"


def _export_error_from_result(result: dict) -> BaseException:
    """Rebuild a typed export exception from the child's plain result."""
    kind = result.get("kind") or "other"
    message = result.get("message") or "Cannot save PDF"
    if kind == "locked":
        return SaveLockedError(message)
    if kind == "encrypted":
        return EncryptedRewriteError(message)
    if kind == "native":
        return NativeAnnotationError(message)
    if kind == "verify":
        return SaveVerificationError(message)
    return InvalidPdfError(message)


def _export_child_main(job: dict, result_queue) -> None:
    """Child entry point: annotate + rewrite + verify one export.

    Module-level so ``multiprocessing``'s ``spawn`` can pickle it. Receives
    plain data only (paths, bytes, dicts) — never a live PyMuPDF handle —
    and returns the verified temporary output path; the parent performs
    the atomic replacement so destination-lock classification and the live
    document handoff stay in one place. PyMuPDF holds the GIL for a whole
    ``save`` call, which is exactly why this runs in a process and not on
    a thread.
    """
    holder = None
    temp_path = None
    try:
        holder, protected = _open_export_snapshot(
            job["snapshot"], job.get("password"))
        if job["encrypted_at_open"] and not protected:
            raise EncryptedRewriteError(
                "The exported copy of this encrypted PDF would lose its "
                "protection. The save was refused and the original file "
                "is unchanged.")
        result_queue.put(("stage", "annotations"))
        try:
            original_attachments = tuple(holder.embfile_names() or ())
        except Exception:
            original_attachments = ()
        markers = _apply_export_annotations(holder, job["native_pages"])
        _write_manifest_attachment(holder, job["manifest"])
        result_queue.put(("stage", "write"))
        try:
            temp_path = _write_export_temp(
                holder, job["target_dir"], job["target"])
        except Exception as exc:
            text = str(exc).lower()
            if job["encrypted_at_open"] and (
                    "encrypt" in text or "password" in text):
                raise EncryptedRewriteError(
                    _ENCRYPTED_REWRITE_MESSAGE) from exc
            raise
        holder.close()
        holder = None
        result_queue.put(("stage", "verify"))
        _verify_saved_pdf(
            temp_path, job["page_count"], job["manifest"],
            job["paper_keywords"], original_attachments,
            expected_markers=markers,
            password=job.get("password") if protected else None,
            expect_encrypted=protected,
            expected_permissions=(job.get("permissions")
                                  if protected else None))
        result_queue.put(("done", {"ok": True, "temp": temp_path}))
        temp_path = None
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        result_queue.put(("done", {
            "ok": False,
            "kind": _export_error_kind(exc),
            "message": str(exc) or type(exc).__name__,
        }))
    finally:
        if holder is not None:
            try:
                holder.close()
            except Exception:
                pass
        if temp_path is not None:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass


def _poll_export_process(proc, result_queue) -> tuple[bool, dict]:
    """Non-blocking poll of one save child: ``(done, payload)``.

    A non-final payload may carry ``{"stage": name}``; the final payload is
    the child's result dict (``ok`` plus ``temp`` or an error descriptor).
    A child that exits without reporting is a hard failure, never a lock.
    """
    stage = None
    while True:
        try:
            kind, value = result_queue.get_nowait()
        except Empty:
            break
        except (OSError, EOFError):
            break
        if kind == "stage":
            stage = value
        elif kind == "done":
            return True, value
    if not proc.is_alive():
        # The feeder can flush just after the process exits.
        try:
            kind, value = result_queue.get(timeout=0.25)
            if kind == "done":
                return True, value
        except (Empty, OSError, EOFError):
            pass
        return True, {
            "ok": False, "kind": "crash",
            "message": ("The save worker stopped unexpectedly "
                        f"(exit code {proc.exitcode})."),
        }
    return False, ({"stage": stage} if stage else {})


def _replace_target(temp_path: str, target_abs: str) -> None:
    """Atomically replace the destination, classifying lock failures.

    Windows sharing violations (WinError 32/33) and access-denied errors
    (WinError 5) mean the destination is held open by another handle. They
    surface as :class:`SaveLockedError` with an actionable message so the
    UI can distinguish them from genuine export failures; every other OS
    error keeps its original detail.
    """
    try:
        os.replace(temp_path, target_abs)
    except PermissionError as exc:
        raise SaveLockedError(_locked_message(target_abs, exc)) from exc
    except OSError as exc:
        if _is_destination_lock_error(exc):
            raise SaveLockedError(_locked_message(target_abs, exc)) from exc
        raise InvalidPdfError(
            f"Cannot replace the saved PDF at {target_abs}: {exc}") from exc


def _write_manifest_attachment(holder, payload: bytes) -> None:
    """Add or replace the single constant Inkfall manifest attachment."""
    names = list(holder.embfile_names() or ())
    if EMBEDDED_NAME in names:
        # PyMuPDF 1.28's ``embfile_upd`` raises AttributeError for a
        # ``bytes`` payload (it dereferences an internal Buffer on the
        # caller's object). Delete + add replaces the content reliably
        # and still leaves exactly one attachment.
        holder.embfile_del(EMBEDDED_NAME)
    holder.embfile_add(
        EMBEDDED_NAME, payload, filename=EMBEDDED_NAME,
        ufilename=EMBEDDED_NAME,
        desc="Inkfall editable annotations (versioned manifest)")


def _native_marker(annot) -> str:
    """Return the annotation's ownership subject, or an empty string."""
    try:
        info = annot.info or {}
    except Exception:
        return ""
    subject = info.get("subject")
    return str(subject) if subject else ""


def _native_has_appearance(document, annot) -> bool:
    """Structurally verify that an annotation has a normal appearance.

    ``/AP /N`` is the appearance stream every non-MuPDF reader uses to
    paint the annotation. Marker/subject presence alone proves nothing
    about rendering, so the saved output is checked for a non-null
    appearance: an indirect stream must exist and expose a ``/BBox``.
    Appearance sub-dictionaries are accepted as-is (their entries are
    streams that PyMuPDF does not expose through a public API).
    """
    try:
        kind, value = document.xref_get_key(annot.xref, "AP/N")
    except Exception:
        return False
    if not kind or kind == "null" or not value:
        return False
    if kind != "xref":
        return True
    try:
        xref = int(str(value).split()[0])
    except (TypeError, ValueError):
        return False
    try:
        if document.xref_is_stream(xref):
            bbox_kind, bbox = document.xref_get_key(xref, "BBox")
            return bool(bbox_kind and bbox_kind != "null" and bbox)
        # Indirect appearance dictionary: require a normal (/N) entry.
        entry_kind, entry_value = document.xref_get_key(xref, "N")
        return bool(entry_kind and entry_kind != "null" and entry_value)
    except Exception:
        return False


def _verify_native_annotations(document, expected_markers) -> None:
    """Verify each expected native annotation exists once, on its page.

    Unrelated or pre-existing prefixed annotations are preserved and
    ignored: only the markers written by this export are required to be
    unique. A missing marker, a duplicate (the signature of accumulation
    across repeated saves), a marker on the wrong page, or an annotation
    without a renderable appearance aborts the save before replacement.
    """
    if not expected_markers:
        return
    seen: dict[str, list[tuple[int, bool]]] = {}
    for page_index in range(len(document)):
        try:
            annots = list(document[page_index].annots() or ())
        except Exception as exc:
            raise NativeAnnotationError(
                "Cannot inspect the saved native annotations on page "
                f"{page_index + 1}: {exc}") from exc
        for annot in annots:
            marker = _native_marker(annot)
            if not marker.startswith(NATIVE_SUBJECT_PREFIX):
                continue
            seen.setdefault(marker, []).append(
                (page_index, _native_has_appearance(document, annot)))

    for page_index, markers in expected_markers.items():
        for marker in markers:
            hits = seen.get(marker) or []
            if not hits:
                raise NativeAnnotationError(
                    f"The saved PDF is missing the native annotation for "
                    f"{marker!r} (page {page_index + 1}).")
            if len(hits) > 1:
                raise NativeAnnotationError(
                    f"The saved PDF contains duplicate native annotations "
                    f"for {marker!r}.")
            hit_page, has_appearance = hits[0]
            if hit_page != page_index:
                raise NativeAnnotationError(
                    f"The native annotation for {marker!r} was saved on "
                    f"page {hit_page + 1} instead of page {page_index + 1}.")
            if not has_appearance:
                raise NativeAnnotationError(
                    f"The native annotation for {marker!r} has no renderable "
                    "appearance stream in the saved PDF.")


def _verify_saved_pdf(path: str, expected_page_count: int,
                      manifest_bytes: bytes,
                      expected_paper_keywords: str | None,
                      expected_attachments=(),
                      expected_markers=None,
                      password: str | None = None,
                      expect_encrypted: bool = False,
                      expected_permissions: int | None = None) -> None:
    """Reopen the temporary output and verify the save invariants.

    Encrypted sources are authenticated with the password used to open
    them, and the output is required to still be encrypted when the source
    was: silently dropping protection is a save failure, not a success.
    """
    verify = None
    try:
        verify = pymupdf.open(path)
        # Sample protection before authenticating: PyMuPDF stops reporting
        # it once access has been granted.
        protected = bool(verify.needs_pass or verify.is_encrypted)
        if expect_encrypted and not protected:
            raise EncryptedRewriteError(
                "The saved output is no longer encrypted, so the original "
                "protection would have been lost. The save was refused and "
                "the original file is unchanged.")
        if protected:
            if not password:
                raise EncryptedRewriteError(
                    "The saved output is encrypted but no password is "
                    "available to verify it.")
            try:
                authenticated = bool(verify.authenticate(password))
            except Exception as exc:
                raise EncryptedRewriteError(
                    f"The saved output could not be authenticated: {exc}"
                ) from exc
            if not authenticated:
                raise EncryptedRewriteError(
                    "The saved output could not be authenticated with the "
                    "password used to open the source PDF.")
        if protected and expected_permissions is not None:
            try:
                actual_permissions = int(verify.permissions)
            except Exception as exc:
                raise EncryptedRewriteError(
                    f"Cannot verify the saved PDF permissions: {exc}"
                ) from exc
            if actual_permissions != int(expected_permissions):
                raise EncryptedRewriteError(
                    "The saved output changed the PDF permissions. The save "
                    "was refused and the original file is unchanged.")
        if not verify.is_pdf:
            raise SaveVerificationError("saved output is not a PDF")
        if len(verify) != expected_page_count:
            raise SaveVerificationError(
                f"saved output has {len(verify)} pages, expected "
                f"{expected_page_count}")
        names = list(verify.embfile_names() or ())
        missing = [name for name in expected_attachments
                   if name not in names]
        if missing:
            raise SaveVerificationError(
                "saved output lost unrelated embedded attachments: "
                + ", ".join(missing[:3]))
        if EMBEDDED_NAME not in names:
            raise SaveVerificationError(
                "saved output is missing the Inkfall manifest attachment")
        stored = verify.embfile_get(EMBEDDED_NAME)
        if stored is None or bytes(stored) != manifest_bytes:
            raise SaveVerificationError(
                "saved output manifest does not match the exported manifest")
        if expected_paper_keywords:
            keywords = (verify.metadata or {}).get("keywords") or ""
            if expected_paper_keywords not in str(keywords):
                raise SaveVerificationError(
                    "saved output lost the Inkfall PaperSpec metadata")
        _verify_native_annotations(verify, expected_markers)
    except InvalidPdfError:
        raise
    except Exception as e:
        raise SaveVerificationError(
            f"Cannot verify saved PDF: {path} ({e})") from e
    finally:
        if verify is not None:
            try:
                verify.close()
            except Exception:
                pass


def _native_point(page, x, y):
    # PyMuPDF 1.28.x transforms points with ``Point * Matrix``; the reverse
    # order (``Matrix * Point``) raises ``ValueError: Matrix: bad seq len``.
    return pymupdf.Point(float(x), float(y)) * page.derotation_matrix


def _native_rect(page, x: float, y: float, width: float, height: float):
    points = [
        _native_point(page, x, y),
        _native_point(page, x + width, y),
        _native_point(page, x + width, y + height),
        _native_point(page, x, y + height),
    ]
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def _native_dashes(style: str):
    # Annot.set_border accepts only integer dash items; float patterns are
    # silently discarded by the installed PyMuPDF API.
    if style == "dashed":
        return [6, 4]
    if style == "dotted":
        return [1, 3]
    return None


def _native_line_ending(value: str) -> int:
    # PDF line-ending codes: 0 None, 2 Circle, 5 ClosedArrow, 6 Butt.
    return {"dot": 2, "bar": 6, "arrow": 5}.get(str(value), 0)


def _apply_native_marker(annot, spec) -> str:
    """Write the ownership marker and return it.

    ``Annot.set_info`` only writes subject/author for annotation types that
    MuPDF considers markup. Sticky-note Text annotations may therefore need
    a direct dictionary fallback. The marker is verified separately, after
    the appearance has been regenerated.
    """
    identity = str(spec.get("id") or "")
    if not identity:
        raise NativeAnnotationError("native annotation identity is missing")
    marker = NATIVE_SUBJECT_PREFIX + identity
    try:
        annot.set_info(subject=marker, title=NATIVE_TITLE)
    except Exception:
        pass
    document = None
    info = annot.info or {}
    if str(info.get("subject") or "") != marker:
        document = annot.parent.parent
        document.xref_set_key(
            annot.xref, "Subj", pymupdf.get_pdf_str(marker))
    info = annot.info or {}
    if str(info.get("subject") or "") != marker:
        raise NativeAnnotationError(
            f"Cannot mark native annotation {identity!r} for ownership")
    if str(info.get("title") or "") != NATIVE_TITLE:
        try:
            if document is None:
                document = annot.parent.parent
            document.xref_set_key(
                annot.xref, "T", pymupdf.get_pdf_str(NATIVE_TITLE))
        except Exception:
            # The subject marker alone is sufficient for ownership.
            pass
    return marker


def _verify_native_marker(annot, spec, marker: str) -> None:
    """Confirm the ownership marker survived appearance regeneration."""
    info = annot.info or {}
    if str(info.get("subject") or "") != marker:
        raise NativeAnnotationError(
            "Cannot mark native annotation "
            f"{spec.get('id')!r} for ownership")


def _refresh_native_appearance(annot, spec) -> None:
    """Regenerate the appearance stream external readers require.

    PyMuPDF only builds ``/AP`` when ``Annot.update()`` is called; the
    colour/border/opacity/line-ending setters mutate the annotation
    dictionary without producing a renderable appearance. Failure is fatal
    for this save: writing an annotation without a usable appearance would
    make it invisible to non-MuPDF readers.
    """
    try:
        annot.update()
    except Exception as exc:
        raise NativeAnnotationError(
            "Cannot generate a renderable appearance for native annotation "
            f"{spec.get('id')!r}: {exc}") from exc


def _apply_native_colors(annot, spec, *, fill: bool = False) -> None:
    stroke = _hex_rgb(spec.get("color"))
    fill_color = None
    if fill and spec.get("fill_enabled") and float(
            spec.get("fill_opacity") or 0.0) > 0.0:
        fill_color = _hex_rgb(spec.get("fill_color"))
    annot.set_colors(stroke=stroke, fill=fill_color)
    opacity = max(0.0, min(1.0, float(spec.get("opacity") or 0.0)))
    if opacity <= 0.0 and fill_color is not None:
        opacity = max(0.0, min(1.0, float(spec.get("fill_opacity") or 0.0)))
    annot.set_opacity(opacity)


def _apply_native_border(annot, spec) -> None:
    width = max(0.2, float(spec.get("width_pt") or 0.5))
    dashes = _native_dashes(str(spec.get("line_style") or "solid"))
    if dashes is None:
        annot.set_border(width=width)
    else:
        annot.set_border(width=width, dashes=dashes)


def _highlight_rects(page, spec):
    points = [_native_point(page, x, y)
              for x, y in (spec.get("points") or ())]
    if not points:
        return []
    half = max(0.5, float(spec.get("width_pt") or 3.0)) / 2.0
    rects = []
    if len(points) == 1:
        p = points[0]
        rects.append(pymupdf.Rect(
            p.x - half, p.y - half, p.x + half, p.y + half))
        return rects
    for first, second in zip(points, points[1:]):
        rects.append(pymupdf.Rect(
            min(first.x, second.x) - half,
            min(first.y, second.y) - half,
            max(first.x, second.x) + half,
            max(first.y, second.y) + half))
    return rects


def _add_native_annotation(page, spec) -> str:
    """Insert one native counterpart and return its ownership marker.

    Geometry, colours, opacity, border, dashes, line endings, rotation and
    the ownership marker are configured first. ``Annot.update()`` then
    regenerates the ``/AP`` appearance stream that external readers require,
    and the marker is verified afterwards. Any failure raises
    :class:`NativeAnnotationError`, which abandons the whole export before
    the destination is touched.
    """
    if not isinstance(spec, dict):
        raise NativeAnnotationError(
            "native annotation specification is invalid")
    identity = str(spec.get("id") or "")
    if not identity:
        raise NativeAnnotationError("native annotation identity is missing")
    kind = spec.get("kind")
    annot = None
    try:
        if kind == "ink":
            points = []
            for x, y in (spec.get("points") or ()):
                transformed = _native_point(page, x, y)
                # add_ink_annot requires sequences of (x, y) pairs, not
                # Point objects.
                points.append((transformed.x, transformed.y))
            if not points:
                raise NativeAnnotationError(
                    f"ink annotation {identity!r} has no points")
            annot = page.add_ink_annot([points])
            if annot is not None:
                _apply_native_colors(annot, spec)
                _apply_native_border(annot, spec)
        elif kind == "highlight":
            rects = _highlight_rects(page, spec)
            if not rects:
                raise NativeAnnotationError(
                    f"highlight annotation {identity!r} has no geometry")
            annot = page.add_highlight_annot(quads=rects)
            if annot is not None:
                _apply_native_colors(annot, spec)
        elif kind == "shape":
            shape_kind = str(spec.get("shape_kind") or "")
            points = [_native_point(page, x, y)
                      for x, y in (spec.get("points") or ())]
            if shape_kind in ("line", "arrow"):
                if len(points) < 2:
                    raise NativeAnnotationError(
                        f"{shape_kind} annotation {identity!r} needs two "
                        "endpoints")
                annot = page.add_line_annot(points[0], points[1])
                if annot is not None:
                    _apply_native_colors(annot, spec, fill=True)
                    _apply_native_border(annot, spec)
                    start = _native_line_ending(spec.get("start_ending"))
                    end = _native_line_ending(spec.get("end_ending"))
                    if shape_kind == "arrow" and end == 0:
                        end = 5
                    try:
                        annot.set_line_ends(start, end)
                    except Exception:
                        pass
            elif shape_kind in ("rectangle", "ellipse"):
                rect = spec.get("rect") or (0.0, 0.0, 0.0, 0.0)
                x, y, width, height = [float(value) for value in rect]
                if width <= 0.0 or height <= 0.0:
                    raise NativeAnnotationError(
                        f"{shape_kind} annotation {identity!r} has an empty "
                        "rectangle")
                native_rect = _native_rect(page, x, y, width, height)
                if shape_kind == "rectangle":
                    annot = page.add_rect_annot(native_rect)
                else:
                    annot = page.add_circle_annot(native_rect)
                if annot is not None:
                    _apply_native_colors(annot, spec, fill=True)
                    _apply_native_border(annot, spec)
                    rotation = float(spec.get("rotation_deg") or 0.0)
                    if rotation:
                        try:
                            annot.set_rotation(int(round(rotation)) % 360)
                        except Exception:
                            pass
            elif shape_kind == "polygon":
                if len(points) < 2:
                    raise NativeAnnotationError(
                        f"polygon annotation {identity!r} needs at least "
                        "two points")
                if spec.get("close_path"):
                    annot = page.add_polygon_annot(points)
                else:
                    annot = page.add_polyline_annot(points)
                if annot is not None:
                    _apply_native_colors(
                        annot, spec, fill=bool(spec.get("close_path")))
                    _apply_native_border(annot, spec)
                    start = _native_line_ending(spec.get("start_ending"))
                    end = _native_line_ending(spec.get("end_ending"))
                    try:
                        annot.set_line_ends(start, end)
                    except Exception:
                        pass
            else:
                raise NativeAnnotationError(
                    f"unsupported native shape kind {shape_kind!r}")
        elif kind == "note":
            point = spec.get("point") or (0.0, 0.0)
            mapped = _native_point(page, point[0], point[1])
            annot = page.add_text_annot(mapped, str(spec.get("text") or ""))
            if annot is not None:
                try:
                    annot.set_colors(stroke=_hex_rgb(spec.get("color")))
                except Exception:
                    pass
        else:
            raise NativeAnnotationError(
                f"unsupported native annotation kind {kind!r}")

        if annot is None:
            raise NativeAnnotationError(
                f"cannot create a native {kind} annotation for {identity!r}")
        marker = _apply_native_marker(annot, spec)
        _refresh_native_appearance(annot, spec)
        _verify_native_marker(annot, spec, marker)
        return marker
    except Exception as exc:
        if annot is not None:
            try:
                page.delete_annot(annot)
            except Exception:
                pass
        if isinstance(exc, NativeAnnotationError):
            raise
        raise NativeAnnotationError(
            f"Cannot create native annotation {identity!r}: {exc}") from exc


def _draw_template_items(page, items, color) -> None:
    """Draw one page's template primitives (see ``document.paper``)."""
    rgb = _hex_rgb(color)
    for item in items or ():
        try:
            kind = item[0]
            if kind == "line":
                _, x0, y0, x1, y1, alpha, width = item
                page.draw_line(pymupdf.Point(x0, y0), pymupdf.Point(x1, y1),
                               color=rgb, width=float(width),
                               stroke_opacity=max(0.0, min(1.0, alpha)))
            elif kind == "dot":
                _, cx, cy, radius = item
                page.draw_circle(pymupdf.Point(cx, cy), float(radius),
                                 color=None, fill=rgb, width=0,
                                 fill_opacity=1.0)
            elif kind == "rect":
                _, x0, y0, w, h, alpha, width = item
                page.draw_rect(pymupdf.Rect(x0, y0, x0 + w, y0 + h),
                               color=rgb, width=float(width),
                               stroke_opacity=max(0.0, min(1.0, alpha)))
            elif kind == "band":
                _, x0, y0, w, h, alpha = item
                page.draw_rect(pymupdf.Rect(x0, y0, x0 + w, y0 + h),
                               color=None, fill=rgb, width=0,
                               fill_opacity=max(0.0, min(1.0, alpha)))
        except Exception:
            continue


def _draw_annotation_items(page, items) -> None:
    """Draw annotation export items onto one page (displayed-frame coords).

    Kinds (produced by ``document.document``):

    ``("seg", x0,y0,x1,y1, hex, alpha, width, dashes)``
    ``("poly", pts, hex, alpha, width, fill_hex, fill_alpha, close, dashes)``
    ``("ell", cx,cy,rx,ry, hex, alpha, width, fill_hex, fill_alpha)``
    ``("text", x,y,w,h, text, hex, size)``

    Every coordinate is derotated through the page matrix first: the
    annotation store keeps displayed-frame geometry, while PDF content
    is stored in the unrotated page.
    """
    matrix = page.derotation_matrix
    # 90/270-degree page rotation swaps the axis meaning of ellipse radii
    # in stored page space.
    swap_radii = int(page.rotation) % 360 in (90, 270)

    def tp(x, y):
        point = pymupdf.Point(float(x), float(y)) * matrix
        return point

    for item in items or ():
        try:
            kind = item[0]
            if kind == "seg":
                _, x0, y0, x1, y1, color, alpha, width, dashes = item
                page.draw_line(tp(x0, y0), tp(x1, y1),
                               color=_hex_rgb(color),
                               width=max(0.2, float(width)),
                               dashes=dashes,
                               lineCap=1,
                               stroke_opacity=max(0.0, min(1.0, alpha)))
            elif kind == "poly":
                (_, points, color, alpha, width, fill_color, fill_alpha,
                 close, dashes) = item
                if not points:
                    continue
                mapped = [tp(x, y) for x, y in points]
                stroke = _hex_rgb(color) if alpha > 0.0 else None
                fill = (_hex_rgb(fill_color)
                        if fill_color is not None and fill_alpha > 0.0
                        else None)
                if stroke is None and fill is None:
                    continue
                page.draw_polyline(
                    mapped,
                    color=stroke,
                    fill=fill,
                    dashes=dashes,
                    width=max(0.2, float(width)),
                    closePath=bool(close),
                    lineCap=1,
                    lineJoin=1,
                    stroke_opacity=max(0.0, min(1.0, alpha)),
                    fill_opacity=max(0.0, min(1.0, fill_alpha)))
            elif kind == "ell":
                (_, cx, cy, rx, ry, color, alpha, width, fill_color,
                 fill_alpha) = item
                center = tp(cx, cy)
                r_x, r_y = float(rx), float(ry)
                if swap_radii:
                    r_x, r_y = r_y, r_x
                rect = pymupdf.Rect(center.x - r_x, center.y - r_y,
                                    center.x + r_x, center.y + r_y)
                stroke = _hex_rgb(color) if alpha > 0.0 else None
                fill = (_hex_rgb(fill_color)
                        if fill_color is not None and fill_alpha > 0.0
                        else None)
                if stroke is None and fill is None:
                    continue
                page.draw_oval(rect, color=stroke, fill=fill,
                               width=max(0.2, float(width)),
                               stroke_opacity=max(0.0, min(1.0, alpha)),
                               fill_opacity=max(0.0, min(1.0, fill_alpha)))
            elif kind == "text":
                _, x, y, w, h, text, color, size = item
                top_left = tp(x, y)
                rect = pymupdf.Rect(top_left.x, top_left.y,
                                    top_left.x + float(w),
                                    top_left.y + float(h))
                page.insert_textbox(
                    rect, str(text), fontsize=float(size),
                    color=_hex_rgb(color), align=pymupdf.TEXT_ALIGN_LEFT)
        except Exception:
            continue


def create_blank_backend(spec: PaperSpec, path: str) -> PdfBackend:
    """Build an in-memory one-page blank document for a paper spec.

    The template pattern is drawn into the page content at creation
    time (vector, not a viewer overlay), so every consumer — render
    pipeline, thumbnails, the saved PDF — sees the same paper. Raises
    ``InvalidPdfError`` if the page cannot be built.
    """
    spec = spec.normalized()
    width, height = page_size_pt(spec)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        # Paper background covers the whole page (some viewers render a
        # transparent PDF page as white regardless of theme).
        page.draw_rect(pymupdf.Rect(0.0, 0.0, width, height),
                       color=None, fill=_hex_rgb(spec.paper_color),
                       width=0, fill_opacity=1.0)
        _draw_template_items(page, pattern_primitives(spec),
                             spec.pattern_color)
        try:
            doc.set_metadata({
                "creator": "Inkfall",
                "producer": "Inkfall",
                "title": "Untitled",
                "keywords": paper_metadata_value(spec),
            })
        except Exception:
            pass
        return PdfBackend(doc, path, DocumentId())
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        if isinstance(e, InvalidPdfError):
            raise
        raise InvalidPdfError(
            f"Cannot create blank document: {e}") from e


def render_page_preview(
    path: str, page_index: int = 0, max_side_px: int = 160
) -> tuple[bytes, int, int, int] | None:
    """Render one page as a small RGBA preview (page navigator).

    The :func:`render_cover_preview` contract generalized to any page:
    runs on a thumbnail worker thread with its own short-lived
    ``pymupdf`` handle — never touches ``RendererEngine``,
    ``ProcessDecoder``, the viewer pipeline, or the GUI-owned
    ``PdfBackend``. Returns ``(rgba, width, height, stride)`` or ``None``
    on any failure (missing/encrypted/invalid file, bad page index).
    """
    try:
        if not os.path.isfile(path):
            return None
        doc = pymupdf.open(path)
    except Exception:
        return None
    try:
        if getattr(doc, "needs_pass", False):
            return None
        try:
            page_count = len(doc)
        except Exception:
            page_count = 0
        if page_count < 1:
            return None
        index = int(page_index)
        if not 0 <= index < page_count:
            return None
        page = doc[index]
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        scale = min(
            float(max_side_px) / float(rect.width),
            float(max_side_px) / float(rect.height),
            2.0,
        )
        if scale <= 0:
            return None
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        try:
            data = bytes(pix.samples)
            return data, int(pix.width), int(pix.height), int(pix.stride)
        finally:
            pix = None  # type: ignore[assignment]
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def render_cover_preview(
    path: str, max_side_px: int = 256
) -> tuple[bytes, int, int, int, int] | None:
    """Render a small first-page RGBA preview (M4 library thumbnails).

    Runs on a thumbnail worker thread with its own short-lived handle —
    never touches ``RendererEngine``, ``ProcessDecoder`` or the viewer
    pipeline. Returns ``(rgba, width, height, stride, page_count)`` or
    ``None`` on any failure (missing/encrypted/invalid file).
    """
    try:
        if not os.path.isfile(path):
            return None
        doc = pymupdf.open(path)
    except Exception:
        return None
    try:
        if getattr(doc, "needs_pass", False):
            return None
        try:
            page_count = len(doc)
        except Exception:
            page_count = 0
        if page_count < 1:
            return None
        page = doc[0]
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        scale = min(
            float(max_side_px) / float(rect.width),
            float(max_side_px) / float(rect.height),
            2.0,
        )
        if scale <= 0:
            return None
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        try:
            data = bytes(pix.samples)
            return data, int(pix.width), int(pix.height), int(pix.stride), int(page_count)
        finally:
            pix = None  # type: ignore[assignment]
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass
