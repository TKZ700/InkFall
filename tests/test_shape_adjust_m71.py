"""M7.1 Phase A: live line-endpoint adjustment geometry (Qt-free).

Pure helper coverage: endpoint selection, immutable anchor, absolute
(no-drift) updates, minimum-segment rejection, degenerate input, and the
preview-space rebase. Viewer lifecycle coverage lives in
``test_line_adjust_m71.py``.
"""

from __future__ import annotations

import math

import pytest

from annotations.shape_adjust import (
    MIN_LINE_LENGTH_PT,
    LineAdjustment,
    adjusted_line,
    begin_line_adjustment,
    centroid,
    rebase_line,
)
from annotations.shape_recognize import MIN_SEGMENT_PT, ShapeCandidate
from core.types import Point


def _line(p0=(10.0, 20.0), p1=(210.0, 20.0), confidence=0.75):
    return ShapeCandidate(kind="line", contour=(p0, p1), closed=False,
                          confidence=confidence)


def _rect():
    return ShapeCandidate(kind="rectangle",
                          contour=((0.0, 0.0), (10.0, 0.0),
                                   (10.0, 10.0), (0.0, 10.0)),
                          closed=True)


# -- centroid ---------------------------------------------------------------

def test_centroid_of_pairs_and_points():
    assert centroid([(0.0, 0.0), (10.0, 20.0)]) == (5.0, 10.0)
    assert centroid([Point(0.0, 0.0), Point(10.0, 20.0)]) == (5.0, 10.0)
    assert centroid([(0.0, 0.0), ("bad", 1.0), (4.0, 8.0)]) == (2.0, 4.0)
    assert centroid([]) is None
    assert centroid(None) is None


# -- endpoint selection / immutable anchor ----------------------------------

def test_moving_endpoint_is_nearest_the_recognition_pointer():
    candidate = _line()
    near_end = begin_line_adjustment(candidate, (204.0, 22.0))
    assert near_end is not None
    assert near_end.moving_index == 1
    assert near_end.moving == (210.0, 20.0)
    assert near_end.anchor == (10.0, 20.0)
    near_start = begin_line_adjustment(candidate, (13.0, 18.0))
    assert near_start is not None
    assert near_start.moving_index == 0
    assert near_start.moving == (10.0, 20.0)
    assert near_start.anchor == (210.0, 20.0)


def test_endpoint_choice_is_frozen_across_frames():
    candidate = _line(confidence=0.4)
    adjustment = begin_line_adjustment(candidate, (205.0, 20.0))
    assert adjustment is not None
    assert adjustment.confidence == pytest.approx(0.4)
    for pointer in ((150.0, 60.0), (300.0, -40.0), (205.0, 20.0)):
        result = adjusted_line(adjustment, pointer)
        assert result is not None
        assert result.kind == "line"
        assert result.closed is False
        assert result.confidence == pytest.approx(0.4)
        assert result.contour[0] == adjustment.anchor
        assert adjustment.moving_index == 1


def test_tie_prefers_the_recorded_end_endpoint():
    candidate = _line((0.0, 0.0), (100.0, 0.0))
    adjustment = begin_line_adjustment(candidate, (50.0, 0.0))
    assert adjustment is not None
    assert adjustment.moving_index == 1
    assert adjustment.moving == (100.0, 0.0)


def test_start_side_move_preserves_contour_order():
    candidate = _line((10.0, 20.0), (210.0, 20.0))
    adjustment = begin_line_adjustment(candidate, (12.0, 21.0))
    assert adjustment is not None
    assert adjustment.moving_index == 0
    frame = adjusted_line(adjustment, (0.0, 90.0))
    assert frame is not None
    assert frame.contour[0] == (-2.0, 89.0)   # moved start stays first
    assert frame.contour[1] == (210.0, 20.0)  # anchor stays second


# -- absolute updates (no cumulative drift) ---------------------------------

def test_updates_are_absolute_not_incremental():
    candidate = _line()
    adjustment = begin_line_adjustment(candidate, (210.0, 20.0))
    assert adjustment is not None
    first = adjusted_line(adjustment, (250.0, 70.0))
    assert first is not None
    assert first.contour[1] == (250.0, 70.0)  # 210+40, 20+50
    # A second call must be derived from the base geometry, never from
    # the first result.
    second = adjusted_line(adjustment, (230.0, 20.0))
    assert second is not None
    assert second.contour[1] == (230.0, 20.0)
    # Returning to the origin restores the recognized line exactly.
    restored = adjusted_line(adjustment, adjustment.origin)
    assert restored is not None
    assert restored.contour == candidate.contour
    # Repeated identical calls are idempotent.
    again = adjusted_line(adjustment, (250.0, 70.0))
    assert again is not None
    assert again.contour == first.contour


