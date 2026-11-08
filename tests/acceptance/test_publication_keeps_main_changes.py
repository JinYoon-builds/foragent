"""Acceptance tests for publication-keeps-main-changes (PM r1278). Locked by Foragent; do not edit.

An order's file bytes are taken when the order is prepared and written to main later, by the
merge owner. A change main received to the same file in between must survive: a change to
another part of the file is kept beside the order's own change; a change overlapping the
order's change refuses the publication with a reason naming the file, and main keeps its
content. A file main did not change is published with exactly the order's content.

The world (``publication_keeps_main_world``) is real Git with a local bare origin and the
existing fake tracker; only the measured pytest runs are scripted.
"""

from publication_keeps_main_world import (  # noqa: F401
    BOTH, LOCKED, MAIN_TAIL, MAIN_TOP, ORDER, OTHER, OTHER_ORDER, case, correction, land, move_main,
    offline_publication, origin_main, prepare, world, world_two)


def test_main_change_to_another_part_survives_beside_the_order_change(world):
    plan = correction(world, {LOCKED: ORDER})
    prepare(world, plan)
    move_main(world, {LOCKED: MAIN_TAIL})

    row = land(world, plan)

    assert row["status"] == "published", (
        "A change main made to another part of the order's file must not stop the publication; "
        f"got {row['status']}: {row.get('reason')}")
    published = origin_main(world, LOCKED)
    assert published == BOTH, (
        "Main must hold both the order's correction and main's later change to another part of "
        f"{LOCKED}, exactly once each.\n--- expected ---\n{BOTH}\n--- origin main ---\n{published}")


def test_overlapping_main_change_refuses_the_publication_and_main_keeps_its_content(world):
    plan = correction(world, {LOCKED: ORDER})
    prepare(world, plan)
    moved = move_main(world, {LOCKED: MAIN_TOP})

    row = land(world, plan)

    published = origin_main(world, LOCKED)
    assert published == MAIN_TOP, (
        "When main's change overlaps the order's change, main must keep its content; "
        f"origin main now holds:\n{published}")
    assert row["status"] != "published", (
        "An order whose change overlaps a change main made since preparation must not be published")
    assert LOCKED in (row.get("reason") or ""), (
        f"The refusal must name the file {LOCKED}; got status {row['status']} with reason {row.get('reason')!r}")
    from conftest import git_command

    assert git_command(world.origin, "rev-parse", "refs/heads/main") == moved, (
        "A refused publication must not write any commit to main")


def test_unchanged_file_publishes_exactly_while_a_changed_one_keeps_main_change(world_two):
    world = world_two
    plan = correction(world, {LOCKED: ORDER, OTHER: OTHER_ORDER})
    prepare(world, plan)
    move_main(world, {LOCKED: MAIN_TAIL})  # main changes only LOCKED; OTHER stays as prepared

    row = land(world, plan)

    assert row["status"] == "published", (
        f"The order must publish when main changed another part of one file; got {row['status']}: "
        f"{row.get('reason')}")
    assert origin_main(world, OTHER) == OTHER_ORDER, (
        f"{OTHER}, which main did not change since preparation, must hold exactly the order's content")
    assert origin_main(world, LOCKED) == BOTH, (
        f"{LOCKED} must hold both the order's correction and main's later change")
