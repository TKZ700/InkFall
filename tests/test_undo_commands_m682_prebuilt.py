"""M6.8.2 preconstructed grouped submission at the history boundary.

Selection editing preconstructs and fully validates *every* command
against the untouched store first, then submits them in a planned
order into one grouped undo step through
``DocumentHistory.push_prebuilt_grouped``. These tests prove the
history-boundary contract only: construction never mutates the store,
invalid construction never corrupts state, an empty plan opens no
group and no entry, and submitted commands execute / undo / redo
exactly like any other command — including exact identity, geometry,
store ordering, and per-page restoration across pages.

Qt macros group executions for undo; they never roll them back. Every
safety claim tested here comes from construction-time validation and
from canonical submission order (descending original position per
page for deletions), not from any rollback illusion.
"""

import pytest

from annotations.models import InkStroke
from annotations.store import AnnotationStore
from app.undo import (
    DeleteAnnotationCommand,
    DocumentHistory,
    ReplaceAnnotationCommand,
)
from core.types import AnnotationId, PageId, Point

pytestmark = pytest.mark.usefixtures("qapp")


def _stroke(i=0, page=None):
    return InkStroke(
        page_id=page or PageId(),
        points=(Point(float(i), 0.0), Point(float(i) + 1.0, 1.0 + i)),
        pressures=(0.5, 0.6),
        times_ms=(100 + i, 108 + i),
    )


def _history(pages=2):
    return DocumentHistory(AnnotationStore(pages))


# ------------------------------------------------------- A: replacement

def test_prebuilt_replace_construction_is_inert_and_submission_executes():
    history = _history(pages=2)
    original = _stroke(0)
    fragment = _stroke(1)
    history.push_add(original, 0)
    # Constructed BEFORE any group opens or anything is pushed.
    command = ReplaceAnnotationCommand(history.store, original.id,
                                       (fragment,))
    # Construction alone: store untouched, no group opened (the stack
    # holds exactly the initial add, nothing more).
    assert history.store.page(0) == (original,)
    assert history.group_open is False
    assert history.stack.count() == 1
    assert history.can_undo() is True

    entries_before = history.stack.count()
    returned = history.push_prebuilt_grouped(command, text="Resize Group")
    assert returned is command
    assert history.group_open is True
    # The replacement executed on push.
    assert history.store.page(0) == (fragment,)
    assert history.store.get(original.id) is None
    assert history.store.get(fragment.id) is fragment

    history.end_group()
    assert history.group_open is False
    # Exactly one undo entry was added for the operation.
    assert history.stack.count() == entries_before + 1

    history.undo()
    assert history.store.page(0) == (original,)
    assert history.store.get(original.id) is original
    assert history.store.get(fragment.id) is None
    history.redo()
    assert history.store.get(fragment.id) is fragment
    assert history.store.get(original.id) is None


def test_prebuilt_replace_preserves_geometry_and_position_in_undo():
    history = _history()
    neighbor = _stroke(7)
    original = _stroke(0)
    fragment = _stroke(1)
    history.push_add(original, 0)
    history.push_add(neighbor, 0)
    command = ReplaceAnnotationCommand(history.store, original.id,
                                       (fragment,))
    history.push_prebuilt_grouped(command)
    history.end_group()
    assert history.store.page(0) == (fragment, neighbor)  # position kept
    history.undo()
    restored = history.store.page(0)
    assert restored == (original, neighbor)
    assert restored[0] is original
    assert restored[0].points == original.points
    assert restored[0].pressures == original.pressures
    assert restored[0].times_ms == original.times_ms


# ------------------------------------------------------- B: prebuilt deletion

def test_prebuilt_delete_construction_is_inert_and_submission_executes():
    history = _history()
    victim = _stroke(0)
    survivor = _stroke(1)
    history.push_add(victim, 0)
    history.push_add(survivor, 0)
    # Preconstructed against the untouched two-annotation store.
    command = DeleteAnnotationCommand(history.store, victim.id)
    assert history.store.page(0) == (victim, survivor)  # inert
    assert history.group_open is False
    assert history.stack.count() == 2  # the two adds only

    entries_before = history.stack.count()
    returned = history.push_prebuilt_grouped(command, text="Delete Group")
    assert returned is command
    assert history.group_open is True
    assert history.store.page(0) == (survivor,)  # deleted on push

    history.end_group()
    assert history.stack.count() == entries_before + 1  # one step only
    history.undo()  # whole grouped step at once
    assert history.store.page(0) == (victim, survivor)
    assert history.store.get(victim.id) is victim
    history.redo()
    assert history.store.page(0) == (survivor,)
    assert history.store.get(victim.id) is None


