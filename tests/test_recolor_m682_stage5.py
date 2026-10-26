"""M6.8.2 Stage 5: Qt-free stroke/fill recolor policy.

``annotations.recolor`` is the single source of truth for *which*
annotations a color operation touches and *what* candidate replaces each
one. These tests pin the semantics the viewer's context menu and keyboard
paths rely on:

* stroke color applies to ink, highlighters, and every shape kind, and
  changes **only** the outline color — geometry, points, rotation, width,
  opacity/alpha, pressure samples, page assignment, identity, and every
  unrelated field survive;
* a recolored highlighter stays a highlighter with its transparency
  intact (it never becomes a pen);
* fill color applies only to fill-capable shapes (rectangle, ellipse,
  polygon) and changes **only** ``fill_color`` — never the outline, never
  ``fill_enabled``/``fill_opacity``;
* a request the annotation does not support, or that would not change the
  stored value (case-insensitively), returns ``None`` so the caller drops
  it and no history entry is opened.
"""

from __future__ import annotations

import dataclasses

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind, StickyNote
from annotations.recolor import (
    FILL_SHAPE_KINDS,
    STROKE_KINDS,
    recolored,
    supports_fill_color,
    supports_stroke_color,
)
from core.types import Point, Rect, Size


def _stroke(**kw):
    base = dict(points=(Point(100.0, 100.0), Point(300.0, 200.0)),
                color="#e01b24", width_mm=2.5, opacity=0.8)
    base.update(kw)
    return InkStroke(**base)


def _highlighter(**kw):
    base = dict(points=(Point(120.0, 140.0), Point(280.0, 140.0)),
                color="#f5d90a", width_mm=4.0, opacity=0.4)
    base.update(kw)
    return Highlighter(**base)


def _box(kind=ShapeKind.RECTANGLE, **kw):
    base = dict(kind=kind,
                rect=Rect(Point(100.0, 100.0), Size(120.0, 80.0)),
                color="#000000", width_mm=1.0,
                fill_enabled=True, fill_color="#ffffff", fill_opacity=0.5,
                rotation_deg=30.0)
    base.update(kw)
    return Shape(**base)


def _polygon(**kw):
    base = dict(kind=ShapeKind.POLYGON,
                points=(Point(0.0, 0.0), Point(50.0, 0.0), Point(50.0, 40.0)),
                color="#123456", width_mm=1.5, close_path=True,
                fill_enabled=True, fill_color="#abcdef")
    base.update(kw)
    return Shape(**base)


# ----------------------------------------------------------- applicability

def test_stroke_support_covers_ink_highlighter_and_all_shapes():
    assert supports_stroke_color(_stroke())
    assert supports_stroke_color(_highlighter())
    for kind in STROKE_KINDS:
        assert supports_stroke_color(_box(kind=kind))
    with_pts = _box(kind=ShapeKind.LINE, points=(Point(0.0, 0.0),
                                                 Point(10.0, 10.0)))
    assert supports_stroke_color(with_pts)


def test_stroke_support_rejects_notes_and_missing():
    assert not supports_stroke_color(StickyNote())
    assert not supports_stroke_color(None)


def test_fill_support_is_only_fillable_shapes():
    assert supports_fill_color(_box(ShapeKind.RECTANGLE))
    assert supports_fill_color(_box(ShapeKind.ELLIPSE))
    assert supports_fill_color(_polygon())
    assert not supports_fill_color(_box(ShapeKind.LINE))
    assert not supports_fill_color(_box(ShapeKind.ARROW))
    assert not supports_fill_color(_stroke())
    assert not supports_fill_color(_highlighter())
    assert not supports_fill_color(StickyNote())
    assert not supports_fill_color(None)
    assert set(FILL_SHAPE_KINDS) == {ShapeKind.RECTANGLE, ShapeKind.ELLIPSE,
                                     ShapeKind.POLYGON}


