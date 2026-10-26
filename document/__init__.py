"""Document package: lifecycle, backend, models, errors.

Only ``document/backend.py`` may import PyMuPDF. Everything else speaks
Inkfall models.
"""

from document.backend import PdfBackend
from document.document import Document
from document.errors import (
    DocumentClosedError,
    DocumentNotFoundError,
    EncryptedPdfError,
    InvalidPageIndexError,
    InvalidPdfError,
    WrongPasswordError,
)
from document.manager import DocumentManager, normalize_path
from document.models import DocumentInfo, DocumentMetadata, PageInfo, page_id_for

__all__ = [
    "Document",
    "DocumentClosedError",
    "DocumentInfo",
    "DocumentManager",
    "DocumentMetadata",
    "DocumentNotFoundError",
    "EncryptedPdfError",
    "InvalidPageIndexError",
    "InvalidPdfError",
    "PageInfo",
    "PdfBackend",
    "WrongPasswordError",
    "normalize_path",
    "page_id_for",
]