# -------------------------------------- C: cross-page grouped replacement

def test_cross_page_grouped_replacements_is_one_undo_step():
    history = _history(pages=3)
    a0, a1 = _stroke(0), _stroke(1)
    a0r1, a0r2 = _stroke(10), _stroke(11)
    a1r1, a1r2 = _stroke(12), _stroke(14)
    c0, c0r = _stroke(2), _stroke(13)
    history.push_add(a0, 0)
    history.push_add(a1, 0)
    history.push_add(c0, 2)
    baseline = {0: history.store.page(0), 2: history.store.page(2)}

    # Every replacement preconstructed against the pristine store,
    # before any group opens and before any sibling pushes. Positions
    # captured: a0 -> 0, a1 -> 1 on page 0; c0 -> 0 on page 2.
    command0 = ReplaceAnnotationCommand(history.store, a0.id,
                                        (a0r1, a0r2))
    command1 = ReplaceAnnotationCommand(history.store, a1.id,
                                        (a1r1, a1r2))
    command2 = ReplaceAnnotationCommand(history.store, c0.id, (c0r,))
    assert history.store.page(0) == baseline[0]  # still inert
    assert history.store.page(2) == baseline[2]

    entries_before = history.stack.count()
    # Same-page commands preconstructed against the pristine store are
    # submitted in DESCENDING original position order (a1 at pos 1
    # first, then a0 at pos 0) so nothing shifts below an already
    # captured position; the page-2 command is independent.
    history.push_prebuilt_grouped(command1, text="Edit Group")
    history.push_prebuilt_grouped(command0)
    history.push_prebuilt_grouped(command2)
    assert history.store.page(0) == (a0r1, a0r2, a1r1, a1r2)
    assert history.store.page(2) == (c0r,)
    history.end_group()
    assert history.group_open is False
    assert history.stack.count() == entries_before + 1  # ONE edit step

    for _ in range(2):
        history.undo()
        # One undo restores the exact original sequence on every page.
        assert history.store.page(0) == baseline[0]
        assert history.store.page(0)[0] is a0
        assert history.store.page(0)[1] is a1
        assert history.store.page(2) == baseline[2]
        assert history.store.page(2)[0] is c0
        for gone in (a0r1, a0r2, a1r1, a1r2, c0r):
            assert history.store.get(gone.id) is None

        history.redo()
        assert history.store.page(0) == (a0r1, a0r2, a1r1, a1r2)
        assert history.store.page(2) == (c0r,)
        for gone in (a0, a1, c0):
            assert history.store.get(gone.id) is None


# ---------------------------------------- D: cross-page grouped deletion

def test_cross_page_grouped_deletions_restore_exact_sequences_repeatedly():
    history = _history(pages=3)
    page0 = [_stroke(i) for i in range(5)]     # a0..a4, delete a1, a3
    page1 = [_stroke(i) for i in (20, 21, 22)]  # delete b0 (pos 0)
    page2 = [_stroke(i) for i in (30, 31)]      # survivors only
    for s in page0:
        history.push_add(s, 0)
    for s in page1:
        history.push_add(s, 1)
    for s in page2:
        history.push_add(s, 2)
    original = {0: history.store.page(0), 1: history.store.page(1),
                2: history.store.page(2)}

    # Preconstruct every deletion against the untouched store.
    del_a3 = DeleteAnnotationCommand(history.store, page0[3].id)  # pos 3
    del_a1 = DeleteAnnotationCommand(history.store, page0[1].id)  # pos 1
    del_b0 = DeleteAnnotationCommand(history.store, page1[0].id)  # pos 0

    # Submit in DESCENDING original position order within each page;
    # page order is deterministic (page 0, then page 1).
    entries_before = history.stack.count()
    history.push_prebuilt_grouped(del_a3, text="Delete Group")
    history.push_prebuilt_grouped(del_a1)
    history.push_prebuilt_grouped(del_b0)
    history.end_group()

    assert history.store.page(0) == (page0[0], page0[2], page0[4])
    assert history.store.page(1) == (page1[1], page1[2])
    assert history.store.page(2) == original[2]  # untouched
    history.end_group()
    assert history.group_open is False
    assert history.stack.count() == entries_before + 1  # ONE delete step

    for _ in range(2):
        history.undo()
        # Every page's exact original sequence, with identical objects.
        assert history.store.page(0) == original[0]
        assert history.store.page(0) == tuple(page0)
        assert history.store.get(page0[1].id) is page0[1]
        assert history.store.get(page0[3].id) is page0[3]
        assert history.store.page(1) == original[1]
        assert history.store.get(page1[0].id) is page1[0]
        assert history.store.page(2) == original[2]

        history.redo()
        assert history.store.page(0) == (page0[0], page0[2], page0[4])
        assert history.store.page(1) == (page1[1], page1[2])
        assert history.store.page(2) == original[2]
        for gone in (page0[1], page0[3], page1[0]):
            assert history.store.get(gone.id) is None


