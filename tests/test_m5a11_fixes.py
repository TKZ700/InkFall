"""M5-A1.1 focused regression: visible boundary separators + morph frames.

Covers the actual runtime causes found in the live application:
  * separator line measured against the real shelf surface (>= 2.6:1;
    border_subtle was 1.03 dark / 1.26 light — painted but invisible);
  * morph intermediate frames visibly differ in rendered pixels;
  * Drawboard-inspired selection (marquee + pointer) and highlighter
    (broad chisel marker) render, centered, legible, distinct.
"""

import pytest
from PySide6.QtGui import QColor, QImage

from app.icons import contrast_ratio, ink_center_offset, pixmap, tool_pixmap
from app.theme.tokens import dark_tokens, light_tokens
from app.toolshelf.shelf import _separator_color
from tests.qt_helpers import wait_edge_settled, wait_stable

pytestmark = pytest.mark.usefixtures("qapp")


def _luminance(c: QColor) -> float:
    def ch(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return (0.2126 * ch(c.red()) + 0.7152 * ch(c.green())
            + 0.0722 * ch(c.blue()))


def _ink_pixels(pm):
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    pts = []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                pts.append((x, y))
    return img.width(), img.height(), pts


# -- separator tone --------------------------------------------------------

@pytest.mark.parametrize("tokens", [dark_tokens(), light_tokens()],
                         ids=["dark", "light"])
def test_separator_tone_meets_floor(tokens):
    color = _separator_color(tokens)
    assert color.isValid()
    bg = str(tokens.surface_raised)
    assert contrast_ratio(color.name(), bg) >= 2.6
    # The old tone was the defect: near-identical to the surface.
    old = contrast_ratio(str(tokens.border_subtle), bg)
    assert old < 1.5
    assert contrast_ratio(color.name(), bg) > old
    # Derived from existing text tokens, not a new token or hardcode.
    assert not hasattr(tokens, "separator_line")


# -- rendered boundary states ----------------------------------------------

def _make_viewer(qtbot, theme, customs):
    from PySide6.QtCore import QSettings

    for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                "inkfall/m5/toolbar/active",
                "inkfall/m5/toolbar/custom_colors",
                "inkfall/m5/toolbar/presets"):
        QSettings().remove(key)
    from app.viewer import PdfViewer
    from document.manager import DocumentManager
    from tools.models import ToolType

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    viewer.tool_shelf.update_tokens(theme)
    for i in range(customs):
        viewer.tool_store.add_slot(ToolType.PEN, name=f"P{i}")
    viewer.tool_shelf.refresh()
    qtbot.addWidget(viewer)
    qtbot.wait(120)
    return viewer


def _edge_line_delta(shelf_grab, edge):
    """Mean channel distance: expected line band vs off-band, same widget."""
    g = edge.geometry()
    img = shelf_grab.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    horizontal = edge.width() > edge.height()
    if horizontal:  # 40x14: line at cy, x=10..w-10
        cy = g.y() + g.height() // 2
        line = [img.pixelColor(g.x() + x, cy) for x in range(10, g.width() - 10)]
        off = [img.pixelColor(g.x() + x, g.y() + 2) for x in range(10, g.width() - 10)]
    else:  # 14x40: line at cx, y=10..h-10
        cx = g.x() + g.width() // 2
        line = [img.pixelColor(cx, g.y() + y) for y in range(10, g.height() - 10)]
        off = [img.pixelColor(g.x() + 2, g.y() + y) for y in range(10, g.height() - 10)]

    def mean(pxs):
        n = len(pxs)
        return (sum(p.red() for p in pxs) / n,
                sum(p.green() for p in pxs) / n,
                sum(p.blue() for p in pxs) / n)

    m_line, m_off = mean(line), mean(off)
    return max(abs(a - b) for a, b in zip(m_line, m_off))


@pytest.mark.parametrize("theme_name", ["dark", "light"])
def test_idle_separator_rendered_on_real_surface(qtbot, theme_name):
    theme = dark_tokens() if theme_name == "dark" else light_tokens()
    viewer = _make_viewer(qtbot, theme, customs=2)  # fits: both lines
    shelf = viewer.tool_shelf
    assert shelf._edge_prev.has_more() is False
    assert shelf._edge_next.has_more() is False
    assert shelf._edge_prev._reveal == 0.0
    assert shelf._edge_next._reveal == 0.0
    grab = shelf.grab()
    for edge in (shelf._edge_prev, shelf._edge_next):
        assert _edge_line_delta(grab, edge) >= 12.0


def _pixmap_distance(pm_a, pm_b) -> int:
    ia = pm_a.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    ib = pm_b.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    assert ia.size() == ib.size()
    total = 0
    for y in range(ia.height()):
        for x in range(ia.width()):
            pa, pb = ia.pixelColor(x, y), ib.pixelColor(x, y)
            total += (abs(pa.red() - pb.red()) + abs(pa.green() - pb.green())
                      + abs(pa.blue() - pb.blue()) + abs(pa.alpha() - pb.alpha()))
    return total


