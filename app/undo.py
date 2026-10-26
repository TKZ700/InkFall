"""Model-level undo/redo for document annotation stores (M6.2).

Qt-backed (``app/*`` may import Qt) but store-agnostic: commands
operate on an :class:`AnnotationStore` through its public API, so the
store and the annotation models stay Qt-free. One
:class:`DocumentHistory` (one ``QUndoStack``) binds to one document's
store; the viewer owns one history per opened document and drops it
on close. Nothing here paints, selects, persists, or draws tools —
future tool milestones create these commands when they commit
annotations, which is also when store contents become visible.
"""

from __future__ import annotations

from PySide6.QtGui import QUndoCommand, QUndoStack

from annotations.models import AnnotationBase
from annotations.store import AnnotationStore


def _identity_key(annotation_id) -> str:
    """Normalize an identity to its registry key (never raises).

    Mirrors :func:`annotations.store._identity_key` so command-side
    validation and the store agree on identity comparison.
    """
    try:
        value = annotation_id.value
    except AttributeError:
        value = annotation_id
    return str(value)

__all__ = [
    "AddAnnotationCommand",
    "BatchMoveCommand",
    "DeleteAnnotationCommand",
    "DeletePageCommand",
    "InsertPageCommand",
    "ReplaceAnnotationCommand",
    "RotatePageCommand",
    "DocumentHistory",
]


class _PageCommand(QUndoCommand):
    """Shared validation for document page operations (M6.8.2 Stage 5.1).

    Page operations change the document (page count, page content,
    annotation pages) rather than a single annotation, so these commands
    hold a *document* and the caller-supplied effect callbacks that own
    the heavier lifecycle (release/rebuild the render pipeline). The
    constructor validates against the document's current page count and
    raises before anything is pushed, so a pushed command cannot fail its
    first execution.
    """

    def __init__(self, document, page_index: int, text: str,
                 allow_append: bool = False) -> None:
        super().__init__(str(text))
        if document is None:
            raise TypeError("document must be an open Document")
        page_count = int(getattr(document, "page_count", 0))
        if isinstance(page_index, bool) or not isinstance(page_index, int):
            raise TypeError(
                f"page_index must be an int, got {page_index!r}")
        upper = page_count + (1 if allow_append else 0)
        if not 0 <= page_index < upper:
            raise ValueError(
                f"page_index {page_index!r} out of range [0, {upper})")
        self._document = document
        self._page_index = page_index
        # M6.8.2 Stage 5.1: the caller may run the effect *before* pushing
        # (so a failure can be detected and rolled back without leaving a
        # broken stack entry), in which case Qt's push-time ``redo()`` is
        # skipped once via :meth:`mark_pre_applied`.
        self._pre_applied = False

    def mark_pre_applied(self) -> None:
        """Record that the effect was already run before the push.

        Qt invokes ``redo()`` from ``QUndoStack.push``; the viewer calls
        :meth:`redo` itself first so a failed page write (disk-full, a
        locked temp file) can roll back and never reach the stack. The
        first stack-invoked ``redo()`` is then a no-op; later undo/redo
        cycles behave normally.
        """
        self._pre_applied = True


class RotatePageCommand(_PageCommand):
    """One cumulative page rotation; undo applies the inverse rotation.

    ``apply(page_index, degrees)`` performs the rotation (backend +
    annotations + working copy + render refresh); undo passes
    ``(-degrees) % 360`` so the inverse is exact for quarter turns.
    """

    def __init__(self, document, page_index: int, degrees: int,
                 apply, text: str = "Rotate Page") -> None:
        super().__init__(document, page_index, text)
        if isinstance(degrees, bool) or not isinstance(degrees, int):
            raise ValueError(
                f"degrees must be an int quarter turn: {degrees!r}")
        if degrees % 90 != 0 or degrees % 360 == 0:
            raise ValueError(
                f"degrees must be a non-zero multiple of 90: {degrees!r}")
        if not callable(apply):
            raise TypeError("apply must be callable")
        self._degrees = int(degrees) % 360
        self._apply = apply

    def redo(self) -> None:
        if self._pre_applied:
            self._pre_applied = False
            return
        self._apply(self._page_index, self._degrees)

    def undo(self) -> None:
        self._apply(self._page_index, (-self._degrees) % 360)


