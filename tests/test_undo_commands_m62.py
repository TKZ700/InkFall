"""M6.2 model-level undo/redo: Add/Delete commands, histories, HUD.

Behavioral tests only: insertion/removal/undo/redo coherence,
identity stability, order restoration, per-document isolation,
lifecycle, and HUD wiring. No drawing, persistence, or tool behavior.
"""

import pytest

from annotations.models import Highlighter, InkStroke, Shape
from annotations.store import AnnotationStore
from app.undo import (
    AddAnnotationCommand,
    DeleteAnnotationCommand,
    DocumentHistory,
)
from core.types import AnnotationId, PageId, Point

pytestmark = pytest.mark.usefixtures("qapp")


def _stroke(i=0):
    return InkStroke(
        page_id=PageId(),
        points=(Point(float(i), 0.0), Point(float(i) + 1.0, 1.0)),
        pressures=(0.5, 0.6),
        times_ms=(100 + i, 108 + i),
    )


def _history(pages=2):
    return DocumentHistory(AnnotationStore(pages))


# ---------------------------------------------------------------- Add

def test_push_add_inserts_exactly_once():
    history = _history()
    s = _stroke()
    history.push_add(s, 0)
    assert history.store.page(0) == (s,)
    assert len(history.store) == 1


def test_add_undo_removes_and_redo_restores_identity():
    history = _history()
    s = _stroke()
    history.push_add(s, 1)
    history.undo()
    assert history.store.page(1) == ()
    assert history.store.get(s.id) is None
    history.redo()
    got = history.store.get(s.id)
    assert got is s
    assert got.points == s.points and got.pressures == s.pressures
    assert history.store.page(1) == (s,)


def test_repeated_undo_redo_cycles_stay_consistent():
    history = _history()
    s = _stroke()
    history.push_add(s, 0)
    for _ in range(3):
        history.undo()
        assert len(history.store) == 0
        history.redo()
        assert history.store.get(s.id) is s
    assert history.store.page(0) == (s,)


def test_duplicate_add_fails_without_corruption():
    history = _history()
    s = _stroke()
    history.push_add(s, 0)
    n = history.stack.count()
    with pytest.raises(ValueError):
        history.push_add(InkStroke(id=s.id), 0)
    with pytest.raises(ValueError):
        history.push_add(InkStroke(id=s.id), 1)
    assert history.stack.count() == n
    assert history.store.page(0) == (s,)
    assert len(history.store) == 1
    # History still usable afterwards.
    t = _stroke(9)
    history.push_add(t, 1)
    history.undo()
    assert history.store.get(t.id) is None
    assert history.store.get(s.id) is s


def test_add_preserves_page_index_and_labels():
    history = _history(pages=3)
    s = _stroke()
    cmd = history.push_add(s, 2)
    assert isinstance(cmd, AddAnnotationCommand)
    assert cmd.text() == "Add Annotation"
    assert history.store.page(2) == (s,)


def test_add_rejects_bad_page_before_push():
    history = _history(pages=2)
    with pytest.raises(ValueError):
        history.push_add(_stroke(), 7)
    assert history.stack.count() == 0
    assert len(history.store) == 0


# ---------------------------------------------------------------- Delete

def test_push_delete_removes_correct_annotation():
    history = _history()
    a, b = _stroke(0), _stroke(1)
    history.push_add(a, 0)
    history.push_add(b, 0)
    history.push_delete(a.id)
    assert history.store.page(0) == (b,)
    assert history.store.get(a.id) is None


def test_delete_undo_restores_order_and_redo_removes():
    history = _history()
    a, b, c = _stroke(0), _stroke(1), _stroke(2)
    for s in (a, b, c):
        history.push_add(s, 0)
    history.push_delete(b.id)
    assert history.store.page(0) == (a, c)
    history.undo()
    restored = history.store.get(b.id)
    assert restored is b
    assert history.store.page(0) == (a, b, c)
    history.redo()
    assert history.store.page(0) == (a, c)
    assert history.store.get(b.id) is None


def test_delete_cannot_hit_unrelated_annotation():
    history = _history()
    a = _stroke(0)
    history.push_add(a, 0)
    cmd = DeleteAnnotationCommand(history.store, a.id)
    assert cmd.text() == "Delete Annotation"
    with pytest.raises(ValueError):
        DeleteAnnotationCommand(history.store, AnnotationId("ghost"))
    # The validated command still targets its own annotation.
    history.stack.push(cmd)
    assert history.store.page(0) == ()