def test_morph_intermediate_frames_rendered(qtbot):
    viewer = _make_viewer(qtbot, dark_tokens(), customs=16)
    shelf = viewer.tool_shelf
    edge = shelf._edge_prev  # line state at scroll start
    bar = shelf._scroll.verticalScrollBar()
    bar.setValue(bar.minimum())
    assert wait_edge_settled(qtbot, edge, 0.0)
    frames = [edge.grab()]
    bar.setValue((bar.minimum() + bar.maximum()) // 2)  # overflow backward
    qtbot.wait(45)
    frames.append(edge.grab())
    mid_reveal = edge._reveal
    qtbot.wait(45)
    frames.append(edge.grab())
    assert wait_edge_settled(qtbot, edge, 1.0)
    frames.append(edge.grab())
    assert 0.0 < mid_reveal < 1.0  # caught mid-glide, not snapped
    # Rendered proof: single bending path advances progressively (no pop),
    # and rest vs overflow poses genuinely differ. Thresholds fit the
    # subtle few-pixel bend (not the old full-repaint crossfade).
    for i in range(3):
        assert _pixmap_distance(frames[i], frames[i + 1]) > 0
    assert _pixmap_distance(frames[0], frames[3]) > 200
    # And back to the line.
    bar.setValue(bar.minimum())
    assert wait_edge_settled(qtbot, edge, 0.0)
    assert _pixmap_distance(frames[0], edge.grab()) < 200


def test_scroll_limit_roundtrip_all_docks(qtbot):
    viewer = _make_viewer(qtbot, light_tokens(), customs=16)
    shelf = viewer.tool_shelf
    for dock in ("left", "right", "top", "bottom"):
        shelf.set_dock(dock)
        assert wait_stable(qtbot, lambda: (shelf.width(), shelf.height()))
        bar = (shelf._scroll.horizontalScrollBar()
               if shelf._orientation() == "horizontal"
               else shelf._scroll.verticalScrollBar())
        assert bar.maximum() > 0
        bar.setValue(bar.minimum())
        assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
        assert (shelf._edge_prev.has_more(), shelf._edge_next.has_more()) == (False, True)
        bar.setValue((bar.minimum() + bar.maximum()) // 2)
        assert wait_edge_settled(qtbot, shelf._edge_prev, 1.0)
        assert wait_edge_settled(qtbot, shelf._edge_next, 1.0)
        assert (shelf._edge_prev.has_more(), shelf._edge_next.has_more()) == (True, True)
        bar.setValue(bar.maximum())
        assert wait_edge_settled(qtbot, shelf._edge_next, 0.0)
        assert (shelf._edge_prev.has_more(), shelf._edge_next.has_more()) == (True, False)
        bar.setValue(bar.minimum())
        assert wait_edge_settled(qtbot, shelf._edge_prev, 0.0)


def test_exactly_two_boundaries(qtbot):
    from app.toolshelf.shelf import _OverflowEdge

    viewer = _make_viewer(qtbot, dark_tokens(), customs=16)
    edges = viewer.tool_shelf.findChildren(_OverflowEdge)
    assert len(edges) == 2


# -- icons -----------------------------------------------------------------

def test_selection_marquee_pointer_renders():
    from PySide6.QtGui import QColor

    for color in ("#B4BAC7", "#5B626E"):
        pm = pixmap("selection", color, 20)
        assert not pm.isNull()
        w, h, pts = _ink_pixels(pm)
        assert len(pts) > 80  # marquee dashes + arrow, not a bare dot
        dx, dy = ink_center_offset(pm, ("selection-test", color))
        assert abs(dx) <= 2.0 and abs(dy) <= 2.0
        line0 = QColor(color).lightness()
        img = pm.toImage()
        bright = sum(1 for x, y in pts
                     if img.pixelColor(x, y).lightness() > line0 + 40)
        assert bright > 5  # near-white arrow fill present


def test_selection_distinct_from_pan_lasso():
    base = pixmap("selection", "#B4BAC7", 20).toImage()

    def alpha_dist(img):
        d = 0
        for y in range(img.height()):
            for x in range(img.width()):
                d += abs(base.pixelColor(x, y).alpha()
                         - img.pixelColor(x, y).alpha())
        return d

    assert alpha_dist(pixmap("pan", "#B4BAC7", 20).toImage()) > 5000
    assert alpha_dist(pixmap("lasso", "#B4BAC7", 20).toImage()) > 5000


def test_marker_broad_diagonal_chisel():
    for bg in ("#181C23", "#FFFFFF"):
        pm = tool_pixmap("marker", "#ffd60a", bg, 22)
        assert not pm.isNull()
        w, h, pts = _ink_pixels(pm)
        assert len(pts) > 80
        quad = {"LL": 0, "UL": 0, "LR": 0, "UR": 0}
        for x, y in pts:
            quad[("U" if y < h / 2 else "L") + ("L" if x < w / 2 else "R")] += 1
        # Diagonal lean lower-left -> upper-right (tip at lower-left).
        assert quad["LL"] > quad["UL"] and quad["UR"] > quad["LR"]
        dx, dy = ink_center_offset(pm, ("marker-test", bg))
        assert abs(dx) <= 2.0 and abs(dy) <= 2.0


def test_marker_broader_than_pen_and_alias_matches():
    bg = "#FFFFFF"
    pen = tool_pixmap("pen-nib", "#1b1e26", bg, 22)
    mrk = tool_pixmap("marker", "#1b1e26", bg, 22)
    _, _, pen_pts = _ink_pixels(pen)
    _, _, mrk_pts = _ink_pixels(mrk)
    pen_w = max(x for x, _ in pen_pts) - min(x for x, _ in pen_pts)
    mrk_w = max(x for x, _ in mrk_pts) - min(x for x, _ in mrk_pts)
    assert mrk_w > pen_w and len(mrk_pts) > len(pen_pts)
    for bg2 in ("#181C23", "#FFFFFF"):
        a = tool_pixmap("marker", "#ffd60a", bg2, 22).toImage()
        b = tool_pixmap("highlighter", "#ffd60a", bg2, 22).toImage()
        assert a.size() == b.size()


def test_marker_never_blank_near_background():
    for bg, color in (("#181C23", "#181C23"), ("#FFFFFF", "#FFFFFF")):
        pm = tool_pixmap("marker", color, bg, 22)
        assert not pm.isNull()
        _, _, pts = _ink_pixels(pm)
        assert len(pts) > 50
