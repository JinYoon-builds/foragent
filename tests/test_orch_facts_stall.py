"""Flow-stall kinds (#2088): supply low after a hold, requests stuck in processing, one signal per episode."""

from datetime import timedelta
from itertools import count
from types import SimpleNamespace

from conftest import BASE_TIME
from foragent import orch_facts_stall, orch_inbox, orch_plan, orch_role_raise
from foragent.config import Config
from foragent.issue import body_hash
from foragent.issue import parse_body
from foragent.orch_facts_runs import _Waiting
from foragent.state import StateStore
from foragent.tracker import Issue

CONFIG = Config(repo="owner/repo", lanes={lane: {"paths": [f"{lane}/**"]} for lane in ("kestrel", "heron", "osprey")})
SHARED = ("<!-- foragent:v1 -->\n{header}## Goal\nWork.\n## Files\n- src/shared.py\n"
          "## Done when\n```sh\ntrue\n```\n")


def store_at(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


NUMBERS = count(100)


def ready(n=1, *extra, lane="main", body=""):
    return [_Waiting(next(NUMBERS), lane, {"approved", "ready", *extra}, parse_body(body)) for _ in range(n)]


def low(store, tmp_path, running, waiting):
    return orch_facts_stall.is_low(store, CONFIG, tmp_path, store._now(), running, waiting)


def test_free_lanes_exclude_busy_ones_and_count_main(tmp_path):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    running = [SimpleNamespace(lane="osprey", number=1, started_at="x"), SimpleNamespace(lane=None, number=2,
                                                                                         started_at="y")]
    assert orch_facts_stall.free_lanes(store, CONFIG, tmp_path, clock[0], running) == ["heron", "kestrel"]
    two_lanes = [*ready(1, lane="heron"), *ready(1, lane="kestrel")]
    assert low(store, tmp_path, running, two_lanes[:1])
    assert not low(store, tmp_path, running, two_lanes)
    unready = [_Waiting(1, "heron", {"approved"}, parse_body(""))] * 5
    assert low(store, tmp_path, running, unready), "Approved but not ready work cannot start"
    for held in ("blocked", "needs-human"):
        assert low(store, tmp_path, running, [*ready(1, held, lane="heron"), *ready(1, held, lane="kestrel")]), (
            f"Ready but {held} work cannot start, so it covers no free lane")
    orch_plan.pause(store, "heron", clock[0] + timedelta(hours=1), "plan says wait")
    assert orch_facts_stall.free_lanes(store, CONFIG, tmp_path, clock[0], running) == ["kestrel"], (
        "A lane the plan paused waits on the plan, not on supply")


def test_work_the_file_rule_holds_does_not_cover_a_free_lane(tmp_path):
    """A live run reserves src/shared.py exclusively; five ready issues need that file and cannot start."""
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    reserving = SHARED.format(header="exclusive:\n- src/shared.py\n")
    store.save_approval(1, body=reserving, body_sha=body_hash(reserving), acceptance_blobs={},
                        approved_by="human", approved_at=(clock[0] - timedelta(hours=1)).isoformat())
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), lane="osprey")
    running = [store.get_run(1)]
    lanes = ("heron", "kestrel", "main", "heron", "kestrel")
    waiting = [_Waiting(10 + index, lane, {"approved", "ready"}, parse_body(SHARED.format(header="")))
               for index, lane in enumerate(lanes)]
    assert orch_facts_stall.startable(store, CONFIG, tmp_path, clock[0], running, waiting) == []
    assert low(store, tmp_path, running, waiting), "Five approved ready issues that cannot start are no supply"
    free = [_Waiting(20 + index, lane, {"approved", "ready"}, parse_body("")) for index, lane in
            enumerate(("heron", "kestrel", "main"))]
    assert not low(store, tmp_path, running, [*waiting, *free]), "Work the file rule admits covers the lanes"
    rivals = [_Waiting(30 + index, lane, {"approved", "ready"}, parse_body(
        SHARED.format(header="exclusive:\n- src/other.py\n").replace("src/shared.py", "src/other.py")))
        for index, lane in enumerate(("heron", "kestrel"))]
    assert orch_facts_stall.startable(store, CONFIG, tmp_path, clock[0], running, rivals) == [30], (
        "Two issues reserving one file: the first admitted claims it, so only one can start")


