"""M6.8.2 Stage 5.1: page rotation + page deletion at the document layer.

Qt-free coverage for the two new document operations and the pure
page-frame geometry they use:

* the frame map (:mod:`annotations.page_rotation`) — the four rotations,
  composition/associativity, inverse round-trips, and the invariant that
  a remapped annotation stays on the same *unrotated* media point (the
  content it was drawn over);
* ``Document.rotate_page`` — cumulative quarter turns, the effective
  width/height swap, per-page isolation, intrinsic-rotation pages,
  inverse rotation, and a rasterized end-to-end alignment probe (the
  working copy the pipeline reads is rendered and compared against the
  stored annotation);
* ``Document.delete_page`` / ``insert_page`` — first/middle/last
  removal, page-index remapping of later annotations, annotation
  removal, reversible restore with original identities/order, the
  last-page guard, and the working-copy artefact.

Widget/wiring coverage (menus, undo/redo, selection self-healing) lives
in ``tests/test_page_menu_m682_stage5_1.py``.
"""

from __future__ import annotations

import pytest

from annotations.models import InkStroke, Shape, ShapeKind
from annotations.page_rotation import rotate_frame_point, rotate_page_annotations
from core.types import Point, Rect, Size
from document.backend import PdfBackend
from document.document import Document, RemovedPage
from document.models import page_id_for
from tests.helpers import (
    dark_pixel_bbox_pt,
    make_probe_pdf,
    make_sized_pdf,
)


# ------------------------------------------------------------------ helpers

def _open(path: str):
    return Document(PdfBackend.open(path), path)


def _stroke(page_id, x=100.0, y=100.0):
    return InkStroke(points=(Point(float(x), float(y)),),
                     page_id=page_id)


def _seed(document, page_index, x=100.0, y=100.0):
    stroke = _stroke(page_id_for(document.id, page_index), x, y)
    document.annotations.add(stroke, page_index)
    return stroke


def _unrotated(lx, ly, rotation, media_w, media_h):
    """Displayed-frame point -> unrotated media point (inverse of frame map)."""
    if rotation == 90:
        return (ly, media_h - lx)
    if rotation == 180:
        return (media_w - lx, media_h - ly)
    if rotation == 270:
        return (media_w - ly, lx)
    return (lx, ly)


def _media(document, page_index):
    info = document.page_info(page_index)
    if info.rotation % 180 == 90:
        return (float(info.size.height), float(info.size.width))
    return (float(info.size.width), float(info.size.height))


def _point_close(actual, expected, tol=1e-6) -> bool:
    return (abs(actual.x - expected[0]) <= tol
            and abs(actual.y - expected[1]) <= tol)


# ==================================================== pure frame geometry

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_frame_map_identity_when_rotation_unchanged(rotation):
    got = rotate_frame_point(37.0, 91.0, media_w=600.0, media_h=400.0,
                             old_rotation=rotation, new_rotation=rotation)
    assert got == (37.0, 91.0)


@pytest.mark.parametrize("media_w,media_h", [(600.0, 400.0), (500.0, 700.0)])
@pytest.mark.parametrize("old_rotation,delta", [
    (0, 90), (0, 180), (0, 270), (90, 90), (90, 180), (90, 270),
    (180, 90), (180, 180), (180, 270), (270, 90), (270, 180), (270, 270),
])
def test_frame_map_inverse_round_trips(old_rotation, delta, media_w, media_h):
    new_rotation = (old_rotation + delta) % 360
    x, y = 123.0, 77.0
    moved = rotate_frame_point(x, y, media_w=media_w, media_h=media_h,
                               old_rotation=old_rotation,
                               new_rotation=new_rotation)
    back = rotate_frame_point(moved[0], moved[1], media_w=media_w,
                              media_h=media_h, old_rotation=new_rotation,
                              new_rotation=old_rotation)
    assert back == pytest.approx((x, y), abs=1e-9)


def test_frame_map_composes():
    """old->new->newer equals old->newer."""
    direct = rotate_frame_point(50.0, 60.0, media_w=600.0, media_h=400.0,
                                old_rotation=0, new_rotation=270)
    step = rotate_frame_point(50.0, 60.0, media_w=600.0, media_h=400.0,
                              old_rotation=0, new_rotation=90)
    two = rotate_frame_point(step[0], step[1], media_w=600.0, media_h=400.0,
                             old_rotation=90, new_rotation=270)
    assert two == pytest.approx(direct, abs=1e-9)


