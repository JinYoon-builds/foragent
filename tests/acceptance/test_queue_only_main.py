"""Acceptance tests for queue-only-main: orders and red checks reach main only through the
merge owner's queue (2026-11-07, Jin q57 A).

With merge_owner on, `fa order publish` and `fa order flush` only submit; the red checker
returns its verdict; the merge owner is the one process that writes main for them. No
Foragent file lock is held while a process fetches, pushes, calls GitHub, or waits.

Entry points: `order.publish` (fa order publish [--defer]), `order_notes.append`
(fa order note --defer), `order_flush.flush` (fa order flush), and one gate pass
(`order_publications.check_pending`, the gate's red-check process, then `merge_owner.tick`).
The world (tests/acceptance/queue_only_main_world.py) is a real local Git origin through the
offline transport, the fake GitHub tracker and fake measured test runs. No network, model or
signal. Assertions read origin's main, the tracker's issues, the request queues and the
orch event journal.
"""

import threading
import time

import pytest

from foragent import order, order_prepare, order_prepare_owner
from foragent.git import GitError
from queue_only_main_world import (  # noqa: F401  (fixtures)
    PASS_OUTPUT, Stopped, Train, command, guarded, make_plan, slow_push, stop_after, locked_file, world,
)
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401


def lane_ready(world, plan):
    """The order's statement issue exists once, open, approved, and resolves to its lane."""
    from foragent.labels import issue_lane

    found = world.child(plan)
    assert len(found) == 1, f"the order's issue must exist exactly once, found {len(found)}"
    issue = found[0]
    assert issue.state == "open", issue
    assert "approved" in issue.labels, f"an approved order's issue is ready for its lane: {sorted(issue.labels)}"
    assert issue_lane(issue.labels, world.config.lanes) == plan["issues"][0]["lane"]
    return issue


def assert_once(world, plan):
    for title, copies in world.issues_of(plan).items():
        assert len(copies) == 1, f"{title!r} must exist exactly once, found {len(copies)}"


# Statement 1 -----------------------------------------------------------------------------------
# With merge_owner on, one approved order goes from publish request through red check to issues
# that exist and are lane-ready within two owner passes, and main gets one commit for it.

def test_one_approved_order_is_lane_ready_within_two_owner_passes_with_one_main_commit(world):
    # The flush entrance: today flush writes main itself. The `fa order publish` entrance already
    # goes through the owner on main and is kept as regression coverage in
    # test_queue_only_main_regression.py.
    plan = world.prepared(make_plan("ledger-import"))
    before, pushed = world.main(), world.pushes()
    world.defer(plan)
    world.flush()
    assert world.tracker.list_issues() == [], "submitting creates no issue: the owner publishes it"
    assert world.main() == before, "submitting does not move main"

    world.owner_pass()
    world.owner_pass()

    issue = lane_ready(world, plan)
    assert_once(world, plan)
    checked = [index for index, event in enumerate(world.events) if event == ("red-check", command(plan))]
    created = [index for index, event in enumerate(world.events) if event == ("issue", issue.title)]
    assert checked, "the order's Done when must be red-checked before it is published"
    assert created and checked[0] < created[0], "the red check runs before the issues are created"
    landed = world.commits_since(before)
    assert len(landed) == 1, f"main gets exactly one commit for the order, got {len(landed)}"
    assert world.pushes() - pushed == 1, "main is pushed once for the order"
    assert world.on_main(locked_file(plan)) == plan["issues"][0]["acceptance_files"][locked_file(plan)].rstrip("\n")
    # A further pass changes nothing.
    tip, issues = world.main(), len(world.tracker.list_issues())
    world.owner_pass()
    assert world.main() == tip and len(world.tracker.list_issues()) == issues


# Statement 2 -----------------------------------------------------------------------------------
# With merge_owner on, `fa order flush` does not push to the remote; its orders and notes reach
# main through the owner.

