"""Live geometry adjustment for an in-progress recognized gesture (M7.1).

While the stylus is still down after hold-to-shape recognition, the viewer
keeps the recognized preview alive and maps further pointer movement onto
the provisional geometry instead of cancelling the conversion (see
``PdfViewer._note_shape_movement``). This module owns the pure, Qt-free
geometry for that mapping: no widgets, no rendering, no document/store
access, no PyMuPDF (same layer rules as the rest of ``annotations/*``).

Scope (M7.1 Phase A + Phase B Part 2):

* pen **line** endpoint adjustment (Phase A, unchanged);
* live adjustment of every closed pen shape the recognizer emits —
  **rectangle**, **circle**, **triangle** and **polygon** (Phase B Part 2).

Highlighter conversions are never adjustable; they keep their existing
cancel-on-movement behaviour.

Contract
--------
* :func:`begin_line_adjustment` freezes the gesture once: one endpoint
  becomes the immutable anchor, the other is the single moving endpoint,
  and the pointer position at recognition is the absolute origin.
* :func:`adjusted_line` rebuilds the whole line from the stored base
  geometry on every call. It never adds an incremental delta to the
  previous result, so repeated updates cannot drift and returning the
  pointer to the origin restores the recognized geometry exactly.
* A result shorter than :data:`MIN_LINE_LENGTH_PT` is rejected (``None``);
  the caller keeps the last valid preview and commits that geometry.
* :func:`rebase_line` translates the recognized contour into the preview
  space the viewer's morph animation showed (``prepare_morph`` aligns the
  fitted side's centroid onto the raw stroke's centroid), so the adjusted
  preview and the released annotation are the same line.
* The closed-shape contract mirrors that: selection is frozen once at the
  recognition pointer (nearest corner/vertex, lowest index on a tie),
  every frame is rebuilt from the frozen rebased base by the absolute
  pointer delta, and rectangle metadata is rebuilt with its contour. Live
  adjustment never mutates the annotation store or history.

Validity floors use the recognizer's own constants and an
*activation-grandfathering* rule: each numerical gate keeps the smaller of
its standard floor and the recognized geometry's value at activation, so a
shape accepted by recognition is never rejected or visibly changed at zero
displacement. Structural rules (ordering, simplicity, orientation) are
never grandfathered.

Nothing here mutates its inputs, touches a store, a history, a timer or a
widget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from annotations.shape_recognize import (
    MIN_CLOSED_PERIMETER_PT,
    MIN_CLOSED_PERIMETER_THICKNESS,
    MIN_SEGMENT_PT,
    ShapeCandidate,
)

__all__ = [
    "CLOSED_KINDS",
    "ClosedAdjustment",
    "LineAdjustment",
    "MIN_LINE_LENGTH_PT",
    "adjusted_closed",
    "adjusted_line",
    "begin_closed_adjustment",
    "begin_line_adjustment",
    "centroid",
    "rebase_closed_candidate",
    "rebase_line",
    "snapped_line",
]

#: Recognizer kinds whose contour is a closed ring and can be live-adjusted.
CLOSED_KINDS = frozenset({"rectangle", "circle", "triangle", "polygon"})

#: Numeric tolerance for the guard arithmetic (page points). Recognition
#: coordinates are ordinary PDF points, so 1e-9 pt is far below any drawn
#: feature and far above accumulated double-rounding noise.
_EPS = 1e-9

#: Shortest segment a live-adjusted line may commit. Single source: the
#: recognizer's own minimum, so an adjusted line can never be shorter than
#: an accepted recognized line.
MIN_LINE_LENGTH_PT = MIN_SEGMENT_PT


def _point(value):
    """Finite ``(x, y)`` float pair from a pair or a ``Point``, or None."""
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, IndexError, KeyError, ValueError):
        try:
            x = float(value.x)
            y = float(value.y)
        except (AttributeError, TypeError, ValueError):
            return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def _confidence(candidate) -> float:
    """The candidate's confidence as a finite float (0.0 when unusable)."""
    try:
        value = float(getattr(candidate, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _line_ends(candidate):
    """``(p0, p1)`` float endpoints for an open line candidate, or None."""
    if getattr(candidate, "kind", None) != "line":
        return None
    if bool(getattr(candidate, "closed", True)):
        return None
    contour = tuple(getattr(candidate, "contour", ()) or ())
    if len(contour) < 2:
        return None
    p0 = _point(contour[0])
    p1 = _point(contour[1])
    if p0 is None or p1 is None:
        return None
    return (p0, p1)


def centroid(points):
    """Mean ``(x, y)`` of a point sequence, or None when unusable.

    Malformed samples are skipped (never raises). The centroid of a
    uniformly resampled straight segment equals its midpoint, which is
    why the viewer can measure the morph preview offset with it.
    """
    total_x = 0.0
    total_y = 0.0
    count = 0
    for value in points or ():
        point = _point(value)
        if point is None:
            continue
        total_x += point[0]
        total_y += point[1]
        count += 1
    if count == 0:
        return None
    return (total_x / count, total_y / count)


@dataclass(frozen=True, slots=True)
class LineAdjustment:
    """Frozen endpoint-adjustment state for one recognized line.

    ``anchor`` never changes for the rest of the gesture. ``moving`` is the
    recognized moving endpoint in preview/commit space, ``origin`` is the
    absolute page-local pointer position at recognition, and
    ``moving_index`` preserves the committed contour order (start, end).
    """

    anchor: tuple
    moving: tuple
    moving_index: int
    origin: tuple
    confidence: float = 0.0


def rebase_line(candidate, offset):
    """Translate a recognized line by ``offset`` (preview -> commit space).

    Returns a new :class:`~annotations.shape_recognize.ShapeCandidate` (the
    original is never mutated), or None when ``candidate`` is not an open
    line or ``offset`` is unusable (then no translation is applied).
    """
    ends = _line_ends(candidate)
    if ends is None:
        return None
    shift = (0.0, 0.0) if offset is None else _point(offset)
    if shift is None:
        shift = (0.0, 0.0)
    contour = tuple((x + shift[0], y + shift[1]) for x, y in ends)
    return ShapeCandidate(kind="line", contour=contour, closed=False,
                          confidence=_confidence(candidate))


def begin_line_adjustment(candidate, origin_pt):
    """Freeze a line adjustment: moving endpoint, immutable anchor, origin.

    The moving endpoint is chosen exactly once — the endpoint nearest the
    recognition pointer, with the recorded end winning a tie. The other
    endpoint becomes the anchor for the whole gesture. Returns None for
    non-line candidates, unusable coordinates, or a segment already below
    :data:`MIN_LINE_LENGTH_PT`.
    """
    ends = _line_ends(candidate)
    origin = _point(origin_pt)
    if ends is None or origin is None:
        return None
    p0, p1 = ends
    d0 = math.hypot(p0[0] - origin[0], p0[1] - origin[1])
    d1 = math.hypot(p1[0] - origin[0], p1[1] - origin[1])
    if d0 < d1:
        moving_index = 0
        moving, anchor = p0, p1
    else:
        moving_index = 1
        moving, anchor = p1, p0
    if math.hypot(moving[0] - anchor[0],
                  moving[1] - anchor[1]) < MIN_LINE_LENGTH_PT:
        return None
    return LineAdjustment(anchor=anchor, moving=moving,
                          moving_index=moving_index, origin=origin,
                          confidence=_confidence(candidate))


def adjusted_line(adjustment, pointer_pt):
    """Recompute the whole line for ``pointer_pt`` (absolute, drift-free).

    Every call derives the geometry from ``adjustment.moving`` and the
    absolute pointer position: two calls with the same pointer always
    produce identical geometry, and moving back to the origin restores the
    recognized line. Returns None when the pointer is unusable or the
    resulting segment is shorter than :data:`MIN_LINE_LENGTH_PT` (the
    caller keeps the last valid preview).
    """
    if not isinstance(adjustment, LineAdjustment):
        return None
    pointer = _point(pointer_pt)
    if pointer is None:
        return None
    dx = pointer[0] - adjustment.origin[0]
    dy = pointer[1] - adjustment.origin[1]
    moving = (adjustment.moving[0] + dx, adjustment.moving[1] + dy)
    anchor = adjustment.anchor
    if math.hypot(moving[0] - anchor[0],
                  moving[1] - anchor[1]) < MIN_LINE_LENGTH_PT:
        return None
    contour = ((moving, anchor) if adjustment.moving_index == 0
               else (anchor, moving))
    return ShapeCandidate(kind="line", contour=contour, closed=False,
                          confidence=adjustment.confidence)


def snapped_line(adjustment, target_pt):
    """Line geometry with the moving endpoint placed exactly on a target.

    Connect Lines snapping is an absolute placement, not a delta: when a
    genuine endpoint/intersection target resolves, the moving endpoint
    must coincide with that target exactly (M7.1 Phase B). The anchor is
    unchanged, the contour order is preserved, and the same minimum
    length applies — ``None`` means the caller keeps its last valid
    preview. This is the only geometry path a snapped adjustment frame
    uses, so the preview and the committed line carry identical numbers.
    """
    if not isinstance(adjustment, LineAdjustment):
        return None
    target = _point(target_pt)
    if target is None:
        return None
    anchor = adjustment.anchor
    if math.hypot(target[0] - anchor[0],
                  target[1] - anchor[1]) < MIN_LINE_LENGTH_PT:
        return None
    contour = ((target, anchor) if adjustment.moving_index == 0
               else (anchor, target))
    return ShapeCandidate(kind="line", contour=contour, closed=False,
                          confidence=adjustment.confidence)


# ------------------------------------------------------------ closed shapes


def _ring(candidate):
    """Finite contour tuples for a closed candidate, or None."""
    try:
        raw = tuple(getattr(candidate, "contour", ()) or ())
    except TypeError:
        return None
    ring = []
    for value in raw:
        point = _point(value)
        if point is None:
            return None
        ring.append(point)
    return tuple(ring)


def _perimeter(ring) -> float:
    """Closed-ring perimeter (pt) for an ordered vertex list."""
    count = len(ring)
    if count < 2:
        return 0.0
    total = 0.0
    for index in range(count):
        x0, y0 = ring[index]
        x1, y1 = ring[(index + 1) % count]
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def _signed_area(ring) -> float:
    """Signed shoelace area (pt^2); its sign is the ring orientation."""
    count = len(ring)
    if count < 3:
        return 0.0
    total = 0.0
    for index in range(count):
        x0, y0 = ring[index]
        x1, y1 = ring[(index + 1) % count]
        total += x0 * y1 - x1 * y0
    return total * 0.5


def _convex_hull(points):
    """Monotone-chain convex hull (counter-clockwise, no repeated vertex)."""
    ordered = sorted(set(points))
    if len(ordered) <= 2:
        return tuple(ordered)

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _hull_area(ring) -> float:
    hull = _convex_hull(ring)
    return abs(_signed_area(hull))


def _nearest_index(ring, point) -> int:
    """Index of the ring vertex nearest ``point`` (lowest index on a tie)."""
    best_index = 0
    best_distance = None
    for index, vertex in enumerate(ring):
        distance = math.hypot(vertex[0] - point[0], vertex[1] - point[1])
        if best_distance is None or distance < best_distance - _EPS:
            best_index = index
            best_distance = distance
    return best_index


def _rect_ring(cx, cy, ux, uy, half_a, half_b):
    """Corners of a rotated box, in the recognizer's ring order."""
    vx, vy = -uy, ux
    return (
        (cx + half_a * ux + half_b * vx, cy + half_a * uy + half_b * vy),
        (cx + half_a * ux - half_b * vx, cy + half_a * uy - half_b * vy),
        (cx - half_a * ux - half_b * vx, cy - half_a * uy - half_b * vy),
        (cx - half_a * ux + half_b * vx, cy - half_a * uy + half_b * vy),
    )


def _circle_ring(cx, cy, radius, steps):
    """Equal-angle circular contour with the recognizer's phase."""
    count = max(8, int(steps))
    ring = []
    for index in range(count):
        phi = 2.0 * math.pi * index / count
        ring.append((cx + radius * math.cos(phi),
                     cy + radius * math.sin(phi)))
    return tuple(ring)


def _ring_orientation(ring) -> int:
    area = _signed_area(ring)
    if abs(area) <= _EPS:
        return 0
    return 1 if area > 0.0 else -1


def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _on_segment(a, b, point) -> bool:
    return (min(a[0], b[0]) - _EPS <= point[0] <= max(a[0], b[0]) + _EPS
            and min(a[1], b[1]) - _EPS <= point[1] <= max(a[1], b[1]) + _EPS)


def _segments_intersect(a, b, c, d) -> bool:
    """True when closed segments ``ab`` and ``cd`` cross or touch.

    Uses an absolute epsilon well above coordinate rounding; callers skip
    adjacent edges (which share an endpoint by construction).
    """
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = _cross(rx, ry, sx, sy)
    qpx, qpy = c[0] - a[0], c[1] - a[1]
    if abs(denom) <= _EPS:
        # Parallel/collinear: only an actual overlap or touch matters.
        if abs(_cross(qpx, qpy, rx, ry)) > _EPS:
            return False
        return (_on_segment(a, b, c) or _on_segment(a, b, d)
                or _on_segment(c, d, a) or _on_segment(c, d, b))
    t = _cross(qpx, qpy, sx, sy) / denom
    u = _cross(qpx, qpy, rx, ry) / denom
    return (-_EPS <= t <= 1.0 + _EPS) and (-_EPS <= u <= 1.0 + _EPS)


def _self_intersects(ring) -> bool:
    """True when a non-adjacent edge pair crosses or touches."""
    count = len(ring)
    if count < 4:
        return False
    for first in range(count):
        a = ring[first]
        b = ring[(first + 1) % count]
        for second in range(first + 1, count):
            if second == first or second == (first + 1) % count:
                continue
            if first == (second + 1) % count:
                continue
            if _segments_intersect(a, b, ring[second],
                                   ring[(second + 1) % count]):
                return True
    return False


def _has_duplicate_vertex(ring) -> bool:
    count = len(ring)
    for first in range(count):
        for second in range(first + 1, count):
            if math.hypot(ring[second][0] - ring[first][0],
                          ring[second][1] - ring[first][1]) <= _EPS:
                return True
    return False


def _candidate_like(base, ring, meta=None):
    """New closed candidate with the base's identity and given geometry."""
    return ShapeCandidate(kind=base.kind, contour=tuple(ring), closed=True,
                          confidence=_confidence(base),
                          meta=tuple(base.meta if meta is None else meta))


def _meta_box(candidate):
    """``(cx, cy, half_a, half_b, rotation_deg)`` for a box candidate."""
    try:
        meta = tuple(getattr(candidate, "meta", ()) or ())
    except TypeError:
        return None
    if len(meta) != 5:
        return None
    values = []
    for value in meta:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        values.append(number)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class ClosedAdjustment:
    """Frozen adjustment state for one recognized closed shape.

    The rebased ``candidate`` is the geometry the morph showed at
    activation; every frame is recomputed from it with the absolute
    pointer delta, so no incremental delta can accumulate. Handle
    selection and every numerical floor are frozen here too.
    """

    candidate: ShapeCandidate
    origin: tuple
    kind: str
    moving_index: int = -1
    anchor: tuple = (0.0, 0.0)
    rotation: float = 0.0
    center: tuple = (0.0, 0.0)
    radius: float = 0.0
    perimeter_floor: float = 0.0
    edge_floors: tuple = ()
    short_side_floor: float = 0.0
    radius_floor: float = 0.0
    area_share_floor: float = 0.0


def rebase_closed_candidate(candidate, offset):
    """Translate a closed recognized candidate into preview/commit space.

    ``prepare_morph`` moves the fitted contour so its centerline centroid
    meets the raw stroke's; live adjustment must use the same translation
    or the preview and the released annotation would differ. The contour
    and box metadata (rectangle/circle) move together. Returns a new
    candidate (the input is never mutated), or None when the candidate is
    not a supported closed shape or ``offset`` is unusable.
    """
    if candidate is None or not _valid_closed(candidate):
        return None
    ring = _ring(candidate)
    if ring is None or len(ring) < 3:
        return None
    shift = (0.0, 0.0) if offset is None else _point(offset)
    if shift is None:
        shift = (0.0, 0.0)
    moved = tuple((x + shift[0], y + shift[1]) for x, y in ring)
    meta = None
    if candidate.kind in ("rectangle", "circle"):
        box = _meta_box(candidate)
        if box is None:
            return None
        meta = (box[0] + shift[0], box[1] + shift[1],
                box[2], box[3], box[4])
    return _candidate_like(candidate, moved, meta)


def _valid_closed(candidate) -> bool:
    return (candidate is not None
            and bool(getattr(candidate, "closed", False))
            and getattr(candidate, "kind", None) in CLOSED_KINDS)


def begin_closed_adjustment(candidate, origin_pt, thickness_pt=0.0):
    """Freeze a closed-shape adjustment at the recognition pointer.

    Chooses the corner/vertex nearest ``origin_pt`` exactly once (lowest
    index breaks ties), stores the immutable anchors, and computes every
    effective floor with activation grandfathering. Returns a
    :class:`ClosedAdjustment`, or None when the candidate is unsupported,
    inconsistent, or numerically unusable.
    """
    if not _valid_closed(candidate):
        return None
    origin = _point(origin_pt)
    if origin is None:
        return None
    ring = _ring(candidate)
    if ring is None:
        return None
    try:
        thickness = float(thickness_pt)
    except (TypeError, ValueError):
        thickness = 0.0
    if not math.isfinite(thickness) or thickness < 0.0:
        thickness = 0.0
    standard_perimeter = max(MIN_CLOSED_PERIMETER_PT,
                             MIN_CLOSED_PERIMETER_THICKNESS * thickness)
    base_perimeter = _perimeter(ring)
    if base_perimeter <= _EPS:
        return None
    perimeter_floor = min(standard_perimeter, base_perimeter)

    if candidate.kind == "rectangle":
        if len(ring) != 4:
            return None
        box = _meta_box(candidate)
        if box is None:
            return None
        _cx, _cy, half_a, half_b, rotation = box
        if half_a <= 0.0 or half_b <= 0.0:
            return None
        short_side = 2.0 * min(half_a, half_b)
        index = _nearest_index(ring, origin)
        return ClosedAdjustment(
            candidate=candidate, origin=origin, kind="rectangle",
            moving_index=index, anchor=ring[(index + 2) % 4],
            rotation=rotation, perimeter_floor=perimeter_floor,
            short_side_floor=min(MIN_SEGMENT_PT, short_side),
        )

    if candidate.kind == "circle":
        box = _meta_box(candidate)
        if box is None:
            return None
        cx, cy, half_a, half_b, rotation = box
        if half_a <= 0.0 or half_b <= 0.0:
            return None
        if abs(half_a - half_b) > _EPS or abs(rotation) > _EPS:
            # The recognizer emits circles only; never turn one into a
            # general ellipse through adjustment.
            return None
        steps = len(ring)
        if steps < 3:
            return None
        factor = 2.0 * steps * math.sin(math.pi / steps)
        standard_radius = max(MIN_SEGMENT_PT / 2.0,
                              standard_perimeter / factor)
        return ClosedAdjustment(
            candidate=candidate, origin=origin, kind="circle",
            center=(cx, cy), radius=half_a,
            perimeter_floor=perimeter_floor,
            radius_floor=min(standard_radius, half_a),
        )

    if candidate.kind == "triangle":
        if len(ring) != 3:
            return None
        if _ring_orientation(ring) == 0:
            return None
        edges = _edges(ring)
        index = _nearest_index(ring, origin)
        return ClosedAdjustment(
            candidate=candidate, origin=origin, kind="triangle",
            moving_index=index, perimeter_floor=perimeter_floor,
            edge_floors=tuple(min(MIN_SEGMENT_PT, edge) for edge in edges),
        )

    # generic polygon
    if not (4 <= len(ring) <= 8):
        return None
    if _ring_orientation(ring) == 0:
        return None
    if _has_duplicate_vertex(ring) or _self_intersects(ring):
        return None
    hull_area = _hull_area(ring)
    if hull_area <= _EPS:
        return None
    share = abs(_signed_area(ring)) / hull_area
    edges = _edges(ring)
    index = _nearest_index(ring, origin)
    return ClosedAdjustment(
        candidate=candidate, origin=origin, kind="polygon",
        moving_index=index, perimeter_floor=perimeter_floor,
        edge_floors=tuple(min(MIN_SEGMENT_PT, edge) for edge in edges),
        area_share_floor=min(0.50, share),
    )


def _edges(ring):
    count = len(ring)
    return tuple(math.hypot(ring[(i + 1) % count][0] - ring[i][0],
                            ring[(i + 1) % count][1] - ring[i][1])
                 for i in range(count))


def adjusted_closed(adjustment, pointer_pt):
    """Rebuild a closed shape for ``pointer_pt`` (absolute, drift-free).

    Returns a new candidate, or None when the frame violates an approved
    geometric constraint; the caller keeps its last valid preview. Every
    call derives from the frozen base, so identical pointer samples yield
    identical geometry and returning to the origin restores it exactly.
    """
    if not isinstance(adjustment, ClosedAdjustment):
        return None
    pointer = _point(pointer_pt)
    if pointer is None:
        return None
    if pointer == adjustment.origin:
        return adjustment.candidate
    dx = pointer[0] - adjustment.origin[0]
    dy = pointer[1] - adjustment.origin[1]
    if adjustment.kind == "rectangle":
        return _adjusted_rectangle(adjustment, dx, dy)
    if adjustment.kind == "circle":
        return _adjusted_circle(adjustment, pointer)
    return _adjusted_vertex(adjustment, dx, dy)


def _adjusted_rectangle(adjustment, dx, dy):
    base = adjustment.candidate
    ring = _ring(base)
    if ring is None or len(ring) != 4:
        return None
    radians = math.radians(adjustment.rotation)
    ux, uy = math.cos(radians), math.sin(radians)
    vx, vy = -uy, ux
    anchor = adjustment.anchor
    index = adjustment.moving_index
    selected = ring[index]
    moved = (selected[0] + dx, selected[1] + dy)
    if not (math.isfinite(moved[0]) and math.isfinite(moved[1])):
        return None
    span_u = ((selected[0] - anchor[0]) * ux
              + (selected[1] - anchor[1]) * uy)
    span_v = ((selected[0] - anchor[0]) * vx
              + (selected[1] - anchor[1]) * vy)
    if abs(span_u) <= _EPS or abs(span_v) <= _EPS:
        return None
    du = (moved[0] - anchor[0]) * ux + (moved[1] - anchor[1]) * uy
    dv = (moved[0] - anchor[0]) * vx + (moved[1] - anchor[1]) * vy
    # The controlled corner may never cross a local axis through the
    # anchor (that would mirror the rectangle).
    if du * span_u <= 0.0 or dv * span_v <= 0.0:
        return None
    half_a = abs(du) * 0.5
    half_b = abs(dv) * 0.5
    if 2.0 * min(half_a, half_b) < adjustment.short_side_floor - _EPS:
        return None
    if 4.0 * (half_a + half_b) < adjustment.perimeter_floor - _EPS:
        return None
    cx = anchor[0] + (moved[0] - anchor[0]) * 0.5
    cy = anchor[1] + (moved[1] - anchor[1]) * 0.5
    contour = _rect_ring(cx, cy, ux, uy, half_a, half_b)
    anchor_index = (index + 2) % 4
    restored = contour[anchor_index]
    if math.hypot(restored[0] - anchor[0],
                  restored[1] - anchor[1]) > 1e-7:
        return None
    meta = (cx, cy, half_a, half_b, adjustment.rotation)
    return _candidate_like(base, contour, meta)


def _adjusted_circle(adjustment, pointer):
    base = adjustment.candidate
    ring = _ring(base)
    if ring is None or len(ring) < 3:
        return None
    cx, cy = adjustment.center
    before = math.hypot(adjustment.origin[0] - cx,
                        adjustment.origin[1] - cy)
    after = math.hypot(pointer[0] - cx, pointer[1] - cy)
    radius = adjustment.radius + (after - before)
    if not math.isfinite(radius):
        return None
    if radius < adjustment.radius_floor - _EPS:
        return None
    steps = len(ring)
    contour = _circle_ring(cx, cy, radius, steps)
    factor = 2.0 * steps * math.sin(math.pi / steps)
    if radius * factor < adjustment.perimeter_floor - _EPS:
        return None
    meta = (cx, cy, radius, radius, 0.0)
    return _candidate_like(base, contour, meta)


def _adjusted_vertex(adjustment, dx, dy):
    base = adjustment.candidate
    ring = _ring(base)
    if ring is None:
        return None
    index = adjustment.moving_index
    if not 0 <= index < len(ring):
        return None
    points = list(ring)
    points[index] = (ring[index][0] + dx, ring[index][1] + dy)
    if not (math.isfinite(points[index][0])
            and math.isfinite(points[index][1])):
        return None
    edges = _edges(points)
    for edge, floor in zip(edges, adjustment.edge_floors):
        if edge < floor - _EPS:
            return None
    if sum(edges) < adjustment.perimeter_floor - _EPS:
        return None
    area = _signed_area(points)
    if abs(area) <= _EPS:
        return None
    if (area > 0.0) != (_signed_area(ring) > 0.0):
        return None
    if adjustment.kind == "polygon":
        if _has_duplicate_vertex(points) or _self_intersects(points):
            return None
        hull_area = _hull_area(points)
        if hull_area <= _EPS:
            return None
        share = abs(area) / hull_area
        if share < adjustment.area_share_floor - _EPS:
            return None
    return _candidate_like(base, tuple(points))
