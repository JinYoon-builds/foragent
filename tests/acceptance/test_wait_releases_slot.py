"""Locked acceptance for wait-releases-slot; do not edit during implementation.

Reject counting idle live runners as machine load, releasing active work,
resuming work before reacquiring a full budget, and keeping separate occupancy
rules in admission, facts and historical scorecards. Only external Git, tracker,
executor, clock and sensor fakes are used; production admission stays real.
"""

from dataclasses import replace
from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import capacity, orch_queue, runner, scheduler
from foragent.issue import body_hash
from foragent.state import StateStore
from test_capacity_aimd import machine_repositories, occupy_machine_slots
from test_local_truth import local_cli, record_gh
from test_orch_review_handoff import make_case
from test_scorecard_flow import (  # noqa: F401
    NOW, _approved, _budget, _card, _metric, _phased, flow,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


WAITS = ("merge-turn-wait", "merge-lock-wait", "ci", "overlap-wait")
ACTIVE = ("implementing", "checking", "reviewing", "resolving-conflict")


@pytest.mark.parametrize("wait,active", list(zip(WAITS, ACTIVE)))
@pytest.mark.parametrize("location", ["own", "sibling"])
def test_tick_starts_highest_priority_work_after_waiters_release_full_budget(
    machine_repositories, monkeypatch, wait, active, location,
):
    """Reject live-pid counting, ignoring active phases, repo-local fixes, or new-start overshoot."""
    first, second = machine_repositories
    for case in (first, second):
        case.ctx.config = replace(case.ctx.config, max_parallel=None)
        case.tick_ctx.config = case.ctx.config
    monkeypatch.setattr(scheduler, "next_parallel", lambda current, **inputs: 2)
    holder = second if location == "own" else first
    occupy_machine_slots(holder, (1, 2), pid=os.getpid())
    for n in (1, 2):
        holder.store.update_run(n, phase=active)
    orch_queue.set_global(second.store, [8, 7, 6, 5, 4, 3, 2, 1])
    assert scheduler.tick(second.tick_ctx).skipped is None
    assert second.launched == [], f"{active} still consumes both machine slots"
    assert capacity.machine_runs(second.store, second.ctx.now()) == 2

    for n in (1, 2):
        holder.store.update_run(n, phase=wait)
    assert scheduler.tick(second.tick_ctx).skipped is None
    assert second.launched == [8, 7], (
        "The next tick must admit ready work in global priority order and fill only two slots",
        wait, location, second.launched,
    )
    assert len(holder.store.active_runs()) >= 2, "Waiting owners stay alive"
    assert capacity.machine_runs(second.store, second.ctx.now()) == 2


@pytest.mark.parametrize("wait", WAITS)
def test_facts_and_capacity_agree_on_own_and_sibling_waiters(
    machine_repositories, monkeypatch, capsys, wait,
):
    """Reject fixing gate admission alone, hiding waiters from run facts, or changing test slots."""
    from foragent import orch_facts_resources

    first, second = machine_repositories
    monkeypatch.setattr(scheduler, "next_parallel", lambda current, **inputs: 4)
    for case in (first, second):
        case.ctx.config = replace(case.ctx.config, max_parallel=None)
        case.tick_ctx.config = case.ctx.config
        occupy_machine_slots(case, (1, 2), pid=os.getpid())
        for n in (1, 2):
            case.store.update_run(n, phase=wait)
    second.store.update_run(1, phase="checking")
    assert scheduler.tick(replace(second.tick_ctx, start_runs=False)).skipped is None
    capacity.write_state(second.store, 4, second.ctx.now())
    # Facts' frozen observation reads the existing offline process boundary.
    monkeypatch.setattr(orch_facts_resources, "read_process_identity", lambda pid: SimpleNamespace(pid=pid))
    main = local_cli(second, monkeypatch)
    calls = record_gh(monkeypatch)
    capsys.readouterr()
    assert main(["orch", "facts", "--json", "--repo-root", str(second.root)]) == 0
    facts = json.loads(capsys.readouterr().out)
    work = facts["resources"]["parallelizable_work"]
    assert (work["machine_runs"], capacity.machine_runs(second.store, second.ctx.now())) == (1, 1), (
        "Only the checking run occupies the shared machine; both views must agree", work,
    )
    assert work["free_run_slots"] == 3
    assert {1, 2} <= {row["issue"] for row in facts["runs"]}, "Run visibility is separate from occupancy"
    assert facts["test_slots"]["current"] == capacity.effective_test_slots(second.config.test_slots)
    assert calls == []


@pytest.mark.parametrize("wait", WAITS)
def test_scorecard_replays_wait_intervals_as_unused_machine_capacity(flow, capsys, wait):
    """Reject changing current facts while still charging historical waits, or freeing a test slot."""
    start = NOW - timedelta(hours=1)
    _approved(flow, 10, start)
    _budget(flow, start, parallel=2, slots=1)
    # The one test slot stays occupied, so only released machine capacity can
    # explain idle minutes. The other runner works for 40m and waits for 20m.
    _phased(flow, 80, start, [("checking", 60)], finish=False)
    _phased(flow, 20, start, [("implementing", 10), (wait, 20), ("reviewing", 30)])
    card = _card(flow, capsys)
    assert _metric(card, "idle_minutes") == pytest.approx(20), (
        "Historical machine occupancy excludes just the 20 waiting minutes; the test slot remains full"
    )


@pytest.mark.parametrize("wait", WAITS)
def test_live_workload_still_counts_when_its_parent_run_is_waiting(tmp_path, monkeypatch, wait):
    """Reject subtracting all work by a waiting PID, or retaining an empty parent slot after work exits."""
    from datetime import UTC, datetime
    from foragent.config import Config

    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    now = datetime.now(UTC)
    store = StateStore(tmp_path / "project", now=lambda: now)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=os.getpid())
    store.update_run(1, phase=wait)
    with capacity.hold_workload(store, Config(), kind="review", issue=1, wait_s=0):
        assert capacity.machine_runs(store, now) == 1, "Real agent work consumes one machine slot"
    assert capacity.machine_holders(store, now) == [], "Only the idle live parent remains"


