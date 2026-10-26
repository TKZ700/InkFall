"""Recolor policy for selected annotations (M6.8.2 Stage 5). Qt-free.

The single source of truth for *which* annotations a stroke/fill recolor
touches and *what* candidate replaces each one, so the viewer's context
menu, its keyboard path, and the Qt-free unit tests all agree. It is
pure: it never imports Qt, never mutates its input, and returns a fresh
``dataclasses.replace`` candidate (or ``None``) for one annotation.

Semantics (documented, tested)
------------------------------
* **Stroke color** applies to every freehand/vector annotation that
  carries an outline color: :class:`~annotations.models.InkStroke`,
  :class:`~annotations.models.Highlighter`, and every
  :class:`~annotations.models.Shape` kind. Only the ``color`` field
  changes — geometry, point order/locations, ``rotation_deg``,
  ``width_mm``, ``opacity`` (and all alpha behaviour), pressure samples,
  ``page_id``, identity, and every unrelated field are preserved. A
  recolored highlighter stays a highlighter with its 0.4 alpha; it never
  becomes a pen and never loses its transparency.
* **Fill color** applies only to a :class:`Shape` that can be filled
  (``RECTANGLE``, ``ELLIPSE``, ``POLYGON``): only ``fill_color`` changes,
  so the outline ``color``, ``fill_enabled``, ``fill_opacity``, and
  everything else stay exactly as they were. Annotations with no fill
  property — ink, highlighters, and ``LINE``/``ARROW`` shapes — are never
  touched by a fill operation.
* A request that would not change the stored value returns ``None`` (a
  true no-op member the caller drops), so a recolor that changes nothing
  never opens a history group.
"""

from __future__ import annotations

import dataclasses

from annotations.models import Highlighter, InkStroke, Shape, ShapeKind

__all__ = [
    "STROKE_KINDS",
    "FILL_SHAPE_KINDS",
    "supports_stroke_color",
    "supports_fill_color",
    "recolored",
]

#: Shape kinds whose outline color is recolorable (all of them).
STROKE_KINDS = (
    ShapeKind.LINE,
    ShapeKind.RECTANGLE,
    ShapeKind.ELLIPSE,
    ShapeKind.ARROW,
    ShapeKind.POLYGON,
)

#: Shape kinds that carry a meaningful fill (Line/Arrow cannot be filled).
FILL_SHAPE_KINDS = (
    ShapeKind.RECTANGLE,
    ShapeKind.ELLIPSE,
    ShapeKind.POLYGON,
)


def _norm(color) -> str | None:
    """Case/space-insensitive comparison key for a color value."""
    try:
        return str(color).strip().lower()
    except Exception:
        return None


def supports_stroke_color(annotation) -> bool:
    """True when ``annotation`` carries an outline color we may recolor."""
    return isinstance(annotation, (InkStroke, Highlighter, Shape))


def supports_fill_color(annotation) -> bool:
    """True when ``annotation`` is a shape with a usable fill color."""
    return (isinstance(annotation, Shape)
            and getattr(annotation, "kind", None) in FILL_SHAPE_KINDS)


def recolored(annotation, *, stroke=None, fill=None):
    """Candidate with one color replaced, or ``None``.

    ``stroke`` (a color string) requests an outline recolour; ``fill``
    requests a fill recolour. Exactly one is expected (``stroke`` wins if
    both are given, matching the menu's one-swatch-at-a-time actions).
    Returns ``None`` when the annotation does not support the requested
    color *or* the stored value already equals it (a true no-op member).
    Every other field — including identity and ``page_id`` — is carried
    over unchanged by :func:`dataclasses.replace`.
    """
    if annotation is None:
        return None
    if stroke is not None:
        if not supports_stroke_color(annotation):
            return None
        current = getattr(annotation, "color", None)
        if _norm(current) == _norm(stroke):
            return None
        return dataclasses.replace(annotation, color=str(stroke))
    if fill is not None:
        if not supports_fill_color(annotation):
            return None
        current = getattr(annotation, "fill_color", None)
        if _norm(current) == _norm(fill):
            return None
        return dataclasses.replace(annotation, fill_color=str(fill))
    return None
