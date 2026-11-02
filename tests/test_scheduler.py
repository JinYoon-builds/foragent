from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
import yaml


from foragent.capacity import write_state
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.issue import body_hash
from foragent.labels import issue_lane
from foragent.reasons import Reason, parse_approval_comment, parse_reason
from foragent.processes import kill_process_group
from foragent.scheduler import TickContext, flag, runner_process_exists, tick
from foragent.state import StateStore
from foragent.tracker import Dependency, FakeTracker, LabelEvent, TrackerError

from conftest import ACCEPTANCE, git_command
from test_runner import import_index


NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BODY = f"""<!-- foragent:v1 -->
## Goal
Implement the feature.
## Out of scope
Other features.
## Done when
```sh
pytest {ACCEPTANCE}
```
"""


@pytest.fixture
def ctx(git_history, tmp_path, monkeypatch, fake_process_table):
    from foragent import processes

    # Model launcher birth evidence, then let production ownership checks run.
    start_run, update_run = StateStore.start_run, StateStore.update_run

    def birth(pid):
        if pid is not None and pid not in fake_process_table.live:
            fake_process_table.add(pid, cwd=git_history.clone, start_time=NOW.timestamp())

    def start(store, number, **fields):
        birth(fields.get("pid"))
        return start_run(store, number, **fields)

    def update(store, number, **fields):
        if fields.get("pid") is not None and store.get_run(number).pid is None:
            birth(fields["pid"])
            fields["process_identities"] = processes.observe_tree(roots=(fields["pid"],))
        return update_run(store, number, **fields)

    monkeypatch.setattr(StateStore, "start_run", start)
    monkeypatch.setattr(StateStore, "update_run", update)
    def git_run(args):
        root, command = git_history.clone, args
        while command[:1] == ["-C"]:
            root, command = Path(command[1]), command[2:]
        if command[:1] == ["-c"] and "commit" in command:
            import_index(root, command[-1])
            return ""
        if command[:1] == ["push"]:
            branch = command[-1]
            git_command(git_history.origin, "fetch", str(root), f"+refs/heads/{branch}:refs/heads/{branch}")
            return ""
        return GitRepo(git_history.clone)._run_git(args)

    context = TickContext(
        repo="owner/repo", repo_root=git_history.clone,
        tracker=FakeTracker(strict=True), store=StateStore(tmp_path / "state"),
        config=Config(approvers=["human"]), git=GitRepo(git_history.clone, git_run), now=lambda: NOW,
        launcher=lambda number: 7000 + number, start_runs=False,
        runner_process_exists=lambda number: False,
        kill_runner=kill_process_group,
    )
    context.tracker._now = lambda: context.now()
    context.store._now = lambda: context.now()
    context.process_table = fake_process_table
    # Scheduler behavior must not depend on this machine's live memory and load.
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: ("normal", 0.0))
    return context


def record_stop(ctx, callback):
    def stop(pid):
        callback(pid)
        ctx.process_table.exit(pid)
    return stop


def write_machine_yml(**values):
    from foragent import machine_budget

    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding="utf-8")


def approve(ctx, number=1, *, body=BODY, actor="human", labels=()):
    ctx.tracker.add_issue(number, f"Issue {number}", body, labels=labels)
    ctx.tracker.record_label(number, "approved", actor)


@pytest.mark.parametrize("pressure", ["warn", "critical"])
def test_tick_memory_pressure_leaves_shared_budgets_alone(ctx, monkeypatch, pressure):
    from foragent import capacity

    # #497: memory never moves a budget; floors in machine.yml are ignored.
    write_machine_yml(test_slots=2, test_slots_floor=2, parallel_floor=3)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample(pressure, 0.5, 60))
    capacity.write_state(ctx.store, 4, NOW)
    assert tick(ctx).skipped is None
    assert capacity.effective_test_slots(2) == 2
    assert capacity.read_state(ctx.store)["parallel"] == 4
    assert not (capacity.machine_root() / capacity.HISTORY_FILE).exists()
    assert not (ctx.store.root.parent / capacity.HISTORY_FILE).exists()


@pytest.mark.parametrize("pressure,started", [("warn", 3), ("critical", 0)])
def test_tick_critical_memory_holds_new_starts_and_records_one_row(ctx, monkeypatch, pressure, started):
    from foragent import capacity

    lanes = {f"lane-{number}": {} for number in range(1, 5)}
    ctx.config = replace(ctx.config, max_parallel=3, test_slots=1, lanes=lanes)
    for number in range(1, 5):
        approve(ctx, number, labels=[f"lane:lane-{number}"])
    capacity.write_state(ctx.store, 3, NOW)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample(pressure, 0.5, 60))
    prebuilds = []
    ctx.baseline_launcher = lambda *args, **kwargs: prebuilds.append(args)
    ctx.start_runs = True
    assert tick(ctx).skipped is None
    assert len(ctx.store.active_runs()) == started
    assert capacity.read_state(ctx.store)["parallel"] == 3
    path = ctx.store.root.parent / capacity.HISTORY_FILE
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    if pressure == "critical":
        assert rows == [{"at": NOW.isoformat(), "rule": "memory-critical", "pressure": "critical",
                         "load_per_cpu": 0.5, "swapins": 60}]
        assert prebuilds == []
    else:
        assert rows == []


def test_tick_critical_memory_records_nothing_when_files_overlap_already_hold_the_work(ctx, monkeypatch):
    from foragent import capacity, scheduler

    # Work that a running issue's files already hold back was not stopped by memory.
    lanes = {f"lane-{number}": {} for number in range(1, 3)}
    ctx.config = replace(ctx.config, max_parallel=3, test_slots=1, lanes=lanes)
    for number in range(1, 3):
        approve(ctx, number, labels=[f"lane:lane-{number}"])
    capacity.write_state(ctx.store, 3, NOW)
    lanes_checked = []

    def overlapping(context, lane, issue):
        lanes_checked.append(lane)
        return False

    monkeypatch.setattr(scheduler, "_can_start_in_lane", overlapping)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample("critical", 0.5, 0))
    ctx.start_runs = True
    assert tick(ctx).skipped is None
    assert sorted(lanes_checked) == ["lane-1", "lane-2"]
    assert ctx.store.active_runs() == []
    assert not (ctx.store.root.parent / capacity.HISTORY_FILE).exists()


def test_tick_critical_memory_without_startable_work_records_nothing(ctx, monkeypatch):
    from foragent import capacity

    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample("critical", 0.5, 0))
    ctx.start_runs = True
    assert tick(ctx).skipped is None
    assert not (ctx.store.root.parent / capacity.HISTORY_FILE).exists()


@pytest.mark.parametrize("needs_human", [False, True])
def test_refresh_populates_blocker_evidence_and_listing_stays_offline(ctx, monkeypatch, needs_human):
    from foragent import orch

    remote = ctx.tracker._tracker
    remote.add_issue(1, "Question", labels=["blocked", *(["needs-human"] if needs_human else [])])
    question = "<!-- foragent:blocked reason=spec-question -->\nWhich locale should we use?"
    remote.comment(1, question)
    assert ctx.store.local_issues() == []
    reads = []
    list_comments = remote.list_comments

    def read(number):
        reads.append(number)
        return list_comments(number)

    monkeypatch.setattr(remote, "list_comments", read)
    tick(ctx)
    assert reads == [1]
    with monkeypatch.context() as patch:
        patch.setattr(remote, "list_comments", lambda n: pytest.fail("orch list must stay offline"))
        item, = orch.list_blocked(ctx)
    assert item["reason"] == "spec-question" and item["details"] == question
    ctx.now = lambda: NOW + timedelta(minutes=4, seconds=59)
    tick(ctx)
    assert reads == [1]
    revised = "<!-- foragent:blocked reason=spec-question -->\nShould we support both locales?"
    remote.comment(1, revised)
    ctx.now = lambda: NOW + timedelta(minutes=5)
    tick(ctx)
    assert reads == [1, 1]
    assert orch.list_blocked(ctx)[0]["details"] == revised


@pytest.mark.parametrize("during", ["list_prs", "list_comments"])
def test_refresh_preserves_local_writes_during_remote_reads(ctx, monkeypatch, during):
    from types import SimpleNamespace
    from foragent import orch
    from foragent.tracker import RecordingTracker

    remote = ctx.tracker._tracker
    for number in (1, 2):
        remote.add_issue(number, f"Question {number}", labels=["blocked", "needs-human"])
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=5)
    remote.update_issue(2, title="Human revision before refresh", body="Human body")
    writer = RecordingTracker(remote, SimpleNamespace(
        store=StateStore(ctx.store.root, now=ctx.now), config=ctx.config, now=ctx.now,
    ))
    read = getattr(remote, during)
    written = {}

    def interleaved(*args, **kwargs):
        result = read(*args, **kwargs)
        if not written:
            writer.remove_labels(1, ["blocked", "needs-human"])
            writer.update_issue(1, title="Local revision during refresh", body="Local body")
            writer.comment(1, "Local evidence during refresh")
            writer.record_board_value(1, "project", "Status", "Todo")
            number = writer.create_issue("Created during refresh", "New body", [])
            assert number == 3
            written.update({row["number"]: row for row in writer._context.store.local_issues()})
        return result

    with monkeypatch.context() as patch:
        patch.setattr(remote, during, interleaved)
        tick(ctx)
    rows = {row["number"]: row for row in StateStore(ctx.store.root).local_issues()}
    assert rows[1] == written[1]
    assert rows[3] == written[3]
    assert rows[2]["title"] == "Human revision before refresh"
    assert remote.get_issue(1).labels == frozenset()
    watched, = orch.watch(ctx)
    assert watched["blocked"] == watched["needs_human"] == [2]

    remote.update_issue(1, title="Human revision after refresh", body="Latest human body")
    remote.add_labels(1, ["needs-human"])
    ctx.now = lambda: NOW + timedelta(minutes=10)
    tick(ctx)
    row = ctx.store.local_issues()[0]
    assert (row["title"], row["body"], row["labels"]) == (
        "Human revision after refresh", "Latest human body", ["needs-human"],
    )


@pytest.mark.parametrize("step", [
    "heartbeat", "reap", "usage", "fetch", "refresh", "recovery", "budgets",
])
def test_tick_opens_orchestrator_when_an_earlier_step_raises(ctx, monkeypatch, step):
    from foragent import orch_spawn, scheduler, usage_recheck
    from foragent.git import GitError

    spawned = []
    monkeypatch.setattr(orch_spawn, "ensure_orchestrator",
                        lambda root, store, config, *, now: spawned.append(now) or "tmux")
    # A failed GitHub refresh (TrackerError) is recorded and the tick goes on; anything else propagates.
    error = {"fetch": GitError("fetch failed")}.get(step, OSError(f"{step} failed"))

    def boom(*args, **kwargs):
        raise error

    target = {
        "heartbeat": (ctx.store, "write_heartbeat"), "reap": (scheduler, "_reap_inactive_runs"),
        "usage": (usage_recheck, "recheck"), "fetch": (scheduler, "fetch_base"),
        "refresh": (ctx.tracker, "list_issues"), "recovery": (scheduler, "_recover_runs"),
        "budgets": (scheduler, "_apply_budgets"),
    }[step]
    monkeypatch.setattr(*target, boom)
    with pytest.raises(type(error)) as raised:
        tick(ctx)
    assert raised.value is error  # The original error propagates unchanged.
    assert len(spawned) == 1


def test_busy_tick_leaves_the_orchestrator_decision_to_the_lock_holder(ctx, monkeypatch):
    from contextlib import contextmanager

    from foragent import orch_spawn

    spawned = []
    monkeypatch.setattr(orch_spawn, "ensure_orchestrator",
                        lambda root, store, config, *, now: spawned.append(now) or "orca")

    @contextmanager
    def held():
        yield False

    monkeypatch.setattr(ctx.store, "tick_lock", held)
    report = tick(ctx)
    assert report.skipped and spawned == [] and report.actions == []