class DeletePageCommand(_PageCommand):
    """One page deletion; undo restores the page exactly at its index.

    ``delete(page_index)`` removes the page and returns the restore token
    (the removed page's content blob + annotations); ``insert(token)``
    puts it back. The token is captured on the first ``redo`` (which Qt
    runs on push) and re-captured on every redo, so a redo after an undo
    is always a faithful deletion of the same logical page.
    """

    def __init__(self, document, page_index: int, delete, insert,
                 text: str = "Delete Page") -> None:
        super().__init__(document, page_index, text)
        if int(getattr(document, "page_count", 0)) <= 1:
            raise ValueError("cannot delete the only page of a document")
        if not callable(delete) or not callable(insert):
            raise TypeError("delete and insert must be callable")
        self._delete = delete
        self._insert = insert
        self._removed = None

    def redo(self) -> None:
        if self._pre_applied:
            self._pre_applied = False
            return
        self._removed = self._delete(self._page_index)

    def undo(self) -> None:
        removed = self._removed
        if removed is None:
            return
        self._insert(removed)


class InsertPageCommand(_PageCommand):
    """One blank-page insertion; undo removes it, redo restores it.

    ``insert_blank(page_index)`` creates the generated page first.
    ``delete(page_index)`` returns the restore token on undo, and
    ``restore(token)`` puts the exact page back on redo. The initial
    insertion is the only one that regenerates content; every later
    redo restores the captured page blob, so undo/redo cannot accumulate
    duplicate template content or annotations.
    """

    def __init__(self, document, page_index: int, insert_blank, delete,
                 restore, text: str = "Insert Blank Page") -> None:
        super().__init__(document, page_index, text, allow_append=True)
        if not callable(insert_blank) or not callable(delete) \
                or not callable(restore):
            raise TypeError(
                "insert_blank, delete and restore must be callable")
        self._insert_blank = insert_blank
        self._delete = delete
        self._restore = restore
        self._removed = None

    def redo(self) -> None:
        if self._pre_applied:
            self._pre_applied = False
            return
        if self._removed is None:
            self._insert_blank(self._page_index)
            return
        self._restore(self._removed)

    def undo(self) -> None:
        self._removed = self._delete(self._page_index)


class AddAnnotationCommand(QUndoCommand):
    """Insert one annotation; undo removes it again by identity.

    The constructor validates everything up front (store, annotation,
    page range, no duplicate identity) and raises before anything is
    pushed, so a pushed command cannot fail its first execution.
    Identity is never regenerated on undo/redo.
    """

    def __init__(self, store: AnnotationStore,
                 annotation: AnnotationBase, page_index: int,
                 parent=None) -> None:
        super().__init__("Add Annotation", parent)
        if not isinstance(store, AnnotationStore):
            raise TypeError(
                "store must be an AnnotationStore, got "
                f"{type(store).__name__}")
        if not isinstance(annotation, AnnotationBase):
            raise TypeError(
                "annotation must be an AnnotationBase, got "
                f"{type(annotation).__name__}")
        if not 0 <= page_index < store.page_count:
            raise ValueError(
                f"page_index {page_index!r} out of range "
                f"[0, {store.page_count})")
        if store.contains(annotation.id):
            raise ValueError(
                f"duplicate annotation identity {annotation.id!r}")
        self._store = store
        self._annotation = annotation
        self._page_index = page_index

    def redo(self) -> None:
        self._store.add(self._annotation, self._page_index)

    def undo(self) -> None:
        self._store.remove(self._annotation.id)


