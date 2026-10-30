"""Park as the orchestrator's response to a blocker, without external services."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foragent import orch_park
from foragent.config import Config
from foragent.orch import OrchError
from foragent.orch_events import append_event
from foragent.scheduler import TickContext, TickReport, _escalate_overdue_blocker
from foragent.state import StateStore
from foragent.tracker import FakeTracker

START = datetime(2026, 10, 21, 9, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    now = lambda: START
    context = TickContext(
        repo="owner/repo", repo_root=tmp_path, tracker=FakeTracker(now=now),
        store=StateStore(tmp_path / "state", now=now),
        config=Config(orchestrator_timeout_minutes=5),
        git=SimpleNamespace(fetch=lambda *args: None), now=now,
        launcher=lambda number: pytest.fail("A blocked issue must not start"),
    )
    context.tracker.add_issue(1, "Blocked work", labels=["blocked"])
    context.store.record_labels(1, ["blocked"], remove=False, lanes=context.config.lanes)
    return context



def _escalate(ctx, minutes: int):
    ctx.now = lambda: START + timedelta(minutes=minutes)
    issue = ctx.tracker.get_issue(1)
    return _escalate_overdue_blocker(ctx, issue, TickReport())


@pytest.mark.parametrize("park_minute,escalated", [(1, False), (0, True)])
def test_park_newer_than_the_blocker_answers_it_even_if_the_label_lingers(ctx, park_minute, escalated):
    rows = ctx.store.local_issues()
    rows[0]["comments"] = [{"id": 1, "body": "<!-- foragent:blocked reason=spec-question -->\nx",
                            "created_at": START.isoformat(), "author": "bot"}]
    ctx.store.local_issues = lambda: rows
    if park_minute:
        append_event(ctx.store, START + timedelta(minutes=park_minute), 1, "parked", "#1: parked; why: wait")
    else:
        append_event(ctx.store, START - timedelta(minutes=1), 1, "parked", "#1: parked; why: older")
    issue = _escalate(ctx, 10)
    assert ("needs-human" in issue.labels) == escalated


def test_parked_at_and_is_parked_follow_the_journal(ctx):
    assert orch_park.parked_at(ctx.store, 1) is None and not orch_park.is_parked(ctx.store, 1)
    append_event(ctx.store, START, 1, "parked", "p")
    append_event(ctx.store, START, 2, "unparked", "other issue")
    assert orch_park.parked_at(ctx.store, 1) == START and orch_park.is_parked(ctx.store, 1)
    append_event(ctx.store, START + timedelta(minutes=1), 1, "unparked", "u")
    assert not orch_park.is_parked(ctx.store, 1)
    assert orch_park.parked_at(ctx.store, 1) == START


def test_unpark_without_park_changes_nothing(ctx):
    ctx.tracker.add_labels(1, ["waiting-person"])
    with pytest.raises(OrchError, match="not parked"):
        orch_park.unpark(ctx, 1)
    assert "waiting-person" in ctx.tracker.get_issue(1).labels
    assert not (ctx.store.root / "orch-events.jsonl").exists()


def test_park_refuses_a_closed_issue(ctx):
    ctx.tracker.close_issue(1, "done")
    with pytest.raises(OrchError):
        orch_park.park(ctx, 1, "why")
    assert not (ctx.store.root / "orch-events.jsonl").exists()


def test_park_removes_approval_with_the_blocker_labels(ctx):
    ctx.tracker.add_labels(1, ["approved", "needs-human", "ready"])
    orch_park.park(ctx, 1, "redesign first")
    assert ctx.tracker.get_issue(1).labels == {"ready"}


def test_unpark_does_not_restore_approval(ctx):
    ctx.tracker.add_labels(1, ["approved"])
    orch_park.park(ctx, 1, "person decides", for_person=True)
    orch_park.unpark(ctx, 1)
    assert ctx.tracker.get_issue(1).labels == set()


def _running(ctx, monkeypatch, *, alive: bool, labels=("running",)):
    from foragent import processes, scheduler_stop
    from foragent.processes import ProcessIdentity, ProcessTable

    ctx.tracker.remove_labels(1, ["blocked"])
    ctx.tracker.add_labels(1, list(labels))
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.repo_root / "wt")
    ctx.store.update_run(1, pid=42000, process_identities=[{"pid": 42000, "start_time": 5.0}])
    rows = (ProcessIdentity(42000, 1, 42000, 42000, 5.0),) if alive else ()
    monkeypatch.setattr(processes, "list_processes", lambda: ProcessTable(rows))
    monkeypatch.setattr(scheduler_stop, "_pid_exists", lambda pid: False)


def _status_state(ctx) -> str:
    from foragent.status import collect_lanes
    from foragent.tracker import Issue

    issue = Issue(1, "Blocked work", "", frozenset(ctx.tracker.get_issue(1).labels), "open")
    lanes, _ = collect_lanes([issue], {"running": [{"number": 1}]}, {}, ctx.store, ctx.config, START)
    return lanes["main"]["state"]


def test_park_status_and_stop_agree_a_live_run_is_running(ctx, monkeypatch):
    from foragent.scheduler_stop import run_is_live

    _running(ctx, monkeypatch, alive=True)
    assert run_is_live(ctx.store.get_run(1))
    with pytest.raises(OrchError, match="is running"):
        orch_park.park(ctx, 1, "wait")
    assert _status_state(ctx) == "working"


def test_park_status_and_stop_agree_a_dead_run_is_not_running(ctx, monkeypatch):
    """#435: a running label and record whose processes are all gone is not running anywhere."""
    from foragent import orch_stop
    from foragent.scheduler_stop import run_is_live

    _running(ctx, monkeypatch, alive=False, labels=("running", "approved"))
    assert not run_is_live(ctx.store.get_run(1))
    assert _status_state(ctx) != "working"
    ctx.monotonic, ctx.sleep = (lambda: 0.0), (lambda seconds: None)
    with pytest.raises(OrchError, match="not running"):
        orch_stop.stop(ctx, 1, "stuck")
    orch_park.park(ctx, 1, "wait")
    assert "approved" not in ctx.tracker.get_issue(1).labels


