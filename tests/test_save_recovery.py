"""Save-failure recovery regression tests (release issue 5).

When an ordinary Save fails because the destination cannot be written
(file lock / sharing violation / unwritable location), Inkfall offers a
Save As picker that saves the current edited document as a copy. Only
classified destination failures (:class:`SaveLockedError`) trigger the
flow — serialization failures, memory exhaustion, and invalid documents
must never be disguised as file locks.

All failures are simulated deterministically by monkeypatching; no real
files are locked and no real documents are overwritten. Qt runs offscreen
(pytest-qt) and the file picker is monkeypatched.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
import pymupdf

from document.backend import PdfBackend, SaveLockedError
from document.document import Document
from document.errors import InvalidPdfError

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def shell(qtbot, monkeypatch, tmp_path):
    """A MainWindow shell with its own library store (m465 pattern).

    Defuses ``QMessageBox.exec`` for the test duration: pytest-qt closes
    widgets in a teardown hook that runs BEFORE fixture finalizers, so a
    dirty document would otherwise block teardown forever on the real
    "Unsaved changes" modal. The patch auto-presses the escape button and
    is undone after each test.
    """
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.main_window import MainWindow
    from document.manager import DocumentManager
    from library.service import LibraryService
    from library.store import LibraryStore

    def _exec_never_blocks(self):
        button = self.escapeButton() or self.defaultButton()
        if button is not None:
            button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _exec_never_blocks)

    app = QApplication.instance()
    if app is not None:
        app.setOrganizationName("Inkfall")
        app.setApplicationName("Inkfall")
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.show()
    qtbot.addWidget(window)
    yield window
    # Belt and braces: discard every document before the store closes.
    try:
        window._viewer.close_document()
    except Exception:
        pass
    try:
        for doc in list(window._manager.documents()):
            try:
                window._manager.close(doc.id)
            except Exception:
                pass
    except Exception:
        pass
    store.close()


def _open_document(window, tmp_path: Path, name: str = "book.pdf"):
    """Open a synthetic two-page PDF in the window's viewer; return path."""
    from tests.helpers import make_pdf

    pdf = make_pdf(str(tmp_path / name), pages=2)
    assert window._viewer.open_document(pdf) is True
    return pdf


def _commit_stroke(doc) -> None:
    """Commit one pen stroke on page 0 and mark the document dirty."""
    from annotations.models import InkStroke
    from core.types import Point
    from document.models import page_id_for

    doc.annotations.add(
        InkStroke(points=(Point(100.0, 100.0), Point(140.0, 140.0)),
                  page_id=page_id_for(doc.id, 0)),
        0)
    doc.mark_changed()


def _lock_targets(*targets):
    """_replace_target stand-in: raises SaveLockedError for the given
    destinations, passes everything else through to os.replace."""
    import os

    locked = {Path(str(t)) for t in targets}

    def _replace(temp_path: str, target_abs: str) -> None:
        if Path(target_abs) in locked:
            raise SaveLockedError("destination in use (simulated)")
        os.replace(temp_path, target_abs)

    return _replace


# ---------------------------------------------------------------------------
# classification (backend level)
# ---------------------------------------------------------------------------


def test_unwritable_destination_classifies_as_locked(
        tmp_path: Path, monkeypatch) -> None:
    """Unwritable destinations raise SaveLockedError, not generic errors.

    Covers the destination-prep steps (temp-file creation, directory
    creation) via platform errnos, without touching the real filesystem.
    """
    from document.backend import PdfBackend
    from tests.helpers import make_pdf

    import document.backend as backend_module

    pdf = make_pdf(str(tmp_path / "doc.pdf"), pages=1)
    doc = Document(
        PdfBackend.open(pdf), pdf)

    real_mkstemp = backend_module.tempfile.mkstemp

    def _denied_mkstemp(*args, **kwargs):
        if kwargs.get("prefix") == ".inkfall-save-":
            raise OSError(errno.EACCES, "simulated EACCES")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(backend_module.tempfile, "mkstemp",
                        _denied_mkstemp)
    with pytest.raises(SaveLockedError):
        PdfBackend.export_pdf(
            doc._backend, str(tmp_path / "out.pdf"), [], b"{}", 1)
    monkeypatch.undo()

    def _readonly_makedirs(path, exist_ok=False):
        raise OSError(errno.EROFS, "simulated read-only filesystem")

    monkeypatch.setattr(backend_module.os, "makedirs", _readonly_makedirs)
    with pytest.raises(SaveLockedError):
        PdfBackend.export_pdf(
            doc._backend, str(tmp_path / "out.pdf"), [], b"{}", 1)
    monkeypatch.undo()
    doc.close()


