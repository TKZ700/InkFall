"""Large-PDF save regression tests (release issue: saving large PDFs crashed).

Root cause addressed by ``PdfBackend.export_pdf``'s disk-backed snapshot:
the previous ``bytes(tobytes())`` snapshot held two to three full document
copies in RAM simultaneously (snapshot bytes, in-memory annotation holder,
same-path replacement handle), exhausting memory on large PDFs. The save
now writes the snapshot to a private temp file and opens file-backed
handles from it, so peak memory stays bounded regardless of document size.

These tests pin:
- No full-document ``tobytes()`` materialization during save (the exact
  regression).
- Many-page and image-heavy documents save and reopen correctly.
- Committed annotations survive the round trip (manifest + native inks).
- Save As to a new destination leaves the original untouched.
- A failed final replacement preserves the original file, the live
  session, and every edit; no snapshot/temp files leak (including across
  consecutive same-path saves).
- The process-backed save reuses the newest render work copy as its
  export input (no second full serialization of identical content) and
  pins the borrowed file's lifecycle: pending-snapshot protection against
  work-copy pruning, Save-As fallback to a fresh snapshot, and failed
  same-path recovery.
- Ordinary save and read-only refusal behavior are unchanged.

All PDFs are synthetic, generated under ``tmp_path``; nothing is committed
to the repository. No Qt is needed (document layer only).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import pymupdf

from annotations.models import InkStroke
from core.types import Point
from document.backend import PdfBackend, SaveLockedError
from document.document import Document
from document.errors import ReadOnlyDocumentError
from document.manager import DocumentManager
from document.models import page_id_for
from tests.helpers import make_pdf

REPO_ROOT = Path(__file__).resolve().parents[1]


def _snapshots_in_temp() -> set[str]:
    """Existing inkfall snapshot temp files (baseline for leak checks)."""
    return set(
        glob.glob(os.path.join(os.environ.get("TEMP", "/tmp"),
                               "inkfall-snapshot-*.pdf")))


def _open_doc(path) -> Document:
    return Document(PdfBackend.open(str(path)), str(path))


def _seed_stroke(doc: Document, page_index: int, x: float = 100.0) -> None:
    doc.annotations.add(
        InkStroke(points=(Point(x, 100.0), Point(x + 40.0, 140.0)),
                  page_id=page_id_for(doc.id, page_index)),
        page_index)


# ---------------------------------------------------------------------------
# the root-cause regression: no bytes snapshot during save
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("same_path", [True, False],
                         ids=["same-path", "save-as"])
def test_save_never_materializes_a_bytes_snapshot(
        tmp_path: Path, monkeypatch, same_path: bool) -> None:
    """The save path must not duplicate the document as Python bytes.

    The crash on large PDFs came from holding ``tobytes()`` snapshots in
    RAM. Any call on the live document during save (unencrypted source)
    is the regression reappearing. Applies to same-path saves and Save As.
    """
    pdf = make_pdf(str(tmp_path / "doc.pdf"), pages=3)
    doc = _open_doc(pdf)
    _seed_stroke(doc, 0)
    target = pdf if same_path else str(tmp_path / "copy.pdf")

    import document.backend as backend_module

    def _forbidden(self, *args, **kwargs):
        raise AssertionError(
            "save materialized a full-document bytes snapshot "
            "(tobytes) — the large-PDF crash regression")

    monkeypatch.setattr(
        backend_module.pymupdf.Document, "tobytes", _forbidden)
    try:
        manager = DocumentManager()
        manager.save_document(doc, target)
    finally:
        monkeypatch.undo()
    assert Path(target).is_file()
    doc.close()


# ---------------------------------------------------------------------------
# large documents
# ---------------------------------------------------------------------------


def test_many_page_document_saves_and_reopens(tmp_path: Path) -> None:
    """A many-page document saves same-path and reopens intact."""
    pages = 500
    pdf = make_pdf(str(tmp_path / "many.pdf"), pages=pages)
    baseline = _snapshots_in_temp()

    doc = _open_doc(pdf)
    _seed_stroke(doc, pages - 1)
    manager = DocumentManager()
    manager.save_document(doc, pdf)

    reopened = pymupdf.open(pdf)
    try:
        assert len(reopened) == pages
        assert "Page 1 of" in reopened[0].get_text()
        assert reopened[pages - 1].first_annot is not None, (
            "committed ink must be a native annotation on the last page")
    finally:
        reopened.close()
    doc.close()
    assert not (_snapshots_in_temp() - baseline), (
        "snapshot temp files leaked after close")


#: Child process for the image-heavy save: builds a PDF whose embedded
#: images make the on-disk document far larger than any sane RAM budget
#: for a *copy* of it, saves it through the real Document/manager path,
#: and reports the process peak RSS. Everything happens in the child so
#: the pytest process itself never holds the document.
_MEMORY_CHILD = r"""
import ctypes, json, os, sys

