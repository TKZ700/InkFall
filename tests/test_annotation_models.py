"""Annotation model tests."""

from annotations.models import Highlighter, InkStroke, TextAssist
from core.types import AnnotationId, PageId, Point


def test_ink_stroke_can_be_represented():
    stroke = InkStroke(
        page_id=PageId(),
        points=(Point(0.0, 0.0), Point(10.0, 10.0)),
        color="#000000",
        width_mm=0.5,
    )
    assert len(stroke.points) == 2
    assert stroke.width_mm == 0.5


def test_highlighter_without_text_data():
    # Must NOT require text extraction.
    hl = Highlighter(
        page_id=PageId(),
        points=(Point(0.0, 0.0), Point(20.0, 0.0)),
        color="#FFFF00",
        opacity=0.4,
    )
    assert hl.text_assist is None
    assert hl.opacity == 0.4


def test_highlighter_with_optional_text_assist():
    hl = Highlighter(
        page_id=PageId(),
        points=(Point(0.0, 0.0),),
        text_assist=TextAssist(char_count=5),
    )
    assert hl.text_assist is not None
    assert hl.text_assist.char_count == 5


def test_annotation_ids_are_stable():
    fixed = AnnotationId("abc123")
    s1 = InkStroke(id=fixed, points=())
    s2 = InkStroke(id=fixed, points=())
    assert s1.id == s2.id
    assert str(s1.id) == "abc123"