class DeleteAnnotationCommand(QUndoCommand):
    """Remove one annotation by identity; undo restores it in place.

    The constructor resolves the annotation and captures its page and
    exact position, raising ``ValueError`` for unknown identities
    before anything is pushed. Undo re-inserts at the captured
    position (original relative order), never appends. Redo removes
    the same identity again.
    """

    def __init__(self, store: AnnotationStore, annotation_id,
                 parent=None) -> None:
        super().__init__("Delete Annotation", parent)
        if not isinstance(store, AnnotationStore):
            raise TypeError(
                "store must be an AnnotationStore, got "
                f"{type(store).__name__}")
        annotation = store.get(annotation_id)
        if annotation is None:
            raise ValueError(
                f"unknown annotation identity {annotation_id!r}")
        located = store.locate(annotation_id)
        if located is None:  # pragma: no cover - defensive; get() hit
            raise ValueError(
                f"unknown annotation identity {annotation_id!r}")
        page_index, position = located
        self._store = store
        self._annotation = annotation
        self._annotation_id = annotation.id
        self._page_index = page_index
        self._position = position

    def redo(self) -> None:
        self._store.remove(self._annotation_id)

    def undo(self) -> None:
        self._store.insert_at(self._annotation, self._page_index,
                              self._position)


class ReplaceAnnotationCommand(QUndoCommand):
    """Replace one annotation with several; undo restores the original.

    M6.6: partial erasing can split one committed stroke into one or more
    surviving fragments. This command removes the original and inserts
    the fragments at the same page position, in order. Undo removes the
    fragments and re-inserts the original at that exact position (original
    identity, geometry, style, page, and relative order intact). Fragment
    identities are assigned once by the caller and never regenerated, so
    redo reproduces the identical fragment set. The constructor validates
    the whole substitution up front and raises before anything is pushed,
    so a pushed command cannot fail its first execution.
    """

    def __init__(self, store: AnnotationStore, annotation_id,
                 replacements, parent=None, text: str = "Erase") -> None:
        super().__init__(str(text), parent)
        if not isinstance(store, AnnotationStore):
            raise TypeError(
                "store must be an AnnotationStore, got "
                f"{type(store).__name__}")
        annotation = store.get(annotation_id)
        if annotation is None:
            raise ValueError(
                f"unknown annotation identity {annotation_id!r}")
        located = store.locate(annotation_id)
        if located is None:  # pragma: no cover - defensive; get() hit
            raise ValueError(
                f"unknown annotation identity {annotation_id!r}")
        page_index, position = located
        reps = list(replacements)
        if not reps:
            raise ValueError("replacements must be non-empty")
        # Validate now so the first redo cannot fail after a push.
        store.validate_substitute([annotation.id], reps, page_index,
                                  position)
        self._store = store
        self._annotation = annotation
        self._annotation_id = annotation.id
        self._replacements = reps
        self._page_index = page_index
        self._position = position

    def redo(self) -> None:
        self._store.substitute(
            [self._annotation_id], self._replacements,
            self._page_index, self._position)

    def undo(self) -> None:
        self._store.substitute(
            [a.id for a in self._replacements], [self._annotation],
            self._page_index, self._position)


class _MoveEntry:
    """One validated relocation slot inside a :class:`BatchMoveCommand`."""

    __slots__ = ("id_key", "original", "target", "src_page", "src_pos",
                 "dst_page")

    def __init__(self, id_key, original, target, src_page, src_pos,
                 dst_page) -> None:
        self.id_key = id_key
        self.original = original
        self.target = target
        self.src_page = src_page
        self.src_pos = src_pos
        self.dst_page = dst_page