def test_frame_map_corner_quarter_turn():
    """A quarter turn maps the displayed top-left to the new top-left band."""
    # Page 600x400 unrotated; 90 deg clockwise displays as 400x600.
    assert rotate_frame_point(0.0, 0.0, media_w=600.0, media_h=400.0,
                              old_rotation=0, new_rotation=90) == (400.0, 0.0)
    assert rotate_frame_point(200.0, 0.0, media_w=600.0, media_h=400.0,
                              old_rotation=0, new_rotation=90) == (400.0, 200.0)


def test_rotate_page_annotations_is_a_noop_for_same_rotation():
    stroke = InkStroke(points=(Point(1.0, 2.0),))
    out = rotate_page_annotations([stroke], media_w=600.0, media_h=400.0,
                                  old_rotation=90, new_rotation=90)
    assert len(out) == 1
    assert out[0] is stroke


def test_rotate_page_annotations_maps_points_and_boxes():
    stroke = InkStroke(points=(Point(100.0, 200.0), Point(300.0, 250.0)))
    box = Shape(kind=ShapeKind.RECTANGLE,
                rect=Rect(Point(50.0, 60.0), Size(100.0, 80.0)),
                rotation_deg=20.0)
    out = rotate_page_annotations([stroke, box], media_w=600.0, media_h=400.0,
                                  old_rotation=0, new_rotation=90)
    for original, candidate in zip((stroke, box), out):
        # identity, style, and page are preserved; only geometry changes.
        assert candidate.id == original.id
        assert candidate.page_id == original.page_id
    expected_point = rotate_frame_point(100.0, 200.0, media_w=600.0,
                                        media_h=400.0, old_rotation=0,
                                        new_rotation=90)
    assert _point_close(out[0].points[0], expected_point, 1e-9)
    # A box keeps its size and accumulates the frame rotation.
    assert out[1].rect.width == pytest.approx(100.0, abs=1e-9)
    assert out[1].rect.height == pytest.approx(80.0, abs=1e-9)
    assert out[1].rotation_deg == pytest.approx(110.0, abs=1e-9)
    center = (out[1].rect.origin.x + out[1].rect.width / 2.0,
              out[1].rect.origin.y + out[1].rect.height / 2.0)
    expected_center = rotate_frame_point(100.0, 100.0, media_w=600.0,
                                         media_h=400.0, old_rotation=0,
                                         new_rotation=90)
    assert center == pytest.approx(expected_center, abs=1e-9)


def test_rotate_page_annotations_keeps_unmappable_annotation():
    """An annotation without measurable geometry is kept, never dropped."""

    class _Bare:
        pass

    # Not an AnnotationBase: apply_group cannot express it, so it survives
    # unchanged rather than being lost.
    bare = _Bare()
    out = rotate_page_annotations([bare], media_w=600.0, media_h=400.0,
                                  old_rotation=0, new_rotation=90)
    assert out == (bare,)


# ==================================================== Document.rotate_page

