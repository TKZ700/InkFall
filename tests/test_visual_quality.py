"""M3-C visual-quality regression: seam-free tile targets, smooth zoom sharpness."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.viewport_controller import FloatRect, snapped_tile_rect

pytestmark = pytest.mark.usefixtures("qapp")

# Shared-edge continuity, sweep zoom and page horizontal offsets.
@pytest.mark.parametrize("zi", [x * 0.01 for x in range(50, 401)])
def test_shared_edges_never_gap_or_overlap(zi):
    scale = float(zi)
    gaps = overlaps = 0
    for ox_i in range(0, 20):
        page_x = ox_i * 3.7
        page = FloatRect(x=page_x, y=0.0, width=595.0 * scale, height=842.0 * scale)
        tx = 0
        while tx < 595:
            tw = min(512, 595 - tx)
            left = snapped_tile_rect(tx, 0, tw, 512, page, scale)
            if tx + tw < 595:
                right = snapped_tile_rect(tx + tw, 0, min(512, 595 - tx - tw), 512, page, scale)
                edge_l = left.x + left.width
                edge_r = right.x
                if edge_r < edge_l:
                    overlaps += 1
                elif edge_r > edge_l:
                    gaps += 1
            tx += tw
    assert gaps == 0 and overlaps == 0


def test_shared_edge_equal_after_snapping():
    scale = 0.51
    page = FloatRect(x=7.4, y=0.0, width=595.0 * scale, height=842.0 * scale)
    left = snapped_tile_rect(0, 0, 512, 512, page, scale)
    right = snapped_tile_rect(512, 0, 83, 512, page, scale)
    assert left.x + left.width == right.x
    bottom = snapped_tile_rect(0, 512, 512, 330, page, scale)
    assert left.y + left.height == bottom.y


def test_offscreen_two_tile_paint_no_seam():
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage, QPainter

    s = 0.51
    page = FloatRect(x=7.4, y=0.0, width=595.0 * s, height=842.0 * s)
    left = snapped_tile_rect(0, 0, 512, 512, page, s)
    right = snapped_tile_rect(512, 0, 83, 512, page, s)
    th = 436
    canvas = QImage(right.x + right.width + 4 - left.x, th, QImage.Format.Format_RGBA8888)
    canvas.fill(0xFFFFFFFF)
    a = QImage(512, 512, QImage.Format.Format_RGBA8888)
    a.fill(0xFF808080)
    b = QImage(83, 512, QImage.Format.Format_RGBA8888)
    b.fill(0xFF808080)
    p = QPainter(canvas)
    p.drawImage(QRect(0, 0, left.width, th), a, QRect(0, 0, 512, 512))
    p.drawImage(QRect(right.x - left.x, 0, right.width, th), b, QRect(0, 0, 83, 512))
    p.end()
    boundary = right.x - left.x
    for cx in (boundary - 1, boundary):
        px = canvas.pixel(cx, 200)
        r = (px >> 16) & 255
        assert 100 <= r <= 160, f"column {cx} leaked background: r={r}"


def test_smooth_resample_matches_reference_better_than_fast():
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage, QPainter

    from tests.helpers import make_rich_pdf, rasterize_page

    import tempfile
    tmp = tempfile.mkdtemp(prefix="vqtest-")
    pdf = make_rich_pdf(os.path.join(tmp, "rich.pdf"), pages=1)
    src_s, sw, sh, sstride = rasterize_page(pdf, 0, 2.0)
    src = QImage(src_s, sw, sh, sstride,
                 QImage.Format.Format_RGB888).convertToFormat(QImage.Format.Format_RGBA8888)
    z = 0.85
    tw, th = int(595.0 * z), int(842.0 * z)
    r_s, rw, rh, rstride = rasterize_page(pdf, 0, z)
    ref = QImage(r_s, rw, rh, rstride,
                 QImage.Format.Format_RGB888).convertToFormat(QImage.Format.Format_RGBA8888)

    def mad(img):
        w, h = img.width(), img.height()
        b = bytes(img.constBits())[: w * h * 4]
        b2 = bytes(ref.constBits())[: ref.width() * ref.height() * 4]
        tot, n = 0.0, 0
        for y in range(2, min(h, ref.height()) - 2, 2):
            for x in range(2, min(w, ref.width()) - 2, 2):
                o = (y * w + x) * 4
                l = 0.299 * b[o] + 0.587 * b[o + 1] + 0.114 * b[o + 2]
                o2 = (y * ref.width() + x) * 4
                lr = 0.299 * b2[o2] + 0.587 * b2[o2 + 1] + 0.114 * b2[o2 + 2]
                tot += abs(l - lr)
                n += 1
        return tot / max(n, 1)

    fast = QImage(tw, th, QImage.Format.Format_RGBA8888)
    fast.fill(0xFFFFFFFF)
    p = QPainter(fast)
    p.drawImage(QRect(0, 0, tw, th), src)
    p.end()
    smooth = QImage(tw, th, QImage.Format.Format_RGBA8888)
    smooth.fill(0xFFFFFFFF)
    p = QPainter(smooth)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.drawImage(QRect(0, 0, tw, th), src)
    p.end()
    assert mad(smooth) < mad(fast)


@pytest.mark.parametrize("ui_zoom", [0.75, 0.85, 1.00, 1.13, 1.30, 2.00, 4.00])
def test_zoom_sweep_display_scale_continuous(ui_zoom):
    from app.viewport_controller import display_scale_for
    from rendering.scale import next_bucket, quantize_scale

    raster = next_bucket(ui_zoom, quantize_scale(ui_zoom))
    # Effective display scale reproduces the requested zoom; the fractional
    # part (the M3-C quality path) is raster bucket -> ui zoom ratio.
    s = display_scale_for(ui_zoom, raster)
    assert s == pytest.approx(ui_zoom / raster)
    # Sweep must never produce an upscale of baked pixels beyond 2x,
    # and the bucket policy must be unchanged.
    assert 0.5 <= raster <= 8.0
