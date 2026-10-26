"""Plain native-PDF annotation specifications.

This module translates Inkfall annotation models into a small, PyMuPDF-free
vocabulary consumed by :mod:`document.backend`. Coordinates stay in the
page's displayed frame (the viewer's coordinate space); the backend applies
the page-derotation matrix at insertion time.

The manifest remains authoritative. These specifications are a best-effort
interoperability layer for external PDF viewers; they intentionally do not
try to encode Inkfall-only fidelity that PDF annotations cannot represent.
"""

from __future__ import annotations

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind, StickyNote

__all__ = ["NATIVE_SUBJECT_PREFIX", "NATIVE_TITLE", "native_specs"]

#: Native annotations created by Inkfall carry this subject prefix followed
#: by their stable Inkfall annotation identity.
NATIVE_SUBJECT_PREFIX = "inkfall:"
NATIVE_TITLE = "Inkfall"

_MM_TO_PT = 72.0 / 25.4


def _width_pt(width_mm) -> float:
    try:
        return max(0.2, float(width_mm) * _MM_TO_PT)
    except (TypeError, ValueError):
        return 0.5 * _MM_TO_PT


def _point(point) -> tuple[float, float]:
    return (float(point.x), float(point.y))


def _points(points) -> list:
    return [_point(point) for point in points or ()]


def _color(value) -> str:
    text = str(value or "").strip()
    return text if text.startswith("#") else "#" + text.lstrip("#")


def _common(annotation) -> dict:
    return {
        "id": str(getattr(annotation.id, "value", annotation.id)),
        "color": _color(getattr(annotation, "color", "#000000")),
    }


def _spec(annotation) -> dict | None:
    if isinstance(annotation, InkStroke):
        points = _points(annotation.points)
        if not points:
            return None
        spec = _common(annotation)
        spec.update({
            "kind": "ink",
            "points": points,
            "width_pt": _width_pt(annotation.width_mm),
            "opacity": max(0.0, min(1.0, float(annotation.opacity))),
        })
        return spec

    if isinstance(annotation, Highlighter):
        points = _points(annotation.points)
        if not points:
            return None
        spec = _common(annotation)
        spec.update({
            "kind": "highlight",
            "points": points,
            "width_pt": _width_pt(annotation.width_mm),
            "opacity": max(0.0, min(1.0, float(annotation.opacity))),
        })
        return spec

    if isinstance(annotation, Shape):
        spec = _common(annotation)
        spec.update({
            "kind": "shape",
            "shape_kind": ShapeKind(annotation.kind).value,
            "rect": [
                float(annotation.rect.origin.x),
                float(annotation.rect.origin.y),
                float(annotation.rect.size.width),
                float(annotation.rect.size.height),
            ],
            "width_pt": _width_pt(annotation.width_mm),
            "opacity": max(0.0, min(1.0, float(annotation.opacity))),
            "fill_enabled": bool(annotation.fill_enabled),
            "fill_color": _color(annotation.fill_color),
            "fill_opacity": max(
                0.0, min(1.0, float(annotation.fill_opacity))),
            "line_style": str(annotation.line_style),
            "start_ending": str(annotation.start_ending),
            "end_ending": str(annotation.end_ending),
            "points": _points(annotation.points),
            "close_path": bool(annotation.close_path),
            "rotation_deg": float(annotation.rotation_deg),
        })
        return spec

    if isinstance(annotation, StickyNote):
        spec = _common(annotation)
        spec.update({
            "kind": "note",
            "point": _point(annotation.position),
            "text": str(annotation.text or ""),
        })
        return spec

    return None


def native_specs(annotations) -> list:
    """Return native insertion specs for one page's annotations."""
    result = []
    for annotation in annotations or ():
        try:
            spec = _spec(annotation)
        except Exception:
            spec = None
        if spec is not None:
            result.append(spec)
    return result