def test_flush_does_not_push_and_its_orders_and_notes_reach_main_through_the_owner(world):
    plan = world.prepared(make_plan("ledger-export"))
    world.defer(plan)
    decision = "Jin chose the owner queue as the one path to main."
    world.note(decision)
    before, pushed = world.main(), world.pushes()

    result = world.flush()

    assert world.pushes() == pushed, "fa order flush must not push to the remote"
    assert world.main() == before, "fa order flush must not move main"
    assert world.tracker.list_issues() == [], "fa order flush leaves the issues to the owner"
    assert result["status"] != "published", f"the boundary is queued, not published by flush: {result}"

    for _ in range(3):
        world.owner_pass()

    lane_ready(world, plan)
    assert_once(world, plan)
    assert world.on_main(locked_file(plan)) is not None, "the flushed order's tests reach main"
    decisions = world.on_main("docs/decisions.md") or ""
    assert decisions.count(decision) == 1, "the flushed note reaches main exactly once"
    # Flushing the same boundary again, and more passes, add nothing.
    tip, pushed = world.main(), world.pushes()
    world.flush()
    world.owner_pass()
    assert world.main() == tip and world.pushes() == pushed
    assert_once(world, plan)


# Statement 3 -----------------------------------------------------------------------------------
# When the red check finds a Done when command that passes on main, the request returns to
# preparation with that output, while another pending request is still applied and a running
# train still lands, and no lock-timeout is reported.

def test_a_green_done_when_returns_while_the_other_request_and_the_running_train_land(world, monkeypatch):
    from foragent import main_write

    returned = world.prepared(make_plan("green-already"))
    other = world.prepared(make_plan("still-red"))
    world.passing.add(command(returned))
    train = Train(world, monkeypatch, ci_passes_at_pass=2)
    assert train.running
    # A wait for the train, if any process makes one, costs no wall time here.
    clock = [0.0]
    monkeypatch.setattr(main_write, "time", type("FakeTime", (), {
        "monotonic": staticmethod(lambda: clock[0]),
        "sleep": staticmethod(lambda seconds: clock.__setitem__(0, clock[0] + max(seconds, 1.0)))}))

    world.publish(returned)
    world.defer(other)
    try:
        world.flush()
    except (order.PlanError, GitError) as exc:
        pytest.fail(f"fa order flush during a running train must submit and return, not fail: {exc}")

    for _ in range(4):
        world.owner_pass()

    row = world.requests_of(returned)[-1]
    assert row["status"] == "preparation", f"the green request returns to preparation, not {row['status']}"
    assert PASS_OUTPUT in (row["reason"] or ""), "the return carries the passing command's output"
    shown = order_prepare.prepare(returned, world.root, world.config, world.git, world.store, status=True)
    assert PASS_OUTPUT in (shown[0]["reason"] or ""), "preparation shows the same output"
    assert world.child(returned) == [], "the returned order has no issues"
    assert world.on_main(locked_file(returned)) is None, "the returned order does not land"

    lane_ready(world, other)
    assert_once(world, other)
    assert ("red-check", command(other)) in world.events, "the other request went through its red check"
    assert world.on_main(locked_file(other)) is not None, "the other pending request is applied"

    assert train.landed_commit is not None and not train.running, "the running train still lands"
    assert world.on_main(Train.FILE) is not None, "the train's change is on main"
    assert world.lock_timeouts() == [], f"no lock-timeout is reported: {world.lock_timeouts()}"


# Statement 4 -----------------------------------------------------------------------------------
# When the owner process stops after committing or after pushing a publication and starts again,
# the order's issues exist once, with no duplicate.

@pytest.mark.parametrize("step", ["commit", "push"])
def test_an_owner_stopped_after_commit_or_push_publishes_the_issues_once_after_restart(world, monkeypatch, step):
    # The flush entrance; the publish entrance passes on main and lives in the regression file.
    plan = world.prepared(make_plan("restartable"))
    before = world.main()
    with stop_after(monkeypatch, step) as fired:
        try:
            world.defer(plan)
            world.flush()
        except Stopped:
            pass  # Only a build whose flush writes main itself stops here; the owner must then recover.
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
    world.flush()  # Running the same boundary again after the restart adds nothing.
    world.owner_pass()
    assert_once(world, plan)
    assert len([event for event in world.events if event == ("issue", plan["issues"][0]["title"])]) == 1, (
        "the order's issue is created once across the stop and the restart")
    assert world.on_main(locked_file(plan)) is not None
    landed = [sha for sha in world.commits_since(before)]
    assert len(landed) == 1, f"main gets the order once, got {len(landed)} commits"


