"""Flow-stall kinds (#2088): supply low after a hold, requests stuck in processing, one signal per episode.

Supply is read through `fa orch facts` itself (``orch_facts.collect``): startable means what the
gate's admission replay would start, so each hold kind is driven through real records."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from conftest import BASE_TIME
from foragent import capacity, orch_facts, orch_facts_stall, orch_inbox, orch_plan, orch_role_raise
from foragent.config import Config
from foragent.issue import body_hash
from foragent.state import StateStore
from foragent.tracker import Issue

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
LANES = ("kestrel", "heron", "osprey")


def store_at(tmp_path, clock):
    return StateStore(tmp_path / "state", now=lambda: clock[0])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.1))
    store = StateStore(tmp_path / "data" / "owner__repo", now=lambda: NOW)
    store.root.mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "README.md").write_text("x\n")  # Lets the case-sensitivity probe answer.
    capacity.write_state(store, 8, NOW)
    return store, tmp_path / "repo"


def approve(store, number, lane, files, *, exclusive=(), labels=("approved", "ready"), active=False,
            done_when="```sh\ntrue\n```\n"):
    header = "<!-- foragent:v1 -->\n" + ("exclusive:\n" + "".join(f"- {path}\n" for path in exclusive)
                                         if exclusive else "")
    body = (header + "## Goal\nChange.\n## Files\n" + "".join(f"- {path}\n" for path in files)
            + "## Done when\n" + done_when)
    store.save_approval(number, body=body, body_sha=body_hash(body), acceptance_blobs={},
                        approved_by="human", approved_at=(NOW - timedelta(hours=1)).isoformat())
    names = [*labels, *([f"lane:{lane}"] if lane != "main" else [])]
    store.update_local_issue(number, title=f"Issue {number}", body=body, labels=names, state="open", lane=lane)
    if active:
        store.start_run(number, branch=f"foragent/issue-{number}", worktree=store.worktree_path(number), lane=lane)


def supply(store, root, **config):
    config = Config(repo="owner/repo", lanes={lane: {"paths": [f"{lane}/**"]} for lane in LANES}, **config)
    facts = orch_facts.collect(store, config, root)
    return facts["supply"], facts["resources"]["parallelizable_work"]["held"]


def feed(store):
    """One approved, ready issue per free lane (osprey runs #9), each on its own file."""
    approve(store, 9, "osprey", ["src/osprey.py"], labels=("approved", "running"), active=True)
    for number, lane in ((1, "kestrel"), (2, "heron"), (3, "main")):
        approve(store, number, lane, [f"src/{lane}.py"])


def test_free_lanes_exclude_busy_ones_and_count_main(env):
    store, root = env
    feed(store)
    current, held = supply(store, root)
    assert current == {"free_lanes": ["heron", "kestrel", "main"], "startable": 3, "short": False}
    assert held == {}
    approve(store, 4, "osprey", ["src/osprey-two.py"])
    current, held = supply(store, root)
    assert held == {} and current["startable"] == 3, "A start in busy osprey feeds no free lane"
    store.update_local_issue(3, labels=["approved"])
    assert supply(store, root)[0] == {"free_lanes": ["heron", "kestrel", "main"], "startable": 2, "short": True}


def block_by_open_issue(store):
    store.update_local_issue(20, title="Blocker", labels=[], state="open")
    store.update_local_issue(2, blocked_by=[{"number": 20, "state": "open", "repository": None}])


def pause_lane(store):
    orch_plan.pause(store, "heron", NOW + timedelta(hours=2), "plan says wait")


def reserve_file(store):
    approve(store, 8, "osprey", ["src/heron.py"], exclusive=["src/heron.py"], labels=("approved", "running"),
            active=True)


def dream(store):
    store.start_dream("heron", worktree=store.root / "dream-heron")


def plan_width(store):
    """The plan lets heron run one issue; a second heron issue and no main work."""
    def narrow(plan, now):
        plan["width"]["heron"] = 1
        return []

    orch_plan._edit(store, "human", narrow)
    store.update_local_issue(3, labels=["approved"])
    approve(store, 4, "heron", ["src/heron-two.py"])


def outbox(store):
    store.update_local_issue(20, title="Other issue", labels=[], state="open")
    store.queue_github_write(20, "add_labels", labels=["approved"])
    assert store.github_blocking_outbox(), "A queued workflow write the start loop waits on"


def project_stop(store):
    (store.root / "stop-requests.jsonl").write_text(json.dumps(
        {"at": NOW.isoformat(), "kind": "stop", "why": "Person stopped the project", "held": []}) + "\n")


def merged_before(store):
    store.start_run(2, branch="foragent/issue-2", worktree=store.worktree_path(2), lane="heron")
    store.finish_run(2, outcome="merged")


def no_machine_check(store):
    approve(store, 2, "heron", ["src/heron.py"], done_when="- [ ] Looks right\n")


def remember(store, number, name, value):
    """A GitHub read the tick remembered on the issue row this tick (``_confirmed_read``)."""
    store.record_read(number, name, value, baseline=store.read_baseline())


def label(action, actor, at):
    return {"label": "approved", "action": action, "actor": actor, "created_at": at.isoformat()}


APPROVED_AT = NOW - timedelta(hours=1)


def outsider_reapproval(store):
    """The person revoked #2's approval; someone who is not an approver added `approved` again."""
    remember(store, 2, "label_events", [label("labeled", "human", APPROVED_AT),
                                       label("unlabeled", "human", NOW - timedelta(minutes=30)),
                                       label("labeled", "outsider", NOW - timedelta(minutes=10))])


def open_pr(store):
    remember(store, 2, "open_pr", 77)


ALL = {"1": None, "2": None, "3": None}
# Each kind of hold the tick's admission makes: how to make it, the config, what admission
# records as held (issue -> reason), and how many issues it still starts in free lanes.
HOLDS = {
    "blocked_by": (block_by_open_issue, {}, {"2": "blocked_by"}, 2),
    "plan_pause": (pause_lane, {}, {"2": "plan_pause"}, 2),
    "exclusive_file": (reserve_file, {}, {"2": "structure"}, 2),
    # The project allows three runs and #9 holds one: the issue ranked last waits for a slot.
    "cap": (lambda store: None, {"max_parallel": 3}, {"3": "cap"}, 2),
    "dream": (dream, {}, {"2": "dream"}, 2),
    # A start this tick fills heron's width of one, so the second heron issue waits.
    "plan_width": (plan_width, {}, {"4": "plan_width"}, 2),
    "outbox": (outbox, {}, dict.fromkeys(ALL, "outbox"), 0),
    "project_stop": (project_stop, {}, dict.fromkeys(ALL, "project_stop"), 0),
    "run_hours": (lambda store: None, {"run_hours": "03-04"}, dict.fromkeys(ALL, "run_hours"), 0),
    "github_wait": (lambda store: store.pause_github(NOW + timedelta(hours=1)), {},
                    dict.fromkeys(ALL, "github"), 0),
    "merged_before": (merged_before, {}, {"2": "merged"}, 2),
    "no_machine_check": (no_machine_check, {}, {"2": "no_machine_check"}, 2),
    "approval_changed": (outsider_reapproval, {"approvers": ["human"]}, {"2": "approval_changed"}, 2),
    "open_pr": (open_pr, {}, {"2": "open_pr"}, 2),
}


@pytest.mark.parametrize("kind", list(HOLDS))
def test_every_admission_hold_leaves_its_lane_without_supply(env, kind):
    """Rounds 1-4 each found one more place where a separate count or replay differed from the
    tick's admission; startable is admission's own decision, so every hold it makes takes the issue
    out of supply, and the replay sees its own earlier starts (plan width)."""
    store, root = env
    feed(store)
    make, config, expected, startable = HOLDS[kind]
    make(store)
    current, held = supply(store, root, **config)
    assert held == expected, f"Admission holds {expected}: {held}"
    assert current["startable"] == startable and current["short"] is True, current


def test_outsider_reapproval_after_revoking_five_issues_leaves_four_free_lanes_without_supply(env):
    """Round 5: approvers are ["human"]; five ready issues lose their approval and an outsider adds it
    back. The tick starts none of them, so supply is zero across four free lanes."""
    store, root = env
    for number, lane in ((1, "kestrel"), (2, "heron"), (3, "main"), (4, "osprey"), (5, "kestrel")):
        approve(store, number, lane, [f"src/{number}.py"])
        remember(store, number, "label_events", [label("labeled", "human", APPROVED_AT),
                                                 label("unlabeled", "human", NOW - timedelta(minutes=30)),
                                                 label("labeled", "outsider", NOW - timedelta(minutes=10))])
    current, held = supply(store, root, approvers=["human"])
    assert held == {str(n): "approval_changed" for n in range(1, 6)}
    assert current == {"free_lanes": ["heron", "kestrel", "main", "osprey"], "startable": 0, "short": True}
    for number in range(1, 6):  # The person approves again: the latest approver event matches the record.
        remember(store, number, "label_events", [label("labeled", "human", APPROVED_AT)])
    assert supply(store, root, approvers=["human"])[0]["short"] is False


def test_memory_at_the_configured_warn_limit_holds_every_start(env, monkeypatch):
    """The start loop holds at `memory_pressure_limit: warn` too (scheduler_start._memory_holds)."""
    from foragent import settings

    store, root = env
    feed(store)
    monkeypatch.setattr(capacity, "machine_load", lambda: ("warn", 0.1))
    assert supply(store, root)[0]["short"] is False, "Warn pressure under the default critical limit holds nothing"
    monkeypatch.setattr(settings, "read_machine", lambda: {"memory_pressure_limit": "warn"})
    current, held = supply(store, root)
    assert held == dict.fromkeys(ALL, "machine") and current["startable"] == 0 and current["short"] is True


def test_work_the_file_rule_holds_does_not_cover_a_free_lane(env):
    """A live run reserves src/shared.py exclusively; ready issues that need it cannot start."""
    store, root = env
    approve(store, 9, "osprey", ["src/shared.py"], exclusive=["src/shared.py"], labels=("approved", "running"),
            active=True)
    for number, lane in ((1, "kestrel"), (2, "heron"), (3, "main"), (4, "kestrel"), (5, "heron")):
        approve(store, number, lane, ["src/shared.py"])
    current, _ = supply(store, root)
    assert current["startable"] == 0 and current["short"] is True, "Five approved ready issues are no supply"
    for number, lane in ((6, "kestrel"), (7, "heron"), (8, "main")):
        approve(store, number, lane, [f"src/{lane}.py"])
    current, _ = supply(store, root)
    assert current["startable"] == 3 and current["short"] is False, "Work admission starts covers the lanes"


def test_supply_low_needs_the_hold_and_the_hold_resets_on_recovery(tmp_path):
    clock = [BASE_TIME]
    store = store_at(tmp_path, clock)
    short = {"free_lanes": ["heron", "kestrel"], "startable": 0, "short": True}
    orch_facts_stall.hold(store, short, clock[0])
    start = clock[0]
    clock[0] += timedelta(minutes=10)
    orch_facts_stall.hold(store, short, clock[0])
    assert orch_facts_stall._since(store) == start, "A later short tick keeps the first short time"
    almost = start + orch_facts_stall.SUPPLY_HOLD - timedelta(seconds=1)
    assert orch_facts_stall.supply_low_numbers(store, almost, short) == {}
    at = start + orch_facts_stall.SUPPLY_HOLD
    assert orch_facts_stall.supply_low_numbers(store, at, short) == {
        "free_lanes": ["heron", "kestrel"], "free_slots": 2, "startable": 0, "unapproved_ready": 0}
    assert orch_facts_stall.supply_low_numbers(store, at, {**short, "short": False}) == {}, (
        "Supply that recovered is no stall even before the tick removes the hold")
    orch_facts_stall.hold(store, {**short, "short": False}, clock[0])
    assert orch_facts_stall._since(store) is None, "Recovered supply ends the hold"
    clock[0] += timedelta(minutes=5)
    orch_facts_stall.hold(store, short, clock[0])
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
