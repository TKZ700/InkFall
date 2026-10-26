"""M6.1 document-owned annotation store and stable identity.

Observable behavior only: isolation, ordering, identity lookup,
duplicate rejection, removal, validation, clearing, and
non-mutating reads — plus unchanged model/ink compatibility.
"""

import pytest

from annotations.models import (
    AnnotationBase,
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
    StickyNote,
)
from annotations.store import AnnotationStore
from core.types import AnnotationId, PageId, Point


def _stroke(i=0, page=None):
    return InkStroke(
        page_id=page or PageId(),
        points=(Point(float(i), 0.0), Point(float(i) + 1.0, 1.0)),
        pressures=(0.5, 0.6),
        times_ms=(100 + i, 108 + i),
    )


def test_independent_owners_have_isolated_stores():
    a, b = AnnotationStore(3), AnnotationStore(3)
    s = _stroke()
    a.add(s, 0)
    assert len(a) == 1 and len(b) == 0
    assert b.get(s.id) is None
    assert b.page(0) == ()


def test_add_and_retrieve_from_page():
    store = AnnotationStore(2)
    s = _stroke()
    assert store.add(s, 1) == s.id
    assert store.page(1) == (s,)
    assert store.page(0) == ()
    assert len(store) == 1 and store.count(1) == 1


def test_page_and_insertion_order_deterministic():
    store = AnnotationStore(3)
    ordered = []
    for page in (2, 0, 2, 1, 0):
        s = _stroke(len(ordered))
        store.add(s, page)
        ordered.append((page, s))
    assert [(p, a) for p, a in store.iter_all()] == sorted(
        ordered, key=lambda e: (e[0], ordered.index(e)))
    assert [a for _, a in store.iter_all()] == [
        s for _, s in sorted(ordered, key=lambda e: e[0])]
    for page in range(3):
        want = tuple(s for p, s in ordered if p == page)
        assert store.page(page) == want


def test_lookup_by_identity():
    store = AnnotationStore(2)
    s = _stroke()
    store.add(s, 0)
    assert store.get(s.id) is s
    assert store.get(AnnotationId(s.id.value)) == s
    assert store.contains(s.id) is True
    assert store.contains(AnnotationId("missing")) is False
    assert store.get("missing") is None


def test_duplicate_identity_rejected_without_state_change():
    store = AnnotationStore(2)
    s = _stroke()
    store.add(s, 0)
    twin = InkStroke(id=s.id, points=(Point(9.0, 9.0),))
    with pytest.raises(ValueError):
        store.add(twin, 1)
    with pytest.raises(ValueError):
        store.add(twin, 0)
    assert store.page(0) == (s,)
    assert store.page(1) == ()
    assert len(store) == 1
    assert store.get(s.id) is s


def test_remove_clears_both_indexes_and_keeps_order():
    store = AnnotationStore(1)
    a, b, c = _stroke(0), _stroke(1), _stroke(2)
    for s in (a, b, c):
        store.add(s, 0)
    assert store.remove(b.id) is True
    assert store.page(0) == (a, c)
    assert store.get(b.id) is None
    assert store.contains(b.id) is False
    assert len(store) == 2
    assert store.remove(b.id) is False
    assert store.remove(AnnotationId("never")) is False


def test_removed_identity_may_be_reinserted_intact():
    store = AnnotationStore(2)
    s = _stroke()
    store.add(s, 0)
    assert store.remove(s.id) is True
    assert store.add(s, 1) == s.id
    assert s.id is not None and store.get(s.id) is s
    assert store.page(0) == () and store.page(1) == (s,)


def test_page_index_validation():
    store = AnnotationStore(2)
    s = _stroke()
    for bad in (-1, 2, 99):
        with pytest.raises(ValueError):
            store.add(s, bad)
        with pytest.raises(ValueError):
            store.page(bad)
        with pytest.raises(ValueError):
            store.count(bad)
    for bad in ("0", None, 1.0, True):
        with pytest.raises(TypeError):
            store.add(s, bad)
        with pytest.raises(TypeError):
            store.page(bad)
    with pytest.raises(TypeError):
        store.add("not-an-annotation", 0)
    with pytest.raises(TypeError):
        AnnotationStore("3")
    with pytest.raises(TypeError):
        AnnotationStore(True)
    with pytest.raises(ValueError):
        AnnotationStore(-1)
    assert len(store) == 0


def test_clear_one_store_leaves_another_untouched():
    a, b = AnnotationStore(2), AnnotationStore(2)
    s = _stroke()
    a.add(s, 0)
    b.add(_stroke(1), 1)
    a.clear()
    a.clear()
    assert len(a) == 0 and a.page(0) == ()
    assert len(b) == 1 and b.contains(s.id) is False
    assert b.page(1) != ()


def test_returned_collections_cannot_mutate_store():
    store = AnnotationStore(2)
    s = _stroke()
    store.add(s, 0)
    page = store.page(0)
    assert isinstance(page, tuple)
    with pytest.raises(AttributeError):
        page.append(s)  # type: ignore[attr-defined]
    listed = list(store.iter_all())
    listed.clear()
    assert len(store) == 1
    assert store.page(0) == (s,)


def test_model_constructors_and_equality_unchanged():
    s = InkStroke(points=(Point(0.0, 0.0),))
    assert isinstance(s, AnnotationBase)
    assert s.id != InkStroke(points=(Point(0.0, 0.0),)).id
    fixed = AnnotationId("abc123")
    assert InkStroke(id=fixed).id == fixed
    # Full-dataclass equality includes auto fields (page_id); identity
    # equality is on .id, which is the contract under test.
    assert InkStroke(id=fixed).id == InkStroke(id=fixed).id
    for ann in (Highlighter(), Shape(), StickyNote(),
                Shape(kind=ShapeKind.ARROW)):
        assert isinstance(ann.id, AnnotationId)
        store = AnnotationStore(1)
        store.add(ann, 0)
        assert store.get(ann.id) is ann


def test_document_owns_isolated_store_cleared_on_close(tmp_path):
    from document.manager import DocumentManager
    from tests.helpers import make_probe_pdf

    manager = DocumentManager()
    pdf_a = make_probe_pdf(str(tmp_path / "a.pdf"))
    pdf_b = make_probe_pdf(str(tmp_path / "b.pdf"))
    try:
        doc_a = manager.open(pdf_a)
        doc_b = manager.open(pdf_b)
        assert doc_a.annotations is doc_a.annotations
        assert doc_a.annotations is not doc_b.annotations
        assert doc_a.annotations.page_count == doc_a.page_count
        s = _stroke()
        doc_a.annotations.add(s, 0)
        assert doc_b.annotations.get(s.id) is None
        manager.close(doc_a.id)
        assert len(doc_a.annotations) == 0
        assert len(doc_b.annotations) == 0
        kept = _stroke(9)
        doc_b.annotations.add(kept, 0)
        assert doc_b.annotations.get(kept.id) is kept
        manager.close(doc_b.id)
        assert len(doc_b.annotations) == 0
    finally:
        manager.close_all()
