"""Acceptance test for talk-state. Locked by Foragent; do not edit."""

from dataclasses import asdict
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex
import subprocess

from test_claude_sessions import SESSION_ID, THREAD_ID, sessions_offline
from test_design_session import TerminalCommands
from test_local_truth import record_gh
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


class TalkCommands(TerminalCommands):
    """Reuse the fake opener and intercept Codex queue without executing a worker."""

    def __init__(self, monkeypatch):
        import shutil

        super().__init__(monkeypatch)
        self.queues, self.terminal_cwds = [], []
        monkeypatch.setattr(shutil, "which", lambda name: f"/offline/{name}"
                            if name in {"orca", "codex", "claude"} else None)
        original = subprocess.run

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv:
                if Path(argv[0]).name == "codex":
                    assert "queue" in argv, "Running workers receive messages through codex queue"
                    self.queues.append((list(argv), kwargs))
                    return subprocess.CompletedProcess(argv, 0, stdout="queued\n", stderr="")
                if Path(argv[0]).name == "orca" and argv[1:3] in (
                    ["terminal", "create"], ["terminal", "split"],
                ):
                    self.terminal_cwds.append(kwargs.get("cwd"))
            return original(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)

    def opened(self):
        return [argv for argv in self.calls if argv[1:3] in (
            ["terminal", "create"], ["terminal", "split"],
        )]


def talk_case(git_history, tmp_path, monkeypatch, *, executor="codex", state="running",
              worktree=True, session=True, **config):
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="claude" if executor == "codex" else "codex",
                     language="en", skill_libraries=[], test_command="true", **config)
    if worktree:
        case.store.worktree_path(1).mkdir(parents=True, exist_ok=True)
    if state == "running":
        log = case.store.log_path(1)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("worker output\n", encoding="utf-8")
        os.utime(log, (case.clock[0].timestamp(), case.clock[0].timestamp()))
    if state != "running":
        case.tracker.remove_labels(1, ["running"])
        if state == "finished":
            case.store.finish_run(1, "merged")
            case.tracker.close_issue(1)
        else:
            case.store.finish_run(1, "blocked")
            case.tracker.add_labels(1, ["blocked"])
    if session:
        # Seed the persisted contract so CLI tests reach the missing command
        # independently of session extraction (covered in claude-sessions).
        run = asdict(case.store.get_run(1))
        run.update(session_id=THREAD_ID if executor == "codex" else SESSION_ID,
                   session_executor=executor,
                   session_ids=[THREAD_ID if executor == "codex" else SESSION_ID])
        (case.store.root / "runs/1.json").write_text(json.dumps(run), encoding="utf-8")
        if state != "running":
            (case.store.root / "runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    main = bind_cli(case, monkeypatch)
    commands = TalkCommands(monkeypatch)
    gh = record_gh(monkeypatch)
    return case, main, commands, gh


def opened_command(commands):
    opened = commands.opened()
    assert len(opened) == 1, commands.calls
    tab = opened[0]
    return tab, shlex.split(tab[tab.index("--command") + 1])


def assert_live_log(commands, case):
    _, command = opened_command(commands)
    assert "resume" not in command and "--resume" not in command, command
    if Path(command[0]).name == "tail":
        assert "-f" in command and str(case.store.log_path(1)) in command, command
    else:
        # A Foragent log pane may own following the same issue log.
        assert Path(command[0]).name in {"fa", "foragent"} or Path(command[0]).name.startswith("python"), command
        assert any("log" in arg for arg in command), command
        assert "1" in command or str(case.store.log_path(1)) in command, command
        assert str(case.root) in command, command


def test_talk_state_running_codex_opens_live_log_and_queues_message_to_recorded_thread(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands, gh = talk_case(git_history, tmp_path, monkeypatch)
    message = "Keep the public API; preserve these spaces."
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root),
                           "--message", message]) == 0
    assert_live_log(commands, case)
    assert len(commands.queues) == 1
    argv, options = commands.queues[0]
    assert "queue" in argv and THREAD_ID in argv, argv
    assert message in argv or options.get("input") == message
    assert options.get("shell", False) is False
    run = case.store.get_run(1)
    assert run.talking is True and run.status == "running" and run.session_id == THREAD_ID
    assert not (case.store.root / "hints/1.txt").exists()
    assert gh == []