def test_park_status_and_stop_agree_a_record_without_processes_is_running(ctx, monkeypatch):
    """#435: a running record with neither runner PID nor executor group, in every reader."""
    from foragent import scheduler, status
    from foragent.scheduler_stop import run_is_live

    ctx.tracker.remove_labels(1, ["blocked"])
    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.repo_root / "wt")
    assert run_is_live(ctx.store.get_run(1))
    with pytest.raises(OrchError, match="is running"):
        orch_park.park(ctx, 1, "wait")
    assert _status_state(ctx) == "working"
    assert [run["number"] for run in status.collect(ctx.store, ctx.config, START)["active_runs"]] == [1]
    stopped = []
    monkeypatch.setattr(scheduler, "_kill_run", lambda c, n, report, **kw: stopped.append(n) or True)
    monkeypatch.setattr(scheduler, "TickContext", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("foragent.scheduler_stop._restore_stopped", lambda *a: None)
    ctx.monotonic, ctx.sleep = (lambda: 0.0), (lambda seconds: None)
    from foragent import orch_stop
    orch_stop.stop(ctx, 1, "stuck")
    assert stopped == [1]


def test_status_active_runs_drop_a_run_whose_processes_are_gone(ctx, monkeypatch):
    from foragent import status

    _running(ctx, monkeypatch, alive=False)
    assert status.collect(ctx.store, ctx.config, START)["active_runs"] == []


def _live_with(ctx, monkeypatch, rows, *, pid=42000, pgid=None, identities=((42000, 5.0),)):
    from foragent import processes, scheduler_stop
    from foragent.processes import ProcessIdentity, ProcessTable
    from foragent.scheduler_stop import run_is_live

    ctx.store.start_run(1, branch="foragent/issue-1", worktree=ctx.repo_root / "wt")
    ctx.store.update_run(1, pid=pid, executor_pgid=pgid,
                         process_identities=[{"pid": p, "start_time": t} for p, t in identities])
    table = ProcessTable(tuple(ProcessIdentity(*row) for row in rows))
    monkeypatch.setattr(processes, "list_processes", lambda: table)
    # Any probe that ignores the table's identities would wrongly report the reused PID alive.
    monkeypatch.setattr(scheduler_stop, "_pid_exists", lambda pid: True)
    return run_is_live(ctx.store.get_run(1))


def test_a_reused_runner_pid_is_not_running_in_park_status_or_stop(ctx, monkeypatch):
    """Defect class: identity-blind liveness. The PID now belongs to a process with another start time."""
    from foragent import orch_stop

    ctx.tracker.remove_labels(1, ["blocked"])
    ctx.tracker.add_labels(1, ["running", "approved"])
    assert not _live_with(ctx, monkeypatch, [(42000, 1, 42000, 42000, 99.0)])
    assert _status_state(ctx) != "working"
    ctx.monotonic, ctx.sleep = (lambda: 0.0), (lambda seconds: None)
    with pytest.raises(OrchError, match="not running"):
        orch_stop.stop(ctx, 1, "stuck")
    orch_park.park(ctx, 1, "wait")
    assert "approved" not in ctx.tracker.get_issue(1).labels


def test_a_matching_runner_identity_is_running(ctx, monkeypatch):
    assert _live_with(ctx, monkeypatch, [(42000, 1, 42000, 42000, 5.0)])


def test_a_reused_executor_pgid_is_not_running(ctx, monkeypatch):
    # The runner is gone; PGID 500's leader and its member belong to a new group.
    rows = [(500, 1, 500, 500, 9.0), (501, 500, 500, 500, 10.0)]
    assert not _live_with(ctx, monkeypatch, rows, pgid=500, identities=((42000, 5.0), (500, 7.0)))


def test_the_original_executor_group_after_its_leader_exits_is_running(ctx, monkeypatch):
    # A PGID cannot be reused while a member remains, so the member is ours.
    rows = [(501, 1, 500, 500, 10.0)]
    assert _live_with(ctx, monkeypatch, rows, pgid=500, identities=((42000, 5.0), (500, 7.0)))


def test_a_runner_without_a_birth_identity_is_running_while_present(ctx, monkeypatch):
    assert _live_with(ctx, monkeypatch, [(42000, 1, 42000, 42000, 99.0)], identities=())
    assert not _live_with(ctx, monkeypatch, [], identities=())
