"""Page-frame annotation remapping for a page rotation (M6.8.2 Stage 5.1).

Qt-free, stdlib-only (same layer rules as the rest of ``annotations/*``).

A page's stored annotation coordinates live in that page's *displayed*
frame: the effective rotation is baked once into the layout entry, so the
paint and input paths stay a pure scale+translate (see
``app.viewer._event_to_page_pt`` — "page-local rotated PDF points").
Rotating the page changes that frame, so every annotation attached to the
page must be re-expressed in the new frame; otherwise it would drift off
the PDF content it was drawn over.

The change of frame is a rigid rotation (plus the origin shift a quarter
turn introduces), so it is expressed as one
:class:`~annotations.transform.GroupTransform` about the origin and
applied through the existing :func:`~annotations.transform.apply_group`:
the same Rect/Ellipse ``rotation_deg`` accumulation, point mapping, and
bounding-rect rebuild the selection transforms already use. No second
geometry model is introduced, and no opaque PyMuPDF object is touched.

Frame convention (clockwise, y-down, origin top-left) — it must match
``rendering.renderer._rotated_tile_to_page_clip`` exactly, i.e. the map
from *unrotated* media-box points to *displayed* points::

    r=0:    (x, y)
    r=90:   (H - y, x)
    r=180:  (W - x, H - y)
    r=270:  (y, W - x)

with ``W``/``H`` the UNROTATED media-box width/height. Composing the
inverse of the old rotation with the forward of the new one yields one
affine map whose linear part is a rotation by ``delta = new - old`` and
whose translation depends only on ``delta`` and the old rotation.
"""

from __future__ import annotations

import dataclasses

from annotations.transform import (
    GroupTransform,
    apply_group,
    normalize_deg,
    rotate_point,
)
from core.types import Point

__all__ = ["rotate_frame_point", "rotate_page_annotations"]

_ROTATIONS = (0, 90, 180, 270)


def _norm_rotation(rotation) -> int:
    """A quarter-turn rotation in ``{0, 90, 180, 270}`` (else 0)."""
    try:
        value = int(rotation) % 360
    except (TypeError, ValueError):
        return 0
    return value if value in _ROTATIONS else 0


def _forward(x: float, y: float, rotation: int, w: float, h: float):
    """Unrotated media-box point -> displayed point (see module docstring)."""
    if rotation == 90:
        return (h - y, x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        return (y, w - x)
    return (x, y)


def _inverse(x: float, y: float, rotation: int, w: float, h: float):
    """Displayed point -> unrotated media-box point (inverse of ``_forward``)."""
    if rotation == 90:
        return (y, h - x)
    if rotation == 180:
        return (w - x, h - y)
    if rotation == 270:
        return (w - y, x)
    return (x, y)


def rotate_frame_point(x, y, *, media_w, media_h, old_rotation,
                       new_rotation) -> tuple:
    """Map a displayed-frame point from the old to the new page frame.

    ``media_w``/``media_h`` are the UNROTATED media-box dimensions in
    points; ``old_rotation``/``new_rotation`` are the displayed
    rotations (degrees, clockwise). Returns ``(x, y)`` in the new frame.
    """
    w = float(media_w)
    h = float(media_h)
    old = _norm_rotation(old_rotation)
    ux, uy = _inverse(float(x), float(y), old, w, h)
    return _forward(ux, uy, _norm_rotation(new_rotation), w, h)


def _delta_transform(old_rotation, new_rotation, media_w, media_h):
    """The page-frame change as one origin-anchored ``GroupTransform``.

    Returns ``None`` when the frame does not change. The identity
    ``p' = R_delta(p) + t`` (with ``t`` the frame change's translation,
    ``t = T(0, 0)``) is realised by a transform that maps
    ``p -> R_delta(p + (dx, dy))``, choosing ``(dx, dy) = R_-delta(t)``.
    """
    old = _norm_rotation(old_rotation)
    new = _norm_rotation(new_rotation)
    delta = normalize_deg(new - old)
    if delta == 0.0:
        return None
    tx, ty = rotate_frame_point(0.0, 0.0, media_w=media_w, media_h=media_h,
                                old_rotation=old, new_rotation=new)
    back = rotate_point(Point(float(tx), float(ty)), 0.0, 0.0, -delta)
    return GroupTransform(pivot=Point(0.0, 0.0), angle_deg=delta,
                          dx=float(back.x), dy=float(back.y))


def _map_positional(annotation, transform):
    """Fallback for annotations without a mappable extent (e.g. notes)."""
    position = getattr(annotation, "position", None)
    if position is None:
        return None
    try:
        x = float(position.x) + float(transform.dx)
        y = float(position.y) + float(transform.dy)
    except Exception:
        return None
    moved = rotate_point(Point(x, y), 0.0, 0.0, float(transform.angle_deg))
    try:
        return dataclasses.replace(annotation, position=moved)
    except Exception:
        return None


def rotate_page_annotations(annotations, *, media_w, media_h, old_rotation,
                            new_rotation) -> tuple:
    """One candidate per annotation after the page frame rotates.

    Order is preserved and every candidate keeps its identity, page,
    style, and unrelated fields. When the rotation is a no-op the input
    is returned unchanged; an annotation whose geometry the transform
    cannot express (no measurable extent) is returned as-is rather than
    dropped, so a page rotation can never lose or orphan content.
    """
    items = list(annotations or ())
    transform = _delta_transform(old_rotation, new_rotation, media_w, media_h)
    if transform is None:
        return tuple(items)
    out = []
    for annotation in items:
        candidate = apply_group(annotation, (0.0, 0.0), transform)
        if candidate is None:
            candidate = _map_positional(annotation, transform)
        out.append(candidate if candidate is not None else annotation)
    return tuple(out)
