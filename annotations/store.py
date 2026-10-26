"""Document-owned annotation store (M6.1 foundation).

Qt-free, stdlib-only (same layer rules as the rest of
``annotations/*``): plain page-indexed storage with stable identity.
The store owns no rendering, selection, undo, or persistence state;
it only answers "which annotations exist on this page, in order".

Identity comes from the annotations themselves
(``AnnotationBase.id`` — a uuid hex assigned once at construction).
The store never invents, rewrites, or derives identities, so an
annotation keeps its identity across moves, reorders, edits, and
removal/reinsertion. Future container children (e.g. inside a note
value) keep their own ``AnnotationId`` values; top-level lookup by
identity stays valid without the store knowing about nesting.
"""

from __future__ import annotations

from annotations.models import AnnotationBase

__all__ = ["AnnotationStore"]


def _identity_key(annotation_id) -> str:
    """Normalize an identity to its registry key (never raises)."""
    try:
        value = annotation_id.value
    except AttributeError:
        value = annotation_id
    return str(value)


class AnnotationStoreReadOnlyError(ValueError):
    """A read-only session's store refused a mutating operation."""


class AnnotationStore:
    """Ordered per-page annotation storage for one document.

    Pages are zero-based indices in ``range(page_count)``; insertion
    order within a page is preserved. All mutating operations validate
    first, so a rejected operation leaves the store unchanged.
    """

    def __init__(self, page_count: int) -> None:
        if isinstance(page_count, bool) or not isinstance(page_count, int):
            raise TypeError(
                f"page_count must be an int, got {page_count!r}")
        if page_count < 0:
            raise ValueError(
                f"page_count must be >= 0, got {page_count}")
        self._page_count = page_count
        self._pages: list[list] = [[] for _ in range(page_count)]
        self._by_id: dict[str, int] = {}
        # Per-page content revision (page-navigator preview keys): bumped
        # by every mutation that can change what a page renders. Purely
        # monotonic ints — cheap to read per paint, never reset.
        self._revisions: list[int] = [0] * page_count
        # Read-only duplicate sessions freeze their store: every mutating
        # operation raises until the flag is cleared (only the owning
        # document sets it, and only after its restore-time replay).
        self._read_only = False

    def _ensure_writable(self) -> None:
        """Refuse mutations from a read-only session's frozen store."""
        if self._read_only:
            raise AnnotationStoreReadOnlyError(
                "This read-only session cannot modify annotations.")

    @property
    def read_only(self) -> bool:
        """True when the owning session froze this store."""
        return bool(self._read_only)

    @read_only.setter
    def read_only(self, value: bool) -> None:
        self._read_only = bool(value)

    def _bump(self, page_index: int) -> None:
        try:
            self._revisions[page_index] += 1
        except Exception:
            pass

    def _bump_all(self) -> None:
        for index in range(len(self._revisions)):
            self._revisions[index] += 1

    def revision(self, page_index: int) -> int:
        """Monotonic content revision for one page (preview cache keys).

        Bumped by every mutating operation affecting the page's rendered
        annotation state (additions, removals, substitution, erasure
        replacements, undo/redo restoration, and page insert/remove
        shifts). Never raises.
        """
        try:
            return self._revisions[self._check_page(page_index)]
        except Exception:
            return 0

    @property
    def page_count(self) -> int:
        """Number of pages this store was built for."""
        return self._page_count

    def __len__(self) -> int:
        """Total annotations across all pages."""
        return len(self._by_id)

    def _check_page(self, page_index: int) -> int:
        if isinstance(page_index, bool) or not isinstance(
                page_index, int):
            raise TypeError(
                f"page_index must be an int, got {page_index!r}")
        if not 0 <= page_index < self._page_count:
            raise ValueError(
                f"page_index {page_index!r} out of range "
                f"[0, {self._page_count})")
        return page_index

    def add(self, annotation: AnnotationBase, page_index: int):
        """Store an annotation on a page; returns its stable identity.

        Raises ``TypeError`` for non-annotation values and ``ValueError``
        for out-of-range pages or duplicate identities (state unchanged).
        """
        self._ensure_writable()
        if not isinstance(annotation, AnnotationBase):
            raise TypeError(
                "annotation must be an AnnotationBase, got "
                f"{type(annotation).__name__}")
        self._check_page(page_index)
        key = _identity_key(annotation.id)
        if key in self._by_id:
            raise ValueError(
                f"duplicate annotation identity {key!r}")
        self._pages[page_index].append(annotation)
        self._by_id[key] = page_index
        self._bump(page_index)
        return annotation.id

    def get(self, annotation_id):
        """Return the annotation for an identity, or None if absent."""
        page_index = self._by_id.get(_identity_key(annotation_id))
        if page_index is None:
            return None
        for annotation in self._pages[page_index]:
            if _identity_key(annotation.id) == _identity_key(
                    annotation_id):
                return annotation
        return None

    def locate(self, annotation_id):
        """Return ``(page_index, position)`` for an identity, or None.

        M6.2: lets commands capture exact restoration points without
        touching private collections. Position is the index within the
        page's insertion-ordered list.
        """
        key = _identity_key(annotation_id)
        page_index = self._by_id.get(key)
        if page_index is None:
            return None
        for position, annotation in enumerate(self._pages[page_index]):
            if _identity_key(annotation.id) == key:
                return page_index, position
        return None

    def insert_at(self, annotation: AnnotationBase, page_index: int,
                  position: int):
        """Insert at an exact page position; returns the identity.

        M6.2: backward-compatible extension for undo restoration
        (exact order, not append). ``position`` must satisfy
        ``0 <= position <= len(page)``; out-of-range positions and
        duplicate identities raise ``ValueError`` with state unchanged.
        """
        self._ensure_writable()
        if not isinstance(annotation, AnnotationBase):
            raise TypeError(
                "annotation must be an AnnotationBase, got "
                f"{type(annotation).__name__}")
        self._check_page(page_index)
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError(
                f"position must be an int, got {position!r}")
        page = self._pages[page_index]
        if not 0 <= position <= len(page):
            raise ValueError(
                f"position {position!r} out of range "
                f"[0, {len(page)}]")
        key = _identity_key(annotation.id)
        if key in self._by_id:
            raise ValueError(
                f"duplicate annotation identity {key!r}")
        page.insert(position, annotation)
        self._by_id[key] = page_index
        self._bump(page_index)
        return annotation.id

    def contains(self, annotation_id) -> bool:
        """True iff an annotation with this identity is stored."""
        return self.get(annotation_id) is not None

    def remove(self, annotation_id) -> bool:
        """Remove by identity; True if something was removed.

        Never raises for absent identities. Remaining order is
        preserved; a removed identity may be reinserted later with its
        original identity intact.
        """
        self._ensure_writable()
        key = _identity_key(annotation_id)
        page_index = self._by_id.get(key)
        if page_index is None:
            return False
        page = self._pages[page_index]
        for i, annotation in enumerate(page):
            if _identity_key(annotation.id) == key:
                del page[i]
                break
        del self._by_id[key]
        self._bump(page_index)
        return True

    def remove_page(self, page_index: int) -> tuple:
        """Drop one page slot; later pages shift down by one (M6.8.2).

        Returns the removed page's annotations in insertion order (the
        caller may re-add them through :meth:`insert_page` + :meth:`add`
        to restore the exact page on undo). Validated first, so a
        rejected call leaves the store unchanged.
        """
        self._ensure_writable()
        self._check_page(page_index)
        removed = tuple(self._pages.pop(page_index))
        self._page_count -= 1
        removed_keys = {_identity_key(a.id) for a in removed}
        remapped: dict[str, int] = {}
        for key, index in self._by_id.items():
            if key in removed_keys:
                continue
            remapped[key] = index - 1 if index > page_index else index
        self._by_id = remapped
        # Pages shifted: every page index's rendered content changed.
        del self._revisions[page_index]
        self._revisions = [r + 1 for r in self._revisions]
        self._revisions.append(1)
        return removed

    def insert_page(self, page_index: int) -> None:
        """Insert an empty page slot; later pages shift up by one (M6.8.2).

        ``page_index`` may equal ``page_count`` (append). Validated
        first, so a rejected call leaves the store unchanged.
        """
        self._ensure_writable()
        if isinstance(page_index, bool) or not isinstance(
                page_index, int):
            raise TypeError(
                f"page_index must be an int, got {page_index!r}")
        if not 0 <= page_index <= self._page_count:
            raise ValueError(
                f"page_index {page_index!r} out of range "
                f"[0, {self._page_count}]")
        self._pages.insert(page_index, [])
        self._page_count += 1
        self._by_id = {
            key: (index + 1 if index >= page_index else index)
            for key, index in self._by_id.items()
        }
        # Pages shifted: every page index's rendered content changed.
        self._revisions.insert(page_index, 0)
        self._revisions = [r + 1 for r in self._revisions]

    def validate_substitute(self, remove_ids, insertions,
                            page_index: int, position: int) -> None:
        """Validate a substitution without mutating (raises on any problem).

        M6.6: the atomic replacement primitive behind partial erasing.
        ``remove_ids`` are removed from ``page_index`` and ``insertions``
        are inserted, in order, starting at ``position`` (measured in the
        page *after* the removals). Everything is checked first, so a
        rejected call leaves the store unchanged.
        """
        self._check_page(page_index)
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError(
                f"position must be an int, got {position!r}")
        try:
            rem_keys = [_identity_key(i) for i in remove_ids]
        except TypeError:
            raise TypeError(
                "remove_ids must be an iterable of identities")
        if len(set(rem_keys)) != len(rem_keys):
            raise ValueError("remove_ids contains duplicates")
        ins_list = list(insertions)
        for annotation in ins_list:
            if not isinstance(annotation, AnnotationBase):
                raise TypeError(
                    "insertions must be AnnotationBase values, got "
                    f"{type(annotation).__name__}")
        ins_keys = [_identity_key(a.id) for a in ins_list]
        if len(set(ins_keys)) != len(ins_keys):
            raise ValueError("insertions contain duplicate identities")
        page = self._pages[page_index]
        present = {_identity_key(a.id) for a in page}
        for key in rem_keys:
            if key not in present:
                raise ValueError(
                    f"unknown annotation identity {key!r} on page "
                    f"{page_index}")
        remaining = present - set(rem_keys)
        for key in ins_keys:
            if key in remaining:
                raise ValueError(
                    f"duplicate annotation identity {key!r}")
        n_after = len(page) - len(rem_keys)
        if not 0 <= position <= n_after:
            raise ValueError(
                f"position {position!r} out of range [0, {n_after}]")

    def substitute(self, remove_ids, insertions, page_index: int,
                   position: int) -> None:
        """Atomically remove some annotations and insert others in place.

        M6.6: used by partial-erasure replacement. Validated first (see
        :meth:`validate_substitute`); page insertion order is preserved
        and callers never touch private collections.
        """
        self._ensure_writable()
        self.validate_substitute(remove_ids, insertions, page_index,
                                 position)
        rem_set = {_identity_key(i) for i in remove_ids}
        ins_list = list(insertions)
        page = self._pages[page_index]
        new_page = [a for a in page
                    if _identity_key(a.id) not in rem_set]
        insert_at = position
        for annotation in ins_list:
            new_page.insert(insert_at, annotation)
            insert_at += 1
        new_by_id = {k: v for k, v in self._by_id.items()
                     if k not in rem_set}
        for annotation in ins_list:
            new_by_id[_identity_key(annotation.id)] = page_index
        self._pages[page_index] = new_page
        self._by_id = new_by_id
        self._bump(page_index)

    def replace(self, annotation_id, replacements):
        """Replace one annotation with several, in its exact position.

        M6.6 convenience over :meth:`substitute`: removes the identified
        annotation and inserts ``replacements`` (a non-empty sequence) at
        the same page position, in order. Returns ``(page_index,
        position)``. Raises before mutating when anything is invalid.
        """
        self._ensure_writable()
        located = self.locate(annotation_id)
        if located is None:
            raise ValueError(
                f"unknown annotation identity {annotation_id!r}")
        page_index, position = located
        reps = list(replacements)
        if not reps:
            raise ValueError("replacements must be non-empty")
        self.substitute([annotation_id], reps, page_index, position)
        return page_index, position

    def page(self, page_index: int) -> tuple:
        """Annotations on one page, in insertion order (a new tuple)."""
        self._check_page(page_index)
        return tuple(self._pages[page_index])

    def count(self, page_index: int) -> int:
        """Number of annotations on one page."""
        self._check_page(page_index)
        return len(self._pages[page_index])

    def iter_all(self):
        """Yield ``(page_index, annotation)`` in page/order sequence."""
        for page_index, page in enumerate(self._pages):
            for annotation in page:
                yield page_index, annotation

    def clear(self) -> None:
        """Drop all annotations. Safe to call repeatedly."""
        self._ensure_writable()
        self._pages = [[] for _ in range(self._page_count)]
        self._by_id.clear()
        self._bump_all()