def test_tick_opens_orchestrator_on_both_github_wait_returns(ctx, monkeypatch):
    from foragent import orch_spawn

    spawned = []
    monkeypatch.setattr(orch_spawn, "ensure_orchestrator",
                        lambda root, store, config, *, now: spawned.append(now) or "tmux")
    until = NOW + timedelta(minutes=10)

    def unavailable():
        raise TrackerError("API rate limit exceeded (HTTP 403)",
                           stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    monkeypatch.setattr(ctx.tracker, "list_issues", unavailable)
    first = tick(ctx)  # The wait starts inside the tick.
    assert "GitHub rate limit" in first.skipped and len(spawned) == 1
    assert "Opened an orchestrator (tmux)" in first.actions
    second = tick(ctx)  # The recorded wait returns before any GitHub call.
    assert "GitHub rate limit" in second.skipped and len(spawned) == 2
    assert "Opened an orchestrator (tmux)" in second.actions


@pytest.mark.parametrize("message,limited", [
    ("API rate limit exceeded (HTTP 403)", True),
    ("HTTP 422 Validation Failed", False),
])
def test_tick_rate_limit_persists_wait_but_other_tracker_errors_raise(ctx, monkeypatch, message, limited):
    calls = []
    until = NOW + timedelta(minutes=10)

    def unavailable():
        calls.append("issues")
        raise TrackerError(message, stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    monkeypatch.setattr(ctx.tracker, "list_issues", unavailable)
    if not limited:
        # Another refresh error is recorded and the tick goes on from stored issues (#995); no wait starts.
        report = tick(ctx)
        assert report.skipped is None
        assert any(message in line for line in report.actions)
        assert ctx.store.issues_observation()["error"] == message
        assert ctx.store.github_wait_until(NOW) is None
        return
    assert "GitHub rate limit" in tick(ctx).skipped
    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    ctx.now = lambda: until - timedelta(seconds=1)
    monkeypatch.setattr(ctx.git, "fetch", lambda *args: pytest.fail("waiting tick must stay offline"))
    assert "GitHub rate limit" in tick(ctx).skipped
    assert calls == ["issues"]
    assert ctx.store.active_runs() == []


def test_github_wait_repairs_running_label_without_crash_or_stall_cost(ctx):
    approve(ctx, labels=["running"])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
    ctx.store.update_run(1, attempts=3, restarts=1, waiting_until=NOW.isoformat())
    ctx.store.finish_run(1, "limit-wait")
    ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Already published", body="Closes #1")
    ctx.start_runs = True
    report = tick(ctx)
    assert any("started running" in action for action in report.actions)
    run = ctx.store.get_run(1)
    assert (run.attempts, run.restarts, run.status) == (3, 1, "running")
    assert ctx.store.load_flag(1) is None


@pytest.mark.parametrize("human_change", [False, True])
def test_outbox_reset_refreshes_before_delivery_inside_the_snapshot_window(ctx, monkeypatch, human_change):
    remote = ctx.tracker._tracker
    remote.add_issue(1, "Feature", labels=["blocked"])
    tick(ctx)
    until = NOW + timedelta(minutes=1)
    close = remote.close_issue
    attempts = []

    def limited(number, comment=None):
        attempts.append((number, comment))
        if ctx.now() < until:
            raise TrackerError("API rate limit exceeded (HTTP 403)",
                               stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")
        return close(number, comment)

    monkeypatch.setattr(remote, "close_issue", limited)
    ctx.tracker.close_issue(1, "Merged")
    assert ctx.store.local_issues()[0]["state"] == "closed"
    ctx.now = lambda: until - timedelta(seconds=1)
    if human_change:
        remote.issues[1] = replace(remote.get_issue(1), title="Human revision", labels=frozenset(),
                                   updated_at=ctx.now().isoformat())
    assert "GitHub rate limit" in tick(ctx).skipped
    assert attempts == [(1, "Merged")]
    ctx.now = lambda: until
    tick(ctx)
    assert ctx.store.github_outbox() == []
    row, = ctx.store.local_issues()
    if human_change:
        assert attempts == [(1, "Merged")]
        assert row["state"] == "open" and row["title"] == "Human revision"
        assert row["comments"] == []
        events = [json.loads(line) for line in (ctx.store.root / "orch-events.jsonl").read_text().splitlines()]
        assert [(event["kind"], event["issue"]) for event in events] == [("outbox-dropped", 1)]
    else:
        assert attempts == [(1, "Merged"), (1, "Merged")]
        assert row["state"] == remote.get_issue(1).state == "closed"


def test_deleted_issue_outbox_does_not_block_tick_progress(ctx, monkeypatch):
    from foragent import orch

    remote = ctx.tracker._tracker
    for number in (1, 2):
        remote.add_issue(number, f"Question {number}", labels=["blocked"])
    tick(ctx)
    until = NOW + timedelta(minutes=1)
    ctx.store.pause_github(until)
    ctx.tracker.comment(1, "Deleted issue evidence")
    ctx.tracker.comment(2, "Surviving evidence")
    approve(ctx, 3)
    del remote.issues[1]
    comment = remote.comment

    def write(number, body):
        if number not in remote.issues:
            raise TrackerError("gh: Not Found (HTTP 404)")
        return comment(number, body)

    monkeypatch.setattr(remote, "comment", write)
    ctx.now = lambda: until
    report = tick(ctx)

    assert report.skipped is None
    assert ctx.store.github_outbox() == []
    assert [item.body for item in remote.list_comments(2)] == ["Surviving evidence"]
    assert "ready" in remote.get_issue(3).labels
    assert all(row["number"] != 1 for row in ctx.store.local_issues())
    assert any(event["kind"] == "outbox-dropped" and event["issue"] == 1
               and "issue not found" in event["summary"] for event in orch.log(ctx))


def test_github_limit_during_launch_labels_defers_without_creating_a_crash(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
    ctx.store.update_run(1, attempts=3, restarts=1)
    ctx.store.finish_run(1, "limit-wait")
    ctx.start_runs = True
    ctx.config = replace(ctx.config, board="project")
    launches = []
    ctx.launcher = lambda n: launches.append(n) or 7001
    until = NOW + timedelta(minutes=10)

    def status(issue, value):
        if value == "Running":
            raise TrackerError("API rate limit exceeded (HTTP 403)",
                               stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")

    monkeypatch.setattr("foragent.board.set_status", status)
    assert "GitHub rate limit" in tick(ctx).skipped
    assert launches == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    run = ctx.store.get_run(1)
    assert (run.outcome, run.attempts, run.restarts) == ("limit-wait", 3, 1)
    ctx.now = lambda: until
    monkeypatch.setattr("foragent.board.set_status", lambda *args: None)
    tick(ctx)
    assert launches == [1]
    assert (ctx.store.get_run(1).attempts, ctx.store.get_run(1).restarts) == (3, 1)
    assert ctx.store.load_flag(1) is None


@pytest.mark.parametrize("failure,reason", [("crashed", Reason.RETRIES_EXCEEDED), ("stalled", Reason.STALLED)])
def test_interrupted_runner_with_open_pr_stays_visible(ctx, monkeypatch, failure, reason):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["ready"])
    ctx.tracker.add_labels(1, ["running"])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1), pid=7001)
    ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Existing PR", body="Closes #1")
    if failure == "crashed":
        ctx.process_table.exit(7001)
    else:
        ctx.now = lambda: NOW + timedelta(minutes=ctx.config.stall_minutes + 1)
    tick(ctx)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.load_flag(1).reason == reason
    assert ctx.store.get_run(1).status == "finished"
    events = recovery_events(ctx)
    assert len(events) == 1
    assert events[0]["kind"] == reason.value
    assert "blocked" in events[0]["summary"]


def edit_body(ctx, number, body):
    ctx.tracker.edit_body(number, body)


def test_human_ready_removal_is_not_promoted_again_for_the_same_approval(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    remote = ctx.tracker._tracker
    remote.remove_labels(1, ["ready"])
    ctx.now = lambda: NOW + timedelta(minutes=5)
    with monkeypatch.context() as patch:
        patch.setattr(remote, "add_labels", lambda *args: pytest.fail("replayed the earlier promotion"))
        tick(ctx)
        ctx.now = lambda: NOW + timedelta(minutes=10)
        tick(ctx)
    assert ctx.store.local_issues()[0]["labels"] == ["approved"]
    ctx.now = lambda: NOW + timedelta(minutes=11)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    assert remote.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("actor,allowed", [("human", True), ("HuMaN", True), ("stranger", False)])
def test_approval_actor_and_snapshot(ctx, actor, allowed):
    approve(ctx, actor=actor)
    report = tick(ctx)
    assert report.skipped is None
    issue = ctx.tracker.get_issue(1)
    if allowed:
        assert issue.labels == {"approved", "ready"}
        approval = ctx.store.load_approval(1)
        assert approval.body == BODY and approval.body_sha == body_hash(BODY)
        assert approval.approved_by == actor and approval.approved_at == NOW.isoformat()
        assert approval.acceptance_blobs == ctx.git.blob_shas("origin/main", [ACCEPTANCE])
        assert parse_approval_comment(ctx.tracker.comments[1][0].body)["body_sha"] == body_hash(BODY)
        assert "#1 promoted to ready" in report.actions
        assert tick(ctx).actions == []
        assert len(ctx.tracker.comments[1]) == 1
    else:
        assert issue.labels == {"approved"}
        assert ctx.store.load_approval(1) is None
        assert ctx.tracker.list_comments(1) == []
        assert len(report.actions) == 1 and "non-approver" in report.actions[0]
        assert len(tick(ctx).actions) == 1


def test_latest_valid_approval_ignores_other_actors(ctx):
    approve(ctx)
    ctx.tracker._now = lambda: NOW + timedelta(seconds=1)
    ctx.tracker.record_label(1, "approved", "stranger")
    report = tick(ctx)
    assert ctx.store.load_approval(1).approved_at == NOW.isoformat()
    assert len([line for line in report.actions if "non-approver" in line]) == 1


def test_scheduler_projects_follow_ready_running_and_blocked_labels(ctx):
    from foragent import board
    from test_board import item_values

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    approve(ctx)
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🌰 Up next"
    ctx.start_runs = True
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🐿 In progress"
    flag(ctx, 1, Reason.SPEC_QUESTION, "Choose a behavior")
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🐿 In progress"
    ctx.store.finish_run(1, outcome="blocked")
    ctx.now = lambda: NOW + timedelta(minutes=31)
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "✋ Your turn"


def _board_writes(ctx, monkeypatch):
    """Count board value writes at the GraphQL boundary below any recorder."""
    from foragent.tracker import RecordingTracker

    remote = ctx.tracker._tracker if isinstance(ctx.tracker, RecordingTracker) else ctx.tracker
    graphql = remote.graphql
    writes = []

    def counted(query, variables):
        if "ForagentSetBoardValue" in query:
            writes.append(variables)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    return writes


@pytest.mark.parametrize("change", ["unapproved", "closed"])
def test_scheduler_moves_unapproved_and_closed_cards_once(ctx, monkeypatch, change):
    from foragent import board
    from test_board import item_values

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    approve(ctx)
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🌰 Up next"
    if change == "unapproved":
        ctx.tracker.remove_labels(1, ["approved", "ready"])
    else:
        ctx.tracker.close_issue(1)
    writes = _board_writes(ctx, monkeypatch)
    tick(ctx)
    wanted = "🗂 Backlog" if change == "unapproved" else "📦 Done"
    assert item_values(ctx.tracker, project_id, 1)["Status"] == wanted
    assert len(writes) == 1
    writes.clear()
    tick(ctx)
    assert writes == [], "a card already in its column needs no board write"


def test_scheduler_never_adds_cards_for_unknown_closed_or_unapproved_issues(ctx, monkeypatch):
    from foragent import board
    from foragent.tracker import RecordingTracker
    from test_board import snapshot

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    ctx.tracker.add_issue(7, "Never on the board")
    ctx.tracker.add_issue(8, "Closed long ago")
    ctx.tracker.close_issue(8)
    remote = ctx.tracker._tracker if isinstance(ctx.tracker, RecordingTracker) else ctx.tracker
    graphql = remote.graphql
    touched = []

    def counted(query, variables):
        if variables.get("number") in {7, 8} or "ForagentAddBoardItem" in query:
            touched.append(query)
        return graphql(query, variables)

    monkeypatch.setattr(remote, "graphql", counted)
    tick(ctx)
    assert not any("ForagentAddBoardItem" in query or "ForagentSetBoardValue" in query for query in touched)
    touched.clear()
    tick(ctx)
    assert touched == [], "an issue already checked for its column needs no board call"
    numbers = {item["content"]["number"] for item in snapshot(ctx.tracker, project_id)["items"]["nodes"]}
    assert not numbers & {7, 8}


@pytest.mark.parametrize("change", ["unapproved", "closed"])
def test_scheduler_moves_existing_cards_without_a_local_record(ctx, monkeypatch, change):
    from foragent import board
    from test_board import item_values

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    approve(ctx)
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🌰 Up next"
    ctx.store.update_local_issue(1, board_values={})  # Adopted card: no local record.
    if change == "unapproved":
        ctx.tracker.remove_labels(1, ["approved", "ready"])
    else:
        ctx.tracker.close_issue(1)
    writes = _board_writes(ctx, monkeypatch)
    tick(ctx)
    wanted = "🗂 Backlog" if change == "unapproved" else "📦 Done"
    assert item_values(ctx.tracker, project_id, 1)["Status"] == wanted
    assert len(writes) == 1
    writes.clear()
    tick(ctx)
    assert writes == []


def test_scheduler_moves_a_card_people_add_after_the_board_had_none(ctx, monkeypatch):
    from foragent import board
    from test_board import item_values

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    ctx.tracker.add_issue(8, "Closed without a card")
    ctx.tracker.close_issue(8)
    tick(ctx)
    assert ctx.tracker.board_value(8, project_id, "Status") == board.NO_CARD
    # Someone adds the closed issue's card in 🐿 In progress on GitHub.
    status = board.Project(ctx.tracker, ctx.repo, project_id).fields()["Status"]
    option = next(option for option in status["options"] if option["name"] == "🐿 In progress")
    item = ctx.tracker.graphql("""mutation ForagentAddBoardItem($input: AddProjectV2ItemByIdInput!) {
      addProjectV2ItemById(input: $input) { item { id } }
    }""", {"input": {"projectId": project_id, "contentId": "I_owner/repo#8"}})["data"]["addProjectV2ItemById"]["item"]
    ctx.tracker.graphql("""mutation ForagentSetBoardValue($input: UpdateProjectV2ItemFieldValueInput!) {
      updateProjectV2ItemFieldValue(input: $input) { projectV2Item { id } }
    }""", {"input": {"projectId": project_id, "itemId": item["id"], "fieldId": status["id"],
                     "value": {"singleSelectOptionId": option["id"]}}})
    writes = _board_writes(ctx, monkeypatch)
    ctx.now = lambda: NOW + timedelta(minutes=6)  # An unchanged listing reads no board (tick-github-budget).
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 8)["Status"] == "🐿 In progress"
    assert writes == []
    ctx.tracker.update_issue(8, title="Closed without a card", body="Edited later")
    ctx.now = lambda: NOW + timedelta(minutes=12)  # A refresh whose listing changed reads the board.
    tick(ctx)
    assert item_values(ctx.tracker, project_id, 8)["Status"] == "📦 Done"
    assert len(writes) == 1
    writes.clear()
    tick(ctx)
    assert writes == []


def test_scheduler_repairs_board_after_transport_recovers(ctx, caplog):
    from foragent import board
    from test_board import item_values

    project_id = board.ensure(ctx.tracker, ctx.repo)
    ctx.config = replace(ctx.config, board=project_id)
    approve(ctx)
    graphql = ctx.tracker.graphql

    def unavailable(*args):
        raise OSError("offline project")

    ctx.tracker.graphql = unavailable
    assert tick(ctx).skipped is None and "ready" in ctx.tracker.get_issue(1).labels
    assert "Could not set board Status" in caplog.text
    ctx.tracker.graphql = graphql
    assert tick(ctx).skipped is None
    assert item_values(ctx.tracker, project_id, 1)["Status"] == "🌰 Up next"


def test_latest_approver_event_is_the_snapshot_identity(ctx):
    approve(ctx)
    later = NOW + timedelta(minutes=1)
    ctx.tracker._now = lambda: later
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    assert ctx.store.load_approval(1).approved_at == later.isoformat()


def test_removed_approval_and_unauthorized_readdition_do_not_start(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.tracker.record_label(1, "approved", "stranger")
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == []
    assert "running" not in ctx.tracker.get_issue(1).labels


def test_removed_then_reapproved_takes_new_snapshot(ctx):
    approve(ctx)
    tick(ctx)
    original = ctx.store.load_approval(1)
    ctx.tracker.remove_labels(1, ["approved"])
    edit_body(ctx, 1, BODY.replace("Implement", "Improve"))
    assert tick(ctx).actions == []
    ctx.tracker._now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    approval = ctx.store.load_approval(1)
    assert approval.approved_at != original.approved_at
    assert approval.body != original.body
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert len(ctx.tracker.list_comments(1)) == 2


@pytest.mark.parametrize("state", ["waiting", "ready", "running"])
def test_body_integrity_for_every_approved_state(ctx, state):
    approve(ctx)
    tick(ctx)
    if state == "waiting":
        ctx.tracker.remove_labels(1, ["ready"])
    elif state == "running":
        ctx.tracker.remove_labels(1, ["ready"])
        ctx.tracker.add_labels(1, ["running"])
        ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1), pid=123)
    edit_body(ctx, 1, BODY.replace("Other features.", "Changed scope."))
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert ctx.store.load_approval(1) is None
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.HASH_MISMATCH
    assert "body changed" in comment and "Re-approve" in comment
    if state == "running":
        assert "#1 runner killed" in report.actions
        assert ctx.store.active_runs() == []
        assert ctx.store.get_run(1).outcome == "killed"
    assert tick(ctx).actions == []


@pytest.mark.parametrize("change", ["body", "acceptance"])
def test_running_integrity_failure_kills_and_finishes_before_flagging(ctx, git_history, monkeypatch, change):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    if change == "body":
        edit_body(ctx, 1, BODY + "Changed scope\n")
    else:
        git_history.update({ACCEPTANCE: "weakened test\n"})
    actions = []
    ctx.kill_runner = record_stop(ctx, lambda pid: actions.append(("kill", pid)))
    add_labels = ctx.tracker.add_labels

    def add(number, labels):
        if "blocked" in labels:
            assert actions == [("kill", 4321)]
            assert ctx.store.get_run(number).outcome == "killed"
        return add_labels(number, labels)

    monkeypatch.setattr(ctx.tracker, "add_labels", add)
    tick(ctx)
    assert actions == [("kill", 4321)]
    assert ctx.store.get_run(1).outcome == "killed"
    assert ctx.store.load_flag(1).reason == Reason.HASH_MISMATCH
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"blocked"}


