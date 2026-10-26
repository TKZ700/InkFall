"""Document model tests."""

from core.types import DocumentId, Size
from document.models import DocumentInfo, PageInfo


def test_document_model_instantiation():
    doc = DocumentInfo(title="Sample", page_count=3)
    assert doc.title == "Sample"
    assert doc.page_count == 3
    assert isinstance(doc.id, DocumentId)


def test_page_model_instantiation():
    doc = DocumentInfo(title="Sample", page_count=1)
    page = PageInfo(document_id=doc.id, index=0, size=Size(595.0, 842.0))
    assert page.document_id == doc.id
    assert page.index == 0
    assert page.size.width == 595.0
