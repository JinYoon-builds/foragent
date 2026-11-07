"""Regression coverage for queue-only-main: the `fa order publish` entrance.

On current main, `fa order publish` with merge_owner on already reaches main through the merge
owner. These cases pin that this keeps working once flush also only submits. They pass on main,
so they are not part of the candidate acceptance command (tests/acceptance/test_queue_only_main.py
holds the red cases for the same statements through the flush entrance).
"""

import pytest

from queue_only_main_world import (  # noqa: F401  (fixtures)
    Stopped, command, make_plan, stop_after, locked_file, world,
)
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401
from test_queue_only_main import assert_once, lane_ready


def test_a_published_order_is_lane_ready_within_two_owner_passes_with_one_main_commit(world):
    plan = world.prepared(make_plan("ledger-import"))
    before, pushed = world.main(), world.pushes()
    world.publish(plan)
    assert world.tracker.list_issues() == [], "submitting creates no issue: the owner publishes it"
    assert world.main() == before, "submitting does not move main"

    world.owner_pass()
    world.owner_pass()

    issue = lane_ready(world, plan)
    assert_once(world, plan)
    checked = [index for index, event in enumerate(world.events) if event == ("red-check", command(plan))]
    created = [index for index, event in enumerate(world.events) if event == ("issue", issue.title)]
    assert checked and created and checked[0] < created[0], "the red check runs before the issues are created"
    assert len(world.commits_since(before)) == 1, "main gets exactly one commit for the order"
    assert world.pushes() - pushed == 1, "main is pushed once for the order"
    tip, issues = world.main(), len(world.tracker.list_issues())
    world.owner_pass()
    assert world.main() == tip and len(world.tracker.list_issues()) == issues


@pytest.mark.parametrize("step", ["commit", "push"])
def test_a_published_order_survives_an_owner_stop_after_commit_or_push(world, monkeypatch, step):
    plan = world.prepared(make_plan("restartable"))
    before = world.main()
    with stop_after(monkeypatch, step) as fired:
        world.publish(plan)
        for _ in range(2):
            try:
                world.owner_pass()
            except Stopped:
                break
    assert fired, f"the owner must reach its {step} for this order"

    world.restart()
    for _ in range(3):
        world.owner_pass()

    lane_ready(world, plan)
    assert_once(world, plan)
    assert len([event for event in world.events if event == ("issue", plan["issues"][0]["title"])]) == 1
    assert world.on_main(locked_file(plan)) is not None
    assert len(world.commits_since(before)) == 1