@pytest.mark.parametrize("seconds,killed", [(0, False), (1, True)])
def test_running_overtime_backstop_kills_alive_runner(ctx, monkeypatch, seconds, killed):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=5432)
    actions = []
    ctx.kill_runner = record_stop(ctx, actions.append)
    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.max_runtime_minutes + 10, seconds=seconds)
    ctx.store.update_run(1, last_progress_at=ctx.now().isoformat())
    tick(ctx)
    assert actions == ([5432] if killed else [])
    if killed:
        assert ctx.store.get_run(1).outcome == "killed"
        assert ctx.store.load_flag(1).reason == Reason.TIMEOUT
        assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    else:
        assert ctx.store.get_run(1).status == "running"


def test_runtime_backstop_outbox_survives_uncollected_old_comment_after_reset(ctx, monkeypatch):
    approve(ctx)
    remote = ctx.tracker._tracker
    old_comment = remote.comment(1, "Earlier discussion")
    tick(ctx)
    mark_running(ctx, pid=5432)
    assert all(comment["id"] != old_comment for comment in ctx.store.local_issues()[0]["comments"])

    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.max_runtime_minutes + 11)
    ctx.store.update_run(1, last_progress_at=ctx.now().isoformat())
    until = ctx.now() + timedelta(minutes=1)
    add_labels = remote.add_labels
    attempts, killed, launches = [], [], []
    ctx.kill_runner = record_stop(ctx, killed.append)
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    ctx.start_runs = True

    def limited(number, labels):
        labels = list(labels)
        attempts.append((number, labels))
        if ctx.now() < until:
            raise TrackerError("API rate limit exceeded (HTTP 403)",
                               stdout=f"X-RateLimit-Reset: {int(until.timestamp())}\r\n")
        return add_labels(number, labels)

    monkeypatch.setattr(remote, "add_labels", limited)
    tick(ctx)
    assert killed == [5432]
    assert ctx.store.get_run(1).outcome == "killed"
    assert ctx.store.load_flag(1).reason == Reason.TIMEOUT
    assert [entry["action"] for entry in ctx.store.github_outbox()] == [
        "add_labels", "remove_labels", "comment",
    ]
    assert remote.get_issue(1).labels == {"approved", "running"}

    ctx.now = lambda: until
    report = tick(ctx)

    assert attempts == [(1, ["blocked"]), (1, ["blocked"])]
    assert ctx.store.github_outbox() == []
    assert remote.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.local_issues()[0]["labels"] == ["approved", "blocked"]
    assert parse_reason(remote.list_comments(1)[-1].body) == Reason.TIMEOUT
    assert ctx.store.get_run(1).outcome == "killed"
    assert ctx.store.load_flag(1).reason == Reason.TIMEOUT
    assert [run.outcome for run in ctx.store.read_history()] == ["killed"]
    assert launches == []
    assert report.actions == []


@pytest.mark.parametrize("trigger", ["integrity", "timeout"])
@pytest.mark.parametrize("force", [False, True])
def test_tick_removes_registered_worktree_after_runner_termination(ctx, monkeypatch, fake_process_table, trigger, force):
    fake_process_table.add(4321, cwd=ctx.repo_root, start_time=10)
    if force:
        fake_process_table.ignore_term.add(4321)
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    (path / "unfinished.txt").write_text("unfinished")
    ctx.kill_runner = kill_process_group
    finish = ctx.store.finish_run

    def finish_after_cleanup(*args, **kwargs):
        assert 4321 not in fake_process_table.live and not path.exists()
        assert str(path) not in git_command(ctx.repo_root, "worktree", "list")
        return finish(*args, **kwargs)

    monkeypatch.setattr(ctx.store, "finish_run", finish_after_cleanup)
    if trigger == "integrity":
        edit_body(ctx, 1, BODY + "Changed scope\n")
    else:
        ctx.now = lambda: NOW + timedelta(minutes=ctx.config.max_runtime_minutes + 11)
    tick(ctx)
    assert any(sig == signal.SIGKILL for _, _, sig in fake_process_table.signals) == force
    assert ctx.store.get_run(1).outcome == "killed"


@pytest.mark.parametrize("force", [False, True])
def test_tick_cleans_worktree_when_real_runner_is_terminated_between_logged_commands(ctx, tmp_path, monkeypatch, fake_process_table, force):
    from process_fakes import install_popen
    from foragent import processes

    approve(ctx)
    tick(ctx)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    # Complete one logged command and verify that its SIGTERM handler is restored.
    previous = signal.getsignal(signal.SIGTERM)
    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["offline-executor"])
    processes.run_logged(["offline-executor"], cwd=path, timeout_s=5, log_path=path / "session.log")
    assert signal.getsignal(signal.SIGTERM) == previous
    root = fake_process_table.add(42010, cwd=path, start_time=20)
    if force:
        fake_process_table.ignore_term.add(root.pid)
    mark_running(ctx, pid=root.pid)
    edit_body(ctx, 1, BODY + "Changed scope\n")
    ctx.kill_runner = kill_process_group
    tick(ctx)
    assert root.pid not in fake_process_table.live
    assert any(sig == signal.SIGKILL for _, _, sig in fake_process_table.signals) is force
    assert ctx.store.get_run(1).outcome == "killed"
    assert not path.exists()
    assert str(path) not in git_command(ctx.repo_root, "worktree", "list")


def test_failed_termination_keeps_worktree_and_active_record(ctx):
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    edit_body(ctx, 1, BODY + "Changed scope\n")

    def failed_kill(pid):
        raise TimeoutError("Runner still alive")

    ctx.kill_runner = failed_kill
    try:
        tick(ctx)
        assert path.exists() and ctx.store.get_run(1).status == "running"
        assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED
        assert "still alive" in ctx.store.get_run(1).detail
    finally:
        ctx.git.remove_worktree(path)


@pytest.mark.parametrize("mode", ["batch", "careful"])
@pytest.mark.parametrize("ready", [False, True])
def test_open_review_pr_is_not_rescheduled_or_allowed_to_starve_next_issue(ctx, mode, ready):
    ctx.config = replace(ctx.config, review=mode)
    approve(ctx)
    approve(ctx, 2)
    tick(ctx)
    if not ready:
        ctx.tracker.remove_labels(1, ["ready"])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
    ctx.store.finish_run(1, "pr-open")
    ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Review", body="Closes #1")
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    ctx.start_runs = True
    tick(ctx)
    tick(ctx)
    assert launches == [2]
    assert ctx.tracker.get_issue(1).labels == {"approved"}
    assert ctx.store.get_run(1).outcome == "pr-open"


def test_pr_opened_between_promotion_and_start_prevents_launch(ctx, monkeypatch):
    approve(ctx)
    ctx.start_runs = True
    find = ctx.tracker.find_pr
    probes = []

    def find_pr(head):
        probes.append(head)
        if len(probes) == 2:
            ctx.tracker.create_pr(head=head, base="main", title="Review", body="Closes #1")
        return find(head)

    monkeypatch.setattr(ctx.tracker, "find_pr", find_pr)
    tick(ctx)
    assert len(probes) == 2
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"approved"}


def test_closed_review_pr_allows_issue_to_run_again(ctx):
    approve(ctx)
    pr = ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Review", body="Closes #1")
    tick(ctx)
    assert "ready" not in ctx.tracker.get_issue(1).labels
    ctx.tracker.close_issue(pr)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.get_run(1).status == "running"


@pytest.mark.parametrize("stale_label", [None, "ready", "running"])
@pytest.mark.parametrize("start_runs", [False, True])
def test_merged_issue_waiting_for_github_closure_is_not_rescheduled(ctx, stale_label, start_runs):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["ready"])
    if stale_label is not None:
        ctx.tracker.add_labels(1, [stale_label])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
    ctx.store.finish_run(1, "merged")
    # Keep the issue open to reproduce GitHub's delayed Closes processing, and
    # reload state so the guard must use persisted execution history.
    ctx.store = StateStore(ctx.store.root, now=lambda: ctx.now())
    ctx.now = lambda: NOW + timedelta(seconds=1)
    approve(ctx, 2)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    ctx.start_runs = start_runs
    for _ in range(2):
        report = tick(ctx)
        assert "#1 promoted to ready" not in report.actions
    assert ctx.tracker.get_issue(1).state == "open"
    assert ctx.tracker.get_issue(1).labels == {"approved"}
    assert ctx.store.get_run(1).outcome == "merged"
    assert [run.outcome for run in ctx.store.read_history() if run.number == 1] == ["merged"]
    assert launches == ([2] if start_runs else [])


@pytest.mark.parametrize("approval_offset", [-1, 0, 1])
@pytest.mark.parametrize("actor", ["human", "stranger"])
def test_merged_issue_requires_valid_approval_strictly_after_run_start(ctx, approval_offset, actor):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["ready"])
    started_at = NOW + timedelta(minutes=1)
    ctx.now = lambda: started_at
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1))
    ctx.store.finish_run(1, "merged")
    ctx.now = lambda: started_at + timedelta(seconds=approval_offset)
    ctx.tracker.record_label(1, "approved", actor)
    ctx.now = lambda: NOW + timedelta(minutes=2)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 7001
    ctx.start_runs = True
    report = tick(ctx)
    allowed = actor == "human" and approval_offset > 0
    assert launches == ([1] if allowed else [])
    assert ("#1 promoted to ready" in report.actions) == allowed
    assert ctx.tracker.get_issue(1).labels == ({"approved", "running"} if allowed else {"approved"})


def test_merged_guard_uses_last_run_for_same_issue(ctx):
    approve(ctx)
    tick(ctx)
    for seconds, number, outcome in [(0, 1, "merged"), (1, 1, "crashed"), (2, 99, "merged")]:
        ctx.now = lambda seconds=seconds: NOW + timedelta(seconds=seconds)
        ctx.store.start_run(number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number))
        ctx.store.finish_run(number, outcome)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.get_run(1).status == "running"
    assert ctx.store.get_run(1).pid == 7001


def test_merge_history_is_rechecked_immediately_before_launch(ctx, monkeypatch):
    approve(ctx)

    def merged_while_checking_dependencies(number):
        ctx.store.start_run(number, branch="foragent/issue-1", worktree=ctx.store.worktree_path(number))
        ctx.store.finish_run(number, "merged")
        return []

    monkeypatch.setattr(ctx.tracker, "blocked_by", merged_while_checking_dependencies)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 7001
    ctx.start_runs = True
    tick(ctx)
    assert launches == []
    assert ctx.store.get_run(1).outcome == "merged"
    assert ctx.tracker.get_issue(1).labels == {"approved"}


def test_dead_overtime_runner_uses_crash_recovery(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(hours=3)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    killed = []
    ctx.kill_runner = record_stop(ctx, killed.append)
    tick(ctx)
    assert killed == [123]
    assert ctx.store.get_run(1).outcome == "crashed"
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("content", ["def test_feature():\n    pass\n", None])
def test_acceptance_integrity_tracks_origin_changes_and_deletions(ctx, git_history, content):
    approve(ctx)
    tick(ctx)
    git_history.update({ACCEPTANCE: content})
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert ctx.store.load_approval(1) is None
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.HASH_MISMATCH
    assert ACCEPTANCE in comment and "Acceptance file changed" in comment


def test_integrity_details_include_body_and_all_changed_files(ctx, git_history):
    second = "tests/acceptance/second.py"
    git_history.update({second: "assert True\n"})
    approve(ctx, body=BODY.replace(f"pytest {ACCEPTANCE}", f"pytest {ACCEPTANCE} {second}"))
    tick(ctx)
    edit_body(ctx, 1, ctx.tracker.get_issue(1).body + "Edited\n")
    git_history.update({ACCEPTANCE: "pass\n", second: "pass\n"})
    tick(ctx)
    comment = ctx.tracker.list_comments(1)[-1].body
    assert "Issue body changed" in comment
    assert ACCEPTANCE in comment and second in comment


def test_missing_acceptance_file_is_snapshotted_and_detected_when_added(ctx, git_history):
    path = "tests/acceptance/not_yet.py"
    approve(ctx, body=BODY.replace(ACCEPTANCE, path))
    tick(ctx)
    assert ctx.store.load_approval(1).acceptance_blobs == {path: "missing"}
    git_history.update({path: "assert True\n"})
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}


def test_no_machine_check_flags_once_and_human_removal_resumes(ctx):
    approve(ctx, body="## Done when\n- [ ] Looks good\n")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.NO_MACHINE_CHECK
    count = len(ctx.tracker.list_comments(1))
    assert tick(ctx).actions == []
    assert len(ctx.tracker.list_comments(1)) == count
    ctx.tracker.remove_labels(1, ["blocked", "approved"])
    edit_body(ctx, 1, BODY)
    ctx.tracker._now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("label", ["blocked", "needs-human"])
def test_flagged_issue_is_skipped_entirely(ctx, monkeypatch, label):
    approve(ctx, labels=[label, "ready"])

    def unexpected(*args):
        pytest.fail("flagged issues must not be inspected")

    monkeypatch.setattr(ctx.tracker, "label_events", unexpected)
    monkeypatch.setattr(ctx.tracker, "blocked_by", unexpected)
    assert tick(ctx).actions == []
    assert ctx.store.load_approval(1) is None