def test_memory_exhaustion_is_not_classified_as_a_lock(
        tmp_path: Path, monkeypatch) -> None:
    """A MemoryError during snapshot stays a non-recoverable failure."""
    import pymupdf as _pymupdf

    from document.backend import PdfBackend
    from tests.helpers import make_pdf

    import document.backend as backend_module

    pdf = make_pdf(str(tmp_path / "doc.pdf"), pages=1)
    doc = Document(PdfBackend.open(pdf), pdf)

    def _oom(_self, _path, **_kwargs):
        raise MemoryError("simulated exhaustion")

    monkeypatch.setattr(_pymupdf.Document, "save", _oom)
    with pytest.raises(InvalidPdfError) as excinfo:
        PdfBackend.export_pdf(
            doc._backend, str(tmp_path / "out.pdf"), [], b"{}", 1)
    assert not isinstance(excinfo.value, SaveLockedError)
    monkeypatch.undo()
    doc.close()


def test_serialization_failure_is_not_classified_as_a_lock(
        tmp_path: Path, monkeypatch) -> None:
    """A manifest-attachment failure never raises SaveLockedError."""
    from document.backend import PdfBackend
    from tests.helpers import make_pdf

    import document.backend as backend_module

    pdf = make_pdf(str(tmp_path / "doc.pdf"), pages=1)
    doc = Document(PdfBackend.open(pdf), pdf)

    def _boom(_holder, _payload):
        raise RuntimeError("simulated embfile failure")

    monkeypatch.setattr(
        backend_module, "_write_manifest_attachment", _boom)
    with pytest.raises(InvalidPdfError) as excinfo:
        PdfBackend.export_pdf(
            doc._backend, str(tmp_path / "out.pdf"), [], b"{}", 1)
    assert not isinstance(excinfo.value, SaveLockedError)
    monkeypatch.undo()
    doc.close()


# ---------------------------------------------------------------------------
# recovery flow (MainWindow level)
# ---------------------------------------------------------------------------


def test_save_lock_offers_save_as_and_copy_preserves_edits(
        shell, tmp_path: Path, monkeypatch) -> None:
    """A simulated file lock offers Save As; the copy keeps all edits."""
    pdf = _open_document(shell, tmp_path)
    original_bytes = Path(pdf).read_bytes()
    doc = shell._viewer.current_document
    _commit_stroke(doc)

    monkeypatch.setattr(
        "document.backend._replace_target", _lock_targets(pdf))

    picks = []

    def _picker(_parent, caption, suggested, _filter):
        picks.append((caption, suggested))
        return str(tmp_path / "book-copy.pdf"), "PDF files (*.pdf)"

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName", _picker)

    assert shell._save_current() is True

    # The recovery picker was offered with a derived non-overwriting name.
    assert picks, "Save As recovery picker was not offered"
    caption, suggested = picks[0]
    assert "copy" in caption.lower()
    assert suggested.replace("\\", "/").endswith("book-copy.pdf")

    # The copy exists with the committed annotation; the original is
    # byte-identical (never overwritten).
    copy_path = tmp_path / "book-copy.pdf"
    assert copy_path.is_file()
    check = pymupdf.open(str(copy_path))
    try:
        assert len(check) == 2
        assert check[0].first_annot is not None
    finally:
        check.close()
    assert Path(pdf).read_bytes() == original_bytes

    # Save As semantics: the session is re-pointed at the copy and clean.
    assert Path(doc.save_target) == copy_path
    assert not doc.is_dirty