def test_delete_missing_identity_is_explicit_and_safe():
    history = _history()
    with pytest.raises(ValueError):
        history.push_delete(AnnotationId("nope"))
    with pytest.raises(ValueError):
        history.push_delete("nope")
    assert history.stack.count() == 0
    assert len(history.store) == 0


def test_middle_deletion_keeps_relative_order_on_undo():
    history = _history()
    items = [_stroke(i) for i in range(5)]
    for s in items:
        history.push_add(s, 0)
    history.push_delete(items[2].id)
    history.push_delete(items[0].id)
    assert history.store.page(0) == (items[1], items[3], items[4])
    history.undo()
    assert history.store.page(0) == (items[0], items[1], items[3],
                                     items[4])
    history.undo()
    assert history.store.page(0) == tuple(items)


# ------------------------------------------------- grouped deletes (M6.5)

def test_push_delete_grouped_is_one_undo_step():
    history = _history()
    a, b, c = _stroke(0), _stroke(1), _stroke(2)
    for s in (a, b, c):
        history.store.add(s, 0)
    history.push_delete_grouped(a.id)
    assert history.group_open is True
    history.push_delete_grouped(b.id)
    assert history.group_open is True
    assert history.store.page(0) == (c,)
    history.end_group()
    assert history.group_open is False
    # Two removals collapsed into a single undo step.
    assert history.stack.count() == 1
    history.undo()
    assert history.store.page(0) == (a, b, c)
    history.redo()
    assert history.store.page(0) == (c,)


def test_grouped_delete_restores_original_relative_order():
    history = _history()
    items = [_stroke(i) for i in range(4)]
    for s in items:
        history.store.add(s, 0)
    for s in (items[1], items[3]):
        history.push_delete_grouped(s.id)
    history.end_group()
    assert history.store.page(0) == (items[0], items[2])
    history.undo()
    assert history.store.page(0) == tuple(items)


def test_end_group_without_deletes_leaves_no_entry():
    history = _history()
    history.store.add(_stroke(0), 0)
    history.end_group()  # no group was opened: safe no-op
    assert history.group_open is False
    assert history.stack.count() == 0
    assert history.can_undo() is False


def test_push_delete_grouped_validates_before_opening_group():
    history = _history()
    history.store.add(_stroke(0), 0)
    with pytest.raises(ValueError):
        history.push_delete_grouped(AnnotationId("ghost"))
    # A rejected first delete must not leave an empty group or command.
    assert history.group_open is False
    assert history.stack.count() == 0


def test_end_group_is_idempotent():
    history = _history()
    s = _stroke(0)
    history.store.add(s, 0)
    history.push_delete_grouped(s.id)
    history.end_group()
    history.end_group()  # second finalize is a safe no-op
    assert history.group_open is False
    assert history.stack.count() == 1
    history.undo()
    assert history.store.page(0) == (s,)


# ---------------------------------------------------------------- isolation

def test_two_documents_have_independent_histories_and_stores():
    ha, hb = _history(), _history()
    sa, sb = _stroke(0), _stroke(1)
    ha.push_add(sa, 0)
    hb.push_add(sb, 1)
    ha.undo()
    assert ha.store.get(sa.id) is None
    assert hb.store.get(sb.id) is sb
    hb.undo()
    assert hb.store.get(sb.id) is None
    ha.redo()
    assert ha.store.get(sa.id) is sa
    assert hb.store.get(sb.id) is None


def test_closing_document_releases_history_without_side_effects():
    ha, hb = _history(), _history()
    sa = _stroke(0)
    ha.push_add(sa, 0)
    hb.push_add(_stroke(1), 0)
    # close_document drops the history object; clearing models that
    # release while the store itself is cleared separately.
    ha.clear()
    assert ha.can_undo() is False
    assert ha.store.get(sa.id) is sa  # history clear touches no data
    assert hb.can_undo() is True
    hb.undo()
    assert hb.store.page(0) == ()
    assert ha.store.get(sa.id) is sa


def test_m61_store_invariants_hold_under_commands():
    history = _history(pages=2)
    s = _stroke()
    history.push_add(s, 0)
    with pytest.raises(ValueError):
        history.store.add(s, 0)
    assert history.store.page(0) == (s,)
    assert history.store.contains(s.id) is True
    assert history.store.count(0) == 1


# ---------------------------------------------------------------- HUD