# ------------------------- E: preflight failure and no-op contracts

def test_invalid_construction_fails_before_any_push_without_corruption():
    history = _history(pages=2)
    present = _stroke(0)
    history.push_add(present, 0)
    store_before = {0: history.store.page(0), 1: history.store.page(1)}
    stack_before = history.stack.count()
    can_undo_before = history.can_undo()

    # Unknown identity: ReplaceAnnotationCommand raises in the
    # constructor, before any group opens and before anything mutates.
    with pytest.raises(ValueError):
        ReplaceAnnotationCommand(history.store, AnnotationId("ghost"),
                                 (_stroke(9),))
    with pytest.raises(ValueError):
        DeleteAnnotationCommand(history.store, AnnotationId("ghost"))

    assert history.stack.count() == stack_before
    assert history.can_undo() is can_undo_before
    assert history.group_open is False
    assert history.store.page(0) == store_before[0]
    assert history.store.page(1) == store_before[1]

    # A later valid prebuilt submission still works: no stale group,
    # no corrupted stack.
    replacement = _stroke(5)
    good = ReplaceAnnotationCommand(history.store, present.id,
                                    (replacement,))
    history.push_prebuilt_grouped(good)
    history.end_group()
    assert history.store.page(0) == (replacement,)
    assert history.stack.count() == stack_before + 1  # one grouped step


def test_empty_plan_makes_no_submission_and_opens_no_macro():
    history = _history(pages=2)
    s = _stroke(0)
    history.push_add(s, 0)
    can_undo_before = history.can_undo()
    # An empty operation plan simply never calls
    # push_prebuilt_grouped; finalizing is the idempotent no-op.
    history.end_group()
    assert history.group_open is False
    assert history.stack.count() == 1  # the add only
    assert history.can_undo() is can_undo_before
    assert history.store.page(0) == (s,)
    # No phantom entry to undo: undoing removes the add, not a group.
    history.undo()
    assert history.store.page(0) == ()
    assert history.can_undo() is False


def test_prebuilt_submission_shares_the_group_with_grouped_helpers():
    history = _history()
    replaced = _stroke(0)
    deleted = _stroke(1)
    survivor = _stroke(2)
    replacement = _stroke(9)
    for s in (replaced, deleted, survivor):
        history.push_add(s, 0)
    # One gesture mixing a helper-built deletion and a prebuilt
    # replacement joins ONE group.
    entries_before = history.stack.count()
    history.push_delete_grouped(deleted.id, text="Edit Group")
    command = ReplaceAnnotationCommand(history.store, replaced.id,
                                       (replacement,))
    history.push_prebuilt_grouped(command)
    history.end_group()
    assert history.store.page(0) == (replacement, survivor)
    assert history.stack.count() == entries_before + 1  # ONE grouped step
    history.undo()
    assert history.store.page(0) == (replaced, deleted, survivor)
    assert history.store.get(replaced.id) is replaced
    assert history.store.get(deleted.id) is deleted
    history.redo()
    assert history.store.page(0) == (replacement, survivor)


def test_invalid_first_prebuilt_leaves_no_open_macro():
    history = _history()
    s = _stroke(0)
    history.push_add(s, 0)
    with pytest.raises(ValueError):
        history.push_prebuilt_grouped(
            DeleteAnnotationCommand(history.store, AnnotationId("??")))
    # The ValueError came from the constructor, before the helper ran:
    # no group was opened and the stack still only holds the add.
    assert history.group_open is False
    assert history.stack.count() == 1
    assert history.store.page(0) == (s,)