def test_blocked_by_waits_until_all_blockers_close(ctx):
    ctx.tracker.add_issue(1)
    approve(ctx, 2)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "open"), Dependency(3, "closed")])
    tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved"}
    ctx.tracker.close_issue(1)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "closed"), Dependency(3, "closed")])
    tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_one_run_per_project_still_promotes_other_issues(ctx):
    approve(ctx, 1)
    approve(ctx, 2)
    active = ctx.store.start_run(99, branch="active", worktree="active", pid=999)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == [active]
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_lowest_number_starts_and_launcher_pid_is_stored(ctx):
    for number in (9, 4, 2):
        approve(ctx, number, labels=["ready"] if number == 4 else [])
    calls = []

    def launch(number):
        calls.append(number)
        run = ctx.store.get_run(number)
        assert run is not None and run.pid is None
        assert "running" in ctx.tracker.get_issue(number).labels
        return 4321

    ctx.launcher = launch
    ctx.start_runs = True
    report = tick(ctx)
    assert calls == [2]
    assert ctx.tracker.get_issue(2).labels == {"approved", "running"}
    assert ctx.tracker.get_issue(4).labels == ctx.tracker.get_issue(9).labels == {"approved", "ready"}
    run = ctx.store.get_run(2)
    assert run.pid == 4321 and run.branch == "foragent/issue-2"
    assert run.worktree == str(ctx.store.worktree_path(2))
    assert [line for line in report.actions if "promoted" in line] == [
        "#2 promoted to ready", "#9 promoted to ready",
    ]


def test_launcher_can_return_no_pid(ctx):
    approve(ctx)
    ctx.start_runs = True
    ctx.launcher = lambda number: None
    tick(ctx)
    assert ctx.store.get_run(1).pid is None


def test_view_opens_once_after_all_available_lanes_launch(ctx, monkeypatch):
    ctx.config = replace(ctx.config, lanes={"ui": {}}, max_parallel=2, language="ko")
    ctx.start_runs = True
    approve(ctx, 1)
    approve(ctx, 2, labels=["lane:ui"])
    opened = []

    def open_view(root, store, language):
        assert {run.number: run.pid for run in store.active_runs()} == {1: 7001, 2: 7002}
        opened.append((root, store, language))

    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.setattr("foragent.scheduler.watch_open.open_view", open_view)
    tick(ctx)
    assert opened == [(ctx.repo_root, ctx.store, "ko")]
    # One attempt per five minutes; a later tick reopens a missing view without a new run.
    ctx.now = lambda: NOW + timedelta(minutes=4, seconds=59)
    tick(ctx)
    assert len(opened) == 1
    ctx.now = lambda: NOW + timedelta(minutes=5)
    tick(ctx)
    assert len(opened) == 2
    monkeypatch.setattr("foragent.watch_open._running", lambda store: True)
    ctx.now = lambda: NOW + timedelta(minutes=15)
    tick(ctx)
    assert len(opened) == 2


def test_view_reopen_skips_idle_repository_off_hours_and_no_start(ctx, monkeypatch):
    opened = []
    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.setattr("foragent.scheduler.watch_open.open_view", lambda *args: opened.append(args))
    ctx.start_runs = True
    tick(ctx)
    assert opened == [], "No active run means no view to reopen"
    approve(ctx)
    ctx.start_runs = False
    tick(ctx)
    assert opened == [] and ctx.store.active_runs() == []
    ctx.start_runs = True
    tick(ctx)
    assert len(opened) == 1
    ctx.now = lambda: NOW + timedelta(minutes=10)
    ctx.start_runs = False
    tick(ctx)
    assert len(opened) == 1, "--no-start never opens a view"
    local_hour = ctx.now().astimezone().hour
    ctx.config = replace(ctx.config, run_hours=f"{(local_hour + 1) % 24:02d}-{(local_hour + 2) % 24:02d}")
    ctx.start_runs = True
    tick(ctx)
    assert len(opened) == 1, "Outside run_hours never opens a view"


@pytest.mark.parametrize("failure", [
    FileNotFoundError("opener gone"), PermissionError("desktop unavailable"),
    subprocess.TimeoutExpired("opener", 5),
])
def test_view_opener_failure_keeps_all_runs_and_reports_success(ctx, monkeypatch, capsys, failure):
    ctx.config = replace(ctx.config, lanes={"ui": {}}, max_parallel=2, language="ko")
    ctx.start_runs = True
    approve(ctx, 1)
    approve(ctx, 2, labels=["lane:ui"])
    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.delenv("ORCA_TERMINAL_HANDLE", raising=False)
    monkeypatch.setattr("foragent.watch_open.shutil.which", lambda name: "/fake/osascript")

    def fail(argv, root):
        raise failure

    monkeypatch.setattr("foragent.watch_open._call", fail)
    report = tick(ctx)
    assert report.skipped is None
    assert {run.number: run.pid for run in ctx.store.active_runs()} == {1: 7001, 2: 7002}
    assert all("running" in ctx.tracker.get_issue(number).labels for number in (1, 2))
    assert {action for action in report.actions if "started running" in action} == {
        "#1 started running (pid=7001)", "#2 started running (pid=7002)",
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "레인 화면을 열지 못했어요" in captured.err
    tick(ctx)
    assert capsys.readouterr() == ("", "")
    # Every failure kind is retried once the spacing has passed.
    ctx.now = lambda: NOW + timedelta(minutes=5)
    tick(ctx)
    assert "레인 화면을 열지 못했어요" in capsys.readouterr().err


@pytest.mark.parametrize("failure", [
    OSError("cannot read watch.pid"), RuntimeError("opener failed\nunexpectedly"),
])
def test_view_opener_exception_preserves_tick_report_and_next_tick(ctx, monkeypatch, failure):
    ctx.config = replace(ctx.config, lanes={"ui": {}}, max_parallel=2)
    ctx.start_runs = True
    approve(ctx, 1)
    approve(ctx, 2, labels=["lane:ui"])
    opened = []

    def fail(root, store, language):
        opened.append((root, store, language))
        raise failure

    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.setattr("foragent.scheduler.watch_open.open_view", fail)
    report = tick(ctx)
    assert report.skipped is None
    assert {run.number: run.pid for run in ctx.store.active_runs()} == {1: 7001, 2: 7002}
    assert all("running" in ctx.tracker.get_issue(number).labels for number in (1, 2))
    assert {action for action in report.actions if "started running" in action} == {
        "#1 started running (pid=7001)", "#2 started running (pid=7002)",
    }
    assert report.actions[-1] == "Couldn't open the lane view (" + " ".join(str(failure).split()) + "). Try again."
    assert opened == [(ctx.repo_root, ctx.store, ctx.config.language)]
    assert tick(ctx).actions == []
    assert len(opened) == 1


def test_outside_local_run_hours_promotes_without_starting(ctx):
    approve(ctx)
    local_hour = ctx.now().astimezone().hour
    start = (local_hour + 1) % 24
    end = (local_hour + 2) % 24
    ctx.config = replace(ctx.config, run_hours=f"{start:02d}-{end:02d}")
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.active_runs() == []


def test_no_start_promotes_without_launching(ctx):
    approve(ctx)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.active_runs() == []


def test_locked_tick_has_no_git_or_tracker_side_effects(ctx, monkeypatch):
    def unexpected(*args):
        pytest.fail("a skipped tick must do no work")

    monkeypatch.setattr(ctx.git, "fetch", unexpected)
    monkeypatch.setattr(ctx.tracker, "list_open_issues", unexpected)
    with ctx.store.tick_lock() as acquired:
        assert acquired
        report = tick(ctx)
    assert report.skipped == "another tick is running" and report.actions == []


def test_fetches_configured_base_once_and_holds_tick_lock_through_launch(ctx, monkeypatch):
    approve(ctx)
    ctx.start_runs = True
    calls = []
    original_fetch = ctx.git.fetch

    def fetch(remote, ref):
        calls.append((remote, ref))
        with ctx.store.tick_lock() as acquired:
            assert not acquired
        original_fetch(remote, ref)

    def launch(number):
        with ctx.store.tick_lock() as acquired:
            assert not acquired
        return 123

    monkeypatch.setattr(ctx.git, "fetch", fetch)
    ctx.launcher = launch
    tick(ctx)
    assert calls == [("origin", "main")]


def test_integrity_rechecked_against_fresh_issue_right_before_start(ctx, monkeypatch):
    approve(ctx)
    ctx.start_runs = True
    original = ctx.tracker.get_issue
    calls = []

    def get_issue(number):
        issue = original(number)
        if "ready" in issue.labels:
            calls.append(number)
            return replace(issue, body=issue.body + "Changed before start\n")
        return issue

    monkeypatch.setattr(ctx.tracker, "get_issue", get_issue)
    tick(ctx)
    assert calls
    assert ctx.store.active_runs() == []
    assert original(1).labels == {"blocked"}
    assert ctx.store.load_approval(1) is None


def test_acceptance_blobs_rechecked_before_start(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    ctx.start_runs = True
    original = ctx.git.blob_shas
    calls = []

    def blob_shas(ref, paths):
        calls.append(ref)
        blobs = original(ref, paths)
        return blobs if len(calls) == 1 else {ACCEPTANCE: "changed"}

    monkeypatch.setattr(ctx.git, "blob_shas", blob_shas)
    tick(ctx)
    assert len(calls) == 2
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"blocked"}


@pytest.mark.parametrize("change", ["closed", "blocked", "needs-human", "unapproved", "reapproved"])
def test_issue_must_still_be_eligible_before_start(ctx, monkeypatch, change):
    approve(ctx)
    tick(ctx)
    ctx.start_runs = True
    original = ctx.tracker.get_issue

    def get_issue(number):
        issue = original(number)
        if change == "closed":
            return replace(issue, state="closed")
        if change in {"blocked", "needs-human"}:
            return replace(issue, labels=issue.labels | {change})
        if change == "unapproved":
            return replace(issue, labels=issue.labels - {"approved"})
        event = next(event for event in ctx.tracker.label_events(number) if event.label == "approved")
        ctx.tracker._events[number].append(
            replace(event, created_at=(NOW + timedelta(minutes=1)).isoformat())
        )
        return issue

    monkeypatch.setattr(ctx.tracker, "get_issue", get_issue)
    tick(ctx)
    assert ctx.store.active_runs() == []


@pytest.mark.parametrize("during", ["budget", "prerequisites"])
@pytest.mark.parametrize("change", ["blocked", "closed", "running", "unapproved"])
def test_start_rechecks_local_eligibility_after_collecting_ready_issues(ctx, monkeypatch, during, change):
    from foragent import scheduler

    approve(ctx)
    tick(ctx)
    ctx.start_runs = True
    launches = []
    ctx.launcher = launches.append
    store = StateStore(ctx.store.root)

    def update_local():
        # The remote deliberately stays ready: the runner's local write is truth.
        if change == "closed":
            store.update_local_issue(1, state="closed")
        else:
            store.record_labels(1, ["ready", "approved"] if change == "unapproved" else ["ready"],
                                remove=True, lanes=ctx.config.lanes)
            if change != "unapproved":
                store.record_labels(1, [change], remove=False, lanes=ctx.config.lanes)

    if during == "budget":
        budget = scheduler._machine_budget

        def machine_budget(*args):
            result = budget(*args)
            update_local()
            return result

        monkeypatch.setattr(scheduler, "_machine_budget", machine_budget)
    else:
        prerequisites = scheduler._check_prerequisites
        calls = []

        def check(*args, **kwargs):
            result = prerequisites(*args, **kwargs)
            calls.append(args[1].number)
            if len(calls) == 2:  # The live pre-start check, after the promotion check.
                update_local()
            return result

        monkeypatch.setattr(scheduler, "_check_prerequisites", check)

    tick(ctx)

    assert launches == []
    assert ctx.store.active_runs() == []
    row, = store.local_issues()
    if change == "closed":
        assert row["state"] == "closed"
    else:
        assert "ready" not in row["labels"]
    if during == "prerequisites":
        assert calls == [1, 1]


@pytest.mark.parametrize("missing_at", ["before-live-read", "before-reservation"])
def test_start_keeps_live_issue_when_local_snapshot_row_is_missing(ctx, monkeypatch, missing_at):
    from foragent import scheduler

    approve(ctx)
    tick(ctx)
    if missing_at == "before-live-read":
        ctx.store.drop_missing_github_issue(1)
    else:
        prerequisites = scheduler._check_prerequisites

        def check(*args, **kwargs):
            result = prerequisites(*args, **kwargs)
            ctx.store.drop_missing_github_issue(1)
            return result

        monkeypatch.setattr(scheduler, "_check_prerequisites", check)
    launched = []
    ctx.launcher = lambda number: launched.append(number) or 7000 + number
    report = scheduler.TickReport()

    started = scheduler._start_ready(
        ctx, {"main": [1]}, lambda issue: issue_lane(issue.labels, ctx.config.lanes),
        "origin/main", report, ("normal", 0.0),
    )

    assert started is True
    assert launched == [1]
    assert ctx.store.get_run(1).status == "running"
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}


def test_flag_only_changes_labels_and_posts_a_reason(ctx):
    approve(ctx, labels=["ready", "running"])
    original = ctx.tracker.get_issue(1)
    flag(ctx, 1, Reason.SPEC_QUESTION, "Choose a behavior.", "Option A or B.")
    issue = ctx.tracker.get_issue(1)
    assert issue.labels == {"approved", "blocked"}
    assert issue.body == original.body and issue.state == "open"
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.SPEC_QUESTION
    assert ctx.tracker.created_prs == ctx.tracker.merged_prs == []


def test_approver_config_is_case_insensitive(ctx):
    ctx.config = replace(ctx.config, approvers=["HUMAN"])
    approve(ctx, actor="human")
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("edit_seconds", [-1, 0, 1])
def test_first_snapshot_rejects_edits_at_or_after_approval(ctx, edit_seconds):
    approve(ctx)
    ctx.tracker._now = lambda: NOW + timedelta(seconds=edit_seconds)
    edit_body(ctx, 1, BODY + "Edited\n")
    ctx.now = lambda: NOW + timedelta(minutes=1)
    tick(ctx)
    if edit_seconds >= 0:
        assert ctx.store.load_approval(1) is None
        assert ctx.tracker.get_issue(1).labels == {"blocked"}
        assert len(ctx.tracker.list_comments(1)) == 1
        comment = ctx.tracker.list_comments(1)[0]
        assert parse_reason(comment.body) == Reason.HASH_MISMATCH
        assert "approval predates an edit" in comment.body
        # The tracker clock is deliberately frozen at the edit, separately from tick's clock.
        assert ctx.store.load_flag(1).flagged_at == comment.created_at == (
            NOW + timedelta(seconds=edit_seconds)
        ).isoformat()
    else:
        assert ctx.store.load_approval(1).body == BODY + "Edited\n"
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_body_changed_and_reverted_before_first_tick_still_requires_reapproval(ctx):
    approve(ctx)
    ctx.now = lambda: NOW + timedelta(seconds=1)
    edit_body(ctx, 1, BODY + "Edited\n")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    edit_body(ctx, 1, BODY)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"blocked"}