class BatchMoveCommand(QUndoCommand):
    """Relocate a batch of annotations, including across pages (M6.8.2).

    One prevalidated user action: every entry relocates one annotation
    from its current page slot to a destination page — a same-page
    entry simply returns to its pristine slot (an in-place replacement,
    the M6.6 semantics), while a cross-page entry is removed from its
    source page and inserted on the destination page with its identity,
    type, appearance, geometry, and every unrelated property intact.

    The constructor resolves every entry against the *current*
    (pristine) store through the public API only, then simulates the
    complete redo sequence and its exact inverse in a mirror of the
    store before anything is pushed: any unknown identity, identity
    mismatch, bad destination page, duplicate entry, or impossible
    insert position raises ``ValueError`` pre-push, so a pushed command
    cannot fail its first redo. Runtime evolution follows the simulation
    step for step because it only issues the same ``remove()`` /
    ``insert_at()`` calls that were dry-run.

    Insertion-order policy (deterministic, documented):
    * a member that stays on its own page keeps its pristine slot
      exactly — its relative z-order against unrelated annotations and
      against fellow movers is untouched;
    * a member landing on a page it did not come from is appended after
      that page's existing content (painted last, i.e. on top), ordered
      by ascending ``(source page, source position)`` — deterministic,
      preserves the movers' relative order, and never reorders the
      destination page's unrelated annotations.

    Undo removes the batch's replacements and re-inserts the originals
    at their pristine page slots, restoring the exact original pages,
    positions, objects, and paint order; redo from the fully restored
    store reproduces the identical layout, so repeated undo/redo cycles
    are stable. As with every other command here, nothing rolls back at
    runtime: validation happens entirely before push.
    """

    def __init__(self, store: AnnotationStore, entries, parent=None,
                 text: str = "Move") -> None:
        super().__init__(str(text), parent)
        if not isinstance(store, AnnotationStore):
            raise TypeError(
                "store must be an AnnotationStore, got "
                f"{type(store).__name__}")
        self._store = store
        try:
            raw = list(entries)
        except TypeError:
            raise TypeError("entries must be an iterable")
        if not raw:
            raise ValueError("entries must be non-empty")
        resolved: list = []
        seen: set = set()
        for item in raw:
            try:
                annotation_id, target, dst_page = item
            except (TypeError, ValueError):
                raise TypeError(
                    "each entry must be (annotation_id, replacement, "
                    "destination_page_index)")
            if not isinstance(target, AnnotationBase):
                raise TypeError(
                    "replacement must be an AnnotationBase, got "
                    f"{type(target).__name__}")
            id_key = _identity_key(annotation_id)
            if id_key in seen:
                raise ValueError(
                    f"duplicate entry identity {id_key!r}")
            seen.add(id_key)
            if _identity_key(target.id) != id_key:
                # A relocation preserves the annotation's stable
                # identity — that is what undo/redo and selection
                # continue to be keyed on.
                raise ValueError(
                    f"replacement identity {id_key!r} does not match "
                    f"the relocated annotation")
            annotation = store.get(annotation_id)
            if annotation is None:
                raise ValueError(
                    f"unknown annotation identity {id_key!r}")
            located = store.locate(annotation_id)
            if located is None:  # pragma: no cover - defensive; get() hit
                raise ValueError(
                    f"unknown annotation identity {id_key!r}")
            src_page, src_pos = located
            if isinstance(dst_page, bool) or not isinstance(dst_page, int):
                raise TypeError(
                    f"destination_page_index must be an int, got "
                    f"{dst_page!r}")
            if not 0 <= dst_page < store.page_count:
                raise ValueError(
                    f"destination page {dst_page!r} out of range "
                    f"[0, {store.page_count})")
            resolved.append(_MoveEntry(id_key, annotation, target,
                                       src_page, src_pos, dst_page))

        # Simulate the full redo sequence and its exact inverse against
        # a lightweight mirror of the store before anything is pushed:
        # every recorded store call is proven valid (bounds, presence,
        # order) here, and the runtime replays exactly the same recorded
        # remove() / insert_at() calls in the same order, so a pushed
        # command cannot fail its first redo.
        involved = sorted({e.src_page for e in resolved}
                          | {e.dst_page for e in resolved})
        moved = [e.id_key for e in resolved]
        moved_set = set(moved)
        pristine = {p: [_identity_key(a.id) for a in store.page(p)]
                    for p in involved}
        pos_of = {p: {key: i for i, key in enumerate(pristine[p])}
                  for p in involved}

        def _pristine_slots(page, keys, state):
            """Indices re-laying ``keys`` into the pristine order.

            ``keys`` arrive in ascending pristine-position order and
            ``state`` is a pristine-ordered subset; each index is the
            count of already-present elements that precede the key in
            pristine order. Mutates ``state`` (the mirror), never the
            store.
            """
            slots = []
            lookup = pos_of[page]
            for key in keys:
                idx = 0
                for element in state:
                    if lookup[element] < lookup[key]:
                        idx += 1
                    else:
                        break
                slots.append(idx)
                state.insert(idx, key)
            return slots

        # Redo plan: (1) remove every moved identity (entry order), then
        # (2) per involved page in ascending order, re-insert the page's
        # stayers at their pristine slots and append the cross-page
        # arrivals after the page's remaining content — ascending
        # (source page, source position) among the arrivals.
        self._redo_plan = [("remove", key) for key in moved]
        for p in involved:
            state = [k for k in pristine[p] if k not in moved_set]
            stayers = sorted(
                (e for e in resolved
                 if e.src_page == p and e.dst_page == p),
                key=lambda e: e.src_pos)
            arrivals = sorted(
                (e for e in resolved
                 if e.src_page != p and e.dst_page == p),
                key=lambda e: (e.src_page, e.src_pos))
            stayer_keys = [e.id_key for e in stayers]
            slots = _pristine_slots(p, stayer_keys, state)
            for e, slot in zip(stayers, slots):
                self._redo_plan.append(("insert", e.target, p, slot))
            for e in arrivals:
                self._redo_plan.append(("insert", e.target, p,
                                        len(state)))
                state.append(e.id_key)
            expected = [k for k in pristine[p]
                        if k not in {e.id_key for e in resolved
                                     if e.src_page == p
                                     and e.dst_page != p}]
            expected += [e.id_key for e in arrivals]
            if state != expected:
                raise ValueError(
                    f"batch move plan inconsistent on page {p}")

        # Undo plan: remove every replacement, then re-insert each
        # original at its pristine slot (ascending pristine position per
        # page) — restoring the exact original pages, positions,
        # objects, and paint order.
        self._undo_plan = [("remove", key) for key in moved]
        for p in involved:
            state = [k for k in pristine[p] if k not in moved_set]
            originals = sorted(
                (e for e in resolved if e.src_page == p),
                key=lambda e: e.src_pos)
            slots = _pristine_slots(
                p, [e.id_key for e in originals], state)
            for e, slot in zip(originals, slots):
                self._undo_plan.append(("insert", e.original, p, slot))
            if state != pristine[p]:
                raise ValueError(
                    f"batch move undo plan inconsistent on page {p}")

    def _apply(self, plan) -> None:
        for step in plan:
            if step[0] == "remove":
                self._store.remove(step[1])
            else:
                self._store.insert_at(step[1], step[2], step[3])

    def redo(self) -> None:
        self._apply(self._redo_plan)

    def undo(self) -> None:
        self._apply(self._undo_plan)