def _open_viewer(qtbot, tmp_path, name):
    from document.manager import DocumentManager
    from tests.helpers import make_probe_pdf

    from app.viewer import PdfViewer

    pdf = make_probe_pdf(str(tmp_path / name))
    viewer = PdfViewer(DocumentManager())
    qtbot.addWidget(viewer)
    assert viewer.open_document(pdf) is True
    return viewer


def test_hud_disabled_on_empty_history(qtbot, tmp_path):
    viewer = _open_viewer(qtbot, tmp_path, "u1.pdf")
    try:
        hud = viewer.undo_hud
        assert hud.can_undo() is False
        assert hud.can_redo() is False
        assert viewer.document_history is not None
        assert viewer.document_history.can_undo() is False
    finally:
        viewer.close()


def test_hud_availability_tracks_push_undo_redo(qtbot, tmp_path):
    viewer = _open_viewer(qtbot, tmp_path, "u2.pdf")
    try:
        hud = viewer.undo_hud
        history = viewer.document_history
        history.push_add(_stroke(0), 0)
        viewer._sync_undo_hud()
        assert hud.can_undo() is True
        assert history.store.page(0) != ()
        hud.undoRequested.emit()
        assert history.store.page(0) == ()
        assert hud.can_undo() is False
        assert hud.can_redo() is True
        hud.redoRequested.emit()
        assert len(history.store.page(0)) == 1
        assert hud.can_undo() is True
        assert hud.can_redo() is False
    finally:
        viewer.close()


def test_hud_targets_current_document_only(qtbot, tmp_path):
    viewer = _open_viewer(qtbot, tmp_path, "u3a.pdf")
    try:
        from tests.helpers import make_probe_pdf

        hud = viewer.undo_hud
        first = viewer.document_history
        first.push_add(_stroke(0), 0)
        viewer._sync_undo_hud()
        assert hud.can_undo() is True
        pdf_b = make_probe_pdf(str(tmp_path / "u3b.pdf"))
        assert viewer.open_document(pdf_b) is True
        # Switched: opening funnels close, which clears the old
        # document's store (M6.1 lifecycle) and drops its viewer
        # history; the HUD reflects only the new, empty document.
        assert hud.can_undo() is False
        assert hud.can_redo() is False
        assert viewer.document_history is not first
        hud.undoRequested.emit()  # must be a safe no-op
        assert hud.can_undo() is False
        assert first.store.page(0) == ()  # cleared with its document
    finally:
        viewer.close()


def test_manager_close_of_other_doc_keeps_viewer_history(qtbot, tmp_path):
    from document.manager import DocumentManager
    from tests.helpers import make_probe_pdf

    from app.viewer import PdfViewer

    manager = DocumentManager()
    try:
        pdf_a = make_probe_pdf(str(tmp_path / "u4a.pdf"))
        pdf_b = make_probe_pdf(str(tmp_path / "u4b.pdf"))
        viewer = PdfViewer(manager)
        qtbot.addWidget(viewer)
        try:
            assert viewer.open_document(pdf_a) is True
            history = viewer.document_history
            history.push_add(_stroke(0), 0)
            other = manager.open(pdf_b)
            manager.close(other.id)
            assert history.store.page(0) != ()
            assert history.can_undo() is True
            viewer._sync_undo_hud()
            assert viewer.undo_hud.can_undo() is True
        finally:
            viewer.close()
    finally:
        manager.close_all()


def test_viewer_close_disables_hud_and_drops_history(qtbot, tmp_path):
    viewer = _open_viewer(qtbot, tmp_path, "u5.pdf")
    try:
        history = viewer.document_history
        history.push_add(_stroke(0), 0)
        viewer.close_document()
        assert viewer.document_history is None
        assert viewer.undo_hud.can_undo() is False
        assert viewer.undo_hud.can_redo() is False
        viewer._undo_hud.undoRequested.emit()  # safe no-op
        viewer._undo_hud.redoRequested.emit()
    finally:
        viewer.close()


def test_stale_closed_document_never_receives_undo(qtbot, tmp_path):
    viewer = _open_viewer(qtbot, tmp_path, "u6.pdf")
    try:
        history = viewer.document_history
        history.push_add(_stroke(0), 0)
        viewer._manager.close(viewer._document.id)
        viewer._sync_undo_hud()
        assert viewer.undo_hud.can_undo() is False
        viewer._undo_hud.undoRequested.emit()  # no-op, no crash
        # Manager-close clears the document store by design (M6.1
        # lifecycle); the HUD never touched the stale stack.
        assert history.store.page(0) == ()
    finally:
        viewer.close()
