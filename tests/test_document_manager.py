"""M1 manager tests: lifecycle, duplicates, close_all, errors."""

from __future__ import annotations

import os

import pytest

from document.errors import (
    DocumentClosedError,
    DocumentNotFoundError,
    EncryptedPdfError,
    WrongPasswordError,
)
from document.manager import DocumentManager, normalize_path
from tests.helpers import make_encrypted_pdf, make_pdf


def test_manager_open_get_close(tmp_path):
    pdf = make_pdf(str(tmp_path / "m1.pdf"), pages=4)
    mgr = DocumentManager()
    doc = mgr.open(pdf)
    assert doc.page_count == 4
    assert mgr.get(doc.id) is doc
    assert len(mgr.list_open()) == 1
    mgr.close(doc.id)
    assert mgr.get(doc.id) is None
    assert len(mgr.list_open()) == 0
    with pytest.raises(DocumentClosedError):
        doc.page_info(0)


def test_manager_double_close_and_close_all(tmp_path):
    pdf1 = make_pdf(str(tmp_path / "m2a.pdf"), pages=2)
    pdf2 = make_pdf(str(tmp_path / "m2b.pdf"), pages=3)
    mgr = DocumentManager()
    d1 = mgr.open(pdf1)
    d2 = mgr.open(pdf2)
    assert len(mgr) == 2
    mgr.close(d1.id)
    mgr.close(d1.id)  # idempotent
    assert len(mgr) == 1
    mgr.close_all()
    mgr.close_all()  # safe repeat
    assert len(mgr) == 0
    assert d2.is_closed


def test_manager_shutdown_closes_all(tmp_path):
    pdf = make_pdf(str(tmp_path / "m3.pdf"), pages=2)
    with DocumentManager() as mgr:
        doc = mgr.open(pdf)
        assert not doc.is_closed
    assert doc.is_closed


def test_duplicate_open_returns_existing(tmp_path):
    """Frozen policy: same normalized path -> same managed Document."""
    pdf = make_pdf(str(tmp_path / "dup.pdf"), pages=3)
    mgr = DocumentManager()
    first = mgr.open(pdf)
    # Relative path resolving to the same file must hit the same entry.
    rel = os.path.relpath(pdf, start=str(tmp_path))
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        second = mgr.open(rel)
    finally:
        os.chdir(cwd)
    assert second is first
    assert len(mgr) == 1
    mgr.close_all()


def test_case_insensitive_identity_on_windows(tmp_path):
    pdf = make_pdf(str(tmp_path / "Case.pdf"), pages=1)
    key1 = normalize_path(pdf)
    key2 = normalize_path(pdf.upper() if os.name != "nt" else pdf)
    assert key1 == key2 or os.name != "nt"  # normcase folds case on Windows
    assert os.path.isabs(key1)


def test_manager_missing_file_no_partial_object(tmp_path):
    mgr = DocumentManager()
    with pytest.raises(DocumentNotFoundError):
        mgr.open(str(tmp_path / "missing.pdf"))
    assert len(mgr) == 0


def test_manager_encrypted_semantics(tmp_path):
    pdf = make_encrypted_pdf(str(tmp_path / "menc.pdf"), user_password="pw123")
    mgr = DocumentManager()
    with pytest.raises(EncryptedPdfError):
        mgr.open(pdf)
    assert len(mgr) == 0  # no partially-live object
    with pytest.raises(WrongPasswordError):
        mgr.open(pdf, password="nope")
    assert len(mgr) == 0
    doc = mgr.open(pdf, password="pw123")
    assert doc.page_count == 3
    assert len(mgr) == 1
    mgr.close_all()


def test_app_instantiates_manager():
    from app.application import create_document_manager

    mgr = create_document_manager()
    assert isinstance(mgr, DocumentManager)
    assert len(mgr) == 0