@pytest.mark.parametrize("content", ["pass\n", None])
def test_acceptance_changed_after_approval_before_first_tick(ctx, git_history, content):
    approve(ctx)
    git_history.update({ACCEPTANCE: content}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    comments = ctx.tracker.list_comments(1)
    assert len(comments) == 1 and parse_reason(comments[0].body) == Reason.HASH_MISMATCH
    assert ACCEPTANCE in comments[0].body


def test_acceptance_snapshot_uses_approval_commit_and_allows_unrelated_changes(ctx, git_history):
    git_history.update({ACCEPTANCE: "assert 1 == 1\n"}, at=NOW - timedelta(minutes=1))
    approved_ref = git_history.tip
    approve(ctx)
    git_history.update({"unrelated.py": "pass\n"}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    approval = ctx.store.load_approval(1)
    assert approval.acceptance_blobs == ctx.git.blob_shas(approved_ref, [ACCEPTANCE])
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_missing_approval_time_base_commit_requires_reapproval(ctx, monkeypatch):
    approve(ctx)
    monkeypatch.setattr(ctx.git, "commit_before", lambda ref, time: None)
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert "no base commit" in ctx.tracker.list_comments(1)[0].body


def test_hash_mismatch_reapproval_is_one_human_action_and_survives_restart(ctx):
    approve(ctx)
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=1)
    edit_body(ctx, 1, BODY + "Changed\n")
    ctx.now = lambda: NOW + timedelta(minutes=2)
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert ctx.store.load_flag(1).reason == Reason.HASH_MISMATCH
    ctx.store = StateStore(ctx.store.root, now=lambda: ctx.now())
    ctx.now = lambda: NOW + timedelta(minutes=3)
    # A person re-approves remotely before the next full snapshot refresh.
    ctx.tracker._tracker.record_label(1, "approved", "HUMAN")
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.load_approval(1).approved_at == ctx.now().isoformat()
    assert ctx.store.load_approval(1).body == BODY + "Changed\n"
    assert "#1 resumed after re-approval" in report.actions


@pytest.mark.parametrize("event_offset", [-1, 0])
def test_reapproval_must_be_strictly_after_flag(ctx, event_offset):
    approve(ctx)
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=2)
    flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
    ctx.tracker._now = lambda: NOW + timedelta(minutes=2, seconds=event_offset)
    ctx.tracker.record_label(1, "approved", "human")
    ctx.now = lambda: NOW + timedelta(minutes=3)
    assert tick(ctx).actions == []
    assert "blocked" in ctx.tracker.get_issue(1).labels


@pytest.mark.parametrize("reason", [Reason.NO_MACHINE_CHECK, Reason.SPEC_QUESTION, Reason.RETRIES_EXCEEDED])
def test_other_flags_require_human_removal_even_with_new_approval(ctx, reason):
    approve(ctx)
    flag(ctx, 1, reason, "Human input required.")
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "human")
    assert tick(ctx).actions == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.load_approval(1) is None
    ctx.tracker.remove_labels(1, ["blocked"])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_nonapprover_cannot_resume_hash_mismatch(ctx):
    approve(ctx)
    flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.record_label(1, "approved", "stranger")
    tick(ctx)
    assert "blocked" in ctx.tracker.get_issue(1).labels
    assert ctx.store.load_approval(1) is None


@pytest.mark.parametrize("needs_human", [False, True])
def test_approval_event_without_current_label_is_invalid(ctx, needs_human):
    approve(ctx)
    if needs_human:
        flag(ctx, 1, Reason.HASH_MISMATCH, "Re-approve.")
        ctx.now = lambda: NOW + timedelta(minutes=1)
        ctx.tracker.record_label(1, "approved", "human")
    issue = ctx.tracker.get_issue(1)
    ctx.tracker.issues[1] = replace(issue, labels=issue.labels - {"approved"})
    assert tick(ctx).actions == []
    assert ctx.store.load_approval(1) is None


def test_current_label_without_valid_timeline_is_invalid(ctx):
    approve(ctx)
    ctx.tracker._events[1].append(LabelEvent(
        "approved", "unlabeled", "human", (NOW + timedelta(minutes=1)).isoformat(),
    ))
    tick(ctx)
    assert ctx.store.load_approval(1) is None
    assert "ready" not in ctx.tracker.get_issue(1).labels


def mark_running(ctx, number=1, pid=123):
    ctx.tracker.remove_labels(number, ["ready"])
    ctx.tracker.add_labels(number, ["running"])
    return ctx.store.start_run(
        number, branch=f"foragent/issue-{number}", worktree=ctx.store.worktree_path(number), pid=pid,
    )


def test_stall_restarts_once_then_flags_second_stall(ctx):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
    (path / "unfinished.txt").write_text("unfinished")
    killed = []
    ctx.kill_runner = record_stop(ctx, killed.append)
    ctx.now = lambda: NOW + timedelta(minutes=31)
    tick(ctx)
    assert killed == [4321] and not path.exists()
    assert ctx.store.get_run(1).outcome == "stalled-restart"
    assert ctx.store.get_run(1).restarts == 1
    assert git_command(ctx.repo_root, "show", "foragent/issue-1:unfinished.txt") == "unfinished"
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert ctx.store.load_flag(1) is None

    # A new store instance must carry the restart budget into the normal start.
    ctx.store = StateStore(ctx.store.root, now=ctx.now)
    ctx.start_runs = True
    ctx.launcher = lambda number: 5678
    tick(ctx)
    assert ctx.store.get_run(1).pid == 5678
    assert ctx.store.get_run(1).restarts == 1
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    ctx.now = lambda: NOW + timedelta(minutes=62)
    tick(ctx)
    assert killed == [4321, 5678]
    assert ctx.store.get_run(1).outcome == "stalled" and ctx.store.get_run(1).restarts == 2
    assert ctx.store.load_flag(1).reason == Reason.STALLED
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert [run.outcome for run in ctx.store.read_history()] == ["stalled-restart", "stalled"]
    events = recovery_events(ctx)
    assert [event["kind"] for event in events] == ["stall-restart", "stalled"]
    assert all(event["issue"] == 1 and event["at"] and event["summary"] for event in events)
    assert tick(ctx).actions == []
    assert recovery_events(ctx) == events


@pytest.mark.parametrize("progress,stalls", [("recent", False), ("boundary", False), ("missing", True)])
def test_stall_uses_latest_progress_or_start_time(ctx, progress, stalls):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=40)
    value = {"recent": NOW + timedelta(minutes=39), "boundary": NOW + timedelta(minutes=10)}
    ctx.store.update_run(1, last_progress_at=value[progress].isoformat() if progress in value else "")
    killed = []
    ctx.kill_runner = record_stop(ctx, killed.append)
    tick(ctx)
    assert bool(killed) == stalls
    assert ctx.store.get_run(1).status == ("finished" if stalls else "running")


def test_failed_stall_termination_does_not_consume_restart(ctx):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=31)

    def failed(pid):
        raise TimeoutError("runner still alive")

    ctx.kill_runner = failed
    tick(ctx)
    run = ctx.store.get_run(1)
    assert run.status == "running" and run.restarts == 0 and run.outcome is None
    assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED


def test_stall_intent_survives_runner_finishing_during_termination(ctx):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=31)

    def terminated(pid):
        ctx.process_table.exit(pid)
        run = ctx.store.get_run(1)
        # Runner's finally block uses the tick's persisted termination outcome.
        ctx.store.finish_run(1, run.outcome or "killed")

    ctx.kill_runner = terminated
    tick(ctx)
    assert ctx.store.get_run(1).outcome == "stalled-restart"
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.get_run(1).restarts == 1


@pytest.mark.parametrize("previous_restarts", [0, 1])
def test_successful_stall_kill_keeps_count_when_locked_worktree_cleanup_fails(ctx, monkeypatch, fake_process_table, previous_restarts):
    fake_process_table.add(4321, cwd=ctx.repo_root, start_time=10)
    fake_process_table.ignore_term.add(4321)
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    ctx.store.update_run(1, restarts=previous_restarts)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    git_command(ctx.repo_root, "worktree", "lock", str(path))
    ctx.kill_runner = kill_process_group
    ctx.now = lambda: NOW + timedelta(minutes=31)
    with pytest.raises(GitError, match="locked"):
        tick(ctx)
    stalled = ctx.store.get_run(1)
    outcome = "stalled-restart" if previous_restarts == 0 else "stalled"
    assert 4321 not in fake_process_table.live
    assert ("pid", 4321, signal.SIGKILL) in fake_process_table.signals
    assert stalled.status == "running" and stalled.restarts == previous_restarts + 1
    assert stalled.outcome == outcome
    git_command(ctx.repo_root, "worktree", "unlock", str(path))
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    tick(ctx)
    assert not path.exists() and ctx.store.get_run(1).outcome == outcome
    assert ctx.store.get_run(1).restarts == previous_restarts + 1
    assert [past.outcome for past in ctx.store.read_history()] == [outcome]
    if previous_restarts == 0:
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    else:
        assert ctx.store.load_flag(1).reason == Reason.STALLED
        assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}


@pytest.mark.parametrize("outcome,restarts", [("stalled-restart", 1), ("stalled", 2)])
@pytest.mark.parametrize("pid", [None, 4321])
def test_dead_active_run_recovers_persisted_stall_intent(ctx, monkeypatch, outcome, restarts, pid):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=pid)
    ctx.store.update_run(1, restarts=restarts, outcome=outcome)
    ctx.now = lambda: NOW + timedelta(minutes=31)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    killed = []
    ctx.kill_runner = record_stop(ctx, killed.append)
    tick(ctx)
    assert killed == ([] if pid is None else [pid])
    assert ctx.store.get_run(1).status == "finished"
    assert ctx.store.get_run(1).outcome == outcome and ctx.store.get_run(1).restarts == restarts
    assert [past.outcome for past in ctx.store.read_history()] == [outcome]
    if restarts == 1:
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
        ctx.start_runs = True
        tick(ctx)
        assert ctx.store.get_run(1).restarts == 1
    else:
        assert ctx.store.load_flag(1).reason == Reason.STALLED
        assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
        ctx.start_runs = True
        tick(ctx)
        assert ctx.store.active_runs() == []


def test_dead_pid_finishes_run_and_restores_ready(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.process_table.exit(123)
    report = tick(ctx)
    assert not ctx.process_table.signals
    assert ctx.store.active_runs() == []
    assert ctx.store.read_history()[0].outcome == "crashed"
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert "#1 runner crashed" in report.actions


@pytest.mark.parametrize("permission_denied", [False, True])
def test_alive_or_inaccessible_pid_keeps_project_slot(ctx, monkeypatch, permission_denied):
    from foragent import processes

    approve(ctx)
    tick(ctx)
    active = mark_running(ctx)
    calls = []

    def kill(pid, signal):
        calls.append((pid, signal))
        if permission_denied:
            raise PermissionError(pid)

    monkeypatch.setattr("foragent.scheduler.os.kill", kill)
    ctx.now = lambda: NOW + timedelta(hours=1)
    active = ctx.store.update_run(1, last_progress_at=ctx.now().isoformat())
    listing = ctx.process_table.list_processes
    cached = iter((tuple(row for row in listing() if row.pid != active.pid),))
    monkeypatch.setattr(processes, "list_processes", lambda: next(cached, None) or listing())
    ctx.start_runs = True
    tick(ctx)
    assert calls == [(123, 0)]
    assert ctx.store.active_runs() == [active]
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}


@pytest.mark.parametrize("elapsed,crashed", [(119, False), (120, True), (121, True)])
def test_pid_none_has_two_minute_startup_grace(ctx, monkeypatch, elapsed, crashed):
    approve(ctx)
    tick(ctx)
    active = mark_running(ctx, pid=None)

    def unexpected(*args):
        pytest.fail("no pid is available to probe")

    monkeypatch.setattr("foragent.scheduler.os.kill", unexpected)
    probes = []
    ctx.runner_process_exists = lambda number: probes.append(number) or False
    ctx.now = lambda: NOW + timedelta(seconds=elapsed)
    tick(ctx)
    assert probes == ([1] if crashed else [])
    if crashed:
        assert ctx.store.active_runs() == []
        assert ctx.store.get_run(1).outcome == "crashed"
        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    else:
        assert ctx.store.active_runs() == [active]


