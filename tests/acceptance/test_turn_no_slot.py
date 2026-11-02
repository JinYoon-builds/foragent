"""Locked acceptance: a ready merge must not wait behind machine work.

Drive run_issue with real queue, admission, verification and cleanup. Only Git
transport, executors, remote facts, time and sensors are faked. No private
production helpers or proposed interfaces are part of this contract.
"""

import os
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent import capacity, run_merge_turn, runner
from foragent.git import GitRepo
from foragent.state import StateStore
from test_orch_review_handoff import make_case
from test_state_split import offline_acceptance  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


@pytest.fixture
def merge_case(git_history, tmp_path, monkeypatch, offline_acceptance):
    git_history.update({"src/work.py": "VALUE = 'base'\n"})
    git_command(git_history.clone, "fetch", "origin")
    git_command(git_history.clone, "merge", "--ff-only", "origin/main")
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[],
                     test_command="true", merge_group_max=1)
    case.store.update_run(1, pid=os.getpid())
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    (case.store.root.parent / "machine.yml").write_text("max_parallel: 2\ntest_slots: 2\n")
    capacity.write_state(case.store, 2, case.ctx.now())
    monkeypatch.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    monkeypatch.setattr(capacity, "kernel_pressure", lambda: "normal")
    case.other = StateStore(case.store.root.parent / "other", now=case.ctx.now)
    case.armed = False
    case.merges = []
    case.local_work = []
    case.slot_waits = []
    case.released = False
    merge = case.tracker.merge_pr
    cached_head = case.tracker.pr_head_sha

    def published_head(pr):
        # The offline service reflects lease pushes too, like GitHub does.
        candidate = next((item for item in case.tracker.created_prs if item.number == pr), None)
        if candidate is not None:
            sha = git_command(case.history.origin, "rev-parse", f"refs/heads/{candidate.head}")
            case.tracker.set_pr_head_sha(pr, sha)
        return cached_head(pr)

    monkeypatch.setattr(case.tracker, "pr_head_sha", published_head)

    def merge_once(pr, *, expected_head_sha, **kwargs):
        assert case.armed, "Exercise the full machine budget before merging"
        assert case.tracker.pr_checks(pr) == "pass"
        assert expected_head_sha == case.tracker.pr_head_sha(pr)
        case.merges.append(pr)
        return merge(pr, expected_head_sha=expected_head_sha, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", merge_once)
    return case


def fill_budget(case):
    for number in range(101, 105):
        case.other.start_run(number, branch=f"work-{number}",
                             worktree=case.other.worktree_path(number), pid=os.getpid())
        case.other.update_run(number, phase="implementing")
    case.armed = True
    assert capacity.workload_budget(case.store) == 2
    assert capacity.machine_runs(case.store, case.ctx.now()) == 4


def fake_admission_clock(case, monkeypatch, sleep):
    monkeypatch.setattr(capacity, "time", SimpleNamespace(
        sleep=sleep, monotonic=lambda: case.clock[0].timestamp(),
        time=lambda: case.clock[0].timestamp(),
    ))


@pytest.mark.parametrize("entry", ["implementation", "review-only"])
@pytest.mark.parametrize("preceding_turn", [False, True], ids=["green-ci", "returned-turn"])
def test_turn_holder_merges_once_while_machine_budget_stays_full(merge_case, monkeypatch, preceding_turn, entry):
    case = merge_case
    if entry == "review-only":
        case.ctx.config = replace(case.ctx.config, review="careful")
        assert runner.run_issue(1, case.ctx) == "pr-open"
        case.ctx.config = replace(case.ctx.config, review="fast")
        case.reviewer.steps = ["VERDICT: PASS"]
    if preceding_turn:
        # This is the supported, persisted queue input, not a patched decision.
        case.store.start_run(90, branch="earlier", worktree=case.store.worktree_path(90), pid=os.getpid())
        case.store.update_run(90, phase="ci")
        run_merge_turn.join(case.ctx, run_merge_turn.seat(case.ctx, 90, "main"), case.history.tip)

    def sleep(seconds):
        if preceding_turn and case.armed and case.store.get_run(90).status == "running":
            assert case.tracker.merged_prs == [], "A run without the turn must wait even at full budget"
            assert case.store.get_run(1).phase in {"merge-lock-wait", "merge-turn-wait"}
            assert capacity.machine_runs(case.store, case.ctx.now()) == 4
            case.store.finish_run(90, outcome="merged")
            return
        assert not case.armed, "A ready turn holder queued for machine capacity instead of merging"
        assert case.store.get_run(1).phase in {"ci", "merge-lock-wait", "merge-turn-wait"}
        fill_budget(case)

    process, merge_base = runner.run_logged, GitRepo.merge_base

    def no_new_checks(*args, **kwargs):
        assert not case.armed, "Merge-only completion must not repeat local checks"
        return process(*args, **kwargs)

    def no_new_refresh(*args, **kwargs):
        assert not case.armed, "The unchanged base needs no local refresh"
        return merge_base(*args, **kwargs)

    monkeypatch.setattr(runner, "run_logged", no_new_checks)
    monkeypatch.setattr(GitRepo, "merge_base", no_new_refresh)
    case.ctx.sleep = sleep
    fake_admission_clock(case, monkeypatch, sleep)
    monkeypatch.setattr(case.tracker, "pr_checks", lambda pr: "pass" if preceding_turn or case.armed else "pending")
    merge = case.tracker.merge_pr

    def full_merge(*args, **kwargs):
        assert all(case.other.get_run(n).status == "running" for n in range(101, 105))
        return merge(*args, **kwargs)

    monkeypatch.setattr(case.tracker, "merge_pr", full_merge)
    outcome = runner.run_issue(1, case.ctx) if entry == "implementation" else runner.review_only(1, case.ctx)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert case.tracker.merged_prs == case.merges and len(case.merges) == 1
    assert len(case.implementer.calls) == 1
    assert len(case.reviewer.calls) == (1 if entry == "implementation" else 2)
    assert case.store.get_run(1).status == "finished"
    assert capacity.machine_runs(case.store, case.ctx.now()) == 4, "Merge completion must release its slot"
    for number in range(101, 105):
        case.other.finish_run(number, outcome="merged")
    assert capacity.machine_runs(case.store, case.ctx.now()) == 0, "No capacity may leak"



@pytest.mark.parametrize("work,entry,timing", [
    ("refresh", "implementation", "before-lock"),
    ("conflict", "implementation", "before-lock"),
    ("refresh", "review-only", "before-lock"),
    ("refresh", "implementation", "final-snapshot"),
    ("refresh", "review-only", "final-snapshot"),
])
def test_turn_holder_local_work_proceeds_without_capacity_wait(merge_case, monkeypatch, work, entry, timing):
    case = merge_case
    if entry == "review-only":
        case.ctx.config = replace(case.ctx.config, review="careful")
        assert runner.run_issue(1, case.ctx) == "pr-open"
        case.ctx.config = replace(case.ctx.config, review="fast")
    initial_sleep = case.ctx.sleep
    moved = []
    process, merge_base = runner.run_logged, GitRepo.merge_base

    def admitted(effect):
        if case.armed:
            assert run_merge_turn.holds_turn(case.store, run_merge_turn.seat(case.ctx, 1, "main"))
            assert capacity.machine_runs(case.store, case.ctx.now()) == 5, "Count local work above the full budget exactly once"
            case.local_work.append(effect)

    def refresh(git, *args, **kwargs):
        admitted("base refresh")
        return merge_base(git, *args, **kwargs)

    def checks(*args, **kwargs):
        admitted("checks")
        return process(*args, **kwargs)

    def implement(kwargs):
        admitted("conflict resolution")
        case.implement(kwargs)
        (kwargs["worktree"] / "src/work.py").write_text("VALUE = 'ours'\n")

    case.implementer.steps = [implement, implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    monkeypatch.setattr(GitRepo, "merge_base", refresh)
    monkeypatch.setattr(runner, "run_logged", checks)

    def sleep(seconds):
        assert not case.armed, "A turn holder must never queue for machine capacity, including real local work"
        initial_sleep(seconds)
        assert case.store.get_run(1).phase == "ci"
        path = "src/new_base.py" if work == "refresh" else "src/work.py"
        if timing == "before-lock":
            case.history.update({path: "VALUE = 'new base'\n"})
            moved.append(True)
        fill_budget(case)

    def ci(pr):
        if timing == "final-snapshot" and case.armed and not moved and case.store.get_run(1).phase == "merging":
            case.history.update({"src/new_base.py": "VALUE = 'new base'\n"})
            moved.append(True)
        return "pass" if case.armed else "pending"

    case.ctx.sleep = sleep
    fake_admission_clock(case, monkeypatch, sleep)
    monkeypatch.setattr(case.tracker, "pr_checks", ci)
    outcome = runner.run_issue(1, case.ctx) if entry == "implementation" else runner.review_only(1, case.ctx)
    assert outcome == "merged", case.store.log_path(1).read_text()
    assert moved and {"base refresh", "checks"} <= set(case.local_work)
    if work != "refresh":
        assert "conflict resolution" in case.local_work and len(case.implementer.calls) == 2
    else:
        assert len(case.implementer.calls) == 1
        assert len(case.reviewer.calls) == (1 if entry == "implementation" else 2)
    assert case.tracker.merged_prs == case.merges and len(case.merges) == 1
    assert capacity.machine_runs(case.store, case.ctx.now()) == 4
    for number in range(101, 105):
        case.other.finish_run(number, outcome="merged")
    assert capacity.machine_runs(case.store, case.ctx.now()) == 0


@pytest.mark.parametrize("opens,minutes", [(True, 1), (False, 1), (False, 20)], ids=["slot-opens", "remaining-time-exit", "ten-minute-exit"])
def test_nonholder_waits_truthfully_and_can_exit_without_a_free_slot(merge_case, monkeypatch, opens, minutes):
    case = merge_case
    case.ctx.config = replace(case.ctx.config, max_runtime_minutes=minutes)
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    initial_sleep = case.ctx.sleep
    began = []
    saved = []

    def sleep(seconds):
        initial_sleep(seconds)
        if not case.armed:
            assert case.store.get_run(1).phase == "ci"
            fill_budget(case)
            began.append(case.clock[0])
            saved.append(case.tracker.pr_head_sha(case.tracker.find_pr("foragent/issue-1")))
            return
        assert not run_merge_turn.holds_turn(case.store, run_merge_turn.seat(case.ctx, 1, "main"))
        phase = case.store.get_run(1).phase
        assert "wait" in phase and "slot" in phase, f"Capacity waiting is displayed as {phase!r}"
        assert case.merges == [] and len(case.implementer.calls) == 1
        assert case.clock[0] - began[0] <= timedelta(seconds=min(600, minutes * 60)), "Re-admission has no bounded way out"
        case.slot_waits.append(phase)
        if opens:
            for number in (102, 103, 104):
                case.other.finish_run(number, outcome="merged")
            case.released = True

    case.ctx.sleep = sleep
    fake_admission_clock(case, monkeypatch, sleep)

    def ci(pr):
        if not case.armed:
            return "pending"
        return "fail" if len(case.implementer.calls) == 1 else "pass"

    monkeypatch.setattr(case.tracker, "pr_checks", ci)
    outcome = runner.run_issue(1, case.ctx)
    assert case.slot_waits
    if opens:
        assert outcome == "merged", case.store.log_path(1).read_text()
        assert len(case.implementer.calls) == 2 and len(case.merges) == 1
        assert capacity.machine_runs(case.store, case.ctx.now()) == 1
    else:
        assert outcome != "merged" and case.store.get_run(1).status == "finished"
        assert case.merges == [] and len(case.implementer.calls) == 1
        assert case.store.get_run(1).waiting_until is None
        assert case.store.get_run(1).pause_regions == {}
        assert case.tracker.pr_head_sha(case.tracker.find_pr("foragent/issue-1")) == saved[0]
        assert "ready" in case.tracker.get_issue(1).labels
        assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "running"}
        assert capacity.machine_runs(case.store, case.ctx.now()) == 4
    for number in range(101, 105):
        case.other.finish_run(number, outcome="merged")
    assert capacity.machine_runs(case.store, case.ctx.now()) == 0


def test_ci_failure_after_getting_the_turn_cannot_block_its_unwind(merge_case, monkeypatch):
    case = merge_case
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    case.store.start_run(90, branch="earlier", worktree=case.store.worktree_path(90), pid=os.getpid())
    case.store.update_run(90, phase="ci")
    run_merge_turn.join(case.ctx, run_merge_turn.seat(case.ctx, 90, "main"), case.history.tip)

    def sleep(seconds):
        assert not case.armed, "A turn holder queued for capacity before it could unwind a CI failure"
        assert case.store.get_run(1).phase in {"merge-lock-wait", "merge-turn-wait"}
        case.store.finish_run(90, outcome="merged")
        fill_budget(case)

    def ci(pr):
        return "fail" if case.armed and len(case.implementer.calls) == 1 else "pass"

    case.ctx.sleep = sleep
    fake_admission_clock(case, monkeypatch, sleep)
    monkeypatch.setattr(case.tracker, "pr_checks", ci)
    assert runner.run_issue(1, case.ctx) == "merged", case.store.log_path(1).read_text()
    assert len(case.implementer.calls) == len(case.reviewer.calls) == 2
    assert case.tracker.merged_prs == case.merges and len(case.merges) == 1
    assert capacity.machine_runs(case.store, case.ctx.now()) == 4
    for number in range(101, 105):
        case.other.finish_run(number, outcome="merged")
    assert capacity.machine_runs(case.store, case.ctx.now()) == 0
