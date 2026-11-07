"""Acceptance test for sim-no-lock-timeout (simulation test S6, Jin r1027 on 2026-11-07).

Statement: in the simulation world, two publications, a preparation claim and a running
train overlap for a full train cycle, every one of them completes, and no lock-timeout
event is recorded.

The world (tests/acceptance/no_lock_timeout_world.py) is the unattended-cycle world: the
real scheduler, runner, publication check and merge owner, local Git, the executable fake
GitHub service and fake executors. Two approved issues become a real merge train whose CI
stays pending until the test releases it. While the window is open, every push and fetch of
the gate process takes real time (a slow remote), and at each of them a preparation claim
(the claim `fa order prepare` makes) starts on its own thread. The lock wait limit is set
below that latency, so any foragent lock held across a fetch, a push or another wait makes
a concurrent claim give up and record a lock-timeout event.

On 11-07 four lock-timeout events came from publish, preparation and a merge train
overlapping. Entry points: the gate tick (scheduler, publication check, merge owner), the
orchestrator's `fa order preread` and `fa order publish --approve`, and
`order_prepare_owner.claim`. Assertions read origin's main, the fake GitHub state, the
publication queue, the claims' results and the sandbox's orch event journals.
"""

from conftest import git_command
from no_lock_timeout_world import OverlapWorld, publication_order
from unattended_cycle_world import FakeOrchestrator

CI_PENDING_TICKS = 3   # Gate ticks (300 s each) the train's CI stays pending while work overlaps.
FINISH_TICKS = 8       # Gate ticks allowed after the train's CI passes.


def on_main(world, path):
    names = git_command(world.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    return world.main_file(path) if path in names else None


def completed(world, plans):
    rows = {row["plan"]["id"]: row for row in world.publications()}
    return (not world.train_running() and world.train_heads
            and all(rows.get(plan["id"], {}).get("status") == "published" for plan in plans))


def test_publications_a_preparation_claim_and_a_train_overlap_and_all_complete_without_a_lock_timeout(
        tmp_path, monkeypatch):
    world = OverlapWorld(tmp_path, monkeypatch)
    orchestrator = FakeOrchestrator(world)
    before = git_command(world.origin, "rev-parse", "main")

    # The two approved issues run and their reviewed PRs become one train; its CI is pending.
    world.tick(start_runs=True)
    world.step()
    assert world.train_running(), "the two reviewed PRs must form a merge train"
    assert world.held, "the train's CI must be pending (no acceptance report for its tip yet)"
    assert world.lock_timeouts() == [], "no lock-timeout before the overlap starts"

    # The overlap window: slow remote, a preparation claim at every push and fetch.
    world.window = True
    first, second = publication_order(world, 7), publication_order(world, 8)
    orchestrator.publish(first)
    assert world.train_running(), "the first publication is submitted while the train runs"
    for _ in range(CI_PENDING_TICKS):
        world.step()
        assert world.train_running() and world.held, "the train's CI stays pending during the overlap"
    orchestrator.publish(second)
    assert world.train_running(), "the second publication is submitted while the train runs"
    world.step()
    assert world.train_running(), "the train is still running when both publications are in flight"

    # The train's CI passes: the train cycle ends, everything in flight must complete.
    world.release_train_ci()
    for _ in range(FINISH_TICKS):
        world.step()
        if completed(world, [first, second]):
            break
    world.step()  # One more tick: nothing is done twice.
    world.window = False
    unfinished = world.join_claims()
    assert unfinished == [], f"every preparation claim finishes: {unfinished}"

    # The claims overlapped the train and the owner's network calls.
    during_train = [row for row in world.claims if row["train_running"]]
    during_push = [row for row in world.claims if row["moment"].startswith("during push")]
    assert during_train, "a preparation claim must be made while the train runs"
    assert during_push, "a preparation claim must be made while the gate pushes"

    # The running train completes: it lands once and its ref is gone.
    assert not world.train_running(), f"the train must finish its cycle: {world.train_refs()}"
    landed = git_command(world.origin, "log", "--first-parent", "--format=%s", f"{before}..main").splitlines()
    for n in (1, 2):
        assert on_main(world, f"src/member_{n}.py") == f"VALUE = {n}", f"train member #{n} lands on main"
        assert sum(subject.startswith(f"Merge member #{n}") or subject == f"Member {n} (#{n})"
                   for subject in landed) == 1, f"train member #{n} lands exactly once: {landed}"
        assert world.state()["issues"][str(n)]["state"] == "closed", f"issue #{n} is closed by its merge"

    # Both publications complete: published once, their issues exist once, their tests on main.
    rows = {row["plan"]["id"]: row for row in world.publications()}
    for plan in (first, second):
        row = rows.get(plan["id"])
        assert row is not None and row["status"] == "published", (
            f"publication {plan['id']} must complete: {row and (row['status'], (row.get('reason') or '')[:300])}")
        issue = plan["issues"][0]
        found = world.created(issue["title"])
        assert len(found) == 1, f"{issue['title']!r} must exist exactly once, found {len(found)}"
        assert found[0]["state"] == "open"
        assert "approved" in {label["name"] for label in found[0]["labels"]}, "the published issue is approved"
        assert len(world.created(plan["title"])) == 1, f"{plan['title']!r} must exist exactly once"
        test_path, text = next(iter(issue["acceptance_files"].items()))
        assert on_main(world, test_path) == text.rstrip("\n"), f"{test_path} reaches main"
    assert len(world.publications()) == 2, "each publication is one request"

    # Every preparation claim completes: it is taken, not reported as waiting.
    failed = [row for row in world.claims if row["result"] is not True or row["error"]]
    assert failed == [], (f"{len(failed)} of {len(world.claims)} preparation claims did not complete: "
                          + "; ".join(f"{row['moment']} (train running: {row['train_running']}) -> "
                                      f"{row['error'] or row['result']} after {row['seconds']:.2f}s"
                                      for row in failed[:5]))

    # No lock-timeout event anywhere in the sandbox.
    timeouts = world.lock_timeouts()
    assert timeouts == [], f"{len(timeouts)} lock-timeout events: " + "; ".join(
        event["summary"] for event in timeouts[:5])

    # Only fakes, and no PM or person action.
    assert world.network_calls == [], "no network call"
    assert world.pm_calls == [] and world.person_routed() == set()
