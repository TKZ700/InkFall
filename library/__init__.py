"""Inkfall library package (M4).

Qt-free application-level abstraction over documents-as-files.

Layers::

    Library metadata (this package)
        -> DocumentManager (document/manager.py)
            -> Document (document/document.py)
                -> PdfBackend (document/backend.py)

The library NEVER opens or renders PDFs to populate itself; it stores
filesystem metadata + user state (folders, favorites, recency) only.
"""

from library.errors import LibraryError, LibraryNotFoundError
from library.models import LibraryFolder, LibraryItem
from library.service import LibraryService, SearchQuery, SortKey, SortOrder
from library.store import LibraryStore

__all__ = [
    "LibraryError",
    "LibraryFolder",
    "LibraryItem",
    "LibraryNotFoundError",
    "LibraryService",
    "LibraryStore",
    "SearchQuery",
    "SortKey",
    "SortOrder",
]
