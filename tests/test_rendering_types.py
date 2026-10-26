"""Rendering type tests."""

from core.types import Size
from rendering.types import RenderRequest, RenderResult


def test_render_request_can_be_created():
    req = RenderRequest(page_index=0, target_size=Size(800.0, 600.0), scale=1.5)
    assert req.page_index == 0
    assert req.scale == 1.5
    assert req.target_size.width == 800.0


def test_render_result_structure_is_valid():
    req = RenderRequest(page_index=2)
    res = RenderResult(request=req, size=Size(800.0, 600.0), scale=1.0, success=True)
    assert res.request == req
    assert res.success is True
    assert res.data is None  # M0 placeholder