def test_cancelled_recovery_preserves_edits_and_reports_no_success(
        shell, tmp_path: Path, monkeypatch) -> None:
    """Cancelling the picker keeps the document dirty; no success report."""
    pdf = _open_document(shell, tmp_path)
    original_bytes = Path(pdf).read_bytes()
    doc = shell._viewer.current_document
    doc.mark_changed()

    monkeypatch.setattr(
        "document.backend._replace_target",
        lambda _t, _g: (_ for _ in ()).throw(
            SaveLockedError("destination in use (simulated)")))
    picker_calls = []

    def _picker(_parent, caption, suggested, _filter):
        picker_calls.append((caption, suggested))
        return "", ""  # user cancels

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName", _picker)

    assert shell._save_current() is False
    assert len(picker_calls) == 1, "recovery picker must be offered once"
    caption, suggested = picker_calls[0]
    assert suggested.replace("\\", "/").endswith("book-copy.pdf")
    assert doc.is_dirty, "cancelled recovery must keep the unsaved state"
    assert doc.is_closed is False
    assert Path(pdf).read_bytes() == original_bytes


def test_failed_copy_save_preserves_state_without_further_picker(
        shell, tmp_path: Path, monkeypatch) -> None:
    """A failing copy save reports once; the session stays recoverable."""
    pdf = _open_document(shell, tmp_path)
    original_bytes = Path(pdf).read_bytes()
    doc = shell._viewer.current_document
    _commit_stroke(doc)

    monkeypatch.setattr(
        "document.backend._replace_target",
        _lock_targets(pdf, tmp_path / "also-locked.pdf"))
    picker_calls = []

    def _picker(_parent, caption, suggested, _filter):
        picker_calls.append(caption)
        # The user picks a destination that is ALSO locked.
        return str(tmp_path / "also-locked.pdf"), "PDF files (*.pdf)"

    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName", _picker)

    assert shell._save_current() is False
    assert len(picker_calls) == 1, (
        "a failing copy save must not loop back into the recovery picker")
    assert doc.is_dirty
    assert not (tmp_path / "also-locked.pdf").exists()
    assert Path(pdf).read_bytes() == original_bytes

    # The user can still retry with a working destination.
    monkeypatch.undo()
    assert shell._save_current() is True
    assert not doc.is_dirty


def test_serialization_failure_does_not_offer_recovery(
        shell, tmp_path: Path, monkeypatch) -> None:
    """InvalidPdfError from serialization never opens the recovery picker."""
    from document.backend import PdfBackend

    _open_document(shell, tmp_path)
    doc = shell._viewer.current_document
    doc.mark_changed()

    def _boom(_self, _target):
        raise InvalidPdfError("simulated serialization failure")

    monkeypatch.setattr(PdfBackend, "export_pdf", _boom)
    picker_calls = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *_a, **_k: picker_calls.append("opened") or ("", ""))

    assert shell._save_current() is False
    assert not picker_calls, (
        "serialization failures must not offer the Save As recovery")
    assert doc.is_dirty


def test_readonly_session_never_reaches_recovery(
        shell, tmp_path: Path, monkeypatch) -> None:
    """Read-only duplicate sessions keep their save refusal, no picker."""
    _open_document(shell, tmp_path)
    ro = shell._manager.open_readonly(
        str(shell._viewer.current_document.normalized_path))
    picker_calls = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QFileDialog.getSaveFileName",
        lambda *_a, **_k: picker_calls.append("opened") or ("", ""))

    # The active document is the primary (editable) session; the read-only
    # duplicate itself refuses every write at the document boundary.
    from document.errors import ReadOnlyDocumentError

    with pytest.raises(ReadOnlyDocumentError):
        ro.write_export(str(tmp_path / "elsewhere.pdf"))
    assert not picker_calls
    shell._manager.close(ro.id)
