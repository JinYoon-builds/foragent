"""Acceptance test for orch-stop. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import timedelta

import pytest

from conftest import ACCEPTANCE, git_command
from foragent import orch, runner, scheduler
from test_keep_unfinished_work import (
    BRANCH, CHANGES, _assert_saved, _case as keep_work_case, _question, _write_changes,
)
from test_orch_review_handoff import bind_cli, tick_context
from test_stop_whole_tree import FakeProcessTable, events, fake_processes, record_tree, stop_context


def _case(git_history, tmp_path, monkeypatch, table):
    # #270's wrapper uses make_case and retains WIP messages via fast-import.
    case = keep_work_case(git_history, tmp_path, monkeypatch, executor="codex")
    case.git.fetch()
    case.tracker.record_label(1, "approved", "human")
    tree = record_tree(case, table)
    _write_changes({"worktree": case.store.worktree_path(1)}, stage=True)
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    return case, tree, main


def _invoke(main, argv, capsys):
    try:
        code = main(["orch", "stop", *argv])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    assert "invalid choice: 'stop'" not in captured.err, "fa orch stop verb is missing"
    return code, captured.out + captured.err


def _snapshot(case):
    """Semantic state, branch refs and dirty files must survive a refusal intact."""
    worktree = case.store.worktree_path(1)
    return {
        "issues": [asdict(case.tracker.get_issue(n)) for n in sorted(case.tracker.issues)],
        "runs": [asdict(case.store.get_run(n)) if case.store.get_run(n) else None for n in (1, 2)],
        "history": [asdict(run) for run in case.store.read_history()],
        "events": events(case),
        "refs": git_command(case.root, "show-ref"),
        "origin_refs": git_command(case.history.origin, "show-ref"),
        "status": git_command(worktree, "status", "--porcelain=v1", "-z"),
        "files": {str(path.relative_to(worktree)): path.read_bytes()
                  for path in worktree.rglob("*") if path.is_file() and path.name != ".git"},
        "comments": {n: [asdict(comment) for comment in case.tracker.list_comments(n)]
                     for n in case.tracker.issues},
        "outbox": case.store.github_outbox(),
    }


def test_orch_stop_keeps_wip_uses_scheduler_path_and_next_tick_resumes(
    git_history, tmp_path, monkeypatch, fake_processes, capsys,
):
    case, tree, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    why = "Keep the locale work while we reprioritize — 우선순위 변경"
    worktree = case.store.worktree_path(1)
    # Exercise #270's locked-file restoration even when the executor staged it.
    (worktree / ACCEPTANCE).write_text("def test_feature():\n    assert False\n", encoding="utf-8")
    git_command(worktree, "add", "-A")
    protected = fake_processes.host_pids | {
        fake_processes.add(43000, cwd=worktree, start_time=0).pid,
    }
    kill_calls = []
    kill_run = scheduler._kill_run

    def traced_kill(ctx, number, report, **kwargs):
        kill_calls.append((number, kwargs))
        return kill_run(ctx, number, report, **kwargs)

    # Wrap, never replace, the real scheduler termination/preservation path.
    monkeypatch.setattr(scheduler, "_kill_run", traced_kill)
    monkeypatch.setattr(orch, "_kill_run", traced_kill, raising=False)
    code, output = _invoke(main, ["1", "--keep-work", "--why", why], capsys)
    assert code == 0, output
    assert len(kill_calls) == 1 and kill_calls[0][0] == 1, "orch stop bypassed scheduler._kill_run"
    assert kill_calls[0][1].get("outcome") == "stopped", kill_calls
    assert not {row.pid for row in tree} & fake_processes.live, "Stop left a run descendant alive"
    assert protected <= fake_processes.live, "Stop touched an unowned process"
    run = case.store.get_run(1)
    assert (run.status, run.outcome) == ("finished", "stopped"), run
    labels = case.tracker.get_issue(1).labels
    assert "ready" in labels and not labels & {"running", "blocked", "needs-human"}, labels
    stopped = [event for event in events(case) if event["kind"] == "stop" and event["issue"] == 1]
    assert len(stopped) == 1 and why in stopped[0]["summary"], stopped
    _assert_saved(case, why)
    saved_head = git_command(case.root, "rev-parse", BRANCH)

    # The next real tick queues the runner; launching is only fake process I/O.
    case.clock[0] += timedelta(minutes=1)
    ctx = tick_context(case)
    ctx.kill_runner = stop_context(case, fake_processes).kill_runner
    ctx.baseline_launcher = lambda *args: pytest.fail("Unexpected baseline process")
    launched = []

    def launch(number):
        launched.append(number)
        row = fake_processes.add(45000, cwd=case.root, start_time=case.clock[0].timestamp(),
                                 marker=f"owner/repo#{number}")
        return row.pid

    ctx.launcher = launch
    report = scheduler.tick(ctx)
    assert launched == [1], report.actions
    assert case.store.get_run(1).status == "running"
    assert git_command(case.root, "rev-parse", BRANCH) == saved_head

    observed = []

    def continue_work(kwargs):
        directory = kwargs["worktree"]
        observed.append({name: (directory / name).read_text(encoding="utf-8")
                         if (directory / name).exists() else None for name in CHANGES})
        assert not (directory / ".foragent/session.log").exists()
        _question(directory)  # Stop before unrelated checks/publication.

    # Baseline execution is unrelated process I/O; preserve actual runner setup
    # and the fake implementer so files are inspected at its first invocation.
    monkeypatch.setattr("foragent.run_implementation._capture_acceptance_baseline", lambda *a, **kw: None)
    case.implementer.steps = [continue_work]
    assert runner.run_issue(1, case.ctx) == "spec-question"
    assert observed == [CHANGES], "The next run discarded WIP before invoking the implementer"
    assert len(case.implementer.calls) == 1


@pytest.mark.parametrize("state", ["finished", "never-started"])
def test_orch_stop_refuses_nonrunning_issue_without_changes(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, state,
):
    case, _, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    number = 1
    if state == "finished":
        case.store.finish_run(1, "killed")
        case.tracker.remove_labels(1, ["running"])
    else:
        number = 2
        case.tracker.add_issue(2, "Never started", labels=["ready"])
    before, alive = _snapshot(case), fake_processes.live
    code, output = _invoke(main, [str(number), "--keep-work", "--why", "Change priorities"], capsys)
    assert code != 0 and "running" in output.lower(), output
    assert _snapshot(case) == before, "Refusing a nonrunning issue changed its state or work"
    assert not fake_processes.signals and fake_processes.live == alive


def test_orch_stop_requires_why_without_changes(git_history, tmp_path, monkeypatch, fake_processes, capsys):
    case, _, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    before, alive = _snapshot(case), fake_processes.live
    code, output = _invoke(main, ["1", "--keep-work"], capsys)
    assert code != 0 and "--why" in output, "orch stop must require an explicit --why"
    assert _snapshot(case) == before, "Missing --why changed the run or unfinished work"
    assert not fake_processes.signals and fake_processes.live == alive


@pytest.mark.parametrize("target", FakeProcessTable.unsafe_targets)
@pytest.mark.parametrize("recorded_as", ["runner", "executor"])
def test_orch_stop_refuses_unsafe_recorded_pid_without_changes(
    git_history, tmp_path, monkeypatch, fake_processes, capsys, target, recorded_as,
):
    case, _, main = _case(git_history, tmp_path, monkeypatch, fake_processes)
    field = "pid" if recorded_as == "runner" else "executor_pgid"
    case.store.update_run(1, **{field: fake_processes.unsafe_targets[target]})
    before, alive = _snapshot(case), fake_processes.live
    code, output = _invoke(main, ["1", "--keep-work", "--why", "Change priorities"], capsys)
    assert code != 0, f"Unsafe recorded PID ({target}) was accepted: {output}"
    assert not fake_processes.signals, f"Unsafe stop sent a signal attempt: {fake_processes.signals}"
    assert fake_processes.live == alive, "Unsafe stop touched another process"
    assert _snapshot(case) == before, "Unsafe stop changed labels, records, events or WIP"