# ------------------------------------------------------------ stroke recolor

def test_stroke_recolor_preserves_geometry_and_unrelated_fields():
    original = _stroke(pressures=(0.2, 0.9), pressure_enabled=True)
    candidate = recolored(original, stroke="#2b6cff")
    assert candidate is not None
    assert candidate.color == "#2b6cff"
    # Everything except color is identical (identity included).
    assert dataclasses.replace(candidate, color=original.color) == original
    assert candidate.id == original.id
    assert candidate.page_id == original.page_id
    assert candidate.points == original.points
    assert candidate.width_mm == original.width_mm
    assert candidate.opacity == original.opacity
    assert candidate.pressures == original.pressures
    assert candidate.pressure_enabled is True


def test_highlighter_recolor_stays_highlighter_with_alpha():
    original = _highlighter()
    candidate = recolored(original, stroke="#30d158")
    assert isinstance(candidate, Highlighter)
    assert not isinstance(candidate, InkStroke)
    assert candidate.color == "#30d158"
    assert candidate.opacity == original.opacity == 0.4
    assert candidate.width_mm == original.width_mm


def test_shape_stroke_recolor_leaves_fill_and_rotation_untouched():
    original = _box(rotation_deg=45.0)
    candidate = recolored(original, stroke="#ff0000")
    assert candidate.color == "#ff0000"
    assert candidate.fill_color == original.fill_color
    assert candidate.fill_enabled == original.fill_enabled
    assert candidate.fill_opacity == original.fill_opacity
    assert candidate.rotation_deg == original.rotation_deg


def test_stroke_recolor_returns_none_for_unsupported_and_noop():
    assert recolored(StickyNote(), stroke="#000000") is None
    assert recolored(None, stroke="#000000") is None
    # Same value (case/space-insensitive) is a true no-op.
    assert recolored(_stroke(color="#e01b24"), stroke="#E01B24") is None
    assert recolored(_stroke(color="#e01b24"), stroke="  #e01b24  ") is None
    # A real change is not a no-op.
    assert recolored(_stroke(color="#e01b24"), stroke="#e01b25") is not None


# -------------------------------------------------------------- fill recolor

def test_fill_recolor_changes_only_fill_color():
    original = _box(fill_enabled=False, fill_opacity=0.25)
    candidate = recolored(original, fill="#8e4ec6")
    assert candidate is not None
    assert candidate.fill_color == "#8e4ec6"
    # Outline and every other fill property are untouched.
    assert candidate.color == original.color
    assert candidate.fill_enabled is False
    assert candidate.fill_opacity == 0.25
    assert candidate.rotation_deg == original.rotation_deg
    assert dataclasses.replace(candidate,
                               fill_color=original.fill_color) == original


def test_fill_recolor_on_polygon():
    original = _polygon()
    candidate = recolored(original, fill="#000000")
    assert candidate is not None
    assert candidate.fill_color == "#000000"
    assert candidate.color == original.color
    assert candidate.close_path is True


def test_fill_recolor_returns_none_for_non_fillable_and_noop():
    assert recolored(_box(ShapeKind.LINE), fill="#000000") is None
    assert recolored(_box(ShapeKind.ARROW), fill="#000000") is None
    assert recolored(_stroke(), fill="#000000") is None
    assert recolored(_highlighter(), fill="#000000") is None
    assert recolored(_box(fill_color="#ffffff"), fill="#FFFFFF") is None


def test_stroke_and_fill_are_independent():
    original = _box(color="#000000", fill_color="#ffffff")
    stroke_only = recolored(original, stroke="#ff0000")
    assert stroke_only.color == "#ff0000"
    assert stroke_only.fill_color == "#ffffff"
    fill_only = recolored(original, fill="#00ff00")
    assert fill_only.color == "#000000"
    assert fill_only.fill_color == "#00ff00"


def test_recolored_with_neither_request_is_none():
    assert recolored(_box()) is None