def test_anchor_is_immutable_and_origin_does_not_change():
    candidate = _line()
    adjustment = begin_line_adjustment(candidate, (210.0, 20.0))
    assert adjustment is not None
    origin = adjustment.origin
    anchor = adjustment.anchor
    for pointer in ((0.0, 0.0), (400.0, 300.0), (210.0, 20.0)):
        frame = adjusted_line(adjustment, pointer)
        assert frame is not None
        assert adjustment.origin == origin
        assert adjustment.anchor == anchor
        assert frame.contour[0] == anchor
    # LineAdjustment is frozen.
    with pytest.raises(Exception):
        adjustment.anchor = (0.0, 0.0)


# -- rejection --------------------------------------------------------------

def test_minimum_segment_length_rejects_degenerate_frames():
    candidate = _line((0.0, 0.0), (200.0, 0.0))
    adjustment = begin_line_adjustment(candidate, (200.0, 0.0))
    assert adjustment is not None
    assert MIN_LINE_LENGTH_PT == pytest.approx(MIN_SEGMENT_PT)
    assert adjusted_line(adjustment, (2.0, 0.0)) is None   # length 2
    assert adjusted_line(adjustment, (0.0, 3.0)) is None   # length 3
    just_ok = adjusted_line(adjustment, (MIN_SEGMENT_PT + 0.01, 0.0))
    assert just_ok is not None
    moving = just_ok.contour[1]
    assert math.hypot(moving[0] - just_ok.contour[0][0],
                      moving[1] - just_ok.contour[0][1]) >= MIN_SEGMENT_PT


def test_begin_rejects_degenerate_inputs():
    assert begin_line_adjustment(_line(), None) is None
    assert begin_line_adjustment(_line(), (float("nan"), 0.0)) is None
    assert begin_line_adjustment(_line(), (float("inf"), 0.0)) is None
    assert begin_line_adjustment(_rect(), (10.0, 0.0)) is None
    assert begin_line_adjustment(
        ShapeCandidate(kind="line", contour=((0.0, 0.0),), closed=False),
        (0.0, 0.0)) is None
    # Already below the recognized minimum.
    assert begin_line_adjustment(
        ShapeCandidate(kind="line", contour=((0.0, 0.0), (3.0, 0.0)),
                       closed=False), (3.0, 0.0)) is None
    # A closed "line" candidate is not an adjustable open line.
    assert begin_line_adjustment(
        ShapeCandidate(kind="line", contour=((0.0, 0.0), (50.0, 0.0)),
                       closed=True), (50.0, 0.0)) is None


def test_adjusted_line_rejects_unusable_state_and_pointer():
    adjustment = begin_line_adjustment(_line(), (210.0, 20.0))
    assert adjustment is not None
    assert adjusted_line(adjustment, None) is None
    assert adjusted_line(adjustment, (float("nan"), 0.0)) is None
    assert adjusted_line(None, (0.0, 0.0)) is None
    assert adjusted_line({"anchor": (0.0, 0.0)}, (0.0, 0.0)) is None
    assert not isinstance({"anchor": (0.0, 0.0)}, LineAdjustment)


# -- preview-space rebase ---------------------------------------------------

def test_rebase_line_translates_and_preserves_the_original():
    candidate = _line((10.0, 20.0), (210.0, 20.0), confidence=0.6)
    rebased = rebase_line(candidate, (0.0, 3.5))
    assert rebased is not None
    assert rebased is not candidate
    assert rebased.kind == "line"
    assert rebased.closed is False
    assert rebased.confidence == pytest.approx(0.6)
    assert rebased.contour == ((10.0, 23.5), (210.0, 23.5))
    # The recognized candidate is frozen and never mutated.
    assert candidate.contour == ((10.0, 20.0), (210.0, 20.0))


def test_rebase_rejects_non_lines_and_falls_back_on_bad_offsets():
    assert rebase_line(_rect(), (0.0, 1.0)) is None
    candidate = _line()
    assert rebase_line(candidate, None).contour == candidate.contour
    assert rebase_line(candidate, (float("nan"), 0.0)).contour == \
        candidate.contour
