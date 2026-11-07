"""Acceptance test for path-phase-records, simulation test S5.

Statement: in the simulation world with 2,000 issues, an issue approved before all the
others still shows its first phase record when it merges.

The simulation world is the unattended-cycle world (#2452): the real scheduler, runner,
tracker, publication check and merge owner, local Git, a fake GitHub service and fake
executors. Issue #1 is approved first and the real gate observes it. Then 2,000 other issues
are approved and observed: their observations are written through the same store API and
in the same shape the gate writes for a waiting approved issue (an issue-state record and a
ready/waiting record each). Listing 2,000 issues through the fake GitHub CLI costs about
0.4 s per issue per tick (measured), so the gate does not list them itself; see the notes
of this preparation. Then the gate and lanes run #1 unattended until it merges.

On main the flow file keeps two 1,024-line generations, so 4,000 later records push #1's
first records out before it merges.
"""

from datetime import datetime, timedelta

from unattended_cycle_world import PLAIN_LOCK, World

from foragent import orch_events

OTHERS = 2000


def own_flow(store, n):
    return [row for row in store.flow_records() if row["issue"] == n]


def _key(row):
    return (row["kind"], row["at"], row["run_started_at"], repr(sorted(row["data"].items())))


def test_s5_issue_approved_before_2000_others_still_shows_its_first_phase_record_when_it_merges(
        tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, lock=PLAIN_LOCK)
    world.tick(start_runs=False)  # The gate observes #1, approved before every other issue.
    store = world.store()
    first_rows = own_flow(store, 1)
    assert first_rows, "Fixture: the gate records #1's first observation when it sees the approved issue"
    first = first_rows[0]
    first_seen = datetime.fromisoformat(first["at"])
    first_phases = [event for event in orch_events.events(store, "phase") if event.get("issue") == 1]

    # 2,000 other approved issues are observed while #1 waits.
    for n in range(10, 10 + OTHERS):
        world.clock += timedelta(milliseconds=200)
        store.record_flow("issue-state", issue=n, labels=["approved"], blocked_by=[], state="open")
        store.record_flow("ready", issue=n, run_started_at=None, reason="starts-off", outcome="waiting")

    assert world.drive(lambda w: w.merged(), ticks=20, step=300), (
        "Fixture: #1 must reach merged unattended: " + world.store().log_path(1).read_text()[-2000:])
    assert world.merged() == ["Member 1 (#1)"], world.merged()

    store = world.store()
    own = own_flow(store, 1)
    kept = [row for row in own if _key(row) == _key(first)]
    assert len(kept) == 1, (
        f"#1's first flow record ({first['kind']} at {first['at']}) must still be kept once when #1 merges; "
        f"its earliest kept records are {[(row['kind'], row['at']) for row in own[:3]]}")
    assert min(datetime.fromisoformat(row["at"]) for row in own) == first_seen, (
        "Nothing of #1's history before its first record may be invented, and nothing after it may replace it")
    assert own[0]["at"] == first["at"], "#1's first record is still its earliest kept record"
    phases = [event for event in orch_events.events(store, "phase") if event.get("issue") == 1]
    for event in first_phases:
        assert event in phases, f"#1's first phase journal event must still be kept: {event}"
    assert phases, "#1 has phase records from its run when it merges"