def test_two_consecutive_crashes_flag_and_stop_relaunching(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.start_runs = True
    ctx.now = lambda: NOW + timedelta(minutes=1)
    tick(ctx)
    assert launches == [1] and ctx.store.get_run(1).pid == 456
    ctx.process_table.exit(456)
    ctx.now = lambda: NOW + timedelta(minutes=2)
    report = tick(ctx)
    assert launches == [1] and ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed", "crashed"]
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert parse_reason(ctx.tracker.list_comments(1)[-1].body) == Reason.RETRIES_EXCEEDED
    assert ctx.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "#1 flagged retries-exceeded" in report.actions
    events = recovery_events(ctx)
    assert [event["kind"] for event in events] == ["crash-restart", "retries-exceeded"]
    assert "blocked" in events[-1]["summary"]
    assert tick(ctx).actions == []
    assert recovery_events(ctx) == events


@pytest.mark.parametrize("intervening_number,expected_flag", [(1, False), (2, True)])
def test_crash_streak_is_per_issue_and_resets_on_another_outcome(ctx, monkeypatch, intervening_number, expected_flag):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.store.finish_run(1, "crashed")
    ctx.now = lambda: NOW + timedelta(seconds=1)
    ctx.store.start_run(intervening_number, branch="other", worktree="other")
    ctx.store.finish_run(intervening_number, "not-implemented")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(seconds=3)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    tick(ctx)
    assert ("blocked" in ctx.tracker.get_issue(1).labels) == expected_flag
    assert ("ready" in ctx.tracker.get_issue(1).labels) != expected_flag


@pytest.mark.parametrize("state", ["closed", "blocked", "needs-human"])
def test_dead_run_finishes_without_requeueing_closed_or_flagged_issue(ctx, monkeypatch, state):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    if state == "closed":
        ctx.tracker.close_issue(1)
    else:
        ctx.tracker.add_labels(1, [state])
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    tick(ctx)
    assert ctx.store.active_runs() == []
    assert ctx.store.get_run(1).outcome == "crashed"
    assert not ctx.tracker.get_issue(1).labels & {"ready", "running"}


def test_cancelled_blocker_flags_once_instead_of_promoting(ctx):
    approve(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "open"), Dependency(3, "closed", "not_planned")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    comment = ctx.tracker.list_comments(1)[-1].body
    assert parse_reason(comment) == Reason.SPEC_QUESTION
    assert "blocker #3 was closed as not planned" in comment
    count = len(ctx.tracker.list_comments(1))
    assert tick(ctx).actions == []
    assert len(ctx.tracker.list_comments(1)) == count


def test_completed_blocker_allows_promotion(ctx):
    approve(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


@pytest.mark.parametrize("command,exists", [
    ("/usr/bin/python -m foragent.cli gate run-issue 1 --repo-root /repo", True),
    ("/repo/.venv/bin/foragent gate run-issue 1 --repo-root /repo", True),
    ("/repo/.venv/bin/fa gate run-issue 1 --repo-root /repo with spaces", True),
    ("/usr/bin/python -m foragent.cli gate run-issue 11 --repo-root /repo", False),
    ("/usr/bin/python -m foragent.cli gate run-issue 2 --repo-root /repo", False),
    ("/repo/.venv/bin/fa gate status --repo-root /repo", False),
    ("/usr/bin/other run-issue 1", False),
    ("/usr/bin/notforagent gate run-issue 1 --repo-root /repo", False),
])
def test_process_probe_parses_ps_and_matches_exact_issue(monkeypatch, command, exists):
    def ps(args, **kwargs):
        assert args == ["ps", "-axo", "pid,command"]
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return subprocess.CompletedProcess(args, 0, stdout=f"  PID COMMAND\n  1234 {command}\n")

    monkeypatch.setattr("foragent.scheduler.subprocess.run", ps)
    assert runner_process_exists(1) is exists


@pytest.mark.parametrize("failure", [
    FileNotFoundError("ps unavailable"), PermissionError("ps denied"),
    subprocess.CalledProcessError(1, ["ps"]),
])
def test_process_probe_cannot_declare_death_when_ps_fails(monkeypatch, failure):
    def ps(*args, **kwargs):
        raise failure

    monkeypatch.setattr("foragent.scheduler.subprocess.run", ps)
    assert runner_process_exists(1)


@pytest.mark.parametrize("elapsed", [120, 3600])
def test_missing_pid_with_live_runner_keeps_project_slot(ctx, elapsed):
    approve(ctx)
    approve(ctx, 2)
    tick(ctx)
    active = mark_running(ctx, pid=None)
    probes = []
    launches = []
    ctx.runner_process_exists = lambda number: probes.append(number) or True
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.now = lambda: NOW + timedelta(seconds=elapsed)
    ctx.start_runs = True
    tick(ctx)
    assert probes == [1] and launches == []
    assert ctx.store.active_runs() == [active]
    assert ctx.store.read_history() == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


def test_pid_persistence_failure_does_not_launch_a_second_runner(ctx, monkeypatch):
    approve(ctx)
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 456
    ctx.start_runs = True

    def fail(*args, **kwargs):
        raise OSError("pid persistence failed")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.store, "update_run", fail)
        with pytest.raises(OSError, match="pid persistence failed"):
            tick(ctx)
    active = ctx.store.get_run(1)
    assert active.pid is None and launches == [1]
    ctx.runner_process_exists = lambda number: True
    ctx.now = lambda: NOW + timedelta(minutes=3)
    tick(ctx)
    assert launches == [1] and ctx.store.active_runs() == [active]
    assert ctx.store.read_history() == []


@pytest.mark.parametrize("approved", [False, True])
def test_orphan_running_without_record_returns_to_ready(ctx, approved):
    if approved:
        approve(ctx, labels=["running"])
    else:
        ctx.tracker.add_issue(1, body=BODY, labels=["running"])
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == ({"approved", "ready"} if approved else {"ready"})
    assert ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed"]
    assert "#1 orphaned running label recovered as crashed" in report.actions
    assert "#1 returned to ready after runner crash" in report.actions
    assert tick(ctx).actions == []
    assert len(ctx.store.read_history()) == 1


def test_orphan_recovery_rechecks_local_labels_before_recording_a_crash(ctx, monkeypatch):
    from foragent.scheduler import TickReport, _recover_orphan

    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    stale = ctx.tracker.get_issue(1)
    store = StateStore(ctx.store.root)
    store.finish_run(1, "timeout")
    store.record_labels(1, ["blocked"], remove=False, lanes=ctx.config.lanes)
    store.record_labels(1, ["running"], remove=True, lanes=ctx.config.lanes)
    history = (store.root / "runs.jsonl").read_text()
    monkeypatch.setattr(ctx.tracker, "get_issue", lambda n: pytest.fail("Recheck must be local"))
    report = TickReport()

    issue = _recover_orphan(ctx, stale, report)

    assert issue.labels == {"approved", "blocked"}
    assert (store.root / "runs.jsonl").read_text() == history
    assert report.actions == []


@pytest.mark.parametrize("action", ["orphan", "crash", "blocker"])
def test_recovery_keeps_supplied_issue_when_local_snapshot_row_is_missing(ctx, action):
    from foragent.scheduler import TickReport, _escalate_overdue_blocker, _recover_orphan, _restore_after_crash

    # Seed and read through the raw tracker so no recorder has observed the issue.
    tracker = ctx.tracker._tracker
    tracker.add_issue(1, "Issue 1", BODY, labels=["blocked" if action == "blocker" else "running"])
    tracker.record_label(1, "approved", "human")
    issue = tracker.get_issue(1)
    assert ctx.store.local_issues() == []
    report = TickReport()
    if action == "orphan":
        path = ctx.store.worktree_path(1)
        ctx.git.add_worktree(path, "foragent/issue-1", "origin/main")
        ctx.store.start_run(1, branch="foragent/issue-1", worktree=path)
        ctx.store.finish_run(1, "error")

        recovered = _recover_orphan(ctx, issue, report)

        assert not path.exists()
        assert recovered.labels == {"approved", "ready"}
        assert ctx.store.get_run(1).outcome == "crashed"
    elif action == "crash":
        _restore_after_crash(ctx, issue, report)

        assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
        assert report.actions == ["#1 returned to ready after runner crash"]
    else:
        ctx.now = lambda: NOW + timedelta(minutes=ctx.config.orchestrator_timeout_minutes + 1)

        current = _escalate_overdue_blocker(ctx, issue, report)

        assert current is issue
        assert ctx.store.local_issues() == []
        assert report.actions == []


@pytest.mark.parametrize("change", ["blocked", "needs-human", "closed", "finished"])
def test_crash_restore_rechecks_local_issue_before_changing_labels(ctx, change):
    from foragent.scheduler import TickReport, _restore_after_crash

    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    stale = ctx.tracker.get_issue(1)
    # Local updates are observations at their call time, so they share the gate's clock.
    store = StateStore(ctx.store.root, now=ctx.now)
    if change == "closed":
        store.update_local_issue(1, state="closed")
    else:
        store.record_labels(1, ["ready"], remove=True, lanes=ctx.config.lanes)
        store.record_labels(1, ["blocked" if change == "finished" else change],
                            remove=False, lanes=ctx.config.lanes)
    if change == "finished":
        store.finish_run(1, "timeout")
        store.record_labels(1, ["running"], remove=True, lanes=ctx.config.lanes)
    report = TickReport()

    _restore_after_crash(ctx, stale, report)

    row, = store.local_issues()
    assert not set(row["labels"]) & {"ready", "running"}
    assert report.actions == []
    assert recovery_events(ctx) == []


def test_runner_finishing_during_process_probe_is_not_recovered(ctx, monkeypatch):
    from foragent.scheduler import TickReport, _recover_runs

    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    store = StateStore(ctx.store.root)
    probes, killed = [], []

    def finish(pid, sig):
        probes.append((pid, sig))
        store.finish_run(1, "timeout")
        store.record_labels(1, ["blocked"], remove=False, lanes=ctx.config.lanes)
        store.record_labels(1, ["running"], remove=True, lanes=ctx.config.lanes)
        raise ProcessLookupError(pid)

    monkeypatch.setattr("foragent.scheduler.os.kill", finish)
    ctx.kill_runner = killed.append
    report = TickReport()

    _recover_runs(ctx, report)

    assert probes == [(123, 0)]
    assert killed == []
    assert report.actions == []
    assert [run.outcome for run in store.read_history()] == ["timeout"]


def test_two_distinct_orphan_running_labels_count_as_consecutive_crashes(ctx):
    approve(ctx, labels=["running"])
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=1)
    ctx.tracker.remove_labels(1, ["ready"])
    ctx.tracker.add_labels(1, ["running"])
    report = tick(ctx)
    assert ctx.store.active_runs() == []
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed", "crashed"]
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "#1 flagged retries-exceeded" in report.actions
    assert tick(ctx).actions == []


@pytest.mark.parametrize("operation", ["get_issue", "add_labels", "remove_labels"])
def test_crash_recovery_replays_after_tracker_failures_without_double_counting(ctx, monkeypatch, operation):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.process_table.exit(ctx.store.get_run(1).pid)

    def unavailable(*args, **kwargs):
        raise TrackerError("HTTP 503")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.tracker, operation, unavailable)
        for _ in range(2):
            with pytest.raises(TrackerError, match="HTTP 503"):
                tick(ctx)
            assert ctx.store.active_runs() == []
            assert len(ctx.store.read_history()) == 1
    ctx.now = lambda: NOW + timedelta(minutes=1)
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert [run.outcome for run in ctx.store.read_history()] == ["crashed"]
    assert ctx.store.load_flag(1) is None
    if operation == "get_issue":
        # The successful label writes were already recorded before the read failed.
        assert report.actions == []
    else:
        assert "#1 orphaned running label recovered as crashed" in report.actions


def test_ready_issue_reapproved_without_machine_check_is_flagged(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.remove_labels(1, ["approved"])
    ctx.now = lambda: NOW + timedelta(seconds=1)
    edit_body(ctx, 1, "## Done when\n- [ ] Looks good\n")
    ctx.now = lambda: NOW + timedelta(seconds=2)
    ctx.tracker.record_label(1, "approved", "human")
    ctx.start_runs = True
    report = tick(ctx)
    assert ctx.store.load_approval(1).body == "## Done when\n- [ ] Looks good\n"
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.active_runs() == []
    assert ctx.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK
    assert "#1 flagged no-machine-check" in report.actions


@pytest.mark.parametrize("reopened", [False, True])
@pytest.mark.parametrize("start_runs", [False, True])
def test_ready_issue_with_new_or_reopened_blocker_returns_to_approved_waiting(ctx, reopened, start_runs):
    approve(ctx)
    if reopened:
        ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "open")])
    ctx.start_runs = start_runs
    report = tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved"}
    assert ctx.store.active_runs() == [] and ctx.store.load_flag(1) is None
    assert "#1 returned to approved waiting for open blockers" in report.actions
    ctx.start_runs = False
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "completed")])
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}


def test_ready_issue_with_cancelled_blocker_is_flagged(ctx):
    approve(ctx)
    tick(ctx)
    ctx.tracker.set_blocked_by(1, [Dependency(2, "closed", "not_planned")])
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.active_runs() == []
    assert ctx.store.load_flag(1).reason == Reason.SPEC_QUESTION


@pytest.mark.parametrize("cancelled", [False, True])
def test_dependencies_are_rechecked_immediately_before_launch(ctx, monkeypatch, cancelled):
    approve(ctx)
    tick(ctx)
    calls = []

    def blocked_by(number):
        calls.append(number)
        if len(calls) == 1:
            return []
        return [Dependency(2, "closed", "not_planned") if cancelled else Dependency(2, "open")]

    monkeypatch.setattr(ctx.tracker, "blocked_by", blocked_by)
    ctx.start_runs = True
    tick(ctx)
    assert calls == [1, 1] and ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == ({"approved", "blocked"} if cancelled else {"approved"})


def test_machine_check_is_rechecked_from_snapshot_immediately_before_launch(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    calls = []

    def has_machine_check(parsed):
        calls.append(parsed)
        return len(calls) == 1

    monkeypatch.setattr("foragent.scheduler.has_machine_check", has_machine_check)
    ctx.start_runs = True
    tick(ctx)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[1].done_when_commands == [f"pytest {ACCEPTANCE}"]
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "blocked"}
    assert ctx.store.load_flag(1).reason == Reason.NO_MACHINE_CHECK


def test_custom_fetch_refspec_cannot_hide_changed_acceptance_files(ctx, git_history):
    approve(ctx)
    tick(ctx)
    git_command(
        git_history.clone, "config", "remote.origin.fetch",
        "+refs/heads/other:refs/remotes/origin/other",
    )
    git_history.update({ACCEPTANCE: "assert False\n"}, at=NOW + timedelta(minutes=1))
    ctx.now = lambda: NOW + timedelta(minutes=2)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.tracker.get_issue(1).labels == {"blocked"}
    assert ctx.store.active_runs() == [] and ctx.store.load_approval(1) is None
    assert ctx.store.load_flag(1).reason == Reason.HASH_MISMATCH


@pytest.mark.parametrize("existing_snapshot", [False, True])
def test_new_approval_snapshots_fresh_body_after_stale_issue_list(ctx, monkeypatch, existing_snapshot):
    approve(ctx)
    if existing_snapshot:
        tick(ctx)
    list_issues = ctx.tracker.list_issues
    refresh_at = NOW + (timedelta(minutes=5) if existing_snapshot else timedelta())
    ctx.now = lambda: refresh_at
    new_body = BODY.replace("Implement the feature.", "Implement the approved revision.")

    def stale_issues():
        issues = list_issues()
        ctx.now = lambda: refresh_at + timedelta(seconds=1)
        ctx.tracker.remove_labels(1, ["approved"])
        edit_body(ctx, 1, new_body)
        ctx.now = lambda: refresh_at + timedelta(seconds=2)
        ctx.tracker.record_label(1, "approved", "human")
        return issues

    monkeypatch.setattr(ctx.tracker, "list_issues", stale_issues)
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.load_approval(1).body == new_body
    assert ctx.store.load_approval(1).approved_at == (refresh_at + timedelta(seconds=2)).isoformat()
    assert ctx.store.load_flag(1) is None
    assert ctx.tracker.get_issue(1).labels == {"approved", "running"}
    assert ctx.store.get_run(1).pid == 7001


@pytest.mark.parametrize("labels,expected", [
    ([], "main"), (["lane:main"], "main"), (["lane:ui"], "ui"),
    (["lane:unknown"], "main"),
])
def test_lane_assignment(labels, expected):
    assert issue_lane(labels, {"ui": {}}) == expected


def test_unknown_lane_reports_once_per_tick_and_uses_main_slot(ctx):
    approve(ctx, 1, labels=["lane:missing"])
    approve(ctx, 2, labels=["lane:missing"])
    ctx.start_runs = True
    for _ in range(2):
        report = tick(ctx)
        assert [action for action in report.actions if "Unknown lane" in action] == [
            "Unknown lane lane:missing; treating as main",
        ]
        assert [run.number for run in ctx.store.active_runs()] == [1]
        assert ctx.store.get_run(1).lane == "main"


