"""Multi-annotation selection state + frame geometry (M6.8.1). Qt-free.

Selection is *view* interaction state, not part of any persisted
annotation and never written to a store or an undo history. This module
holds two things:

* :class:`SelectionState` — an ordered collection of selected
  annotations, each addressed by its stable
  :class:`~core.types.AnnotationId` (never a list position or a
  transient object), plus self-healing helpers
  (``revalidate``/``resolve``) so a selection whose annotations left
  the store (undo, redo, eraser) simply disappears. The order is
  **caller order** — the viewer passes ids in document order (page
  index ascending, store insertion order within a page) — so two
  selections of the same annotations always carry the same order.
* :class:`SelectionFrame` / :func:`frame_for` — a paint-independent
  description of one annotation's selection frame in page-local pt:
  the padded rendered ``bounds`` and the eight M6.8.2 handle
  positions. M6.8.1 paints only ``bounds`` (frame-only feedback); the
  handles are planned geometry, never drawn, so nothing advertises an
  action the slice does not implement.

Kept deliberately minimal so M6.8.2 (transforms) can extend the same
idea later. Qt-free and stdlib-only (enforced by
``tests/test_import_boundaries.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from annotations.hit_test import annotation_extent

__all__ = ["SelectionFrame", "SelectionState", "frame_for"]


def _identity_key(annotation_id) -> str:
    """Stable string key for an ``AnnotationId`` (or anything id-like)."""
    try:
        value = annotation_id.value
    except AttributeError:
        value = annotation_id
    return str(value)


@dataclass(frozen=True, slots=True)
class SelectionFrame:
    """Paint-independent geometry of a selection frame (page-local pt).

    ``bounds`` is ``(minx, miny, maxx, maxy)`` of the selected
    annotation's rendered extent. ``handles`` is the eight grabbable
    positions M6.8.2 will use for move/resize/rotation — corners plus
    edge midpoints, in a stable clockwise order starting top-left. Both
    are empty when nothing is selected or the annotation has no
    measurable geometry.
    """

    bounds: tuple = ()
    handles: tuple = ()


def frame_for(annotation) -> SelectionFrame:
    """Build a :class:`SelectionFrame` from an annotation's extent."""
    extent = annotation_extent(annotation)
    if extent is None:
        return SelectionFrame()
    minx, miny, maxx, maxy = extent
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    handles = (
        (minx, miny), (cx, miny), (maxx, miny),
        (maxx, cy), (maxx, maxy), (cx, maxy),
        (minx, maxy), (minx, cy),
    )
    return SelectionFrame(bounds=(minx, miny, maxx, maxy), handles=handles)


class SelectionState:
    """The ordered set of selected annotation identities, or none.

    Multiple annotations, each identified by its stable ``AnnotationId``.
    The state is document-scoped: the viewer clears it whenever the
    document changes or the active tool leaves Selection/Lasso. Every
    mutator returns whether the selection actually changed, so callers
    repaint only on a real change and a repeated gesture is a true
    no-op.
    """

    def __init__(self) -> None:
        self._ids: list = []
        self._keys: set = set()

    @property
    def selected_ids(self) -> tuple:
        """The selected ``AnnotationId``s in caller order (document order)."""
        return tuple(self._ids)

    @property
    def count(self) -> int:
        """How many annotations are currently selected."""
        return len(self._ids)

    @property
    def selected_id(self):
        """The sole selected ``AnnotationId``, or ``None``.

        A convenience for single-selection callers; ``None`` unless
        exactly one annotation is selected.
        """
        return self._ids[0] if len(self._ids) == 1 else None

    @property
    def has_selection(self) -> bool:
        """True while at least one annotation is selected."""
        return bool(self._ids)

    def is_selected(self, annotation_id) -> bool:
        """True when ``annotation_id`` is among the selected identities."""
        if annotation_id is None:
            return False
        return _identity_key(annotation_id) in self._keys

    def select(self, annotation_id) -> bool:
        """Replace the selection with one id (or none); returns changed."""
        if annotation_id is None:
            return self.clear()
        key = _identity_key(annotation_id)
        if self._keys == {key}:
            return False
        self._ids = [annotation_id]
        self._keys = {key}
        return True

    def set(self, ids) -> bool:
        """Replace the selection with ``ids`` in caller order.

        Duplicates are de-duplicated (first occurrence wins). Returns
        True only when the resulting set/order differs, so committing
        the same gesture twice is a no-op.
        """
        ordered: list = []
        keys: set = set()
        for annotation_id in ids or ():
            if annotation_id is None:
                continue
            key = _identity_key(annotation_id)
            if key in keys:
                continue
            keys.add(key)
            ordered.append(annotation_id)
        if ordered == self._ids:
            return False
        self._ids = ordered
        self._keys = keys
        return True

    def add(self, ids) -> bool:
        """Extend the selection, preserving order; returns changed."""
        ids = tuple(ids or ())
        if not ids:
            return False
        merged = list(self._ids)
        added = False
        keys = set(self._keys)
        for annotation_id in ids:
            if annotation_id is None:
                continue
            key = _identity_key(annotation_id)
            if key not in keys:
                keys.add(key)
                merged.append(annotation_id)
                added = True
        if not added:
            return False
        self._ids = merged
        self._keys = keys
        return True

    def toggle(self, annotation_id) -> bool:
        """Add or drop one annotation; returns changed."""
        if annotation_id is None:
            return False
        key = _identity_key(annotation_id)
        if key in self._keys:
            self._ids = [a for a in self._ids
                         if _identity_key(a) != key]
            self._keys = set(_identity_key(a) for a in self._ids)
            return True
        self._ids = self._ids + [annotation_id]
        self._keys = set(self._keys)
        self._keys.add(key)
        return True

    def clear(self) -> bool:
        """Clear the selection; returns True only when something cleared."""
        if not self._ids:
            return False
        self._ids = []
        self._keys = set()
        return True

    def revalidate(self, store):
        """Drop identities no longer present in ``store``.

        Returns True when the selection shrank. Safe on a ``None``/
        unreadable store: a selection that cannot be verified is
        cleared rather than left as a set of stale references.
        """
        if not self._ids:
            return False

        def present(annotation_id) -> bool:
            try:
                return store is not None and store.contains(annotation_id)
            except Exception:
                return False
        kept = [a for a in self._ids if present(a)]
        if len(kept) == len(self._ids):
            return False
        self._ids = kept
        self._keys = set(_identity_key(a) for a in kept)
        return True

    def resolve(self, store):
        """The selected annotations from ``store``, in selection order.

        Self-clearing: identities absent from ``store`` (or an
        unreadable store) drop first, so a caller never acts on stale
        references. Returns ``()`` when nothing survives.
        """
        self.revalidate(store)
        resolved = []
        for annotation_id in list(self._ids):
            try:
                annotation = store.get(annotation_id)
            except Exception:
                annotation = None
            if annotation is not None:
                resolved.append(annotation)
            else:
                key = _identity_key(annotation_id)
                self._ids = [a for a in self._ids
                             if _identity_key(a) != key]
        self._keys = set(_identity_key(a) for a in self._ids)
        return tuple(resolved)