def test_talk_state_running_claude_saves_hint_for_next_attempt_and_says_so_in_one_line(
    git_history, tmp_path, monkeypatch, capsys,
):
    from foragent.runner import run_issue

    case, main, commands, gh = talk_case(git_history, tmp_path, monkeypatch,
                                        executor="claude", worktree=False)
    message = "Preserve the Korean labels on the next attempt."
    labels = set(case.tracker.get_issue(1).labels)
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root),
                           "--message", message]) == 0
    output = capsys.readouterr()
    lines = (output.out + output.err).strip().splitlines()
    assert len(lines) == 1 and "next" in lines[0].lower(), lines
    assert any(word in lines[0].lower() for word in ("attempt", "retry")), lines
    assert (case.store.root / "hints/1.txt").read_text(encoding="utf-8") == message
    assert_live_log(commands, case)
    assert commands.queues == [] and case.tracker.get_issue(1).labels == labels
    assert case.store.get_run(1).status == "running"
    assert run_issue(1, case.ctx) == "merged"
    assert message in case.implementer.calls[0]["prompt"]
    assert gh == []


def test_talk_state_finished_or_blocked_resumes_each_executor_in_worktree_or_repo_root(
    git_history, tmp_path, monkeypatch,
):
    scenarios = [("codex", "finished", True), ("claude", "finished", False),
                 ("codex", "blocked", False), ("claude", "blocked", True)]
    for executor, state, has_worktree in scenarios:
        with monkeypatch.context() as patch:
            case, main, commands, gh = talk_case(
                git_history, tmp_path / f"{executor}-{state}", patch,
                executor=executor, state=state, worktree=has_worktree,
            )
            assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
            tab, command = opened_command(commands)
            expected = ["codex", "resume", THREAD_ID] if executor == "codex" else [
                "claude", "--resume", SESSION_ID,
            ]
            assert [Path(command[0]).name, *command[1:3]] == expected, command
            cwd = Path(tab[tab.index("--cwd") + 1]) if "--cwd" in tab else Path(commands.terminal_cwds[0])
            assert cwd == (case.store.worktree_path(1) if has_worktree else case.root)
            assert commands.queues == [] and gh == []


def test_talk_state_live_terminal_pauses_stall_detection_and_closure_restarts_its_clock(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from foragent.state import StateStore

    case, main, commands, gh = talk_case(git_history, tmp_path, monkeypatch,
                                        worktree=False, stall_minutes=3)
    # PID existence is a fact supplied by this fixture, never a real process probe.
    monkeypatch.setattr("foragent.scheduler.os.kill", lambda pid, signal: None)
    # Read the fixture JSON before the new Run fields are available today.
    path = case.store.root / "runs/1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pid"] = 88001
    path.write_text(json.dumps(record), encoding="utf-8")
    log = case.store.log_path(1)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("last output before the conversation\n", encoding="utf-8")
    os.utime(log, (case.clock[0].timestamp(), case.clock[0].timestamp()))
    case.clock[0] += timedelta(minutes=5)
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root)]) == 0
    assert_live_log(commands, case)
    assert StateStore(case.store.root).get_run(1).talking is True
    ctx = tick_context(case, start_runs=False)
    killed = []
    ctx.kill_runner = killed.append
    case.clock[0] += timedelta(minutes=30)
    report = tick(ctx)
    assert killed == [] and not any("stall" in action for action in report.actions)
    assert case.store.get_run(1).talking is True
    commands.alive = False
    tick(ctx)
    run = StateStore(case.store.root).get_run(1)
    assert run.talking is False and run.status == "running" and killed == []
    assert any(argv[1:3] == ["terminal", "show"] for argv in commands.calls)
    case.clock[0] += timedelta(minutes=2)
    tick(ctx)
    assert killed == []
    case.clock[0] += timedelta(minutes=2)
    report = tick(ctx)
    assert killed == [88001] and any("stall" in action for action in report.actions)
    assert gh == []


def test_talk_state_missing_session_is_one_line_nonzero_and_opens_nothing(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main, commands, gh = talk_case(git_history, tmp_path, monkeypatch, session=False)
    before = {path.relative_to(case.store.root): path.read_bytes()
              for path in case.store.root.rglob("*") if path.is_file()}
    assert cli_exit(main, ["lanes", "talk", "1", "--repo-root", str(case.root)]) != 0
    output = capsys.readouterr()
    lines = (output.out + output.err).strip().splitlines()
    assert len(lines) == 1 and "session" in lines[0].lower(), lines
    after = {path.relative_to(case.store.root): path.read_bytes()
             for path in case.store.root.rglob("*") if path.is_file()}
    assert after == before and commands.calls == [] and commands.queues == [] and gh == []