@pytest.mark.parametrize("source,work", [
    ("ci", "refresh"), ("turn", "refresh"), ("overlap", "refresh"),
    ("ci", "conflict"), ("ci", "retry"),
])
def test_returning_waiter_queues_before_work_and_counts_after_slot_opens(
    git_history, tmp_path, monkeypatch, offline_acceptance, source, work,
):
    """Reject phase-only filtering, checks-only reacquisition, conflict/retry bypasses, and leaked slots."""
    from foragent import run_merge_turn

    git_history.update({"src/work.py": "VALUE = 'base'\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    git_command(git_history.clone, "merge", "-q", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     test_command="true", merge_group_max=1, max_retries=1, conflict_retries=1)
    case.store.update_run(1, pid=os.getpid())
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    (case.store.root.parent / "machine.yml").write_text("max_parallel: 1\ntest_slots: 2\n")
    capacity.write_state(case.store, 1, case.ctx.now())
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    other = StateStore(case.store.root.parent / "other", now=case.ctx.now)
    armed, released, queued, effects = [], [], [], []
    initial_sleep = case.ctx.sleep

    def assert_admitted(effect):
        if not armed:
            return
        assert released, f"Resumed {effect} while another repository filled the machine budget"
        assert capacity.machine_runs(case.store, case.ctx.now()) == 1, "Returning work counts again exactly once"
        assert case.store.get_run(1).phase not in WAITS, "Active work cannot retain an idle phase"
        effects.append(effect)

    transport = case.git._run

    def git_io(args):
        command = args[2:] if args[:1] == ["-C"] else args
        while command[:1] == ["-c"]:
            command = command[2:]
        if command[:1] == ["merge"]:
            assert_admitted("base refresh")
        return transport(args)

    monkeypatch.setattr(case.git, "_run", git_io)
    process = runner.run_logged

    def checks(*args, **kwargs):
        assert_admitted("verification")
        return process(*args, **kwargs)

    monkeypatch.setattr(runner, "run_logged", checks)

    def implement(kwargs):
        assert_admitted("implementation" if work == "retry" else "conflict resolution")
        case.implement(kwargs)
        (kwargs["worktree"] / "src/work.py").write_text("VALUE = 'ours'\n")

    case.implementer.steps = [implement, implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]

    # A same-repository wait owner blocks the real queue or the late file
    # claim. It is released by external progress at the first fake sleep.
    if source in {"turn", "overlap"}:
        body = case.tracker.get_issue(1).body
        if source == "overlap":
            body = body.replace("## Out of scope", "## Files\n- feature.txt\n## Out of scope")
            case.tracker.update_issue(1, title=case.tracker.get_issue(1).title, body=body)
            saved = case.store.load_approval(1)
            case.store.save_approval(1, body=body, body_sha=body_hash(body), acceptance_blobs=saved.acceptance_blobs,
                                     approved_by="human", approved_at=case.ctx.now().isoformat())
            # make_case already prepared a start for its original body. Start
            # this fixture's run under the approval whose Files it must claim.
            case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1), pid=os.getpid())
            body = body.replace("- feature.txt", "- src/work.py")
            case.ctx.config = replace(case.ctx.config, overlap_per_file=1)
        case.tracker.add_issue(90, "Wait owner", body, labels=["approved", "running"])
        case.store.save_approval(90, body=body, body_sha=body_hash(body), acceptance_blobs={},
                                approved_by="human", approved_at=case.ctx.now().isoformat())
        case.store.start_run(90, branch="foragent/issue-90", worktree=case.store.worktree_path(90), pid=os.getpid())
        case.store.update_run(90, phase="merge-lock-wait")
        if source == "turn":
            run_merge_turn.join(case.ctx, run_merge_turn.seat(case.ctx, 90, "main"), git_history.tip)

    def sleep(seconds):
        initial_sleep(seconds)
        if not armed:
            expected = {"ci": "ci", "turn": "merge-lock-wait", "overlap": "overlap-wait"}[source]
            assert case.store.get_run(1).phase == expected, "Scenario must enter its real wait"
            if source != "ci":
                case.store.finish_run(90, outcome="merged")
            if work != "retry":
                path = "src/work.py" if work == "conflict" else "src/from_main.py"
                git_history.update({path: "VALUE = 'main'\n"})
            other.start_run(101, branch="foragent/issue-101", worktree=other.worktree_path(101), pid=os.getpid())
            other.update_run(101, phase="implementing")
            armed.append(True)
        elif not released:
            assert effects == [], "No resumed work may run before admission"
            assert capacity.machine_runs(case.store, case.ctx.now()) == 1, "Queued returner must not overshoot"
            queued.append(True)
            other.finish_run(101, outcome="merged")
            released.append(True)
        else:
            pytest.fail("A returning runner should make progress after the slot opens")

    case.ctx.sleep = sleep
    # Replace this adapter's clock, not Python's shared time module: subprocess
    # polling must retain its real clock and cannot drive our scenario.
    monkeypatch.setattr(capacity, "time", SimpleNamespace(
        sleep=sleep, monotonic=lambda: case.clock[0].timestamp(), time=lambda: case.clock[0].timestamp(),
    ))

    def ci(pr):
        if source == "ci" and not armed:
            return "pending"
        if work == "retry" and len(case.implementer.calls) == 1:
            return "fail"
        return "pass"

    monkeypatch.setattr(case.tracker, "pr_checks", ci)
    outcome = runner.run_issue(1, case.ctx)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert armed and queued and released and effects, "Exercise wait, full-budget queue and resumed work"
    if work in {"conflict", "retry"}:
        assert len(case.implementer.calls) == 2, "Actually execute the new worker turn"
    assert capacity.machine_runs(case.store, case.ctx.now()) == 0, "Finishing releases the reacquired slot"
