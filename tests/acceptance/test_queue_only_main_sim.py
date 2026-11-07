"""Simulation test S2 for queue-only-main (Jin r1027, q64 A).

Statement: in the simulation world (the unattended-cycle world with the real merge owner and a
train), an order published while a train's CI is pending lands on main after the train, the
train lands, a preparation claim made during the train finishes, and no lock-timeout event is
recorded.

World: tests/acceptance/queue_only_main_sim_world.py, the S1 world
(tests/acceptance/unattended_cycle_world.py) with batch merge on. The real scheduler, runner,
GhTracker (over the executable fake GitHub), publication check and merge owner run; models
are fakes. Two approved issues become one real merge train; its CI stays pending until the
test releases it.

While the train's CI is pending, an order is published through the boundary path
(`fa order publish --approve --defer`, then `fa order flush`) and a preparation claim
(`order_prepare_owner.claim`, as `fa order prepare` takes it) is made while the flush is in its
first GitHub call. The fake GitHub answers in milliseconds where the real one takes seconds,
so Foragent's lock-wait limit is 1 s here (FORAGENT_LOCK_TIMEOUT_SECONDS): a claim that has to
wait for a publication's GitHub calls then shows up as a lock-timeout event, as on 11-07.
"""

from queue_only_main_sim_world import ClaimDuringCommand, TrainWorld, member_order
from unattended_cycle_world import FakeOrchestrator
from foragent import order_publications

BOUNDARY = "after-train"
NINE_TEST = "tests/acceptance/test_member_9.py"


def test_s2_an_order_published_during_a_pending_train_lands_after_the_train_with_no_lock_timeout(
        tmp_path, monkeypatch):
    monkeypatch.setenv("FORAGENT_LOCK_TIMEOUT_SECONDS", "1")
    world = TrainWorld(tmp_path, monkeypatch)
    orchestrator = FakeOrchestrator(world)

    # Two approved issues run unattended and become one merge train with CI pending.
    orchestrator.publish(member_order(world, "pair", [7, 8]))
    assert world.drive(lambda w: w.train_pending(), ticks=20, step=60), (
        "the two approved issues must become a merge train whose CI is pending")
    train = world.train_refs()
    before = world.first_parent()[-1]

    # During the train: an order is published through the boundary, and a preparation claim
    # is made while that publication is in its first GitHub call.
    path = world.write_order(member_order(world, "nine", [9]))
    code, out = world.fa("order", "publish", str(path), "--approve", "--defer", BOUNDARY)
    assert code == 0, out
    claim = ClaimDuringCommand(world, monkeypatch)
    claim.armed = True
    code, out = world.fa("order", "flush", "--boundary", BOUNDARY)
    claim.armed = False
    assert code == 0, f"fa order flush during the train must submit and return: {out}"
    claim.finish()
    assert claim.during_train, "the claim must be made while the train's CI is pending"
    assert claim.error is None, f"the preparation claim made during the train must finish: {claim.error}"
    assert claim.result is True, (
        f"the preparation claim made during the train must be taken (it gave up after {claim.elapsed:.2f}s)")

    # The order waits while the train runs.
    world.drive(lambda w: False, ticks=2, step=60)
    assert world.train_refs() == train and world.train_pending(), "the train is still running"
    assert world.introduced(NINE_TEST) is None, "the order does not land on main during the train"

    # The train's CI passes: the train lands, then the order lands.
    world.release_train_ci()

    def finished(w):
        rows = order_publications.requests(w.store())
        return (not w.train_refs() and w.introduced(NINE_TEST) is not None
                and rows and all(row["status"] == "published" for row in rows))

    landed = world.drive(finished, ticks=12, step=60)
    rows = [(row["title"], row["status"], (row.get("reason") or "")[:300])
            for row in order_publications.requests(world.store())]
    trained = [world.introduced("src/member_7.py"), world.introduced("src/member_8.py")]
    assert all(trained), "the train lands: both members reach main"
    assert not world.train_refs(), "the train is finished"
    assert landed, f"the order published during the train lands on main after it; requests: {rows}"
    history = world.first_parent()
    nine = world.introduced(NINE_TEST)
    assert history.index(before) < max(history.index(sha) for sha in trained) < history.index(nine), (
        "the order lands on main after the train")
    titles = [issue["title"] for issue in world.state()["issues"].values()]
    assert titles.count("Member 9") == 1, "the order's issue exists once"
    assert world.lock_timeouts() == [], f"no lock-timeout event is recorded: {world.lock_timeouts()}"
    assert world.network_calls == [] and world.pm_calls == [] and world.person_calls == []