def test_supply_low_needs_the_hold_and_the_hold_resets_on_recovery(tmp_path, monkeypatch):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    state = [True]
    monkeypatch.setattr(orch_facts_stall, "is_low", lambda *args: state[0])
    orch_facts_stall.hold(store, CONFIG, tmp_path, clock[0])
    start = clock[0]
    clock[0] += timedelta(minutes=10)
    orch_facts_stall.hold(store, CONFIG, tmp_path, clock[0])
    assert orch_facts_stall._since(store) == start, "A later low tick keeps the first low time"
    almost = start + orch_facts_stall.SUPPLY_HOLD - timedelta(seconds=1)
    assert orch_facts_stall.supply_low_numbers(store, CONFIG, tmp_path, almost, [], []) == {}
    at = start + orch_facts_stall.SUPPLY_HOLD
    numbers = orch_facts_stall.supply_low_numbers(store, CONFIG, tmp_path, at, [], [])
    assert numbers == {"free_lanes": ["heron", "kestrel", "main", "osprey"], "free_slots": 4, "startable": 0,
                       "unapproved_ready": 0}
    state[0] = False
    orch_facts_stall.hold(store, CONFIG, tmp_path, clock[0])
    assert orch_facts_stall._since(store) is None, "Recovered supply ends the hold"
    state[0] = True
    clock[0] += timedelta(minutes=5)
    orch_facts_stall.hold(store, CONFIG, tmp_path, clock[0])
    assert orch_facts_stall._since(store) == clock[0], "A new dip starts a new hold"


def test_request_stuck_counts_from_its_latest_move_to_processing(tmp_path):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    old = orch_inbox.add_request(store, "orchestrator", "Turn order A into issues")["id"]
    orch_inbox.set_request_status(store, old, "processing")
    fresh = orch_inbox.add_request(store, "orchestrator", "Another")["id"]
    clock[0] += orch_facts_stall.STUCK_AFTER - timedelta(minutes=1)
    orch_inbox.set_request_status(store, fresh, "processing")
    assert orch_facts_stall.request_stuck_numbers(store, clock[0]) == {}
    clock[0] += timedelta(minutes=1)
    assert orch_facts_stall.request_stuck_numbers(store, clock[0]) == {"requests": [old]}
    assert orch_facts_stall.stuck_requests(store, clock[0])[0]["minutes"] == 180
    orch_inbox.set_request_status(store, old, "new")
    orch_inbox.set_request_status(store, old, "processing")
    assert orch_facts_stall.request_stuck_numbers(store, clock[0]) == {}, "A new move to processing restarts it"


def test_a_progress_note_in_processing_does_not_restart_the_age(tmp_path):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    request = orch_inbox.add_request(store, "orchestrator", "Turn order B into issues")["id"]
    orch_inbox.set_request_status(store, request, "processing")
    clock[0] += timedelta(hours=2)
    orch_inbox.set_request_status(store, request, "processing", "Half of the issues drafted")
    clock[0] += timedelta(hours=1)
    assert orch_facts_stall.request_stuck_numbers(store, clock[0]) == {"requests": [request]}, (
        "Processing without a break since hour zero is stuck at hour three")
    assert orch_facts_stall.stuck_requests(store, clock[0])[0]["minutes"] == 180


BODY = "## Goal\nWork.\n## Done when\n```sh\ntrue\n```\n"


def test_unapproved_ready_reads_the_current_approval_not_an_old_record(tmp_path):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    rows = {1: {"lane:heron"}, 2: {"lane:heron", "approved", "ready"}, 3: {"lane:heron", "blocked"},
            4: {"lane:heron", "needs-human"}, 5: {"lane:heron", "approved"}, 6: {"lane:heron"}}
    for number, labels in rows.items():
        store.record_issue(Issue(number, f"Issue {number}", BODY, frozenset(labels), "open"), {})
    for number in (1, 2):  # #1's approval was revoked: its label is gone, its old record stays.
        store.save_approval(number, body=BODY, body_sha="x", acceptance_blobs={}, approved_by="human",
                            approved_at=BASE_TIME.isoformat())
    store.queue_github_write(6, "comment", body="<!-- foragent:orch kind=parked -->\n#6: parked; why: waits for #9")
    # #2 is approved; #6 is parked; #3 and #4 wait on something else first; #5 has the label but no recorded approval.
    assert orch_facts_stall._unapproved_ready(store) == 2


def test_stall_kinds_are_never_worse_within_one_episode():
    before = {"kinds": {"supply_low": {"numbers": {"free_lanes": ["heron"], "free_slots": 1, "startable": 0,
                                                  "unapproved_ready": 2}}}}
    after = {"kinds": {"supply_low": {"numbers": {"free_lanes": ["heron", "kestrel", "plover"], "free_slots": 3,
                                                 "startable": 0, "unapproved_ready": 9}}}}
    assert not orch_role_raise.worse(before, after)
    assert orch_role_raise.worse({"kinds": {}}, after), "A stall absent from the baseline is a new one"
    assert orch_role_raise.clear_rearms(after), "A clear (the stall ended) re-arms the next episode"


def test_orchestrator_receives_one_signal_per_stall_kind():
    config = SimpleNamespace(roles={})
    signals = orch_role_raise._signals("orchestrator", {"signals": []}, config)
    assert [signal["kinds"] for signal in signals] == [[kind] for kind in orch_facts_stall.KINDS]
    assert orch_role_raise._signals("docs", {"signals": []}, config) == []