@pytest.mark.parametrize("cap,expected,finished,following", [
    (1, [1], 1, 2), (2, [1, 2], 2, 3), (3, [1, 2, 4], 2, 3),
])
def test_lane_single_run_cap_order_and_lowest_issue(ctx, cap, expected, finished, following):
    # Lanes rank together by issue number (#603); a lane still runs one issue at a time.
    ctx.config = replace(ctx.config, lanes={"beta": {}, "alpha": {}}, max_parallel=cap)
    # A roomy machine-wide AIMD value leaves the project ceiling as the binding cap.
    write_state(ctx.store, 8, NOW - timedelta(hours=1))
    for number, lane in [(1, "main"), (3, "alpha"), (2, "alpha"), (4, "beta")]:
        approve(ctx, number, labels=[f"lane:{lane}"])
    launches = []
    ctx.launcher = lambda number: launches.append(number) or 7000 + number
    ctx.start_runs = True
    tick(ctx)
    tick(ctx)
    assert launches == expected
    assert len(ctx.store.active_runs()) == cap
    assert len({run.lane for run in ctx.store.active_runs()}) == cap
    ctx.tracker.close_issue(finished)
    ctx.tracker.remove_labels(finished, ["running"])
    ctx.store.finish_run(finished, "merged")
    tick(ctx)
    assert launches == [*expected, following]


def test_integrity_is_rechecked_before_each_lane_start(ctx):
    ctx.config = replace(ctx.config, lanes={"alpha": {}, "beta": {}})
    approve(ctx, 1, labels=["lane:alpha"])
    approve(ctx, 2, labels=["lane:beta"])
    launches = []

    def launch(number):
        launches.append(number)
        ctx.tracker.edit_body(2, BODY + "changed after ready")
        return 7000 + number

    ctx.launcher = launch
    ctx.start_runs = True
    tick(ctx)
    assert launches == [1]
    assert ctx.store.load_flag(2).reason == Reason.HASH_MISMATCH


def test_paused_lane_skipped_until_expiry(ctx):
    ctx.config = replace(ctx.config, lanes={"ui": {}})
    approve(ctx, labels=["lane:ui"])
    ctx.store.pause_lane("ui", NOW + timedelta(minutes=60))
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == []
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready", "lane:ui"}
    ctx.now = lambda: NOW + timedelta(minutes=60)
    tick(ctx)
    assert ctx.store.get_run(1).lane == "ui"


def test_tick_runtime_backstop_excludes_waited_seconds(ctx):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=130)
    run = ctx.store.update_run(1, waited_seconds=60 * 60, last_progress_at=ctx.now().isoformat())
    tick(ctx)
    assert ctx.store.active_runs() == [run]
    ctx.store.update_run(1, waited_seconds=0)
    tick(ctx)
    assert ctx.store.get_run(1).outcome == "killed"
    assert ctx.store.load_flag(1).reason == Reason.TIMEOUT


def test_tick_does_not_stall_a_waiting_executor(ctx):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.store.update_run(1, waiting_until=(NOW + timedelta(minutes=60)).isoformat(), waited_seconds=3600)
    ctx.now = lambda: NOW + timedelta(minutes=40)
    tick(ctx)
    assert len(ctx.store.active_runs()) == 1
    assert ctx.store.read_history() == []


def test_limit_wait_can_resume_an_issue_with_an_existing_retry_pr(ctx):
    ctx.config = replace(ctx.config, lanes={"ui": {}})
    approve(ctx, labels=["lane:ui"])
    tick(ctx)
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.store.worktree_path(1), lane="ui")
    ctx.store.update_run(1, attempts=2, restarts=1)
    ctx.store.finish_run(1, "limit-wait")
    ctx.store.pause_lane("ui", NOW + timedelta(minutes=60))
    ctx.tracker.create_pr(head="foragent/issue-1", base="main", title="Retry in progress", body="Closes #1")
    ctx.start_runs = True
    tick(ctx)
    assert ctx.store.active_runs() == [] and "ready" in ctx.tracker.get_issue(1).labels
    ctx.now = lambda: NOW + timedelta(minutes=60)
    tick(ctx)
    assert ctx.store.get_run(1).status == "running" and ctx.store.get_run(1).lane == "ui"
    assert (ctx.store.get_run(1).attempts, ctx.store.get_run(1).restarts) == (2, 1)


def recovery_events(ctx):
    path = ctx.store.root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("identities", [None, []])
def test_dead_old_format_run_does_not_abort_tick_and_preserves_wip(ctx, identities):
    ctx.config = replace(ctx.config, lanes={"other": {}})
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    (path / "unfinished.txt").write_text("unfinished")
    ctx.store.update_run(1, executor_pgid=4322)
    record = ctx.store.root / "runs/1.json"
    value = json.loads(record.read_text())
    if identities is None:
        value.pop("process_identities")
    else:
        value["process_identities"] = identities
    record.write_text(json.dumps(value))
    ctx.process_table.exit(4321)
    approve(ctx, 2, labels=["ready", "lane:other"])
    ctx.start_runs = True

    report = tick(ctx)

    assert ctx.store.get_run(2).status == "running"
    assert "#1 runner crashed" in report.actions
    history, = ctx.store.read_history()
    assert history.number == 1 and history.outcome == "crashed"
    assert history.executor_pgid is None
    assert "Crash recovery for running label generation" in history.detail
    assert git_command(ctx.repo_root, "show", "foragent/issue-1:unfinished.txt") == "unfinished"
    assert not path.exists()
    assert not ctx.process_table.signals
    assert [event["kind"] for event in recovery_events(ctx)] == ["crash-restart"]


@pytest.mark.parametrize("alive", ["runner", "executor", "leaderless-executor"])
def test_alive_unverified_run_flags_once_and_tick_processes_other_issues(ctx, alive):
    ctx.config = replace(ctx.config, lanes={"other": {}})
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    if alive != "runner":
        ctx.process_table.exit(4321)
        pid = 4322 if alive == "executor" else 4323
        ctx.process_table.add(pid, pgid=4322, cwd=path, start_time=NOW.timestamp())
        ctx.store.update_run(1, executor_pgid=4322)
    record = ctx.store.root / "runs/1.json"
    value = json.loads(record.read_text())
    value["process_identities"] = []
    record.write_text(json.dumps(value))
    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.stall_minutes + 1)
    approve(ctx, 2, labels=["ready", "lane:other"])
    ctx.start_runs = True

    tick(ctx)
    flag_before = ctx.store.load_flag(1)
    events = recovery_events(ctx)
    checked_at = ctx.now()
    ctx.now = lambda: checked_at + timedelta(seconds=1)
    tick(ctx)

    assert ctx.store.get_run(1).status == "running"
    assert ctx.store.get_run(1).process_identities == []
    assert (ctx.store.get_run(1).outcome, ctx.store.get_run(1).restarts) == (None, 0)
    assert ctx.store.get_run(2).status == "running"
    assert path.exists() and ctx.store.read_history() == []
    assert not ctx.process_table.signals
    assert ctx.store.load_flag(1) == flag_before and flag_before.reason == Reason.STOP_REFUSED
    assert recovery_events(ctx) == events
    assert [event["kind"] for event in events] == ["stop-refused"]
    assert "No recorded process identity" in events[0]["summary"]
    assert sum(parse_reason(comment.body) == Reason.STOP_REFUSED
               for comment in ctx.tracker.list_comments(1)) == 1


@pytest.mark.parametrize("error", [ValueError, PermissionError, TimeoutError])
@pytest.mark.parametrize("trigger", ["crash", "stall", "timeout", "integrity"])
@pytest.mark.parametrize("target", ["runner", "executor"])
def test_refused_tick_stop_preserves_run_and_processes_other_issue(ctx, error, trigger, target):
    ctx.config = replace(ctx.config, lanes={"other": {}})
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    ctx.process_table.add(4322, ppid=4321, cwd=ctx.repo_root, start_time=NOW.timestamp() + 1)
    ctx.store.update_run(1, executor_pgid=4322)
    if trigger == "crash":
        ctx.process_table.exit(4321)
    elif trigger == "stall":
        ctx.now = lambda: NOW + timedelta(minutes=ctx.config.stall_minutes + 1)
    elif trigger == "timeout":
        ctx.now = lambda: NOW + timedelta(minutes=ctx.config.max_runtime_minutes + 11)
    else:
        edit_body(ctx, 1, BODY + "Changed scope\n")
    calls = []

    def refused(pid):
        calls.append(pid)
        if target == "executor" and pid == 4321:
            ctx.process_table.exit(pid)
            return
        raise error("stop could not finish")

    ctx.kill_runner = refused
    approve(ctx, 2, labels=["ready", "lane:other"])
    tick(ctx)

    run = ctx.store.get_run(1)
    assert calls == ([4321, 4322] if target == "executor" else [4321])
    assert run.status == "running" and run.outcome is None and run.restarts == 0
    assert run.executor_pgid == 4322
    assert ctx.store.get_run(2) is None  # Promotion is processed without starting workers.
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert ctx.store.read_history() == []
    assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED
    assert [event["kind"] for event in recovery_events(ctx)] == ["stop-refused"]
    assert not ctx.process_table.signals
    events = recovery_events(ctx)
    calls_before = calls[:]
    tick(ctx)
    assert calls == calls_before and recovery_events(ctx) == events


@pytest.mark.parametrize("error", [ValueError, PermissionError, TimeoutError])
def test_refused_stop_after_process_exit_recovers_as_crashed(ctx, error):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.stall_minutes + 1)
    approve(ctx, 2)

    def exited(pid):
        ctx.process_table.exit(pid)
        raise error("process exited during stop")

    ctx.kill_runner = exited
    tick(ctx)

    run = ctx.store.get_run(1)
    assert (run.status, run.outcome, run.restarts) == ("finished", "crashed", 0)
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert ctx.store.load_flag(1) is None
    assert [past.outcome for past in ctx.store.read_history()] == ["crashed"]
    assert [event["kind"] for event in recovery_events(ctx)] == ["crash-restart"]
    assert not ctx.process_table.signals


@pytest.mark.parametrize("error", [ValueError, PermissionError, TimeoutError])
def test_refused_inactive_reaping_continues_tick_and_flags_once(ctx, monkeypatch, error):
    from foragent import processes

    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    ctx.store.finish_run(1, "killed")
    approve(ctx, 2)

    def refused(pid):
        raise error("inactive process still alive")

    monkeypatch.setattr(processes, "kill_process_group", refused)
    tick(ctx)
    events = recovery_events(ctx)
    tick(ctx)
    assert "ready" in ctx.tracker.get_issue(2).labels
    assert ctx.store.get_run(1).outcome == "killed"
    assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED
    assert recovery_events(ctx) == events
    assert [event["kind"] for event in events] == ["stop-refused"]
    assert not ctx.process_table.signals


def test_crash_cleanup_kills_group_before_removing_worktree(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx, pid=4321)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    actions = []
    ctx.kill_runner = record_stop(ctx, lambda pid: actions.append(("kill", pid)))
    monkeypatch.setattr(ctx.git, "remove_worktree", lambda path: actions.append(("remove", path)))
    tick(ctx)
    assert actions == [("kill", 4321), ("remove", ctx.store.worktree_path(1))]
    events = recovery_events(ctx)
    assert len(events) == 1
    assert events[0] == {"at": NOW.isoformat(), "issue": 1, "kind": "crash-restart",
                         "summary": "Runner crashed; returned to ready for restart."}
    tick(ctx)
    assert recovery_events(ctx) == events


def test_crash_group_termination_failure_preserves_worktree_and_active_run(ctx, monkeypatch):
    approve(ctx)
    tick(ctx)
    mark_running(ctx)
    ctx.process_table.add(124, ppid=123, cwd=ctx.repo_root, start_time=NOW.timestamp() + 1)
    ctx.store.update_run(1, observe=True)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    monkeypatch.setattr(ctx.git, "remove_worktree", lambda path: pytest.fail("group is not stopped"))

    def fail(pid):
        raise PermissionError("cannot terminate group")

    ctx.kill_runner = fail
    tick(ctx)
    assert ctx.store.get_run(1).status == "running"
    assert ctx.store.load_flag(1).reason == Reason.STOP_REFUSED
    assert [event["kind"] for event in recovery_events(ctx)] == ["stop-refused"]


@pytest.mark.parametrize("outcome", ["merged", "blocked", "killed", "limit-wait"])
def test_dead_run_with_recorded_completion_does_not_signal_group(ctx, monkeypatch, outcome):
    from foragent.scheduler import TickReport, _recover_runs

    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    path = ctx.store.worktree_path(1)
    ctx.git.add_worktree(path, run.branch, "origin/main")
    ctx.store.update_run(1, outcome=outcome)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    ctx.kill_runner = lambda pid: pytest.fail("completion does not need another signal")

    _recover_runs(ctx, TickReport())

    assert not path.exists()
    assert ctx.store.get_run(1).status == "finished"
    assert ctx.store.get_run(1).outcome == outcome
    assert recovery_events(ctx) == []


def test_crash_recovery_with_default_killer_tolerates_missing_group(ctx, monkeypatch, fake_process_table):
    fake_process_table.add(4321, cwd=ctx.repo_root, start_time=10)
    approve(ctx)
    tick(ctx)
    run = mark_running(ctx, pid=4321)
    ctx.git.add_worktree(ctx.store.worktree_path(1), run.branch, "origin/main")
    fake_process_table.exit(4321)
    ctx.process_table.exit(ctx.store.get_run(1).pid)
    calls = []

    def missing_group(pid, sig):
        calls.append((pid, sig))
        raise ProcessLookupError()

    monkeypatch.setattr("foragent.processes.os.killpg", missing_group)
    # time.sleep is the global module function; subprocess polling (git fetch on Linux) uses it too.
    # The killpg call list below already proves no SIGKILL wait happened.
    monkeypatch.setattr("foragent.processes.time.sleep", lambda _: None)
    remove_worktree = ctx.git.remove_worktree

    def remove_after_kill(path):
        assert calls == []
        remove_worktree(path)

    monkeypatch.setattr(ctx.git, "remove_worktree", remove_after_kill)
    ctx.kill_runner = TickContext.__dataclass_fields__["kill_runner"].default
    tick(ctx)
    assert ctx.store.get_run(1).status == "finished"
    assert ctx.tracker.get_issue(1).labels == {"approved", "ready"}
    assert not ctx.store.worktree_path(1).exists()
    assert [event["kind"] for event in recovery_events(ctx)] == ["crash-restart"]
    tick(ctx)
    assert calls == []


def test_overdue_blocker_escalation_is_journaled_once(ctx):
    approve(ctx)
    tick(ctx)
    flag(ctx, 1, Reason.STALLED, "Runner stalled again.")
    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.orchestrator_timeout_minutes + 1)
    tick(ctx)
    events = recovery_events(ctx)
    assert len(events) == 1
    assert events[0]["kind"] == "orchestrator-timeout"
    assert events[0]["at"] == ctx.now().isoformat()
    assert events[0]["issue"] == 1 and "needs-human" in events[0]["summary"]
    assert "needs-human" in ctx.tracker.get_issue(1).labels
    tick(ctx)
    assert recovery_events(ctx) == events


