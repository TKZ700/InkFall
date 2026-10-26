"""M3-B layout tests: offsets, bisect lookup, rotation, rebuilds."""

from __future__ import annotations

import pytest

from core.types import DocumentId, Size
from rendering.layout import DocumentLayout, effective_rotation, unrotated_size


def _sizes(n: int, w: float = 595.0, h: float = 842.0) -> list[Size]:
    return [Size(w, h) for _ in range(n)]


def test_rotation_truth_table():
    cases = [
        (0, 0, 0), (0, 90, 90), (0, 180, 180), (0, 270, 270),
        (90, 0, 90), (90, 90, 180), (90, 180, 270), (90, 270, 0),
        (180, 0, 180), (180, 90, 270), (180, 180, 0), (180, 270, 90),
        (270, 0, 270), (270, 90, 0), (270, 180, 90), (270, 270, 180),
    ]
    for intrinsic, user, expected in cases:
        assert effective_rotation(intrinsic, user) == expected


def test_unrotated_size_derotation():
    # Mirrors verified PyMuPDF behavior: rect is pre-rotated by /Rotate.
    assert unrotated_size(800.0, 600.0, 90) == (600.0, 800.0)
    assert unrotated_size(800.0, 600.0, 270) == (600.0, 800.0)
    assert unrotated_size(600.0, 800.0, 0) == (600.0, 800.0)
    assert unrotated_size(600.0, 800.0, 180) == (600.0, 800.0)


def test_single_page():
    layout = DocumentLayout(DocumentId("d"), _sizes(1), None, 1.0)
    assert layout.page_count == 1
    assert layout.total_height_px == 842
    assert layout.pages_in(0, 600) == (0,)
    assert (layout.entry(0).render_width_px, layout.entry(0).render_height_px) == (595, 842)


def test_offsets_gaps_total():
    layout = DocumentLayout(DocumentId("d"), _sizes(3), None, 1.0, gap_px=16)
    assert [layout.entry(i).y_offset_px for i in range(3)] == [0, 858, 1716]
    assert layout.total_height_px == 842 * 3 + 16 * 2
    assert layout.pages_in(850, 900) == (1,)
    assert layout.pages_in(843, 857) == ()
    assert len(layout) == 3


def test_variable_and_mixed_sizes():
    sizes = [Size(595.0, 842.0), Size(842.0, 595.0), Size(500.0, 500.0)]
    layout = DocumentLayout(DocumentId("d"), sizes, None, 1.0, gap_px=10)
    assert (layout.entry(1).render_width_px, layout.entry(1).render_height_px) == (842, 595)
    assert layout.entry(2).y_offset_px == 842 + 10 + 595 + 10


def test_intrinsic_rotation_dims_and_offsets():
    layout = DocumentLayout(DocumentId("d"), _sizes(2, 600.0, 800.0), [0, 90], 1.0, gap_px=0)
    assert (layout.entry(0).render_width_px, layout.entry(0).render_height_px) == (600, 800)
    assert (layout.entry(1).render_width_px, layout.entry(1).render_height_px) == (800, 600)
    assert layout.entry(1).y_offset_px == 800


def test_user_rotation_composes_per_page():
    layout = DocumentLayout(
        DocumentId("d"), _sizes(2, 600.0, 800.0), [90, 0], 1.0, user_rotation=90, gap_px=0
    )
    assert layout.entry(0).effective_rotation == 180
    assert layout.entry(1).effective_rotation == 90
    assert (layout.entry(1).render_width_px, layout.entry(1).render_height_px) == (800, 600)


def test_scale_rebuild_doubles_offsets():
    small = DocumentLayout(DocumentId("d"), _sizes(2), None, 1.0, gap_px=0)
    big = DocumentLayout(DocumentId("d"), _sizes(2), None, 2.0, gap_px=0)
    assert big.entry(1).y_offset_px == 2 * small.entry(1).y_offset_px


def test_page_viewport_mapping():
    layout = DocumentLayout(DocumentId("d"), _sizes(2), None, 1.0, gap_px=16)
    vs = layout.page_viewport(1, 900.0, 800, 600, generation=4)
    assert vs.page_index == 1
    assert vs.scroll_y == pytest.approx(900.0 - 858.0)
    assert vs.scroll_x == 0.0
    assert vs.generation == 4


def test_page_ids_deterministic():
    a = DocumentLayout(DocumentId("d"), _sizes(2), None, 1.0)
    b = DocumentLayout(DocumentId("d"), _sizes(2), None, 1.0)
    assert a.entry(0).page_id == b.entry(0).page_id


def test_invalid_inputs():
    with pytest.raises(ValueError):
        DocumentLayout(DocumentId("d"), _sizes(1), None, 0.0)
    with pytest.raises(ValueError):
        DocumentLayout(DocumentId("d"), _sizes(1), None, 1.0, user_rotation=45)
    with pytest.raises(ValueError):
        DocumentLayout(DocumentId("d"), _sizes(2), [0], 1.0)
    with pytest.raises(ValueError):
        DocumentLayout(DocumentId("d"), _sizes(1), None, 1.0, gap_px=-1)


def test_six_hundred_pages_scalar_only():
    layout = DocumentLayout(DocumentId("d"), _sizes(600), None, 1.0, gap_px=16)
    assert layout.page_count == 600
    assert layout.total_height_px == 600 * 842 + 599 * 16
    assert layout.pages_in(0, 600) == (0,)
    mid = layout.entry(300).y_offset_px
    assert layout.pages_in(mid, mid + 600) == (300,)
    assert layout.pages_in(10**9, 10**9 + 600) == ()
    # No bitmaps: entries carry numbers only.
    assert layout.entry(0).render_width_px == 595
