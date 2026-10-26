"""M1 backend tests: open, pages, errors, encryption, laziness."""

from __future__ import annotations

import os

import pytest

from document.backend import PdfBackend
from document.errors import (
    DocumentClosedError,
    DocumentNotFoundError,
    EncryptedPdfError,
    InvalidPageIndexError,
    InvalidPdfError,
    WrongPasswordError,
)
from tests.helpers import make_encrypted_pdf, make_invalid_pdf, make_pdf


def test_valid_pdf_opens_page_count_metadata(tmp_path):
    pdf = make_pdf(str(tmp_path / "a.pdf"), pages=5)
    backend = PdfBackend.open(pdf)
    try:
        assert backend.page_count == 5
        meta = backend.metadata
        assert meta.title == "Inkfall Test"
        assert meta.author == "Inkfall"
        assert backend.is_encrypted is False
        assert backend.needs_password is False
    finally:
        backend.close()


def test_path_is_preserved_and_normalized_by_manager(tmp_path):
    from document.manager import normalize_path

    pdf = make_pdf(str(tmp_path / "b.pdf"), pages=2)
    key = normalize_path(pdf)
    assert os.path.isabs(key)


def test_page_zero_middle_last_geometry(tmp_path):
    pdf = make_pdf(str(tmp_path / "c.pdf"), pages=7)
    backend = PdfBackend.open(pdf)
    try:
        first = backend.page_info(0)
        mid = backend.page_info(3)
        last = backend.page_info(6)
        assert (first.width, first.height) == (595.0, 842.0)
        assert mid.index == 3 and last.index == 6
        assert first.rotation == 0
        # Deterministic IDs: same (doc, index) -> same page ID.
        assert backend.page_info(0).id == first.id
        assert backend.page_info(6).id == last.id
        assert first.document_id == backend.document_id
    finally:
        backend.close()


@pytest.mark.parametrize("bad_index", [-1, 7, 100])
def test_invalid_page_index(tmp_path, bad_index):
    pdf = make_pdf(str(tmp_path / "d.pdf"), pages=7)
    backend = PdfBackend.open(pdf)
    try:
        with pytest.raises(InvalidPageIndexError):
            backend.page_info(bad_index)
    finally:
        backend.close()


def test_close_double_close_and_access_after_close(tmp_path):
    pdf = make_pdf(str(tmp_path / "e.pdf"), pages=2)
    backend = PdfBackend.open(pdf)
    backend.close()
    backend.close()  # safe repeat
    assert backend.is_closed
    with pytest.raises(DocumentClosedError):
        _ = backend.page_count
    with pytest.raises(DocumentClosedError):
        backend.page_info(0)
    with pytest.raises(DocumentClosedError):
        _ = backend.metadata


def test_missing_file(tmp_path):
    with pytest.raises(DocumentNotFoundError):
        PdfBackend.open(str(tmp_path / "nope.pdf"))


def test_invalid_pdf(tmp_path):
    pdf = make_invalid_pdf(str(tmp_path / "bad.pdf"))
    with pytest.raises(InvalidPdfError):
        PdfBackend.open(pdf)


def test_encrypted_requires_password_wrong_and_correct(tmp_path):
    pdf = make_encrypted_pdf(str(tmp_path / "enc.pdf"), pages=3, user_password="s3cret")
    with pytest.raises(EncryptedPdfError):
        PdfBackend.open(pdf)
    with pytest.raises(WrongPasswordError):
        PdfBackend.open(pdf, password="wrong")
    backend = PdfBackend.open(pdf, password="s3cret")
    try:
        assert backend.page_count == 3
        assert backend.is_encrypted is True
    finally:
        backend.close()


def test_open_does_not_process_pages(tmp_path):
    """Lazy open: no page_info calls during open, no raster/text APIs used.

    Uses backend-level instrumentation only (counts our own page_info
    calls) plus a static check that the backend never invokes pixmap or
    text-extraction methods. No private PyMuPDF internals are touched.
    """
    import document.backend as backend_mod

    calls = []
    orig = PdfBackend.page_info

    def spy(self, index):
        calls.append(index)
        return orig(self, index)

    pdf = make_pdf(str(tmp_path / "lazy.pdf"), pages=60)
    PdfBackend.page_info = spy  # type: ignore[method-assign]
    try:
        backend = PdfBackend.open(pdf)
        try:
            assert calls == [], "open() must not resolve any page"
            assert backend.page_count == 60
            assert calls == [], "page_count must not resolve any page"
            _ = backend.metadata
            assert calls == [], "metadata must not resolve any page"
            info = backend.page_info(10)
            assert calls == [10]
            assert info.index == 10
        finally:
            backend.close()
    finally:
        PdfBackend.page_info = orig  # type: ignore[method-assign]

    source = open(os.path.join(os.path.dirname(backend_mod.__file__), "backend.py")).read()
    # Structural laziness guard: the PdfBackend document-handle path
    # (open/page_count/metadata/page_info) must never rasterize or extract
    # text. Scoped to the class body so the opt-in M4 thumbnail helper
    # (render_cover_preview: own short-lived handle, thumbnail worker only)
    # does not trip it — open() laziness itself is asserted at runtime above.
    import ast as _ast

    tree = _ast.parse(source)
    class_source = ""
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name == "PdfBackend":
            class_source = _ast.get_source_segment(source, node) or ""
            break
    assert class_source, "PdfBackend class must exist"
    for forbidden in ("get_pixmap", "get_textpage", "get_text(", "get_textbox", "tobytes"):
        assert forbidden not in class_source, f"PdfBackend must not call {forbidden}"