@pytest.mark.parametrize("change", ["ready", "closed", "needs-human"])
def test_blocker_escalation_rechecks_local_state_before_escalating(ctx, change):
    from foragent.scheduler import TickReport, _escalate_overdue_blocker

    approve(ctx)
    tick(ctx)
    flag(ctx, 1, Reason.STALLED, "Runner stalled again.")
    stale = ctx.tracker.get_issue(1)
    ctx.now = lambda: NOW + timedelta(minutes=ctx.config.orchestrator_timeout_minutes + 1)
    store = StateStore(ctx.store.root)
    if change == "closed":
        store.update_local_issue(1, state="closed")
    else:
        if change == "ready":
            store.record_labels(1, ["blocked"], remove=True, lanes=ctx.config.lanes)
        store.record_labels(1, [change], remove=False, lanes=ctx.config.lanes)
    before = store.local_issues()
    report = TickReport()

    _escalate_overdue_blocker(ctx, stale, report)

    assert store.local_issues() == before
    assert report.actions == []
    assert recovery_events(ctx) == []


def test_project_ceiling_counts_only_this_repositorys_runs(ctx, tmp_path):
    other = StateStore(tmp_path / "other")
    for number in (1, 2):
        other.start_run(number, branch=f"b{number}", worktree=other.worktree_path(number), pid=8000 + number)
    write_state(ctx.store, 4, NOW - timedelta(hours=1))
    ctx.config = replace(ctx.config, lanes={"alpha": {}, "beta": {}}, max_parallel=2)
    for number, lane in [(1, "main"), (2, "alpha"), (3, "beta")]:
        approve(ctx, number, labels=[f"lane:{lane}"])
    ctx.start_runs = True
    tick(ctx)
    # Machine: 2 elsewhere + 2 here fills the shared 4; the ceiling of 2 is per repository.
    # Lanes rank together by issue number (#603), so main's #1 is not held back.
    assert sorted(run.number for run in ctx.store.active_runs()) == [1, 2]
    tick(ctx)
    assert len(ctx.store.active_runs()) == 2


def test_start_checks_and_github_writes_run_outside_the_machine_lock(ctx, monkeypatch):
    import fcntl

    from foragent import capacity, scheduler

    held = []

    def probe(name):
        with (ctx.store.root.parent / capacity.LOCK_FILE).open("a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(name)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def wrap(owner, name):
        original = getattr(owner, name)

        def call(*args, **kwargs):
            probe(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, call)

    ctx.config = replace(ctx.config, lanes={"alpha": {}})
    approve(ctx, 1)
    approve(ctx, 2, labels=["lane:alpha"])
    ctx.start_runs = True
    for name in ("get_issue", "label_events", "add_labels", "remove_labels"):
        wrap(ctx.tracker, name)
    for name in ("_check_integrity", "_check_prerequisites"):
        wrap(scheduler, name)
    wrap(scheduler.board, "sync_status")
    tick(ctx)
    assert sorted(run.number for run in ctx.store.active_runs()) == [1, 2]
    assert held == []


def test_refresh_tick_reuses_confirmed_reads_and_a_kick_tick_reads_live(ctx, monkeypatch):
    ctx.tracker.add_issue(1, "Blocker")
    approve(ctx, 2)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "open")])
    remote = ctx.tracker._tracker
    reads = []
    for name in ("label_events", "blocked_by", "find_pr"):
        def counted(arg, _name=name, _original=getattr(remote, name)):
            reads.append((_name, arg))
            return _original(arg)
        monkeypatch.setattr(remote, name, counted)
    tick(ctx)
    ctx.now = lambda: NOW + timedelta(minutes=5)
    tick(ctx)
    reads.clear()
    ctx.now = lambda: NOW + timedelta(minutes=10)
    tick(ctx)
    assert reads == [], "A refresh that shows no change confirms the remembered reads"
    ctx.now = lambda: NOW + timedelta(minutes=11)
    tick(ctx)
    assert {name for name, _ in reads} == {"label_events", "blocked_by", "find_pr"}, \
        "Between refreshes nothing confirms a read, so the tick reads live"


def test_closed_blocker_releases_a_dependent_whose_own_facts_did_not_change(ctx):
    ctx.tracker.add_issue(1, "Blocker")
    approve(ctx, 2)
    ctx.tracker.add_blocked_by(2, 1)
    for minutes in (0, 5):
        ctx.now = lambda minutes=minutes: NOW + timedelta(minutes=minutes)
        tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved"}
    ctx.tracker.close_issue(1)
    ctx.now = lambda: NOW + timedelta(minutes=10)
    tick(ctx)
    assert ctx.tracker.get_issue(2).labels == {"approved", "ready"}


@pytest.mark.parametrize("jitter", [timedelta(), timedelta(milliseconds=-300)])
def test_periodic_tick_300_seconds_later_is_a_refresh_tick_despite_fetch_delay(ctx, monkeypatch, jitter):
    clock = [NOW]
    ctx.now = lambda: clock[0]
    fetch = ctx.git.fetch

    def slow_fetch(*args):
        clock[0] += timedelta(seconds=3)  # git fetch takes time before the refresh runs
        return fetch(*args)

    monkeypatch.setattr(ctx.git, "fetch", slow_fetch)
    ctx.tracker.add_issue(1, "Blocker")
    approve(ctx, 2)
    ctx.tracker.set_blocked_by(2, [Dependency(1, "open")])
    remote = ctx.tracker._tracker
    reads = []
    for name in ("label_events", "blocked_by", "find_pr"):
        def counted(arg, _name=name, _original=getattr(remote, name)):
            reads.append((_name, arg))
            return _original(arg)
        monkeypatch.setattr(remote, name, counted)
    for cycle in range(3):  # Two ticks settle the approval snapshot; the third has nothing new.
        reads.clear()
        clock[0] = NOW + cycle * (timedelta(seconds=300) + jitter)
        started = clock[0]
        tick(ctx)
        assert ctx.store.issues_observation()["as_of"] == started.isoformat(), \
            "Each periodic tick refreshes, stamped with its start"
    assert reads == [], "The periodic refresh confirms the earlier reads"


def test_tick_without_ready_work_still_applies_machine_parallel_ceiling(ctx):
    from foragent import capacity, machine_budget

    path = machine_budget.machine_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("max_parallel: 2\n", encoding="utf-8")
    capacity.write_state(ctx.store, 5, NOW - timedelta(hours=1))
    assert not ctx.start_runs
    tick(ctx)
    assert capacity.read_state(ctx.store)["parallel"] == 2


def test_tick_during_github_wait_still_applies_machine_ceilings_once(ctx, monkeypatch):
    from foragent import capacity

    write_machine_yml(max_parallel=2, test_slots=1)
    capacity.write_state(ctx.store, 5, NOW - timedelta(hours=1))
    root = capacity.machine_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps({"slots": 3, "changed_at": NOW.isoformat()}))
    ctx.store.pause_github(NOW + timedelta(minutes=30))
    samples = []
    monkeypatch.setattr("foragent.scheduler.machine_load",
                        lambda: samples.append(1) or capacity.LoadSample("normal", 0.0, 0))
    report = tick(ctx)
    assert report.skipped and "GitHub rate limit" in report.skipped
    assert capacity.read_state(ctx.store)["parallel"] == 2
    assert capacity.read_test_slots()["slots"] == 1
    assert samples == [1], "one sensor sample and one budget update per tick"


def test_tick_during_github_wait_still_rechecks_free_usage(ctx, monkeypatch):
    from foragent import usage_recheck

    until = NOW + timedelta(hours=2)
    usage_recheck.note_limit(ctx.store, "codex", until, [{"name": "5h", "used_percent": 100, "exhausted": True}], NOW)
    ctx.store.pause_executor("codex", until)
    ctx.store.pause_github(NOW + timedelta(minutes=30))
    monkeypatch.setattr(usage_recheck, "read_codex", lambda now: [{"name": "5h", "used_percent": 4}])
    report = tick(ctx)
    assert report.skipped and "GitHub rate limit" in report.skipped
    assert not ctx.store.executor_paused("codex", NOW)
    events = [json.loads(line) for line in (ctx.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events] == ["quota-recovered"]


def test_rate_limit_error_after_budgets_does_not_apply_them_twice(ctx, monkeypatch):
    from foragent import capacity, scheduler
    from foragent.scheduler_start import _apply_budgets

    calls = []
    monkeypatch.setattr("foragent.scheduler.machine_load",
                        lambda: calls.append(1) or capacity.LoadSample("normal", 0.0, 0))

    def locked(context):
        context._budget_sample = None
        _apply_budgets(context, {})
        raise TrackerError("API rate limit exceeded")

    monkeypatch.setattr(scheduler, "_tick_locked", locked)
    monkeypatch.setattr(scheduler.orch_github, "pause", lambda context, exc: NOW + timedelta(minutes=5))
    assert "GitHub rate limit" in tick(ctx).skipped
    assert calls == [1]


@pytest.mark.parametrize("busy", [0.0, 0.5])
def test_tick_memory_hold_history_records_cpu_measurements(ctx, monkeypatch, busy):
    from foragent import capacity

    ctx.config = replace(ctx.config, max_parallel=3, test_slots=1)
    approve(ctx)
    capacity.write_state(ctx.store, 3, NOW)
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample(
        "critical", busy / 0.6, 60, load_average_per_cpu=1.82, cpu_busy=busy,
    ))
    ctx.baseline_launcher = lambda *args, **kwargs: None
    ctx.start_runs = True
    assert tick(ctx).skipped is None
    assert ctx.store.active_runs() == []
    rows = [json.loads(line) for line in (ctx.store.root.parent / capacity.HISTORY_FILE).read_text().splitlines()]
    assert rows == [{
        "at": NOW.isoformat(), "rule": "memory-critical", "pressure": "critical",
        "load_per_cpu": busy / 0.6, "swapins": 60, "load_average_per_cpu": 1.82, "cpu_busy": busy,
    }]


@pytest.mark.parametrize("busy", [0.0, 0.5])
def test_tick_budget_ceiling_history_records_cpu_measurements(ctx, monkeypatch, busy):
    from foragent import capacity

    write_machine_yml(max_parallel=2, test_slots=1)
    capacity.write_state(ctx.store, 5, NOW - timedelta(hours=1))
    root = capacity.machine_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / capacity.TEST_SLOTS_FILE).write_text(json.dumps({"slots": 3, "changed_at": NOW.isoformat()}))
    ctx.store.pause_github(NOW + timedelta(minutes=30))
    monkeypatch.setattr("foragent.scheduler.machine_load", lambda: capacity.LoadSample(
        "normal", busy / 0.6, 0, load_average_per_cpu=1.82, cpu_busy=busy,
    ))
    assert "GitHub rate limit" in tick(ctx).skipped
    for history_root in (root, ctx.store.root.parent):
        rows = [json.loads(line) for line in (history_root / capacity.HISTORY_FILE).read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["load_per_cpu"] == busy / 0.6
        assert rows[0]["load_average_per_cpu"] == 1.82 and rows[0]["cpu_busy"] == busy


def test_tick_during_github_wait_still_expires_the_plan_and_drops_lapsed_pauses(ctx):
    """D95: the tick journals the expiry and derives the plan in force; it never writes plan.json."""
    from foragent import orch_plan

    ctx.store.root.mkdir(parents=True, exist_ok=True)
    (ctx.store.root / "plan.json").write_text(json.dumps({
        "queues": {"main": [3]}, "paused": {}, "width": {}, "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
        "authors": {"queues": {"main": "human"}, "width": {}, "expires_at": "human"},
    }))
    ctx.store.pause_github(NOW + timedelta(minutes=30))
    before = (ctx.store.root / "plan.json").read_text()
    report = tick(ctx)
    assert report.skipped and "GitHub rate limit" in report.skipped
    assert (ctx.store.root / "plan.json").read_text() == before, "The tick never writes the plan"
    assert orch_plan.load(ctx.store, NOW)["queues"] == {}
    kinds = [json.loads(line)["kind"] for line in (ctx.store.root / "orch-events.jsonl").read_text().splitlines()]
    assert kinds.count("plan-expired") == 1

    (ctx.store.root / "plan.json").write_text(json.dumps({
        "queues": {}, "paused": {"main": {"until": NOW.isoformat(), "why": "short", "by": "human"}},
        "width": {"main": 2}, "expires_at": None, "authors": {"queues": {}, "width": {"main": "human"}},
    }))
    tick(ctx)
    stored = json.loads((ctx.store.root / "plan.json").read_text())
    assert list(stored["paused"]) == ["main"], "The tick never writes the plan"
    assert orch_plan.load(ctx.store, NOW)["paused"] == {}
    assert orch_plan.load(ctx.store, NOW)["width"] == {"main": 2}


def _fail_refresh_and_count_reads(ctx, monkeypatch):
    """Make the bulk listing fail and count every issue GET of the next tick (#995)."""
    from collections import Counter

    def listing_down(*args, **kwargs):
        raise TrackerError("HTTP 502 Bad Gateway")

    monkeypatch.setattr(ctx.tracker._tracker, "list_issues", listing_down)
    reads, get_issue = Counter(), ctx.tracker.get_issue

    def counted(number):
        reads[number] += 1
        return get_issue(number)

    monkeypatch.setattr(ctx.tracker, "get_issue", counted)
    return reads


def test_refresh_failure_reads_a_new_approval_never_and_a_stored_one_once(ctx, monkeypatch):
    from foragent import capacity

    approve(ctx)
    assert tick(ctx).skipped is None  # #1's approval snapshot is stored and it is ready.
    approve(ctx, 2)  # #2's approval is new: its snapshot needs a read.
    ctx.now = lambda: NOW + timedelta(minutes=6)
    capacity.write_state(ctx.store, 2, ctx.now())
    reads = _fail_refresh_and_count_reads(ctx, monkeypatch)
    ctx.start_runs = True
    report = tick(ctx)
    assert report.skipped is None
    assert reads == {1: 1}, "After a failed refresh only the start read may GET an issue, once"
    assert [run.number for run in ctx.store.active_runs()] == [1]
    assert ctx.store.load_approval(2) is None


def test_refresh_failure_with_queued_writes_starts_nothing(ctx, monkeypatch):
    from foragent import capacity

    approve(ctx)
    assert tick(ctx).skipped is None
    ctx.store.queue_github_write(1, "comment", body="Queued while GitHub was limited")
    ctx.now = lambda: NOW + timedelta(minutes=6)
    capacity.write_state(ctx.store, 2, ctx.now())
    _fail_refresh_and_count_reads(ctx, monkeypatch)
    launched = []
    ctx.start_runs, ctx.launcher = True, lambda number: launched.append(number) or 7001
    report = tick(ctx)
    assert launched == [] and ctx.store.active_runs() == []
    assert "No run starts this tick: queued GitHub writes are not delivered yet" in report.actions
    assert {"approved", "ready"} <= ctx.tracker._tracker.get_issue(1).labels
    assert ctx.store.github_outbox()  # The queued write waits for a successful refresh.
