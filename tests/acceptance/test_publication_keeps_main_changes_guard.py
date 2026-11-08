"""Regression guard for publication-keeps-main-changes (PM r1278). Locked by Foragent; do not edit.

Not part of Done when: these already hold on main and must keep holding. A file main did not
change since the order was prepared is published with exactly the order's content, whether
main stayed where it was or moved by changing another file.
"""

from publication_keeps_main_world import (  # noqa: F401
    LOCKED, ORDER, case, correction, land, move_main, offline_publication, origin_main, prepare, world)


def test_order_publishes_exactly_its_content_when_main_did_not_move(world):
    plan = correction(world, {LOCKED: ORDER})
    prepare(world, plan)

    row = land(world, plan)

    assert row["status"] == "published", f"{row['status']}: {row.get('reason')}"
    assert origin_main(world, LOCKED) == ORDER


def test_order_publishes_exactly_its_content_when_main_changed_another_file(world):
    plan = correction(world, {LOCKED: ORDER})
    prepare(world, plan)
    move_main(world, {"docs/unrelated.md": "Main moved on another file.\n"})

    row = land(world, plan)

    assert row["status"] == "published", f"{row['status']}: {row.get('reason')}"
    assert origin_main(world, LOCKED) == ORDER
    assert origin_main(world, "docs/unrelated.md") == "Main moved on another file.\n"
