"""Acceptance test for closed-issue-stops-its-run.

An issue can be closed outside the gate (a person merges its work by hand, or
closes it) while its run is still working. The gate tick already records that
close. The next tick(s) must then stop that run the way `fa orch stop N
--keep-work` stops one: the unfinished work stays on the issue branch, the
run's processes end, the run no longer holds a slot, the issue gets no lane
hold, and the saved work names the close as the reason. A running issue that
is still open is not touched.

Real tick, real state store, real Git history (a local bare origin); the
tracker and the process table are the project's fakes.
"""

from datetime import timedelta

from conftest import git_command
from test_keep_unfinished_work import BRANCH, _expected_tree, _tree
from test_orch_stop import _case
from test_stop_sees_running import gate_status
from test_stop_whole_tree import events, fake_processes, stop_context  # noqa: F401 - fixture

OTHER = 2
OTHER_BRANCH = f"foragent/issue-{OTHER}"
OTHER_FILE = "other-work.txt"


def _start_other_open_run(case, table):
    """A second running issue that stays open, with its own worktree and process tree."""
    body = case.tracker.get_issue(1).body
    case.tracker.add_issue(OTHER, "Other running work", body, labels=["approved", "running"])
    worktree = case.store.worktree_path(OTHER)
    case.git.add_worktree(worktree, OTHER_BRANCH, "origin/main")
    (worktree / OTHER_FILE).write_text("Still being written.\n", encoding="utf-8")
    start = case.clock[0].timestamp()
    root = table.add(46000, cwd=worktree, start_time=start,
                     argv=("fa", "gate", "run-issue", str(OTHER)), marker=f"owner/repo#{OTHER}")
    executor = table.add(46001, ppid=root.pid, cwd=worktree, start_time=start + 1)
    case.store.start_run(OTHER, branch=OTHER_BRANCH, worktree=worktree, pid=root.pid)
    case.store.update_run(OTHER, executor_pgid=executor.pid)
    return worktree, {root.pid, executor.pid}


def test_issue_closed_outside_the_gate_stops_its_run_and_keeps_work_open_run_untouched(
    git_history, tmp_path, monkeypatch, fake_processes, capsys,
):
    case, tree, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    other_worktree, other_pids = _start_other_open_run(case, fake_processes)
    unrelated = fake_processes.live - {row.pid for row in tree} - other_pids
    before = {row["number"] for row in gate_status(main, capsys)["active_runs"]}
    assert before == {1, OTHER}, before

    # Outside the gate: a person merged #1's work by hand and closed the issue.
    case.clock[0] += timedelta(minutes=1)
    case.tracker.close_issue(1, "Merged by hand in a direct PR.")

    launched = []

    def launch(number):
        launched.append(number)
        return 47000 + number

    for _ in range(2):  # Within one tick of the close being observed; a second tick changes nothing.
        case.clock[0] += timedelta(minutes=1)
        ctx = stop_context(case, fake_processes, start_runs=True)
        ctx.launcher = launch
        from foragent import scheduler

        scheduler.tick(ctx)

    # The closed issue's run is stopped: workers gone, slot free, record finished once.
    run = case.store.get_run(1)
    assert run is not None and run.status == "finished", (
        f"The run of an issue closed outside the gate kept working: {run}"
    )
    assert not {row.pid for row in tree} & fake_processes.live, "The closed issue's workers are still alive"
    assert unrelated <= fake_processes.live, "Stopping the closed issue's run touched an unrelated process"
    active = {row["number"] for row in gate_status(main, capsys)["active_runs"]}
    assert 1 not in active, f"The closed issue's run still holds a slot: {active}"
    assert len([past for past in case.store.read_history() if past.number == 1]) == 1, (
        "The stop must be recorded exactly once"
    )
    assert 1 not in launched, "A closed issue must not be started again"

    # Its work is kept on its branch (local and origin), once, with the close as the reason.
    head = git_command(case.root, "rev-parse", BRANCH)
    assert head != case.base, "The closed issue's unfinished work was discarded"
    assert _tree(case.root, head) == _expected_tree(case), "The saved work must be exactly the run's changes"
    assert git_command(case.root, "rev-list", "--count", f"{case.base}..{head}") == "1", (
        "The unfinished work must be saved exactly once"
    )
    message = git_command(case.root, "log", "-1", "--format=%B", head)
    assert "closed" in message.lower(), f"The saved work must name the close as its reason: {message!r}"
    assert git_command(case.history.origin, "rev-parse", f"refs/heads/{BRANCH}") == head
    assert git_command(case.history.origin, "rev-parse", "main") == case.base, "Stopping must not merge anything"

    # The issue stays closed with no lane hold and is not queued again.
    issue = case.tracker.get_issue(1)
    assert issue.state == "closed", "Stopping the run must not reopen the issue"
    assert not issue.labels & {"running", "ready", "blocked", "needs-human"}, issue.labels

    # The open issue's run is untouched.
    other = case.store.get_run(OTHER)
    assert other is not None and other.status == "running", f"An open issue's run was stopped: {other}"
    assert other_pids <= fake_processes.live, "An open issue's workers were signalled"
    assert OTHER in active, f"An open issue's run lost its slot: {active}"
    assert (other_worktree / OTHER_FILE).read_text(encoding="utf-8") == "Still being written.\n"
    assert "running" in case.tracker.get_issue(OTHER).labels
    assert not [event for event in events(case) if event.get("issue") == OTHER], (
        "An open issue's run must get no stop record"
    )