class DocumentHistory:
    """One ``QUndoStack`` bound to one document's annotation store.

    Histories never cross documents: each opened document gets its own
    instance (owned by the viewer), and closing a document drops its
    history, releasing commands and their annotation values.
    """

    def __init__(self, store: AnnotationStore) -> None:
        if not isinstance(store, AnnotationStore):
            raise TypeError(
                "store must be an AnnotationStore, got "
                f"{type(store).__name__}")
        self._store = store
        self._stack = QUndoStack()
        # M6.5: True while a grouped undo step (a Qt macro) is open and
        # still accepting child commands.
        self._open_macro = False

    @property
    def store(self) -> AnnotationStore:
        """The bound annotation store."""
        return self._store

    @property
    def stack(self) -> QUndoStack:
        """The underlying Qt undo stack."""
        return self._stack

    def push_add(self, annotation: AnnotationBase,
                 page_index: int) -> AddAnnotationCommand:
        """Validate, push, and execute an insertion."""
        command = AddAnnotationCommand(self._store, annotation,
                                       page_index)
        self._stack.push(command)
        return command

    def push_delete(self, annotation_id) -> DeleteAnnotationCommand:
        """Validate, push, and execute a removal by identity."""
        command = DeleteAnnotationCommand(self._store, annotation_id)
        self._stack.push(command)
        return command

    @property
    def group_open(self) -> bool:
        """True while a grouped undo step is still accepting commands."""
        return self._open_macro

    def push_delete_grouped(self, annotation_id,
                            text: str = "Erase") -> DeleteAnnotationCommand:
        """Push a removal, opening one group on the first call (M6.5).

        The command is constructed (and fully validated) *before* the
        group is opened, so the first push cannot fail and an empty
        group can never be created. The command's ``redo()`` runs
        immediately on push, so the erased annotation leaves the store
        at once (the gesture feels immediate); every further call while
        the group is open joins the same single undo step, so one Undo
        restores everything a gesture erased and one Redo removes it
        again. ``text`` only labels the group in Qt's undo text.
        """
        command = DeleteAnnotationCommand(self._store, annotation_id)
        self._ensure_group(text)
        self._stack.push(command)
        return command

    def push_replace_grouped(self, annotation_id, replacements,
                             text: str = "Erase"
                             ) -> ReplaceAnnotationCommand:
        """Push a replacement, joining the gesture's single group (M6.6).

        Same grouping contract as :meth:`push_delete_grouped`: the
        command is validated before the group opens, so a partial-erase
        gesture mixes whole-stroke removals and geometry replacements in
        one undo step, and a no-op never opens an empty group.
        """
        command = ReplaceAnnotationCommand(self._store, annotation_id,
                                            replacements)
        self._ensure_group(text)
        self._stack.push(command)
        return command

    def push(self, command: QUndoCommand) -> QUndoCommand:
        """Push an already-constructed, validated command (M6.8.2).

        Used by the document page operations (rotate/delete page): the
        command is built and validated by its constructor before the
        push, and its ``redo()`` runs immediately. No group is opened —
        a page operation is always exactly one undo step.
        """
        self._stack.push(command)
        return command

    def push_prebuilt_grouped(self, command: QUndoCommand,
                              text: str = "Edit") -> QUndoCommand:
        """Submit an already-constructed command into the shared group.

        M6.8.2: a selection-editing gesture preconstructs and fully
        validates *every* command against the untouched store first
        (each constructor ``locate()``/``validate_substitute()`` capture
        must happen before any sibling command mutates), then submits
        them in a planned order. This helper — like the grouped helpers
        above — opens the shared gesture macro on the first submission
        and pushes the supplied command immediately (its ``redo()``
        runs at once), returning the submitted command. Deletions and
        replacements, pre-built or helper-built, join the same group.

        Caller contract: ``command`` must be completely constructed and
        validated *before* the first submission of the group — this
        method constructs nothing and validates nothing, and commands
        on the same page preconstructed against the pristine store are
        submitted in DESCENDING original position order within that
        page (later lower positions stay valid while nothing shifts
        below them). It provides no transaction rollback and cannot
        make arbitrary runtime exceptions safe: a Qt macro groups
        executions for undo, it never rolls them back.
        """
        self._ensure_group(text)
        self._stack.push(command)
        return command

    def _ensure_group(self, text: str) -> None:
        """Open the shared gesture macro once (delete + replace share it)."""
        if not self._open_macro:
            self._stack.beginMacro(str(text))
            self._open_macro = True

    def end_group(self) -> bool:
        """Finalize the open group; idempotent, never raises.

        Returns ``True`` when no group remains open afterwards — either
        none was open or ``endMacro()`` succeeded — and ``False`` when Qt
        refused to close the macro. The open-group flag is cleared only
        *after* the macro actually closes, so ``group_open`` never claims
        a still-open group is finished and the caller can tell that the
        history is not yet usable.
        """
        if not self._open_macro:
            return True
        try:
            self._stack.endMacro()
        except Exception:
            return False
        self._open_macro = False
        return True

    def undo(self) -> None:
        """Undo once; no-op when there is nothing to undo."""
        self._stack.undo()

    def redo(self) -> None:
        """Redo once; no-op when there is nothing to redo."""
        self._stack.redo()

    def can_undo(self) -> bool:
        """True iff undo would do something."""
        return self._stack.canUndo()

    def can_redo(self) -> bool:
        """True iff redo would do something."""
        return self._stack.canRedo()

    def clear(self) -> None:
        """Drop all history entries (and any open group marker)."""
        self._open_macro = False
        self._stack.clear()