import pymupdf

pdf_path, = sys.argv[1:2]
noise_pages = 3
W, H = 3000, 4000  # ~36 MB of incompressible RGB per page


def peak_rss_mb() -> float:
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.wintypes.DWORD),
                        ("PageFaultCount", ctypes.wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        for getter in (ctypes.windll.kernel32.K32GetProcessMemoryInfo,
                       ctypes.windll.psapi.GetProcessMemoryInfo):
            getter.argtypes = [ctypes.wintypes.HANDLE,
                               ctypes.POINTER(PMC), ctypes.wintypes.DWORD]
            getter.restype = ctypes.wintypes.BOOL
            if getter(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.PeakWorkingSetSize / (1024 * 1024)
        raise RuntimeError("GetProcessMemoryInfo failed")
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


doc = pymupdf.open()
for i in range(noise_pages):
    page = doc.new_page(width=595.0, height=842.0)
    samples = os.urandom(W * H * 3)
    pix = pymupdf.Pixmap(pymupdf.csRGB, W, H, samples, False)
    page.insert_image(page.rect, pixmap=pix)
    pix = None
    samples = None
doc.save(pdf_path)
doc.close()

from document.manager import DocumentManager

manager = DocumentManager()
document = manager.open(pdf_path)
pre_save_peak_mb = peak_rss_mb()
manager.save_document(document, pdf_path)
document.close()

check = pymupdf.open(pdf_path)
pages = len(check)
check.close()

print(json.dumps({
    "pdf_mb": round(os.path.getsize(pdf_path) / (1024 * 1024), 1),
    "pre_save_peak_mb": round(pre_save_peak_mb, 1),
    "peak_rss_mb": round(peak_rss_mb(), 1),
    "pages": pages,
}))
"""


def test_large_image_pdf_save_succeeds_with_bounded_memory(
        tmp_path: Path) -> None:
    """An image-heavy PDF saves through the real path without exploding RAM.

    The child builds a PDF dominated by a large embedded image, saves it
    same-path via ``DocumentManager.save_document`` and reports its peak
    RSS. The bound allows the document itself plus generous interpreter
    and MuPDF cache overhead — but NOT a second full-document bytes copy,
    which is the crash regression this guards against. The bound is
    deliberately loose (measured peak stays far below it); its purpose is
    to fail on the 2-3x duplication, not to pin exact usage.
    """
    pdf = str(tmp_path / "heavy.pdf")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _MEMORY_CHILD, pdf],
        capture_output=True, text=True, timeout=580, cwd=str(REPO_ROOT),
        env=env)
    assert result.returncode == 0, (
        f"child save failed:\n{result.stdout}\n{result.stderr}")
    stats = json.loads(result.stdout.strip().splitlines()[-1])

    assert stats["pages"] == 3
    assert stats["pdf_mb"] >= 20, (
        f"fixture too small to exercise memory bounds ({stats['pdf_mb']} MB)")
    # The save-phase memory delta (peak RSS growth across
    # DocumentManager.save_document) is the discriminating measurement.
    # Measured on this issue's evidence run for a 103 MB document:
    # old bytes(tobytes()) snapshot style added 240.7 MB (~2.3x the
    # document); the disk-backed save adds 69.3 MB (~0.7x). The bound
    # (1.5x document + 50 MB slack) passes the disk-backed behavior with
    # ~3x headroom and fails the removed bytes-snapshot implementation
    # decisively. It scales with document size instead of pinning a
    # machine-specific constant.
    save_delta_mb = stats["peak_rss_mb"] - stats["pre_save_peak_mb"]
    bound_mb = stats["pdf_mb"] * 1.5 + 50.0
    assert save_delta_mb <= bound_mb, (
        f"save added {save_delta_mb:.1f} MB of peak RSS for a "
        f"{stats['pdf_mb']} MB document (bound {bound_mb:.1f} MB) — "
        "full-document duplication returned?")


# ---------------------------------------------------------------------------
# annotation round trip, Save As, failure handling
# ---------------------------------------------------------------------------


def test_committed_annotations_round_trip_through_save(tmp_path: Path) -> None:
    """Strokes committed before a save are restored when the file reopens."""
    pdf = make_pdf(str(tmp_path / "inked.pdf"), pages=4)
    doc = _open_doc(pdf)
    _seed_stroke(doc, 0, x=80.0)
    _seed_stroke(doc, 2, x=200.0)
    manager = DocumentManager()
    manager.save_document(doc, pdf)

    reopened_doc = manager.open(pdf)
    try:
        strokes = [a for _page, a in reopened_doc.annotations.iter_all()
                   if isinstance(a, InkStroke)]
        assert len(strokes) == 2, "manifest must restore both committed inks"
    finally:
        reopened_doc.close()
        doc.close()


def test_save_as_to_new_destination_preserves_original(tmp_path: Path) -> None:
    """Save As writes the copy; the original file is untouched."""
    pdf = make_pdf(str(tmp_path / "original.pdf"), pages=2)
    original_bytes = Path(pdf).read_bytes()
    baseline = _snapshots_in_temp()

    doc = _open_doc(pdf)
    _seed_stroke(doc, 0)
    manager = DocumentManager()
    copy_path = str(tmp_path / "saved-copy.pdf")
    normalized = manager.save_document(doc, copy_path)

    assert Path(copy_path).is_file()
    assert Path(pdf).read_bytes() == original_bytes, (
        "Save As must never modify the source document")
    assert normalized == copy_path or Path(normalized).samefile(copy_path)
    reopened = pymupdf.open(copy_path)
    try:
        assert reopened[0].first_annot is not None
    finally:
        reopened.close()
    assert not (_snapshots_in_temp() - baseline), (
        "a non-same-path save must not leave snapshot temp files behind")
    doc.close()


def test_failed_replacement_preserves_original_and_session(
        tmp_path: Path, monkeypatch) -> None:
    """A lock at the final replace fails the save with everything intact.

    The original file keeps its bytes, the live session stays usable with
    its unsaved edits (dirty flag, store, page access), and no snapshot or
    output temp files outlive the closed document.
    """
    pdf = make_pdf(str(tmp_path / "locked.pdf"), pages=2)
    original_bytes = Path(pdf).read_bytes()
    baseline = _snapshots_in_temp()
    temp_saves_before = set(glob.glob(
        str(tmp_path / ".inkfall-save-*")))

    doc = _open_doc(pdf)
    _seed_stroke(doc, 0)
    doc.mark_changed()

    import document.backend as backend_module

    def _locked(_temp, _target):
        raise SaveLockedError("simulated destination lock")

    monkeypatch.setattr(backend_module, "_replace_target", _locked)
    with pytest.raises(SaveLockedError):
        DocumentManager().save_document(doc, pdf)
    monkeypatch.undo()

    # Original untouched, session fully usable, edits preserved.
    assert Path(pdf).read_bytes() == original_bytes
    assert not doc.is_closed
    assert doc.page_count == 2
    assert doc.is_dirty, "a failed save must keep the unsaved-edits state"
    assert len([a for _p, a in doc.annotations.iter_all()]) == 1
    # The session keeps working: a retry to a fresh destination succeeds.
    DocumentManager().save_document(doc, str(tmp_path / "retry.pdf"))

    doc.close()
    assert not (_snapshots_in_temp() - baseline), (
        "snapshot temp files leaked after close")
    assert set(glob.glob(str(tmp_path / ".inkfall-save-*"))) == temp_saves_before, (
        "output temp files leaked in the target directory")


def test_consecutive_same_path_saves_do_not_leak_snapshots(
        tmp_path: Path) -> None:
    """Each same-path save retires the previous save's snapshot file."""
    pdf = make_pdf(str(tmp_path / "twice.pdf"), pages=2)
    baseline = _snapshots_in_temp()
    doc = _open_doc(pdf)
    manager = DocumentManager()
    _seed_stroke(doc, 0)
    manager.save_document(doc, pdf)
    _seed_stroke(doc, 1)
    manager.save_document(doc, pdf)
    reopened = pymupdf.open(pdf)
    try:
        assert len(reopened) == 2
        assert reopened[0].first_annot is not None
        assert reopened[1].first_annot is not None
    finally:
        reopened.close()
    doc.close()
    assert not (_snapshots_in_temp() - baseline), (
        "a second same-path save leaked its (or the previous) snapshot")


# ---------------------------------------------------------------------------
# ordinary save + read-only behavior stays as before
# ---------------------------------------------------------------------------


def test_ordinary_unmodified_save_unchanged(tmp_path: Path) -> None:
    """A plain save of an untouched document still verifies and reopens."""
    pdf = make_pdf(str(tmp_path / "plain.pdf"), pages=3)
    doc = _open_doc(pdf)
    DocumentManager().save_document(doc, pdf)
    assert page_count(pdf) == 3
    doc.close()
    assert doc.is_closed


def test_readonly_session_still_refuses_to_save(tmp_path: Path) -> None:
    """Read-only duplicate sessions keep their document-side save guards."""
    pdf = make_pdf(str(tmp_path / "ro.pdf"), pages=2)
    manager = DocumentManager()
    ro = manager.open_readonly(pdf)
    assert ro.can_save is False
    with pytest.raises(ReadOnlyDocumentError):
        ro.write_export(str(tmp_path / "elsewhere.pdf"))
    with pytest.raises(ValueError):
        manager.save_document(ro, str(tmp_path / "elsewhere.pdf"))
    ro.close()


def page_count(pdf_path: str) -> int:
    doc = pymupdf.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# process-path save reuses the render work copy (single serialization)
# ---------------------------------------------------------------------------


def _pumping_pump(stages: list | None = None, done_payload: dict | None = None):
    """Headless stand-in for the UI's event pump: plain blocking loop.

    Stage arrivals are coalesced by the poller exactly as in production
    (the labels only feed the progress dialog), so ``stages`` may miss
    fast intermediate stages; the final payload is always complete.
    """

    def pump(poll):
        while True:
            done, payload = poll()
            if stages is not None and payload.get("stage"):
                stages.append(payload["stage"])
            if done:
                if done_payload is not None:
                    done_payload.update(payload)
                return payload
            time.sleep(0.01)

    return pump


def _work_copy_prefix(path: str) -> bool:
    return os.path.basename(path).startswith("inkfall-work-")


def test_same_path_save_borrows_the_render_work_copy(tmp_path: Path) -> None:
    """A same-path save exports from the work copy, never a second write.

    After prepare_for_save materializes the sanitized working copy, the
    export input IS that file: the backend's pending snapshot (the file
    backing the replacement handle) must be the borrowed work copy, no
    fresh inkfall-snapshot temp may appear, and the document must stay
    fully usable and export correctly.
    """
    pdf = make_pdf(str(tmp_path / "borrow.pdf"), pages=3)
    baseline = _snapshots_in_temp()
    doc = _open_doc(pdf)
    _seed_stroke(doc, 0)
    doc.mark_changed()
    doc.prepare_for_save(pdf)  # materialize, as the UI save job does

    stages: list[str] = []
    done_payload: dict = {}
    doc.write_export(pdf, pump=_pumping_pump(stages, done_payload))

    backend = doc._backend
    assert backend.pending_snapshot == doc.render_path, (
        "the export must borrow the render work copy as the pending "
        "snapshot instead of writing a second full serialization")
    assert _work_copy_prefix(backend.pending_snapshot)
    assert os.path.isfile(backend.pending_snapshot)
    assert not (_snapshots_in_temp() - baseline), (
        "a borrowed-work-copy save must not create snapshot temp files")
    assert "verify" in stages
    assert done_payload.get("ok") is True
    assert "write_s" in (done_payload.get("timings") or {}), (
        "the child must report per-stage timings for diagnostics")
    reopened = pymupdf.open(pdf)
    try:
        assert len(reopened) == 3
        assert reopened[0].first_annot is not None
    finally:
        reopened.close()
    borrowed = backend.pending_snapshot
    doc.close()
    assert borrowed and not os.path.isfile(borrowed), (
        "close() must retire the borrowed snapshot")


def test_save_as_borrows_when_materialized_and_falls_back_otherwise(
        tmp_path: Path) -> None:
    """Save As borrows a current work copy; a fresh doc writes its own."""
    pdf = make_pdf(str(tmp_path / "fresh.pdf"), pages=2)
    original_bytes = Path(pdf).read_bytes()
    baseline = _snapshots_in_temp()

    # Freshly opened, nothing stripped: renderer still reads the source,
    # so there is no work copy to borrow and the backend writes its own
    # snapshot (then deletes it — a Save As never keeps one).
    doc = _open_doc(pdf)
    assert doc._export_snapshot_candidate() is None
    copy_path = str(tmp_path / "as-copy.pdf")
    doc.write_export(copy_path, pump=_pumping_pump())
    assert doc._backend.pending_snapshot is None, (
        "a non-same-path save must not pin a pending snapshot")
    assert Path(copy_path).is_file()
    assert Path(pdf).read_bytes() == original_bytes
    doc.close()
    assert not (_snapshots_in_temp() - baseline), (
        "the fallback snapshot of a Save As must be deleted afterwards")

    # A document whose renderer is on a work copy (page op materialized)
    # borrows that copy even for Save As, and it stays a work copy.
    pdf2 = make_pdf(str(tmp_path / "op.pdf"), pages=2)
    doc2 = _open_doc(pdf2)
    doc2.rotate_page(0, 90)
    candidate = doc2._export_snapshot_candidate()
    assert candidate is not None and _work_copy_prefix(candidate)
    doc2.write_export(str(tmp_path / "as-copy2.pdf"), pump=_pumping_pump())
    assert os.path.isfile(candidate), (
        "a borrowed file must survive a Save As as the render work copy")
    assert doc2._backend.pending_snapshot is None
    doc2.close()


def test_backend_export_falls_back_when_borrow_path_is_unusable(
        tmp_path: Path) -> None:
    """An unusable borrowed path falls back to the backend's own snapshot."""
    pdf = make_pdf(str(tmp_path / "unit.pdf"), pages=2)
    backend = PdfBackend.open(pdf)
    try:
        backend.export_pdf_in_process(
            pdf, [], b'{"format":"probe"}', 2,
            pump=_pumping_pump(), snapshot_path=str(tmp_path / "missing.pdf"))
        assert Path(pdf).is_file()
        assert backend.pending_snapshot is None or not _work_copy_prefix(
            backend.pending_snapshot), (
            "the fallback snapshot must not be mistaken for a borrowed "
            "work copy")
    finally:
        backend.close()

    # The borrow itself, at the backend contract level: the offered file
    # becomes the pending snapshot on a same-path save.
    backend2 = PdfBackend.open(pdf)
    work = str(tmp_path / "inkfall-work-unit.pdf")
    backend2.save_copy(work)
    try:
        backend2.export_pdf_in_process(
            pdf, [], b'{"format":"probe"}', 2,
            pump=_pumping_pump(), snapshot_path=work)
        assert backend2.pending_snapshot == work
        assert os.path.isfile(work)
    finally:
        backend2.close()
    assert not os.path.isfile(work), (
        "close() must retire the pending (borrowed) snapshot")


def test_prune_keeps_the_pending_snapshot_alive(tmp_path: Path) -> None:
    """A borrowed snapshot outlives work-copy pruning until it is retired."""
    pdf = make_pdf(str(tmp_path / "prune.pdf"), pages=2)
    doc = _open_doc(pdf)
    doc.prepare_for_save(pdf)
    doc.write_export(pdf, pump=_pumping_pump())
    borrowed = doc._backend.pending_snapshot
    assert borrowed is not None and _work_copy_prefix(borrowed)

    # A page op materializes a new work copy; the pruning that follows
    # must keep the file that backs the live handle.
    doc.rotate_page(0, 90)
    assert os.path.isfile(borrowed), (
        "materialize pruning deleted the pending snapshot backing the "
        "live handle")
    assert doc.page_count == 2

    # The next same-path save retires it: the handle closed before the
    # replacement, so the old backing file may finally vanish.
    doc.write_export(pdf, pump=_pumping_pump())
    assert not os.path.isfile(borrowed)
    assert doc._backend.pending_snapshot != borrowed
    doc.close()


def test_failed_same_path_save_with_borrow_keeps_session_usable(
        tmp_path: Path, monkeypatch) -> None:
    """A lock at replace after a borrowed export leaves everything intact."""
    pdf = make_pdf(str(tmp_path / "locked2.pdf"), pages=2)
    original_bytes = Path(pdf).read_bytes()
    baseline = _snapshots_in_temp()

    doc = _open_doc(pdf)
    _seed_stroke(doc, 0)
    doc.mark_changed()
    doc.rotate_page(1, 180)  # materialize so the export borrows a work copy

    import document.backend as backend_module

    def _locked(_temp, _target):
        raise SaveLockedError("simulated destination lock")

    monkeypatch.setattr(backend_module, "_replace_target", _locked)
    with pytest.raises(SaveLockedError):
        doc.write_export(pdf, pump=_pumping_pump())
    monkeypatch.undo()

    assert Path(pdf).read_bytes() == original_bytes
    assert doc.page_count == 2
    assert doc.is_dirty
    assert len([a for _p, a in doc.annotations.iter_all()]) == 1
    borrowed = doc._backend.pending_snapshot
    assert borrowed is not None and os.path.isfile(borrowed), (
        "a failed same-path save must keep a usable live handle backed "
        "by the borrowed file")
    # Retry to a fresh destination succeeds through the same process path.
    doc.write_export(str(tmp_path / "retry.pdf"), pump=_pumping_pump())
    assert Path(str(tmp_path / "retry.pdf")).is_file()
    doc.close()
    assert not (_snapshots_in_temp() - baseline)
    assert not glob.glob(str(tmp_path / ".inkfall-save-*")), (
        "output temp files leaked in the target directory")