# Statement 5 -----------------------------------------------------------------------------------
# While the owner spends 30 seconds in a push, a flush and a preparation claim each finish
# waiting within 1 second.

def test_a_flush_and_a_preparation_claim_do_not_wait_for_an_owner_push(world, monkeypatch):
    landing = world.prepared(make_plan("slow-push"))
    waiting = world.prepared(make_plan("queued-meanwhile"))
    world.publish(landing)
    world.defer(waiting)
    world.note("A note flushed while the owner pushes.")
    from foragent import order_publications
    from foragent.scheduler import TickReport

    order_publications.check_pending(world.ctx, TickReport())  # The landing request is certified.
    errors, elapsed = [], {}

    def run(name, action):
        started = time.monotonic()
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(f"{name}: {exc!r}")
        finally:
            elapsed[name] = time.monotonic() - started

    claimed = []
    record = {"plan": "unrelated-order", "issues": {}}
    folder = world.store.root / "prepare"

    with slow_push(monkeypatch, seconds=30.0) as (pushing, release):
        from foragent import merge_owner

        owner = threading.Thread(target=run, args=("owner", lambda: merge_owner.tick(world.ctx, TickReport())),
                                 daemon=True)
        owner.start()
        assert pushing.wait(15), "the owner must reach its push for the certified order"
        flush = threading.Thread(target=run, args=("flush", world.flush), daemon=True)
        claim = threading.Thread(target=run, args=("claim", lambda: claimed.append(order_prepare_owner.claim(
            folder, record, ["issue"], lambda: None))), daemon=True)
        flush.start()
        claim.start()
        flush.join(2.0)
        claim.join(2.0)
        release.set()
        for thread in (flush, claim, owner):
            thread.join(30)
    assert elapsed["flush"] < 1.0, f"fa order flush waited {elapsed['flush']:.2f}s for the owner's push"
    assert elapsed["claim"] < 1.0, f"the preparation claim waited {elapsed['claim']:.2f}s for the owner's push"
    assert not errors, errors
    assert claimed == [True], "the preparation claim is taken"
    assert world.lock_timeouts() == []


# Statement 6 -----------------------------------------------------------------------------------
# A fake network call (fetch, push, GitHub, CI status) made while any foragent file lock is held
# fails the test.

def scenario_publish(world):
    plan = world.prepared(make_plan("guarded-publish"))
    world.publish(plan)
    for _ in range(2):
        world.owner_pass()
    return [plan]


def scenario_flush(world):
    plan = world.prepared(make_plan("guarded-flush"))
    world.defer(plan)
    world.note("A guarded decision.")
    world.flush()
    for _ in range(3):
        world.owner_pass()
    return [plan]


def scenario_returned(world):
    green = world.prepared(make_plan("guarded-green"))
    red = world.prepared(make_plan("guarded-red"))
    world.passing.add(command(green))
    world.publish(green)
    world.publish(red)
    for _ in range(3):
        world.owner_pass()
    assert world.requests_of(green)[-1]["status"] == "preparation"
    return [red]


@pytest.mark.parametrize("scenario", [scenario_publish, scenario_flush, scenario_returned])
def test_no_network_call_is_made_while_a_foragent_lock_is_held(world, monkeypatch, scenario):
    with guarded(world, monkeypatch) as guard:
        published = scenario(world)
    for plan in published:
        lane_ready(world, plan)
    kinds = {call.split()[0] + " " + call.split()[1] for call in guard.calls}
    assert any(call.startswith("git push") for call in kinds), "the scenario must push (the guard saw no push)"
    assert any(call.startswith("GitHub") for call in kinds), "the scenario must call GitHub"
    assert guard.violations == [], "network calls made while a Foragent file lock was held:\n" + "\n".join(
        sorted(set(guard.violations)))