def test_rotate_page_cumulative_and_dimension_swap(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"), [(600.0, 400.0)])
    document = _open(path)
    try:
        assert document.page_info(0).rotation == 0
        assert document.page_info(0).size == Size(600.0, 400.0)
        assert document.rotate_page(0, 90) == 90
        assert document.page_info(0).size == Size(400.0, 600.0)
        assert document.rotate_page(0, 90) == 180
        assert document.page_info(0).size == Size(600.0, 400.0)
        assert document.rotate_page(0, 180) == 0
        assert document.page_info(0).size == Size(600.0, 400.0)
        # Counterclockwise is the -90 equivalent.
        assert document.rotate_page(0, -90) == 270
        assert document.page_info(0).size == Size(400.0, 600.0)
    finally:
        document.close()


def test_rotate_page_normalizes_accumulated_rotations(tmp_path):
    path = make_probe_pdf(str(tmp_path / "p.pdf"), page_width=600.0,
                          page_height=400.0)
    document = _open(path)
    try:
        for _ in range(4):
            document.rotate_page(0, 90)
        assert document.page_info(0).rotation == 0
        document.rotate_page(0, 270)
        assert document.page_info(0).rotation == 270
    finally:
        document.close()


def test_rotate_page_rejects_bad_degrees(tmp_path):
    path = make_probe_pdf(str(tmp_path / "p.pdf"))
    document = _open(path)
    try:
        with pytest.raises(ValueError):
            document.rotate_page(0, 45)
        with pytest.raises((ValueError, TypeError)):
            document.rotate_page(0, True)
        with pytest.raises(Exception):
            document.rotate_page(5, 90)
        assert document.page_info(0).rotation == 0
    finally:
        document.close()


def test_rotate_page_keeps_annotations_on_the_same_content(tmp_path):
    """Annotations and rasterized content rotate together (end-to-end).

    The probe rect lives at unrotated (0,0)-(200,200); an annotation is
    placed at its displayed centre. After a 90 deg clockwise rotation the
    stored annotation must sit at the new displayed position of the same
    unrotated point, and the actual raster of the pipeline's working copy
    must cover it.
    """
    path = make_probe_pdf(str(tmp_path / "probe.pdf"), page_width=600.0,
                          page_height=400.0)
    document = _open(path)
    try:
        stroke = _seed(document, 0, 100.0, 100.0)
        assert dark_pixel_bbox_pt(document.render_path, 0) == \
            pytest.approx((0.0, 0.0, 200.0, 200.0), abs=1.5)
        document.rotate_page(0, 90)
        moved = document.annotations.get(stroke.id)
        assert _point_close(moved.points[0], (300.0, 100.0))
        # The raster (from the working copy the pipeline reads) agrees.
        bbox = dark_pixel_bbox_pt(document.render_path, 0)
        assert bbox == pytest.approx((200.0, 0.0, 400.0, 200.0), abs=1.5)
        assert bbox[0] <= moved.points[0].x <= bbox[2]
        assert bbox[1] <= moved.points[0].y <= bbox[3]
    finally:
        document.close()


@pytest.mark.parametrize("delta", [90, 180, 270])
def test_rotate_page_annotation_frame_matches_layout_rotation(tmp_path, delta):
    """The remap uses exactly the rotation the layout reports."""
    path = make_probe_pdf(str(tmp_path / "probe.pdf"), page_width=600.0,
                          page_height=400.0)
    document = _open(path)
    try:
        stroke = _seed(document, 0, 137.0, 211.0)
        media_w, media_h = _media(document, 0)
        old_rotation = document.page_info(0).rotation
        document.rotate_page(0, delta)
        moved = document.annotations.get(stroke.id)
        new_rotation = document.page_info(0).rotation
        # Same unrotated media point before and after.
        before = _unrotated(137.0, 211.0, old_rotation, media_w, media_h)
        after = _unrotated(moved.points[0].x, moved.points[0].y,
                           new_rotation, media_w, media_h)
        assert after == pytest.approx(before, abs=1e-6)
    finally:
        document.close()


def test_rotate_page_is_per_page_isolated(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        strokes = [_seed(document, i, 50.0, 60.0) for i in range(3)]
        before = [document.page_info(i).size for i in range(3)]
        document.rotate_page(1, 90)
        assert document.page_info(0).size == before[0]
        assert document.page_info(2).size == before[2]
        assert document.page_info(1).size == Size(700.0, 500.0)
        # Other pages' annotations are untouched (same objects).
        assert document.annotations.get(strokes[0].id) is strokes[0]
        assert document.annotations.get(strokes[2].id) is strokes[2]
        assert document.annotations.get(strokes[1].id) is not strokes[1]
    finally:
        document.close()


def test_rotate_page_honours_intrinsic_rotation(tmp_path):
    from tests.helpers import make_intrinsic_pdf

    path = make_intrinsic_pdf(str(tmp_path / "intr.pdf"), rotations=(90,),
                              page_width=600.0, page_height=800.0)
    document = _open(path)
    try:
        assert document.page_info(0).rotation == 90
        # Probe centre at rot 90 displays at (H-100, 100) = (700, 100).
        stroke = _seed(document, 0, 700.0, 100.0)
        document.rotate_page(0, 90)
        assert document.page_info(0).rotation == 180
        moved = document.annotations.get(stroke.id)
        # Same unrotated point (100, 100) now displays at (500, 700).
        assert _point_close(moved.points[0], (500.0, 700.0))
        bbox = dark_pixel_bbox_pt(document.render_path, 0, scale=0.5)
        # The page also carries a text label, so assert the probe's
        # horizontal band and that the remapped point stays inside the
        # dark region rather than the full bbox.
        assert bbox[0] == pytest.approx(400.0, abs=2.0)
        assert bbox[2] == pytest.approx(600.0, abs=2.0)
        assert bbox[1] <= moved.points[0].y <= bbox[3]
    finally:
        document.close()


def test_rotate_page_inverse_is_exact(tmp_path):
    path = make_probe_pdf(str(tmp_path / "probe.pdf"), page_width=600.0,
                          page_height=400.0)
    document = _open(path)
    try:
        stroke = _seed(document, 0, 137.0, 211.0)
        document.rotate_page(0, 270)
        document.rotate_page(0, 90)
        assert document.page_info(0).rotation == 0
        back = document.annotations.get(stroke.id)
        assert _point_close(back.points[0], (137.0, 211.0))
    finally:
        document.close()


def test_rotate_page_updates_backend_not_original_file(tmp_path):
    path = make_probe_pdf(str(tmp_path / "probe.pdf"), page_width=600.0,
                          page_height=400.0)
    document = _open(path)
    try:
        document.rotate_page(0, 90)
        assert document.render_path != path
        # The original file on disk is untouched.
        original = PdfBackend.open(path)
        try:
            assert original.page_info(0).rotation == 0
        finally:
            original.close()
        # The working copy reflects the rotation (the reopen artefact).
        reopened = PdfBackend.open(document.render_path)
        try:
            assert reopened.page_info(0).rotation == 90
        finally:
            reopened.close()
    finally:
        document.close()


# ==================================================== Document.delete_page

def test_delete_middle_page_remaps_later_annotations(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        strokes = [_seed(document, i, 10.0 * i, 20.0 * i) for i in range(3)]
        removed = document.delete_page(1)
        assert isinstance(removed, RemovedPage)
        assert removed.index == 1
        assert removed.annotations == (strokes[1],)
        assert document.page_count == 2
        # Page 0 is untouched; old page 2 is now page 1.
        assert document.annotations.locate(strokes[0].id) == (0, 0)
        assert document.annotations.get(strokes[0].id) is strokes[0]
        assert document.annotations.locate(strokes[2].id) == (1, 0)
        rekeyed = document.annotations.get(strokes[2].id)
        assert rekeyed.page_id == page_id_for(document.id, 1)
        assert document.annotations.get(strokes[1].id) is None
        # Layout size follows the surviving pages.
        assert document.page_info(0).size == Size(600.0, 400.0)
        assert document.page_info(1).size == Size(400.0, 400.0)
    finally:
        document.close()


def test_delete_first_and_last_page(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        strokes = [_seed(document, i) for i in range(3)]
        document.delete_page(0)
        assert document.page_count == 2
        assert document.annotations.get(strokes[1].id).page_id == \
            page_id_for(document.id, 0)
        document.delete_page(1)  # last of the two
        assert document.page_count == 1
        assert document.annotations.get(strokes[1].id).page_id == \
            page_id_for(document.id, 0)
        assert document.annotations.get(strokes[2].id) is None
    finally:
        document.close()


def test_delete_page_is_reversible_with_identity_and_order(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        # Two annotations on the middle page to prove order is restored.
        first = _seed(document, 1, 10.0, 10.0)
        second = _seed(document, 1, 20.0, 20.0)
        third = _seed(document, 2, 30.0, 30.0)
        snapshot = [document.page_info(i) for i in range(3)]
        removed = document.delete_page(1)
        document.insert_page(removed)
        assert document.page_count == 3
        assert document.annotations.page(1) == (first, second)
        assert document.annotations.locate(first.id) == (1, 0)
        assert document.annotations.locate(second.id) == (1, 1)
        assert document.annotations.locate(third.id) == (2, 0)
        assert [document.page_info(i) for i in range(3)] == snapshot
    finally:
        document.close()


def test_delete_page_restores_intrinsic_rotation_and_metadata(tmp_path):
    from tests.helpers import make_intrinsic_pdf

    path = make_intrinsic_pdf(str(tmp_path / "intr.pdf"),
                              rotations=(0, 90, 180),
                              page_width=600.0, page_height=800.0)
    document = _open(path)
    try:
        removed = document.delete_page(1)
        assert document.page_info(1).rotation == 180
        document.insert_page(removed)
        assert document.page_info(1).rotation == 90
        assert document.page_info(0).rotation == 0
        assert document.page_info(2).rotation == 180
    finally:
        document.close()


def test_delete_page_refuses_the_last_page(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"), [(600.0, 400.0)])
    document = _open(path)
    try:
        with pytest.raises(ValueError):
            document.delete_page(0)
        assert document.page_count == 1
    finally:
        document.close()


def test_delete_page_rejects_invalid_index(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0)])
    document = _open(path)
    try:
        with pytest.raises((ValueError, TypeError)):
            document.delete_page(2)
        with pytest.raises((ValueError, TypeError)):
            document.delete_page(-1)
        assert document.page_count == 2
    finally:
        document.close()


def test_delete_page_working_copy_reflects_removal(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        document.delete_page(1)
        # Original file untouched.
        original = PdfBackend.open(path)
        try:
            assert original.page_count == 3
        finally:
            original.close()
        reopened = PdfBackend.open(document.render_path)
        try:
            assert reopened.page_count == 2
            assert reopened.page_info(0).size == Size(600.0, 400.0)
            assert reopened.page_info(1).size == Size(400.0, 400.0)
        finally:
            reopened.close()
    finally:
        document.close()


def test_repeated_deletes_keep_page_ids_consistent(tmp_path):
    path = make_sized_pdf(str(tmp_path / "sized.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0),
                           (450.0, 450.0)])
    document = _open(path)
    try:
        strokes = [_seed(document, i, 5.0 * i, 5.0 * i) for i in range(4)]
        document.delete_page(1)
        document.delete_page(0)
        assert document.page_count == 2
        # Survivors are old pages 2 and 3, now 0 and 1, with fresh page ids.
        assert document.annotations.get(strokes[2].id).page_id == \
            page_id_for(document.id, 0)
        assert document.annotations.get(strokes[3].id).page_id == \
            page_id_for(document.id, 1)
        for index in range(document.page_count):
            for annotation in document.annotations.page(index):
                assert annotation.page_id == page_id_for(document.id, index)
    finally:
        document.close()


# ------------------------------------------------- failure rollback


def _fail_save(monkeypatch, document):
    """Make the working-copy write raise, as a disk/lock fault would."""
    def boom(_self, _target):  # patched method on the PdfBackend class
        raise RuntimeError("simulated working-copy write failure")

    monkeypatch.setattr(type(document._backend), "save_copy", boom)


def test_rotate_page_rolls_back_when_the_copy_cannot_be_written(
        tmp_path, monkeypatch):
    path = make_sized_pdf(str(tmp_path / "r.pdf"), [(400.0, 300.0)])
    document = _open(path)
    try:
        stroke = _seed(document, 0, 100.0, 100.0)
        before_render = document.render_path
        before_work = list(document._work_paths)
        _fail_save(monkeypatch, document)
        with pytest.raises(RuntimeError):
            document.rotate_page(0, 90)
        # No partial mutation: rotation, annotations, working copy and
        # page count are all exactly as before the call.
        assert document.page_info(0).rotation == 0
        assert _point_close(document.annotations.get(stroke.id).points[0],
                            (100.0, 100.0))
        assert document.render_path == before_render
        assert document._work_paths == before_work
        assert document.page_count == 1
    finally:
        document.close()


def test_delete_page_rolls_back_when_the_copy_cannot_be_written(
        tmp_path, monkeypatch):
    path = make_sized_pdf(str(tmp_path / "d.pdf"),
                          [(600.0, 400.0), (500.0, 700.0), (400.0, 400.0)])
    document = _open(path)
    try:
        a = _seed(document, 1, 120.0, 80.0)
        before_render = document.render_path
        _fail_save(monkeypatch, document)
        with pytest.raises(RuntimeError):
            document.delete_page(1)
        assert document.page_count == 3
        assert document.annotations.page(1) == (a,)
        assert document.annotations.locate(a.id) == (1, 0)
        assert document.render_path == before_render
        # The un-mutated document still deletes and restores faithfully.
        monkeypatch.undo()
        removed = document.delete_page(1)
        document.insert_page(removed)
        assert document.page_count == 3
        assert document.annotations.page(1) == (a,)
    finally:
        document.close()


def test_insert_page_rolls_back_when_the_copy_cannot_be_written(
        tmp_path, monkeypatch):
    path = make_sized_pdf(str(tmp_path / "i.pdf"),
                          [(400.0, 300.0), (400.0, 300.0)])
    document = _open(path)
    try:
        a = _seed(document, 1, 50.0, 50.0)
        removed = document.delete_page(1)
        assert document.page_count == 1
        before_render = document.render_path
        _fail_save(monkeypatch, document)
        with pytest.raises(RuntimeError):
            document.insert_page(removed)
        # The insert was undone: one page, no re-added annotation.
        assert document.page_count == 1
        assert document.render_path == before_render
        assert document.annotations.get(a.id) is None
    finally:
        document.close()
