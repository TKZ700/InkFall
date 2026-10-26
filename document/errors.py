"""Document-level errors (M1).

All derive from :class:`core.errors.DocumentError` so callers can catch
either the specific failure or the whole document family. Backend
exceptions are translated here; raw PyMuPDF errors never cross the
backend boundary (original kept as ``__cause__``).
"""

from core.errors import DocumentError


class DocumentNotFoundError(DocumentError):
    """File does not exist or is not readable as a path."""


class InvalidPdfError(DocumentError):
    """File exists but is not a valid PDF (or I/O failed while opening)."""


class EncryptedPdfError(DocumentError):
    """PDF requires a password and none (or no valid one) was supplied."""


class WrongPasswordError(EncryptedPdfError):
    """A password was supplied but authentication failed."""


class DocumentClosedError(DocumentError):
    """Operation attempted on a closed document/backend."""


class ReadOnlyDocumentError(DocumentError):
    """A mutating operation was attempted on a read-only session.

    Read-only sessions (an explicitly requested duplicate view of a file
    that is already open) can navigate and render but never write: page
    mutations, annotation saves, and exports are refused here, at the
    document boundary — not merely in the UI.
    """


class InvalidPageIndexError(DocumentError):
    """Page index out of range for the open document."""
