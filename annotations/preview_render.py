"""UI-independent annotation snapshot capture + thumbnail overlay render.

The page navigator's previews must show the annotated page — the
rendered PDF content PLUS Inkfall's live annotation overlay. This
module is the bridge: it captures a page's annotations into an
immutable PLAIN-DATA snapshot on the GUI thread (the only thread that
may read the document's ``AnnotationStore``), and renders that snapshot
into a transparent overlay image on a WORKER thread with the same
drawing semantics the live canvas uses — the same pure geometry
primitives (``pressure_widths``, ``smooth_centerline``, ``build_outline``,
``highlighter_band_hulls``), the same pen construction (width in
page-pt scaled uniformly, round caps/joins, dash patterns), the same
winding-fill compositing for highlighter bands and pressure outlines,
and the same shape geometry (rotation about the box center, fills,
endings).

Snapshots are tuples of primitives only — safe to hand to any worker;
the store, the document, Qt widgets, and PyMuPDF handles are never
shared across threads.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from annotations.ink_geometry import (
    build_outline,
    highlighter_band_hulls,
    pressure_widths,
    smooth_centerline,
)
from annotations.models import InkStroke, Highlighter, Shape

__all__ = ["capture_page_annotations", "render_annotation_overlay"]

_PT_PER_MM = 72.0 / 25.4


def _pt(value, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _color(value: str, fallback: str) -> QColor:
    try:
        base = QColor(str(value))
        if base.isValid():
            return base
    except Exception:
        pass
    return QColor(fallback)


def capture_page_annotations(store, page_index: int) -> tuple:
    """Immutable plain-data snapshot of one page's canvas annotations.

    Runs on the GUI thread (the store is single-threaded and owned by
    the document). Covers every annotation type the live canvas paints
    on a page: pen ink (with pressure), highlighters, and shapes.
    StickyNotes have no canvas painting (the viewer never draws them),
    so they are excluded here for pixel-consistency with the canvas.
    Returns an empty tuple for pages without annotations.
    """
    try:
        items = store.page(int(page_index))
    except Exception:
        return ()
    snapshot: list = []
    try:
        for ann in items:
            try:
                if isinstance(ann, InkStroke):
                    points = ann.points or ()
                    if not points:
                        continue
                    snapshot.append((
                        "ink",
                        tuple((float(p.x), float(p.y)) for p in points),
                        str(ann.color),
                        float(ann.width_mm),
                        float(ann.opacity),
                        tuple(float(p) for p in (ann.pressures or ())),
                        bool(ann.pressure_enabled),
                        str(getattr(ann, "pressure_profile", "normal")),
                    ))
                elif isinstance(ann, Highlighter):
                    points = ann.points or ()
                    if not points:
                        continue
                    snapshot.append((
                        "hl",
                        tuple((float(p.x), float(p.y)) for p in points),
                        str(ann.color),
                        float(ann.width_mm),
                        float(ann.opacity),
                        tuple(float(p) for p in (ann.pressures or ())),
                    ))
                elif isinstance(ann, Shape):
                    r = ann.rect
                    rect = (float(r.x0), float(r.y0),
                            float(r.x1), float(r.y1))
                    snapshot.append((
                        "shape",
                        str(getattr(ann.kind, "value", ann.kind)),
                        rect,
                        str(ann.color),
                        float(ann.width_mm),
                        float(ann.opacity),
                        bool(ann.fill_enabled),
                        str(ann.fill_color),
                        float(ann.fill_opacity),
                        str(ann.line_style),
                        tuple((float(p.x), float(p.y))
                              for p in (ann.points or ())),
                        bool(ann.close_path),
                        float(getattr(ann, "rotation_deg", 0.0)),
                        str(getattr(ann, "start_ending", "none")),
                        str(getattr(ann, "end_ending", "none")),
                    ))
                # Other AnnotationBase subtypes (StickyNote) have no
                # canvas painting in the viewer — excluded on purpose.
            except Exception:
                continue
    except Exception:
        return tuple(snapshot)
    return tuple(snapshot)


def _pen_color(color: str, fallback: str, opacity: float) -> QColor:
    base = _color(color, fallback)
    base.setAlphaF(max(0.0, min(1.0, opacity)))
    return base


def _smooth(points, widths):
    """Smoothed centerline via the shared geometry (never raises)."""
    try:
        spts, swidths = smooth_centerline(points, widths)
        return spts, swidths
    except ValueError:
        return tuple(points), widths


def _paint_ink(painter: QPainter, item, factor: float) -> None:
    (_tag, points, color, width_mm, opacity, pressures,
     pressure_enabled, profile) = item
    if len(points) < 1:
        return
    base = _pen_color(color, "#000000", opacity)
    base_pt = max(0.1, _pt(width_mm, 0.5) * _PT_PER_MM)
    if pressure_enabled and pressures and len(pressures) == len(points):
        try:
            widths = pressure_widths(pressures, base_pt, profile)
            smooth_pts, smooth_widths = _smooth(points, widths)
            outline = build_outline(smooth_pts, smooth_widths)
        except ValueError:
            outline = None
        if outline is not None:
            painter.save()
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing,
                                      True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(base)
                if outline.polygon:
                    poly = QPolygonF([
                        QPointF(float(p.x) * factor, float(p.y) * factor)
                        for p in outline.polygon])
                    path = QPainterPath()
                    path.addPolygon(poly)
                    path.setFillRule(Qt.FillRule.WindingFill)
                    painter.fillPath(path, base)
                for dot in (list(outline.dots) + list(outline.discs)):
                    d = float(dot.radius) * factor
                    painter.drawEllipse(
                        QPointF(float(dot.center.x) * factor,
                                float(dot.center.y) * factor), d, d)
            finally:
                painter.restore()
            return
    # Legacy scalar-width path (the viewer's fallback semantics).
    spts, _sw = _smooth(points, None)
    pen = QPen(base)
    pen.setWidthF(max(1.0, base_pt * factor))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if len(spts) == 1:
            x, y = spts[0]
            painter.drawPoint(QPointF(x * factor, y * factor))
            return
        poly = QPolygonF([QPointF(x * factor, y * factor)
                          for x, y in spts])
        painter.drawPolyline(poly)
    finally:
        painter.restore()


def _paint_highlighter(painter: QPainter, item, factor: float) -> None:
    (_tag, points, color, width_mm, opacity, pressures) = item
    if len(points) < 1:
        return
    base = _pen_color(color, "#FFFF00", opacity)
    thickness_pt = max(0.1, _pt(width_mm, 3.0) * _PT_PER_MM)
    widths = None
    if pressures and len(pressures) == len(points):
        try:
            mapped = pressure_widths(pressures, thickness_pt)
            if mapped and len(mapped) == len(points):
                widths = mapped
        except ValueError:
            widths = None
    spts, swidths = _smooth(points, widths)
    if swidths is not None and len(swidths) != len(spts):
        swidths = None
    try:
        hulls = highlighter_band_hulls(spts, thickness_pt, widths=swidths)
    except ValueError:
        return
    if not hulls:
        return
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(base)
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        for ring in hulls:
            first = True
            poly = QPolygonF()
            for x, y in ring:
                poly.append(QPointF(x * factor, y * factor))
                first = False
            if poly.count() >= 3:
                sub = QPainterPath()
                sub.addPolygon(poly)
                sub.closeSubpath()
                path = path.united(sub) if not path.isEmpty() else sub
        painter.drawPath(path)
    finally:
        painter.restore()


def _stroke_pen(color: str, width_mm: float, opacity: float,
                line_style: str, factor: float):
    base = _pen_color(color, "#000000", opacity)
    pen = QPen(base)
    pen.setWidthF(max(1.0, _pt(width_mm, 0.5) * _PT_PER_MM * factor))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if line_style == "dashed":
        pen.setDashPattern([3.0, 2.0])
    elif line_style == "dotted":
        pen.setDashPattern([1.0, 2.0])
    return pen, base


def _draw_ending(painter: QPainter, ending: str, tip, direction,
                 base: QColor, width_px: float, size: float) -> None:
    ending = str(ending or "none")
    if ending in ("", "none"):
        return
    dx, dy = direction
    length = math.hypot(dx, dy)
    ux, uy = (1.0, 0.0) if length <= 0.0 else (dx / length, dy / length)
    if ending == "dot":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(base))
        r = max(2.0, size / 2.0)
        painter.drawEllipse(tip, r, r)
    elif ending == "bar":
        px, py = -uy, ux
        half = max(3.0, size * 0.7)
        pen = QPen(base)
        pen.setWidthF(max(1.0, width_px))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(tip.x() + px * half, tip.y() + py * half),
                         QPointF(tip.x() - px * half, tip.y() - py * half))
    elif ending == "arrow":
        px, py = -uy, ux
        back = max(6.0, size)
        wing = back * 0.5
        pen = QPen(base)
        pen.setWidthF(max(1.0, width_px))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        bx = tip.x() - ux * back
        by = tip.y() - uy * back
        painter.drawLine(QPointF(tip.x(), tip.y()),
                         QPointF(bx + px * wing, by + py * wing))
        painter.drawLine(QPointF(tip.x(), tip.y()),
                         QPointF(bx - px * wing, by + py * wing))


def _paint_shape(painter: QPainter, item, factor: float) -> None:
    (_tag, kind, rect, color, width_mm, opacity, fill_enabled, fill_color,
     fill_opacity, line_style, points, close_path, rotation_deg,
     start_ending, end_ending) = item
    pen, base = _stroke_pen(color, width_mm, opacity, line_style, factor)
    width_px = pen.widthF()
    brush = QBrush(Qt.BrushStyle.NoBrush)
    closed_ok = (kind != "polygon" or bool(close_path))
    if (fill_enabled and closed_ok
            and kind in ("rectangle", "ellipse", "polygon")):
        fc = _color(fill_color, "#ffffff")
        fc.setAlphaF(max(0.0, min(1.0, fill_opacity)))
        brush = QBrush(fc)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen)
        painter.setBrush(brush)
        x0, y0, x1, y1 = rect
        if kind in ("rectangle", "ellipse"):
            deg = _pt(rotation_deg, 0.0)
            if deg % 360.0 != 0.0:
                cx = (x0 + x1) / 2.0 * factor
                cy = (y0 + y1) / 2.0 * factor
                painter.translate(cx, cy)
                painter.rotate(deg)
                painter.translate(-cx, -cy)
            box = QRectF(x0 * factor, y0 * factor,
                         (x1 - x0) * factor, (y1 - y0) * factor)
            if kind == "rectangle":
                painter.drawRect(box)
            else:
                painter.drawEllipse(box)
        elif kind == "polygon":
            pts = [QPointF(p[0] * factor, p[1] * factor)
                   for p in points]
            if len(pts) >= 2:
                if bool(close_path):
                    painter.drawPolygon(QPolygonF(pts))
                else:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPolyline(QPolygonF(pts))
        else:  # line / arrow
            if len(points) >= 2:
                p0 = QPointF(points[0][0] * factor, points[0][1] * factor)
                p1 = QPointF(points[1][0] * factor, points[1][1] * factor)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawLine(p0, p1)
                dx = p1.x() - p0.x()
                dy = p1.y() - p0.y()
                size = max(6.0, width_px * 3.0)
                _draw_ending(painter, start_ending, p0, (-dx, -dy),
                             base, width_px, size)
                _draw_ending(painter, end_ending, p1, (dx, dy),
                             base, width_px, size)
    finally:
        painter.restore()


def render_annotation_overlay(snapshot: tuple, width: int, height: int,
                              page_w_pt: float, page_h_pt: float) -> QImage:
    """Render one page's annotation snapshot into a TRANSPARENT overlay.

    ``width``/``height`` must be the base PDF thumbnail's pixel
    dimensions; ``page_w_pt``/``page_h_pt`` its displayed page size in
    points (rotation composed). One uniform scale factor maps page-pt
    annotation geometry into the overlay, so annotations scale exactly
    with the page image and never stretch independently. Runs on a
    worker thread (QImage + QPainter are thread-safe; no QPixmap, no
    widgets).
    """
    width = max(1, int(width))
    height = max(1, int(height))
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    if not snapshot:
        return image
    try:
        page_w = _pt(page_w_pt, 0.0)
        page_h = _pt(page_h_pt, 0.0)
        if page_w <= 0 or page_h <= 0:
            return image
        factor = min(width / page_w, height / page_h)
        if factor <= 0:
            return image
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Clip to the page bounds: annotation geometry beyond the
            # media box never leaks onto the gutter.
            painter.setClipRect(QRectF(0.0, 0.0, float(width),
                                       float(height)))
            for item in snapshot:
                try:
                    tag = item[0]
                    if tag == "ink":
                        _paint_ink(painter, item, factor)
                    elif tag == "hl":
                        _paint_highlighter(painter, item, factor)
                    elif tag == "shape":
                        _paint_shape(painter, item, factor)
                except Exception:
                    continue
        finally:
            try:
                painter.end()
            except Exception:
                pass
    except Exception:
        return image
    return image
