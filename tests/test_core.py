"""Core tests: IDs, geometry, coordinates, diagnostics."""

import time

from core.diagnostics import Timer, get_rss_mb
from core.types import (
    AnnotationId,
    DocumentId,
    PageId,
    Point,
    Rect,
    Size,
    ToolId,
    Transform,
    Viewport,
)


def test_ids_can_be_created_and_are_unique():
    ids = {str(DocumentId()) for _ in range(10)}
    assert len(ids) == 10
    assert str(PageId())
    assert str(AnnotationId())
    assert str(ToolId())


def test_geometry_types_work():
    p = Point(1.0, 2.0)
    assert (p.x, p.y) == (1.0, 2.0)
    s = Size(10.0, 20.0)
    assert (s.width, s.height) == (10.0, 20.0)
    r = Rect(Point(0.0, 0.0), Size(10.0, 10.0))
    assert r.contains(Point(5.0, 5.0))
    assert not r.contains(Point(11.0, 5.0))
    assert r.x1 == 10.0 and r.y1 == 10.0


def test_coordinate_types_and_mapping():
    vp = Viewport(size=Size(800.0, 600.0), scale=2.0, offset=Point(10.0, 5.0))
    doc_pt = Point(15.0, 10.0)
    view_pt = vp.document_to_viewport(doc_pt)
    assert view_pt == Point(10.0, 10.0)
    assert vp.viewport_to_document(view_pt) == doc_pt

    t = Transform(scale=2.0, translation=Point(1.0, 1.0))
    assert t.apply(Point(3.0, 4.0)) == Point(7.0, 9.0)


def test_diagnostics_timer_and_rss():
    with Timer() as t:
        time.sleep(0.01)
    assert t.elapsed > 0.0
    rss = get_rss_mb()
    assert isinstance(rss, float)
    assert rss >= 0.0
