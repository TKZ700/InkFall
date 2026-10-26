"""Document/page data models (M1).

Pure data. No PyMuPDF here — the backend maps onto these models.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.types import DocumentId, PageId, Size


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Basic PDF metadata in an Inkfall-owned representation.

    Missing values are ``None``. No transformation beyond copying the
    backend's string values.
    """

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    keywords: str | None = None
    creator: str | None = None
    producer: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None
    is_encrypted: bool = False


def page_id_for(document_id: DocumentId, index: int) -> PageId:
    """Derive a deterministic page ID for (document, index).

    Repeated ``page_info(i)`` calls for the same document return the same
    page ID, which the laziness/stability tests rely on.
    """
    return PageId(f"{document_id.value}:p{index}")


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """Metadata describing an open document."""

    id: DocumentId = field(default_factory=DocumentId)
    title: str = "Untitled"
    page_count: int = 0
    path: str | None = None
    metadata: DocumentMetadata | None = None
    is_encrypted: bool = False

    def __post_init__(self) -> None:
        if self.page_count < 0:
            raise ValueError("page_count must be non-negative")


@dataclass(frozen=True, slots=True)
class PageInfo:
    """Lazy page description: geometry + rotation only.

    No pixmaps, no text. Obtained on demand via the backend.
    """

    id: PageId = field(default_factory=PageId)
    document_id: DocumentId = field(default_factory=DocumentId)
    index: int = 0
    size: Size = field(default_factory=lambda: Size(595.0, 842.0))  # A4 @ 72dpi
    rotation: int = 0

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("page index must be non-negative")

    @property
    def width(self) -> float:
        """Width in points as reported by the backend.

        These are PyMuPDF ``page.rect`` dimensions, i.e. the media box
        already composed with ``/Rotate``: for 90/270-rotated pages the
        width/height are swapped relative to the unrotated media box.
        Consumers needing unrotated geometry must derotate (see
        ``rendering.layout.unrotated_size``).
        """
        return self.size.width

    @property
    def height(self) -> float:
        """Height in points as reported by the backend (see :attr:`width`)."""
        return self.size.height
